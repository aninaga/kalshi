"""Optional LLM tiebreaker for the genuinely-ambiguous matching residue.

The deterministic :class:`DistinguishingEntityVerifier` resolves 99%+ of
cross-venue candidate pairs on pure-Python rules. A small residue is genuinely
undecidable from the titles alone — overwhelmingly the **asymmetric time
anchor** case:

    Kalshi:     "Will the U.S. confirm that aliens exist in 2028?"
    Polymarket: "Will the US confirm that aliens exist by September 30?"

    Kalshi:     "Will Conor McGregor fight next?"
    Polymarket: "UFC: Will Conor McGregor Fight in 2026?"

The deterministic year-veto can only reject when BOTH sides name explicit,
differing years. Here one side states a year (or a dated deadline) the other
omits — and 40+ *true* matches in the labeled corpus share that exact shape
(an election that one venue tags "2026" and the other leaves undated). No title
rule can separate the false pairs from the true ones without sacrificing recall,
so the deterministic path keeps the pass (favouring recall) and flags the pair
``uncertain``. This module escalates only those flagged pairs to a single Claude
call that judges whether the two contracts resolve on the same real-world event.

Design contract:
  * **Deterministic-first / graceful no-op.** A NO-OP unless an API key is
    present AND the ``anthropic`` SDK is importable. With no key the deterministic
    verdict stands unchanged — the bot never depends on a network call to trade.
  * **Cheapest suitable model** (``claude-haiku-4-5`` by default).
  * **Prompt caching** of the shared system prompt (``cache_control: ephemeral``)
    so repeated scans within the cache TTL don't re-pay for the instructions.
  * **Structured output** via a forced tool call — one ``{same_event, polarity}``
    record per input pair, validated by the tool's JSON schema (no brittle
    free-text JSON parsing, no extra Pydantic dependency).
  * **Batching** — all of a scan's uncertain pairs go in ONE request.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import Config

logger = logging.getLogger(__name__)

# Mirror the polarity vocabulary used by the deterministic verifier.
ALIGNED = "aligned"
INVERTED = "inverted"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class TiebreakResult:
    """A single same-event judgement for one (Kalshi, Polymarket) pair."""

    same_event: bool
    polarity: str = UNKNOWN       # aligned | inverted | unknown
    confidence: float = 0.0       # 0..1, the model's stated confidence
    reason: str = ""              # short rationale, for the audit trail


# The shared, cacheable instruction block. Kept stable across calls so the
# ephemeral prompt cache can serve it on repeated scans (it is the only large,
# unchanging part of the request — the per-pair data goes in the user turn).
_SYSTEM_PROMPT = """\
You are a careful adjudicator for a cross-venue prediction-market arbitrage bot.
Two contracts — one from Kalshi, one from Polymarket — are a TRUE match only if
they resolve YES/NO on the EXACT SAME real-world outcome, so that a position in
one can hedge a position in the other. A wrong "true" makes the bot place two
legs that do NOT hedge, leaving an un-hedged directional bet — so when in doubt,
answer NOT the same event.

These pairs have already passed a deterministic check that confirmed the SAME
subject, scope, office, and proposition. The ONLY thing left for you to judge is
whether they cover the SAME TIME PERIOD / resolution window. The hard cases all
have an asymmetric time anchor: one title names a year or a dated deadline that
the other omits or states differently.

Decide same_event with these rules:
  - Different explicit years (2026 vs 2028) → NOT the same event.
  - One side names a SPECIFIC year/deadline and the other names a DIFFERENT,
    incompatible window (e.g. "in 2028" vs "by September 30" of a near year;
    "fight in 2026" vs an open-ended "fight next" with no horizon) → NOT the
    same event: the resolution windows don't coincide.
  - One side states the year and the other leaves it implicit BUT context makes
    them the same recurring event/window (e.g. the same election cycle, the same
    season) → SAME event.
  - If the windows plainly coincide or one clearly contains/equals the other for
    the named outcome → SAME event.

For SAME-event pairs also give polarity:
  - "aligned": Kalshi-YES means the same as Polymarket-YES.
  - "inverted": Kalshi-YES means the same as Polymarket-NO (a negation/threshold
    flip). Use "unknown" only if you truly cannot tell.
For NOT-same-event pairs use polarity "unknown".

Judge ONLY from the titles and any rules text provided. Do not invent facts.
Record exactly one result per input pair, in the same order, via the tool.\
"""

# Forced-tool schema: an array of per-pair judgements. Forcing the tool yields
# structured, schema-validated output without parsing free-text JSON.
_TOOL = {
    "name": "record_judgements",
    "description": "Record one same-event judgement per input pair, in order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "judgements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "0-based index of the pair being judged.",
                        },
                        "same_event": {
                            "type": "boolean",
                            "description": "True iff the two contracts resolve on the same outcome.",
                        },
                        "polarity": {
                            "type": "string",
                            "enum": ["aligned", "inverted", "unknown"],
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0..1 confidence in same_event.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "One short clause explaining the call.",
                        },
                    },
                    "required": ["index", "same_event", "polarity"],
                },
            }
        },
        "required": ["judgements"],
    },
}


def _market_brief(market: Dict) -> Dict:
    """The minimal fields the judge needs from a processed market dict."""
    raw = market.get("raw_data", {}) or {}
    rules = ""
    for key in ("rules_primary", "description", "subtitle"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            rules = val.strip()
            break
    brief = {"title": market.get("title") or market.get("clean_title") or ""}
    if rules:
        brief["rules"] = rules[:400]  # keep the request small
    if market.get("close_time"):
        brief["close_time"] = str(market.get("close_time"))
    return brief


class LLMTiebreaker:
    """Escalate uncertain pairs to a single batched Claude same-event call.

    No-op (every judgement is ``None``) unless ``enabled``: config flag on, an
    API key in the environment, and the ``anthropic`` SDK importable.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self.model = model or Config.MATCH_LLM_TIEBREAKER_MODEL
        key_env = Config.MATCH_LLM_TIEBREAKER_API_KEY_ENV
        self.api_key = api_key if api_key is not None else os.environ.get(key_env, "")
        self._config_enabled = (
            enabled if enabled is not None
            else getattr(Config, "MATCH_LLM_TIEBREAKER_ENABLED", False)
        )
        self._client = None  # lazily constructed, then reused (cache-friendly)
        self._import_failed = False

    # --- availability ------------------------------------------------------- #

    @property
    def enabled(self) -> bool:
        """True only if configured on, keyed, and the SDK is importable."""
        if not self._config_enabled or not self.api_key or self._import_failed:
            return False
        return self._get_client() is not None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._import_failed:
            return None
        try:
            import anthropic  # lazy: absent SDK must not break import or trading
        except Exception:  # pragma: no cover - exercised only without the SDK
            self._import_failed = True
            logger.info("anthropic SDK not installed; LLM tiebreaker disabled.")
            return None
        try:
            self._client = anthropic.Anthropic(api_key=self.api_key)
        except Exception as exc:  # pragma: no cover - defensive
            self._import_failed = True
            logger.warning("Could not initialize Anthropic client: %s", exc)
            return None
        return self._client

    # --- judging ------------------------------------------------------------ #

    def judge(self, kalshi_market: Dict, polymarket_market: Dict) -> Optional[TiebreakResult]:
        """Judge a single pair (batch of one). Returns None when disabled."""
        results = self.judge_batch([(kalshi_market, polymarket_market)])
        return results[0] if results else None

    def judge_batch(
        self, pairs: Sequence[Tuple[Dict, Dict]]
    ) -> List[Optional[TiebreakResult]]:
        """Judge every pair in one request. Returns a same-length list; each
        entry is a :class:`TiebreakResult`, or ``None`` when the tiebreaker is
        disabled or the call/parse failed (caller keeps the deterministic pass).
        """
        if not pairs:
            return []
        if not self.enabled:
            return [None] * len(pairs)
        try:
            judgements = self._call(pairs)
        except Exception as exc:  # network/SDK/parse — fail open to deterministic
            logger.warning("LLM tiebreaker call failed (%s); keeping deterministic verdicts.", exc)
            return [None] * len(pairs)
        return [judgements.get(i) for i in range(len(pairs))]

    def _call(self, pairs: Sequence[Tuple[Dict, Dict]]) -> Dict[int, TiebreakResult]:
        client = self._get_client()
        lines = []
        for i, (k, p) in enumerate(pairs):
            lines.append({
                "index": i,
                "kalshi": _market_brief(k),
                "polymarket": _market_brief(p),
            })
        import json
        user_text = (
            "Judge these "
            f"{len(pairs)} pair(s). Return exactly one record per index.\n\n"
            + json.dumps(lines, ensure_ascii=False, indent=2)
        )

        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Cache the stable instructions so repeated scans within the
                    # cache TTL skip re-encoding them. (Engages once the cached
                    # prefix exceeds the model's minimum; a harmless no-op below.)
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": _TOOL["name"]},
            messages=[{"role": "user", "content": user_text}],
        )

        out: Dict[int, TiebreakResult] = {}
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            data = getattr(block, "input", {}) or {}
            for rec in data.get("judgements", []):
                idx = rec.get("index")
                if not isinstance(idx, int) or not (0 <= idx < len(pairs)):
                    continue
                same = bool(rec.get("same_event"))
                polarity = rec.get("polarity", UNKNOWN)
                if polarity not in (ALIGNED, INVERTED, UNKNOWN):
                    polarity = UNKNOWN
                conf = rec.get("confidence")
                try:
                    conf = float(conf)
                except (TypeError, ValueError):
                    conf = 0.9  # model omitted it; a forced judgement is high-conf
                out[idx] = TiebreakResult(
                    same_event=same,
                    polarity=polarity if same else UNKNOWN,
                    confidence=max(0.0, min(1.0, conf)),
                    reason=str(rec.get("reason", ""))[:200],
                )
        return out


def resolve_uncertain_batch(
    matches: Sequence[Dict],
    tiebreaker: Optional[LLMTiebreaker] = None,
) -> List[Dict]:
    """Production helper: resolve a whole scan's uncertain matches in ONE call.

    ``matches`` are detection-result dicts (each carrying ``kalshi_market`` and
    ``polymarket_market``, and a ``verification`` dict from ``CompositeVerifier``
    with an ``uncertain`` flag). Returns the matches to KEEP: every certain match
    plus those the judge confirms as same-event (with polarity/verification
    updated). When the tiebreaker is disabled the input is returned unchanged
    (deterministic behaviour — uncertain matches keep their pass).
    """
    tb = tiebreaker or LLMTiebreaker()
    uncertain_idx = [
        i for i, m in enumerate(matches)
        if (m.get("verification") or {}).get("uncertain")
    ]
    if not uncertain_idx or not tb.enabled:
        return list(matches)

    pairs = [
        (matches[i]["kalshi_market"], matches[i]["polymarket_market"])
        for i in uncertain_idx
    ]
    results = tb.judge_batch(pairs)

    kept: List[Dict] = []
    drop = set()
    resolved = {uncertain_idx[j]: results[j] for j in range(len(uncertain_idx))}
    for i, match in enumerate(matches):
        res = resolved.get(i)
        if res is None:
            kept.append(match)  # certain, or judge no-op → keep deterministic
            continue
        if not res.same_event:
            drop.add(i)
            continue
        # Confirmed same-event: clear the uncertain flag, record the call.
        match = dict(match)
        verification = dict(match.get("verification") or {})
        verification["uncertain"] = False
        verification.setdefault("reasons", []).append(f"llm_tiebreaker:{res.reason}")
        if verification.get("polarity", UNKNOWN) == UNKNOWN and res.polarity != UNKNOWN:
            verification["polarity"] = res.polarity
            match["polarity"] = res.polarity
        match["verification"] = verification
        kept.append(match)
    logger.info(
        "LLM tiebreaker: %d uncertain pair(s) judged, %d dropped as different-event.",
        len(uncertain_idx), len(drop),
    )
    return kept
