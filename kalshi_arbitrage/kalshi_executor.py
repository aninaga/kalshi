"""Kalshi order placement client with RSA-PSS authentication."""

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import Config

logger = logging.getLogger(__name__)


def kalshi_auth_headers(method: str, path: str) -> Dict[str, str]:
    """Generate RSA-PSS signed authentication headers for Kalshi API.

    Args:
        method: HTTP method (GET, POST, DELETE, etc.)
        path: API path starting with /trade-api/... (no host)

    Returns:
        Dict of headers, or empty dict if credentials are missing.
    """
    api_key = os.getenv('KALSHI_API_KEY')
    if not api_key:
        return {}

    # The signing key path must be EXPLICIT. The old behavior silently fell
    # back to a well-known filename at the repo root, which meant any checkout
    # containing a stray key file could sign real order requests without the
    # operator ever configuring credentials. Signing without an explicit path
    # now fails LOUD instead of guessing.
    key_path = os.getenv('KALSHI_PRIVATE_KEY_PATH')
    if not key_path:
        raise RuntimeError(
            "KALSHI_PRIVATE_KEY_PATH is not set. Refusing to sign Kalshi API "
            "requests without an explicitly configured private-key path (the "
            "implicit repo-root .pem fallback was removed). Set "
            "KALSHI_PRIVATE_KEY_PATH to the RSA key that pairs with KALSHI_API_KEY."
        )
    key_path = Path(key_path).expanduser()
    if not key_path.exists():
        raise RuntimeError(
            f"Kalshi private key not found at {key_path} "
            f"(from KALSHI_PRIVATE_KEY_PATH). Refusing to sign."
        )

    try:
        with open(key_path, 'rb') as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)

        timestamp = str(int(time.time() * 1000))
        message = (timestamp + method.upper() + path).encode('utf-8')
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            'Content-Type': 'application/json',
            'User-Agent': Config.HTTP_USER_AGENT,
            'KALSHI-ACCESS-KEY': api_key,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode('utf-8'),
            'KALSHI-ACCESS-TIMESTAMP': timestamp,
        }
    except Exception as e:
        logger.error(f"Kalshi auth signing failed: {e}")
        return {}


class KalshiOrderClient:
    """Client for placing and managing orders on Kalshi."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        # Base URL already includes /trade-api/v2
        self._base = Config.KALSHI_API_BASE

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=Config.default_headers())
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _api_path(self, endpoint: str) -> str:
        """Return the path portion (no host) for auth signing."""
        # Config.KALSHI_API_BASE = https://api.elections.kalshi.com/trade-api/v2
        # We need to sign /trade-api/v2/...
        return '/trade-api/v2' + endpoint

    # Methods that can move money / mutate venue state. Gated below.
    _MUTATING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})

    def _mutation_blocked(self, method: str, endpoint: str,
                          risk_reducing: bool = False) -> Optional[str]:
        """Last-line safety gate INSIDE the raw client (fail CLOSED).

        The gateway layer already checks the live-trading lock, but this raw
        client is importable from any REPL/agent/script, so the client itself
        must refuse mutating requests when the system is not armed or is
        halted. Semantics:

          * ALL mutating methods require the live-trading lock to be ARMED
            (paper mode / disarmed -> nothing real can ever be POSTed). This is
            NEVER bypassed — not even for a risk-reducing order.
          * Order CREATION (POST/PUT/PATCH) additionally requires the kill
            switch to be inactive — UNLESS the order is explicitly
            ``risk_reducing`` (an exposure-DECREASING hedge/flatten leg). A halt
            must never block its own unwind: a one-leg failure that trips the
            breaker would otherwise leave the book one-legged until manual
            reset (the cross-lane conflict the 2026-06-12 review caught). A
            risk-reducing order can only shrink exposure, so it passes the halt
            (loudly logged); arming is still required, so nothing real fires in
            paper/disarmed mode regardless.
          * Cancels (DELETE) are permitted during a kill-switch halt — a halt
            must be able to take risk OFF (cancel resting orders), never put
            new risk on.
          * Any error while evaluating the gate itself BLOCKS the request.

        Returns a rejection reason string, or None when the request may proceed.
        """
        try:
            # Lazy imports: execution/__init__ imports venue_gateway which
            # imports this module, so a top-level import would be circular.
            from .execution.kill_switch import KillSwitch
            from .execution.live_lock import LiveTradingLock

            if not LiveTradingLock.instance().is_armed():
                logger.critical(
                    "BLOCKED %s %s: live trading lock is NOT armed — the raw "
                    "Kalshi client refuses all mutating requests.", method, endpoint)
                return 'live_trading_locked'
            if method != 'DELETE':
                active, why = KillSwitch.instance().is_active()
                if active:
                    if risk_reducing:
                        logger.critical(
                            "HALT-BYPASS %s %s: kill switch active (%s) but this "
                            "is a RISK-REDUCING (exposure-decreasing) order — "
                            "permitting the unwind so the halt cannot strand a "
                            "one-legged book.", method, endpoint, why)
                        return None
                    logger.critical(
                        "BLOCKED %s %s: execution kill switch active (%s).",
                        method, endpoint, why)
                    return f'halted:{why}'
            return None
        except Exception as exc:  # fail CLOSED: a broken gate blocks trading
            logger.critical(
                "BLOCKED %s %s: safety-gate check itself failed (%s) — "
                "failing closed.", method, endpoint, exc)
            return f'safety_gate_error:{exc}'

    async def _request(self, method: str, endpoint: str,
                       body: Optional[Dict] = None,
                       risk_reducing: bool = False) -> Dict[str, Any]:
        """Make an authenticated API request.

        ``risk_reducing`` marks an exposure-decreasing order (hedge/flatten leg)
        that may pass a kill-switch halt (still requires the lock armed). It is
        threaded only from ``place_order``; never set it on an opening order.
        """
        method = method.upper()
        if method in self._MUTATING_METHODS:
            blocked = self._mutation_blocked(method, endpoint,
                                             risk_reducing=risk_reducing)
            if blocked:
                return {'error': blocked}
        url = self._base + endpoint
        path = self._api_path(endpoint)
        headers = kalshi_auth_headers(method, path)
        if not headers:
            return {'error': 'missing_credentials'}

        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=Config.EXECUTION_TIMEOUT_SECONDS)

        try:
            kwargs: Dict[str, Any] = {'headers': headers, 'timeout': timeout}
            if body is not None:
                kwargs['json'] = body

            async with session.request(method, url, **kwargs) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    logger.warning(f"Kalshi API {method} {endpoint} → {resp.status}: {data}")
                    return {'error': f'http_{resp.status}', 'detail': data}
                return data
        except asyncio.TimeoutError:
            logger.error(f"Kalshi API timeout: {method} {endpoint}")
            return {'error': 'timeout'}
        except Exception as e:
            logger.error(f"Kalshi API error: {method} {endpoint}: {e}")
            return {'error': str(e)}

    # ---- Account ----

    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance. Useful for auth verification."""
        return await self._request('GET', '/portfolio/balance')

    # ---- Orders ----

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: int,
        ttl_seconds: int = 0,
        client_order_id: Optional[str] = None,
        risk_reducing: bool = False,
    ) -> Dict[str, Any]:
        """Place a limit order on Kalshi.

        Args:
            ticker: Market ticker (e.g. "KXBTC-25FEB28-T100000")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            price_cents: Limit price in cents (1-99)
            ttl_seconds: Time-to-live in seconds. 0 = GTC. >0 = pseudo-IOC via
                         expiration_ts. Default uses Config.KALSHI_ORDER_TTL_SECONDS.
            risk_reducing: True ONLY for an exposure-decreasing hedge/flatten
                         leg — lets the order pass a kill-switch halt (arming
                         still required) so a halt cannot strand a one-legged
                         book. NEVER set this on an opening order.

        Returns:
            Order response dict with 'order' key on success.
        """
        if ttl_seconds <= 0:
            ttl_seconds = Config.KALSHI_ORDER_TTL_SECONDS

        body: Dict[str, Any] = {
            'ticker': ticker,
            'action': action,
            'side': side,
            'type': 'limit',
            'count': count,
            'sell_position_floor': 0,
            'buy_max_cost': price_cents * count,
        }
        # Set the price on the appropriate side
        if side == 'yes':
            body['yes_price'] = price_cents
        else:
            body['no_price'] = price_cents

        # Expiration for pseudo-IOC behavior
        if ttl_seconds > 0:
            body['expiration_ts'] = int(time.time()) + ttl_seconds

        # Idempotency: a deterministic client order id lets a retry reuse the
        # same id so the exchange de-dupes instead of creating a duplicate.
        if client_order_id:
            body['client_order_id'] = client_order_id

        logger.info(f"Kalshi place_order: {action} {count}x {side} @ {price_cents}c on {ticker}"
                    + (" [risk-reducing]" if risk_reducing else ""))
        result = await self._request('POST', '/portfolio/orders', body,
                                     risk_reducing=risk_reducing)
        if 'error' not in result:
            order = result.get('order', {})
            logger.info(f"Kalshi order placed: id={order.get('order_id')}, status={order.get('status')}")
        return result

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        result = await self._request('DELETE', f'/portfolio/orders/{order_id}')
        if 'error' in result:
            logger.warning(f"Kalshi cancel failed for {order_id}: {result}")
            return False
        logger.info(f"Kalshi order cancelled: {order_id}")
        return True

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get order status and fill details."""
        return await self._request('GET', f'/portfolio/orders/{order_id}')

    async def get_fills(self, order_id: Optional[str] = None,
                        ticker: Optional[str] = None,
                        limit: int = 100) -> List[Dict]:
        """Get fill events, optionally filtered by order_id or ticker."""
        params = f'?limit={limit}'
        if order_id:
            params += f'&order_id={order_id}'
        if ticker:
            params += f'&ticker={ticker}'
        result = await self._request('GET', f'/portfolio/fills{params}')
        return result.get('fills', [])

    async def get_positions(self) -> List[Dict]:
        """Get current positions."""
        result = await self._request('GET', '/portfolio/positions')
        return result.get('market_positions', [])

    # ---- Market resolution (settlement reconciliation) ----

    async def is_resolved(self, ticker: Optional[str]) -> bool:
        """True only when the venue POSITIVELY reports the market resolved.

        Used by ``reconcile.Reconciler`` to flip a locked-in receipt to
        settled-realized. Fails CLOSED: any error, missing ticker, or
        ambiguous status returns False (the receipt simply stays locked-in
        until a later run can confirm).
        """
        if not ticker:
            return False
        resp = await self._request('GET', f'/markets/{ticker}')
        if not isinstance(resp, dict) or 'error' in resp:
            return False
        market = resp.get('market') or {}
        if not isinstance(market, dict):
            return False
        status = str(market.get('status') or '').lower()
        result = str(market.get('result') or '').lower()
        # 'settled'/'finalized' = payout done; 'determined' (or an explicit
        # yes/no result) = outcome known, payout imminent. Anything else
        # (active/open/closed-awaiting-determination) is NOT resolved.
        return status in ('settled', 'finalized', 'determined') or result in ('yes', 'no')

    # ---- Convenience ----

    async def buy_yes(self, ticker: str, count: int, price_cents: int,
                      ttl_seconds: int = 0) -> Dict:
        return await self.place_order(ticker, 'yes', 'buy', count, price_cents, ttl_seconds)

    async def buy_no(self, ticker: str, count: int, price_cents: int,
                     ttl_seconds: int = 0) -> Dict:
        return await self.place_order(ticker, 'no', 'buy', count, price_cents, ttl_seconds)

    async def sell_yes(self, ticker: str, count: int, price_cents: int,
                       ttl_seconds: int = 0) -> Dict:
        return await self.place_order(ticker, 'yes', 'sell', count, price_cents, ttl_seconds)

    async def sell_no(self, ticker: str, count: int, price_cents: int,
                      ttl_seconds: int = 0) -> Dict:
        return await self.place_order(ticker, 'no', 'sell', count, price_cents, ttl_seconds)
