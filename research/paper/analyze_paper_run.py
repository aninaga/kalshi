"""Analyze a captured paper-trading run (Phase C).

Reads the JSONL execution capture and computes the confidence metrics that gate
the live pilot (Phase D):

  * estimated-vs-realized net drift (mean / p50 / p95) + bootstrap CI on mean,
  * fill / skip breakdown by reason,
  * hedge rate and unwind-failure count,
  * polarity-error count (any execution whose match polarity was inverted/unknown
    but still traded — must be zero before live).

Usage::

    python -m research.paper.analyze_paper_run market_data/executions/executions.jsonl
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def load_records(path: str) -> List[Dict]:
    records = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(pct * len(s)))
    return s[idx]


def _bootstrap_mean_ci(values: List[float], samples: int = 2000,
                       alpha: float = 0.05, seed: int = 7):
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (lo, hi)


@dataclass
class PaperRunReport:
    total: int = 0
    filled: int = 0
    skipped: int = 0
    by_source: Dict[str, int] = field(default_factory=dict)
    by_skip_reason: Dict[str, int] = field(default_factory=dict)
    realized_net_sum: float = 0.0
    est_vs_real_mean: float = 0.0
    est_vs_real_p50: float = 0.0
    est_vs_real_p95: float = 0.0
    est_vs_real_ci: tuple = (0.0, 0.0)
    hedge_count: int = 0
    unwind_failures: int = 0
    polarity_errors: int = 0

    def summary(self) -> str:
        return (
            f"executions={self.total} filled={self.filled} skipped={self.skipped}\n"
            f"  by_source={self.by_source}\n"
            f"  by_skip_reason={self.by_skip_reason}\n"
            f"  realized_net_sum={self.realized_net_sum:.2f}\n"
            f"  est_vs_real: mean={self.est_vs_real_mean:.4f} "
            f"p50={self.est_vs_real_p50:.4f} p95={self.est_vs_real_p95:.4f} "
            f"CI95={self.est_vs_real_ci}\n"
            f"  hedges={self.hedge_count} unwind_failures={self.unwind_failures} "
            f"polarity_errors={self.polarity_errors}"
        )

    def to_dict(self) -> Dict:
        return self.__dict__.copy()


def analyze(records: List[Dict]) -> PaperRunReport:
    report = PaperRunReport(total=len(records))
    deltas: List[float] = []
    source_counter: Counter = Counter()
    skip_counter: Counter = Counter()

    for r in records:
        source_counter[r.get("confirmation_source") or "unknown"] += 1
        if r.get("skipped_reason"):
            report.skipped += 1
            skip_counter[r["skipped_reason"]] += 1
            continue
        report.filled += 1
        report.realized_net_sum += float(r.get("realized_net") or 0.0)
        if r.get("est_vs_real_delta") is not None:
            deltas.append(float(r["est_vs_real_delta"]))
        if float(r.get("hedge_residual") or 0.0) > 0 and not r.get("hedge_filled"):
            report.unwind_failures += 1
        elif r.get("hedge_filled"):
            report.hedge_count += 1
        # A traded execution whose match polarity wasn't a clean "aligned"/"inverted"
        # resolution is a polarity risk; track explicit unknowns that still traded.
        if r.get("polarity") == "unknown":
            report.polarity_errors += 1

    report.by_source = dict(source_counter)
    report.by_skip_reason = dict(skip_counter)
    if deltas:
        report.est_vs_real_mean = sum(deltas) / len(deltas)
        report.est_vs_real_p50 = _percentile(deltas, 0.50)
        report.est_vs_real_p95 = _percentile(deltas, 0.95)
        report.est_vs_real_ci = _bootstrap_mean_ci(deltas)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: analyze_paper_run.py <executions.jsonl>")
        return 2
    records = load_records(argv[0])
    print(analyze(records).summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
