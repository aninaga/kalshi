"""Live-reconciliation wiring: venue lineage + locked-in vs settled + reconciler.

For a live cross-venue arb, profit is LOCKED at fill but REALIZED at resolution
(months out). These pin: gateways capture venue fill ids; the executor/capture
carry them; a live receipt lands confirmed-but-not-settled (locked-in bucket);
the reconciler overwrites estimated fees with venue truth and settles when both
markets resolve. No network — venue clients are faked.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor
from kalshi_arbitrage.confirmed_pnl import ConfirmedPnLTracker
from kalshi_arbitrage.execution.capture import ExecutionCapture
from kalshi_arbitrage.execution.order_types import OrderRequest
from kalshi_arbitrage.execution.venue_gateway import KalshiGateway
from kalshi_arbitrage.reconcile import Reconciler, tracker_from_capture


# --- gateway captures venue fill ids from the order response ---------------- #

def test_kalshi_gateway_captures_fill_ids():
    g = KalshiGateway(client=object())  # _outcome_from_order doesn't touch the client
    req = OrderRequest(venue="kalshi", action="buy", size=100, limit_price=0.40,
                       ticker="KX", outcome_side="yes")
    order = {"count_filled": 100, "average_fill_price": 40,
             "fills": [{"trade_id": "t1"}, {"trade_id": "t2"}]}
    out = g._outcome_from_order(req, order, "ord-1", "coid", {})
    assert out.fill_ids == ["t1", "t2"]
    assert out.order_id == "ord-1" and out.filled_size == 100


# --- executor result + capture row carry the lineage ------------------------ #

class _Outcome:
    def __init__(self, venue, price, fee, size, fill_ids=None, trade_ids=None):
        self.venue, self.avg_price, self.fees, self.filled_size = venue, price, fee, size
        self.order_id = f"{venue}-ord"
        self.fill_ids = fill_ids or []
        self.trade_ids = trade_ids or []


def test_build_result_and_capture_carry_lineage(tmp_path):
    ex = ArbitrageExecutor()
    buy = _Outcome("kalshi", 0.83, 0.5, 100, fill_ids=["kf1"])
    sell = _Outcome("polymarket", 0.0745, 0.1, 100, trade_ids=["pt1"])
    opp = {"strategy_type": "complementary", "max_tradeable_volume": 100,
           "polymarket_token": "tok"}
    res = ex._build_result("opp", opp, buy, sell, 100, 0.0, None)
    assert res.buy_fill_id == "kf1" and res.sell_trade_id == "pt1"
    assert res.buy_order_id == "kalshi-ord" and res.sell_order_id == "polymarket-ord"

    cap = ExecutionCapture(path=str(tmp_path / "ex.jsonl"))
    row = cap.record(opp, res)
    for key in ("buy_order_id", "sell_order_id", "buy_fill_id", "sell_trade_id", "settlement_id"):
        assert key in row
    assert row["buy_fill_id"] == "kf1" and row["sell_trade_id"] == "pt1"


# --- settlement policy: locked-in vs settled -------------------------------- #

def _live_receipt(tracker, settled):
    return tracker.record_execution_receipt(
        opportunity_id="o", buy_platform="kalshi", sell_platform="polymarket",
        buy_price=0.11, buy_size=100, buy_fee=0.1,
        sell_price=0.835, sell_size=100, sell_fee=0.0,
        source="exchange", mark_confirmed=True, mark_settled=settled)


def test_live_fill_is_locked_in_not_settled():
    t = ConfirmedPnLTracker(allow_simulated_confirmed=False)
    _live_receipt(t, settled=False)          # the live policy: not settled at fill
    s = t.summarize(scan_interval_seconds=30)
    assert s["locked_in_execution_count"] == 1
    assert s["locked_in_pnl_usd"] > 0
    assert s["realized_pnl_usd"] == 0 and s["cumulative_settled_execution_count"] == 0


def test_settling_moves_locked_to_realized():
    t = ConfirmedPnLTracker(allow_simulated_confirmed=False)
    eid = _live_receipt(t, settled=False)
    assert t.mark_settled(eid)
    s = t.summarize(scan_interval_seconds=30)
    assert s["locked_in_execution_count"] == 0
    assert s["cumulative_settled_execution_count"] == 1


# --- reconciler: venue truth overwrite + settlement ------------------------- #

class _FakeKalshi:
    async def get_fills(self, order_id=None):
        return [{"trade_id": "AUTH-T", "fee_cents": 7}]   # 7c authoritative fee

    async def is_resolved(self, market):
        return True


class _FakePoly:
    async def get_trades(self, order_id=None):
        return [{"id": "0xabc", "fee": 0.0}]

    async def is_resolved(self, market):
        return True


def test_reconciler_overwrites_fee_and_settles():
    t = ConfirmedPnLTracker(allow_simulated_confirmed=False)
    eid = t.record_execution_receipt(
        opportunity_id="o", buy_platform="kalshi", sell_platform="polymarket",
        buy_price=0.11, buy_size=100, buy_fee=0.99,      # estimated fee, to be overwritten
        sell_price=0.835, sell_size=100, sell_fee=0.0,
        buy_order_id="k-ord", sell_order_id="p-ord", token_id="tok",
        source="exchange", mark_confirmed=True, mark_settled=False)
    execution = t.executions[eid]
    rec = Reconciler(t, kalshi=_FakeKalshi(), poly=_FakePoly())
    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(rec.reconcile_fills(execution))
    assert execution.buy_fills[0].trade_id == "AUTH-T"
    assert abs(execution.buy_fills[0].fee - 0.07) < 1e-9   # 7c, overwritten
    assert loop.run_until_complete(rec.reconcile_settlement(execution))
    assert t.summarize(scan_interval_seconds=30)["cumulative_settled_execution_count"] == 1
    loop.close()


# --- rebuild tracker from the capture ledger -------------------------------- #

def test_tracker_from_capture_loads_only_exchange_fills(tmp_path):
    ledger = tmp_path / "ex.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in [
        {"opportunity_id": "live", "confirmation_source": "exchange", "skipped_reason": None,
         "filled_volume": 100, "buy_platform": "kalshi", "sell_platform": "polymarket",
         "avg_buy_price": 0.11, "avg_sell_price": 0.835, "buy_fees": 0.1, "sell_fees": 0.0,
         "execution_id": "e1", "ts": 1000},
        {"opportunity_id": "paper", "confirmation_source": "paper", "skipped_reason": None,
         "filled_volume": 100, "avg_buy_price": 0.1, "avg_sell_price": 0.8, "ts": 1001},
        {"opportunity_id": "skip", "confirmation_source": "exchange",
         "skipped_reason": "daily_loss_limit", "filled_volume": 0, "ts": 1002},
    ]))
    t = tracker_from_capture(str(ledger))
    assert len(t.executions) == 1   # only the live exchange fill
    s = t.summarize(scan_interval_seconds=30)
    assert s["locked_in_execution_count"] == 1  # loaded confirmed-but-not-settled


def test_kalshi_fee_live_key_is_cents():
    # The LIVE Kalshi /portfolio/fills returns fee in CENTS under "fee" (no
    # "fee_cents" key). Must convert -> dollars; the old code booked it 100x.
    from kalshi_arbitrage.reconcile import _apply_kalshi_fill
    fr = type("FR", (), {"trade_id": None, "fill_id": None, "fee": 9.9, "source": None})()
    assert _apply_kalshi_fill(fr, [{"trade_id": "t", "fee": 7}])
    assert abs(fr.fee - 0.07) < 1e-9   # 7 cents -> $0.07, not $7


def test_capture_to_settlement_roundtrip(tmp_path):
    # Write a live row -> rebuild via tracker_from_capture -> reconcile_settlement
    # must SETTLE using the per-venue ids carried in metadata. This is the path
    # that was silently dead (ids dropped on load, both legs shared one id).
    ledger = tmp_path / "ex.jsonl"
    ledger.write_text(json.dumps({
        "opportunity_id": "o", "confirmation_source": "exchange", "skipped_reason": None,
        "filled_volume": 100, "buy_platform": "kalshi", "sell_platform": "polymarket",
        "avg_buy_price": 0.11, "avg_sell_price": 0.835, "buy_fees": 0.1, "sell_fees": 0.0,
        "execution_id": "e1", "kalshi_ticker": "KXM-27", "polymarket_token": "0xtok", "ts": 1000}))
    t = tracker_from_capture(str(ledger))
    execution = list(t.executions.values())[0]
    assert t.summarize(30)["cumulative_settled_execution_count"] == 0  # locked-in, not settled
    rec = Reconciler(t, kalshi=_FakeKalshi(), poly=_FakePoly())   # both is_resolved -> True
    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(rec.reconcile_settlement(execution))
    assert t.summarize(30)["cumulative_settled_execution_count"] == 1
    loop.close()
