"""Large-corpus precision/recall gate for cross-venue matching.

691 real candidate pairs captured from a live LOSSLESS scan and HAND-LABELED by
a team of independent labelers (blind to the verifier's decisions), with a
shared 40-pair calibration set that reached 98% four-way agreement. This is the
statistical-power ground truth: it guards against precision OR recall
regressions across the full distribution of live market types (elections by
district, esports tournament stages, crypto, arrests, economic prints with
thresholds, sports leagues). Measured at commit: precision 0.9925, recall
1.000 (2 residual false positives, both the asymmetric time-anchor case — "in
2028" vs "by September 30", "fight next" vs "in 2026"). Those 2 are the
genuinely-ambiguous residue an OPTIONAL LLM tiebreaker resolves to precision
1.000 when an ANTHROPIC_API_KEY is present (see test_llm_tiebreaker.py); this
gate measures the always-on DETERMINISTIC path, which must never depend on a
network call.
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


def _by_id(pairs, kalshi_id):
    matches = [p for p in pairs if p.kalshi_id == kalshi_id]
    assert matches, f"audit regression row {kalshi_id!r} missing from corpus"
    return matches[0]


def test_audit_c4_up_vs_down_resolves_inverted(pairs):
    # Regression row for audit C4: an up-vs-down pair is the SAME event with
    # OPPOSITE polarity. The verifier must resolve INVERTED (calling it ALIGNED
    # is an un-hedged two-leg position, the exact failure the module exists to
    # prevent).
    pair = _by_id(pairs, "AUDIT-C4-BTC-DOWN")
    assert pair.polarity == "inverted"
    verdict = CompositeVerifier().verify(*pair.to_market_dicts())
    assert verdict.passed
    assert verdict.polarity == "inverted", verdict.reasons


def test_audit_one_sided_scope_is_not_auto_clean(pairs):
    # Regression row for the one-sided out-of-gazetteer scope: "2026 election"
    # vs "2026 Jakarta election". It must NOT come back clean — held-for-review
    # (uncertain) is the floor, so it never auto-allowlists.
    pair = _by_id(pairs, "AUDIT-SCOPE-ELECTION-NATIONAL")
    assert not pair.is_true_match()
    verdict = CompositeVerifier().verify(*pair.to_market_dicts())
    assert verdict.uncertain, verdict.reasons
