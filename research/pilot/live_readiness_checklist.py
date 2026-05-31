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

from research.matching.gate import MatchingGate
from research.paper.analyze_paper_run import analyze, load_records


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
                    max_drift: Optional[float] = None) -> CheckResult:
    max_drift = max_drift if max_drift is not None else Config.PAPER_MAX_EST_VS_REAL_DRIFT_USD
    if not os.path.exists(executions_path):
        return CheckResult("paper_run", False, f"no capture at {executions_path}")
    report = analyze(load_records(executions_path))
    if report.filled == 0:
        return CheckResult("paper_run", False, "no filled paper executions")
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
    ks = kill_switch or KillSwitch.instance()
    was_active = ks.is_active()[0]
    ks.trip("readiness_probe")
    tripped = ks.is_active()[0]
    ks.reset()
    # After reset, active iff EXECUTION_ENABLED is False — that's expected.
    cleared = ks._tripped is False
    return CheckResult("kill_switch", tripped and cleared,
                       "trip/reset functional" if (tripped and cleared) else "malfunction")


def check_live_readiness(
    labeled_pairs_path: str = "market_data/matching/labeled_pairs.jsonl",
    executions_path: str = "market_data/executions/executions.jsonl",
    allowlist_path: Optional[str] = None,
    checks: Optional[List[Callable[[], CheckResult]]] = None,
) -> ReadinessReport:
    report = ReadinessReport()
    if checks is None:
        checks = [
            lambda: check_matching_gate(labeled_pairs_path),
            lambda: check_paper_run(executions_path),
            lambda: check_allowlist(allowlist_path),
            lambda: check_kill_switch(),
        ]
    for check in checks:
        result = check()
        report.add(result.name, result.passed, result.detail)
    return report


def main(argv=None) -> int:  # pragma: no cover
    report = check_live_readiness()
    print(report.summary())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
