"""
Polymarket Order Executor

Handles order placement on Polymarket's CLOB via Polygon network.
Supports limit orders, market orders, and FOK execution.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any
from enum import Enum

import aiohttp

logger = logging.getLogger(__name__)


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class PolymarketOrder:
    """Represents a Polymarket order."""
    order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    status: str  # 'live', 'filled', 'cancelled'
    filled_size: Decimal = Decimal("0")
    avg_price: Optional[Decimal] = None
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)


class PolymarketExecutor:
    """
    Production-grade Polymarket order executor.

    Handles wallet signing, CLOB API interaction, and order management.
    Uses Polymarket's official CLOB API endpoints.
    """

    CLOB_API = "https://clob.polymarket.com"
    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(
        self,
        private_key: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
    ):
        # Wallet private key for signing
        self.private_key = private_key or os.getenv("POLYMARKET_PRIVATE_KEY")

        # API credentials (for authenticated endpoints)
        self.api_key = api_key or os.getenv("POLYMARKET_API_KEY")
        self.api_secret = api_secret or os.getenv("POLYMARKET_API_SECRET")
        self.api_passphrase = api_passphrase or os.getenv("POLYMARKET_API_PASSPHRASE")

        self._session: Optional[aiohttp.ClientSession] = None
        self._web3 = None
        self._account = None

        # Rate limiting
        self._last_request_time = 0
        self._min_request_interval = 0.1  # 10 requests/second

        # Statistics
        self.stats = {
            "orders_placed": 0,
            "orders_filled": 0,
            "orders_cancelled": 0,
            "total_volume": Decimal("0"),
            "gas_spent": Decimal("0"),
        }

        logger.info("PolymarketExecutor initialized")

    async def initialize(self) -> bool:
        """Initialize the executor, setup web3, verify connection."""
        try:
            # Create HTTP session
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )

            # Initialize Web3 if private key is provided
            if self.private_key:
                try:
                    from web3 import Web3
                    from eth_account import Account

                    # Connect to Polygon RPC
                    polygon_rpc = os.getenv(
                        "POLYGON_RPC_URL",
                        "https://polygon-rpc.com"
                    )
                    self._web3 = Web3(Web3.HTTPProvider(polygon_rpc))

                    # Load account from private key
                    if self.private_key.startswith("0x"):
                        self._account = Account.from_key(self.private_key)
                    else:
                        self._account = Account.from_key(f"0x{self.private_key}")

                    balance = await self._get_usdc_balance()
                    logger.info(
                        f"Polymarket connected: wallet={self._account.address[:10]}... "
                        f"USDC=${balance:.2f}"
                    )

                except ImportError:
                    logger.warning("web3 not installed, wallet signing disabled")
                except Exception as e:
                    logger.error(f"Web3 initialization failed: {e}")

            # Verify CLOB API is accessible
            markets = await self._request("GET", "/markets", api="clob")
            if markets is not None:
                logger.info("Polymarket CLOB API connected")
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to initialize Polymarket executor: {e}")
            return False

    async def _get_usdc_balance(self) -> Decimal:
        """Get USDC balance on Polygon."""
        if not self._web3 or not self._account:
            return Decimal("0")

        try:
            # USDC contract on Polygon
            usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            usdc_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function",
                }
            ]

            contract = self._web3.eth.contract(
                address=self._web3.to_checksum_address(usdc_address),
                abi=usdc_abi
            )

            balance = contract.functions.balanceOf(self._account.address).call()
            return Decimal(str(balance)) / Decimal("1000000")  # USDC has 6 decimals

        except Exception as e:
            logger.error(f"Failed to get USDC balance: {e}")
            return Decimal("0")

    def _get_auth_headers(self) -> Dict[str, str]:
        """Generate authentication headers for CLOB API."""
        if not all([self.api_key, self.api_secret, self.api_passphrase]):
            return {}

        timestamp = str(int(time.time() * 1000))

        # HMAC signature
        import hmac
        import hashlib

        message = f"{timestamp}GET/auth".encode()
        signature = hmac.new(
            self.api_secret.encode(),
            message,
            hashlib.sha256
        ).hexdigest()

        return {
            "POLY-ADDRESS": self._account.address if self._account else "",
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-API-KEY": self.api_key,
            "POLY-PASSPHRASE": self.api_passphrase,
        }

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
        api: str = "clob",
        authenticated: bool = False,
    ) -> Optional[Any]:
        """Make request to Polymarket API."""
        # Rate limiting
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            await asyncio.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

        base_url = self.CLOB_API if api == "clob" else self.GAMMA_API
        url = f"{base_url}{path}"

        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers.update(self._get_auth_headers())

        try:
            async with self._session.request(
                method, url, headers=headers, json=data
            ) as response:
                if response.status in (200, 201):
                    return await response.json()
                else:
                    error_text = await response.text()
                    logger.error(f"Polymarket API error {response.status}: {error_text}")
                    return None

        except Exception as e:
            logger.error(f"Polymarket request failed: {e}")
            return None

    async def get_market(self, condition_id: str) -> Optional[Dict]:
        """Get market details by condition ID."""
        return await self._request("GET", f"/markets/{condition_id}", api="clob")

    async def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """Get orderbook for a token."""
        return await self._request("GET", f"/book?token_id={token_id}", api="clob")

    async def get_price(self, token_id: str) -> Optional[Dict]:
        """Get current price for a token."""
        return await self._request("GET", f"/price?token_id={token_id}", api="clob")

    async def place_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
        order_type: str = "GTC",  # GTC, GTD, FOK, IOC
        market_id: Optional[str] = None,
    ) -> Optional[PolymarketOrder]:
        """
        Place an order on Polymarket CLOB.

        Args:
            token_id: The token ID to trade
            side: BUY or SELL
            price: Price per share (0.01 to 0.99)
            size: Number of shares
            order_type: GTC, GTD, FOK, or IOC
            market_id: Optional market/condition ID for reference

        Returns:
            PolymarketOrder if successful, None otherwise
        """
        if not self._account:
            logger.error("No wallet configured for Polymarket")
            return None

        try:
            # Build order data
            order_data = {
                "tokenID": token_id,
                "price": str(price),
                "size": str(size),
                "side": side.value,
                "orderType": order_type,
                "owner": self._account.address,
            }

            # Sign the order
            order_data["signature"] = await self._sign_order(order_data)

            logger.info(
                f"Placing Polymarket order: {side.value} {size}x @ ${price} "
                f"token={token_id[:16]}..."
            )

            result = await self._request(
                "POST", "/order", data=order_data, api="clob", authenticated=True
            )

            if result and "orderID" in result:
                self.stats["orders_placed"] += 1

                order = PolymarketOrder(
                    order_id=result["orderID"],
                    market_id=market_id or "",
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                    status=result.get("status", "live"),
                )

                if order.status == "filled":
                    self.stats["orders_filled"] += 1
                    self.stats["total_volume"] += size * price

                logger.info(f"Order placed: {order.order_id} status={order.status}")
                return order

            return None

        except Exception as e:
            logger.error(f"Failed to place Polymarket order: {e}")
            return None

    async def _sign_order(self, order_data: Dict) -> str:
        """Sign order with wallet private key."""
        if not self._account:
            raise ValueError("No wallet configured")

        try:
            from eth_account.messages import encode_defunct

            # Create message to sign
            message = json.dumps(order_data, sort_keys=True)
            message_hash = encode_defunct(text=message)

            # Sign
            signed = self._account.sign_message(message_hash)
            return signed.signature.hex()

        except Exception as e:
            logger.error(f"Failed to sign order: {e}")
            raise

    async def place_market_order(
        self,
        token_id: str,
        side: OrderSide,
        size: Decimal,
        market_id: Optional[str] = None,
    ) -> Optional[PolymarketOrder]:
        """
        Place a market order (takes best available price).

        Uses IOC (immediate-or-cancel) with aggressive pricing.
        """
        # Get current best price
        book = await self.get_orderbook(token_id)
        if not book:
            return None

        if side == OrderSide.BUY:
            # Take best ask
            asks = book.get("asks", [])
            if not asks:
                return None
            best_price = Decimal(str(asks[0]["price"]))
        else:
            # Take best bid
            bids = book.get("bids", [])
            if not bids:
                return None
            best_price = Decimal(str(bids[0]["price"]))

        return await self.place_order(
            token_id=token_id,
            side=side,
            price=best_price,
            size=size,
            order_type="IOC",
            market_id=market_id,
        )

    async def place_fok_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
        market_id: Optional[str] = None,
    ) -> Optional[PolymarketOrder]:
        """Place a fill-or-kill order."""
        return await self.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type="FOK",
            market_id=market_id,
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        result = await self._request(
            "DELETE",
            f"/order/{order_id}",
            api="clob",
            authenticated=True
        )
        if result:
            self.stats["orders_cancelled"] += 1
            logger.info(f"Order cancelled: {order_id}")
            return True
        return False

    async def get_order(self, order_id: str) -> Optional[Dict]:
        """Get order status."""
        return await self._request(
            "GET",
            f"/order/{order_id}",
            api="clob",
            authenticated=True
        )

    async def get_open_orders(self) -> List[Dict]:
        """Get all open orders."""
        if not self._account:
            return []
        result = await self._request(
            "GET",
            f"/orders?owner={self._account.address}",
            api="clob",
            authenticated=True
        )
        return result if result else []

    async def close(self):
        """Close the executor and cleanup resources."""
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info(f"PolymarketExecutor closed. Stats: {self.stats}")
