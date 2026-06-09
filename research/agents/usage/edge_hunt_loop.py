"""Budget-aware GPT-5.5 edge-hunt loop (codex exec + ChatGPT-Pro 20x).

This is the V2 autoresearch driver. It is deliberately small and reuses the
tested pieces:

  - `usage.limits`      — poll your agent-limits tool, govern worker count.
  - `usage.probe_gpt55.run_codex_exec` — one codex exec subprocess.
  - `workers/gpt55_edge_hunter_prompt.md` — the research-grade prompt.

The loop, per wave:
  1. poll limits → governor decides how many GPT-5.5 workers are safe (0 ⇒ sleep
     until the 7d window resets).
  2. pick the next direction from the markets × mechanisms matrix (round-robin,
     persisted) — this is where the prior project went wrong: it ground the
     efficient moneyline DSL. V2 aims at less-watched books and level/anchoring
     mechanisms, the cell that actually produced the certified totals edge.
  3. spawn N codex exec workers in parallel, each with direction.md + the
     edge-hunter prompt, each writing result.md.
  4. parse each result.md's JSON, append to a JSONL ledger, surface PROMOTE/
     NEEDS_DATA candidates for the human (the gate + a test-set unlock stay
     human-gated — workers never touch the test split).

Run on the machine with codex + your account::

    python -m research.agents.usage.edge_hunt_loop --waves 6 --workers 3
    python -m research.agents.usage.edge_hunt_loop --dry-run        # plan only, no codex
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from research.agents.usage import limits as L
from research.agents.usage.probe_gpt55 import run_codex_exec

_ROOT = Path(__file__).resolve().parents[3]
PROMPT = _ROOT / "research/agents/workers/gpt55_edge_hunter_prompt.md"
LEDGER = _ROOT / "research/reports/alpha/edge_hunt_ledger.jsonl"
STATE = _ROOT / "research/reports/alpha/edge_hunt_state.json"


@dataclass
class Direction:
    key: str
    market: str
    hypothesis: str


# NO hardcoded directions, by design. Which strategies get pursued is decided at
# runtime by the agent layer: `research.lab.scout` ORIGINATES hypotheses from
# `research.lab.eda` diagnostics, and `research.lab.director` PRIORITIZES the
# open pool against the research ledger. This loop only carries that machinery —
# never a menu of strategies. (Kept empty intentionally; do not re-add a list.)
DIRECTIONS: list[Direction] = []


def _hypothesis_to_direction(h) -> Direction:
    """Map a `research.lab.types.Hypothesis` row onto a `Direction`.

    The registry stores a structured idea (mechanism / observable signal /
    pre-registered direction); flatten it into the free-text ``hypothesis``
    blurb a worker reads from ``direction.md``. ``key`` uses the hypothesis id
    (stable dedupe hash) so ledger rows trace back to the registry row.
    """
    parts = [h.mechanism.strip()]
    if getattr(h, "signal_desc", ""):
        parts.append(f"Signal: {h.signal_desc.strip()}")
    if getattr(h, "direction", ""):
        parts.append(f"Pre-registered direction: {h.direction.strip()}")
    return Direction(
        key=(h.id or h.hash()),
        market=h.market,
        hypothesis=" ".join(parts),
    )


def _next_directions(k: int, *, ledger_path: str | None = None,
                     originate: bool = True) -> list[Direction]:
    """The next ``k`` directions to pursue — agent-decided, never hardcoded.

    ``research.lab.scout`` ORIGINATES hypotheses from data (only when the open
    pool is empty AND ``originate`` is set, since it needs a live agent), and
    ``research.lab.director`` PRIORITIZES the open pool against the research
    ledger. Returns ``[]`` when there is genuinely nothing to pursue — there is
    no canned menu to fall back to (that is the point).
    """
    try:
        from research.lab import director, hypothesis as _hyp  # lazy
    except ImportError:
        return []
    try:
        if originate and not _hyp.open_hypotheses():
            from research.lab import scout
            scout.propose()  # agent origination from EDA diagnostics
        chosen = director.select(k=k, ledger_path=ledger_path)
    except Exception:  # noqa: BLE001 — never let the layer crash the loop
        return []
    return [_hypothesis_to_direction(h) for h in chosen]


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"dir_idx": 0, "waves_done": 0}


def _save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _direction_md(d: Direction, worker_i: int) -> str:
    return (f"# Research direction\n\nmarket: {d.market}\nkey: {d.key}\n"
            f"worker: {worker_i}\n\n## Hypothesis to explore\n{d.hypothesis}\n\n"
            "Form your own mechanism, pre-register direction, iterate on a cloned "
            "evaluator, run the gate, and write result.md per the prompt. Honest "
            "negatives (verdict=DEAD) are valuable.")


def _harvest(workdir: Path) -> dict:
    rm = workdir / "result.md"
    out = {"workdir": workdir.name, "ok": rm.exists()}
    if rm.exists():
        import re
        blocks = re.findall(r"```json\s*(\{.*?\})\s*```", rm.read_text(), flags=__import__("re").DOTALL)
        if blocks:
            try:
                out.update(json.loads(blocks[-1]))
            except Exception:  # noqa: BLE001
                out["parse_error"] = True
    return out


def run_wave(direction: Direction, n_workers: int, base: Path, model: str,
             effort: str, dry_run: bool) -> list[dict]:
    prompt = PROMPT.read_text()
    base.mkdir(parents=True, exist_ok=True)
    tasks = []
    for i in range(n_workers):
        wd = base / f"{direction.key}_{int(time.time())}_{i}"
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "direction.md").write_text(_direction_md(direction, i))
        tasks.append(wd)
    if dry_run:
        return [{"workdir": t.name, "dry_run": True, "direction": direction.key} for t in tasks]

    full = prompt + "\n\n---\n\n(Your direction.md is in this working directory.)"

    def _one(wd: Path) -> dict:
        run_codex_exec(full, wd, model=model, effort=effort)
        return _harvest(wd)

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        return list(ex.map(_one, tasks))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--waves", type=int, default=6)
    ap.add_argument("--workers", type=int, default=3, help="base GPT-5.5 workers/wave")
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--dry-run", action="store_true", help="plan only; no codex, no limits needed")
    a = ap.parse_args(argv)

    base = _ROOT / "research/experiments/gpt55"
    state = _load_state()
    LEDGER.parent.mkdir(parents=True, exist_ok=True)

    for w in range(a.waves):
        # The agent layer decides what to pursue this wave (scout originates if
        # the pool is empty; director ranks). No hardcoded menu, no round-robin.
        dirs = _next_directions(1, ledger_path=str(LEDGER), originate=not a.dry_run)
        if not dirs:
            print("no open hypotheses to pursue — originate ideas by running the "
                  "scout with a live agent (codex); nothing hardcoded to fall back "
                  "to. stopping.", flush=True)
            break
        direction = dirs[0]
        if a.dry_run:
            n = a.workers
            plan_reason = "dry-run (limits not polled)"
            sleep_until = None
        else:
            snap = L.poll()
            decision = L.decide_workers(snap, a.workers)
            n, plan_reason, sleep_until = decision.gpt55_workers, decision.reason, decision.sleep_until_epoch
        print(f"[wave {w+1}/{a.waves}] dir={direction.key} market={direction.market} "
              f"workers={n} :: {plan_reason}", flush=True)

        if sleep_until:
            nap = max(0, sleep_until - time.time())
            print(f"  tapped → sleeping {nap/60:.0f} min until reset", flush=True)
            if not a.dry_run:
                time.sleep(min(nap, 3600))
            continue
        if n == 0:
            continue

        results = run_wave(direction, n, base, a.model, a.effort, a.dry_run)
        for r in results:
            r["wave"] = w + 1
            r["direction"] = direction.key
            r["hypothesis_id"] = direction.key   # registry id, for the feedback step
            if not a.dry_run:
                with LEDGER.open("a") as fh:
                    fh.write(json.dumps(r) + "\n")
            tag = r.get("verdict", "dry-run" if a.dry_run else "?")
            print(f"    worker {r['workdir']}: verdict={tag} "
                  f"real_edge={r.get('real_edge')} gate={r.get('gate_passed')}", flush=True)

        promote = [r for r in results if str(r.get("verdict", "")).upper() == "PROMOTE"]
        if promote:
            print(f"  ** {len(promote)} PROMOTE candidate(s) — human review + optional test-set "
                  f"unlock required (workers never touch test) **", flush=True)

        # Feedback: fold this wave's verdicts back into the registry so the
        # director's next pick reflects what just happened (DEAD ideas leave the
        # open pool; PROMISING leads stay). Pure results-driven, not hardcoded.
        if not a.dry_run:
            try:
                from research.lab import director
                director.incorporate_results(str(LEDGER))
            except Exception:  # noqa: BLE001
                pass
        state["waves_done"] += 1
        _save_state(state)

    print(f"\nDone. Ledger: {LEDGER}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
