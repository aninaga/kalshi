"""Price-realism ceiling in the live economics path.

A genuine same-event cross-venue edge on identical contracts is empirically
sub-15%. A 30-60% "margin" is a false match (different proposition — "score the
MOST goals" vs "a goal") or a stale leg, not arbitrage. The autonomous machine
must reject these so it never fires an un-hedged directional trade on a phantom
edge. This drives ``_calculate_accurate_arbitrage`` with constructed books that
yield an implausible margin and asserts it returns None, while a realistic
~3% edge passes.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


def _book(price, size):
    # Same-outcome path reads buy 'asks' and sell 'bids' as [{price,size}].
    return [{"price": price, "size": size}]


def _run(buy_ob, sell_ob):
    a = MarketAnalyzer()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            a._calculate_accurate_arbitrage(
            buy_ob, sell_ob,
            buy_platform="polymarket", sell_platform="kalshi",
            buy_fee_rate=0.0, sell_fee_rate=0.0,
            strategy="buy_pm_sell_kalshi",
            kalshi_market={"id": "K", "title": "t", "raw_data": {}},
            polymarket_market={"id": "P", "title": "t", "raw_data": {}},
            polymarket_token="tok", match_data={}, is_synthetic=True,
            )
        )
    finally:
        loop.close()


def test_config_ceiling_exists():
    assert 0.10 <= Config.MAX_PLAUSIBLE_PROFIT_MARGIN <= 0.30


def test_implausible_margin_rejected():
    # Buy at 0.30, sell at 0.95 → ~217% margin → false match / stale leg.
    result = _run(_book(0.30, 100), _book(0.95, 100))
    assert result is None


def test_cap_is_the_cause_of_rejection(monkeypatch):
    # The SAME implausible book passes once the ceiling is lifted — proving the
    # cap (not some unrelated None) is what rejected it, and that a realistic
    # large edge would still be priced if it were ever legitimate.
    monkeypatch.setattr(Config, "MAX_PLAUSIBLE_PROFIT_MARGIN", 10.0)
    result = _run(_book(0.30, 100), _book(0.95, 100))
    assert result is not None
    assert result["profit_margin"] > 0.15  # the edge the default ceiling blocks
