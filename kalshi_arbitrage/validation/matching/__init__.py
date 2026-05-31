"""Matching backtest harness (Phase A).

Measures matcher precision/recall/polarity-accuracy against a human-labeled
candidate-pair set, and gates execution-eligibility on a bootstrapped precision
CI. Separate from ``research/harness/`` (an NBA single-game PnL backtester that
cannot validate cross-venue matching).
"""

from .dataset import build_dataset, candidate_from_match, make_hard_negatives
from .evaluate import MatchingReport, evaluate_matcher
from .gate import MatchingGate, MatchingGateDecision

__all__ = [
    "build_dataset",
    "candidate_from_match",
    "make_hard_negatives",
    "MatchingReport",
    "evaluate_matcher",
    "MatchingGate",
    "MatchingGateDecision",
]
