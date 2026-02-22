"""Tests for orderbook data-quality controls and fee estimation."""

import os
import sys
import time

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


class DummyKalshiNoOrderbook:
    async def get_market_orderbook(self, market_id):
        return None

    async def get_market_prices(self, market_id):
        return {"yes_price": 0.52, "no_price": 0.48}


class DummyPolymarketWithOrderbook:
    async def get_market_orderbook(self, market_id, token_id=None):
        return {
            "bids": [{"price": 0.56, "size": 50}],
            "asks": [{"price": 0.58, "size": 50}],
            "timestamp": time.time(),
        }


@pytest.mark.asyncio
async def test_strict_mode_skips_synthetic_orderbooks():
    analyzer = MarketAnalyzer()
    analyzer.kalshi_client = DummyKalshiNoOrderbook()

    original_strict = Config.REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED
    try:
        Config.REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED = True
        strict_result = await analyzer._get_cached_orderbook("TEST-MARKET", "kalshi")
        assert strict_result is None

        Config.REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED = False
        relaxed_result = await analyzer._get_cached_orderbook("TEST-MARKET", "kalshi")
        assert relaxed_result is not None
        assert relaxed_result.get("is_synthetic") is True
    finally:
        Config.REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED = original_strict


@pytest.mark.asyncio
async def test_real_orderbook_is_marked_non_synthetic():
    analyzer = MarketAnalyzer()
    analyzer.polymarket_client = DummyPolymarketWithOrderbook()

    original_strict = Config.REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED
    try:
        Config.REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED = True
        result = await analyzer._get_cached_orderbook("PM-MARKET", "polymarket", "TOKEN-1")
    finally:
        Config.REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED = original_strict

    assert result is not None
    assert result.get("is_synthetic") is False


def test_fee_estimation_uses_venue_models():
    analyzer = MarketAnalyzer()

    kalshi_fee = analyzer._estimate_trade_fee("kalshi", price=0.60, volume=100, fallback_fee_rate=0.0)
    polymarket_fee = analyzer._estimate_trade_fee("polymarket", price=0.60, volume=100, fallback_fee_rate=0.02)

    assert kalshi_fee > 0.0
    assert polymarket_fee >= 0.0
