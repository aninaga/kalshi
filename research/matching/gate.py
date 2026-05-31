"""Promotion gate for matching quality (Phase A).

Mirrors the statistical-gate discipline in ``research/scorer/`` (point
estimates lie; put a confidence interval on the metric that matters). Here the
gated metric is **precision**, because a false-positive match is what turns
"risk-free arbitrage" into an un-hedged directional bet. A bootstrap CI over
the labeled pairs guards against a lucky point estimate on a small set.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .evaluate import evaluate_matcher


@dataclass(frozen=True)
class MatchingGate:
    """Thresholds a matcher must clear before its output is execution-eligible."""

    min_precision: float = 0.99          # the bar for enabling live trading
    min_recall: float = 0.70             # secondary — missing a match only costs opportunity
    min_polarity_accuracy: float = 1.0   # a polarity error is unacceptable
    bootstrap_samples: int = 2000
    ci_alpha: float = 0.05               # 95% CI
    seed: int = 7

    def evaluate(self, verifier, labeled_pairs: Sequence) -> "MatchingGateDecision":
        report = evaluate_matcher(verifier, labeled_pairs)
        lo, hi = self._bootstrap_precision_ci(verifier, labeled_pairs)

        reasons: List[str] = []
        # Gate on the LOWER bound of the precision CI, not the point estimate.
        if lo < self.min_precision:
            reasons.append(f"precision CI low {lo:.4f} < {self.min_precision}")
        if report.recall < self.min_recall:
            reasons.append(f"recall {report.recall:.4f} < {self.min_recall}")
        if report.polarity_accuracy < self.min_polarity_accuracy:
            reasons.append(
                f"polarity_accuracy {report.polarity_accuracy:.4f} < {self.min_polarity_accuracy}"
            )

        return MatchingGateDecision(
            passed=not reasons,
            precision=report.precision,
            precision_ci=(lo, hi),
            recall=report.recall,
            polarity_accuracy=report.polarity_accuracy,
            reasons=tuple(reasons),
            report=report,
        )

    def _bootstrap_precision_ci(self, verifier, labeled_pairs: Sequence):
        pairs = list(labeled_pairs)
        if not pairs:
            return (1.0, 1.0)
        rng = random.Random(self.seed)
        n = len(pairs)
        precisions: List[float] = []
        for _ in range(self.bootstrap_samples):
            resample = [pairs[rng.randrange(n)] for _ in range(n)]
            precisions.append(evaluate_matcher(verifier, resample).precision)
        precisions.sort()
        lo_idx = int((self.ci_alpha / 2) * len(precisions))
        hi_idx = min(len(precisions) - 1, int((1 - self.ci_alpha / 2) * len(precisions)))
        return (precisions[lo_idx], precisions[hi_idx])


@dataclass(frozen=True)
class MatchingGateDecision:
    passed: bool
    precision: float
    precision_ci: tuple
    recall: float
    polarity_accuracy: float
    reasons: tuple
    report: object = None

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        ci = f"[{self.precision_ci[0]:.4f}, {self.precision_ci[1]:.4f}]"
        out = (
            f"[{verdict}] precision={self.precision:.4f} CI95={ci} "
            f"recall={self.recall:.4f} polarity_acc={self.polarity_accuracy:.4f}"
        )
        if self.reasons:
            out += " | " + "; ".join(self.reasons)
        return out
