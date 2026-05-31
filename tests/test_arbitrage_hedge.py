"""Phase B: arbitrage orchestration — partial-fill-aware hedge + paper fees."""

import pytest

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor
from kalshi_arbitrage.execution import (
    KALSHI,
    POLYMARKET,
    STATUS_FILLED,
    KillSwitch,
    OrderOutcome,
)


class FakeEngine:
    """Returns scripted outcomes based on the stable_key suffix (buy/sell/hedge)."""

    def __init__(self, by_suffix):
        self.by_suffix = by_suffix
        self.requests = {}

    async def execute(self, req, stable_key=""):
        suffix = stable_key.rsplit(":", 1)[-1]
        self.requests[suffix] = req
        filled = self.by_suffix.get(suffix, req.size)
        status = STATUS_FILLED
        return OrderOutcome(req.venue, status, req.size, filled_size=filled,
                            avg_price=req.limit_price, fees=0.0, order_id=f"o-{suffix}",
                            client_order_id=req.client_order_id)


def _opp():
    return {
        "opportunity_id": "opp-1",
        "strategy_type": "same_outcome",
        "buy_platform": "kalshi",
        "sell_platform": "polymarket",
        "kalshi_price": 0.40,
        "polymarket_price": 0.50,
        "max_tradeable_volume": 100,
        "total_profit": 10.0,
        "polymarket_token": "tok",
        "match_data": {"kalshi_market": {"id": "TICK"}},
    }


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    monkeypatch.setattr(Config, "RISK_GATE_ENABLED", False, raising=False)
    monkeypatch.setattr(Config, "REQUIRE_BALANCE_CHECK", False, raising=False)
    monkeypatch.setattr(Config, "MAX_POSITION_SIZE_USD", 1000.0, raising=False)
    KillSwitch.instance().reset()
    yield
    KillSwitch.instance().reset()


async def test_equal_fills_no_hedge():
    ex = ArbitrageExecutor()
    ex.engine = FakeEngine({"buy": 100, "sell": 100})
    result = await ex.execute_arbitrage(_opp())
    assert result.confirmation_source == "exchange"
    assert result.filled_volume == 100
    assert "hedge" not in ex.engine.requests


async def test_partial_imbalance_triggers_confirmed_hedge():
    ex = ArbitrageExecutor()
    # buy fills 100, sell fills 60 → imbalance 40; hedge sells 40 and fully fills.
    ex.engine = FakeEngine({"buy": 100, "sell": 60, "hedge": 40})
    result = await ex.execute_arbitrage(_opp())
    assert "hedge" in ex.engine.requests, "imbalance must trigger a hedge"
    hedge_req = ex.engine.requests["hedge"]
    assert hedge_req.size == pytest.approx(40)
    assert hedge_req.action == "sell"          # unwind the over-filled BUY leg
    assert hedge_req.venue == KALSHI
    assert result.filled_volume == 60          # matched size
    assert not KillSwitch.instance().is_active()[0]


async def test_failed_unwind_trips_kill_switch():
    ex = ArbitrageExecutor()
    # imbalance 40 but hedge only fills 0 → residual → kill switch.
    ex.engine = FakeEngine({"buy": 100, "sell": 60, "hedge": 0})
    await ex.execute_arbitrage(_opp())
    active, reason = KillSwitch.instance().is_active()
    assert active and "unwind_failed" in reason


async def test_paper_mode_charges_real_polymarket_fees(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_MODE", "paper", raising=False)
    ex = ArbitrageExecutor()

    async def fake_bps(token_id):
        return 1000
    ex.pm_fee_client.get_fee_rate_bps = fake_bps

    result = await ex.execute_arbitrage(_opp())
    assert result.confirmation_source == "paper"
    # Sell leg is on Polymarket — fees must be non-zero (old code hardcoded 0).
    assert result.sell_fees > 0
