"""LLM tiebreaker: structured-output parsing, graceful no-op, and wiring.

The tiebreaker escalates ONLY the genuinely-ambiguous residue (asymmetric time
anchors) to a single batched Claude call. These tests use a fake Anthropic
client so they never touch the network, and assert:
  * a no-op when no API key / SDK is present (deterministic verdict stands),
  * the request is built correctly (cheapest model, prompt caching, forced tool,
    one batched call for many pairs),
  * tool-use output is parsed into per-pair results in index order,
  * any call/parse failure fails OPEN (keeps the deterministic pass),
  * the composite + batch helper drop false-event pairs and keep true ones,
  * certain pairs are never escalated.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.matching import (
    ALIGNED,
    INVERTED,
    UNKNOWN,
    CompositeVerifier,
    LLMTiebreaker,
    TiebreakResult,
    resolve_uncertain_batch,
)
from kalshi_arbitrage.matching.llm_tiebreaker import _SYSTEM_PROMPT, _TOOL


def _mkt(title, market_id="m", rules="", close_time=None):
    return {
        "id": market_id,
        "title": title,
        "clean_title": "",
        "close_time": close_time,
        "raw_data": {"rules_primary": rules},
    }


# --- A fake Anthropic client that records the request and returns tool_use --- #

class _FakeMessages:
    def __init__(self, judgements, capture, raise_exc=None):
        self._judgements = judgements
        self._capture = capture
        self._raise = raise_exc

    def create(self, **kwargs):
        self._capture.update(kwargs)
        if self._raise is not None:
            raise self._raise
        block = SimpleNamespace(type="tool_use", name=_TOOL["name"],
                               input={"judgements": self._judgements})
        # Include a stray text block to prove the parser skips non-tool blocks.
        text = SimpleNamespace(type="text", text="ignored")
        return SimpleNamespace(content=[text, block])


class _FakeClient:
    def __init__(self, judgements, capture, raise_exc=None):
        self.messages = _FakeMessages(judgements, capture, raise_exc)


def _wired(judgements, capture=None, raise_exc=None):
    """An LLMTiebreaker forced enabled with a fake client injected."""
    capture = capture if capture is not None else {}
    tb = LLMTiebreaker(model="claude-haiku-4-5", api_key="test-key", enabled=True)
    tb._client = _FakeClient(judgements, capture, raise_exc)
    return tb, capture


# --- graceful no-op --------------------------------------------------------- #

def test_disabled_without_key_is_noop(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tb = LLMTiebreaker(enabled=True)  # config on, but no key
    assert tb.enabled is False
    assert tb.judge(_mkt("a"), _mkt("b")) is None
    assert tb.judge_batch([(_mkt("a"), _mkt("b"))]) == [None]


def test_disabled_by_config_is_noop():
    tb = LLMTiebreaker(api_key="test-key", enabled=False)
    assert tb.enabled is False
    assert tb.judge(_mkt("a"), _mkt("b")) is None


def test_empty_batch_returns_empty():
    tb, _ = _wired([])
    assert tb.judge_batch([]) == []


# --- request construction --------------------------------------------------- #

def test_request_uses_cheapest_model_caching_and_forced_tool():
    tb, cap = _wired([{"index": 0, "same_event": True, "polarity": "aligned"}])
    tb.judge(_mkt("X in 2026"), _mkt("X"))
    assert cap["model"] == "claude-haiku-4-5"
    # System prompt is sent as a cacheable block.
    assert isinstance(cap["system"], list)
    assert cap["system"][0]["text"] == _SYSTEM_PROMPT
    assert cap["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The same-event tool is forced (structured output, not free-text JSON).
    assert cap["tool_choice"] == {"type": "tool", "name": "record_judgements"}
    assert cap["tools"][0]["name"] == "record_judgements"


def test_batches_all_pairs_in_one_call():
    judgements = [
        {"index": 0, "same_event": True, "polarity": "aligned"},
        {"index": 1, "same_event": False, "polarity": "unknown"},
        {"index": 2, "same_event": True, "polarity": "inverted"},
    ]
    tb, cap = _wired(judgements)
    pairs = [(_mkt(f"k{i}"), _mkt(f"p{i}")) for i in range(3)]
    results = tb.judge_batch(pairs)
    # One request for all three pairs.
    assert "Judge these 3 pair(s)" in cap["messages"][0]["content"]
    assert [r.same_event for r in results] == [True, False, True]
    assert results[0].polarity == ALIGNED
    assert results[2].polarity == INVERTED
    # A false-event result never carries a non-unknown polarity.
    assert results[1].polarity == UNKNOWN


# --- output parsing & robustness -------------------------------------------- #

def test_parses_results_in_index_order_even_if_shuffled():
    judgements = [
        {"index": 1, "same_event": True, "polarity": "aligned", "confidence": 0.8},
        {"index": 0, "same_event": False, "polarity": "unknown", "confidence": 0.7},
    ]
    tb, _ = _wired(judgements)
    pairs = [(_mkt("a"), _mkt("b")), (_mkt("c"), _mkt("d"))]
    results = tb.judge_batch(pairs)
    assert results[0].same_event is False
    assert results[1].same_event is True


def test_missing_index_yields_none_for_that_pair():
    # Model returns a judgement only for pair 0.
    tb, _ = _wired([{"index": 0, "same_event": True, "polarity": "aligned"}])
    pairs = [(_mkt("a"), _mkt("b")), (_mkt("c"), _mkt("d"))]
    results = tb.judge_batch(pairs)
    assert results[0] is not None and results[0].same_event is True
    assert results[1] is None  # caller keeps deterministic pass


def test_call_failure_fails_open():
    tb, _ = _wired([], raise_exc=RuntimeError("network down"))
    results = tb.judge_batch([(_mkt("a"), _mkt("b"))])
    assert results == [None]  # deterministic verdict preserved


def test_bad_polarity_is_coerced_to_unknown():
    tb, _ = _wired([{"index": 0, "same_event": True, "polarity": "sideways"}])
    res = tb.judge(_mkt("a"), _mkt("b"))
    assert res.polarity == UNKNOWN


# --- composite wiring ------------------------------------------------------- #

# The two real residual false positives from the labeled corpus.
ALIENS = (_mkt("Will the U.S. confirm that aliens exist in 2028?", "K1"),
          _mkt("Will the US confirm that aliens exist by September 30?", "P1"))
MCGREGOR = (_mkt("Will Conor McGregor fight next?", "K2"),
            _mkt("UFC: Will Conor McGregor Fight in 2026?", "P2"))


def test_deterministic_flags_residue_uncertain():
    # Without a tiebreaker the deterministic chain PASSES these (favouring
    # recall) but marks them uncertain so the escalation layer can act.
    c = CompositeVerifier()
    for k, p in (ALIENS, MCGREGOR):
        verdict = c.verify(k, p)
        assert verdict.passed is True
        assert verdict.uncertain is True


def test_certain_true_pair_is_not_uncertain():
    c = CompositeVerifier()
    verdict = c.verify(_mkt("Will it rain in NYC on 2026-06-01"),
                       _mkt("Will it rain in NYC on 2026-06-01"))
    assert verdict.passed is True
    assert verdict.uncertain is False


def test_composite_tiebreaker_drops_false_event():
    tb, _ = _wired([{"index": 0, "same_event": False, "polarity": "unknown",
                     "reason": "2028 vs by September 30"}])
    c = CompositeVerifier(tiebreaker=tb)
    verdict = c.verify(*ALIENS)
    assert verdict.passed is False
    assert any("llm_tiebreaker" in r for r in verdict.reasons)


def test_composite_tiebreaker_keeps_true_event_and_clears_uncertain():
    tb, _ = _wired([{"index": 0, "same_event": True, "polarity": "aligned",
                     "reason": "same cycle"}])
    c = CompositeVerifier(tiebreaker=tb)
    verdict = c.verify(*MCGREGOR)
    assert verdict.passed is True
    assert verdict.uncertain is False


def test_certain_pair_never_calls_tiebreaker():
    capture = {}
    tb, cap = _wired([{"index": 0, "same_event": False, "polarity": "unknown"}], capture)
    c = CompositeVerifier(tiebreaker=tb)
    c.verify(_mkt("Will it rain in NYC on 2026-06-01"),
             _mkt("Will it rain in NYC on 2026-06-01"))
    assert cap == {}  # no API call made for a certain pair


# --- batch helper (production path) ----------------------------------------- #

def _match(k, p, uncertain):
    return {
        "kalshi_market": k,
        "polymarket_market": p,
        "polarity": UNKNOWN,
        "verification": {"passed": True, "polarity": UNKNOWN, "uncertain": uncertain},
    }


def test_resolve_uncertain_batch_drops_false_keeps_true_and_certain():
    matches = [
        _match(*ALIENS, uncertain=True),      # judged false → dropped
        _match(*MCGREGOR, uncertain=True),    # judged true → kept
        _match(_mkt("certain k"), _mkt("certain p"), uncertain=False),  # never escalated
    ]
    # Two uncertain pairs, in order: ALIENS (idx0), MCGREGOR (idx1).
    tb, cap = _wired([
        {"index": 0, "same_event": False, "polarity": "unknown"},
        {"index": 1, "same_event": True, "polarity": "inverted", "reason": "same cycle"},
    ])
    kept = resolve_uncertain_batch(matches, tb)
    titles = [m["kalshi_market"]["title"] for m in kept]
    assert "Will the U.S. confirm that aliens exist in 2028?" not in titles
    assert "Will Conor McGregor fight next?" in titles
    assert "certain k" in titles
    # The kept McGregor pair has its uncertain flag cleared and polarity applied.
    mcg = next(m for m in kept if m["kalshi_market"]["title"] == "Will Conor McGregor fight next?")
    assert mcg["verification"]["uncertain"] is False
    assert mcg["polarity"] == INVERTED
    # Exactly one batched call for both uncertain pairs.
    assert "Judge these 2 pair(s)" in cap["messages"][0]["content"]


def test_resolve_uncertain_batch_noop_when_disabled():
    matches = [_match(*ALIENS, uncertain=True)]
    tb = LLMTiebreaker(enabled=False)
    kept = resolve_uncertain_batch(matches, tb)
    assert len(kept) == 1  # unchanged — deterministic pass stands


def test_resolve_uncertain_batch_no_uncertain_skips_call():
    matches = [_match(_mkt("a"), _mkt("b"), uncertain=False)]
    tb, cap = _wired([{"index": 0, "same_event": False, "polarity": "unknown"}])
    kept = resolve_uncertain_batch(matches, tb)
    assert len(kept) == 1
    assert cap == {}  # nothing uncertain → no API call
