"""Phase A: cross-venue match verification tests."""

import re
from pathlib import Path

import pytest

from kalshi_arbitrage.matching import (
    ALIGNED,
    INVERTED,
    UNKNOWN,
    AllowlistVerifier,
    CompositeVerifier,
    OutcomePolarityVerifier,
    ResolutionCriteriaVerifier,
)


def _mkt(title, close_time=None, rules="", market_id="m"):
    return {
        "id": market_id,
        "title": title,
        "clean_title": "",
        "close_time": close_time,
        "raw_data": {"rules_primary": rules, "description": rules},
    }


# --- OutcomePolarityVerifier ------------------------------------------------ #

def test_polarity_negation_flip_is_inverted():
    v = OutcomePolarityVerifier()
    verdict = v.verify(
        _mkt("Will Trump win the 2028 election"),
        _mkt("Will Trump NOT win the 2028 election"),
    )
    assert verdict.polarity == INVERTED


def test_polarity_threshold_direction_flip_is_inverted():
    v = OutcomePolarityVerifier()
    verdict = v.verify(
        _mkt("Will CPI be over 3%"),
        _mkt("Will CPI be under 3%"),
    )
    assert verdict.polarity == INVERTED


def test_polarity_same_direction_is_aligned():
    v = OutcomePolarityVerifier()
    verdict = v.verify(
        _mkt("Will CPI be above 3%"),
        _mkt("Will CPI be above 3%"),
    )
    assert verdict.polarity == ALIGNED


def test_polarity_plain_identical_is_aligned():
    v = OutcomePolarityVerifier()
    verdict = v.verify(_mkt("Will it rain in NYC tomorrow"), _mkt("Will it rain in NYC tomorrow"))
    assert verdict.polarity == ALIGNED


# --- ResolutionCriteriaVerifier --------------------------------------------- #

def test_resolution_rejects_different_year_close_times():
    # A DIFFERENT resolution year is a different event.
    v = ResolutionCriteriaVerifier()
    verdict = v.verify(
        _mkt("X happens", close_time="2026-06-01T00:00:00"),
        _mkt("X happens", close_time="2028-06-01T00:00:00"),
    )
    assert not verdict.passed


def test_resolution_accepts_large_same_year_close_skew():
    # The two venues legitimately list different close dates for the SAME event
    # (election day vs certification / far-out resolve-by), so a months-long
    # same-year skew must NOT reject — this was a recall killer on live data.
    v = ResolutionCriteriaVerifier()
    verdict = v.verify(
        _mkt("X happens", close_time="2026-01-01T00:00:00"),
        _mkt("X happens", close_time="2026-06-01T00:00:00"),
    )
    assert verdict.passed


def test_resolution_rejects_threshold_mismatch():
    v = ResolutionCriteriaVerifier()
    # Player prop: 8+ (=>7.5) vs O/U 4.5 differ by >1.0 → incompatible
    verdict = v.verify(
        _mkt("LeBron James points 8+"),
        _mkt("LeBron James points O/U 4.5"),
    )
    assert not verdict.passed


# --- AllowlistVerifier ------------------------------------------------------ #

def test_allowlist_denied_fails(tmp_path):
    f = tmp_path / "allow.json"
    f.write_text('{"approved": [], "denied": [{"kalshi": "K1", "polymarket": "P1"}]}')
    v = AllowlistVerifier(path=str(f))
    verdict = v.verify(_mkt("a", market_id="K1"), _mkt("a", market_id="P1"))
    assert not verdict.passed


def test_allowlist_approved_passes_with_polarity(tmp_path):
    f = tmp_path / "allow.json"
    f.write_text(
        '{"approved": [{"kalshi": "K1", "polymarket": "P1", "polarity": "inverted"}], "denied": []}'
    )
    v = AllowlistVerifier(path=str(f))
    verdict = v.verify(_mkt("a", market_id="K1"), _mkt("a", market_id="P1"))
    assert verdict.passed and verdict.polarity == INVERTED


def test_allowlist_unknown_pair_flagged_not_allowlisted(tmp_path):
    f = tmp_path / "allow.json"
    f.write_text('{"approved": [], "denied": []}')
    v = AllowlistVerifier(path=str(f))
    verdict = v.verify(_mkt("a", market_id="K9"), _mkt("a", market_id="P9"))
    assert verdict.passed and "not_allowlisted" in verdict.reasons


# --- CompositeVerifier ------------------------------------------------------ #

def test_composite_fails_when_any_verifier_fails():
    c = CompositeVerifier()
    verdict = c.verify(
        _mkt("X happens", close_time="2026-01-01T00:00:00"),
        _mkt("X happens", close_time="2027-06-01T00:00:00"),
    )
    assert not verdict.passed


def test_composite_require_allowlist_blocks_unlisted(tmp_path):
    f = tmp_path / "allow.json"
    f.write_text('{"approved": [], "denied": []}')
    c = CompositeVerifier(verifiers=[AllowlistVerifier(path=str(f)), OutcomePolarityVerifier()],
                          require_allowlist=True)
    verdict = c.verify(_mkt("rain", market_id="K1"), _mkt("rain", market_id="P1"))
    assert not verdict.passed


def test_composite_carries_inverted_polarity_through():
    c = CompositeVerifier()
    verdict = c.verify(
        _mkt("Will the Lakers win tonight"),
        _mkt("Will the Lakers NOT win tonight"),
    )
    assert verdict.passed and verdict.polarity == INVERTED


# --- No hardcoded special-cases regression ---------------------------------- #

def test_no_hardcoded_subject_strings_in_utils():
    """Phase A removed brittle whack-a-mole special-cases from utils.py."""
    src = Path("kalshi_arbitrage/utils.py").read_text().lower()
    for banned in ("powell", "leavepowell", "'fed'", "'chair'", "'admin'"):
        assert banned not in src, f"hardcoded special-case {banned!r} still present in utils.py"
