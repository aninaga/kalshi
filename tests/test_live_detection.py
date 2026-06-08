"""Live-detection capability test.

Proves the detection pipeline actually surfaces a cross-venue arbitrage when one
exists in the order books — driven through the REAL detection method
(`MarketAnalyzer._find_best_arbitrage`), not a reimplementation. We seed the
venue clients' orderbook/price caches with a hand-built arbitrage (Kalshi YES
cheap, Polymarket YES rich) exactly as the live feeds would populate them, then
assert the analyzer finds it, prices it net of fees, and gets the polarity right.

This is the guarantee behind "live-detect arbitrage": the moment both venues
have liquid books, the system identifies the opportunity. (In some environments
the Kalshi feed serves empty books, so a live scan finds nothing — that is a data
reality, not a detector defect. This test isolates the detector.)
"""

import time

import pytest

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


def _seed_books(analyzer, k_ticker, pm_id, yes_token, no_token,
                k_yes_ask, pm_yes_bid):
    """Populate the venue caches with a real arbitrage:
    buy Kalshi YES @ k_yes_ask, sell Polymarket YES @ pm_yes_bid (pm_yes_bid > k_yes_ask).
    """
    now = time.time()
    kc = analyzer.kalshi_client
    pc = analyzer.polymarket_client

    # Kalshi book: a YES ask (someone selling YES we can buy) + a token of depth.
    kc.orderbook_cache[k_ticker] = {
        "yes_asks": [{"price": k_yes_ask, "size": 500}],
        "yes_bids": [{"price": round(k_yes_ask - 0.02, 4), "size": 500}],
        "no_asks": [{"price": round(1 - k_yes_ask - 0.02, 4), "size": 500}],
        "no_bids": [{"price": round(1 - k_yes_ask - 0.04, 4), "size": 500}],
        "timestamp": now,
        "is_synthetic": False,
    }

    # Polymarket YES-token book: a bid (someone buying YES we can sell into).
    pc.orderbook_cache[pm_id] = {
        yes_token: {
            "bids": [{"price": pm_yes_bid, "size": 500}],
            "asks": [{"price": round(pm_yes_bid + 0.02, 4), "size": 500}],
            "timestamp": now,
            "is_synthetic": False,
        },
        no_token: {
            "bids": [{"price": round(1 - pm_yes_bid - 0.02, 4), "size": 500}],
            "asks": [{"price": round(1 - pm_yes_bid, 4), "size": 500}],
            "timestamp": now,
            "is_synthetic": False,
        },
    }
    # Price cache carries the outcome labels the detector uses to pick YES/NO.
    pc.price_cache[pm_id] = {
        yes_token: {"outcome": "Yes", "buy_price": pm_yes_bid, "sell_price": pm_yes_bid,
                    "executable": True, "timestamp": now},
        no_token: {"outcome": "No", "buy_price": 1 - pm_yes_bid, "sell_price": 1 - pm_yes_bid,
                   "executable": True, "timestamp": now},
    }
    pc.token_to_market[yes_token] = pm_id
    pc.token_to_market[no_token] = pm_id
    pc.token_outcome_map[yes_token] = "Yes"
    pc.token_outcome_map[no_token] = "No"


@pytest.fixture
def analyzer(monkeypatch):
    # Detection must use the real (seeded) books, never synthetic fallback.
    monkeypatch.setattr(Config, "REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", False, raising=False)
    return MarketAnalyzer()


async def test_detects_clear_cross_venue_arbitrage(analyzer):
    """Buy Kalshi YES @ 0.40, sell Polymarket YES @ 0.55 → must be detected."""
    k_ticker, pm_id = "GOVTEST-26", "0xpmtest"
    yes_token, no_token = "yes-tok", "no-tok"
    _seed_books(analyzer, k_ticker, pm_id, yes_token, no_token,
                k_yes_ask=0.40, pm_yes_bid=0.55)

    kalshi_market = {"id": k_ticker, "title": "Will party X win the 2026 governor race",
                     "clean_title": "will party x win the 2026 governor race",
                     "close_time": "2026-11-03T00:00:00", "raw_data": {}}
    polymarket_market = {"id": pm_id, "title": "Will party X win the 2026 governor race",
                         "clean_title": "will party x win the 2026 governor race",
                         "close_time": "2026-11-03T00:00:00", "raw_data": {}}
    match = {"kalshi_market": kalshi_market, "polymarket_market": polymarket_market,
             "similarity_score": 0.95, "polarity": "aligned"}

    pm_prices = await analyzer._get_cached_market_prices(pm_id)
    opp = await analyzer._find_best_arbitrage(kalshi_market, polymarket_market, pm_prices, match)

    assert opp is not None, "detector failed to surface a clear 15-cent arbitrage"
    assert opp["total_profit"] > 0
    assert opp["max_tradeable_volume"] > 0
    # The winning strategy must be buy-Kalshi-YES / sell-Polymarket-YES.
    assert opp["buy_platform"] == "kalshi" and opp["sell_platform"] == "polymarket"


async def test_no_arbitrage_when_books_priced_efficiently(analyzer):
    """Kalshi YES @ 0.50, Polymarket YES bid @ 0.49 → no profit after fees."""
    k_ticker, pm_id = "EFFTEST-26", "0xpmeff"
    yes_token, no_token = "yes2", "no2"
    _seed_books(analyzer, k_ticker, pm_id, yes_token, no_token,
                k_yes_ask=0.50, pm_yes_bid=0.49)

    km = {"id": k_ticker, "title": "t", "clean_title": "t",
          "close_time": "2026-11-03T00:00:00", "raw_data": {}}
    pm = {"id": pm_id, "title": "t", "clean_title": "t",
          "close_time": "2026-11-03T00:00:00", "raw_data": {}}
    match = {"kalshi_market": km, "polymarket_market": pm,
             "similarity_score": 0.95, "polarity": "aligned"}

    pm_prices = await analyzer._get_cached_market_prices(pm_id)
    opp = await analyzer._find_best_arbitrage(km, pm, pm_prices, match)
    # Either None or non-positive profit — never a phantom positive edge.
    assert opp is None or opp["total_profit"] <= 0


async def test_empty_kalshi_book_yields_no_detection(analyzer):
    """Reproduces the live environment: PM has a book, Kalshi is empty → None."""
    pm_id, yes_token, no_token = "0xpmempty", "yes3", "no3"
    now = time.time()
    analyzer.kalshi_client.orderbook_cache["EMPTY-26"] = {
        "yes_asks": [], "yes_bids": [], "no_asks": [], "no_bids": [],
        "timestamp": now, "is_synthetic": False,
    }
    analyzer.polymarket_client.orderbook_cache[pm_id] = {
        yes_token: {"bids": [{"price": 0.55, "size": 500}],
                    "asks": [{"price": 0.57, "size": 500}], "timestamp": now},
    }
    analyzer.polymarket_client.price_cache[pm_id] = {
        yes_token: {"outcome": "Yes", "buy_price": 0.55, "sell_price": 0.55,
                    "executable": True, "timestamp": now},
    }
    km = {"id": "EMPTY-26", "title": "t", "clean_title": "t", "raw_data": {}}
    pm = {"id": pm_id, "title": "t", "clean_title": "t", "raw_data": {}}
    match = {"kalshi_market": km, "polymarket_market": pm, "polarity": "aligned"}
    pm_prices = await analyzer._get_cached_market_prices(pm_id)
    opp = await analyzer._find_best_arbitrage(km, pm, pm_prices, match)
    assert opp is None
