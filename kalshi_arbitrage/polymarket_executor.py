"""Polymarket order placement client for CLOB V2 via the py-clob-client-v2 SDK.

Polymarket migrated to CLOB V2 on 2026-04-28: the legacy ``py-clob-client``
package was archived 2026-05-11 and can no longer trade against production
(orders wiped at cutover; the EIP-712 exchange domain moved to version "2";
pUSD replaced USDC.e as collateral). The official successor is
``py-clob-client-v2`` (PyPI, v1.0.1 2026-05-09) — verified against
docs.polymarket.com/v2-migration and github.com/Polymarket/py-clob-client-v2
on 2026-06-09.

V2 notes that shape this module:

- Orders no longer carry ``feeRateBps``/``nonce``; fees are charged at match
  time by the protocol (parabolic ``C × rate × p × (1−p)``, takers only).
- Limit orders need per-market ``tick_size`` and ``neg_risk`` flags
  (``PartialCreateOrderOptions``); both are fetched from the CLOB and cached
  here per token.
- ``FAK`` (fill-and-kill) joins FOK/GTC/GTD as a supported order type.
- Signature types: 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE, 3=POLY_1271. As of
  June 2026 the Python SDK has a known bug with type 3 (deposit wallets):
  ``create_or_derive_api_key`` binds creds to the EOA address and orders are
  rejected (py-clob-client-v2#71). Default to EOA (0).
- Collateral is pUSD: USDC.e must be wrapped via the CollateralOnramp before
  trading (docs.polymarket.com/concepts/pusd) — an operator step, not code.

The public surface (used by ``execution/venue_gateway.PolymarketGateway`` and
``reconcile``) is unchanged: ``place_order/cancel_order/cancel_all/get_order/
get_trades/get_balance_usdc/get_positions/buy/sell/close``.
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .config import Config

logger = logging.getLogger(__name__)

# Lazy-import the SDK to avoid a hard dependency when not executing.
_sdk = None


def _ensure_sdk():
    """Import the py-clob-client-v2 SDK on first use."""
    global _sdk
    if _sdk is None:
        try:
            import py_clob_client_v2 as sdk
        except ImportError:
            raise ImportError(
                "py-clob-client-v2 is required for Polymarket execution. "
                "(CLOB V2 went live 2026-04-28; the legacy py-clob-client is "
                "archived and cannot trade.) Install with: "
                "pip install py-clob-client-v2"
            )
        _sdk = sdk
    return _sdk


def _sdk_attr(name: str, default=None):
    """Fetch a class/constant from the SDK, tolerating top-level or submodule layouts."""
    sdk = _ensure_sdk()
    if hasattr(sdk, name):
        return getattr(sdk, name)
    for sub in ("clob_types", "client", "constants"):
        mod = getattr(sdk, sub, None)
        if mod is not None and hasattr(mod, name):
            return getattr(mod, name)
    return default


class PolymarketOrderClient:
    """Client for placing and managing orders on Polymarket via CLOB V2."""

    def __init__(self):
        self._client = None
        self._initialized = False
        # Per-token market metadata caches (tick size / neg-risk flag are
        # required to build V2 limit orders; stable per market).
        self._tick_size_cache: Dict[str, str] = {}
        self._neg_risk_cache: Dict[str, bool] = {}

    def _get_client(self):
        """Lazily initialize the V2 ClobClient with credentials from env."""
        if self._client is not None:
            return self._client

        sdk = _ensure_sdk()

        private_key = os.getenv('POLYMARKET_PRIVATE_KEY')
        if not private_key:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY not set. Add your Ethereum wallet private key to .env"
            )

        funder = os.getenv('POLYMARKET_FUNDER_ADDRESS')
        sig_type = int(os.getenv('POLYMARKET_SIGNATURE_TYPE', '0'))
        if sig_type == 3:
            # py-clob-client-v2#71: POLY_1271 creds bind to the EOA address and
            # orders are rejected. Loud warning rather than silent failure.
            logger.warning(
                "POLYMARKET_SIGNATURE_TYPE=3 (POLY_1271) has a known V2 SDK bug "
                "(py-clob-client-v2#71): orders are rejected with a signer/API-key "
                "address mismatch. Use an EOA wallet (type 0) until it is patched."
            )

        ClobClient = _sdk_attr('ClobClient')
        if ClobClient is None:
            raise RuntimeError("py-clob-client-v2 does not expose ClobClient")

        kwargs = {
            'host': Config.POLYMARKET_CLOB_BASE,
            'key': private_key,
            'chain_id': 137,  # Polygon mainnet
        }
        if funder:
            kwargs['funder'] = funder
        if sig_type > 0:
            kwargs['signature_type'] = sig_type

        self._client = ClobClient(**kwargs)

        # Derive Level-2 API credentials (HMAC keys for authenticated
        # endpoints). V2 renamed create_or_derive_api_creds → ..._api_key;
        # accept either for SDK-version tolerance.
        try:
            derive = (getattr(self._client, 'create_or_derive_api_key', None)
                      or getattr(self._client, 'create_or_derive_api_creds', None))
            if derive is None:
                raise RuntimeError("SDK client lacks create_or_derive_api_key")
            creds = derive()
            self._client.set_api_creds(creds)
            self._initialized = True
            logger.info("Polymarket CLOB V2 client initialized with L2 API credentials")
        except Exception as e:
            logger.error(f"Failed to derive Polymarket API credentials: {e}")
            self._client = None
            raise

        return self._client

    async def close(self) -> None:
        """Clean up resources."""
        self._client = None
        self._initialized = False

    # ---- Market metadata (V2 limit orders need tick size + neg-risk) ----

    async def _get_tick_size(self, token_id: str) -> str:
        """Per-market tick size ("0.1"|"0.01"|"0.001"|"0.0001"), cached."""
        cached = self._tick_size_cache.get(token_id)
        if cached:
            return cached
        tick = "0.01"  # safe default if the lookup fails
        try:
            client = self._get_client()
            getter = getattr(client, 'get_tick_size', None)
            if getter is not None:
                raw = await asyncio.to_thread(getter, token_id)
                if raw:
                    tick = str(raw)
                    self._tick_size_cache[token_id] = tick
        except Exception as e:
            logger.warning(f"Polymarket tick-size lookup failed for {token_id}: {e}")
        return tick

    async def _get_neg_risk(self, token_id: str) -> bool:
        """Whether the token belongs to a neg-risk market (different exchange
        contract), cached. Defaults False on lookup failure."""
        if token_id in self._neg_risk_cache:
            return self._neg_risk_cache[token_id]
        neg = False
        try:
            client = self._get_client()
            getter = getattr(client, 'get_neg_risk', None)
            if getter is not None:
                raw = await asyncio.to_thread(getter, token_id)
                neg = bool(raw.get('neg_risk') if isinstance(raw, dict) else raw)
                self._neg_risk_cache[token_id] = neg
        except Exception as e:
            logger.warning(f"Polymarket neg-risk lookup failed for {token_id}: {e}")
        return neg

    def _order_options(self, tick_size: str, neg_risk: bool):
        """Build PartialCreateOrderOptions if the SDK exposes it (else None)."""
        Options = _sdk_attr('PartialCreateOrderOptions')
        if Options is None:
            return None
        try:
            return Options(tick_size=tick_size, neg_risk=neg_risk)
        except TypeError:
            return Options(tick_size=tick_size)

    @staticmethod
    def _normalize_response(resp) -> Dict[str, Any]:
        if isinstance(resp, dict):
            # V2 reports success/errorMsg explicitly; surface failures as the
            # 'error' key the gateway contract expects.
            if resp.get('success') is False and not resp.get('orderID'):
                resp = dict(resp)
                resp.setdefault('error', resp.get('errorMsg') or 'order_rejected')
            return resp
        return {'raw': str(resp)}

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
        """Place an order on Polymarket (CLOB V2).

        Args:
            token_id: CLOB token ID for the outcome (YES or NO token).
            side: "BUY" or "SELL".
            price: Probability price (0.01 to 0.99).
            size: pUSD amount for FOK/FAK BUY (market order), shares otherwise.
            order_type: "FOK", "FAK", "GTC", or "GTD". Default from config.
            ttl_seconds: For GTD orders, seconds until expiry.

        Returns:
            Response dict with 'orderID' on success, 'error' on failure.
            V2 responses also carry 'status', 'transactionsHashes', 'tradeIDs'.
        """
        if not order_type:
            order_type = Config.POLYMARKET_ORDER_TYPE

        client = self._get_client()
        Side = _sdk_attr('Side')
        OrderType = _sdk_attr('OrderType')
        sdk_side = getattr(Side, side.upper(), side.upper()) if Side else side.upper()

        tick_size = await self._get_tick_size(token_id)
        neg_risk = await self._get_neg_risk(token_id)
        options = self._order_options(tick_size, neg_risk)

        logger.info(
            f"Polymarket V2 place_order: {side} {size}x @ {price} on {token_id} "
            f"({order_type}, tick={tick_size}, neg_risk={neg_risk})"
        )

        try:
            if order_type in ('FOK', 'FAK'):
                # Marketable order: MarketOrderArgs (amount = pUSD for BUY,
                # shares for SELL).
                MarketOrderArgs = _sdk_attr('MarketOrderArgs')
                market_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=size,
                    side=sdk_side,
                )
                ot = getattr(OrderType, order_type, order_type)
                poster = getattr(client, 'create_and_post_market_order', None)
                if poster is not None:
                    if options is not None:
                        resp = await asyncio.to_thread(
                            poster, market_args, options=options, order_type=ot)
                    else:
                        resp = await asyncio.to_thread(
                            poster, market_args, order_type=ot)
                else:  # older create/post split
                    signed = await asyncio.to_thread(
                        client.create_market_order, market_args)
                    resp = await asyncio.to_thread(client.post_order, signed, ot)
            else:
                # GTC or GTD limit order.
                OrderArgs = _sdk_attr('OrderArgs')
                kwargs = dict(token_id=token_id, price=price, size=size, side=sdk_side)
                if order_type == 'GTD' and ttl_seconds > 0:
                    # V2: expiration is a wire-level field (not EIP-712 signed).
                    kwargs['expiration'] = int(time.time()) + ttl_seconds
                try:
                    order_args = OrderArgs(**kwargs)
                except TypeError:
                    kwargs.pop('expiration', None)
                    order_args = OrderArgs(**kwargs)
                    if order_type == 'GTD' and ttl_seconds > 0:
                        setattr(order_args, 'expiration', int(time.time()) + ttl_seconds)

                ot = getattr(OrderType, order_type, order_type)
                poster = getattr(client, 'create_and_post_order', None)
                if poster is not None:
                    if options is not None:
                        resp = await asyncio.to_thread(
                            poster, order_args, options=options, order_type=ot)
                    else:
                        resp = await asyncio.to_thread(
                            poster, order_args, order_type=ot)
                else:
                    signed = await asyncio.to_thread(client.create_order, order_args)
                    resp = await asyncio.to_thread(client.post_order, signed, ot)

            resp = self._normalize_response(resp)
            if resp.get('orderID'):
                logger.info(
                    f"Polymarket order placed: id={resp['orderID']} "
                    f"status={resp.get('status', '?')}"
                )
            else:
                logger.warning(f"Polymarket order response: {resp}")
            return resp
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
        """Get order status (V2: GET /data/order/<id>)."""
        try:
            client = self._get_client()
            resp = await asyncio.to_thread(client.get_order, order_id=order_id)
            return resp if isinstance(resp, dict) else {'raw': str(resp)}
        except Exception as e:
            logger.error(f"Polymarket get_order error for {order_id}: {e}")
            return {'error': str(e)}

    async def get_trades(self, order_id: Optional[str] = None) -> List[Dict]:
        """Authoritative fills (V2: GET /data/trades), used by the reconciler.

        Returns [] on error (reconciliation treats venue data as best-effort
        and retries on its own schedule).
        """
        try:
            client = self._get_client()
            getter = getattr(client, 'get_trades', None)
            if getter is None:
                return []
            TradeParams = _sdk_attr('TradeParams')
            if order_id and TradeParams is not None:
                try:
                    resp = await asyncio.to_thread(getter, TradeParams(id=order_id))
                except TypeError:
                    resp = await asyncio.to_thread(getter)
            else:
                resp = await asyncio.to_thread(getter)
            trades = resp if isinstance(resp, list) else (
                resp.get('trades', []) if isinstance(resp, dict) else [])
            if order_id:
                trades = [
                    t for t in trades
                    if str(t.get('taker_order_id') or t.get('order_id') or '') == str(order_id)
                    or order_id in str(t.get('maker_orders') or '')
                ] or trades
            return trades
        except Exception as e:
            logger.error(f"Polymarket get_trades error: {e}")
            return []

    # ---- Account ----

    async def get_balance_usdc(self) -> Optional[float]:
        """Return available collateral (pUSD) balance, or None if unavailable.

        Required for a pre-trade balance gate. V2 collateral is pUSD (6
        decimals, like USDC); SDK shapes vary, so parse defensively and
        convert from base units when needed.
        """
        try:
            client = self._get_client()
            params = None
            BalanceAllowanceParams = _sdk_attr('BalanceAllowanceParams')
            if BalanceAllowanceParams is not None:
                AssetType = _sdk_attr('AssetType')
                collateral = getattr(AssetType, 'COLLATERAL', None) if AssetType else None
                params = (BalanceAllowanceParams(asset_type=collateral)
                          if collateral else BalanceAllowanceParams())
            resp = await asyncio.to_thread(client.get_balance_allowance, params)
            if not isinstance(resp, dict):
                return None
            raw = resp.get("balance")
            if raw is None:
                return None
            val = float(raw)
            # Heuristic: CLOB returns collateral in 6-decimal base units.
            return val / 1_000_000 if val > 10_000 else val
        except Exception as e:
            logger.error(f"Polymarket get_balance error: {e}")
            return None

    async def get_positions(self) -> List[Dict]:
        """Return current positions. Best-effort; returns [] if unsupported."""
        try:
            client = self._get_client()
            if hasattr(client, "get_positions"):
                resp = await asyncio.to_thread(client.get_positions)
                if isinstance(resp, list):
                    return resp
                if isinstance(resp, dict):
                    return resp.get("positions", [])
            return []
        except Exception as e:
            logger.error(f"Polymarket get_positions error: {e}")
            return []

    # ---- Convenience ----

    async def buy(self, token_id: str, price: float, size: float,
                  order_type: str = "", ttl_seconds: int = 0) -> Dict:
        return await self.place_order(token_id, 'BUY', price, size, order_type, ttl_seconds)

    async def sell(self, token_id: str, price: float, size: float,
                   order_type: str = "", ttl_seconds: int = 0) -> Dict:
        return await self.place_order(token_id, 'SELL', price, size, order_type, ttl_seconds)
