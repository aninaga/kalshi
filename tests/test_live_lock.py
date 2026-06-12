"""The live-trading lock + simulated gateway: run everything but place real orders.

These tests pin the core guarantee the user asked for: a local run does the
ENTIRE pipeline (detect → match → price → decide → build orders → pre-flight →
simulated fills → PnL) but never places a real order until the lock is armed.
"""

import pytest

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor
from kalshi_arbitrage.execution import KALSHI, POLYMARKET, OrderRequest, SimulatedGateway
from kalshi_arbitrage.execution.live_lock import ARM_PHRASE, LiveTradingLock
from kalshi_arbitrage.execution.venue_gateway import KalshiGateway, PolymarketGateway


def _opp():
    return {
        "opportunity_id": "opp-1", "strategy_type": "same_outcome",
        "buy_platform": "kalshi", "sell_platform": "polymarket",
        "kalshi_price": 0.40, "polymarket_price": 0.50,
        "max_tradeable_volume": 100, "total_profit": 10.0,
        "polymarket_token": "tok",
        "match_data": {"kalshi_market": {"id": "TICK"}},
    }


def _req(venue=KALSHI):
    return OrderRequest(venue=venue, action="buy", size=10, limit_price=0.5,
                        ticker="T", outcome_side="yes", token_id="tok")


# --- Default paper mode runs the FULL path, no real order ------------------- #

async def test_paper_mode_runs_full_path_with_simulated_fills(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "paper", raising=False)
    monkeypatch.setattr(Config, "RISK_GATE_ENABLED", False, raising=False)
    ex = ArbitrageExecutor()

    async def fake_bps(token_id):
        return 1000
    ex.pm_fee_client.get_fee_rate_bps = fake_bps

    result = await ex.execute_arbitrage(_opp())
    assert result.skipped_reason is None
    assert result.confirmation_source == "paper"      # simulated, not exchange
    assert result.filled_volume > 0                    # the full path executed
    assert result.sell_fees > 0                        # real PM fee model applied
    # The active engine must be using SIMULATED gateways in paper mode.
    assert all(getattr(g, "simulated", False) for g in ex.engine.gateways.values())


# --- The real gateways refuse to place unless the lock is armed ------------- #

async def test_real_kalshi_gateway_blocked_when_lock_disarmed(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    LiveTradingLock.instance().disarm()
    gw = KalshiGateway()
    outcome = await gw.place(_req(KALSHI))
    assert outcome.status == "failed"
    assert outcome.error == "live_trading_locked"
    assert outcome.filled_size == 0


async def test_real_polymarket_gateway_blocked_when_lock_disarmed(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    LiveTradingLock.instance().disarm()
    gw = PolymarketGateway()
    outcome = await gw.place(_req(POLYMARKET))
    assert outcome.status == "failed"
    assert outcome.error == "live_trading_locked"


# --- Lock arming logic ------------------------------------------------------ #

def test_lock_not_armed_in_paper_mode(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "paper", raising=False)
    LiveTradingLock.instance().arm()  # even if armed...
    # ...paper mode is never "armed" for real trading.
    assert LiveTradingLock.instance().is_armed() is False


def test_lock_requires_live_mode_and_token(monkeypatch):
    lock = LiveTradingLock.instance()
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    lock.disarm()
    assert lock.is_armed() is False          # live but not armed
    lock.arm()
    assert lock.is_armed() is True           # live + armed
    lock.disarm()
    assert lock.is_armed() is False


def test_env_override_arms_lock(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    LiveTradingLock.instance().disarm()
    monkeypatch.setenv("ARB_LIVE_TRADING_ARM", ARM_PHRASE)
    assert LiveTradingLock.instance().is_armed() is True


def test_simulated_gateway_never_has_real_client():
    gw = SimulatedGateway(KALSHI)
    assert gw.simulated is True
    assert not hasattr(gw, "client")   # nothing that could POST to a venue
