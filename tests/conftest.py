"""Shared test fixtures.

Execution safety is enforced through process-wide singletons (the kill switch and
the live-trading lock) and a global circuit-breaker registry. Those persist
across tests in one pytest process, so without an explicit reset a test that
trips the kill switch (e.g. the failed-unwind test) leaks a halted state into
later tests. Reset all of them around every test for isolation.

CRITICAL: the resets below remove the durable kill-switch sentinel and the
live-trading arm file under ``Config.DATA_DIR``. With DATA_DIR pointing at the
real ``market_data/``, simply RUNNING the test suite would delete a real
operator halt (``EXECUTION_HALT``) — silently disarming a production safety
stop. ``_isolated_data_dir`` therefore points DATA_DIR at a per-test temp dir
BEFORE any reset runs, so no test (or fixture) can ever read or mutate the
production safety state.
"""

import pytest

from kalshi_arbitrage.circuit_breaker import circuit_breaker_manager
from kalshi_arbitrage.config import Config
from kalshi_arbitrage.execution.kill_switch import KillSwitch
from kalshi_arbitrage.execution.live_lock import LiveTradingLock


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Point Config.DATA_DIR at a per-test temp dir (never the real market_data).

    Everything safety-critical resolves under DATA_DIR: the kill-switch
    sentinel, the live-trading arm file, the operator allowlist, and the
    execution-capture ledger. Isolating it per test (a) protects the real
    operator halt from the singleton resets below, and (b) guarantees a test
    that trips/arms anything cannot leak durable state into production or into
    other tests.
    """
    data_dir = tmp_path / "exec_data"
    monkeypatch.setattr(Config, "DATA_DIR", str(data_dir), raising=False)
    yield str(data_dir)


@pytest.fixture(autouse=True)
def _reset_execution_singletons(_isolated_data_dir):
    # Depends on _isolated_data_dir so DATA_DIR is already patched when the
    # resets delete sentinel/arm files (and is still patched at teardown).
    def _reset():
        KillSwitch.instance().reset()
        LiveTradingLock.instance().disarm()
        circuit_breaker_manager._breakers.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _no_production_capture(monkeypatch):
    """Never let a test write the production execution-capture ledger.

    ``ArbitrageExecutor()`` constructs an ``ExecutionCapture`` when
    ``Config.EXECUTION_CAPTURE_ENABLED`` is true, defaulting its path to
    ``market_data/executions/executions.jsonl`` (relative to CWD, i.e. the repo
    root under pytest). ``reconcile.py`` then replays every
    ``confirmation_source=="exchange"`` row as locked-in *live* P&L — so a test
    that exercises the executor with capture enabled and ``DATA_DIR`` unpatched
    silently books fake fills as real money (defect C6).

    Disable capture for every test by default. The handful of tests that
    legitimately exercise capture construct ``ExecutionCapture`` directly with
    an explicit ``tmp_path`` and are unaffected; a test that genuinely needs the
    executor's capture sink can re-enable it (DATA_DIR is already isolated by
    ``_isolated_data_dir``). ``capture.py`` also hard-refuses the default
    production path under pytest as a second line of defence.
    """
    monkeypatch.setattr(Config, "EXECUTION_CAPTURE_ENABLED", False, raising=False)
    yield
