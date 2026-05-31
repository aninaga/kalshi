"""General-purpose execution layer (Phase B).

Decouples order execution from arbitrage so it is reusable for any
Kalshi/Polymarket order flow. Components:

  * ``order_types``   — venue-agnostic OrderRequest / OrderOutcome
  * ``venue_gateway`` — uniform Kalshi / Polymarket gateways
  * ``single_leg``    — ExecutionEngine (kill switch + circuit breaker + retries)
  * ``kill_switch``   — global halt
"""

from .kill_switch import KillSwitch
from .order_types import (
    KALSHI,
    POLYMARKET,
    STATUS_FAILED,
    STATUS_FILLED,
    STATUS_HALTED,
    STATUS_PARTIAL,
    OrderOutcome,
    OrderRequest,
)
from .single_leg import ExecutionEngine
from .venue_gateway import (
    KalshiGateway,
    PolymarketGateway,
    VenueGateway,
    gateway_for,
)

__all__ = [
    "KALSHI",
    "POLYMARKET",
    "STATUS_FAILED",
    "STATUS_FILLED",
    "STATUS_HALTED",
    "STATUS_PARTIAL",
    "OrderOutcome",
    "OrderRequest",
    "KillSwitch",
    "ExecutionEngine",
    "KalshiGateway",
    "PolymarketGateway",
    "VenueGateway",
    "gateway_for",
]
