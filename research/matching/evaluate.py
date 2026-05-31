"""Measure matcher precision / recall against a labeled pair set (Phase A).

The matching backtest answers the question the user's skepticism demands: *how
often does the matcher say two contracts are the same when they aren't?* A
false positive in the execution path fires an un-hedged trade, so precision is
the metric that gates live trading; recall is secondary.

Usage::

    from kalshi_arbitrage.matching import CompositeVerifier, load_labeled_pairs
    from research.matching.evaluate import evaluate_matcher

    pairs = load_labeled_pairs("market_data/matching/labeled_pairs.jsonl")
    report = evaluate_matcher(CompositeVerifier(), pairs)
    print(report.summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence


@dataclass
class MatchingReport:
    """Confusion matrix + derived metrics for a matcher over labeled pairs."""

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    polarity_correct: int = 0
    polarity_total: int = 0
    false_positive_pairs: List[Dict] = field(default_factory=list)
    polarity_error_pairs: List[Dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.false_negative + self.true_negative

    @property
    def precision(self) -> float:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def polarity_accuracy(self) -> float:
        return self.polarity_correct / self.polarity_total if self.polarity_total else 1.0

    @property
    def lexical_baseline_precision(self) -> float:
        """Precision if we accepted every lexically-similar candidate (no verifier)."""
        positives = self.true_positive + self.false_negative
        return positives / self.total if self.total else 1.0

    def summary(self) -> str:
        return (
            f"pairs={self.total} "
            f"precision={self.precision:.4f} recall={self.recall:.4f} f1={self.f1:.4f} "
            f"polarity_acc={self.polarity_accuracy:.4f} "
            f"(baseline_precision={self.lexical_baseline_precision:.4f}) "
            f"TP={self.true_positive} FP={self.false_positive} "
            f"FN={self.false_negative} TN={self.true_negative}"
        )

    def to_dict(self) -> Dict:
        return {
            "total": self.total,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "polarity_accuracy": self.polarity_accuracy,
            "lexical_baseline_precision": self.lexical_baseline_precision,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "true_negative": self.true_negative,
            "false_positive_pairs": self.false_positive_pairs,
            "polarity_error_pairs": self.polarity_error_pairs,
        }


def evaluate_matcher(verifier, labeled_pairs: Sequence) -> MatchingReport:
    """Run ``verifier`` over labeled pairs and tally a :class:`MatchingReport`.

    ``verifier`` is anything with ``.verify(kalshi_market, polymarket_market)``
    returning a verdict with ``.passed`` and ``.polarity``.
    """
    report = MatchingReport()

    for pair in labeled_pairs:
        kalshi_market, polymarket_market = pair.to_market_dicts()
        verdict = verifier.verify(kalshi_market, polymarket_market)
        predicted_match = bool(verdict.passed)
        actual_match = pair.is_true_match()

        if actual_match and predicted_match:
            report.true_positive += 1
            # Polarity is only meaningful for genuine matches with a known label.
            if pair.polarity and pair.polarity != "unknown":
                report.polarity_total += 1
                if verdict.polarity == pair.polarity:
                    report.polarity_correct += 1
                else:
                    report.polarity_error_pairs.append({
                        "kalshi": pair.kalshi_title,
                        "polymarket": pair.polymarket_title,
                        "expected": pair.polarity,
                        "got": verdict.polarity,
                    })
        elif actual_match and not predicted_match:
            report.false_negative += 1
        elif (not actual_match) and predicted_match:
            report.false_positive += 1
            report.false_positive_pairs.append({
                "kalshi": pair.kalshi_title,
                "polymarket": pair.polymarket_title,
                "reasons": list(verdict.reasons),
            })
        else:
            report.true_negative += 1

    return report
