"""PnL accounting for the COMPLEMENTARY ($1-payout) strategy.

Regression for a bug caught by paper-executing a real complementary opportunity:
_build_result used same-outcome (sell - buy) accounting for all strategies, so a
complementary arb estimated at +$7.63 was booked as -$69.64 (a 92% phantom loss)
that tripped the daily-loss halt. Complementary gross must be
$1*filled - (buy + sell)*filled.

The SAME defect existed independently in ConfirmedPnLTracker (the settled/
locked-in ledger): ExecutionRecord.realized_pnl() applied sell-buy math to
every receipt, so a true +$8.95 complementary trade was reported as -$76.15.
The tracker tests below pin the strategy-aware accounting end to end,
including the capture-ledger replay path used by `kalshi-arb reconcile`.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor
from kalshi_arbitrage.confirmed_pnl import ConfirmedPnLTracker
from kalshi_arbitrage.reconcile import tracker_from_capture


class _Outcome:
    def __init__(self, venue, avg_price, fees, size):
        self.venue = venue
        self.avg_price = avg_price
        self.fees = fees
        self.filled_size = size
        self.order_id = "sim-x"


def _result(strategy_type, buy_price, sell_price, filled=100, buy_fees=0.5, sell_fees=0.1):
    ex = ArbitrageExecutor()
    buy = _Outcome("kalshi", buy_price, buy_fees, filled)
    sell = _Outcome("polymarket", sell_price, sell_fees, filled)
    opp = {"strategy_type": strategy_type, "max_tradeable_volume": filled}
    return ex._build_result("opp", opp, buy, sell, filled, 0.0, None)


def test_complementary_gross_is_dollar_minus_both_legs():
    # Buy Kalshi-NO @ 0.83 + buy PM @ 0.0745, 100 contracts → $1 payout each.
    # gross = 100*(1 - 0.83 - 0.0745) = 9.55; net = 9.55 - 0.5 - 0.1 = 8.95.
    r = _result("complementary", buy_price=0.83, sell_price=0.0745)
    assert abs(r.gross_profit - 9.55) < 1e-6
    assert abs(r.net_profit - 8.95) < 1e-6
    assert r.profit_margin > 0


def test_complementary_not_booked_as_same_outcome_loss():
    # The bug: same-outcome accounting would give gross = (0.0745-0.83)*100 = -75.5.
    r = _result("complementary", buy_price=0.83, sell_price=0.0745)
    assert r.net_profit > 0  # NOT the ~-69 phantom loss


def test_same_outcome_unchanged():
    # Buy @ 0.30, sell @ 0.35 → gross = (0.35-0.30)*100 = 5.0; net = 5 - 0.6 = 4.4.
    r = _result("same_outcome", buy_price=0.30, sell_price=0.35)
    assert abs(r.gross_profit - 5.0) < 1e-6
    assert abs(r.net_profit - 4.4) < 1e-6


# --------------------------------------------------------------------------- #
#  ConfirmedPnLTracker: the settled / locked-in ledger must use the same
#  strategy-aware accounting (it independently re-derives PnL from fills).
# --------------------------------------------------------------------------- #

def _receipt(tracker, strategy_type, settled=True, **overrides):
    kw = dict(
        opportunity_id="o", buy_platform="kalshi", sell_platform="polymarket",
        buy_price=0.83, buy_size=100, buy_fee=0.5,
        sell_price=0.0745, sell_size=100, sell_fee=0.1,
        source="exchange", strategy_type=strategy_type,
        mark_confirmed=True, mark_settled=settled,
    )
    kw.update(overrides)
    return tracker.record_execution_receipt(**kw)


def test_tracker_books_complementary_at_settlement_value():
    # 100 pairs: $1 payout each, entries 0.83 + 0.0745, fees 0.5 + 0.1.
    # realized = 100*(1 - 0.9045) - 0.6 = 8.95. The old same-outcome math
    # booked this exact trade as 7.45 - 83 - 0.6 = -76.15.
    t = ConfirmedPnLTracker()
    eid = _receipt(t, "complementary")
    assert abs(t.executions[eid].realized_pnl() - 8.95) < 1e-9


def test_tracker_complementary_not_booked_as_sell_buy_loss():
    t = ConfirmedPnLTracker()
    eid = _receipt(t, "complementary")
    pnl = t.executions[eid].realized_pnl()
    assert pnl > 0, f"complementary trade booked as phantom loss: {pnl}"
    assert abs(pnl - (-76.15)) > 1.0  # explicitly NOT the buggy figure


def test_tracker_same_outcome_math_unchanged():
    # Buy @ 0.30, sell @ 0.35, 100 shares, fees 0.6 → 5.0 - 0.6 = 4.4.
    t = ConfirmedPnLTracker()
    eid = _receipt(t, "same_outcome", buy_price=0.30, sell_price=0.35)
    assert abs(t.executions[eid].realized_pnl() - 4.4) < 1e-9


def test_tracker_missing_strategy_defaults_to_same_outcome():
    # Old ledger rows carry no strategy_type → legacy accounting, unchanged.
    t = ConfirmedPnLTracker()
    eid = _receipt(t, None, buy_price=0.30, sell_price=0.35)
    assert abs(t.executions[eid].realized_pnl() - 4.4) < 1e-9


def test_tracker_complementary_partial_fill_prorates():
    # Legs filled 100 vs 80 → matched 80 pairs; fees pro-rated to matched size.
    # realized = 80*(1-0.9045) - 0.5*(80/100) - 0.1*(80/80) = 7.64 - 0.4 - 0.1.
    t = ConfirmedPnLTracker()
    eid = _receipt(t, "complementary", sell_size=80)
    assert abs(t.executions[eid].realized_pnl() - (80 * 0.0955 - 0.4 - 0.1)) < 1e-9


def test_tracker_summary_locked_in_uses_complementary_math():
    t = ConfirmedPnLTracker()
    _receipt(t, "complementary", settled=False)  # live: locked-in, not settled
    s = t.summarize(scan_interval_seconds=30)
    assert s["locked_in_execution_count"] == 1
    assert abs(s["locked_in_pnl_usd"] - 8.95) < 1e-9
    assert s["locked_in_pnl_usd"] > 0  # NOT a -76 phantom loss


def test_capture_replay_carries_strategy_type(tmp_path):
    # The reconcile path rebuilds the tracker from the capture ledger; a
    # complementary row must keep its accounting through the round trip.
    ledger = tmp_path / "ex.jsonl"
    ledger.write_text(json.dumps({
        "opportunity_id": "o", "confirmation_source": "exchange",
        "skipped_reason": None, "strategy_type": "complementary",
        "filled_volume": 100, "buy_platform": "kalshi",
        "sell_platform": "polymarket",
        "avg_buy_price": 0.83, "avg_sell_price": 0.0745,
        "buy_fees": 0.5, "sell_fees": 0.1, "execution_id": "e1", "ts": 1000,
    }))
    t = tracker_from_capture(str(ledger))
    execution = list(t.executions.values())[0]
    assert execution.strategy_type() == "complementary"
    assert abs(execution.realized_pnl() - 8.95) < 1e-9
