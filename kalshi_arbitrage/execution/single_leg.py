"""General-purpose single-leg execution engine (Phase B).

This is the reusable core of the execution platform — it is **arb-agnostic**.
Given an ``OrderRequest`` and a venue gateway it:

  * checks the global kill switch (risk-REDUCING orders — hedge unwinds,
    operator flatten — are allowed through a tripped switch: a halt must not
    block its own unwind),
  * routes the placement through a per-venue circuit breaker; a gateway that
    RETURNS a failed outcome counts as a breaker failure just like a raised
    exception (returning failures is the dominant venue failure mode),
  * retries transient failures with backoff, reusing a stable
    ``client_order_id``. NOTE: only KALSHI de-dupes server-side on that id;
    Polymarket never receives it (the V2 SDK salts every order), so the
    Polymarket gateway marks ambiguous failures non-retryable and the engine
    honors that — it never blind-retries them,
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


class _VenueReturnedFailure(Exception):
    """Internal: wraps a RETURNED failed OrderOutcome.

    Gateways report venue failures by returning ``OrderOutcome.failed`` (they
    do not raise), but the circuit breaker only counts raised exceptions.
    Raising this inside ``breaker.call`` makes a returned failure increment
    the breaker instead of resetting it; the engine unwraps it immediately.
    """

    def __init__(self, outcome: OrderOutcome):
        super().__init__(outcome.error or "venue_failure")
        self.outcome = outcome


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
        idempotency id so retries of the same logical leg reuse one id (server
        de-dup on KALSHI only — Polymarket never receives it)."""
        active, reason = self.kill_switch.is_active()
        if active:
            if not req.risk_reducing:
                logger.warning("Order blocked by kill switch (%s): %s", reason, req.meta)
                return OrderOutcome.halted(req.venue, req.size)
            # A risk-REDUCING order (hedge unwind / flatten) strictly shrinks
            # exposure — blocking it would lock in the very risk the halt is
            # protecting against. Let it through, loudly.
            logger.warning(
                "Kill switch active (%s) but order is RISK-REDUCING "
                "(flatten/unwind) — allowing through: %s", reason, req.meta)

        gateway = self.gateways.get(req.venue)
        if gateway is None:
            return OrderOutcome.failed(req.venue, req.size, "no_gateway")

        # Fix the client order id ONCE so every retry reuses it.
        req.ensure_client_order_id(stable_key)
        breaker = self._breaker(req.venue)

        async def _place_counted() -> OrderOutcome:
            out = await gateway.place(req)
            if out.status == STATUS_FAILED:
                # Venues RETURN failures; raise inside the breaker so it counts
                # as a failure (not a success that resets the counter).
                raise _VenueReturnedFailure(out)
            return out

        last: Optional[OrderOutcome] = None
        for attempt in range(Config.EXECUTION_MAX_RETRIES + 1):
            req.meta["attempt"] = attempt
            try:
                if req.risk_reducing:
                    # Exposure-reducing orders must not be blocked by an OPEN
                    # breaker either — call the gateway directly.
                    outcome = await gateway.place(req)
                else:
                    outcome = await breaker.call(_place_counted)
            except _VenueReturnedFailure as vf:
                outcome = vf.outcome
                last = outcome
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

            if not getattr(outcome, "retryable", True):
                # Ambiguous failure (e.g. timeout after a Polymarket POST — no
                # server dedup there): a retry could double-order. Fail CLOSED.
                logger.error("Non-retryable failure on %s (%s) — NOT retrying",
                             req.venue, outcome.error)
                return outcome

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
