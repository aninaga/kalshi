"""Shared live-probe economics: fee-aware complementary-arb pricing.

These pin the pure (no-network) economics that the discovery scan, the capture
monitor, and the backtest all share, so the three tools can never drift on what
counts as a real, fee-clearing edge.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.live_probe import kalshi_fee_per_contract, walk_complementary


def test_kalshi_fee_curve_peaks_midprice():
    # 0.07*P*(1-P): maximal at 0.5 (1.75c), ~0 at the extremes — the reason
    # durable arb sits at extreme prices, not mid.
    assert abs(kalshi_fee_per_contract(0.5) - 0.0175) < 1e-9
    assert kalshi_fee_per_contract(0.05) < kalshi_fee_per_contract(0.5)
    assert kalshi_fee_per_contract(0.95) < kalshi_fee_per_contract(0.5)


def _lvl(price, size):
    return [(price, size)]


def test_extreme_price_gap_clears_fee():
    # Buy A (PM, no fee) @ 0.06 + B (Kalshi) @ 0.90 = 0.96 < 1. Kalshi leg at 0.90
    # has a tiny fee (0.07*0.9*0.1=0.0063) → net positive.
    res = walk_complementary(_lvl(0.06, 100), _lvl(0.90, 100),
                             a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is not None and res["net"] > 0
    assert res["size"] == 100


def test_midprice_gap_eaten_by_fee():
    # A (PM) @ 0.49 + B (Kalshi) @ 0.50 = 0.99 → 1c gross, but Kalshi fee at 0.50
    # is 1.75c > 1c → net negative → no size.
    res = walk_complementary(_lvl(0.49, 100), _lvl(0.50, 100),
                             a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is None


def test_walk_stops_when_marginal_turns_negative():
    # Level 1 clears (PM 0.05 + Kalshi 0.90), level 2 doesn't (Kalshi 0.97 → sum
    # 1.02). Size capped at the first level.
    A = [(0.05, 50), (0.05, 50)]
    B = [(0.90, 50), (0.97, 50)]
    res = walk_complementary(A, B, a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is not None and res["size"] == 50


def test_no_arb_when_sum_exceeds_one():
    res = walk_complementary(_lvl(0.60, 100), _lvl(0.60, 100),
                             a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is None


def test_price_pair_polarity_failsafe(monkeypatch):
    # Same live books, two polarities: ALIGNED is a genuine arb (legs sum ~0.93),
    # INVERTED buys the same real outcome on both venues (legs sum ~0.17) — the
    # phantom the auditor demonstrated. The complementary-sum floor must reject
    # the phantom while keeping the genuine one. (Guards the silent-polarity bug.)
    from kalshi_arbitrage import live_probe as lp
    # Kalshi: yes_ask 0.89, no_ask 0.13.  PM: yes-token ask 0.97, no-token ask 0.04.
    monkeypatch.setattr(lp, "kalshi_book", lambda tk: ([(0.89, 500)], [(0.13, 500)]))
    monkeypatch.setattr(lp, "pm_asks",
                        lambda tok: [(0.97, 500)] if tok == "y" else [(0.04, 500)])
    pair = {"ktk": "K", "polarity": "aligned",
            "tokens": [{"outcome": "Yes", "token_id": "y"}, {"outcome": "No", "token_id": "n"}]}
    genuine = lp.price_pair(pair, pm_fee_bps=0)
    assert genuine is not None and genuine["net"] > 0      # real arb survives

    phantom = lp.price_pair({**pair, "polarity": "inverted"}, pm_fee_bps=0)
    assert phantom is None                                  # phantom rejected by the floor


def test_gamma_row_maps_to_clob_shape():
    # Gamma stores outcomes/clobTokenIds as JSON STRINGS; the mapper must produce
    # the exact sampling-markets shape match_pairs consumes. This is the catalog
    # top-up that keeps near-resolved extreme-priced markets (shed from
    # /sampling-markets) discoverable — where durable arb lives.
    from kalshi_arbitrage.live_probe import _gamma_to_clob_row
    g = {"conditionId": "0xc1", "question": "Elon Musk trillionaire before 2027?",
         "description": "Resolves YES if ...", "endDate": "2026-12-31",
         "active": True, "closed": False,
         "outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]'}
    r = _gamma_to_clob_row(g)
    assert r == {"condition_id": "0xc1", "question": "Elon Musk trillionaire before 2027?",
                 "description": "Resolves YES if ...", "end_date_iso": "2026-12-31",
                 "active": True, "closed": False,
                 "tokens": [{"token_id": "111", "outcome": "Yes"},
                            {"token_id": "222", "outcome": "No"}]}


def test_gamma_row_rejects_closed_nonbinary_and_garbage():
    from kalshi_arbitrage.live_probe import _gamma_to_clob_row
    base = {"conditionId": "0xc1", "question": "q", "active": True, "closed": False,
            "outcomes": '["Yes", "No"]', "clobTokenIds": '["1", "2"]'}
    assert _gamma_to_clob_row({**base, "closed": True}) is None
    assert _gamma_to_clob_row({**base, "active": False}) is None
    assert _gamma_to_clob_row({**base, "clobTokenIds": '["1"]'}) is None       # non-binary
    assert _gamma_to_clob_row({**base, "outcomes": "not json"}) is None        # garbage
    assert _gamma_to_clob_row(None) is None


def test_fetch_polymarket_tops_up_from_gamma(monkeypatch):
    # A market missing from /sampling-markets but present in Gamma must appear in
    # the catalog exactly once (dedup by condition_id).
    from kalshi_arbitrage import live_probe as lp
    sampling_row = {"condition_id": "0xa", "question": "A?", "active": True,
                    "closed": False, "tokens": [{"token_id": "1", "outcome": "Yes"},
                                                {"token_id": "2", "outcome": "No"}]}
    gamma_dup = {"conditionId": "0xa", "question": "A?", "active": True, "closed": False,
                 "outcomes": '["Yes", "No"]', "clobTokenIds": '["1", "2"]'}
    gamma_new = {"conditionId": "0xmusk", "question": "Musk trillionaire?", "active": True,
                 "closed": False, "outcomes": '["Yes", "No"]',
                 "clobTokenIds": '["31", "32"]', "endDate": "2026-12-31",
                 "description": ""}

    def fake_get(url, **kw):
        if "sampling-markets" in url:
            return {"data": [sampling_row], "next_cursor": "LTE="}
        if "gamma-api" in url:
            return [gamma_dup, gamma_new] if "offset=0" in url else []
        return None

    monkeypatch.setattr(lp, "get", fake_get)
    rows = lp.fetch_polymarket()
    ids = [r["condition_id"] for r in rows]
    assert ids == ["0xa", "0xmusk"]


def test_match_pairs_backfills_kalshi_rules_from_single_market_get(monkeypatch):
    # The Kalshi LIST/events endpoints ship rules_primary='' — without the
    # single-market backfill every rules-text gate verifies against an EMPTY
    # Kalshi side, which is exactly how the live Musk pair (strict 'more than'
    # vs PM's inclusive 'reaches or exceeds') sailed through as clean.
    from kalshi_arbitrage import live_probe as lp
    kalshi_row = {"ticker": "KXMUSKTRILLION-27",
                  "title": "Will Elon Musk be a trillionaire before 2027?",
                  "close_time": "2027-01-01T00:00:00Z",
                  "yes_sub_title": "Before 2027", "no_sub_title": "Before 2027",
                  "rules_primary": "", "rules_secondary": ""}
    pm_row = {"condition_id": "0xmusk",
              "question": "Will Elon Musk be a trillionaire before 2027?",
              "description": "This market will resolve to Yes if Elon Musk's net "
                             "worth, as listed on the Bloomberg Billionaires Index, "
                             "reaches or exceeds $1 trillion at any point by "
                             "December 31, 2026, 11:59 PM ET.",
              "end_date_iso": "2026-12-31",
              "tokens": [{"token_id": "1", "outcome": "Yes"},
                         {"token_id": "2", "outcome": "No"}]}
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        assert "markets/KXMUSKTRILLION-27" in url
        return {"market": {"rules_primary":
                           "If Elon Musk has a net worth more than $1 trillion "
                           "before Jan 1, 2027, then the market resolves to Yes.",
                           "rules_secondary": ""}}

    monkeypatch.setattr(lp, "get", fake_get)
    lp._KRULES_CACHE.clear()
    pairs = lp.match_pairs([pm_row], [kalshi_row])
    assert len(pairs) == 1
    assert pairs[0]["uncertain"] is True          # comparator gate now SEES the rules
    assert len(calls) == 1                        # one cached GET per ticker

    # Second sweep in the same process: cache hit, no extra GET.
    lp.match_pairs([pm_row], [kalshi_row])
    assert len(calls) == 1


def test_kalshi_rules_failed_fetch_not_cached(monkeypatch):
    from kalshi_arbitrage import live_probe as lp
    lp._KRULES_CACHE.clear()
    monkeypatch.setattr(lp, "get", lambda url, **kw: None)
    assert lp._kalshi_rules("T1") == {"rules_primary": "", "rules_secondary": ""}
    assert "T1" not in lp._KRULES_CACHE           # transient failure → retry later


def test_fetch_polymarket_pages_past_gammas_100_row_cap(monkeypatch):
    # Gamma silently caps limit at 100/page. The sweep must page by the ACTUAL
    # returned length — a deep market (page 2) was missed when the loop assumed
    # 500-row pages and stopped at the top-100.
    from kalshi_arbitrage import live_probe as lp

    def gamma_row(i):
        return {"conditionId": f"0x{i}", "question": f"Q{i}?", "active": True,
                "closed": False, "outcomes": '["Yes", "No"]',
                "clobTokenIds": f'["{2*i}", "{2*i+1}"]'}

    def fake_get(url, **kw):
        if "sampling-markets" in url:
            return {"data": [], "next_cursor": "LTE="}
        if "gamma-api" in url:
            off = int(url.split("offset=")[1])
            if off == 0:
                return [gamma_row(i) for i in range(100)]      # full page -> keep going
            if off == 100:
                return [gamma_row(100 + i) for i in range(40)]  # short page -> stop after
            raise AssertionError(f"unexpected offset {off}")
        return None

    monkeypatch.setattr(lp, "get", fake_get)
    rows = lp.fetch_polymarket()
    assert len(rows) == 140
    assert rows[-1]["condition_id"] == "0x139"
