"""research.lab.heartbeat — the cadence that makes the research factory run itself.

The substrate can originate, prioritize, test, and judge ideas (scout ->
director -> analyst -> gate), and ``research.lab.analyst.run_analyst`` closes one
pass end to end. This module is the optional *clock*: it fires passes on a
cadence, persists state, and stops cleanly when the open pool is exhausted. It is
model-agnostic — the live agent is INJECTED at the ``executor`` seam (a Claude
Opus agent the operator spawns from chat; see ``OPERATING_MODEL.md``).

Two modes, one engine:

  * ``--once``  — run exactly ONE analyst pass and exit. Idempotent and
    state-persisted, so an external scheduler (cron, or the operator firing it
    from chat) provides the cadence.
  * ``--interval N`` — stay resident and fire a pass every ``N`` seconds until
    ``--max-passes`` or a stop condition. For an always-on research box.

Stop conditions (so a resident loop never spins for free):
  * ``--max-passes`` reached, OR
  * ``--stop-after-idle`` consecutive idle (0-processed) passes — the open pool
    is exhausted, or no executor is injected — so we stand down rather than burn
    ticks.

The executor (the live agent) and the ``sleep`` primitive are INJECTABLE so the
whole loop is deterministically testable without spawning anything or waiting.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from research.lab.analyst import DEFAULT_LEDGER, run_analyst

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = "research/reports/alpha/heartbeat_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HeartbeatState:
    """Persisted across passes so a cron-driven ``--once`` accumulates history."""
    passes_done: int = 0
    total_processed: int = 0
    consecutive_idle: int = 0
    last_started: Optional[str] = None
    last_finished: Optional[str] = None
    last_summary: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "HeartbeatState":
        p = Path(path)
        if p.exists():
            try:
                return cls(**{k: v for k, v in json.loads(p.read_text()).items()
                              if k in cls.__dataclass_fields__})
            except Exception:  # noqa: BLE001 — corrupt state starts fresh
                pass
        return cls()

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.__dict__, indent=2, default=str))


def run_pass(
    *,
    brief: Optional[str] = None,
    max_ideas: int = 3,
    market: Optional[str] = None,
    executor: Optional[Callable[[dict], dict]] = None,
    ledger_path: Optional[str] = DEFAULT_LEDGER,
) -> dict:
    """Fire ONE analyst pass.

    Model-agnostic: ``executor=None`` resolves to the no-op stub (assignments
    prepared, not run). The real executor is INJECTED — a Claude Opus agent the
    operator spawns from chat (see ``OPERATING_MODEL.md``). Tests pass a fake.
    """
    if executor is None:
        from research.lab.analyst import _noop_executor
        executor = _noop_executor
    return run_analyst(
        brief,
        max_ideas=max_ideas,
        market=market,
        executor=executor,
        ledger_path=ledger_path,
    )


def run_heartbeat(
    *,
    once: bool = False,
    interval_sec: float = 3600.0,
    max_passes: int = 0,
    stop_after_idle: int = 3,
    brief: Optional[str] = None,
    max_ideas: int = 3,
    market: Optional[str] = None,
    executor: Optional[Callable[[dict], dict]] = None,
    ledger_path: Optional[str] = DEFAULT_LEDGER,
    state_path: str = DEFAULT_STATE,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> HeartbeatState:
    """Drive analyst passes on a cadence; return the final persisted state.

    ``--once`` (``once=True``) fires a single pass and returns — the external
    scheduler owns the clock. Otherwise loop every ``interval_sec`` until
    ``max_passes`` (0 = unbounded) or ``stop_after_idle`` consecutive idle passes
    (pool exhausted / no executor injected). ``sleep`` and ``executor`` are
    injectable so this is fully testable without waiting or spawning.
    """
    state = HeartbeatState.load(state_path)

    def _one() -> int:
        state.last_started = _now()
        summary = run_pass(
            brief=brief, max_ideas=max_ideas, market=market,
            executor=executor, ledger_path=ledger_path,
        )
        processed = int(summary.get("processed", 0))
        state.passes_done += 1
        state.total_processed += processed
        state.consecutive_idle = 0 if processed > 0 else state.consecutive_idle + 1
        state.last_finished = _now()
        state.last_summary = summary
        state.save(state_path)
        budget = summary.get("budget", {})
        log(f"[heartbeat pass {state.passes_done}] processed={processed} "
            f"open_remaining={summary.get('open_remaining')} idle_streak="
            f"{state.consecutive_idle} :: {budget.get('reason', '')}")
        return processed

    if once:
        _one()
        return state

    while True:
        _one()
        if max_passes and state.passes_done >= max_passes:
            log(f"[heartbeat] reached max_passes={max_passes}; standing down.")
            break
        if stop_after_idle and state.consecutive_idle >= stop_after_idle:
            log(f"[heartbeat] {state.consecutive_idle} idle passes "
                f"(budget tapped / pool exhausted); standing down.")
            break
        sleep(interval_sec)

    return state


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="run exactly one pass and exit (let cron own the cadence)")
    ap.add_argument("--interval", type=float, default=3600.0,
                    help="seconds between passes in resident mode (default: 3600)")
    ap.add_argument("--max-passes", type=int, default=0,
                    help="stop after N passes (0 = unbounded)")
    ap.add_argument("--stop-after-idle", type=int, default=3,
                    help="stand down after N consecutive idle (0-processed) passes")
    ap.add_argument("--brief", default=None, help="analyst brief threaded into assignments")
    ap.add_argument("--max-ideas", type=int, default=3, help="max ideas worked per pass")
    ap.add_argument("--market", default=None, help="restrict to one market")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER, help="shared trial ledger ('' to disable)")
    ap.add_argument("--state", default=DEFAULT_STATE, help="heartbeat state file")
    a = ap.parse_args(argv)

    # The CLI runs with the model-agnostic no-op executor (assignments prepared,
    # not run). The real executor is INJECTED via run_heartbeat(executor=...) —
    # a Claude Opus agent the operator spawns from chat (see OPERATING_MODEL.md).
    state = run_heartbeat(
        once=a.once,
        interval_sec=a.interval,
        max_passes=a.max_passes,
        stop_after_idle=a.stop_after_idle,
        brief=a.brief,
        max_ideas=a.max_ideas,
        market=a.market,
        executor=None,
        ledger_path=(a.ledger or None),
        state_path=a.state,
    )
    print(json.dumps(state.__dict__, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
