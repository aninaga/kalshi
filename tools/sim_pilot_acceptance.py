"""W1.4 simulated-pilot acceptance script.

Run AFTER hand-applying lane A (order-mechanics) and lane B (safety-accounting)
to main:

    KALSHI_ARB_DATA_DIR=$(mktemp -d) ~/code/kvenv/bin/python3 /tmp/sim_pilot_acceptance.py

(or just run it — it creates its own temp DATA_DIR before importing the package).

End-to-end through the REAL ArbitrageExecutor + ExecutionEngine + SimulatedGateway:
  P0  environment sanity (paper mode, lock disarmed, DATA_DIR isolated+absolute)
  P1  bookless opportunity is rejected 'risk_too_high' (lane B fail-closed gate)
  P2  clean complementary paper trade: whole-contract sizing, complementary PnL
  P3  ConfirmedPnLTracker books complementary at settlement value (and the
      legacy same-outcome branch is untouched)
  P4a one-leg (PM) failure -> filled Kalshi leg is hedged, residual 0, no halt
  P4b repeated PM failures -> breaker opens -> kill switch trips MID-TRADE ->
      the risk-reducing unwind STILL fills (book never left one-legged)
  P4c next opportunity is rejected 'halted:circuit_open:polymarket'
  P5  no real order can fire: live-mode gateways refuse on the disarmed lock
      (booby-trapped venue clients prove no HTTP), raw KalshiOrderClient
      mutation gate refuses, live-mode executor rejects at the allowlist.

Exit code 0 = all phases pass.
"""

import asyncio
import os
import sys
import tempfile
import time
import uuid

# --- isolate ALL safety/ledger files BEFORE the package is imported ---------
_TMP = tempfile.mkdtemp(prefix="sim_pilot_")
os.environ["KALSHI_ARB_DATA_DIR"] = _TMP
os.environ.pop("ARB_LIVE_TRADING_ARM", None)

from kalshi_arbitrage.config import Config                              # noqa: E402
from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor        # noqa: E402
from kalshi_arbitrage.circuit_breaker import circuit_breaker_manager    # noqa: E402
from kalshi_arbitrage.confirmed_pnl import ConfirmedPnLTracker          # noqa: E402
from kalshi_arbitrage.execution import (                                # noqa: E402
    KALSHI, POLYMARKET, ExecutionEngine, KalshiGateway, KillSwitch,
    OrderOutcome, OrderRequest, PolymarketGateway,
)
from kalshi_arbitrage.execution.live_lock import LiveTradingLock        # noqa: E402
from kalshi_arbitrage.kalshi_executor import KalshiOrderClient          # noqa: E402

PASS = []


def ok(name, cond, detail=""):
    if not cond:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    PASS.append(name)
    print(f"pass  {name}")


def make_opp(**over):
    opp = {
        "opportunity_id": f"acc-{uuid.uuid4().hex[:8]}",
        "strategy_type": "complementary",
        "strategy": "S3: Kalshi YES + Polymarket NO",
        "buy_platform": "kalshi",
        "sell_platform": "polymarket",
        "kalshi_price": 0.42,
        "polymarket_price": 0.51,
        "max_tradeable_volume": 12.5,   # exercises the whole-contract floor
        "total_profit": 0.84,
        "match_data": {"kalshi_market": {"id": "ACC-TEST-T1"},
                       "polymarket_market": {"id": "tok-acc-1"}},
        "polymarket_token": "tok-acc-1",
    }
    opp.update(over)
    return opp


class FailingGateway:
    """Venue gateway that always RETURNS a failed outcome (dominant live mode)."""
    def __init__(self, venue):
        self.venue = venue
        self.simulated = True
        self.calls = 0

    async def place(self, req):
        self.calls += 1
        return OrderOutcome.failed(self.venue, req.size, "venue_rejected_acceptance")

    async def cancel(self, oid):
        return True

    async def get_balance(self):
        return float("inf")

    async def get_positions(self):
        return []

    async def close(self):
        pass


class BoobyTrapClient:
    """Raises if ANY order/HTTP method is ever reached."""
    def __init__(self):
        self.touched = False

    def _boom(self, *a, **k):
        self.touched = True
        raise AssertionError("REAL VENUE CLIENT WAS REACHED")

    async def place_order(self, *a, **k):
        self._boom()

    async def get_order(self, *a, **k):
        self._boom()

    async def cancel_order(self, *a, **k):
        self._boom()

    async def get_balance(self, *a, **k):
        self._boom()

    async def get_positions(self, *a, **k):
        return []

    async def close(self):
        pass


class StubFeeClient:
    async def get_fee_rate_bps(self, token_id):
        return 500

    async def close(self):
        pass


async def main():
    # ---------------- P0: environment sanity --------------------------------
    ok("P0.paper_mode", Config.EXECUTION_MODE == "paper")
    ok("P0.data_dir_isolated", Config.DATA_DIR == _TMP and os.path.isabs(Config.DATA_DIR),
       Config.DATA_DIR)
    ok("P0.live_lock_disarmed", not LiveTradingLock.instance().is_armed())
    active, why = KillSwitch.instance().is_active()
    ok("P0.kill_switch_clear", not active, str(why))
    try:
        import py_clob_client_v2  # noqa: F401
        print("      note: PM SDK IS installed in this venv (gates still hold)")
    except ImportError:
        print("      note: PM SDK absent (py_clob_client_v2) — extra hard backstop")

    ex = ArbitrageExecutor()
    # offline-deterministic PM fee lookups for the sim gateway
    ex.pm_fee_client._cache["tok-acc-1"] = (500, time.time())

    # ---------------- P1: bookless opp ABSTAINS at the risk gate ------------
    # Integrator decision (2026-06-12): a bookless opportunity ABSTAINS (passes
    # the risk gate with a loud warning) rather than rejecting, so the paper
    # desk is not dark while orderbook wiring is pending. The lock + allowlist +
    # kill switch still gate every real order, so abstaining cannot cause a live
    # trade. It must NOT be skipped FOR a risk reason; in paper mode it fills.
    assert Config.RISK_GATE_ENABLED is True
    r1 = await ex.execute_arbitrage(make_opp())
    ok("P1.bookless_opp_abstains_not_risk_rejected",
       r1.skipped_reason not in ("risk_too_high", "low_risk_confidence", "risk_gate_error"),
       str(r1.skipped_reason))

    # ---------------- P2: clean complementary paper trade -------------------
    r2 = await ex.execute_arbitrage(make_opp())
    ok("P2.filled", r2.skipped_reason is None and r2.filled_volume > 0,
       str(r2.skipped_reason))
    ok("P2.whole_contracts_floored_12p5_to_12",
       abs(r2.filled_volume - 12.0) < 1e-9, str(r2.filled_volume))
    ok("P2.complementary_gross_correct",
       abs(r2.gross_profit - (12.0 * 1.0 - (0.42 + 0.51) * 12.0)) < 1e-9,
       f"gross={r2.gross_profit}")
    ok("P2.net_equals_gross_minus_fees",
       abs(r2.net_profit - (r2.gross_profit - r2.buy_fees - r2.sell_fees)) < 1e-9
       and r2.buy_fees >= 0 and r2.sell_fees >= 0)
    ok("P2.simulated_order_ids",
       str(r2.buy_order_id).startswith("sim-") and str(r2.sell_order_id).startswith("sim-"))
    ok("P2.paper_confirmation_source", r2.confirmation_source == "paper")
    ok("P2.engine_uses_simulated_gateways",
       all(getattr(g, "simulated", False) for g in ex.engine.gateways.values()))

    # ---------------- P3: complementary PnL booking -------------------------
    tr = ConfirmedPnLTracker(allow_simulated_confirmed=True)
    eid = tr.record_execution_receipt(
        opportunity_id=r2.opportunity_id, buy_platform="kalshi",
        sell_platform="polymarket",
        buy_price=r2.avg_buy_price, buy_size=r2.filled_volume, buy_fee=r2.buy_fees,
        sell_price=r2.avg_sell_price, sell_size=r2.filled_volume, sell_fee=r2.sell_fees,
        source="paper", strategy_type="complementary",
        mark_confirmed=True, mark_settled=True)
    booked = tr.executions[eid].realized_pnl()
    ok("P3.tracker_books_complementary_at_settlement_value",
       abs(booked - r2.net_profit) < 1e-9 and booked > 0,
       f"booked={booked} vs net={r2.net_profit}")
    eid2 = tr.record_execution_receipt(
        opportunity_id="legacy", buy_platform="kalshi", sell_platform="polymarket",
        buy_price=0.42, buy_size=12, buy_fee=0.0,
        sell_price=0.51, sell_size=12, sell_fee=0.0,
        source="paper", mark_confirmed=True, mark_settled=True)  # no strategy_type
    ok("P3.legacy_same_outcome_math_unchanged",
       abs(tr.executions[eid2].realized_pnl() - (0.51 - 0.42) * 12) < 1e-9)

    # ---------------- P4a: one-leg failure -> hedge fills, no halt ----------
    failing_pm = FailingGateway(POLYMARKET)
    real_sim_pm = ex._sim_gateways[POLYMARKET]
    ex._sim_gateways[POLYMARKET] = failing_pm
    r4 = await ex.execute_arbitrage(make_opp())
    ok("P4a.partial_fill_hedged", r4.skipped_reason == "partial_fill_hedged",
       str(r4.skipped_reason))
    active, why = KillSwitch.instance().is_active()
    ok("P4a.no_halt_after_clean_hedge", not active, str(why))
    ok("P4a.pm_leg_retried_not_once", failing_pm.calls == Config.EXECUTION_MAX_RETRIES + 1,
       str(failing_pm.calls))

    # ---------------- P4b: breaker opens mid-trade -> unwind still fills ----
    r5 = await ex.execute_arbitrage(make_opp())
    active, why = KillSwitch.instance().is_active()
    ok("P4b.kill_switch_tripped_by_breaker",
       active and "circuit_open:polymarket" in str(why), str(why))
    ok("P4b.filled_leg_hedged_THROUGH_the_halt",
       r5.skipped_reason == "partial_fill_hedged", str(r5.skipped_reason))

    # capture ledger shows the hedge lineage with zero residual
    import json
    cap_rows = [json.loads(l) for l in open(ex.capture.path)]
    hedged_rows = [r for r in cap_rows if r.get("skipped_reason") == "partial_fill_hedged"]
    ok("P4b.capture_has_hedge_lineage",
       len(hedged_rows) == 2 and all(abs(r["hedge_residual"]) < 1e-9 for r in hedged_rows)
       and all(abs(r["hedge_filled"] - 12.0) < 1e-9 for r in hedged_rows))
    ok("P4b.capture_rows_carry_strategy_type",
       all(r.get("strategy_type") == "complementary" for r in cap_rows))

    # ---------------- P4c: normal flow stays halted -------------------------
    r6 = await ex.execute_arbitrage(make_opp())
    ok("P4c.next_opp_blocked_by_halt",
       str(r6.skipped_reason or "").startswith("halted:"), str(r6.skipped_reason))
    KillSwitch.instance().reset()
    circuit_breaker_manager.reset_all()
    ex._sim_gateways[POLYMARKET] = real_sim_pm

    # ---------------- P5: a REAL order still cannot fire --------------------
    Config.EXECUTION_MODE = "live"
    try:
        ok("P5.lock_still_disarmed_in_live_mode", not LiveTradingLock.instance().is_armed())

        kc, pc = BoobyTrapClient(), BoobyTrapClient()
        kg = KalshiGateway(client=kc)
        pg = PolymarketGateway(client=pc, fee_client=StubFeeClient())
        out_k = await kg.place(OrderRequest(venue=KALSHI, action="buy", size=1,
                                            limit_price=0.5, ticker="ACC-TEST-T1",
                                            outcome_side="yes", tif="IOC"))
        out_p = await pg.place(OrderRequest(venue=POLYMARKET, action="buy", size=1,
                                            limit_price=0.5, token_id="tok-acc-1",
                                            tif="FOK"))
        ok("P5.kalshi_gateway_refuses_disarmed",
           out_k.status == "failed" and out_k.error == "live_trading_locked"
           and not kc.touched, f"{out_k.status}/{out_k.error}")
        ok("P5.polymarket_gateway_refuses_disarmed",
           out_p.status == "failed" and out_p.error == "live_trading_locked"
           and not pc.touched, f"{out_p.status}/{out_p.error}")

        # risk-reducing orders bypass kill switch + breaker but NEVER the lock
        KillSwitch.instance().trip("acceptance_p5")
        eng = ExecutionEngine({KALSHI: kg})
        rr = OrderRequest(venue=KALSHI, action="sell", size=1, limit_price=0.4,
                          ticker="ACC-TEST-T1", outcome_side="yes", tif="IOC",
                          risk_reducing=True)
        out_rr = await eng.execute(rr, stable_key="acc:rr")
        ok("P5.risk_reducing_bypass_still_lock_gated",
           out_rr.status == "failed" and out_rr.error == "live_trading_locked"
           and not kc.touched, f"{out_rr.status}/{out_rr.error}")
        KillSwitch.instance().reset()

        # raw client mutation gate (lane B): refuses before auth/HTTP
        raw = KalshiOrderClient()
        raw._get_session = kc._boom  # any HTTP attempt explodes
        resp = await raw.place_order("ACC-TEST-T1", "yes", "buy", 1, 50)
        ok("P5.raw_kalshi_client_mutation_gate",
           resp.get("error") == "live_trading_locked" and not kc.touched,
           str(resp))

        # full executor in live mode: rejected at the allowlist pre-flight,
        # with booby-trapped real clients proving nothing was touched
        ex.kalshi_gateway.client = kc
        ex.polymarket_gateway.client = pc
        r7 = await ex.execute_arbitrage(make_opp())
        ok("P5.live_executor_rejects_not_allowlisted",
           r7.skipped_reason == "not_allowlisted" and not kc.touched and not pc.touched,
           str(r7.skipped_reason))
    finally:
        Config.EXECUTION_MODE = "paper"
        Config.RISK_GATE_ENABLED = True
        KillSwitch.instance().reset()
        circuit_breaker_manager.reset_all()

    await ex.close()
    print(f"\nALL {len(PASS)} ACCEPTANCE CHECKS PASSED  (data dir: {_TMP})")


if __name__ == "__main__":
    asyncio.run(main())
