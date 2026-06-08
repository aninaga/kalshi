"""Cross-venue match verification (Phase A).

Lexical similarity (``utils.get_similarity_score``) tells us two market titles
*look* alike. That is necessary but nowhere near sufficient to trade on: for
arbitrage the only thing that matters is whether the two contracts resolve to
the *same real-world outcome*. A false positive in the execution path fires two
legs believing they hedge when they don't, leaving an un-hedged directional
position — the opposite of risk-free arbitrage.

This module adds a pluggable verification layer that runs *after* the
similarity threshold passes:

  * ``OutcomePolarityVerifier``     — does Kalshi-YES mean the same event as the
                                      Polymarket YES token, or its complement?
  * ``ResolutionCriteriaVerifier``  — do close dates / thresholds / rules agree?
  * ``AllowlistVerifier``           — operator allow/deny override (Phase D gate)
  * ``CompositeVerifier``           — runs all; a match passes only if none fail.

Each verifier consumes the *processed market dicts* produced by
``MarketAnalyzer._process_*_markets`` (keys: ``title``, ``clean_title``,
``close_time``, ``raw_data``) and returns a :class:`MatchVerdict`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Protocol, Sequence

from ..config import Config
from ..utils import (
    _BOILERPLATE,
    clean_title,
    extract_prop_threshold,
    thresholds_compatible,
)
from .gazetteer import extract_scope_entities, is_national_scope

logger = logging.getLogger(__name__)

# Polarity labels.
ALIGNED = "aligned"      # Kalshi YES  ==  Polymarket YES token
INVERTED = "inverted"    # Kalshi YES  ==  Polymarket NO token
UNKNOWN = "unknown"      # could not resolve

# Words that flip the meaning of an otherwise-identical proposition.
_NEGATION_TOKENS = frozenset({
    "not", "no", "never", "without", "fail", "fails", "failed",
    "miss", "misses", "missed", "lose", "loses", "lost",
})

# Direction words for threshold markets (over/under, above/below).
_OVER_TOKENS = frozenset({"over", "above", "more", "greater", "exceed", "exceeds", "least", "higher", "up"})
_UNDER_TOKENS = frozenset({"under", "below", "less", "fewer", "lower", "most", "down", "beneath"})


@dataclass(frozen=True)
class MatchVerdict:
    """Outcome of verifying a single candidate (Kalshi, Polymarket) pair."""

    passed: bool
    polarity: str = UNKNOWN          # ALIGNED | INVERTED | UNKNOWN
    confidence: float = 0.0          # 0..1 — how sure the verifier is
    reasons: tuple = field(default_factory=tuple)  # human-readable explanations
    verifier: str = ""               # which verifier produced this verdict

    def to_dict(self) -> Dict:
        return {
            "passed": self.passed,
            "polarity": self.polarity,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
            "verifier": self.verifier,
        }


class MatchVerifier(Protocol):
    """Anything that can adjudicate a candidate match."""

    def verify(self, kalshi_market: Dict, polymarket_market: Dict) -> MatchVerdict:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------- #
#  Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _tokens(market: Dict) -> set:
    """Distinguishing (non-boilerplate) tokens of a market's clean title."""
    text = clean_title(market.get("title") or market.get("clean_title") or "")
    return set(text.split()) - _BOILERPLATE


def _direction(text: str) -> Optional[str]:
    """Return 'over' / 'under' if the title expresses a threshold direction."""
    words = set(text.lower().split())
    over = bool(words & _OVER_TOKENS) or "+" in text
    under = bool(words & _UNDER_TOKENS)
    if over and not under:
        return "over"
    if under and not over:
        return "under"
    return None


def _negation_parity(text: str) -> bool:
    """True if the title contains an odd number of negations (meaning flipped)."""
    words = text.lower().split()
    neg = sum(1 for w in words if w in _NEGATION_TOKENS)
    # treat "won't" / "wont" style contractions
    neg += len(re.findall(r"\bwon'?t\b|\bcan'?t\b|\bdoesn'?t\b", text.lower()))
    return (neg % 2) == 1


def _parse_time(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat,):
        try:
            dt = parse(s)
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _rules_text(market: Dict) -> str:
    """Best-effort resolution-criteria text for a market."""
    raw = market.get("raw_data", {}) or {}
    parts: List[str] = []
    for key in ("rules_primary", "rules_secondary", "description", "subtitle"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    return clean_title(" ".join(parts)) if parts else ""


def _clean(market: Dict) -> str:
    """Canonically-cleaned title for a market.

    Always re-normalize the raw title with utils.clean_title rather than trust
    the venue's pre-set clean_title — the per-venue cleaners differ (one keeps
    hyphens/'?', e.g. "second-hottest", "record?"), which would corrupt the
    token sets the entity/qualifier checks rely on.
    """
    return clean_title(market.get("title") or market.get("clean_title") or "")


def _scope_text(market: Dict) -> str:
    """Cleaned title PLUS the Kalshi scope subtitles, for geography extraction.

    A state-race Kalshi market often carries the state only in its
    yes_sub_title / title; fold those in so the gazetteer sees them.
    """
    raw = market.get("raw_data", {}) or {}
    extra = " ".join(
        str(raw.get(k, "")) for k in ("yes_sub_title", "no_sub_title", "subtitle")
        if raw.get(k)
    )
    base = _clean(market)
    return f"{base} {clean_title(extra)}".strip() if extra else base


def _token_matches(word: str, larger: set) -> bool:
    """Does ``word`` refer to the SAME entity as some token in ``larger``?

    Tolerant of spelling/punctuation variants (republican/republicans,
    jaemyung/myung) without relaxing into different entities:
      - exact match
      - shared 4+ char prefix (either direction)
      - one token is a substring of the other (>=4 chars) — handles concatenated
        multi-word names like "jaemyung" containing "myung"
      - high-cutoff fuzzy ratio (>=0.9) for single-token spelling variants
    """
    if word in larger:
        return True
    if len(word) >= 4:
        pre = word[:4]
        for lw in larger:
            if len(lw) >= 4 and (lw.startswith(pre) or word.startswith(lw[:4])):
                # Shared 4-char prefix, BUT reject if one carries a negating/
                # ordinal qualifier the other lacks (hottest vs secondhottest,
                # which would otherwise match on substring) — handled by the
                # length-ratio guard: a true variant is similar length.
                if min(len(word), len(lw)) / max(len(word), len(lw)) >= 0.6:
                    return True
            # Substring only when the shorter token is itself >= 5 chars (a real
            # name component like "myung" in "jaemyung"), not a short common word.
            if len(lw) >= 5 and len(word) >= 5 and (word in lw or lw in word):
                return True
    for lw in larger:
        if len(word) >= 4 and len(lw) >= 4 and _ratio(word, lw) >= 0.9:
            return True
    return False


def _ratio(a: str, b: str) -> float:
    """Pure-stdlib similarity ratio (no deps) for single-token spelling variants."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


# Words that, when present on only ONE side, change the PROPOSITION (not the
# subject): ordinals/superlatives ranking, and outcome-stage verbs. Same subject
# + a divergent qualifier here = different question (e.g. "hottest" vs
# "second-hottest" year; "on the ballot" vs "win").
_PREDICATE_QUALIFIERS = frozenset({
    "hottest", "coldest", "highest", "lowest", "biggest", "largest", "smallest",
    "secondhottest", "secondhighest", "secondlowest", "second", "third",
    "ballot", "nominee", "nominated", "primary", "runoff", "reelection",
    "impeached", "indicted", "convicted", "acquitted",
})

# Generic event/structural words that recur across contests and are NOT the
# distinguishing subject (the team/person/topic is). Removed before the
# "distinct subjects" check so an event template ("qualify stage IEM Cologne
# Major") doesn't mask a different competitor, and a lone leftover template word
# ("Team", "Major", "MoM") doesn't falsely split a true match.
_TEMPLATE_TOKENS = frozenset({
    "team", "major", "minor", "cup", "open", "series", "match", "stage",
    "tournament", "championship", "league", "qualify", "qualifier", "qualified",
    "round", "final", "finals", "group", "playoffs", "playoff", "bracket",
    "record", "edition", "tour", "event", "title", "medal", "award",
    "rise", "rose", "fall", "fell", "mom", "yoy", "month", "monthly",
    "core", "headline", "report", "data", "number", "level", "value",
})


def _is_categorical(market: Dict) -> bool:
    """True if the title poses a multi-outcome question ("Who will...", "Which
    ...", "What will...") rather than a single-subject yes/no proposition.

    Such a Kalshi market enumerates candidates as separate contracts and must
    not pair with a specific-subject binary on the other venue.
    """
    title = (market.get("title") or "").strip().lower()
    first = title.split()[0] if title.split() else ""
    return first in {"who", "which"} or title.startswith("what will")


# Elected offices / contest types. Stripped as boilerplate for the *entity*
# overlap, but they DISTINGUISH the contest — an Attorney-General race is not a
# Governor race even in the same state. Multi-word offices are canonical keys.
_OFFICE_PHRASES = {
    ("attorney", "general"): "attorney_general",
    ("secretary", "of", "state"): "secretary_of_state",
    ("lieutenant", "governor"): "lieutenant_governor",
}
_OFFICE_SINGLE = {
    "governor": "governor", "governorship": "governor",
    "senate": "senate", "senator": "senate",
    "house": "house",
    "mayor": "mayor", "mayoral": "mayor",
    "president": "president", "presidency": "president", "presidential": "president",
    "comptroller": "comptroller", "treasurer": "treasurer",
    "sheriff": "sheriff", "council": "council",
}


def _offices(clean_text: str) -> set:
    """Set of elected-office keys named in a cleaned title."""
    tokens = clean_text.split()
    found = set()
    consumed = set()
    n = len(tokens)
    for i in range(n):
        if i in consumed:
            continue
        for plen, key in (
            (3, _OFFICE_PHRASES.get(tuple(tokens[i:i + 3]))),
            (2, _OFFICE_PHRASES.get(tuple(tokens[i:i + 2]))),
        ):
            if key:
                found.add(key)
                consumed.update(range(i, i + plen))
                break
    for i, tok in enumerate(tokens):
        if i not in consumed and tok in _OFFICE_SINGLE:
            found.add(_OFFICE_SINGLE[tok])  # canonical form (governorship->governor)
    return found


# --------------------------------------------------------------------------- #
#  Verifiers                                                                    #
# --------------------------------------------------------------------------- #

class OutcomePolarityVerifier:
    """Resolve whether Kalshi-YES maps to the Polymarket YES token or its NO.

    Heuristic and intentionally conservative: it returns INVERTED only when it
    finds a clear meaning-flip (odd negation parity difference, or opposite
    threshold directions). When it finds clear agreement it returns ALIGNED.
    Otherwise UNKNOWN — the composite decides whether UNKNOWN is fatal.
    """

    name = "outcome_polarity"

    def verify(self, kalshi_market: Dict, polymarket_market: Dict) -> MatchVerdict:
        k_title = clean_title(kalshi_market.get("title") or kalshi_market.get("clean_title") or "")
        p_title = clean_title(polymarket_market.get("title") or polymarket_market.get("clean_title") or "")

        reasons: List[str] = []

        # 1) Negation parity: an odd difference flips meaning.
        k_neg = _negation_parity(kalshi_market.get("title", "") or k_title)
        p_neg = _negation_parity(polymarket_market.get("title", "") or p_title)
        neg_flip = k_neg != p_neg

        # 2) Threshold direction.
        k_dir = _direction(kalshi_market.get("title", "") or k_title)
        p_dir = _direction(polymarket_market.get("title", "") or p_title)
        dir_flip = k_dir is not None and p_dir is not None and k_dir != p_dir
        dir_same = k_dir is not None and p_dir is not None and k_dir == p_dir

        if neg_flip and not dir_flip:
            reasons.append("negation_parity_differs")
            return MatchVerdict(True, INVERTED, 0.75, tuple(reasons), self.name)
        if dir_flip and not neg_flip:
            reasons.append(f"threshold_direction k={k_dir} p={p_dir}")
            return MatchVerdict(True, INVERTED, 0.8, tuple(reasons), self.name)
        if neg_flip and dir_flip:
            # two flips cancel out → aligned
            reasons.append("double_flip_cancels")
            return MatchVerdict(True, ALIGNED, 0.7, tuple(reasons), self.name)

        if dir_same:
            reasons.append(f"threshold_direction_match={k_dir}")
            return MatchVerdict(True, ALIGNED, 0.8, tuple(reasons), self.name)

        # No negation/threshold signal on either side and titles are similar:
        # default to ALIGNED with modest confidence (binary YES/YES is the
        # overwhelmingly common case for matched contracts).
        if not k_neg and not p_neg:
            reasons.append("no_flip_signals")
            return MatchVerdict(True, ALIGNED, 0.55, tuple(reasons), self.name)

        reasons.append("ambiguous_polarity")
        return MatchVerdict(True, UNKNOWN, 0.0, tuple(reasons), self.name)


class DistinguishingEntityVerifier:
    """Reject pairs whose distinguishing entity / scope differs.

    The lexical similarity score rewards shared boilerplate, so different
    real-world events with the same template ("Will <X> be arrested before
    2027", "Will the Republicans win the <Y> Senate") score 0.76-0.82 and slip
    through the threshold. After stripping boilerplate, the SET of distinguishing
    tokens (the subject/scope) must agree. Two complementary checks:

      (A) Geography/scope veto — if the two titles name different states or
          countries, or one is national while the other names a sub-scope (state),
          reject. Catches U.S.-Senate vs Montana-Senate, Israel-Qatar vs
          Israel-Saudi, national-governorship vs South-Dakota-governor.
      (B) Distinguishing-entity overlap — the smaller distinguishing token set
          must be >= MATCH_MIN_DISTINGUISHING_OVERLAP covered by the other,
          tolerant of spelling variants. Catches Kash-Patel vs Joe-Biden.

    Runs for ALL above-threshold pairs (verifiers run after the similarity gate),
    so it fires in the 0.76-0.85 band where utils._penalize_divergent_terms does
    not. Deterministic, pure-Python.
    """

    name = "distinguishing_entity"

    def __init__(self, min_overlap: Optional[float] = None):
        self.min_overlap = (
            min_overlap if min_overlap is not None
            else getattr(Config, "MATCH_MIN_DISTINGUISHING_OVERLAP", 0.5)
        )

    def verify(self, kalshi_market: Dict, polymarket_market: Dict) -> MatchVerdict:
        reasons: List[str] = []

        # (A0) Categorical (multi-outcome) veto. A "Who will / Which X" market
        # enumerates many candidates as separate contracts; it is NOT the same
        # tradeable contract as a single-subject binary ("Will <specific> X").
        # Matching them yields a structurally wrong "buy YES / sell YES" hedge.
        k_cat = _is_categorical(kalshi_market)
        p_cat = _is_categorical(polymarket_market)
        if k_cat != p_cat:
            return MatchVerdict(False, UNKNOWN, 0.0,
                                (f"categorical_vs_binary k_cat={k_cat} p_cat={p_cat}",), self.name)

        # (A) Geography / scope veto. Use title + Kalshi subtitles for scope.
        # The scope SETS must be equal: if either venue names a place the other
        # doesn't, it's a different event — whether that's national-vs-state
        # (k={} p={montana}) or a country swap (k={israel,qatar} p={israel,
        # saudi_arabia}, which share israel but differ on the distinguishing one).
        k_scope = extract_scope_entities(_scope_text(kalshi_market))
        p_scope = extract_scope_entities(_scope_text(polymarket_market))
        if k_scope != p_scope:
            return MatchVerdict(False, UNKNOWN, 0.0,
                                (f"scope_mismatch k={sorted(k_scope)} p={sorted(p_scope)}",),
                                self.name)

        # (A1) Office veto: a contest for a DIFFERENT elected office is a
        # different event (Attorney General race vs Governor race in the same
        # state). Offices are stripped as boilerplate for entity overlap, so
        # check them explicitly: if both name an office and they don't intersect,
        # reject.
        k_off = _offices(_clean(kalshi_market))
        p_off = _offices(_clean(polymarket_market))
        if k_off and p_off and not (k_off & p_off):
            return MatchVerdict(False, UNKNOWN, 0.0,
                                (f"office_mismatch k={sorted(k_off)} p={sorted(p_off)}",), self.name)

        kt = set(_clean(kalshi_market).split()) - _BOILERPLATE
        pt = set(_clean(polymarket_market).split()) - _BOILERPLATE

        # (B) Numeric/year veto: a year or numeric token present on BOTH sides
        # must agree. Different years (2026 vs 2028) → different event. (A shared
        # year is template, not identity, so it's excluded from the entity
        # overlap below.)
        k_nums = {w for w in kt if any(c.isdigit() for c in w)}
        p_nums = {w for w in pt if any(c.isdigit() for c in w)}
        if k_nums and p_nums and not (k_nums & p_nums):
            return MatchVerdict(False, UNKNOWN, 0.0,
                                (f"numeric_mismatch k={sorted(k_nums)} p={sorted(p_nums)}",),
                                self.name)

        # (C) Distinguishing-ENTITY check on the ALPHABETIC tokens (subject /
        # identity). Numeric tokens are excluded so a shared year can't inflate
        # different subjects. The decisive rule: after removing matched tokens
        # and generic template words, if BOTH sides still carry a distinct
        # substantive token, they name DIFFERENT subjects — reject. This catches
        # "TYLOO" vs "BetBoom" (event template "qualify stage IEM Cologne"
        # matches, but the team differs) and "Kash Patel" vs "Joe Biden", while
        # passing "Vitality"=="Vitality" and "Team Falcons"=="Falcons" (only one
        # side has a leftover template word).
        # Entity tokens: anything with a letter (keeps alphanumeric team/ticker
        # names like "g2", "b8", "ma06"); pure-numeric tokens (years) are
        # excluded — they're handled by the numeric veto above.
        ke = {w for w in kt if any(c.isalpha() for c in w)}
        pe = {w for w in pt if any(c.isalpha() for c in w)}
        if not ke or not pe:
            return MatchVerdict(True, UNKNOWN, 0.3, ("no_entity_tokens",), self.name)
        k_only = {w for w in ke if not _token_matches(w, pe)} - _TEMPLATE_TOKENS
        p_only = {w for w in pe if not _token_matches(w, ke)} - _TEMPLATE_TOKENS
        if k_only and p_only:
            return MatchVerdict(False, UNKNOWN, 0.0,
                                (f"distinct_subjects k={sorted(k_only)} p={sorted(p_only)}",),
                                self.name)
        # Secondary: require a reasonable fraction of the smaller set to match
        # (guards the case where one side is a strict superset of unrelated
        # template words).
        smaller, larger = (ke, pe) if len(ke) <= len(pe) else (pe, ke)
        matched = sum(1 for w in smaller if _token_matches(w, larger))
        overlap = matched / len(smaller)
        if overlap < self.min_overlap:
            return MatchVerdict(False, UNKNOWN, 0.0,
                                (f"entity_overlap={overlap:.2f} sm={sorted(smaller)}",), self.name)

        # (D) Predicate-qualifier veto: same subject, DIFFERENT proposition.
        # An ordinal/superlative/outcome qualifier present on one side but not
        # EXACTLY on the other changes what's being asked ("hottest" vs
        # "second-hottest" year; "on the ballot" vs "win"). Use EXACT set
        # difference (not fuzzy _token_matches) so "secondhottest" is not
        # smoothed into "hottest".
        q_mismatch = (ke ^ pe) & _PREDICATE_QUALIFIERS
        if q_mismatch:
            return MatchVerdict(False, UNKNOWN, 0.0,
                                (f"predicate_qualifier_mismatch {sorted(q_mismatch)}",), self.name)

        return MatchVerdict(True, UNKNOWN, 0.7,
                            (f"entity_overlap={overlap:.2f}",), self.name)


class ResolutionCriteriaVerifier:
    """Reject pairs whose resolution criteria diverge.

    Checks: (a) close/resolution-time proximity, (b) numeric-threshold
    compatibility, (c) rules-text divergence using the same boilerplate-aware
    machinery as the matcher.
    """

    name = "resolution_criteria"

    def __init__(self, max_close_skew_hours: Optional[float] = None):
        self.max_close_skew_hours = (
            max_close_skew_hours
            if max_close_skew_hours is not None
            else Config.MATCH_MAX_CLOSE_TIME_SKEW_HOURS
        )

    def verify(self, kalshi_market: Dict, polymarket_market: Dict) -> MatchVerdict:
        reasons: List[str] = []

        # (a) Close-time check — ONLY reject when the resolution YEARS differ.
        # Live data shows the two venues legitimately list very different close
        # times for the SAME event (one uses election day, the other a far-out
        # "resolve by" / certification date) — skews of weeks to months are
        # common on identical-title pairs, so an hour-based skew limit destroys
        # recall. A different YEAR is the reliable "different event" signal (and
        # same-year title-numeric divergence is already caught elsewhere). Only
        # applied when the skew is large enough that the year boundary is
        # meaningful (> ~120 days), to avoid Dec/Jan edge flips.
        k_close = _parse_time(kalshi_market.get("close_time"))
        p_close = _parse_time(polymarket_market.get("close_time"))
        if k_close and p_close:
            skew_h = abs((k_close - p_close).total_seconds()) / 3600.0
            if k_close.year != p_close.year and skew_h > 120 * 24:
                reasons.append(f"close_year_mismatch {k_close.year}!={p_close.year} ({skew_h:.0f}h)")
                return MatchVerdict(False, UNKNOWN, 0.0, tuple(reasons), self.name)
            reasons.append(f"close_time_skew={skew_h:.1f}h_ok")

        # (b) Numeric thresholds (e.g. "above 4.5%" vs "4.25-4.50%").
        k_title = kalshi_market.get("title", "")
        p_title = polymarket_market.get("title", "")
        if not thresholds_compatible(k_title, p_title):
            k_thr = extract_prop_threshold(k_title)
            p_thr = extract_prop_threshold(p_title)
            reasons.append(f"threshold_mismatch k={k_thr} p={p_thr}")
            return MatchVerdict(False, UNKNOWN, 0.0, tuple(reasons), self.name)

        # (c) Rules-text divergence (only when both sides expose rules). Also
        # apply a scope veto on the rules text — a state/country named in one
        # venue's rules but absent from the other's is a different event.
        k_rules = _rules_text(kalshi_market)
        p_rules = _rules_text(polymarket_market)
        if k_rules and p_rules:
            k_rscope = extract_scope_entities(k_rules)
            p_rscope = extract_scope_entities(p_rules)
            if k_rscope and p_rscope and not (k_rscope & p_rscope):
                reasons.append(f"rules_scope_mismatch k={sorted(k_rscope)} p={sorted(p_rscope)}")
                return MatchVerdict(False, UNKNOWN, 0.0, tuple(reasons), self.name)
            d1 = set(k_rules.split()) - _BOILERPLATE
            d2 = set(p_rules.split()) - _BOILERPLATE
            if d1 and d2:
                smaller, larger = (d1, d2) if len(d1) <= len(d2) else (d2, d1)
                shared = sum(
                    1 for w in smaller
                    if w in larger or (len(w) >= 4 and any(lw.startswith(w[:4]) for lw in larger))
                )
                containment = shared / len(smaller)
                min_cont = getattr(Config, "MATCH_MIN_RULES_CONTAINMENT", 0.4)
                if containment < min_cont:
                    reasons.append(f"rules_divergent containment={containment:.2f}")
                    return MatchVerdict(False, UNKNOWN, 0.0, tuple(reasons), self.name)
                reasons.append(f"rules_ok containment={containment:.2f}")

        return MatchVerdict(True, UNKNOWN, 0.6, tuple(reasons or ("criteria_unverified",)), self.name)


class AllowlistVerifier:
    """Operator allow/deny override.

    File format (``Config.MATCH_ALLOWLIST_FILE`` under ``Config.DATA_DIR``)::

        {
          "approved": [{"kalshi": "<ticker>", "polymarket": "<id>",
                        "polarity": "aligned"}],
          "denied":   [{"kalshi": "<ticker>", "polymarket": "<id>"}]
        }

    Behaviour: denied → fail; approved → pass with the recorded polarity and
    confidence 1.0; neither → pass but flag ``not_allowlisted`` so the live gate
    (``CompositeVerifier(require_allowlist=True)``) can reject it.
    """

    name = "allowlist"

    def __init__(self, path: Optional[str] = None):
        if path is None:
            path = os.path.join(Config.DATA_DIR, Config.MATCH_ALLOWLIST_FILE)
        self.path = path
        self._approved: Dict = {}
        self._denied: set = set()
        self._mtime: Optional[float] = None
        self._load()

    @staticmethod
    def _key(kalshi_id, polymarket_id) -> tuple:
        return (str(kalshi_id), str(polymarket_id))

    def _load(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self._approved, self._denied, self._mtime = {}, set(), None
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load match allowlist %s: %s", self.path, exc)
            return
        self._approved = {
            self._key(e.get("kalshi"), e.get("polymarket")): e
            for e in data.get("approved", [])
        }
        self._denied = {
            self._key(e.get("kalshi"), e.get("polymarket"))
            for e in data.get("denied", [])
        }
        self._mtime = mtime

    def verify(self, kalshi_market: Dict, polymarket_market: Dict) -> MatchVerdict:
        self._load()
        key = self._key(kalshi_market.get("id"), polymarket_market.get("id"))
        if key in self._denied:
            return MatchVerdict(False, UNKNOWN, 0.0, ("denied_by_operator",), self.name)
        entry = self._approved.get(key)
        if entry:
            polarity = entry.get("polarity", ALIGNED)
            return MatchVerdict(True, polarity, 1.0, ("approved_by_operator",), self.name)
        return MatchVerdict(True, UNKNOWN, 0.0, ("not_allowlisted",), self.name)

    def is_allowlisted(self, kalshi_id, polymarket_id) -> bool:
        self._load()
        return self._key(kalshi_id, polymarket_id) in self._approved


class CompositeVerifier:
    """Run a chain of verifiers; pass only if none fails. Resolve polarity.

    Polarity resolution precedence: an explicit allowlist polarity wins; then
    the highest-confidence non-UNKNOWN polarity; ties → UNKNOWN.
    """

    name = "composite"

    def __init__(
        self,
        verifiers: Optional[Sequence[MatchVerifier]] = None,
        reject_unknown_polarity: Optional[bool] = None,
        require_allowlist: bool = False,
    ):
        self.verifiers = list(verifiers) if verifiers is not None else default_verifiers()
        self.reject_unknown_polarity = (
            reject_unknown_polarity
            if reject_unknown_polarity is not None
            else Config.MATCH_REJECT_UNKNOWN_POLARITY
        )
        self.require_allowlist = require_allowlist

    def verify(self, kalshi_market: Dict, polymarket_market: Dict) -> MatchVerdict:
        reasons: List[str] = []
        polarity = UNKNOWN
        polarity_conf = -1.0
        allowlisted = False
        confidences: List[float] = []

        for verifier in self.verifiers:
            verdict = verifier.verify(kalshi_market, polymarket_market)
            vname = getattr(verifier, "name", verifier.__class__.__name__)
            reasons.extend(f"{vname}:{r}" for r in verdict.reasons)
            if not verdict.passed:
                return MatchVerdict(False, verdict.polarity, 0.0, tuple(reasons), self.name)
            confidences.append(verdict.confidence)

            if vname == AllowlistVerifier.name and "approved_by_operator" in verdict.reasons:
                allowlisted = True
                polarity, polarity_conf = verdict.polarity, 1.0
            elif verdict.polarity != UNKNOWN and verdict.confidence > polarity_conf and not allowlisted:
                polarity, polarity_conf = verdict.polarity, verdict.confidence

        if self.require_allowlist and not allowlisted:
            return MatchVerdict(False, polarity, 0.0, tuple(reasons + ["require_allowlist"]), self.name)

        if polarity == UNKNOWN and self.reject_unknown_polarity:
            return MatchVerdict(False, UNKNOWN, 0.0, tuple(reasons + ["unknown_polarity_rejected"]), self.name)

        confidence = min(confidences) if confidences else 0.0
        return MatchVerdict(True, polarity, confidence, tuple(reasons), self.name)


def default_verifiers() -> List[MatchVerifier]:
    """The standard detection-time verifier chain."""
    return [
        AllowlistVerifier(),
        OutcomePolarityVerifier(),
        DistinguishingEntityVerifier(),
        ResolutionCriteriaVerifier(),
    ]
