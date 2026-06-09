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


def test_exclusion_clause_asymmetry_flags_uncertain():
    # Same deadline, but Polymarket carves out exceptions ("...will NOT qualify")
    # that Kalshi's one-liner doesn't — the arrest basis-risk. Not a hard reject
    # (recall), but flagged UNCERTAIN so it's held from auto-allowlist / sent to
    # the LLM tiebreaker.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will Tom Homan be arrested before Jan 2027?",
            "If Tom Homan is arrested before Jan 1, 2027, then Yes. Voluntarily "
            "surrendering pursuant to an indictment without an arrest warrant is sufficient."),
        _mk("Will Tom Homan be arrested before 2027?",
            "resolve Yes if arrested by December 31, 2026. A qualifying arrest includes "
            "house arrest or electronic monitoring. The following scenarios will not "
            "qualify: being named in an indictment without arrest."),
    )
    assert verdict.passed and verdict.uncertain
    assert any("exclusion_clause_asymmetry" in r for r in verdict.reasons)


def test_symmetric_rules_not_flagged():
    # Neither side has an exclusion clause → clean pass, not uncertain.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will Flavio Bolsonaro win the 2026 Brazilian presidential election?",
            "If Flavio Bolsonaro wins the 2026 Brazilian presidential election, then Yes."),
        _mk("Will Flavio Bolsonaro win the 2026 Brazilian presidential election?",
            "A presidential election is scheduled in Brazil on October 4, 2026. "
            "Resolves according to the candidate that wins this election."),
    )
    assert verdict.passed and not verdict.uncertain


def test_threshold_comparator_asymmetry_flags_uncertain():
    # The live Musk-trillionaire pair: Kalshi resolves on "MORE THAN $1 trillion"
    # (strict >), Polymarket on "REACHES OR EXCEEDS $1 trillion" (inclusive >=,
    # pinned to the Bloomberg Billionaires Index). Exactly $1.000T resolves YES
    # on PM and NO on Kalshi — same deadline, different resolution function.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will Elon Musk be a trillionaire before 2027?",
            "If Elon Musk has a net worth more than $1 trillion before Jan 1, 2027, "
            "then the market resolves to Yes."),
        _mk("Elon Musk trillionaire before 2027?",
            "This market will resolve to Yes if Elon Musk's net worth, as listed on "
            "the Bloomberg Billionaires Index, reaches or exceeds $1 trillion at any "
            "point by December 31, 2026, 11:59 PM ET."),
    )
    assert verdict.passed and verdict.uncertain
    assert any("threshold_comparator_asymmetry" in r for r in verdict.reasons)


def test_matching_comparators_not_flagged():
    v = ResolutionCongruenceVerifier()
    # strict/strict
    verdict = v.verify(
        _mk("Measles cases above 3000 in 2026?", "more than 3,000 cases by Dec 31, 2026"),
        _mk("Measles cases above 3000 in 2026?", "exceeds 3,000 cases by December 31, 2026"))
    assert verdict.passed and not verdict.uncertain
    # inclusive/inclusive
    verdict = v.verify(
        _mk("Measles cases 3000+ in 2026?", "at least 3,000 cases by Dec 31, 2026"),
        _mk("Measles cases 3000+ in 2026?", "3,000 or more cases by December 31, 2026"))
    assert verdict.passed and not verdict.uncertain


def test_no_threshold_language_not_flagged():
    # Comparator words without a numeric threshold must not fire (e.g. "above" in
    # a non-numeric context on one side only).
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will X win the 2026 election?", "wins the 2026 election described above"),
        _mk("Will X win the 2026 election?", "resolves to the winner of the 2026 election"))
    assert verdict.passed and not verdict.uncertain


def test_resolution_authority_asymmetry_flags_uncertain():
    # The live pandemic pair: Polymarket resolves on "official announcements
    # from the World Health Organization" — and the WHO has no formal pandemic-
    # declaration mechanism — while Kalshi resolves on "any disease becomes a
    # pandemic" with no source named. A colloquial pandemic with no WHO
    # declaration splits the venues; with K-NO + PM-YES legs that's a total
    # loss wearing an arbitrage costume.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Pandemic in 2026?",
            "If any disease becomes a pandemic in 2026, then the market resolves to Yes."),
        _mk("New pandemic in 2026?",
            "This market will resolve to Yes if the World Health Organization (WHO) "
            "declares any disease a pandemic between January 1, 2026 and December 31, "
            "2026 11:59 PM ET. The resolution source will be official announcements "
            "from the World Health Organization."),
    )
    assert verdict.passed and verdict.uncertain
    assert any("resolution_authority_asymmetry" in r for r in verdict.reasons)


def test_same_authority_both_sides_not_flagged():
    # Both venues pin the SAME authority → congruent, stays clean.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Pandemic in 2026?",
            "If the World Health Organization declares a pandemic in 2026, then Yes."),
        _mk("New pandemic in 2026?",
            "Resolves Yes if the World Health Organization (WHO) declares any disease "
            "a pandemic by December 31, 2026 11:59 PM ET."),
    )
    assert verdict.passed and not verdict.uncertain


def test_generic_official_results_phrasing_not_flagged():
    # "Official results/announcements" without a curated authority name must NOT
    # fire — else every election/sports pair lands in review and recall dies.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("Will Édouard Philippe win the 2027 French presidential election?",
            "If Édouard Philippe wins the 2027 French presidential election, then Yes."),
        _mk("Will Édouard Philippe win the 2027 French presidential election?",
            "Resolves to Yes according to the official results of the 2027 French "
            "presidential election announced by the government."),
    )
    assert verdict.passed and not verdict.uncertain


def test_authority_gate_needs_rules_text_on_both_sides():
    # An empty Kalshi side (e.g. transient rules-backfill failure) cannot be
    # said to "omit" an authority — must not mass-flag the catalog.
    v = ResolutionCongruenceVerifier()
    verdict = v.verify(
        _mk("New pandemic in 2026?", ""),
        _mk("New pandemic in 2026?",
            "Resolves Yes if the World Health Organization declares a pandemic "
            "by December 31, 2026."),
    )
    assert verdict.passed and not verdict.uncertain


def test_acronym_authorities_case_sensitive():
    # Sentence-initial "Who" must not read as the WHO.
    from kalshi_arbitrage.matching.verification import _named_authorities
    auth, has = _named_authorities(
        {"raw_data": {"rules_primary": "Who wins the 2026 election determines "
                                       "resolution of this market.",
                      "description": ""}})
    assert auth == frozenset() and has
    auth, _ = _named_authorities(
        {"raw_data": {"rules_primary": "Resolves per WHO announcements.",
                      "description": ""}})
    assert auth == frozenset({"WHO"})


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
