"""General-purpose execution layer (Phase B).

Decouples order execution from arbitrage so it is reusable for any
Kalshi/Polymarket order flow. Components:

  * ``order_types``   — venue-agnostic OrderRequest / OrderOutcome
  * ``venue_gateway`` — uniform Kalshi / Polymarket gateways
  * ``single_leg``    — ExecutionEngine (kill switch + circuit breaker + retries)
  * ``kill_switch``   — global halt
"""

from .capture import ExecutionCapture
from .kill_switch import KillSwitch
from .live_lock import LiveTradingLock
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
from .simulated_gateway import SimulatedGateway
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
    "LiveTradingLock",
    "ExecutionCapture",
    "ExecutionEngine",
    "KalshiGateway",
    "PolymarketGateway",
    "SimulatedGateway",
    "VenueGateway",
    "gateway_for",
]
