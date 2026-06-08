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


# Markets × mechanisms. Ordered by my prior of where edge survives: less-watched
# books first, level/anchoring mechanisms over timing (honest i+1 kills timing).
DIRECTIONS = [
    Direction("totals_extend", "totals",
              "Extend the certified pace-anchoring totals edge: quarter-conditioned "
              "entries, team-specific pace priors, or a spreads-implied total cross-check. "
              "Confirm it survives the freshness guard and the gate at 2c."),
    Direction("spread_anchor", "spread",
              "Spread/handicap line anchors on the pregame number and under-reacts to the "
              "live margin trajectory — mirror totals_alpha for the spread book."),
    Direction("altline_drift", "alt_total",
              "Alternate total lines (over X+k) drift stale relative to the main line they "
              "should track; fade the cross-line inconsistency, hold to settlement."),
    Direction("props_thin", "player_prop",
              "Player-prop books are thin and slow to mark foul trouble / blowout benching; "
              "look for a level mispricing, NOT a timing play. Verify liquidity first."),
    Direction("totals_endgame", "totals",
              "Late-game totals: fouling/clock effects make the final-minutes total "
              "predictable vs a line that stops updating; honest i+1, hold to settlement."),
]


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
        direction = DIRECTIONS[state["dir_idx"] % len(DIRECTIONS)]
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

        state["dir_idx"] += 1
        state["waves_done"] += 1
        _save_state(state)

    print(f"\nDone. Ledger: {LEDGER}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
