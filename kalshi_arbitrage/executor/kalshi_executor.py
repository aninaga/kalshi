"""
Kalshi Order Executor

Handles authenticated order placement on Kalshi using RSA signatures.
Supports market orders, limit orders, and FOK (fill-or-kill).
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class KalshiOrder:
    """Represents a Kalshi order."""
    order_id: str
    ticker: str
    side: str  # 'yes' or 'no'
    action: str  # 'buy' or 'sell'
    price: int  # cents (1-99)
    count: int  # number of contracts
    status: str  # 'pending', 'filled', 'partial', 'cancelled'
    filled_count: int = 0
    avg_price: Optional[int] = None
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)


class KalshiExecutor:
    """
    Production-grade Kalshi order executor.

    Handles RSA authentication, order placement, and position management.
    """

    API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key_path: Optional[str] = None,
        private_key_content: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("KALSHI_API_KEY")
        self.private_key_path = private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH")
        self.private_key_content = private_key_content or os.getenv("KALSHI_PRIVATE_KEY")

        self._private_key = None
        self._session: Optional[aiohttp.ClientSession] = None

        # Rate limiting
        self._last_request_time = 0
        self._min_request_interval = 0.05  # 20 requests/second max

        # Statistics
        self.stats = {
            "orders_placed": 0,
            "orders_filled": 0,
            "orders_cancelled": 0,
            "total_volume": Decimal("0"),
        }

        logger.info("KalshiExecutor initialized")

    async def initialize(self) -> bool:
        """Initialize the executor, load keys, verify connection."""
        try:
            # Load private key
            if not self._load_private_key():
                logger.error("Failed to load Kalshi private key")
                return False

            # Create session
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )

            # Verify credentials by getting account info
            account = await self.get_account()
            if account:
                logger.info(f"Kalshi connected: balance=${account.get('balance', 0)/100:.2f}")
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to initialize Kalshi executor: {e}")
            return False

    def _load_private_key(self) -> bool:
        """Load RSA private key for signing."""
        try:
            from cryptography.hazmat.primitives import serialization

            pem_data = None

            # Try content first (for env var)
            if self.private_key_content:
                pem_data = self.private_key_content.encode()
            # Then try file path
            elif self.private_key_path and os.path.exists(self.private_key_path):
                with open(self.private_key_path, "rb") as f:
                    pem_data = f.read()

            if not pem_data:
                logger.warning("No Kalshi private key configured")
                return False

            self._private_key = serialization.load_pem_private_key(
                pem_data, password=None
            )
            return True

        except Exception as e:
            logger.error(f"Failed to load private key: {e}")
            return False

    def _generate_signature(self, method: str, path: str, timestamp: str) -> str:
        """Generate RSA signature for Kalshi API authentication."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        message = f"{timestamp}{method}{path}".encode()

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )

        return base64.b64encode(signature).decode()

    def _get_auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """Generate authentication headers for request."""
        timestamp = str(int(time.time() * 1000))
        signature = self._generate_signature(method, path, timestamp)

        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Make authenticated request to Kalshi API."""
        # Rate limiting
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            await asyncio.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

        url = f"{self.API_BASE}{path}"
        headers = self._get_auth_headers(method, path)

        try:
            async with self._session.request(
                method, url, headers=headers, json=data
            ) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 201:
                    return await response.json()
                else:
                    error_text = await response.text()
                    logger.error(f"Kalshi API error {response.status}: {error_text}")
                    return None

        except Exception as e:
            logger.error(f"Kalshi request failed: {e}")
            return None

    async def get_account(self) -> Optional[Dict]:
        """Get account information including balance."""
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> List[Dict]:
        """Get all current positions."""
        result = await self._request("GET", "/portfolio/positions")
        return result.get("market_positions", []) if result else []

    async def get_market(self, ticker: str) -> Optional[Dict]:
        """Get market details by ticker."""
        return await self._request("GET", f"/markets/{ticker}")

    async def place_order(
        self,
        ticker: str,
        side: str,  # 'yes' or 'no'
        action: str,  # 'buy' or 'sell'
        count: int,
        price: Optional[int] = None,  # None for market order
        order_type: str = "limit",  # 'limit' or 'market'
        time_in_force: str = "gtc",  # 'gtc', 'ioc', 'fok'
    ) -> Optional[KalshiOrder]:
        """
        Place an order on Kalshi.

        Args:
            ticker: Market ticker (e.g., 'BTC-100K-2025')
            side: 'yes' or 'no'
            action: 'buy' or 'sell'
            count: Number of contracts
            price: Price in cents (1-99), None for market order
            order_type: 'limit' or 'market'
            time_in_force: 'gtc', 'ioc', or 'fok'

        Returns:
            KalshiOrder if successful, None otherwise
        """
        order_data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }

        if order_type == "limit" and price is not None:
            order_data["yes_price" if side == "yes" else "no_price"] = price

        if time_in_force != "gtc":
            order_data["time_in_force"] = time_in_force

        logger.info(f"Placing Kalshi order: {action} {count}x {side} @ {price}c on {ticker}")

        result = await self._request("POST", "/portfolio/orders", order_data)

        if result and "order" in result:
            order_info = result["order"]
            self.stats["orders_placed"] += 1

            order = KalshiOrder(
                order_id=order_info.get("order_id", ""),
                ticker=ticker,
                side=side,
                action=action,
                price=price or 0,
                count=count,
                status=order_info.get("status", "pending"),
                filled_count=order_info.get("filled_count", 0),
            )

            if order.status in ("filled", "partial"):
                self.stats["orders_filled"] += 1
                self.stats["total_volume"] += Decimal(str(count * (price or 50) / 100))

            logger.info(f"Order placed: {order.order_id} status={order.status}")
            return order

        return None

    async def place_market_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
    ) -> Optional[KalshiOrder]:
        """Place a market order (immediate execution at best price)."""
        return await self.place_order(
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            order_type="market",
        )

    async def place_fok_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price: int,
    ) -> Optional[KalshiOrder]:
        """Place a fill-or-kill order (all or nothing at specified price)."""
        return await self.place_order(
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            price=price,
            order_type="limit",
            time_in_force="fok",
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        result = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        if result:
            self.stats["orders_cancelled"] += 1
            logger.info(f"Order cancelled: {order_id}")
            return True
        return False

    async def get_order(self, order_id: str) -> Optional[Dict]:
        """Get order status."""
        return await self._request("GET", f"/portfolio/orders/{order_id}")

    async def close(self):
        """Close the executor and cleanup resources."""
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info(f"KalshiExecutor closed. Stats: {self.stats}")
