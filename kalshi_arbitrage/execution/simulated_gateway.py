"""Simulated venue gateway — fills orders WITHOUT touching any venue API.

Used in paper mode so the full execution path runs end-to-end (leg building,
pre-flight checks, the ExecutionEngine's idempotency/circuit-breaker/kill-switch,
the partial-fill-aware hedge, capture, PnL) — everything a live run does EXCEPT
the actual order placement. This is the "do all but literally auto trade" path.

The fill model assumes our limit order at the opportunity price fills for the
requested (already depth-capped) size, and charges the REAL venue fee. It is a
read-only object: it never calls place/cancel on a real client.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, List, Optional

from ..config import Config
from ..mock_execution import FeeModel, PolymarketFeeClient
from .order_types import (
    KALSHI,
    POLYMARKET,
    STATUS_FILLED,
    OrderOutcome,
    OrderRequest,
)

logger = logging.getLogger(__name__)


class SimulatedGateway:
    """Drop-in VenueGateway that simulates fills instead of placing orders."""

    def __init__(self, venue: str, fee_client: Optional[PolymarketFeeClient] = None):
        self.venue = venue
        self.fee_client = fee_client or PolymarketFeeClient()
        self.simulated = True  # marker so callers know this never hits an API

    async def close(self) -> None:
        await self.fee_client.close()

    async def place(self, req: OrderRequest) -> OrderOutcome:
        coid = req.ensure_client_order_id()
        size = float(req.size)
        if size <= 0:
            return OrderOutcome.failed(self.venue, req.size, "non_positive_size", coid)

        price = max(0.01, min(0.99, float(req.limit_price)))
        if self.venue == KALSHI:
            fee = FeeModel.kalshi_taker_fee(price, size)
        else:
            bps = await self.fee_client.get_fee_rate_bps(req.token_id) or \
                Config.POLYMARKET_ESTIMATED_FEE_RATE_BPS
            fee = FeeModel.polymarket_taker_fee(price, size, bps)

        return OrderOutcome(
            venue=self.venue,
            status=STATUS_FILLED,
            requested_size=size,
            filled_size=size,
            avg_price=price,
            fees=fee,
            order_id=f"sim-{uuid.uuid4().hex[:12]}",
            client_order_id=coid,
            raw={"simulated": True},
            timestamp=time.time(),
        )

    async def cancel(self, order_id: str) -> bool:
        return True

    async def get_balance(self) -> Optional[float]:
        # Plenty of virtual capital so the balance gate never blocks a sim.
        return float("inf")

    async def get_positions(self) -> List[Dict]:
        return []
