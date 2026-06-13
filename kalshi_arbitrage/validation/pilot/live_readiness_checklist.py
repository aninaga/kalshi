"""Live-pilot readiness gate (Phase D).

A scripted go/no-go check that must pass before flipping ``EXECUTION_MODE`` to
``live``. It composes the Phase A and Phase C gates with operational checks so
"are we ready for real money?" is a single, auditable answer rather than a
judgement call.

Checks:
  1. Matching precision gate passed on the labeled set (Phase A).
  2. Paper run: estimated-vs-realized drift within tolerance AND zero polarity
     errors (Phase C).
  3. An operator allowlist exists and is non-empty.
  4. The kill switch is functional (trip → active → reset).

Each check is injectable so the gate is unit-testable without live credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.execution.kill_switch import KillSwitch
from kalshi_arbitrage.matching import CompositeVerifier, load_labeled_pairs

from kalshi_arbitrage.validation.matching.gate import MatchingGate
from kalshi_arbitrage.validation.paper.analyze_paper_run import analyze, filter_records, load_records


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ReadinessReport:
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name, passed, detail))

    def summary(self) -> str:
        lines = [("PASS" if self.passed else "FAIL") + " — live readiness"]
        for c in self.checks:
            lines.append(f"  [{'x' if c.passed else ' '}] {c.name}: {c.detail}")
        return "\n".join(lines)


def check_matching_gate(labeled_pairs_path: str,
                        gate: Optional[MatchingGate] = None) -> CheckResult:
    gate = gate or MatchingGate()
    if not os.path.exists(labeled_pairs_path):
        return CheckResult("matching_precision_gate", False,
                           f"no labeled pairs at {labeled_pairs_path}")
    pairs = load_labeled_pairs(labeled_pairs_path)
    if not pairs:
        return CheckResult("matching_precision_gate", False, "labeled set empty")
    decision = gate.evaluate(CompositeVerifier(), pairs)
    return CheckResult("matching_precision_gate", decision.passed, decision.summary())


def check_paper_run(executions_path: str,
                    max_drift: Optional[float] = None,
                    source: str = "paper",
                    since: Optional[float] = None) -> CheckResult:
    max_drift = max_drift if max_drift is not None else Config.PAPER_MAX_EST_VS_REAL_DRIFT_USD
    if not os.path.exists(executions_path):
        return CheckResult("paper_run", False, f"no capture at {executions_path}")
    # Evaluate only real paper fills (source='paper') — the shared capture file
    # also holds sample fixtures and historical 'exchange'/'simulation' rows that
    # would otherwise pollute drift/unwind. Pass --since to scope to one session.
    report = analyze(filter_records(load_records(executions_path), source=source, since=since))
    if report.filled == 0:
        return CheckResult("paper_run", False,
                           f"no filled '{source}' executions (run `kalshi-arb monitor --execute`)")
    if report.polarity_errors > 0:
        return CheckResult("paper_run", False,
                           f"{report.polarity_errors} polarity errors (must be 0)")
    drift = abs(report.est_vs_real_mean)
    ok = drift <= max_drift
    return CheckResult("paper_run", ok,
                       f"drift={drift:.4f} (max {max_drift}), filled={report.filled}, "
                       f"unwind_failures={report.unwind_failures}")


def check_allowlist(path: Optional[str] = None) -> CheckResult:
    if path is None:
        path = os.path.join(Config.DATA_DIR, Config.MATCH_ALLOWLIST_FILE)
    if not os.path.exists(path):
        return CheckResult("allowlist", False, f"no allowlist at {path}")
    import json
    try:
        data = json.load(open(path))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("allowlist", False, f"unreadable: {exc}")
    approved = data.get("approved", [])
    return CheckResult("allowlist", bool(approved), f"{len(approved)} approved pairs")


def check_kill_switch(kill_switch: Optional[KillSwitch] = None) -> CheckResult:
    """Verify trip→active→reset WITHOUT touching the production halt state.

    The previous probe tripped and then unconditionally ``reset()`` the
    PRODUCTION singleton — deleting a durable operator halt
    (``DATA_DIR/EXECUTION_HALT``) as a side effect of merely *checking
    readiness*. Now:

      * a currently-active halt FAILS the check and is left exactly as found
        (you are not live-ready while halted; clearing it is an explicit
        operator action, never a readiness side effect);
      * the functional trip/reset probe runs on a THROWAWAY ``KillSwitch``
        instance with ``Config.DATA_DIR`` temporarily pointed at a fresh temp
        dir, so the production sentinel can be neither created nor deleted.

    An injected ``kill_switch`` (tests) is probed directly, as before.
    """
    # 1) Never clear a real halt — an active halt blocks readiness.
    current = kill_switch or KillSwitch.instance()
    active, why = current.is_active()
    if active:
        return CheckResult(
            "kill_switch", False,
            f"operator halt ACTIVE ({why}) — left in place; clear it explicitly "
            f"before any go-live")

    if kill_switch is not None:
        ks = kill_switch
        ks.trip("readiness_probe")
        tripped = ks.is_active()[0]
        ks.reset()
        cleared = ks._tripped is False
        return CheckResult("kill_switch", tripped and cleared,
                           "trip/reset functional" if (tripped and cleared) else "malfunction")

    # 2) Functional probe on a throwaway instance in an isolated DATA_DIR.
    import tempfile
    original_data_dir = Config.DATA_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="kill_switch_probe_") as tmp:
            Config.DATA_DIR = tmp
            probe = KillSwitch()  # NOT the singleton
            probe.trip("readiness_probe")
            tripped = probe.is_active()[0]
            probe.reset()
            cleared = (probe._tripped is False
                       and not os.path.exists(probe.sentinel_path))
    finally:
        Config.DATA_DIR = original_data_dir
    return CheckResult("kill_switch", tripped and cleared,
                       "trip/reset functional (probed in isolated temp dir)"
                       if (tripped and cleared) else "malfunction")


def _default_labeled_path() -> str:
    """The operator's labeled set, else the shipped 691-pair gold corpus."""
    for p in ("market_data/matching/labeled_pairs.jsonl",
              os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "tests", "data", "matching", "labeled_corpus.jsonl")):
        if os.path.exists(p):
            return p
    return "market_data/matching/labeled_pairs.jsonl"


def check_live_readiness(
    labeled_pairs_path: Optional[str] = None,
    executions_path: str = "market_data/executions/executions.jsonl",
    allowlist_path: Optional[str] = None,
    paper_source: str = "paper",
    paper_since: Optional[float] = None,
    checks: Optional[List[Callable[[], CheckResult]]] = None,
) -> ReadinessReport:
    labeled_pairs_path = labeled_pairs_path or _default_labeled_path()
    report = ReadinessReport()
    if checks is None:
        checks = [
            lambda: check_matching_gate(labeled_pairs_path),
            lambda: check_paper_run(executions_path, source=paper_source, since=paper_since),
            lambda: check_allowlist(allowlist_path),
            lambda: check_kill_switch(),
        ]
    for check in checks:
        result = check()
        report.add(result.name, result.passed, result.detail)
    return report


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="readiness")
    ap.add_argument("--labeled", default=None, help="labeled set (default: shipped gold corpus)")
    ap.add_argument("--executions", default="market_data/executions/executions.jsonl")
    ap.add_argument("--allowlist", default=None)
    ap.add_argument("--source", default="paper", help="paper-fill source to evaluate")
    ap.add_argument("--since", type=float, default=None,
                    help="scope paper_run to fills with ts >= this epoch (one session)")
    args = ap.parse_args(argv)
    report = check_live_readiness(
        labeled_pairs_path=args.labeled, executions_path=args.executions,
        allowlist_path=args.allowlist, paper_source=args.source, paper_since=args.since)
    print(report.summary())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
