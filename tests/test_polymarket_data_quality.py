"""Tests for Polymarket data-quality validation and counters."""

import os
import sys
import time

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.api_clients import PolymarketClient
from kalshi_arbitrage.websocket_client import StreamMessage


@pytest.mark.asyncio
async def test_price_update_rejects_out_of_range_probability():
    client = PolymarketClient()
    client.token_to_market["token-1"] = "market-1"

    msg = StreamMessage(
        platform="polymarket",
        channel="price",
        market_id="",
        data={"asset_id": "token-1", "price": 1.2, "outcome": "Yes"},
        timestamp=time.time(),
    )

    await client._handle_price_update(msg)

    stats = client.get_data_quality_stats()
    assert stats["price_updates_received"] == 1
    assert stats["price_updates_accepted"] == 0
    assert stats["price_updates_dropped"] == 1
    assert stats["invalid_price_updates"] == 1
    assert "market-1" not in client.price_cache


@pytest.mark.asyncio
async def test_orderbook_update_keeps_valid_levels_and_drops_invalid():
    client = PolymarketClient()
    client.token_to_market["token-1"] = "market-1"
    client.token_outcome_map["token-1"] = "Yes"

    msg = StreamMessage(
        platform="polymarket",
        channel="orderbook",
        market_id="",
        data={
            "asset_id": "token-1",
            "bids": [
                {"price": "0.55", "size": "20"},
                {"price": "1.2", "size": "10"},  # invalid price
            ],
            "asks": [
                {"price": "0.65", "size": "15"},
                {"price": "0.70", "size": "0"},  # invalid size
            ],
        },
        timestamp=time.time(),
    )

    await client._handle_orderbook_update(msg)

    stats = client.get_data_quality_stats()
    assert stats["orderbook_updates_received"] == 1
    assert stats["orderbook_updates_accepted"] == 1
    assert stats["invalid_orderbook_levels"] == 2

    book = client.orderbook_cache["market-1"]["token-1"]
    assert len(book["bids"]) == 1
    assert len(book["asks"]) == 1
    assert book["bids"][0]["price"] == pytest.approx(0.55)
    assert book["asks"][0]["price"] == pytest.approx(0.65)

