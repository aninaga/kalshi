"""Tests for guaranteed PnL reporting output."""

import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


def test_pnl_summary_splits_estimated_vs_guaranteed():
    analyzer = MarketAnalyzer()
    original_interval = Config.SCAN_INTERVAL_SECONDS
    Config.SCAN_INTERVAL_SECONDS = 30

    opportunities = [
        {"total_profit": 5.0},
        {"total_profit": 7.0, "simulated_execution": {"net_profit": 4.0, "filled_volume": 25.0}},
        {"total_profit": -2.0},
        {"total_profit": 3.0, "simulated_execution": {"skipped_reason": "stale_orderbook"}},
    ]

    try:
        summary = analyzer._build_guaranteed_pnl_summary(opportunities)
    finally:
        Config.SCAN_INTERVAL_SECONDS = original_interval

    assert summary["estimated_pnl_per_scan_usd"] == pytest.approx(15.0)
    assert summary["estimated_pnl_per_hour_usd"] == pytest.approx(1800.0)
    assert summary["estimated_pnl_per_day_usd"] == pytest.approx(43200.0)
    assert summary["estimated_pnl_opportunity_count"] == 3

    assert summary["guaranteed_pnl_per_scan_usd"] == pytest.approx(4.0)
    assert summary["guaranteed_pnl_per_hour_usd"] == pytest.approx(480.0)
    assert summary["guaranteed_pnl_per_day_usd"] == pytest.approx(11520.0)
    assert summary["guaranteed_pnl_opportunity_count"] == 1
    assert summary["active_bot_scan_interval_seconds"] == pytest.approx(30.0)
    assert summary["confirmed_realized_pnl_per_scan_usd"] == pytest.approx(0.0)
    assert summary["confirmed_realized_pnl_opportunity_count"] == 0


def test_guaranteed_pnl_is_never_negative():
    analyzer = MarketAnalyzer()
    summary = analyzer._build_guaranteed_pnl_summary(
        [
            {"total_profit": -10.0},
            {"total_profit": "not-a-number"},
            {"simulated_execution": {"net_profit": -1.0}},
        ]
    )

    assert summary["estimated_pnl_per_scan_usd"] == pytest.approx(0.0)
    assert summary["estimated_pnl_opportunity_count"] == 0
    assert summary["guaranteed_pnl_per_scan_usd"] == pytest.approx(0.0)
    assert summary["guaranteed_pnl_opportunity_count"] == 0
    assert summary["confirmed_realized_pnl_per_scan_usd"] == pytest.approx(0.0)
    assert summary["confirmed_realized_pnl_opportunity_count"] == 0
