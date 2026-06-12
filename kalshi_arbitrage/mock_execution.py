import asyncio
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

import aiohttp

import venue_fees

from .config import Config


@dataclass
class Fill:
    price: float
    size: float


@dataclass
class ExecutionResult:
    opportunity_id: str
    buy_platform: str
    sell_platform: str
    requested_volume: float
    filled_volume: float
    avg_buy_price: float
    avg_sell_price: float
    buy_fees: float
    sell_fees: float
    gross_profit: float
    net_profit: float
    profit_margin: float
    latency_ms: int
    timestamp: float
    skipped_reason: Optional[str] = None
    execution_id: Optional[str] = None
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    buy_fill_id: Optional[str] = None
    sell_fill_id: Optional[str] = None
    buy_trade_id: Optional[str] = None
    sell_trade_id: Optional[str] = None
    settlement_id: Optional[str] = None
    confirmation_source: str = "simulation"


class PolymarketFeeClient:
    def __init__(self):
        self._cache: Dict[str, Tuple[int, float]] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            # Browser UA required: Polymarket's CLOB fee-rate endpoint sits
            # behind Cloudflare and 403s the default Python UA.
            self._session = aiohttp.ClientSession(headers=Config.default_headers())
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_fee_rate_bps(self, token_id: str) -> Optional[int]:
        if not token_id:
            return None
        now = time.time()
        cached = self._cache.get(token_id)
        if cached:
            fee_rate_bps, ts = cached
            if now - ts <= Config.POLYMARKET_FEE_RATE_TTL_SECONDS:
                return fee_rate_bps

        async with self._lock:
            cached = self._cache.get(token_id)
            if cached:
                fee_rate_bps, ts = cached
                if now - ts <= Config.POLYMARKET_FEE_RATE_TTL_SECONDS:
                    return fee_rate_bps

            session = await self._get_session()
            url = Config.POLYMARKET_FEE_RATE_ENDPOINT.format(token_id=token_id)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            except Exception:
                return None

            fee_rate_bps = data.get('fee_rate_bps')
            if fee_rate_bps is None:
                return None
            try:
                fee_rate_bps = int(fee_rate_bps)
            except (TypeError, ValueError):
                return None

            self._cache[token_id] = (fee_rate_bps, now)
            return fee_rate_bps


class FeeModel:
    @staticmethod
    def kalshi_taker_fee(price: float, size: float) -> float:
        # fees = round up(0.07 x C x P x (1-P)) — delegates to venue_fees,
        # the repo-wide canonical schedule.
        return venue_fees.kalshi_taker_fee(price, size)

    @staticmethod
    def polymarket_taker_fee(price: float, size: float, fee_rate_bps: int) -> float:
        """Official Polymarket taker fee: ``C × feeRate × P × (1−P)`` (USDC).

        Verified against docs.polymarket.com/trading/fees (fetched 2026-06-09):
        takers pay ``shares × rate × p × (1−p)`` with a PER-CATEGORY rate
        (Geopolitics 0, Sports 0.03, Politics/Finance/Tech/Mentions 0.04,
        Economics/Culture/Weather/Other 0.05, Crypto 0.07); makers are never
        charged; fees round to 5 decimals with a 0.00001 USDC minimum charged.
        Same parabolic shape as Kalshi's curve — ~0 at the extreme prices where
        durable cross-venue arb concentrates. ``fee_rate_bps`` is the category
        rate in basis points (500 = 0.05; conservative default in
        ``Config.POLYMARKET_ESTIMATED_FEE_RATE_BPS``); a per-token live rate
        from ``PolymarketFeeClient`` plugs in directly.

        Replaced the legacy curve+flat-2%-of-notional model on 2026-06-09
        (coordinated with research.harness.realistic_fills + test_arb_ws5):
        the flat piece does not exist in the official schedule and over-charged
        extreme-priced legs by ~an order of magnitude, understating the
        capturable edge exactly where it lives. Rate 0 (Geopolitics) is a
        genuine zero-fee category, not a missing-data sentinel.

        2026-06-12: delegates to ``venue_fees`` (repo-wide canonical schedule).
        Missing/zero ``fee_rate_bps`` stays a genuine 0 fee here (this method's
        contract); venue_fees' category/default fallback is NOT applied.
        """
        if not fee_rate_bps or fee_rate_bps <= 0:
            return 0.0
        return venue_fees.pm_taker_fee(price, size, fee_rate_bps=fee_rate_bps)


class MockExecutionEngine:
    def __init__(self):
        self.fee_client = PolymarketFeeClient()

    async def close(self) -> None:
        await self.fee_client.close()

    def _normalize_levels(self, levels: List[Dict]) -> List[Dict[str, float]]:
        normalized = []
        for level in levels or []:
            if isinstance(level, dict):
                price = level.get('price')
                size = level.get('size') if level.get('size') is not None else level.get('quantity')
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]
            else:
                continue
            try:
                price = float(price)
                size = float(size)
            except (TypeError, ValueError):
                continue
            normalized.append({'price': price, 'size': size})
        return normalized

    def _fill_from_asks(self, asks: List[Dict], target_size: float) -> Tuple[List[Fill], float]:
        fills: List[Fill] = []
        remaining = target_size
        for level in sorted(asks, key=lambda x: x['price']):
            if remaining <= 0:
                break
            take = min(level['size'], remaining)
            if take > 0:
                fills.append(Fill(price=level['price'], size=take))
                remaining -= take
        filled = target_size - remaining
        return fills, filled

    def _fill_from_bids(self, bids: List[Dict], target_size: float) -> Tuple[List[Fill], float]:
        fills: List[Fill] = []
        remaining = target_size
        for level in sorted(bids, key=lambda x: x['price'], reverse=True):
            if remaining <= 0:
                break
            take = min(level['size'], remaining)
            if take > 0:
                fills.append(Fill(price=level['price'], size=take))
                remaining -= take
        filled = target_size - remaining
        return fills, filled

    def _truncate_fills(self, fills: List[Fill], target_size: float) -> List[Fill]:
        if not fills:
            return []
        remaining = target_size
        out: List[Fill] = []
        for fill in fills:
            if remaining <= 0:
                break
            take = min(fill.size, remaining)
            out.append(Fill(price=fill.price, size=take))
            remaining -= take
        return out

    def _avg_price(self, fills: List[Fill]) -> float:
        if not fills:
            return 0.0
        notional = sum(f.price * f.size for f in fills)
        size = sum(f.size for f in fills)
        return (notional / size) if size > 0 else 0.0

    async def execute_market(
        self,
        opportunity_id: str,
        buy_platform: str,
        sell_platform: str,
        buy_asks: List[Dict],
        sell_bids: List[Dict],
        token_id: Optional[str],
        requested_volume: float,
        latency_ms: int,
    ) -> ExecutionResult:
        buy_levels = self._normalize_levels(buy_asks)
        sell_levels = self._normalize_levels(sell_bids)

        if not buy_levels or not sell_levels:
            return ExecutionResult(
                opportunity_id=opportunity_id,
                buy_platform=buy_platform,
                sell_platform=sell_platform,
                requested_volume=requested_volume,
                filled_volume=0.0,
                avg_buy_price=0.0,
                avg_sell_price=0.0,
                buy_fees=0.0,
                sell_fees=0.0,
                gross_profit=0.0,
                net_profit=0.0,
                profit_margin=0.0,
                latency_ms=latency_ms,
                timestamp=time.time(),
                skipped_reason='empty_orderbook',
            )

        buy_fills, buy_filled = self._fill_from_asks(buy_levels, requested_volume)
        sell_fills, sell_filled = self._fill_from_bids(sell_levels, requested_volume)
        filled = min(buy_filled, sell_filled)
        if filled <= 0:
            return ExecutionResult(
                opportunity_id=opportunity_id,
                buy_platform=buy_platform,
                sell_platform=sell_platform,
                requested_volume=requested_volume,
                filled_volume=0.0,
                avg_buy_price=0.0,
                avg_sell_price=0.0,
                buy_fees=0.0,
                sell_fees=0.0,
                gross_profit=0.0,
                net_profit=0.0,
                profit_margin=0.0,
                latency_ms=latency_ms,
                timestamp=time.time(),
                skipped_reason='no_liquidity',
            )

        buy_fills = self._truncate_fills(buy_fills, filled)
        sell_fills = self._truncate_fills(sell_fills, filled)

        buy_notional = sum(f.price * f.size for f in buy_fills)
        sell_notional = sum(f.price * f.size for f in sell_fills)

        buy_fees = 0.0
        sell_fees = 0.0

        if buy_platform == 'kalshi':
            for fill in buy_fills:
                buy_fees += FeeModel.kalshi_taker_fee(fill.price, fill.size)
        elif buy_platform == 'polymarket':
            fee_rate_bps = await self.fee_client.get_fee_rate_bps(token_id) or 0
            for fill in buy_fills:
                buy_fees += FeeModel.polymarket_taker_fee(fill.price, fill.size, fee_rate_bps)

        if sell_platform == 'kalshi':
            for fill in sell_fills:
                sell_fees += FeeModel.kalshi_taker_fee(fill.price, fill.size)
        elif sell_platform == 'polymarket':
            fee_rate_bps = await self.fee_client.get_fee_rate_bps(token_id) or 0
            for fill in sell_fills:
                sell_fees += FeeModel.polymarket_taker_fee(fill.price, fill.size, fee_rate_bps)

        gross_profit = sell_notional - buy_notional
        net_profit = gross_profit - buy_fees - sell_fees
        profit_margin = (net_profit / buy_notional) if buy_notional > 0 else 0.0

        execution_id = f"sim-exec-{uuid.uuid4().hex[:16]}"
        timestamp = time.time()

        return ExecutionResult(
            opportunity_id=opportunity_id,
            buy_platform=buy_platform,
            sell_platform=sell_platform,
            requested_volume=requested_volume,
            filled_volume=filled,
            avg_buy_price=self._avg_price(buy_fills),
            avg_sell_price=self._avg_price(sell_fills),
            buy_fees=buy_fees,
            sell_fees=sell_fees,
            gross_profit=gross_profit,
            net_profit=net_profit,
            profit_margin=profit_margin,
            latency_ms=latency_ms,
            timestamp=timestamp,
            skipped_reason=None,
            execution_id=execution_id,
            buy_order_id=f"{execution_id}-buy-order",
            sell_order_id=f"{execution_id}-sell-order",
            buy_fill_id=f"{execution_id}-buy-fill",
            sell_fill_id=f"{execution_id}-sell-fill",
            buy_trade_id=f"{execution_id}-buy-trade",
            sell_trade_id=f"{execution_id}-sell-trade",
            settlement_id=f"{execution_id}-settlement",
            confirmation_source="simulation",
        )
