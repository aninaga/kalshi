"""Polymarket order placement client using py-clob-client SDK."""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .config import Config

logger = logging.getLogger(__name__)

# Lazy-import the SDK to avoid hard dependency when not executing
_clob_client = None
_clob_types = None
_order_constants = None


def _ensure_sdk():
    """Import py-clob-client SDK on first use."""
    global _clob_client, _clob_types, _order_constants
    if _clob_client is None:
        try:
            from py_clob_client import client as cc
            from py_clob_client import clob_types as ct
            from py_clob_client.order_builder import constants as oc
            _clob_client = cc
            _clob_types = ct
            _order_constants = oc
        except ImportError:
            raise ImportError(
                "py-clob-client is required for Polymarket execution. "
                "Install with: pip install py-clob-client"
            )


class PolymarketOrderClient:
    """Client for placing and managing orders on Polymarket via CLOB API."""

    def __init__(self):
        self._client = None
        self._initialized = False

    def _get_client(self):
        """Lazily initialize the ClobClient with credentials from env."""
        if self._client is not None:
            return self._client

        _ensure_sdk()

        private_key = os.getenv('POLYMARKET_PRIVATE_KEY')
        if not private_key:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY not set. Add your Ethereum wallet private key to .env"
            )

        funder = os.getenv('POLYMARKET_FUNDER_ADDRESS')
        sig_type = int(os.getenv('POLYMARKET_SIGNATURE_TYPE', '0'))

        kwargs = {
            'host': Config.POLYMARKET_CLOB_BASE,
            'key': private_key,
            'chain_id': 137,  # Polygon mainnet
        }
        if funder:
            kwargs['funder'] = funder
        if sig_type > 0:
            kwargs['signature_type'] = sig_type

        self._client = _clob_client.ClobClient(**kwargs)

        # Derive Level-2 API credentials (HMAC keys for authenticated endpoints)
        try:
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            self._initialized = True
            logger.info("Polymarket CLOB client initialized with L2 API credentials")
        except Exception as e:
            logger.error(f"Failed to derive Polymarket API credentials: {e}")
            self._client = None
            raise

        return self._client

    async def close(self) -> None:
        """Clean up resources."""
        self._client = None
        self._initialized = False

    # ---- Orders ----

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "",
        ttl_seconds: int = 0,
    ) -> Dict[str, Any]:
        """Place an order on Polymarket.

        Args:
            token_id: CLOB token ID for the outcome (YES or NO token).
            side: "BUY" or "SELL".
            price: Probability price (0.01 to 0.99).
            size: USDC amount for BUY, shares for SELL.
            order_type: "FOK", "GTC", or "GTD". Default from config.
            ttl_seconds: For GTD orders, seconds until expiry.

        Returns:
            Response dict with 'orderID' on success, 'error' on failure.
        """
        if not order_type:
            order_type = Config.POLYMARKET_ORDER_TYPE

        _ensure_sdk()
        client = self._get_client()

        sdk_side = _order_constants.BUY if side.upper() == 'BUY' else _order_constants.SELL

        logger.info(f"Polymarket place_order: {side} {size}x @ {price} on {token_id} ({order_type})")

        try:
            if order_type == 'FOK':
                # FOK uses MarketOrderArgs
                market_args = _clob_types.MarketOrderArgs(
                    token_id=token_id,
                    amount=size,
                    side=sdk_side,
                )
                signed = await asyncio.to_thread(client.create_market_order, market_args)
                resp = await asyncio.to_thread(
                    client.post_order, signed, _clob_types.OrderType.FOK
                )
            else:
                # GTC or GTD limit order
                order_args = _clob_types.OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=sdk_side,
                )
                if order_type == 'GTD' and ttl_seconds > 0:
                    order_args.expiration = int(time.time()) + ttl_seconds

                signed = await asyncio.to_thread(client.create_order, order_args)

                ot = (
                    _clob_types.OrderType.GTD if order_type == 'GTD'
                    else _clob_types.OrderType.GTC
                )
                resp = await asyncio.to_thread(client.post_order, signed, ot)

            if isinstance(resp, dict) and resp.get('orderID'):
                logger.info(f"Polymarket order placed: id={resp['orderID']}")
            else:
                logger.warning(f"Polymarket order response: {resp}")

            return resp if isinstance(resp, dict) else {'raw': str(resp)}
        except Exception as e:
            logger.error(f"Polymarket place_order error: {e}")
            return {'error': str(e)}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        try:
            client = self._get_client()
            resp = await asyncio.to_thread(client.cancel, order_id=order_id)
            cancelled = resp.get('canceled', []) if isinstance(resp, dict) else []
            success = order_id in cancelled
            if success:
                logger.info(f"Polymarket order cancelled: {order_id}")
            else:
                logger.warning(f"Polymarket cancel response for {order_id}: {resp}")
            return success
        except Exception as e:
            logger.error(f"Polymarket cancel error for {order_id}: {e}")
            return False

    async def cancel_all(self) -> Dict[str, Any]:
        """Cancel all open orders."""
        try:
            client = self._get_client()
            resp = await asyncio.to_thread(client.cancel_all)
            logger.info(f"Polymarket cancel_all: {resp}")
            return resp if isinstance(resp, dict) else {'raw': str(resp)}
        except Exception as e:
            logger.error(f"Polymarket cancel_all error: {e}")
            return {'error': str(e)}

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get order status."""
        try:
            client = self._get_client()
            resp = await asyncio.to_thread(client.get_order, order_id=order_id)
            return resp if isinstance(resp, dict) else {'raw': str(resp)}
        except Exception as e:
            logger.error(f"Polymarket get_order error for {order_id}: {e}")
            return {'error': str(e)}

    # ---- Convenience ----

    async def buy(self, token_id: str, price: float, size: float,
                  order_type: str = "", ttl_seconds: int = 0) -> Dict:
        return await self.place_order(token_id, 'BUY', price, size, order_type, ttl_seconds)

    async def sell(self, token_id: str, price: float, size: float,
                   order_type: str = "", ttl_seconds: int = 0) -> Dict:
        return await self.place_order(token_id, 'SELL', price, size, order_type, ttl_seconds)
