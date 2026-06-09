"""Uniform venue gateways (Phase B).

Wrap the venue-specific order clients (``KalshiOrderClient``,
``PolymarketOrderClient``) behind one interface so the execution engine — and
any non-arb strategy — speaks ``OrderRequest`` / ``OrderOutcome`` and never
touches venue quirks (cents vs probability, FOK market orders vs limit orders,
RSA vs HMAC auth, fee formulas).

Each gateway's ``place()`` is responsible for the full lifecycle of one order:
submit → confirm fills (with backoff polling) → compute *real* fees → return an
``OrderOutcome`` with the actual average fill price.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Protocol

from ..config import Config
from ..kalshi_executor import KalshiOrderClient
from ..mock_execution import FeeModel, PolymarketFeeClient
from ..polymarket_executor import PolymarketOrderClient
from .live_lock import LiveTradingLock
from .order_types import (
    KALSHI,
    POLYMARKET,
    STATUS_FAILED,
    STATUS_FILLED,
    STATUS_PARTIAL,
    OrderOutcome,
    OrderRequest,
)

logger = logging.getLogger(__name__)


class VenueGateway(Protocol):
    venue: str

    async def place(self, req: OrderRequest) -> OrderOutcome: ...
    async def cancel(self, order_id: str) -> bool: ...
    async def get_balance(self) -> Optional[float]: ...
    async def get_positions(self) -> List[Dict]: ...
    async def close(self) -> None: ...


async def _backoff_poll(poll_fn, *, budget: float, base: float, cap: float):
    """Call ``poll_fn`` with exponential backoff until it returns a truthy
    (terminal) result or the time budget is exhausted. Returns the last result.
    """
    deadline = time.monotonic() + budget
    interval = base
    last = None
    while time.monotonic() < deadline:
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        last = await poll_fn()
        if last is not None:
            return last
        interval = min(interval * 2, cap)
    return last


class KalshiGateway:
    venue = KALSHI

    def __init__(self, client: Optional[KalshiOrderClient] = None):
        self.client = client or KalshiOrderClient()

    async def close(self) -> None:
        await self.client.close()

    async def place(self, req: OrderRequest) -> OrderOutcome:
        coid = req.ensure_client_order_id()
        # HARD BACKSTOP: a real Kalshi order only goes out when the live-trading
        # lock is armed (post-validation). Otherwise refuse — never POST.
        armed, reason = LiveTradingLock.instance().assert_or_warn(KALSHI)
        if not armed:
            return OrderOutcome.failed(KALSHI, req.size, reason, coid)
        count = int(req.size)
        if count <= 0:
            return OrderOutcome.failed(KALSHI, req.size, "non_positive_size", coid)

        price_cents = max(1, min(99, int(round(req.limit_price * 100))))
        ttl = req.ttl_seconds or Config.KALSHI_ORDER_TTL_SECONDS

        resp = await self.client.place_order(
            req.ticker, req.outcome_side, req.action, count, price_cents,
            ttl_seconds=ttl, client_order_id=coid,
        )
        if "error" in resp:
            return OrderOutcome.failed(KALSHI, req.size, resp.get("error", "error"), coid, resp)

        order = resp.get("order", {})
        order_id = order.get("order_id", "")
        status = order.get("status", "")

        if status in ("executed", "filled"):
            return self._outcome_from_order(req, order, order_id, coid, resp)

        # Poll to a terminal state with backoff.
        async def _poll():
            r = await self.client.get_order(order_id)
            o = r.get("order", {})
            st = o.get("status", "")
            if st in ("executed", "filled", "canceled", "expired"):
                return o
            return None

        final = await _backoff_poll(
            _poll, budget=Config.FILL_POLL_BUDGET_SECONDS,
            base=Config.FILL_POLL_BASE_INTERVAL, cap=Config.FILL_POLL_MAX_INTERVAL,
        )
        if final is None:
            # Unknown — best-effort cancel and report what we know (zero fill).
            await self.cancel(order_id)
            return OrderOutcome(
                venue=KALSHI, status=STATUS_FAILED, requested_size=req.size,
                order_id=order_id, client_order_id=coid, error="poll_timeout", raw=resp,
            )
        return self._outcome_from_order(req, final, order_id, coid, resp)

    def _outcome_from_order(self, req, order, order_id, coid, resp) -> OrderOutcome:
        filled = float(order.get("count_filled", 0))
        # average_fill_price is in cents.
        avg_cents = order.get("average_fill_price")
        avg_price = (float(avg_cents) / 100.0) if avg_cents is not None else req.limit_price
        fees = FeeModel.kalshi_taker_fee(avg_price, filled) if filled > 0 else 0.0
        status = STATUS_FILLED if filled >= int(req.size) else (STATUS_PARTIAL if filled > 0 else STATUS_FAILED)
        # Best-effort venue fill ids if the order response carries them; the
        # authoritative ids/fees are re-fetched by the reconciler via order_id.
        fill_ids = [str(f.get("trade_id") or f.get("fill_id"))
                    for f in (order.get("fills") or []) if isinstance(f, dict)]
        return OrderOutcome(
            venue=KALSHI, status=status, requested_size=req.size,
            filled_size=filled, avg_price=avg_price, fees=fees,
            order_id=order_id, client_order_id=coid, raw=resp,
            fill_ids=[fid for fid in fill_ids if fid and fid != "None"],
        )

    async def cancel(self, order_id: str) -> bool:
        return await self.client.cancel_order(order_id)

    async def get_balance(self) -> Optional[float]:
        resp = await self.client.get_balance()
        if isinstance(resp, dict) and "balance" in resp:
            # Kalshi returns cents.
            return float(resp["balance"]) / 100.0
        return None

    async def get_positions(self) -> List[Dict]:
        return await self.client.get_positions()


class PolymarketGateway:
    venue = POLYMARKET

    def __init__(self, client: Optional[PolymarketOrderClient] = None,
                 fee_client: Optional[PolymarketFeeClient] = None):
        self.client = client or PolymarketOrderClient()
        self.fee_client = fee_client or PolymarketFeeClient()

    async def close(self) -> None:
        await self.client.close()
        await self.fee_client.close()

    async def place(self, req: OrderRequest) -> OrderOutcome:
        coid = req.ensure_client_order_id()
        # HARD BACKSTOP: a real Polymarket order only goes out when the
        # live-trading lock is armed (post-validation). Otherwise refuse.
        armed, reason = LiveTradingLock.instance().assert_or_warn(POLYMARKET)
        if not armed:
            return OrderOutcome.failed(POLYMARKET, req.size, reason, coid)
        side = "BUY" if req.action == "buy" else "SELL"
        order_type = req.tif if req.tif in ("FOK", "GTC", "GTD") else Config.POLYMARKET_ORDER_TYPE
        ttl = req.ttl_seconds or Config.POLYMARKET_ORDER_TTL_SECONDS

        resp = await self.client.place_order(
            token_id=req.token_id, side=side, price=req.limit_price,
            size=req.size, order_type=order_type, ttl_seconds=ttl,
        )
        if isinstance(resp, dict) and resp.get("error"):
            return OrderOutcome.failed(POLYMARKET, req.size, resp["error"], coid, resp)

        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        if not order_id:
            return OrderOutcome.failed(POLYMARKET, req.size, "no_order_id", coid, resp)

        filled, avg_price = self._parse_fill(resp, req)
        if filled <= 0 and order_type in ("GTC", "GTD"):
            # Resting limit order — poll for a fill.
            async def _poll():
                r = await self.client.get_order(order_id)
                if isinstance(r, dict):
                    st = r.get("status", "")
                    if st in ("matched", "filled", "canceled", "expired"):
                        return r
                return None

            final = await _backoff_poll(
                _poll, budget=Config.FILL_POLL_BUDGET_SECONDS,
                base=Config.FILL_POLL_BASE_INTERVAL, cap=Config.FILL_POLL_MAX_INTERVAL,
            )
            if final is not None:
                filled, avg_price = self._parse_fill(final, req)

        fees = await self._fees(req.token_id, avg_price, filled)
        status = STATUS_FILLED if filled >= req.size * 0.999 else (STATUS_PARTIAL if filled > 0 else STATUS_FAILED)
        # Best-effort on-chain trade/tx ids; the reconciler re-fetches the
        # authoritative trades (and fees) from the data-api via order_id.
        tx = (resp.get("transactionsHashes") or resp.get("transactionHashes")
              or resp.get("tradeIds") or []) if isinstance(resp, dict) else []
        trade_ids = [str(t) for t in tx if t] if isinstance(tx, list) else []
        return OrderOutcome(
            venue=POLYMARKET, status=status, requested_size=req.size,
            filled_size=filled, avg_price=avg_price, fees=fees,
            order_id=order_id, client_order_id=coid, raw=resp, trade_ids=trade_ids,
        )

    @staticmethod
    def _parse_fill(resp: Dict, req: OrderRequest) -> tuple:
        """Extract (filled_size, avg_price) from a CLOB response, best-effort.

        Polymarket returns matched amounts in several shapes across SDK
        versions; prefer explicit matched size, fall back to taking/making
        amounts, and derive avg price from notional when possible.
        """
        if not isinstance(resp, dict):
            return 0.0, req.limit_price
        # Explicit matched size.
        for key in ("size_matched", "sizeMatched", "matched_size"):
            if resp.get(key) is not None:
                try:
                    matched = float(resp[key])
                except (TypeError, ValueError):
                    continue
                price = resp.get("price")
                avg = float(price) if price is not None else req.limit_price
                return matched, avg
        # taking/making amounts (USDC notional / shares).
        taking = resp.get("takingAmount") or resp.get("taking_amount")
        making = resp.get("makingAmount") or resp.get("making_amount")
        if taking is not None and making is not None:
            try:
                taking, making = float(taking), float(making)
                # For a BUY: making=USDC paid, taking=shares received.
                if req.action == "buy" and taking > 0:
                    avg = making / taking if taking else req.limit_price
                    return taking, avg
                if req.action == "sell" and making > 0:
                    avg = taking / making if making else req.limit_price
                    return making, avg
            except (TypeError, ValueError):
                pass
        # FOK success flag → assume full fill at request price (last resort).
        if resp.get("success"):
            return req.size, req.limit_price
        return 0.0, req.limit_price

    async def _fees(self, token_id: Optional[str], price: float, size: float) -> float:
        if size <= 0:
            return 0.0
        fee_rate_bps = await self.fee_client.get_fee_rate_bps(token_id) or 0
        return FeeModel.polymarket_taker_fee(price, size, fee_rate_bps)

    async def cancel(self, order_id: str) -> bool:
        return await self.client.cancel_order(order_id)

    async def get_balance(self) -> Optional[float]:
        return await self.client.get_balance_usdc()

    async def get_positions(self) -> List[Dict]:
        return await self.client.get_positions()


def gateway_for(venue: str, **kwargs) -> VenueGateway:
    if venue == KALSHI:
        return KalshiGateway(**kwargs)
    if venue == POLYMARKET:
        return PolymarketGateway(**kwargs)
    raise ValueError(f"unknown venue: {venue}")
