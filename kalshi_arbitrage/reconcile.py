"""Post-trade reconciliation → authoritative, venue-confirmed realized P&L.

A live cross-venue complementary arb is LOCKED at fill but only REALIZED when
both markets resolve (months out). The ledger therefore records live fills as
confirmed-but-not-settled; this module:

  1. Rebuilds a ``ConfirmedPnLTracker`` from the capture ledger (``executions.jsonl``).
  2. ``reconcile_fills`` — re-fetches the AUTHORITATIVE fill (trade_id, price, fee)
     from each venue by ``order_id`` and overwrites the order-response estimates.
  3. ``reconcile_settlement`` — when BOTH venues show the market resolved, marks
     the receipt settled, moving it from the "locked-in" bucket to "realized".

Venue fetches are best-effort and duck-typed (clients injected), so the
orchestration is unit-tested with fakes; the live clients need creds. Run as
``kalshi-arb reconcile`` (periodically — markets resolve months out).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .confirmed_pnl import ConfirmedPnLTracker

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Rebuild the tracker from the append-only capture ledger                     #
# --------------------------------------------------------------------------- #

def tracker_from_capture(path: str, allow_simulated: bool = False) -> ConfirmedPnLTracker:
    """Replay live exchange-confirmed fills from the capture JSONL into a fresh
    tracker as confirmed-but-not-settled receipts (so reconcile can settle them).
    Skipped rows and non-exchange (paper/sim) rows are ignored for live recon."""
    tracker = ConfirmedPnLTracker(allow_simulated_confirmed=allow_simulated)
    p = Path(path)
    if not p.exists():
        return tracker
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("skipped_reason") or r.get("confirmation_source") != "exchange":
            continue
        vol = float(r.get("filled_volume") or 0)
        if vol <= 0:
            continue
        tracker.record_execution_receipt(
            opportunity_id=r.get("opportunity_id", "unknown"),
            buy_platform=r.get("buy_platform", ""),
            sell_platform=r.get("sell_platform", ""),
            buy_price=float(r.get("avg_buy_price") or 0), buy_size=vol,
            buy_fee=float(r.get("buy_fees") or 0),
            sell_price=float(r.get("avg_sell_price") or 0), sell_size=vol,
            sell_fee=float(r.get("sell_fees") or 0),
            execution_id=r.get("execution_id"),
            buy_order_id=r.get("buy_order_id"), sell_order_id=r.get("sell_order_id"),
            buy_fill_id=r.get("buy_fill_id"), sell_fill_id=r.get("sell_fill_id"),
            buy_trade_id=r.get("buy_trade_id"), sell_trade_id=r.get("sell_trade_id"),
            settlement_id=r.get("settlement_id"),
            source="exchange",
            # Stash per-venue resolution ids so reconcile_settlement can check
            # each venue separately (the tracker's single market_id/token_id
            # can't carry both the Kalshi ticker and the Polymarket token).
            metadata={"kalshi_ticker": r.get("kalshi_ticker"),
                      "polymarket_token": r.get("polymarket_token")},
            mark_confirmed=True,
            mark_settled=False,          # awaiting resolution; reconcile settles
            timestamp=float(r.get("ts") or time.time()),
        )
    return tracker


# --------------------------------------------------------------------------- #
#  Reconciler                                                                  #
# --------------------------------------------------------------------------- #

class Reconciler:
    """Fetch authoritative venue records and confirm/settle the tracker.

    ``kalshi`` must expose ``async get_fills(order_id=...) -> List[Dict]`` and
    (for settlement) ``async get_settlements()`` / ``get_positions()``;
    ``poly`` ``async get_trades(...) -> List[Dict]``. Either may be None (that
    leg is skipped). All venue calls are wrapped — a fetch error never aborts the
    run; it just leaves that leg unreconciled.
    """

    def __init__(self, tracker: ConfirmedPnLTracker, kalshi=None, poly=None):
        self.tracker = tracker
        self.kalshi = kalshi
        self.poly = poly

    async def _safe(self, coro):
        try:
            return await coro
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("reconcile venue fetch failed: %s", exc)
            return None

    async def reconcile_fills(self, execution) -> bool:
        """Overwrite each leg's fill with the venue-authoritative trade_id/fee."""
        updated = False
        for leg, fills in (("buy", execution.buy_fills), ("sell", execution.sell_fills)):
            for fr in fills:
                if fr.platform == "kalshi" and self.kalshi and fr.order_id:
                    venue = await self._safe(self.kalshi.get_fills(order_id=fr.order_id))
                    updated |= _apply_kalshi_fill(fr, venue)
                elif fr.platform == "polymarket" and self.poly and fr.order_id:
                    venue = await self._safe(self.poly.get_trades(order_id=fr.order_id))
                    updated |= _apply_poly_trade(fr, venue)
        return updated

    async def reconcile_settlement(self, execution) -> bool:
        """Mark the receipt settled once BOTH venues show resolution."""
        if execution.is_settled() or not execution.has_confirmed_legs():
            return False
        k_done = await self._venue_resolved("kalshi", execution)
        p_done = await self._venue_resolved("polymarket", execution)
        if k_done and p_done:
            return self.tracker.mark_settled(execution.execution_id, settled_at=time.time())
        return False

    async def _venue_resolved(self, platform: str, execution) -> bool:
        client = self.kalshi if platform == "kalshi" else self.poly
        checker = getattr(client, "is_resolved", None)
        if client is None or checker is None:
            return False
        # Per-venue id: Kalshi ticker vs Polymarket token. Prefer the metadata
        # ids (populated from the capture row); fall back to the fill's own
        # market_id/token_id for in-memory receipts.
        meta = getattr(execution, "metadata", {}) or {}
        mkt = meta.get("kalshi_ticker") if platform == "kalshi" else meta.get("polymarket_token")
        if not mkt:
            fills = execution.buy_fills + execution.sell_fills
            mkt = next((f.market_id or f.token_id for f in fills if f.platform == platform), None)
        return bool(await self._safe(checker(mkt))) if mkt else False

    async def reconcile_all(self, mode: str = "all") -> Dict[str, Any]:
        fills_updated = settled = 0
        for execution in list(self.tracker.executions.values()):
            if mode in ("fills", "all"):
                if await self.reconcile_fills(execution):
                    fills_updated += 1
            if mode in ("settlement", "all"):
                if await self.reconcile_settlement(execution):
                    settled += 1
        return {"fills_updated": fills_updated, "settled": settled}


def _apply_kalshi_fill(fill_record, venue_fills: Optional[List[Dict]]) -> bool:
    if not venue_fills:
        return False
    f = venue_fills[0] if isinstance(venue_fills, list) else venue_fills
    if not isinstance(f, dict):
        return False
    tid = f.get("trade_id") or f.get("fill_id")
    if tid:
        fill_record.trade_id = str(tid)
        fill_record.fill_id = fill_record.fill_id or str(tid)
    # Kalshi /portfolio/fills denominates the fee in CENTS under "fee" (it does
    # NOT send a "fee_cents" key on the live path). Always convert cents->dollars;
    # the prior code passed the live "fee" through un-divided -> 100x overstatement.
    if "fee_cents" in f and f["fee_cents"] is not None:
        fill_record.fee = float(f["fee_cents"]) / 100.0
    elif f.get("fee") is not None:
        fill_record.fee = float(f["fee"]) / 100.0
    fill_record.source = "exchange"
    return True


def _apply_poly_trade(fill_record, venue_trades: Optional[List[Dict]]) -> bool:
    if not venue_trades:
        return False
    t = venue_trades[0] if isinstance(venue_trades, list) else venue_trades
    if not isinstance(t, dict):
        return False
    tid = t.get("id") or t.get("transaction_hash") or t.get("trade_id")
    if tid:
        fill_record.trade_id = str(tid)
    if t.get("fee") is not None:
        fill_record.fee = float(t["fee"])
    fill_record.source = "exchange"
    return True


# --------------------------------------------------------------------------- #
#  CLI                                                                          #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    import argparse
    import asyncio

    from .config import Config

    ap = argparse.ArgumentParser(prog="reconcile", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", default=None,
                    help="capture JSONL (default: the live capture file)")
    ap.add_argument("--mode", choices=["report", "fills", "settlement", "all"],
                    default="report",
                    help="report = load+summarize only (no creds); fills/settlement/all "
                         "fetch venue records (needs live creds)")
    args = ap.parse_args(argv)

    import os
    ledger = args.ledger or os.path.join(Config.DATA_DIR, Config.EXECUTION_CAPTURE_FILE)
    tracker = tracker_from_capture(ledger)
    summary = tracker.summarize(scan_interval_seconds=30)
    print(f"Loaded {len(tracker.executions)} exchange receipts from {ledger}")
    print(f"  locked-in (confirmed, awaiting resolution): "
          f"${summary['locked_in_pnl_usd']:.2f} across {summary['locked_in_execution_count']} pairs")
    print(f"  settled-realized: ${summary['cumulative_realized_pnl_usd']:.2f} "
          f"across {summary['cumulative_settled_execution_count']} pairs")

    if args.mode == "report":
        print("\n(report-only; pass --mode fills|settlement|all with live creds to reconcile)")
        return 0

    try:
        from .execution.venue_gateway import KalshiGateway, PolymarketGateway
        kalshi = KalshiGateway().client
        poly = PolymarketGateway().client
    except Exception as exc:
        print(f"Could not build venue clients (need live creds): {exc}")
        return 1
    rec = Reconciler(tracker, kalshi=kalshi, poly=poly)
    result = asyncio.new_event_loop().run_until_complete(rec.reconcile_all(mode=args.mode))
    after = tracker.summarize(scan_interval_seconds=30)
    print(f"\nReconciled: {result['fills_updated']} fills updated, {result['settled']} newly settled.")
    print(f"  settled-realized now: ${after['cumulative_realized_pnl_usd']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
