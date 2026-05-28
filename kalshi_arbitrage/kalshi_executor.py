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

    key_path = os.getenv('KALSHI_PRIVATE_KEY_PATH')
    if not key_path:
        key_path = Path(__file__).resolve().parents[1] / 'kalshi_private_key.pem'
    key_path = Path(key_path).expanduser()
    if not key_path.exists():
        logger.error(f"Kalshi private key not found at {key_path}")
        return {}

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
            self._session = aiohttp.ClientSession()
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

    async def _request(self, method: str, endpoint: str,
                       body: Optional[Dict] = None) -> Dict[str, Any]:
        """Make an authenticated API request."""
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

        logger.info(f"Kalshi place_order: {action} {count}x {side} @ {price_cents}c on {ticker}")
        result = await self._request('POST', '/portfolio/orders', body)
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
