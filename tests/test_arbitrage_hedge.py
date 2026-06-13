"""Phase B: arbitrage orchestration — partial-fill-aware hedge + paper fees."""

import asyncio

import pytest

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor
from kalshi_arbitrage.execution import (
    KALSHI,
    POLYMARKET,
    STATUS_FILLED,
    ExecutionEngine,
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
    monkeypatch.setattr(Config, "MATCH_REQUIRE_ALLOWLIST_FOR_LIVE", False, raising=False)
    monkeypatch.setattr(Config, "MAX_POSITION_SIZE_USD", 1000.0, raising=False)
    monkeypatch.setattr(Config, "LIVE_MAX_NOTIONAL_USD", 1000.0, raising=False)
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


# --- Whole-contract sizing shared by BOTH legs ------------------------------- #

async def test_whole_contract_sizing_shared_across_legs(monkeypatch):
    """$5 cap at 0.40 = 12.5 raw → both legs must get the SAME integer 12.

    Before the fix the float 12.5 reached both gateways: Kalshi truncated to
    12 while Polymarket kept 12.5 — a permanent half-share leg imbalance."""
    monkeypatch.setattr(Config, "MAX_POSITION_SIZE_USD", 5.0, raising=False)
    monkeypatch.setattr(Config, "LIVE_MAX_NOTIONAL_USD", 5.0, raising=False)
    ex = ArbitrageExecutor()
    ex.engine = FakeEngine({})
    result = await ex.execute_arbitrage(_opp())
    buy_req = ex.engine.requests["buy"]
    sell_req = ex.engine.requests["sell"]
    assert buy_req.size == 12 and sell_req.size == 12
    assert float(buy_req.size).is_integer() and float(sell_req.size).is_integer()
    assert result.filled_volume == 12


async def test_zero_floored_size_aborts_opportunity(monkeypatch):
    """cap $0.30 at 0.40 = 0.75 raw → floors to 0 → must ABORT, never place a
    partial fractional leg."""
    monkeypatch.setattr(Config, "MAX_POSITION_SIZE_USD", 0.30, raising=False)
    monkeypatch.setattr(Config, "LIVE_MAX_NOTIONAL_USD", 0.30, raising=False)
    ex = ArbitrageExecutor()
    ex.engine = FakeEngine({})
    result = await ex.execute_arbitrage(_opp())
    assert result.skipped_reason == "unbuildable_legs"
    assert ex.engine.requests == {}, "no leg may fire when the size floors to 0"


# --- Hedge is risk-reducing and survives a tripped kill switch --------------- #

async def test_hedge_request_is_marked_risk_reducing():
    ex = ArbitrageExecutor()
    ex.engine = FakeEngine({"buy": 100, "sell": 60, "hedge": 40})
    await ex.execute_arbitrage(_opp())
    assert ex.engine.requests["hedge"].risk_reducing is True


class _ScriptGateway:
    """Real-engine fake: fills everything, or raises the first N calls."""

    def __init__(self, venue, raises_first=0):
        self.venue = venue
        self.raises_first = raises_first
        self.placed = []

    async def place(self, req):
        self.placed.append(req)
        if self.raises_first > 0:
            self.raises_first -= 1
            raise RuntimeError("venue down")
        return OrderOutcome(self.venue, STATUS_FILLED, req.size,
                            filled_size=req.size, avg_price=req.limit_price,
                            order_id=f"o-{len(self.placed)}",
                            client_order_id=req.client_order_id)

    async def cancel(self, order_id):
        return True

    async def get_balance(self):
        return None

    async def get_positions(self):
        return []

    async def close(self):
        pass


async def test_hedge_unwinds_even_after_kill_switch_trip(monkeypatch):
    """One leg's breaker trips the kill switch; the hedge that unwinds the
    OTHER (filled) leg must still go through — before the fix the halt blocked
    its own unwind and the book stayed one-legged."""
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 1, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_RETRY_BASE_DELAY", 0.0, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_CB_FAILURE_THRESHOLD", 1, raising=False)
    kalshi_gw = _ScriptGateway(KALSHI)
    pm_gw = _ScriptGateway(POLYMARKET, raises_first=2)  # fails, retry hits OPEN breaker
    ex = ArbitrageExecutor()
    # Make the executor's mode-sync install OUR fakes into the real engine.
    ex._real_gateways = {KALSHI: kalshi_gw, POLYMARKET: pm_gw}
    ex.engine = ExecutionEngine({KALSHI: kalshi_gw, POLYMARKET: pm_gw},
                                kill_switch=KillSwitch.instance())
    result = await ex.execute_arbitrage(_opp())

    active, reason = KillSwitch.instance().is_active()
    assert active and "circuit_open" in reason          # the PM breaker halted us
    hedge_reqs = [r for r in kalshi_gw.placed if r.meta.get("hedge")]
    assert hedge_reqs, "the unwind must reach the venue despite the halt"
    assert hedge_reqs[0].risk_reducing is True
    assert hedge_reqs[0].size == pytest.approx(100)     # unwind the filled buy leg
    assert result.skipped_reason == "partial_fill_hedged"
    # The unwind FILLED, so the failed-unwind path must NOT have re-tripped.
    assert "unwind_failed" not in (reason or "")


# --- Execution timeout must not orphan a filled leg --------------------------- #

class _HangingEngine:
    """Buy leg completes instantly; sell leg hangs past the timeout."""

    def __init__(self, hang=("sell",)):
        self.hang = hang
        self.requests = {}

    async def execute(self, req, stable_key=""):
        suffix = stable_key.rsplit(":", 1)[-1]
        self.requests[suffix] = req
        if suffix in self.hang:
            await asyncio.sleep(30)
        return OrderOutcome(req.venue, STATUS_FILLED, req.size,
                            filled_size=req.size, avg_price=req.limit_price,
                            order_id=f"o-{suffix}",
                            client_order_id=req.client_order_id)


async def test_timeout_captures_filled_leg_and_hedges_it(monkeypatch):
    """Outer timeout used to cancel BOTH legs and report a bare
    execution_timeout — discarding the buy leg that had already FILLED.
    The filled leg must be kept, hedged, and recorded."""
    monkeypatch.setattr(Config, "EXECUTION_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(Config, "FILL_POLL_BUDGET_SECONDS", 0.0, raising=False)
    ex = ArbitrageExecutor()
    ex.engine = _HangingEngine(hang=("sell",))
    result = await ex.execute_arbitrage(_opp())
    assert "hedge" in ex.engine.requests, "the filled buy leg must be unwound, not dropped"
    hedge_req = ex.engine.requests["hedge"]
    assert hedge_req.size == pytest.approx(100)
    assert hedge_req.venue == KALSHI and hedge_req.action == "sell"
    assert result.skipped_reason == "partial_fill_hedged"
    assert not KillSwitch.instance().is_active()[0]     # hedge confirmed → no trip


async def test_timeout_with_no_fills_reports_execution_timeout(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(Config, "FILL_POLL_BUDGET_SECONDS", 0.0, raising=False)
    ex = ArbitrageExecutor()
    ex.engine = _HangingEngine(hang=("buy", "sell"))
    result = await ex.execute_arbitrage(_opp())
    assert result.skipped_reason == "execution_timeout"
    assert "hedge" not in ex.engine.requests
