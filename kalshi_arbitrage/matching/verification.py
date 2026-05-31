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
    text = market.get("clean_title") or clean_title(market.get("title", ""))
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
        k_title = kalshi_market.get("clean_title") or clean_title(kalshi_market.get("title", ""))
        p_title = polymarket_market.get("clean_title") or clean_title(polymarket_market.get("title", ""))

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

        # (a) Close-time proximity — a year/date defines the event.
        k_close = _parse_time(kalshi_market.get("close_time"))
        p_close = _parse_time(polymarket_market.get("close_time"))
        if k_close and p_close:
            skew_h = abs((k_close - p_close).total_seconds()) / 3600.0
            if skew_h > self.max_close_skew_hours:
                reasons.append(f"close_time_skew={skew_h:.1f}h")
                return MatchVerdict(False, UNKNOWN, 0.0, tuple(reasons), self.name)
            reasons.append(f"close_time_ok={skew_h:.1f}h")

        # (b) Numeric thresholds (e.g. "above 4.5%" vs "4.25-4.50%").
        k_title = kalshi_market.get("title", "")
        p_title = polymarket_market.get("title", "")
        if not thresholds_compatible(k_title, p_title):
            k_thr = extract_prop_threshold(k_title)
            p_thr = extract_prop_threshold(p_title)
            reasons.append(f"threshold_mismatch k={k_thr} p={p_thr}")
            return MatchVerdict(False, UNKNOWN, 0.0, tuple(reasons), self.name)

        # (c) Rules-text divergence (only when both sides expose rules).
        k_rules = _rules_text(kalshi_market)
        p_rules = _rules_text(polymarket_market)
        if k_rules and p_rules:
            d1 = set(k_rules.split()) - _BOILERPLATE
            d2 = set(p_rules.split()) - _BOILERPLATE
            if d1 and d2:
                smaller, larger = (d1, d2) if len(d1) <= len(d2) else (d2, d1)
                shared = sum(
                    1 for w in smaller
                    if w in larger or (len(w) >= 4 and any(lw.startswith(w[:4]) for lw in larger))
                )
                containment = shared / len(smaller)
                if containment < 0.3:
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
        ResolutionCriteriaVerifier(),
    ]
