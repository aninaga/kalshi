"""General-purpose single-leg execution engine (Phase B).

This is the reusable core of the execution platform — it is **arb-agnostic**.
Given an ``OrderRequest`` and a venue gateway it:

  * checks the global kill switch,
  * routes the placement through a per-venue circuit breaker,
  * retries transient failures with backoff, reusing a stable
    ``client_order_id`` so a retry never creates a duplicate order,
  * returns a realized ``OrderOutcome``.

Any strategy (arbitrage, market-making, manual order flow) can drive it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from ..circuit_breaker import (
    CircuitBreakerConfig,
    CircuitOpenError,
    circuit_breaker_manager,
)
from ..config import Config
from .kill_switch import KillSwitch
from .order_types import STATUS_FAILED, OrderOutcome, OrderRequest
from .venue_gateway import VenueGateway

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Places a single order safely (kill switch + circuit breaker + retries)."""

    def __init__(
        self,
        gateways: Dict[str, VenueGateway],
        kill_switch: Optional[KillSwitch] = None,
    ):
        self.gateways = gateways
        self.kill_switch = kill_switch or KillSwitch.instance()
        self._cb_config = CircuitBreakerConfig(
            failure_threshold=Config.EXECUTION_CB_FAILURE_THRESHOLD,
            recovery_timeout=Config.EXECUTION_CB_RECOVERY_TIMEOUT,
        )

    def _breaker(self, venue: str):
        return circuit_breaker_manager.get_or_create(f"exec:{venue}", self._cb_config)

    async def execute(self, req: OrderRequest, stable_key: str = "") -> OrderOutcome:
        """Execute one order. ``stable_key`` (e.g. opportunity_id+leg) feeds the
        idempotency id so retries of the same logical leg de-dupe."""
        active, reason = self.kill_switch.is_active()
        if active:
            logger.warning("Order blocked by kill switch (%s): %s", reason, req.meta)
            return OrderOutcome.halted(req.venue, req.size)

        gateway = self.gateways.get(req.venue)
        if gateway is None:
            return OrderOutcome.failed(req.venue, req.size, "no_gateway")

        # Fix the client order id ONCE so every retry reuses it.
        req.ensure_client_order_id(stable_key)
        breaker = self._breaker(req.venue)

        last: Optional[OrderOutcome] = None
        for attempt in range(Config.EXECUTION_MAX_RETRIES + 1):
            req.meta["attempt"] = attempt
            try:
                outcome = await breaker.call(gateway.place, req)
            except CircuitOpenError:
                logger.error("Circuit breaker OPEN for %s — halting placement", req.venue)
                self.kill_switch.trip(f"circuit_open:{req.venue}")
                return OrderOutcome.halted(req.venue, req.size)
            except Exception as exc:  # gateway raised — counts as a breaker failure
                logger.error("Gateway %s raised: %s", req.venue, exc)
                last = OrderOutcome.failed(req.venue, req.size, str(exc),
                                           req.client_order_id)
                outcome = last

            outcome.attempts = attempt + 1
            if outcome.is_done:
                return outcome
            last = outcome

            # Transient failure — back off and retry with the SAME order id.
            if attempt < Config.EXECUTION_MAX_RETRIES:
                delay = Config.EXECUTION_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Order attempt %d/%d failed (%s) — retrying in %.1fs",
                    attempt + 1, Config.EXECUTION_MAX_RETRIES + 1,
                    outcome.error, delay,
                )
                await asyncio.sleep(delay)

        return last or OrderOutcome.failed(req.venue, req.size, "exhausted_retries",
                                           req.client_order_id)

    async def cancel(self, venue: str, order_id: str) -> bool:
        gateway = self.gateways.get(venue)
        return await gateway.cancel(order_id) if gateway else False
