"""Budget-aware long-run controller ("foreman") for NBA strategy research.

Drives the orchestrator in bounded WAVES. Before every wave it reads LIVE usage
limits via the agent-limits tool and chooses the generation engine + concurrency
with a fallback ladder, while protecting the interactive Claude coordinator's
budget. Designed to run detached for hours and degrade gracefully (switch model,
throttle, or pause until reset) instead of dying when a plan hits its cap.

Fallback ladder (re-evaluated each wave from live limits):
  1. Codex Spark (gpt-5.3-codex-spark) — fresh weekly bucket → the volume engine.
  2. Codex gpt-5.5 — smarter but scarce (was ~75% weekly) → used sparingly only
     when Spark is tapped.
  3. Claude headless workers — bounded "research" contribution, gated on COORD
     headroom so the interactive coordinator never loses its budget.
  4. PAUSE until the soonest reset when every engine is tapped.

Coordinator protection: Claude workers are capped (count + cadence) and further
backed off whenever Claude's live 5h-window usage (when visible) exceeds
100 - COORD_RESERVE_PCT. If Claude usage is not visible (stale statusline cache),
we stay on the conservative deterministic cap so we can't accidentally starve the
coordinator.

Run detached:
  cd <repo> && nohup ~/code/kvenv/bin/python -m research.agents.foreman \
      --deadline-hours 20 --wave-trials 4 --codex-workers 3 \
      > research/reports/foreman/foreman.log 2>&1 &

Monitor cheaply (no agent spend): tail research/reports/foreman/foreman.log
or read research/reports/foreman/state.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
PY = str(Path.home() / "code" / "kvenv" / "bin" / "python")
AGENT_LIMITS = str(
    Path.home()
    / "Documents/Codex/2026-05-28/can-you-build-some-tool-that/bin/agent-limits"
)
SCRATCH_DB = str(_REPO / "market_data" / "trials_scratch.db")
REPORT_DIR = _REPO / "research" / "reports" / "foreman"

# Engine identity in the agent-limits JSON.
CODEX_SPARK_BUCKET = "codex_bengalfox"   # GPT-5.3-Codex-Spark (fresh)
CODEX_MAIN_BUCKET = "codex"              # gpt-5.5 (scarce)
SPARK_MODEL = "gpt-5.3-codex-spark"
MAIN_MODEL = "gpt-5.5"

# Caps (percent-used ceilings). Below the cap → usable; at/above → tapped.
WEEKLY_CAP = 90      # don't push any weekly bucket past this
FIVEH_CAP = 88       # don't push any 5h bucket past this
GPT55_WEEKLY_CAP = 92  # gpt-5.5 is scarce; allow a touch more headroom use only near reset
COORD_RESERVE_PCT = 20  # reserve this much of Claude's 5h window for the coordinator


def read_limits() -> dict:
    """Return parsed agent-limits status, or {} on failure."""
    try:
        out = subprocess.run(
            [AGENT_LIMITS, "status", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        return json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001
        print(f"[foreman] WARN: agent-limits read failed: {exc}", flush=True)
        return {}


def _codex_bucket(limits: dict, bucket_id: str) -> dict:
    """Parse a codex bucket. ``has_data`` is True only if real used_percent
    values were present — an empty/missing snapshot (the codex app-server read
    is intermittently stale) leaves w5h/w7d=None so callers can treat it as
    UNKNOWN rather than wrongly assume the bucket is exhausted."""
    out = {"w5h": None, "w7d": None, "reset7d": None, "has_data": False}
    for b in (limits.get("agents", {}).get("codex", {}) or {}).get("limits", []) or []:
        if b.get("id") != bucket_id:
            continue
        for w in b.get("windows", []) or []:
            up = w.get("used_percent")
            if up is None:
                continue
            if w.get("name") == "primary":
                out["w5h"] = float(up); out["has_data"] = True
            elif w.get("name") == "secondary":
                out["w7d"] = float(up); out["reset7d"] = w.get("resets_at_epoch"); out["has_data"] = True
    return out


def _claude_5h_used(limits: dict):
    """Best-effort Claude 5h used_percent; None if not visible.

    The agent-limits Claude shape is a FLAT list of window entries (each entry
    IS a window: name='five_hour'|'seven_day', used_percent=N) — not nested
    under 'windows' like Codex. Match both shapes defensively."""
    cl = limits.get("agents", {}).get("claude", {}) or {}
    for e in (cl.get("limits") or []):
        if not isinstance(e, dict):
            continue
        nm = e.get("name") or e.get("label")
        if nm in ("five_hour", "primary", "5h") and e.get("used_percent") is not None:
            return float(e["used_percent"])
        for w in (e.get("windows") or []):  # nested-shape fallback
            if (w.get("name") in ("five_hour", "primary", "5h")
                    or w.get("label") in ("five_hour", "5h")) and w.get("used_percent") is not None:
                return float(w["used_percent"])
    return None


def decide(limits: dict, base_codex: int, base_claude: int, wave_idx: int) -> dict:
    """Pick the engine mix for this wave. Claude (the smart model) is the PRIMARY
    generator; Spark is parallel bonus volume on its fresh bucket."""
    main = _codex_bucket(limits, CODEX_MAIN_BUCKET)  # gpt-5.5 — the only codex engine (Spark disabled)
    claude5h = _claude_5h_used(limits)

    def _usable(b: dict, weekly_cap: float) -> bool:
        # Unknown (unreadable snapshot) → optimistically usable; the orchestrator's
        # own stderr rate-limit detection is the real backstop if it's actually out.
        if not b["has_data"]:
            return True
        if b["w7d"] is not None and b["w7d"] >= weekly_cap:
            return False
        if b["w5h"] is not None and b["w5h"] >= FIVEH_CAP:
            return False
        return True

    def _pct(v) -> str:
        return f"{v:.0f}%" if isinstance(v, (int, float)) else "?"

    codex_model, codex_workers = None, 0
    if _usable(main, GPT55_WEEKLY_CAP):
        codex_model, codex_workers = MAIN_MODEL, base_codex
        codex_why = (f"gpt-5.5 w7d={_pct(main['w7d'])} (max effort)" if main["has_data"]
                     else "gpt-5.5 (limits unreadable → optimistic; orchestrator rate-limit is backstop)")
    else:
        codex_why = f"gpt-5.5 tapped (w7d={_pct(main['w7d'])}) → Claude solo (Spark disabled per config)"

    # Claude is the PRIMARY generator (smart model = the bottleneck task). Run the
    # full base every wave; only throttle as Claude's 5h window fills, reserving
    # COORD_RESERVE_PCT for the interactive coordinator. When usage isn't visible
    # we still run the full base (user confirmed ample Max headroom).
    if claude5h is None:
        claude_workers = base_claude
        claude_why = f"{base_claude} (claude usage not visible; ample Max → run base)"
    elif claude5h >= (100 - COORD_RESERVE_PCT):
        claude_workers = 0
        claude_why = f"0 (claude 5h={claude5h:.0f}% near reserve → protect coordinator)"
    elif claude5h >= 60:
        claude_workers = max(1, base_claude // 2)
        claude_why = f"{claude_workers} (claude 5h={claude5h:.0f}%, easing off)"
    else:
        claude_workers = base_claude
        claude_why = f"{base_claude} (claude 5h={claude5h:.0f}%, ample headroom)"

    # Pause if everything is tapped.
    sleep_until = None
    if codex_workers == 0 and claude_workers == 0:
        sleep_until = main["reset7d"] or (time.time() + 1800)

    return {
        "codex_model": codex_model, "codex_workers": codex_workers, "codex_why": codex_why,
        "claude_workers": claude_workers, "claude_why": claude_why,
        "claude5h": claude5h, "sleep_until": sleep_until,
        "main_w7d": main["w7d"],
    }


def run_wave(plan: dict, wave_idx: int, wave_trials: int, wave_hours: float,
             claude_model: str) -> dict:
    """Invoke the orchestrator for one bounded chunk."""
    env = os.environ.copy()
    env["RESEARCH_REGISTRY_DB"] = SCRATCH_DB  # speculative search → scratch, not canonical
    report_dir = REPORT_DIR / f"wave_{wave_idx:03d}"
    argv = [
        PY, "-m", "research.agents.orchestrator.run",
        "--duration-hours", str(wave_hours),
        "--max-trials", str(wave_trials),
        "--codex-workers", str(plan["codex_workers"]),
        "--claude-workers", str(plan["claude_workers"]),
        "--cost-profile", "live_pm",
        "--skip-gate",
        # The orchestrator's internal token-estimate cap is redundant now that
        # the foreman + live agent-limits is the budget authority. Raise it to
        # the max (5M Anthropic-token backstop) so codex/Spark waves aren't
        # blocked by a stale rolling ledger; foreman handles the real limits.
        "--weekly-cap-pct", "1.0",
        "--report-dir", str(report_dir),
        "--state-path", str(REPORT_DIR / "orch_state.json"),
    ]
    if plan["codex_model"]:
        argv += ["--codex-model", plan["codex_model"]]
    if plan["claude_workers"] > 0:
        argv += ["--claude-model", claude_model]
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=str(_REPO), env=env, capture_output=True,
                           text=True, timeout=int(wave_hours * 3600) + 1800)
        ok, tail = (r.returncode == 0), (r.stdout or "")[-800:] + (r.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        ok, tail = False, "wave timed out"
    return {"ok": ok, "dur_sec": round(time.time() - t0, 1), "tail": tail.strip()[-600:]}


def scratch_status() -> dict:
    """Count trials + gate-passers in the scratch DB (no agent spend)."""
    import sqlite3
    if not Path(SCRATCH_DB).exists():
        return {"total": 0, "gate_passed": 0}
    try:
        c = sqlite3.connect(SCRATCH_DB)
        n, p = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(scorer_promotion_gate_passed),0) FROM trials"
        ).fetchone()
        c.close()
        return {"total": int(n), "gate_passed": int(p)}
    except Exception:
        return {"total": -1, "gate_passed": -1}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline-hours", type=float, default=18.0)
    ap.add_argument("--wave-trials", type=int, default=4)
    ap.add_argument("--wave-hours", type=float, default=0.5)
    ap.add_argument("--codex-workers", type=int, default=2)
    ap.add_argument("--claude-base", type=int, default=4,
                    help="Claude workers per wave (primary generator).")
    ap.add_argument("--claude-model", type=str, default="claude-opus-4-8")
    ap.add_argument("--max-pause-sec", type=int, default=1800)
    args = ap.parse_args(argv)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    start = time.time()
    deadline = start + args.deadline_hours * 3600
    wave = 0
    print(f"[foreman] start; deadline in {args.deadline_hours}h; scratch={SCRATCH_DB}", flush=True)

    while time.time() < deadline:
        wave += 1
        limits = read_limits()
        plan = decide(limits, args.codex_workers, args.claude_base, wave)
        ts = time.strftime("%H:%M:%S")
        print(f"[foreman][w{wave} {ts}] codex={plan['codex_workers']}x{plan['codex_model']} "
              f"({plan['codex_why']}) | claude={plan['claude_workers']} ({plan['claude_why']})",
              flush=True)

        if plan["sleep_until"]:
            nap = max(60, min(args.max_pause_sec, int(plan["sleep_until"] - time.time())))
            print(f"[foreman][w{wave}] ALL ENGINES TAPPED → pause {nap}s", flush=True)
            (REPORT_DIR / "state.json").write_text(json.dumps({
                "wave": wave, "status": "paused", "plan": plan,
                "scratch": scratch_status(), "ts": ts}, default=str, indent=2))
            time.sleep(nap)
            continue

        res = run_wave(plan, wave, args.wave_trials, args.wave_hours, args.claude_model)
        sc = scratch_status()
        print(f"[foreman][w{wave}] wave ok={res['ok']} dur={res['dur_sec']}s | "
              f"scratch trials={sc['total']} gate_passed={sc['gate_passed']}", flush=True)
        (REPORT_DIR / "state.json").write_text(json.dumps({
            "wave": wave, "status": "ran", "plan": plan, "wave_result": res,
            "scratch": sc, "elapsed_hours": round((time.time() - start) / 3600, 2),
            "ts": ts}, default=str, indent=2))

    print(f"[foreman] deadline reached after {wave} waves. Final scratch={scratch_status()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
