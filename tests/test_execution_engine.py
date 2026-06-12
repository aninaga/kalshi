"""Phase B: general-purpose execution engine + gateway tests.

Uses a fake gateway so no real venue calls happen.
"""

import asyncio

import pytest

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.execution import (
    KALSHI,
    POLYMARKET,
    STATUS_FAILED,
    STATUS_FILLED,
    STATUS_HALTED,
    STATUS_PARTIAL,
    ExecutionEngine,
    KillSwitch,
    OrderOutcome,
    OrderRequest,
)
from kalshi_arbitrage.circuit_breaker import circuit_breaker_manager


class FakeGateway:
    """Scriptable gateway: returns queued outcomes / raises queued errors."""

    def __init__(self, venue, script):
        self.venue = venue
        self.script = list(script)
        self.calls = []
        self.balance = None
        self.canceled = []

    async def place(self, req):
        self.calls.append(req.client_order_id)
        action = self.script.pop(0) if self.script else ("fill", req.size)
        kind, val = action
        if kind == "raise":
            raise RuntimeError(val)
        if kind == "fail":
            return OrderOutcome.failed(self.venue, req.size, val, req.client_order_id)
        if kind == "partial":
            return OrderOutcome(self.venue, STATUS_PARTIAL, req.size, filled_size=val,
                                avg_price=req.limit_price, order_id="o", client_order_id=req.client_order_id)
        return OrderOutcome(self.venue, STATUS_FILLED, req.size, filled_size=(val if val else req.size),
                            avg_price=req.limit_price, order_id="o", client_order_id=req.client_order_id)

    async def cancel(self, order_id):
        self.canceled.append(order_id)
        return True

    async def get_balance(self):
        return self.balance

    async def get_positions(self):
        return []

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Breakers are cached in the global manager; clear them so each test gets a
    # fresh breaker that picks up the test's threshold config.
    circuit_breaker_manager._breakers.clear()
    KillSwitch.instance().reset()
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_RETRY_BASE_DELAY", 0.0, raising=False)
    yield
    KillSwitch.instance().reset()


def _engine(gateway):
    return ExecutionEngine({gateway.venue: gateway})


def _req(venue=KALSHI, size=10):
    return OrderRequest(venue=venue, action="buy", size=size, limit_price=0.5,
                        ticker="T", outcome_side="yes", token_id="tok")


async def test_fill_returns_done():
    gw = FakeGateway(KALSHI, [("fill", 0)])
    out = await _engine(gw).execute(_req(), stable_key="k")
    assert out.status == STATUS_FILLED and out.filled_size == 10


async def test_kill_switch_blocks():
    KillSwitch.instance().trip("test")
    gw = FakeGateway(KALSHI, [("fill", 0)])
    out = await _engine(gw).execute(_req())
    assert out.status == STATUS_HALTED
    assert gw.calls == []  # never reached the gateway


async def test_retry_reuses_same_client_order_id():
    gw = FakeGateway(KALSHI, [("fail", "transient"), ("fill", 0)])
    out = await _engine(gw).execute(_req(), stable_key="k")
    assert out.status == STATUS_FILLED
    assert len(gw.calls) == 2
    assert gw.calls[0] == gw.calls[1], "retry must reuse the same client_order_id (idempotency)"


async def test_exhausted_retries_returns_failure(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 1, raising=False)
    gw = FakeGateway(KALSHI, [("fail", "x"), ("fail", "x")])
    out = await _engine(gw).execute(_req())
    assert out.status == STATUS_FAILED
    assert len(gw.calls) == 2


async def test_circuit_breaker_opens_and_trips_kill_switch(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_CB_FAILURE_THRESHOLD", 2, raising=False)
    gw = FakeGateway(KALSHI, [("raise", "boom"), ("raise", "boom"), ("fill", 0)])
    engine = _engine(gw)
    await engine.execute(_req())   # failure 1
    await engine.execute(_req())   # failure 2 → breaker opens
    out = await engine.execute(_req())  # blocked by open breaker
    assert out.status == STATUS_HALTED
    assert KillSwitch.instance().is_active()[0]
