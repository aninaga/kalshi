"""Live-perimeter regressions: every path that could place, permit, or
silently un-block a real order must fail CLOSED.

Covers:
  * the RAW KalshiOrderClient refusing mutating requests when not armed/halted
    (previously gated only one layer up, so any REPL/script could POST);
  * no implicit repo-root .pem signing-key fallback;
  * Config.DATA_DIR anchored absolute (a halt from the wrong cwd was a no-op);
  * the risk gate actually able to reject (max score was 38 vs threshold 60)
    and failing closed on scoring errors;
  * the readiness probe and the test suite never clearing a real operator halt.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.execution.kill_switch import KillSwitch
from kalshi_arbitrage.execution.live_lock import ARM_PHRASE, LiveTradingLock
from kalshi_arbitrage.kalshi_executor import KalshiOrderClient, kalshi_auth_headers
from kalshi_arbitrage.risk_engine import RiskEngine
from kalshi_arbitrage.validation.pilot.live_readiness_checklist import check_kill_switch

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --------------------------------------------------------------------------- #
#  Raw KalshiOrderClient: the client itself must refuse mutating requests      #
# --------------------------------------------------------------------------- #

@pytest.fixture
def _gated_client(monkeypatch):
    """A KalshiOrderClient whose HTTP layer explodes if ever reached."""
    client = KalshiOrderClient()

    async def _never(*a, **k):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("HTTP layer must never be reached when blocked")

    monkeypatch.setattr(client, "_get_session", _never)
    # Credentials present so the old code path would have proceeded to sign.
    monkeypatch.setenv("KALSHI_API_KEY", "test-key")
    return client


def test_raw_client_blocks_place_order_when_not_armed(_gated_client):
    # Default state: paper mode, lock disarmed. The RAW client must refuse.
    resp = _run(_gated_client.place_order("KX-T", "yes", "buy", 1, 50))
    assert resp == {"error": "live_trading_locked"}


def test_raw_client_blocks_cancel_when_not_armed(_gated_client):
    ok = _run(_gated_client.cancel_order("some-order"))
    assert ok is False  # cancel surfaced as failure, HTTP never reached


def test_raw_client_blocks_post_when_kill_switch_tripped(monkeypatch, _gated_client):
    # Even fully ARMED, a tripped kill switch must block new orders.
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setenv("ARB_LIVE_TRADING_ARM", ARM_PHRASE)
    assert LiveTradingLock.instance().is_armed()
    KillSwitch.instance().trip("test_halt")
    resp = _run(_gated_client.place_order("KX-T", "yes", "buy", 1, 50))
    assert resp["error"].startswith("halted:")


def test_mutation_gate_allows_cancel_during_halt_when_armed(monkeypatch):
    # A halt must be able to take risk OFF: cancels stay permitted (armed),
    # new opening orders do not.
    client = KalshiOrderClient()
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setenv("ARB_LIVE_TRADING_ARM", ARM_PHRASE)
    KillSwitch.instance().trip("test_halt")
    assert client._mutation_blocked("POST", "/portfolio/orders").startswith("halted:")
    assert client._mutation_blocked("DELETE", "/portfolio/orders/x") is None


def test_risk_reducing_post_passes_halt_but_opening_post_does_not(monkeypatch):
    # CROSS-LANE FIX (2026-06-12): a risk-reducing (exposure-decreasing) POST —
    # a hedge/flatten unwind — must pass a kill-switch halt one layer below the
    # engine, or a tripped breaker would strand a one-legged book at the raw
    # client. An OPENING POST stays blocked. Arming is still required for both.
    client = KalshiOrderClient()
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setenv("ARB_LIVE_TRADING_ARM", ARM_PHRASE)
    KillSwitch.instance().trip("test_halt")
    # Opening order: still blocked.
    assert client._mutation_blocked(
        "POST", "/portfolio/orders", risk_reducing=False).startswith("halted:")
    # Risk-reducing unwind: permitted through the halt.
    assert client._mutation_blocked(
        "POST", "/portfolio/orders", risk_reducing=True) is None


def test_risk_reducing_post_still_blocked_when_not_armed(monkeypatch):
    # The arming requirement is NEVER bypassed — risk-reducing does not make a
    # disarmed/paper system fire a real order.
    client = KalshiOrderClient()
    monkeypatch.setattr(Config, "EXECUTION_MODE", "paper", raising=False)
    monkeypatch.delenv("ARB_LIVE_TRADING_ARM", raising=False)
    assert client._mutation_blocked(
        "POST", "/portfolio/orders", risk_reducing=True) == "live_trading_locked"


def test_mutation_gate_check_failure_fails_closed(monkeypatch):
    # If the safety-gate evaluation itself breaks, the request must be BLOCKED.
    client = KalshiOrderClient()

    def _boom():
        raise RuntimeError("gate machinery broken")

    # Scoped context: undo the breakage before the conftest teardown (which
    # itself calls LiveTradingLock.instance()) runs.
    with monkeypatch.context() as m:
        m.setattr(LiveTradingLock, "instance", classmethod(lambda cls: _boom()))
        blocked = client._mutation_blocked("POST", "/portfolio/orders")
    assert blocked is not None and blocked.startswith("safety_gate_error")


# --------------------------------------------------------------------------- #
#  Signing key: explicit path only — no repo-root .pem fallback                #
# --------------------------------------------------------------------------- #

def test_auth_headers_require_explicit_key_path(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY", "test-key")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(RuntimeError, match="KALSHI_PRIVATE_KEY_PATH"):
        kalshi_auth_headers("POST", "/trade-api/v2/portfolio/orders")


def test_auth_headers_missing_key_file_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_API_KEY", "test-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem"))
    with pytest.raises(RuntimeError, match="not found"):
        kalshi_auth_headers("POST", "/trade-api/v2/portfolio/orders")


def test_no_credentials_still_returns_empty(monkeypatch):
    # No API key at all -> no signing attempted -> the old {} contract holds.
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    assert kalshi_auth_headers("GET", "/trade-api/v2/markets") == {}


def test_no_repo_root_pem_default_in_source():
    src = (REPO_ROOT / "kalshi_arbitrage" / "kalshi_executor.py").read_text()
    assert "kalshi_private_key.pem" not in src, (
        "the implicit repo-root signing-key fallback must stay removed")


# --------------------------------------------------------------------------- #
#  DATA_DIR: safety state must resolve identically regardless of cwd           #
# --------------------------------------------------------------------------- #

def _load_fresh_config():
    """Import a pristine copy of config.py (the autouse fixture patches the
    shared Config class, so the live attribute can't be inspected directly)."""
    cfg_path = REPO_ROOT / "kalshi_arbitrage" / "config.py"
    spec = importlib.util.spec_from_file_location("_cfg_fresh_probe", cfg_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Config


def test_data_dir_is_absolute_and_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.delenv("KALSHI_ARB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # a halt from the "wrong" directory
    from_tmp = _load_fresh_config().DATA_DIR
    monkeypatch.chdir(REPO_ROOT)
    from_repo = _load_fresh_config().DATA_DIR
    assert os.path.isabs(from_tmp)
    assert from_tmp == from_repo == str(REPO_ROOT / "market_data")


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_ARB_DATA_DIR", str(tmp_path / "override"))
    assert _load_fresh_config().DATA_DIR == str(tmp_path / "override")


def test_kill_switch_sentinel_under_data_dir():
    ks = KillSwitch.instance()
    assert ks.sentinel_path == os.path.join(Config.DATA_DIR, Config.EXECUTION_HALT_SENTINEL)


# --------------------------------------------------------------------------- #
#  Risk gate: must be able to reject, and must fail closed on errors           #
# --------------------------------------------------------------------------- #

def test_risk_scoring_error_fails_closed():
    engine = RiskEngine()
    # Garbage volume makes Decimal() raise inside scoring.
    bad_opp = {"opportunity_id": "x", "max_tradeable_volume": "not-a-number"}
    a = _run(engine.assess_opportunity(bad_opp, None, None))
    assert float(a.total_risk_score) > Config.MAX_RISK_SCORE
    assert float(a.confidence) < Config.MIN_RISK_CONFIDENCE
    assert any("fail" in r.lower() for r in a.recommendations)


def test_toxic_opportunity_can_exceed_reject_threshold():
    # No orderbooks to re-verify + sub-2% margin + cross-venue: the worst
    # realistic case must be REJECTABLE. (Pre-rescale max score was ~38, below
    # the 60 threshold, so the gate could never reject anything.)
    engine = RiskEngine()
    opp = {"opportunity_id": "x", "max_tradeable_volume": 100,
           "profit_margin": 0.01, "buy_platform": "kalshi",
           "sell_platform": "polymarket"}
    a = _run(engine.assess_opportunity(opp, None, None))
    assert float(a.total_risk_score) > Config.MAX_RISK_SCORE
    # And with zero orderbook data there is zero assessment confidence.
    assert float(a.confidence) < Config.MIN_RISK_CONFIDENCE


def test_executor_abstains_on_bookless_opportunity():
    # Integrator decision (2026-06-12): the risk gate ABSTAINS on a bookless
    # opportunity (nothing in the pipeline attaches risk_orderbooks yet) rather
    # than rejecting 100% of opps and darkening the paper desk. So a bookless
    # opp must NOT be skipped for a RISK reason — the lock/allowlist/kill-switch
    # remain the real gates (covered elsewhere). The risk ENGINE can still
    # reject a toxic opp when books ARE present
    # (see test_toxic_opportunity_can_exceed_reject_threshold).
    from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor

    opp = {"opportunity_id": "opp-1", "strategy_type": "same_outcome",
           "buy_platform": "kalshi", "sell_platform": "polymarket",
           "kalshi_price": 0.40, "polymarket_price": 0.50,
           "max_tradeable_volume": 100, "total_profit": 10.0,
           "polymarket_token": "P1",
           "match_data": {"kalshi_market": {"id": "K1"}}}
    ex = ArbitrageExecutor()
    result = _run(ex.execute_arbitrage(opp))
    assert result.skipped_reason not in (
        "risk_too_high", "low_risk_confidence", "risk_gate_error"), \
        f"bookless opp must abstain at the risk gate, got {result.skipped_reason}"


# --------------------------------------------------------------------------- #
#  Readiness probe + test suite must never clear a real operator halt          #
# --------------------------------------------------------------------------- #

def test_readiness_probe_preserves_active_halt():
    ks = KillSwitch.instance()
    ks.trip("operator_halt")  # lands in the per-test isolated DATA_DIR
    sentinel = ks.sentinel_path
    assert os.path.exists(sentinel)
    res = check_kill_switch()
    assert res.passed is False, "readiness must FAIL while a halt is active"
    assert os.path.exists(sentinel), "the probe deleted a real operator halt"
    assert ks.is_active()[0], "the probe disarmed a real operator halt"


def test_readiness_probe_never_touches_production_switch(monkeypatch):
    ks = KillSwitch.instance()
    calls = []
    orig_trip, orig_reset = ks.trip, ks.reset
    monkeypatch.setattr(ks, "trip", lambda reason: (calls.append(("trip", reason)), orig_trip(reason)))
    monkeypatch.setattr(ks, "reset", lambda: (calls.append(("reset",)), orig_reset()))
    res = check_kill_switch()
    assert res.passed is True
    assert calls == [], "probe must use a throwaway instance, not the singleton"
    # And no sentinel may appear under the live DATA_DIR as a side effect.
    assert not os.path.exists(os.path.join(Config.DATA_DIR, Config.EXECUTION_HALT_SENTINEL))


def test_suite_runs_with_isolated_data_dir():
    # The autouse conftest fixture must keep every test away from the repo's
    # real market_data — otherwise the singleton resets between tests would
    # delete a production EXECUTION_HALT sentinel.
    assert Path(Config.DATA_DIR).resolve() != (REPO_ROOT / "market_data").resolve()
    assert KillSwitch.instance().sentinel_path.startswith(str(Config.DATA_DIR))
