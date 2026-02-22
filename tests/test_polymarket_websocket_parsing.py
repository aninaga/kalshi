"""Tests for robust Polymarket websocket payload parsing."""

import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.websocket_client import PolymarketWebSocketClient


@pytest.mark.asyncio
async def test_parse_book_snapshot_list_returns_orderbook_messages():
    client = PolymarketWebSocketClient(config={"endpoint": "wss://example.invalid"})
    payload = [
        {
            "market": "0xmarket1",
            "asset_id": "asset-1",
            "event_type": "book",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
            "timestamp": "1771307701198",
        },
        {
            "market": "0xmarket2",
            "asset_id": "asset-2",
            "event_type": "book",
            "bids": [{"price": "0.25", "size": "50"}],
            "asks": [{"price": "0.75", "size": "50"}],
            "timestamp": "1771307701198",
        },
    ]

    parsed = await client._parse_message(payload)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert all(msg.channel == "orderbook" for msg in parsed)
    assert parsed[0].data["asset_id"] == "asset-1"
    assert parsed[0].data["bids"]
    assert parsed[0].data["asks"]


@pytest.mark.asyncio
async def test_parse_price_change_array_returns_price_messages():
    client = PolymarketWebSocketClient(config={"endpoint": "wss://example.invalid"})
    payload = {
        "market": "0xmarket1",
        "event_type": "price_change",
        "timestamp": "1771307701198",
        "price_changes": [
            {"asset_id": "asset-1", "price": "0.61", "size": "12", "side": "BUY"},
            {"asset_id": "asset-2", "price": "0.39", "size": "8", "side": "SELL"},
        ],
    }

    parsed = await client._parse_message(payload)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert all(msg.channel == "price" for msg in parsed)
    assert parsed[0].data["price"] == pytest.approx(0.61)
    assert parsed[1].data["price"] == pytest.approx(0.39)


@pytest.mark.asyncio
async def test_handle_message_ignores_empty_and_counts_invalid_json():
    client = PolymarketWebSocketClient(config={"endpoint": "wss://example.invalid"})

    await client._handle_message("")
    await client._handle_message("   ")
    await client._handle_message("not-json")

    stats = client.get_stats()
    assert stats["messages_ignored_empty"] == 2
    assert stats["messages_invalid_json"] == 1


@pytest.mark.asyncio
async def test_handle_message_classifies_server_error_text():
    client = PolymarketWebSocketClient(config={"endpoint": "wss://example.invalid"})
    await client._handle_message("INVALID OPERATION")
    stats = client.get_stats()
    assert stats["messages_server_errors"] == 1
    assert stats["messages_invalid_json"] == 0
