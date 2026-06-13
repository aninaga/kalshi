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
            risk_reducing=req.risk_reducing,
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
            # Unknown — best-effort cancel, then RE-FETCH the order. Fills may
            # have landed before (or while) the cancel did; reporting filled=0
            # without checking would leave an unrecorded, unhedged position.
            await self.cancel(order_id)
            refreshed = None
            try:
                r = await self.client.get_order(order_id)
                if isinstance(r, dict):
                    o = r.get("order")
                    if isinstance(o, dict) and o:
                        refreshed = o
            except Exception as exc:
                logger.error("Post-cancel re-fetch failed for %s: %s", order_id, exc)
            if refreshed is not None:
                out = self._outcome_from_order(req, refreshed, order_id, coid, resp)
                if out.filled_size <= 0:
                    out.error = out.error or "poll_timeout"
                return out
            # Could not verify the canceled order's fills — a blind retry could
            # double the position. Fail CLOSED: report failure, forbid retry.
            out = OrderOutcome(
                venue=KALSHI, status=STATUS_FAILED, requested_size=req.size,
                order_id=order_id, client_order_id=coid,
                error="poll_timeout_unverified", raw=resp,
            )
            out.retryable = False
            return out
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


# Error substrings that mean the POST may have reached the venue even though we
# never saw a clean response (timeout / dropped connection after send). For
# Polymarket these are AMBIGUOUS: there is no server-side dedup, so a retry
# could place a duplicate order.
_PM_AMBIGUOUS_MARKERS = (
    "timeout", "timed out", "connection aborted", "connection reset",
    "connection error", "remote end closed", "broken pipe",
    "bad gateway", "gateway time-out", "gateway timeout", "service unavailable",
)

# CLOB V2 accepts marketable-BUY amounts (pUSD) at 2-decimal precision.
_PM_USD_DECIMALS = 2


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
        # NOTE: Polymarket never receives this id (the V2 SDK salts every
        # order) — it is lineage-only here. There is NO server-side dedup, so
        # ambiguous failures below are returned non-retryable.
        coid = req.ensure_client_order_id()
        # HARD BACKSTOP: a real Polymarket order only goes out when the
        # live-trading lock is armed (post-validation). Otherwise refuse.
        armed, reason = LiveTradingLock.instance().assert_or_warn(POLYMARKET)
        if not armed:
            return OrderOutcome.failed(POLYMARKET, req.size, reason, coid)
        side = "BUY" if req.action == "buy" else "SELL"
        order_type = req.tif if req.tif in ("FOK", "FAK", "GTC", "GTD") else Config.POLYMARKET_ORDER_TYPE
        ttl = req.ttl_seconds or Config.POLYMARKET_ORDER_TTL_SECONDS

        # CLOB V2 marketable (FOK/FAK) BUY: MarketOrderArgs.amount is pUSD
        # DOLLARS, not shares. Convert here and ONLY here — req.size stays
        # shares everywhere else (cross-lane invariant) and the converted
        # amount is never written back to the request. SELLs and limit orders
        # are denominated in shares and pass through unchanged.
        marketable_buy_amount: Optional[float] = None
        if order_type in ("FOK", "FAK") and req.action == "buy":
            if req.limit_price <= 0:
                return OrderOutcome.failed(
                    POLYMARKET, req.size, "marketable_buy_requires_limit_price", coid)
            marketable_buy_amount = round(req.size * req.limit_price, _PM_USD_DECIMALS)
            if marketable_buy_amount <= 0:
                return OrderOutcome.failed(
                    POLYMARKET, req.size, "non_positive_notional", coid)
        send_size = marketable_buy_amount if marketable_buy_amount is not None else req.size

        resp = await self.client.place_order(
            token_id=req.token_id, side=side, price=req.limit_price,
            size=send_size, order_type=order_type, ttl_seconds=ttl,
        )
        if isinstance(resp, dict) and resp.get("error"):
            err = str(resp["error"])
            if self._is_ambiguous_error(err):
                # The POST may have landed. Reconcile fills BEFORE anyone
                # retries — Polymarket has no server dedup.
                return await self._ambiguous_outcome(req, coid, err, resp,
                                                     marketable_buy_amount)
            return OrderOutcome.failed(POLYMARKET, req.size, err, coid, resp)

        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        if not order_id:
            # The venue accepted the POST but we cannot address the order —
            # also ambiguous: it may exist (and fill) server-side.
            return await self._ambiguous_outcome(req, coid, "no_order_id", resp,
                                                 marketable_buy_amount)

        filled, avg_price = self._parse_fill(resp, req, marketable_buy_amount)
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
        tx = (resp.get("tradeIDs") or resp.get("tradeIds")
              or resp.get("transactionsHashes") or resp.get("transactionHashes")
              or []) if isinstance(resp, dict) else []
        trade_ids = [str(t) for t in tx if t] if isinstance(tx, list) else []
        return OrderOutcome(
            venue=POLYMARKET, status=status, requested_size=req.size,
            filled_size=filled, avg_price=avg_price, fees=fees,
            order_id=order_id, client_order_id=coid, raw=resp, trade_ids=trade_ids,
        )

    @staticmethod
    def _is_ambiguous_error(error: str) -> bool:
        e = error.lower()
        return any(marker in e for marker in _PM_AMBIGUOUS_MARKERS)

    async def _ambiguous_outcome(self, req: OrderRequest, coid: str, err: str,
                                 resp, marketable_buy_amount: Optional[float] = None
                                 ) -> OrderOutcome:
        """Handle a POST whose fate is unknown (timeout/connection drop after
        send, or a response without an order id).

        Polymarket has NO server-side dedup (the SDK salts every order), so a
        blind retry could double-order. Reconcile recent fills FIRST: if the
        order actually landed, report the real fill so it is recorded and
        hedged. If we cannot prove it landed, fail CLOSED — return a
        NON-retryable failure so the engine never re-POSTs it.
        """
        filled, avg_price, order_id, trade_ids = await self._find_recent_fill(req)
        if filled > 0:
            logger.warning(
                "Ambiguous Polymarket POST (%s) reconciled to a REAL fill: "
                "%.4f @ %.4f on %s", err, filled, avg_price, req.token_id)
            fees = await self._fees(req.token_id, avg_price, filled)
            status = STATUS_FILLED if filled >= req.size * 0.999 else STATUS_PARTIAL
            return OrderOutcome(
                venue=POLYMARKET, status=status, requested_size=req.size,
                filled_size=filled, avg_price=avg_price, fees=fees,
                order_id=order_id, client_order_id=coid,
                error=f"ambiguous_post_reconciled:{err}",
                raw=resp if isinstance(resp, dict) else None, trade_ids=trade_ids,
            )
        out = OrderOutcome.failed(POLYMARKET, req.size, f"ambiguous_post:{err}",
                                  coid, resp if isinstance(resp, dict) else None)
        out.retryable = False
        return out

    async def _find_recent_fill(self, req: OrderRequest,
                                window_seconds: float = 120.0) -> tuple:
        """Best-effort scan of recent venue trades for a fill matching ``req``
        (same token, same side, within the recency window). Returns
        (filled_shares, avg_price, order_id, trade_ids)."""
        try:
            trades = await self.client.get_trades()
        except Exception as exc:
            logger.error("Ambiguity reconcile: get_trades failed: %s", exc)
            return 0.0, req.limit_price, None, []
        want_side = "BUY" if req.action == "buy" else "SELL"
        cutoff = time.time() - window_seconds
        filled = 0.0
        notional = 0.0
        order_id = None
        trade_ids: List[str] = []
        for t in trades or []:
            if not isinstance(t, dict):
                continue
            token = str(t.get("asset_id") or t.get("token_id") or t.get("market") or "")
            if token != str(req.token_id):
                continue
            side = str(t.get("side") or "").upper()
            if side and side != want_side:
                continue
            ts = t.get("match_time") or t.get("timestamp") or t.get("created_at")
            try:
                if ts is not None and float(ts) < cutoff:
                    continue
            except (TypeError, ValueError):
                pass  # unparseable timestamp — keep the trade (err toward recording)
            try:
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            filled += size
            notional += size * price
            order_id = order_id or (str(t.get("taker_order_id")
                                        or t.get("order_id") or "") or None)
            tid = t.get("id") or t.get("trade_id")
            if tid:
                trade_ids.append(str(tid))
        avg = (notional / filled) if filled > 0 else req.limit_price
        return filled, avg, order_id, trade_ids

    @staticmethod
    def _parse_fill(resp: Dict, req: OrderRequest,
                    marketable_buy_amount: Optional[float] = None) -> tuple:
        """Extract (filled_size_in_SHARES, avg_price) from a CLOB response.

        Polymarket returns matched amounts in several shapes across SDK
        versions; prefer explicit matched size, fall back to taking/making
        amounts, and derive avg price from notional when possible.
        ``marketable_buy_amount`` is the pUSD notional actually sent for a
        marketable (FOK/FAK) BUY — used to derive shares in the bare-success
        fallback, since for those orders the venue consumed dollars.
        """
        if not isinstance(resp, dict):
            return 0.0, req.limit_price
        # Explicit matched size (reported in shares).
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
        # FOK success flag (last resort, no amounts in the response).
        if resp.get("success"):
            if req.action == "buy" and marketable_buy_amount is not None:
                # We sent DOLLARS; a FOK fill consumed exactly that notional.
                # Derive shares from the notional actually sent — req.size is
                # only the pre-rounding intent.
                try:
                    price = float(resp.get("price") or req.limit_price)
                except (TypeError, ValueError):
                    price = req.limit_price
                if price > 0:
                    return marketable_buy_amount / price, price
                return 0.0, req.limit_price
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
