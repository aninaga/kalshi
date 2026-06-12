"""Targeted regression tests for the matching-verification audit fixes.

Covers three defects that each silently turned a *non*-hedge into a clean
"go trade this" verdict:

  C4   OutcomePolarityVerifier called an up-vs-down pair ALIGNED (it is the
       SAME event with OPPOSITE polarity → INVERTED). Two underlying bugs:
       (a) tokenized the RAW title with str.split, so "down?" != "down" and no
           negation/direction signal fired → defaulted to ALIGNED;
       (b) "down" lived in BOTH the negation set and the UNDER-direction set, so
           even on a clean title the up/down pair double-flipped to ALIGNED.
  major one-sided out-of-gazetteer scope ("2026 election" vs "2026 Jakarta
       election") passed CLEAN because the distinct-subjects veto required BOTH
       sides to carry a leftover; a one-sided distinguishing token must be at
       least UNCERTAIN.
  major tools/run_machine.py replaced the whole 'approved' array every run,
       wiping operator (source="review") approvals; it must MERGE by source.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.matching.verification import (
    ALIGNED,
    INVERTED,
    DistinguishingEntityVerifier,
    OutcomePolarityVerifier,
    _direction,
    _negation_parity,
)


def _mkt(title, mid="x"):
    return {"id": mid, "title": title, "clean_title": "", "raw_data": {}}


# --------------------------------------------------------------------------- #
#  C4 — outcome polarity                                                       #
# --------------------------------------------------------------------------- #

def test_c4_punctuation_does_not_defeat_tokenizer():
    # The decisive bug: trailing "?" left a terminal token (e.g. "down?",
    # "won?") unmatched under str.split, so no signal fired.
    # Single odd negation ending in "?" must still register as a parity flip.
    assert _negation_parity("Will the bill fail?") is True
    assert _negation_parity("Will the bill pass?") is False
    # Direction words ending in "?" must still classify.
    assert _direction("Will Bitcoin end the year up?") == "over"
    assert _direction("Will Bitcoin end the year down?") == "under"


def test_c4_down_is_not_double_counted_as_negation():
    # "down" must be a DIRECTION word only, never also a negation — otherwise an
    # up/down pair double-flips and cancels to a spurious ALIGNED.
    from kalshi_arbitrage.matching.verification import _NEGATION_TOKENS
    assert "down" not in _NEGATION_TOKENS


def test_c4_up_vs_down_is_inverted_not_aligned():
    v = OutcomePolarityVerifier().verify(
        _mkt("Will Bitcoin end the year down?", "k"),
        _mkt("Will Bitcoin end the year up?", "p"),
    )
    assert v.polarity == INVERTED, v.reasons
    assert v.polarity != ALIGNED


def test_c4_same_direction_still_aligned():
    # Guard against over-correcting: matching directions stay ALIGNED.
    v = OutcomePolarityVerifier().verify(
        _mkt("Will Bitcoin end the year up?", "k"),
        _mkt("Will Bitcoin close the year up?", "p"),
    )
    assert v.polarity == ALIGNED, v.reasons


def test_c4_genuine_double_negation_still_cancels():
    # "not decline" vs "decline" is a single negation flip → INVERTED, and a
    # true double-flip (negation + direction, neither involving "down") cancels.
    v = OutcomePolarityVerifier().verify(
        _mkt("Will inflation not decline?", "k"),
        _mkt("Will inflation decline?", "p"),
    )
    assert v.polarity == INVERTED, v.reasons


# --------------------------------------------------------------------------- #
#  one-sided out-of-gazetteer scope                                            #
# --------------------------------------------------------------------------- #

def test_one_sided_scope_token_is_uncertain_not_clean():
    v = DistinguishingEntityVerifier().verify(
        _mkt("2026 election", "k"),
        _mkt("2026 Jakarta election", "p"),
    )
    # It must NOT be a clean pass; held-for-review (uncertain) is the floor.
    assert v.passed and v.uncertain, v.reasons
    assert "one_sided_distinguishing_token" in " ".join(v.reasons)


def test_one_sided_scope_symmetric_in_argument_order():
    v = DistinguishingEntityVerifier().verify(
        _mkt("2026 Jakarta election", "k"),
        _mkt("2026 election", "p"),
    )
    assert v.passed and v.uncertain, v.reasons


def test_two_sided_template_leftover_stays_clean():
    # Recall guard: "Team Falcons" vs "Falcons" differs only by a TEMPLATE word,
    # so it must stay a clean pass (not be dragged into review).
    v = DistinguishingEntityVerifier().verify(
        _mkt("Will Team Falcons win the Major?", "k"),
        _mkt("Will Falcons win the Major?", "p"),
    )
    assert v.passed and not v.uncertain, v.reasons


# --------------------------------------------------------------------------- #
#  run_machine allowlist merge                                                 #
# --------------------------------------------------------------------------- #

def _auto_row(ktk, pid, kt, pt):
    return {"ktk": ktk, "pid": pid, "polarity": "aligned", "net": 5.0,
            "net_edge": 0.03, "cost": 100.0, "kt": kt, "pt": pt}


def test_run_machine_merges_and_preserves_operator_approvals(tmp_path, monkeypatch):
    import tools.run_machine as rm

    allow = tmp_path / "match_allowlist.json"
    # An operator-approved pair already on file (as review_pairs.py writes it),
    # plus a STALE auto pair that the new discovery no longer surfaces.
    allow.write_text(json.dumps({
        "approved": [
            {"kalshi": "OP-K", "polymarket": "OP-P", "polarity": "aligned",
             "source": "review"},
            {"kalshi": "STALE-K", "polymarket": "STALE-P", "polarity": "aligned",
             "source": "auto"},
        ],
        "denied": [{"kalshi": "DENY-K", "polymarket": "DENY-P"}],
    }, indent=2))

    found = [_auto_row("NEW-K", "NEW-P", "new kalshi", "new poly")]
    monkeypatch.setattr(rm, "discover_buckets",
                        lambda *a, **k: ([1], found, [], []))

    rc = rm.main(["--allowlist", str(allow)])
    assert rc == 0

    out = json.loads(allow.read_text())
    keys = {(e["kalshi"], e["polymarket"]) for e in out["approved"]}
    # Operator approval survived.
    assert ("OP-K", "OP-P") in keys
    # Freshly discovered pair added.
    assert ("NEW-K", "NEW-P") in keys
    # Stale auto pair (no longer discovered) was dropped.
    assert ("STALE-K", "STALE-P") not in keys
    # Denied list preserved.
    assert {e["kalshi"] for e in out["denied"]} == {"DENY-K"}
    # Provenance preserved.
    by_key = {(e["kalshi"], e["polymarket"]): e for e in out["approved"]}
    assert by_key[("OP-K", "OP-P")]["source"] == "review"
    assert by_key[("NEW-K", "NEW-P")]["source"] == "auto"


def test_run_machine_prefers_operator_entry_on_conflict(tmp_path, monkeypatch):
    import tools.run_machine as rm

    allow = tmp_path / "match_allowlist.json"
    allow.write_text(json.dumps({
        "approved": [{"kalshi": "DUP-K", "polymarket": "DUP-P",
                      "polarity": "inverted", "source": "review"}],
        "denied": [],
    }, indent=2))

    # Discovery surfaces the SAME pair with a different polarity.
    found = [_auto_row("DUP-K", "DUP-P", "k", "p")]
    monkeypatch.setattr(rm, "discover_buckets",
                        lambda *a, **k: ([1], found, [], []))

    assert rm.main(["--allowlist", str(allow)]) == 0
    out = json.loads(allow.read_text())
    dup = [e for e in out["approved"]
           if (e["kalshi"], e["polymarket"]) == ("DUP-K", "DUP-P")]
    assert len(dup) == 1, "conflict must not duplicate the pair"
    assert dup[0]["source"] == "review"
    assert dup[0]["polarity"] == "inverted"  # operator decision wins


def test_run_machine_fails_loud_on_corrupt_allowlist(tmp_path, monkeypatch):
    import tools.run_machine as rm

    allow = tmp_path / "match_allowlist.json"
    allow.write_text("{ this is not valid json")
    monkeypatch.setattr(rm, "discover_buckets",
                        lambda *a, **k: ([1], [_auto_row("K", "P", "k", "p")], [], []))

    rc = rm.main(["--allowlist", str(allow)])
    # Must refuse to overwrite rather than silently wiping approvals.
    assert rc == 2
    # File untouched.
    assert allow.read_text() == "{ this is not valid json"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
