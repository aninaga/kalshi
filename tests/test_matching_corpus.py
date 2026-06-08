"""Large-corpus precision/recall gate for cross-venue matching.

691 real candidate pairs captured from a live LOSSLESS scan and HAND-LABELED by
a team of independent labelers (blind to the verifier's decisions), with a
shared 40-pair calibration set that reached 98% four-way agreement. This is the
statistical-power ground truth: it guards against precision OR recall
regressions across the full distribution of live market types (elections by
district, esports tournament stages, crypto, arrests, economic prints with
thresholds, sports leagues). Measured at commit: precision 0.985, recall 1.000.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.matching import CompositeVerifier, load_labeled_pairs
from kalshi_arbitrage.validation.matching.evaluate import evaluate_matcher
from kalshi_arbitrage.validation.matching.gate import MatchingGate

CORPUS = Path(__file__).resolve().parent / "data" / "matching" / "labeled_corpus.jsonl"


@pytest.fixture(scope="module")
def pairs():
    return load_labeled_pairs(str(CORPUS))


def test_corpus_is_substantial(pairs):
    # Guard against the fixture being truncated/emptied.
    assert len(pairs) >= 400
    trues = sum(1 for p in pairs if p.is_true_match())
    assert 50 <= trues <= len(pairs) - 50  # both classes well represented


def test_corpus_precision_and_recall(pairs):
    report = evaluate_matcher(CompositeVerifier(), pairs)
    # Precision is the hard gate (a false match = un-hedged trade).
    assert report.precision >= 0.98, report.summary()
    # Recall must also stay high — we proved 1.0 at commit time; allow a small
    # margin for future label additions without silently regressing.
    assert report.recall >= 0.95, report.summary()


def test_corpus_matching_gate(pairs):
    decision = MatchingGate(min_precision=0.97, min_recall=0.93,
                            bootstrap_samples=400).evaluate(CompositeVerifier(), pairs)
    assert decision.passed, decision.summary()
