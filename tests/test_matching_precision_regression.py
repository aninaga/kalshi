"""Matching precision regression — real false/true positives from a live scan.

Pins the DistinguishingEntityVerifier (scope/geography veto + entity overlap)
against the actual failure cases observed live: national-vs-state scope, person
mismatches, and country swaps — while preserving spelling-variant true matches.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.matching import CompositeVerifier, load_labeled_pairs
from kalshi_arbitrage.validation.matching.evaluate import evaluate_matcher
from kalshi_arbitrage.validation.matching.gate import MatchingGate

FIXTURE = Path(__file__).resolve().parent / "data" / "matching" / "precision_regression.jsonl"


@pytest.fixture(scope="module")
def pairs():
    return load_labeled_pairs(str(FIXTURE))


def test_each_known_false_positive_is_rejected(pairs):
    c = CompositeVerifier()
    failures = []
    for p in pairs:
        if p.is_true_match():
            continue
        verdict = c.verify(*p.to_market_dicts())
        if verdict.passed:
            failures.append(f"{p.kalshi_title!r} == {p.polymarket_title!r}")
    assert not failures, "these false positives were NOT rejected:\n" + "\n".join(failures)


def test_known_true_positives_still_pass(pairs):
    c = CompositeVerifier()
    failures = []
    for p in pairs:
        if not p.is_true_match():
            continue
        verdict = c.verify(*p.to_market_dicts())
        if not verdict.passed:
            failures.append(f"{p.kalshi_title!r} == {p.polymarket_title!r} ({list(verdict.reasons)})")
    assert not failures, "these true positives were wrongly rejected:\n" + "\n".join(failures)


def test_precision_recall_metrics(pairs):
    report = evaluate_matcher(CompositeVerifier(), pairs)
    # Precision is the gate metric: no false positive may survive.
    assert report.precision == 1.0, report.summary()
    # Recall must stay healthy (don't nuke true matches to gain precision).
    assert report.recall >= 0.75, report.summary()


def test_matching_gate_passes(pairs):
    decision = MatchingGate(min_precision=0.95, min_recall=0.70,
                            bootstrap_samples=500).evaluate(CompositeVerifier(), pairs)
    assert decision.passed, decision.summary()
