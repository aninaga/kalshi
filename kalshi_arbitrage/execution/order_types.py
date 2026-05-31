"""Venue-agnostic order primitives (Phase B).

These types decouple the execution layer from arbitrage. An ``OrderRequest`` is
"place this order on this venue"; an ``OrderOutcome`` is "here's what actually
happened, with real fills and real fees". Any strategy (arb or otherwise) can
drive the execution engine with these, so the platform is reusable for general
Kalshi/Polymarket order flow.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Venues
KALSHI = "kalshi"
POLYMARKET = "polymarket"

# Terminal/working statuses (venue-agnostic).
STATUS_FILLED = "filled"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_HALTED = "halted"


@dataclass
class OrderRequest:
    """A single venue-agnostic order to place."""

    venue: str                       # KALSHI | POLYMARKET
    action: str                      # "buy" | "sell"
    size: float                      # contracts (Kalshi) or shares/USDC (PM)
    limit_price: float               # probability price 0..1
    # Kalshi addressing:
    ticker: Optional[str] = None
    outcome_side: str = "yes"        # "yes" | "no"
    # Polymarket addressing:
    token_id: Optional[str] = None
    # Time-in-force: "IOC" | "FOK" | "GTC" | "GTD"
    tif: str = "IOC"
    ttl_seconds: int = 0
    client_order_id: Optional[str] = None
    # Free-form context for logging / lineage (e.g. opportunity_id, leg).
    meta: Dict = field(default_factory=dict)

    def ensure_client_order_id(self, *parts: str) -> str:
        """Deterministically derive a client order id if one isn't set.

        Deterministic so a retry reuses the SAME id and the venue de-dupes
        rather than creating a duplicate order.
        """
        if not self.client_order_id:
            seed = "|".join(
                str(p) for p in (
                    self.venue, self.ticker, self.token_id, self.outcome_side,
                    self.action, f"{self.size:.6f}", f"{self.limit_price:.6f}", *parts,
                )
            )
            digest = hashlib.sha256(seed.encode()).hexdigest()[:24]
            self.client_order_id = f"coid-{digest}"
        return self.client_order_id


@dataclass
class OrderOutcome:
    """The realized result of an ``OrderRequest`` — real fills, real fees."""

    venue: str
    status: str                      # STATUS_*
    requested_size: float
    filled_size: float = 0.0
    avg_price: float = 0.0           # actual avg fill price (not the request price)
    fees: float = 0.0                # real fees on the filled portion
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    fill_ids: List[str] = field(default_factory=list)
    attempts: int = 0
    error: Optional[str] = None
    raw: Optional[Dict] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def is_done(self) -> bool:
        return self.status in (STATUS_FILLED, STATUS_PARTIAL)

    @property
    def unfilled_size(self) -> float:
        return max(0.0, self.requested_size - self.filled_size)

    @staticmethod
    def failed(venue: str, requested_size: float, error: str,
               client_order_id: Optional[str] = None, raw: Optional[Dict] = None) -> "OrderOutcome":
        return OrderOutcome(
            venue=venue, status=STATUS_FAILED, requested_size=requested_size,
            error=error, client_order_id=client_order_id, raw=raw,
        )

    @staticmethod
    def halted(venue: str, requested_size: float) -> "OrderOutcome":
        return OrderOutcome(
            venue=venue, status=STATUS_HALTED, requested_size=requested_size,
            error="kill_switch_active",
        )
