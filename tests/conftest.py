"""Shared test fixtures.

Execution safety is enforced through process-wide singletons (the kill switch and
the live-trading lock) and a global circuit-breaker registry. Those persist
across tests in one pytest process, so without an explicit reset a test that
trips the kill switch (e.g. the failed-unwind test) leaks a halted state into
later tests. Reset all of them around every test for isolation.
"""

import pytest

from kalshi_arbitrage.circuit_breaker import circuit_breaker_manager
from kalshi_arbitrage.execution.kill_switch import KillSwitch
from kalshi_arbitrage.execution.live_lock import LiveTradingLock


@pytest.fixture(autouse=True)
def _reset_execution_singletons():
    def _reset():
        KillSwitch.instance().reset()
        LiveTradingLock.instance().disarm()
        circuit_breaker_manager._breakers.clear()

    _reset()
    yield
    _reset()
