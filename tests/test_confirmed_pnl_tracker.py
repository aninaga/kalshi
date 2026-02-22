"""Comprehensive tests for confirmed execution PnL tracking."""

import os
import sys
import time

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.confirmed_pnl import ConfirmedPnLTracker


def test_settled_confirmed_execution_realized_pnl():
    tracker = ConfirmedPnLTracker()

    tracker.record_execution_receipt(
        opportunity_id="opp-1",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.52,
        buy_size=100,
        buy_fee=1.0,
        sell_price=0.56,
        sell_size=100,
        sell_fee=1.0,
        source="exchange",
        mark_confirmed=True,
        mark_settled=True,
    )

    summary = tracker.summarize(scan_interval_seconds=30)
    assert summary["realized_pnl_usd"] == pytest.approx(2.0)
    assert summary["realized_pnl_opportunity_count"] == 1
    assert summary["settled_execution_count"] == 1
    assert summary["pending_execution_count"] == 0


def test_unsettled_execution_excluded_until_settled():
    tracker = ConfirmedPnLTracker()

    execution_id = tracker.record_execution_receipt(
        opportunity_id="opp-2",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=0.0,
        sell_price=0.55,
        sell_size=100,
        sell_fee=0.0,
        source="exchange",
        mark_confirmed=True,
        mark_settled=False,
    )

    before_settle = tracker.summarize(scan_interval_seconds=30)
    assert before_settle["realized_pnl_usd"] == pytest.approx(0.0)
    assert before_settle["settled_execution_count"] == 0
    assert before_settle["pending_execution_count"] == 1

    assert tracker.mark_settled(execution_id, settlement_id="settle-2") is True

    after_settle = tracker.summarize(scan_interval_seconds=30)
    assert after_settle["realized_pnl_usd"] == pytest.approx(5.0)
    assert after_settle["settled_execution_count"] == 1
    assert after_settle["pending_execution_count"] == 0


def test_mark_settled_requires_confirmed_legs_by_default():
    tracker = ConfirmedPnLTracker()
    execution_id = tracker.register_execution(
        opportunity_id="opp-3",
        buy_platform="kalshi",
        sell_platform="polymarket",
    )

    tracker.record_fill(
        execution_id=execution_id,
        leg="buy",
        fill_id="fill-buy-1",
        platform="kalshi",
        price=0.50,
        size=100,
        fee=0.1,
    )
    tracker.record_fill(
        execution_id=execution_id,
        leg="sell",
        fill_id="fill-sell-1",
        platform="polymarket",
        price=0.55,
        size=100,
        fee=0.1,
    )

    assert tracker.mark_settled(execution_id, settlement_id="settle-3") is False
    tracker.mark_leg_confirmed(execution_id, "buy")
    tracker.mark_leg_confirmed(execution_id, "sell")
    assert tracker.mark_settled(execution_id, settlement_id="settle-3") is True


def test_duplicate_fill_ids_are_ignored():
    tracker = ConfirmedPnLTracker()
    execution_id = tracker.register_execution(
        opportunity_id="opp-4",
        buy_platform="kalshi",
        sell_platform="polymarket",
    )

    added_first = tracker.record_fill(
        execution_id=execution_id,
        leg="buy",
        fill_id="dup-fill",
        platform="kalshi",
        price=0.50,
        size=50,
        fee=0.05,
    )
    added_duplicate = tracker.record_fill(
        execution_id=execution_id,
        leg="buy",
        fill_id="dup-fill",
        platform="kalshi",
        price=0.50,
        size=50,
        fee=0.05,
    )

    assert added_first is True
    assert added_duplicate is False
    assert len(tracker.executions[execution_id].buy_fills) == 1


def test_simulated_receipts_excluded_by_default():
    tracker = ConfirmedPnLTracker(allow_simulated_confirmed=False)

    tracker.record_execution_receipt(
        opportunity_id="opp-5",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=0.0,
        sell_price=0.54,
        sell_size=100,
        sell_fee=0.0,
        source="simulation",
        mark_confirmed=True,
        mark_settled=True,
    )

    summary = tracker.summarize(scan_interval_seconds=30)
    assert summary["realized_pnl_usd"] == pytest.approx(0.0)
    assert summary["settled_execution_count"] == 0


def test_simulated_receipts_included_when_enabled():
    tracker = ConfirmedPnLTracker(allow_simulated_confirmed=True)

    tracker.record_execution_receipt(
        opportunity_id="opp-6",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=0.0,
        sell_price=0.54,
        sell_size=100,
        sell_fee=0.0,
        source="simulation",
        mark_confirmed=True,
        mark_settled=True,
    )

    summary = tracker.summarize(scan_interval_seconds=30)
    assert summary["realized_pnl_usd"] == pytest.approx(4.0)
    assert summary["settled_execution_count"] == 1


def test_unbalanced_leg_sizes_use_matched_volume_only():
    tracker = ConfirmedPnLTracker()

    tracker.record_execution_receipt(
        opportunity_id="opp-7",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=0.0,
        sell_price=0.60,
        sell_size=60,
        sell_fee=0.0,
        source="exchange",
        mark_confirmed=True,
        mark_settled=True,
    )

    summary = tracker.summarize(scan_interval_seconds=30)
    assert summary["realized_pnl_usd"] == pytest.approx(6.0)


def test_summary_since_timestamp_filters_window():
    tracker = ConfirmedPnLTracker()

    t1 = time.time() - 600
    t2 = time.time() - 10

    tracker.record_execution_receipt(
        opportunity_id="opp-8a",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=0.0,
        sell_price=0.54,
        sell_size=100,
        sell_fee=0.0,
        source="exchange",
        mark_confirmed=True,
        mark_settled=True,
        timestamp=t1,
    )
    tracker.record_execution_receipt(
        opportunity_id="opp-8b",
        buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=0.50,
        buy_size=100,
        buy_fee=0.0,
        sell_price=0.56,
        sell_size=100,
        sell_fee=0.0,
        source="exchange",
        mark_confirmed=True,
        mark_settled=True,
        timestamp=t2,
    )

    summary_recent = tracker.summarize(scan_interval_seconds=30, since_timestamp=t2 - 1)
    assert summary_recent["realized_pnl_usd"] == pytest.approx(6.0)
    assert summary_recent["settled_execution_count"] == 1

    summary_all = tracker.summarize(scan_interval_seconds=30)
    assert summary_all["realized_pnl_usd"] == pytest.approx(10.0)
    assert summary_all["settled_execution_count"] == 2


def test_trade_tape_reconciliation_marks_leg_confirmed():
    tracker = ConfirmedPnLTracker()
    execution_id = tracker.register_execution(
        opportunity_id="opp-9",
        buy_platform="kalshi",
        sell_platform="polymarket",
    )

    tracker.record_fill(
        execution_id=execution_id,
        leg="buy",
        fill_id="fill-buy-9",
        platform="kalshi",
        price=0.51,
        size=100,
        fee=0.0,
        auto_confirm=False,
    )

    tracker.ingest_trade_tape_event(
        platform="kalshi",
        market_id="MKT-9",
        data={"price": 0.51, "count": 100, "trade_id": "trade-9"},
    )

    matched = tracker.reconcile_leg_from_trade_tape(
        execution_id=execution_id,
        leg="buy",
        platform="kalshi",
        market_id="MKT-9",
        max_age_seconds=300,
    )
    assert matched is True
    assert tracker.executions[execution_id].buy_confirmed_at is not None
