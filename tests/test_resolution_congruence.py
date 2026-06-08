"""ResolutionCongruenceVerifier: deadline-based basis-risk gate.

The matcher confirms two markets name the same subject/scope/proposition; this
gate confirms they resolve on the same *deadline*. The dangerous case is a pair
that looks identical but settles on different cutoffs — "reopen embassy before
Sep 1" vs "by Dec 31", "release album before Dec 1" vs "by Dec 31" — where a
window exists in which one resolves YES and the other NO. That's an un-hedged
directional bet wearing an arbitrage costume; these tests pin the real pairs I
found live, and guard that same-deadline phrasing variants are NOT rejected.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.matching import ResolutionCongruenceVerifier
from kalshi_arbitrage.matching.verification import _extract_deadline


def _mk(title, rules=""):
    return {"id": "x", "title": title, "clean_title": "",
            "raw_data": {"rules_primary": rules, "description": rules}}


# --- deadline extraction normalizes equivalent phrasings -------------------- #

def test_same_cutoff_phrasings_collapse():
    from datetime import date
    base = date(2026, 12, 31).toordinal()
    for text in ("arrested before Jan 1, 2027", "arrested by December 31, 2026",
                 "happens in 2026", "wins the 2026 election"):
        d = _extract_deadline(text)
        assert d is not None and abs(d - base) <= 1, (text, d)


def test_distinct_cutoffs_stay_apart():
    sep1 = _extract_deadline("reopen embassy before Sep 1, 2026")
    dec31 = _extract_deadline("reopen embassy by December 31, 2026")
    assert sep1 is not None and dec31 is not None
    assert abs(sep1 - dec31) > 100  # ~4 months


def test_no_deadline_returns_none():
    assert _extract_deadline("will it rain tomorrow in nyc") is None


# --- verifier: rejects real date-mismatch basis-risk pairs ------------------ #

def test_rejects_embassy_deadline_mismatch():
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will the US reopen its embassy in Iran before Sep 1, 2026?",
            "announces the opening of an embassy in Iran before Sep 1, 2026"),
        _mk("Will the US reopen its embassy in Iran in 2026?",
            "announces the reopening by December 31, 2026, 11:59 PM ET"),
    )
    assert not verdict.passed
    assert "deadline_mismatch" in verdict.reasons[0]


def test_rejects_album_deadline_mismatch():
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will Kendrick Lamar release a new album before Dec 1, 2026?",
            "releases a new album before Dec 1, 2026"),
        _mk("Will Kendrick Lamar release an album in 2026?",
            "officially releases a new album by December 31, 2026, 11:59 PM PT"),
    )
    assert not verdict.passed


# --- verifier: preserves genuine same-deadline pairs (recall guard) --------- #

def test_passes_arrest_same_instant_phrasing():
    # "before Jan 1, 2027" and "by December 31, 2026" are the SAME cutoff.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will Tom Homan be arrested before Jan 2027?",
            "If Tom Homan is arrested before Jan 1, 2027, then Yes."),
        _mk("Will Tom Homan be arrested before 2027?",
            "resolve Yes if arrested by December 31, 2026, 11:59 PM ET"),
    )
    assert verdict.passed


def test_passes_election_same_year():
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will Flavio Bolsonaro win the 2026 Brazilian presidential election?"),
        _mk("Will Flavio Bolsonaro win the 2026 Brazilian presidential election?",
            "A presidential election is scheduled in Brazil on October 4, 2026."),
    )
    assert verdict.passed


def test_passes_when_no_deadline_extractable():
    # Missing deadline → cannot assess → must NOT reject (recall-preserving).
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(_mk("Will it rain in NYC tomorrow"),
                       _mk("Will it rain in NYC tomorrow"))
    assert verdict.passed and "unverified" in verdict.reasons[0]


def test_tolerance_is_configurable():
    strict = ResolutionCongruenceVerifier(tolerance_days=0)
    # 1-day phrasing gap is rejected under zero tolerance...
    v = strict.verify(_mk("X before Jan 1, 2027", "before Jan 1, 2027"),
                      _mk("X by Dec 31, 2026", "by December 31, 2026"))
    assert not v.passed
    # ...but passes under the default 7-day tolerance.
    assert ResolutionCongruenceVerifier().verify(
        _mk("X before Jan 1, 2027", "before Jan 1, 2027"),
        _mk("X by Dec 31, 2026", "by December 31, 2026")).passed
