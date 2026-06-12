"""Executor PnL accounting for the COMPLEMENTARY ($1-payout) strategy.

Regression for a bug caught by paper-executing a real complementary opportunity:
_build_result used same-outcome (sell - buy) accounting for all strategies, so a
complementary arb estimated at +$7.63 was booked as -$69.64 (a 92% phantom loss)
that tripped the daily-loss halt. Complementary gross must be
$1*filled - (buy + sell)*filled.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor


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
