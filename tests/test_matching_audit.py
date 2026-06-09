"""Regression tests for the 2026-06-09 matching-algorithm audit.

The audit of the LIVE watch-list (315 pairs) found three precision failures
and one recall failure, each pinned here against the real market text:

  P1. Kalshi's World Series market ("Pro Baseball Championship") matched PM's
      AL-pennant market ("American League Championship Series") and was
      labeled CLEAN — hierarchically related markets at different tiers of
      the same tournament are not the same event.
  P2. Kalshi "Racing Bulls" matched BOTH PM "Racing Bulls" (right) and PM
      "Red Bull Racing" (wrong, sister team): one binary market equals at
      most one binary market on the other venue, so multi-pairings need
      arbitration.
  P3. (dissolved on inspection: PM Berlin pins "greatest number of seats" but
      Kalshi's rules_secondary defines win-the-election the same way — the
      seats gate must stay SYMMETRIC-quiet on that real pair.)
  R1. 33 EXACT-title "next Prime Minister of Romania/Israel" pairs were
      hard-rejected by the rules-containment check (terse Kalshi one-liner vs
      PM legalese, containment 0.33-0.36). At near-identical titles, rules
      divergence means review-not-reject.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.live_probe import _arbitrate_conflicts
from kalshi_arbitrage.matching import CompositeVerifier
from kalshi_arbitrage.matching.verification import (
    ResolutionCriteriaVerifier,
    _competition_tiers,
)
from kalshi_arbitrage.utils import clean_title


def _kd(title, rules):
    return {"id": "K", "title": title, "clean_title": clean_title(title),
            "raw_data": {"rules_primary": rules, "rules_secondary": ""}}


def _pd(title, desc):
    return {"id": "P", "title": title, "clean_title": clean_title(title),
            "raw_data": {"description": desc}}


# --- P1: competition-tier asymmetry ----------------------------------------- #

ALCS_PM = _pd(
    "Will Los Angeles Angels win the 2026 American League Championship Series?",
    "This market will resolve according to the team that wins the 2026 "
    "American League Championship Series. If at any point it becomes "
    "impossible for a listed team to win per the rules of the MLB, the "
    "corresponding market will resolve to No.")


def test_world_series_vs_pennant_flags_uncertain():
    verdict = CompositeVerifier().verify(
        _kd("Will Los Angeles A win the 2026 Pro Baseball Championship?",
            "If Los Angeles A wins the 2026 Pro Baseball Championship, "
            "then the market resolves to Yes."),
        ALCS_PM)
    assert verdict.passed and verdict.uncertain
    assert any("competition_tier_asymmetry" in r for r in verdict.reasons)


def test_pennant_vs_pennant_stays_clean():
    verdict = CompositeVerifier().verify(
        _kd("Will Los Angeles A win the 2026 Pro Baseball American League "
            "Championship?",
            "If Los Angeles A win the 2026 Pro Baseball American League "
            "Championship, then the market resolves to Yes."),
        ALCS_PM)
    assert verdict.passed and not verdict.uncertain


def test_bare_league_acronym_is_tier_equivalent_to_full_phrase():
    # Kalshi "NL Hank Aaron Award" vs PM "National League Hank Aaron Award"
    # must NOT create a tier asymmetry.
    k = _competition_tiers({"title": "Will Shohei Ohtani win NL Hank Aaron Award?",
                            "raw_data": {}})
    p = _competition_tiers({"title": "Will Shohei Ohtani win the 2026 National "
                                     "League Hank Aaron Award?", "raw_data": {}})
    assert k == p == frozenset({"national_league"})


def test_tier_acronyms_are_case_sensitive():
    # "Al Gore" must never read as the American League.
    assert _competition_tiers({"title": "Will Al Gore endorse a candidate?",
                               "raw_data": {}}) == frozenset()


# --- P2: one market == at most one counterpart (conflict arbitration) ------- #

def test_arbitration_demotes_lookalike_pairing():
    # Live: Kalshi "Racing Bulls" matched PM "Racing Bulls" (sim 1.0) AND PM
    # "Red Bull Racing" (lower sim). The lookalike is demoted on BOTH key
    # sides; the two genuine pairings are untouched.
    pairs = [{"ktk": "RAC", "pid": "P_RAC", "sim": 1.0, "uncertain": False},
             {"ktk": "RAC", "pid": "P_RED", "sim": 0.88, "uncertain": False},
             {"ktk": "RED", "pid": "P_RED", "sim": 1.0, "uncertain": False}]
    out = _arbitrate_conflicts(pairs)
    assert [p["uncertain"] for p in out] == [False, True, False]
    assert out[1]["conflict"] == "ktk,pid"
    assert "conflict" not in out[0] and "conflict" not in out[2]


def test_arbitration_keeps_already_uncertain_and_tags_conflict():
    pairs = [{"ktk": "K1", "pid": "PA", "sim": 0.99, "uncertain": True},
             {"ktk": "K1", "pid": "PB", "sim": 0.90, "uncertain": True}]
    out = _arbitrate_conflicts(pairs)
    assert all(p["uncertain"] for p in out)
    assert "conflict" not in out[0] and out[1]["conflict"] == "ktk"


def test_arbitration_no_conflicts_is_identity():
    pairs = [{"ktk": "K1", "pid": "P1", "sim": 0.9, "uncertain": False},
             {"ktk": "K2", "pid": "P2", "sim": 0.8, "uncertain": True}]
    assert _arbitrate_conflicts(pairs) == pairs


# --- R1: rules containment at near-identical titles → review, not reject ---- #

PONTA_K = _kd(
    "Will Victor Ponta be the next Prime Minister of Romania?",
    "If the first new person to hold Prime Minister of Romania after Issuance "
    "is Victor Ponta, then the market resolves to Yes.")
PONTA_P = _pd(
    "Will Victor Ponta be the next Prime Minister of Romania?",
    "This market will resolve to the next individual who officially assumes "
    "the office of Prime Minister of Romania by December 31, 2027, 11:59 PM "
    "ET. To count for resolution, the individual must be formally appointed "
    "by the President following nomination procedures.")


def test_exact_title_with_terse_vs_legalese_rules_is_review_not_reject():
    verdict = CompositeVerifier().verify(PONTA_K, PONTA_P)
    assert verdict.passed and verdict.uncertain
    assert any("rules_divergent_review" in r for r in verdict.reasons)


def test_moderate_similarity_divergent_rules_still_hard_rejects():
    # Below the title bar, low containment keeps meaning "different events
    # sharing a template" and must stay a hard reject.
    verdict = ResolutionCriteriaVerifier().verify(
        _kd("Will the next big thing happen at the June summit?",
            "If the delegates ratify the fusion accord at the June summit "
            "in Geneva, then the market resolves to Yes."),
        _pd("Will the next big thing happen at the trade summit?",
            "This market resolves Yes when customs tariffs are abolished "
            "across all member states following the trade summit."))
    assert not verdict.passed
    assert any("rules_divergent containment" in r for r in verdict.reasons)


# --- P3 guard: symmetric seats criterion stays clean ------------------------ #

def test_seats_plurality_on_both_sides_stays_clean():
    # The real Berlin pair: PM pins "greatest number of seats" and Kalshi's
    # rules ALSO define the win as most-seats — symmetric, no flag.
    verdict = CompositeVerifier().verify(
        _kd("Will CDU win the 2026 Berlin state election?",
            "If CDU wins the 2026 Berlin state election, then the market "
            "resolves to Yes. For parliamentary elections, the party or "
            "coalition with the most seats wins."),
        _pd("Will CDU win the most seats in the 2026 Berlin state elections?",
            "Parliamentary elections to elect the Abgeordnetehaus of Berlin "
            "are scheduled to take place in Berlin on September 20, 2026. "
            "This market will resolve to the political party that wins the "
            "greatest number of seats in the formal deliberative assembly of "
            "Berlin (Abgeordnetenhaus) as a result of this election."))
    assert verdict.passed and not verdict.uncertain


def test_seats_plurality_on_one_side_only_flags_uncertain():
    verdict = CompositeVerifier().verify(
        _kd("Will the Purple Party win the 2030 Ruritania election?",
            "If the Purple Party wins the 2030 Ruritania general election, "
            "then the market resolves to Yes. The outcome is determined by "
            "official government announcements of the final result."),
        _pd("Will the Purple Party win the 2030 Ruritania election?",
            "This market will resolve to the political party that wins the "
            "greatest number of seats in the national assembly of Ruritania "
            "as a result of the 2030 general election, according to official "
            "government announcements of the final result."))
    assert verdict.passed and verdict.uncertain
    assert any("seats_plurality_asymmetry" in r for r in verdict.reasons)
