"""Tests for confirmed realized PnL reporting in MarketAnalyzer summaries."""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


def test_pnl_summary_includes_confirmed_fields_default_zero():
    analyzer = MarketAnalyzer()
    summary = analyzer._build_guaranteed_pnl_summary([], scan_started_at=datetime.now())

    assert summary["confirmed_realized_pnl_per_scan_usd"] == pytest.approx(0.0)
    assert summary["confirmed_realized_pnl_per_hour_usd"] == pytest.approx(0.0)
    assert summary["confirmed_realized_pnl_per_day_usd"] == pytest.approx(0.0)
    assert summary["confirmed_realized_pnl_opportunity_count"] == 0
    assert summary["confirmed_settled_execution_count"] == 0


def test_pnl_summary_uses_exchange_settled_receipts():
    analyzer = MarketAnalyzer()
    original_interval = Config.SCAN_INTERVAL_SECONDS
    Config.SCAN_INTERVAL_SECONDS = 30

    scan_started_at = datetime.now() - timedelta(seconds=1)
    analyzer.confirmed_pnl_tracker.record_execution_receipt(
        opportunity_id="opp-r1",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=1.0,
        sell_price=0.55,
        sell_size=100,
        sell_fee=1.0,
        source="exchange",
        mark_confirmed=True,
        mark_settled=True,
        timestamp=datetime.now().timestamp(),
    )

    try:
        summary = analyzer._build_guaranteed_pnl_summary([], scan_started_at=scan_started_at)
    finally:
        Config.SCAN_INTERVAL_SECONDS = original_interval

    # Gross=5.0, fees=2.0 => realized=3.0
    assert summary["confirmed_realized_pnl_per_scan_usd"] == pytest.approx(3.0)
    assert summary["confirmed_realized_pnl_per_hour_usd"] == pytest.approx(360.0)
    assert summary["confirmed_realized_pnl_per_day_usd"] == pytest.approx(8640.0)
    assert summary["confirmed_realized_pnl_opportunity_count"] == 1
    assert summary["confirmed_settled_execution_count"] == 1


def test_simulation_receipts_do_not_count_unless_enabled():
    analyzer = MarketAnalyzer()

    opportunity = {
        "opportunity_id": "opp-sim-1",
        "strategy": "test",
        "buy_platform": "kalshi",
        "sell_platform": "polymarket",
        "polymarket_token": "token-1",
        "match_data": {
            "kalshi_market": {"id": "K-1"},
            "polymarket_market": {"id": "P-1"},
        },
    }
    simulated_execution = {
        "filled_volume": 100.0,
        "avg_buy_price": 0.50,
        "avg_sell_price": 0.55,
        "buy_fees": 0.0,
        "sell_fees": 0.0,
        "timestamp": datetime.now().timestamp(),
        "execution_id": "sim-exec-1",
        "buy_order_id": "sim-buy-order-1",
        "sell_order_id": "sim-sell-order-1",
        "buy_fill_id": "sim-buy-fill-1",
        "sell_fill_id": "sim-sell-fill-1",
        "buy_trade_id": "sim-buy-trade-1",
        "sell_trade_id": "sim-sell-trade-1",
        "settlement_id": "sim-settle-1",
        "confirmation_source": "simulation",
    }

    execution_id = analyzer._record_execution_receipt(opportunity, simulated_execution)
    assert execution_id == "sim-exec-1"

    summary_default = analyzer._build_guaranteed_pnl_summary([], scan_started_at=datetime.now() - timedelta(seconds=1))
    assert summary_default["confirmed_realized_pnl_per_scan_usd"] == pytest.approx(0.0)

    analyzer.confirmed_pnl_tracker.allow_simulated_confirmed = True
    summary_enabled = analyzer._build_guaranteed_pnl_summary([], scan_started_at=datetime.now() - timedelta(seconds=1))
    assert summary_enabled["confirmed_realized_pnl_per_scan_usd"] == pytest.approx(5.0)


def test_public_exchange_receipt_hook_updates_confirmed_summary():
    analyzer = MarketAnalyzer()

    analyzer.record_exchange_execution_receipt(
        opportunity_id="opp-hook-1",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=0.0,
        sell_price=0.53,
        sell_size=100,
        sell_fee=0.0,
        execution_id="exec-hook-1",
        buy_order_id="buy-order-hook-1",
        sell_order_id="sell-order-hook-1",
        buy_fill_id="buy-fill-hook-1",
        sell_fill_id="sell-fill-hook-1",
        buy_trade_id="buy-trade-hook-1",
        sell_trade_id="sell-trade-hook-1",
        settlement_id="settle-hook-1",
        token_id="token-hook-1",
        mark_confirmed=True,
        mark_settled=True,
    )

    summary = analyzer._build_guaranteed_pnl_summary([], scan_started_at=datetime.now() - timedelta(seconds=1))
    assert summary["confirmed_realized_pnl_per_scan_usd"] == pytest.approx(3.0)
    assert summary["confirmed_settled_execution_count"] == 1
