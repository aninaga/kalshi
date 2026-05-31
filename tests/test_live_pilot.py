"""Phase D: live-pilot gates, operator controls, readiness checklist."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor
from kalshi_arbitrage.execution import KALSHI, POLYMARKET, KillSwitch, OrderOutcome
from kalshi_arbitrage.execution.operator import OperatorControls
from research.pilot.live_readiness_checklist import (
    check_allowlist,
    check_kill_switch,
    check_live_readiness,
)


def _opp():
    return {
        "opportunity_id": "opp-1", "strategy_type": "same_outcome",
        "buy_platform": "kalshi", "sell_platform": "polymarket",
        "kalshi_price": 0.40, "polymarket_price": 0.50,
        "max_tradeable_volume": 100, "total_profit": 10.0,
        "polymarket_token": "P1",
        "match_data": {"kalshi_market": {"id": "K1"}},
    }


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MODE", "live", raising=False)
    monkeypatch.setattr(Config, "RISK_GATE_ENABLED", False, raising=False)
    monkeypatch.setattr(Config, "REQUIRE_BALANCE_CHECK", False, raising=False)
    KillSwitch.instance().reset()
    yield
    KillSwitch.instance().reset()


# --- Allowlist gate --------------------------------------------------------- #

async def test_live_rejects_unlisted_pair(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "MATCH_REQUIRE_ALLOWLIST_FOR_LIVE", True, raising=False)
    f = tmp_path / "allow.json"
    f.write_text('{"approved": [], "denied": []}')
    ex = ArbitrageExecutor()
    ex.allowlist.path = str(f)
    ex.allowlist._mtime = None
    result = await ex.execute_arbitrage(_opp())
    assert result.skipped_reason == "not_allowlisted"


async def test_live_allows_listed_pair(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "MATCH_REQUIRE_ALLOWLIST_FOR_LIVE", True, raising=False)
    f = tmp_path / "allow.json"
    f.write_text('{"approved": [{"kalshi": "K1", "polymarket": "P1"}], "denied": []}')
    ex = ArbitrageExecutor()
    ex.allowlist.path = str(f)
    ex.allowlist._mtime = None

    class FakeEngine:
        async def execute(self, req, stable_key=""):
            return OrderOutcome(req.venue, "filled", req.size, filled_size=req.size,
                                avg_price=req.limit_price, order_id="o")
    ex.engine = FakeEngine()
    result = await ex.execute_arbitrage(_opp())
    assert result.skipped_reason is None
    assert result.confirmation_source == "exchange"


# --- Staged-size clamp ------------------------------------------------------ #

def test_live_notional_clamp(monkeypatch):
    monkeypatch.setattr(Config, "LIVE_MAX_NOTIONAL_USD", 5.0, raising=False)
    monkeypatch.setattr(Config, "MAX_POSITION_SIZE_USD", 100.0, raising=False)
    ex = ArbitrageExecutor()
    # buy price 0.40 → 5 USD / 0.40 = 12.5 shares cap, well below 100 requested.
    vol = ex._capped_volume(_opp())
    assert vol == pytest.approx(12.5)


# --- Concurrency cap -------------------------------------------------------- #

async def test_max_concurrent_positions(monkeypatch):
    monkeypatch.setattr(Config, "MATCH_REQUIRE_ALLOWLIST_FOR_LIVE", False, raising=False)
    monkeypatch.setattr(Config, "LIVE_MAX_CONCURRENT_POSITIONS", 1, raising=False)
    ex = ArbitrageExecutor()
    ex._live_in_flight = 1  # pretend one trade already in flight
    result = await ex.execute_arbitrage(_opp())
    assert result.skipped_reason == "max_concurrent_positions"


# --- Operator controls ------------------------------------------------------ #

class FakeGateway:
    def __init__(self, venue, positions):
        self.venue = venue
        self._positions = positions
        self.placed = []

        class _Client:
            async def cancel_all(self_inner):
                return {}
        self.client = _Client()

    async def get_positions(self):
        return self._positions

    async def place(self, req):
        self.placed.append(req)
        return OrderOutcome(self.venue, "filled", req.size, filled_size=req.size,
                            avg_price=req.limit_price, order_id="o")

    async def cancel(self, oid):
        return True

    async def close(self):
        pass


async def test_flatten_all_unwinds_positions():
    gw_k = FakeGateway(KALSHI, [{"ticker": "K1", "position": 30}])
    gw_p = FakeGateway(POLYMARKET, [{"asset": "P1", "size": -20}])
    op = OperatorControls(gateways={KALSHI: gw_k, POLYMARKET: gw_p})
    report = await op.flatten_all()
    assert KillSwitch.instance().is_active()[0]      # halted during flatten
    assert gw_k.placed[0].action == "sell" and gw_k.placed[0].size == 30
    assert gw_p.placed[0].action == "buy" and gw_p.placed[0].size == 20
    assert report[KALSHI] and report[POLYMARKET]


async def test_position_health_flags_open_exposure():
    gw_k = FakeGateway(KALSHI, [{"ticker": "K1", "position": 30}])
    op = OperatorControls(gateways={KALSHI: gw_k})
    health = await op.position_health()
    assert health["alerts"], "open position should raise an alert"


# --- Readiness checklist ---------------------------------------------------- #

def test_check_kill_switch_functional():
    KillSwitch.instance().reset()
    assert check_kill_switch().passed


def test_check_allowlist_empty_fails(tmp_path):
    f = tmp_path / "a.json"
    f.write_text('{"approved": [], "denied": []}')
    assert not check_allowlist(str(f)).passed


def test_check_allowlist_nonempty_passes(tmp_path):
    f = tmp_path / "a.json"
    f.write_text('{"approved": [{"kalshi": "K1", "polymarket": "P1"}]}')
    assert check_allowlist(str(f)).passed


def test_readiness_report_fails_when_inputs_missing():
    report = check_live_readiness(
        labeled_pairs_path="/nonexistent/labeled.jsonl",
        executions_path="/nonexistent/ex.jsonl",
        allowlist_path="/nonexistent/allow.json",
    )
    assert not report.passed
    assert "FAIL" in report.summary()
    # kill switch check should still pass even when files are missing.
    names = {c.name: c.passed for c in report.checks}
    assert names["kill_switch"] is True
    assert names["matching_precision_gate"] is False
