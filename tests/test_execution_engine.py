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

    def __init__(self, venue, script, positions=None):
        self.venue = venue
        self.script = list(script)
        self.calls = []
        self.requests = []
        self.balance = None
        self.canceled = []
        self.positions = positions or []

    async def place(self, req):
        self.calls.append(req.client_order_id)
        self.requests.append(req)
        action = self.script.pop(0) if self.script else ("fill", req.size)
        kind, val = action
        if kind == "raise":
            raise RuntimeError(val)
        if kind == "fail":
            return OrderOutcome.failed(self.venue, req.size, val, req.client_order_id)
        if kind == "fail_noretry":
            out = OrderOutcome.failed(self.venue, req.size, val, req.client_order_id)
            out.retryable = False
            return out
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
        return self.positions

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


# --- Breaker must count RETURNED failures (the dominant venue failure mode) -- #

async def test_returned_failures_open_breaker_and_trip_kill_switch(monkeypatch):
    """Gateways RETURN OrderOutcome.failed (they rarely raise). Those must
    increment the breaker exactly like raised exceptions — before the fix the
    breaker counted them as SUCCESSES and never opened."""
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_CB_FAILURE_THRESHOLD", 2, raising=False)
    gw = FakeGateway(KALSHI, [("fail", "venue_reject"), ("fail", "venue_reject"),
                              ("fill", 0)])
    engine = _engine(gw)
    await engine.execute(_req())   # returned failure 1
    await engine.execute(_req())   # returned failure 2 → breaker opens
    out = await engine.execute(_req())
    assert out.status == STATUS_HALTED, "open breaker must block further orders"
    assert KillSwitch.instance().is_active()[0]
    assert len(gw.calls) == 2      # third call never reached the gateway


async def test_returned_failure_does_not_reset_breaker_counter(monkeypatch):
    """A returned failure between two raised ones must not reset the count."""
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_CB_FAILURE_THRESHOLD", 3, raising=False)
    gw = FakeGateway(KALSHI, [("raise", "boom"), ("fail", "venue_reject"),
                              ("raise", "boom"), ("fill", 0)])
    engine = _engine(gw)
    await engine.execute(_req())   # raised   → 1
    await engine.execute(_req())   # returned → 2 (the bug reset this to 0)
    await engine.execute(_req())   # raised   → 3 → breaker opens
    out = await engine.execute(_req())
    assert out.status == STATUS_HALTED
    assert len(gw.calls) == 3


async def test_genuine_success_still_resets_breaker(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_CB_FAILURE_THRESHOLD", 3, raising=False)
    gw = FakeGateway(KALSHI, [("fail", "x"), ("fill", 0), ("fail", "x"), ("fill", 0)])
    engine = _engine(gw)
    await engine.execute(_req())               # failure 1
    await engine.execute(_req())               # success → consecutive counter resets
    await engine.execute(_req())               # failure 1 again (not 2)
    out = await engine.execute(_req())         # breaker still closed → fills
    assert out.status == STATUS_FILLED
    assert not KillSwitch.instance().is_active()[0]


# --- Non-retryable failures (ambiguous venue state) are never retried -------- #

async def test_non_retryable_failure_short_circuits_retries(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 3, raising=False)
    gw = FakeGateway(KALSHI, [("fail_noretry", "ambiguous_post:timeout"), ("fill", 0)])
    out = await _engine(gw).execute(_req())
    assert out.status == STATUS_FAILED
    assert len(gw.calls) == 1, "non-retryable failure must never be re-sent"


# --- Risk-reducing orders pass a tripped kill switch / open breaker ---------- #

async def test_risk_reducing_order_bypasses_tripped_kill_switch():
    KillSwitch.instance().trip("test_halt")
    gw = FakeGateway(KALSHI, [("fill", 0)])
    req = _req()
    req.risk_reducing = True
    out = await _engine(gw).execute(req, stable_key="unwind")
    assert out.status == STATUS_FILLED, "an unwind must run even while halted"
    assert len(gw.calls) == 1
    # The halt itself stays in force for everything else.
    assert KillSwitch.instance().is_active()[0]


async def test_normal_order_still_blocked_while_halted():
    KillSwitch.instance().trip("test_halt")
    gw = FakeGateway(KALSHI, [("fill", 0)])
    out = await _engine(gw).execute(_req())
    assert out.status == STATUS_HALTED
    assert gw.calls == []


async def test_risk_reducing_order_bypasses_open_breaker(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_CB_FAILURE_THRESHOLD", 1, raising=False)
    gw = FakeGateway(KALSHI, [("raise", "boom"), ("fill", 0), ("fill", 0)])
    engine = _engine(gw)
    await engine.execute(_req())              # failure → breaker OPEN
    req = _req()
    req.risk_reducing = True
    out = await engine.execute(req, stable_key="unwind")
    assert out.status == STATUS_FILLED, "flatten/unwind must not be blocked by an open breaker"
    # A normal order is still blocked (and trips the kill switch).
    out2 = await engine.execute(_req())
    assert out2.status == STATUS_HALTED


# --- Kalshi poll-timeout must re-fetch fills after cancel (black hole b) ----- #

class _PollTimeoutKalshiClient:
    """place → resting order; polls never go terminal; after cancel the
    re-fetch reveals the order actually part-filled before the cancel."""

    def __init__(self, filled_after_cancel=3, refetch_raises=False):
        self.cancel_called = False
        self.filled_after_cancel = filled_after_cancel
        self.refetch_raises = refetch_raises

    async def place_order(self, *a, **k):
        return {"order": {"order_id": "K1", "status": "resting"}}

    async def get_order(self, order_id):
        if self.cancel_called:
            if self.refetch_raises:
                raise RuntimeError("api down")
            return {"order": {"order_id": "K1", "status": "canceled",
                              "count_filled": self.filled_after_cancel,
                              "average_fill_price": 40}}
        return {"order": {"order_id": "K1", "status": "resting"}}

    async def cancel_order(self, order_id):
        self.cancel_called = True
        return True

    async def close(self):
        pass


@pytest.fixture()
def _fast_poll(monkeypatch):
    from kalshi_arbitrage.execution.live_lock import LiveTradingLock

    monkeypatch.setattr(Config, "FILL_POLL_BUDGET_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(Config, "FILL_POLL_BASE_INTERVAL", 0.005, raising=False)
    monkeypatch.setattr(Config, "FILL_POLL_MAX_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(LiveTradingLock.instance(), "assert_or_warn",
                        lambda venue: (True, None))


async def test_kalshi_poll_timeout_records_fills_found_after_cancel(_fast_poll):
    from kalshi_arbitrage.execution.venue_gateway import KalshiGateway

    client = _PollTimeoutKalshiClient(filled_after_cancel=3)
    gw = KalshiGateway(client=client)
    out = await gw.place(_req(KALSHI, size=10))
    assert client.cancel_called
    assert out.filled_size == 3, "fills landed before cancel must be recorded, not zeroed"
    assert out.status == STATUS_PARTIAL
    assert out.avg_price == pytest.approx(0.40)
    assert out.order_id == "K1"


async def test_kalshi_poll_timeout_unverified_fails_closed(_fast_poll):
    from kalshi_arbitrage.execution.venue_gateway import KalshiGateway

    client = _PollTimeoutKalshiClient(refetch_raises=True)
    gw = KalshiGateway(client=client)
    out = await gw.place(_req(KALSHI, size=10))
    assert out.status == STATUS_FAILED
    assert out.error == "poll_timeout_unverified"
    assert out.retryable is False, "unverifiable cancel state must not be blind-retried"


# --- Operator flatten_all runs through the risk-reducing engine path --------- #

async def test_flatten_all_unwinds_while_halted_via_engine():
    from kalshi_arbitrage.execution.operator import OperatorControls

    gw = FakeGateway(KALSHI, [("fill", 0)],
                     positions=[{"ticker": "T1", "position": 7}])
    ops = OperatorControls(gateways={KALSHI: gw})
    KillSwitch.instance().trip("operator_halt")
    report = await ops.flatten_all()
    assert KillSwitch.instance().is_active()[0]          # flatten keeps the halt
    assert len(gw.requests) == 1, "the unwind must reach the gateway despite the halt"
    unwind = gw.requests[0]
    assert unwind.risk_reducing is True
    assert unwind.action == "sell" and unwind.size == 7
    assert report[KALSHI][0]["unwind_filled"] == 7
