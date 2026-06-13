"""Operator controls for the live pilot (Phase D).

Out-of-band controls a human can invoke to stop and flatten the book:

  * ``halt`` / ``resume``  — trip / clear the durable kill switch.
  * ``flatten_all``        — cancel every open order and market-unwind every open
                             position on both venues.
  * ``position_health``    — report net exposure per venue and alert on anything
                             that looks unhedged.

These operate through the venue gateways, so they work for any order flow, not
just arbitrage.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ..config import Config
from .kill_switch import KillSwitch
from .order_types import KALSHI, POLYMARKET, OrderRequest
from .single_leg import ExecutionEngine
from .venue_gateway import KalshiGateway, PolymarketGateway, VenueGateway

logger = logging.getLogger(__name__)


class OperatorControls:
    def __init__(
        self,
        gateways: Optional[Dict[str, VenueGateway]] = None,
        kill_switch: Optional[KillSwitch] = None,
        alert_manager=None,
        engine: Optional[ExecutionEngine] = None,
    ):
        if gateways is None:
            gateways = {KALSHI: KalshiGateway(), POLYMARKET: PolymarketGateway()}
        self.gateways = gateways
        self.kill_switch = kill_switch or KillSwitch.instance()
        self.alert_manager = alert_manager
        # Flatten orders route through the ExecutionEngine's RISK-REDUCING
        # path: they pass a tripped kill switch and an open circuit breaker
        # (a flatten strictly reduces exposure, never opens it) while keeping
        # the engine's retries and lineage.
        self.engine = engine or ExecutionEngine(dict(self.gateways),
                                                kill_switch=self.kill_switch)

    # --- Halt / resume ----------------------------------------------------- #

    def halt(self, reason: str = "operator_halt") -> None:
        self.kill_switch.trip(reason)

    def resume(self) -> None:
        self.kill_switch.reset()

    # --- Flatten ----------------------------------------------------------- #

    async def flatten_all(self) -> Dict[str, List[Dict]]:
        """Cancel all orders and market-unwind all positions on every venue.

        Halts first so no new orders race the flatten. Returns a per-venue log
        of the unwind orders attempted.
        """
        self.halt("flatten_all")
        report: Dict[str, List[Dict]] = {}
        for venue, gateway in self.gateways.items():
            entries: List[Dict] = []
            # Cancel resting orders best-effort.
            cancel_all = getattr(getattr(gateway, "client", None), "cancel_all", None)
            if cancel_all is not None:
                try:
                    await cancel_all()
                except Exception as exc:
                    logger.error("cancel_all failed on %s: %s", venue, exc)

            try:
                positions = await gateway.get_positions()
            except Exception as exc:
                logger.error("get_positions failed on %s: %s", venue, exc)
                positions = []

            for pos in positions:
                req = self._unwind_request(venue, pos)
                if req is None:
                    continue
                # Flatten orders are RISK-REDUCING: the engine lets them
                # through the (deliberately) tripped kill switch and any open
                # breaker, while still providing retries.
                try:
                    outcome = await self.engine.execute(
                        req,
                        stable_key=f"flatten:{venue}:{req.ticker or req.token_id}",
                    )
                    entries.append({
                        "position": pos, "unwind_filled": outcome.filled_size,
                        "status": outcome.status,
                    })
                except Exception as exc:
                    logger.error("flatten place failed on %s: %s", venue, exc)
                    entries.append({"position": pos, "error": str(exc)})
            report[venue] = entries
        return report

    @staticmethod
    def _unwind_request(venue: str, pos: Dict) -> Optional[OrderRequest]:
        """Build a market-ish unwind order for an open position (best-effort).

        Position shapes differ by venue; read defensively. A positive net
        quantity is sold, a negative net quantity is bought back.
        """
        if venue == KALSHI:
            ticker = pos.get("ticker") or pos.get("market_ticker")
            qty = pos.get("position") or pos.get("net_position") or 0
            if not ticker or not qty:
                return None
            action = "sell" if qty > 0 else "buy"
            price = 0.01 if action == "sell" else 0.99  # cross hard to exit
            return OrderRequest(venue=KALSHI, action=action, size=abs(float(qty)),
                                limit_price=price, ticker=ticker, outcome_side="yes",
                                tif="IOC", risk_reducing=True, meta={"flatten": True})
        token = pos.get("asset") or pos.get("token_id") or pos.get("tokenID")
        qty = pos.get("size") or pos.get("net_size") or 0
        if not token or not qty:
            return None
        action = "sell" if float(qty) > 0 else "buy"
        price = 0.01 if action == "sell" else 0.99
        return OrderRequest(venue=POLYMARKET, action=action, size=abs(float(qty)),
                            limit_price=price, token_id=token, tif="FOK",
                            risk_reducing=True, meta={"flatten": True})

    # --- Health ------------------------------------------------------------ #

    async def position_health(self) -> Dict:
        """Report positions per venue and flag any non-empty exposure."""
        health: Dict = {"venues": {}, "alerts": []}
        for venue, gateway in self.gateways.items():
            try:
                positions = await gateway.get_positions()
            except Exception as exc:
                health["venues"][venue] = {"error": str(exc)}
                continue
            open_positions = [p for p in positions if (p.get("position") or p.get("size") or 0)]
            health["venues"][venue] = {"open_positions": len(open_positions)}
            if open_positions:
                msg = f"{venue} has {len(open_positions)} open position(s)"
                health["alerts"].append(msg)
                if self.alert_manager is not None:
                    try:
                        await self.alert_manager.create_alert(message=msg)
                    except Exception:
                        pass
        return health
