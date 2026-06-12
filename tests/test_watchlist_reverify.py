"""Cache-retained watch-list pairs must be re-verified by the CURRENT verifier.

Observed live 2026-06-09: the WHO-pinned pandemic pair stayed labeled "clean"
through a monitor restart — after the resolution-authority gate had shipped —
because the pair was retained from the watch-list cache with its verdict baked
at (pre-gate) discovery time. ``lp.reverify_pair`` re-runs the verifier over
retained pairs on every cache load so verifier upgrades reach the whole
watch-list, not just freshly-discovered pairs.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp
from kalshi_arbitrage.matching import MatchVerdict
from tools.monitor_arb import _merge_watchlists

K_RULES_PANDEMIC = ("If any disease becomes a pandemic in 2026, then the "
                    "market resolves to Yes.")
P_DESC_WHO = ("This market will resolve to 'Yes' if the World Health "
              "Organization (WHO) declares any disease a pandemic between "
              "January 1, 2026 and December 31, 2026, 11:59 PM ET. The "
              "resolution source will be official announcements from the "
              "World Health Organization.")

PANDEMIC_PAIR = {"ktk": "KXNEWOUTBREAK-P-26", "pid": "0xfaa", "tokens": [],
                 "kt": "Pandemic in 2026?", "pt": "New pandemic in 2026?",
                 "polarity": "aligned", "uncertain": False}  # stale pre-gate label


@pytest.fixture(autouse=True)
def _clear_caches():
    lp._KRULES_CACHE.clear()
    lp._PDESC_CACHE.clear()
    yield
    lp._KRULES_CACHE.clear()
    lp._PDESC_CACHE.clear()


def _fake_get(url, timeout=25):
    if url.startswith(lp.KBASE):
        return {"market": {"rules_primary": K_RULES_PANDEMIC,
                           "rules_secondary": ""}}
    if url.startswith(lp.GAMMA):
        return [{"description": P_DESC_WHO}]
    raise AssertionError(f"unexpected fetch: {url}")


def test_stale_clean_label_flips_under_current_verifier(monkeypatch):
    # The live incident, end to end: pre-gate "clean" pandemic pair retained
    # from cache → reverify fetches the docs → authority gate flags it.
    monkeypatch.setattr(lp, "get", _fake_get)
    out = lp.reverify_pair(dict(PANDEMIC_PAIR))
    assert out is not None
    assert out["uncertain"] is True
    # The fetched description is persisted so later loads re-verify offline
    # even after PM's catalogs shed the market.
    assert out["pdesc"] == P_DESC_WHO


def test_reverify_uses_stored_pdesc_without_gamma(monkeypatch):
    def kalshi_only(url, timeout=25):
        assert url.startswith(lp.KBASE), f"unexpected fetch: {url}"
        return {"market": {"rules_primary": K_RULES_PANDEMIC,
                           "rules_secondary": ""}}
    monkeypatch.setattr(lp, "get", kalshi_only)
    out = lp.reverify_pair({**PANDEMIC_PAIR, "pdesc": P_DESC_WHO})
    assert out is not None and out["uncertain"] is True


def test_reverify_fails_open_when_docs_unavailable(monkeypatch):
    # A transient network blip must not relabel or drop the cache.
    monkeypatch.setattr(lp, "get", lambda url, timeout=25: None)
    no_pdesc = dict(PANDEMIC_PAIR)
    assert lp.reverify_pair(no_pdesc) is no_pdesc          # Gamma down
    with_pdesc = {**PANDEMIC_PAIR, "pdesc": P_DESC_WHO}
    assert lp.reverify_pair(with_pdesc) is with_pdesc      # Kalshi rules down


class _StubVerifier:
    def __init__(self, verdict):
        self._verdict = verdict

    def verify(self, kd, pd):
        return self._verdict


def test_reverify_drops_pair_that_no_longer_passes(monkeypatch):
    monkeypatch.setattr(lp, "get", _fake_get)
    verdict = MatchVerdict(False, None, 0.0, ("rules_conflict",), "stub")
    assert lp.reverify_pair(dict(PANDEMIC_PAIR), verifier=_StubVerifier(verdict)) is None


def test_merge_watchlists_revalidates_retained_only():
    fresh = [{"ktk": "K1", "pid": "P1"}]
    cached = [
        {"ktk": "K1", "pid": "P1", "uncertain": False},  # also fresh → not revalidated
        {"ktk": "K2", "pid": "P2", "uncertain": False},  # retained → relabeled
        {"ktk": "K3", "pid": "P3", "uncertain": False},  # retained → no longer passes
    ]
    seen = []

    def reval(p):
        seen.append(p["ktk"])
        return None if p["ktk"] == "K3" else {**p, "uncertain": True}

    merged = _merge_watchlists(fresh, cached, revalidate=reval)
    assert seen == ["K2", "K3"]                      # fresh pair skipped revalidation
    assert [p["ktk"] for p in merged] == ["K1", "K2"]
    assert merged[0] is fresh[0]                     # fresh verdict untouched
    assert merged[1]["uncertain"] is True            # stale label refreshed


def test_match_pairs_persists_pm_description(monkeypatch):
    # pdesc must ride along in the pair dict — it is the one verification doc
    # that becomes unrecoverable once PM's catalogs shed the market.
    monkeypatch.setattr(lp, "get", _fake_get)
    pm = [{"condition_id": "0xfaa", "question": "New pandemic in 2026?",
           "description": P_DESC_WHO, "end_date_iso": None,
           "tokens": [{"token_id": "1", "outcome": "Yes"},
                      {"token_id": "2", "outcome": "No"}]}]
    ks = [{"ticker": "KXNEWOUTBREAK-P-26", "title": "Pandemic in 2026?",
           "close_time": None, "yes_sub_title": "", "no_sub_title": "",
           "rules_primary": "", "rules_secondary": ""}]
    pairs = lp.match_pairs(pm, ks)
    assert len(pairs) == 1
    assert pairs[0]["pdesc"] == P_DESC_WHO
    assert pairs[0]["uncertain"] is True             # authority gate, at discovery
