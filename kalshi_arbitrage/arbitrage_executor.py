"""Arbitrage execution orchestrator.

Fires both legs of an arbitrage simultaneously, confirms fills, and
auto-hedges if one leg fails.  Supports paper mode (log-only) and
live mode (real orders).
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Config
from .confirmed_pnl import ConfirmedPnLTracker
from .kalshi_executor import KalshiOrderClient
from .mock_execution import ExecutionResult, FeeModel
from .polymarket_executor import PolymarketOrderClient

logger = logging.getLogger(__name__)


@dataclass
class LegResult:
    """Result of executing one leg of the arbitrage."""
    platform: str
    order_id: Optional[str]
    filled_size: float
    avg_price: float
    fees: float
    status: str  # "filled", "partial", "failed", "paper"
    raw: Optional[Dict] = None


class ArbitrageExecutor:
    """Orchestrates simultaneous execution of two-legged arbitrage trades."""

    def __init__(self, pnl_tracker: Optional[ConfirmedPnLTracker] = None):
        self.kalshi = KalshiOrderClient()
        self.polymarket = PolymarketOrderClient()
        self.pnl_tracker = pnl_tracker
        self._daily_loss = 0.0
        self._daily_reset_date: Optional[str] = None

    async def close(self) -> None:
        await self.kalshi.close()
        await self.polymarket.close()

    # ------------------------------------------------------------------ #
    #  Main entry point                                                    #
    # ------------------------------------------------------------------ #

    async def execute_arbitrage(self, opportunity: Dict) -> ExecutionResult:
        """Execute an arbitrage opportunity.

        Returns an ExecutionResult compatible with the existing simulation
        interface so it can be recorded via _record_execution_receipt().
        """
        opp_id = opportunity.get('opportunity_id', f'opp-{uuid.uuid4().hex[:8]}')
        strategy_type = opportunity.get('strategy_type', 'same_outcome')
        t0 = time.time()

        # Pre-flight checks
        rejection = self._pre_flight_checks(opportunity)
        if rejection:
            return self._skipped_result(opp_id, opportunity, rejection, t0)

        if strategy_type == 'complementary':
            return await self._execute_complementary(opportunity, opp_id, t0)
        else:
            return await self._execute_same_outcome(opportunity, opp_id, t0)

    # ------------------------------------------------------------------ #
    #  Pre-flight checks                                                   #
    # ------------------------------------------------------------------ #

    def _pre_flight_checks(self, opp: Dict) -> Optional[str]:
        """Return a rejection reason string, or None if checks pass."""
        # Daily loss limit
        self._maybe_reset_daily_loss()
        if self._daily_loss >= Config.MAX_DAILY_LOSS_USD:
            return 'daily_loss_limit'

        # Position size cap
        volume = float(opp.get('max_tradeable_volume', 0))
        avg_price = float(opp.get('kalshi_price', 0.5))
        notional = volume * avg_price
        if notional > Config.MAX_POSITION_SIZE_USD:
            # Clamp — don't reject, just cap later
            pass

        # Re-verify minimum profit
        total_profit = float(opp.get('total_profit', 0))
        if total_profit < Config.MIN_PROFIT_AFTER_FEES_USD:
            return 'below_min_profit'

        return None

    def _maybe_reset_daily_loss(self):
        today = time.strftime('%Y-%m-%d')
        if self._daily_reset_date != today:
            self._daily_loss = 0.0
            self._daily_reset_date = today

    # ------------------------------------------------------------------ #
    #  Same-outcome execution (S1/S2)                                      #
    # ------------------------------------------------------------------ #

    async def _execute_same_outcome(self, opp: Dict, opp_id: str,
                                    t0: float) -> ExecutionResult:
        """Buy on one platform, sell on the other."""
        buy_platform = opp.get('buy_platform', '')
        sell_platform = opp.get('sell_platform', '')
        ticker = opp.get('match_data', {}).get('kalshi_market', {}).get('id', '')
        token_id = opp.get('polymarket_token', '')
        volume = self._capped_volume(opp)
        buy_price = float(opp.get('kalshi_price', 0)) if buy_platform == 'kalshi' else float(opp.get('polymarket_price', 0))
        sell_price = float(opp.get('polymarket_price', 0)) if sell_platform == 'polymarket' else float(opp.get('kalshi_price', 0))

        is_paper = Config.EXECUTION_MODE != 'live'

        if is_paper:
            return self._paper_result(opp_id, opp, buy_platform, sell_platform,
                                      buy_price, sell_price, volume, t0)

        # Fire both legs simultaneously
        buy_coro = self._place_leg(buy_platform, 'buy', ticker, token_id,
                                   buy_price, volume)
        sell_coro = self._place_leg(sell_platform, 'sell', ticker, token_id,
                                    sell_price, volume)

        try:
            buy_result, sell_result = await asyncio.wait_for(
                asyncio.gather(buy_coro, sell_coro, return_exceptions=True),
                timeout=Config.EXECUTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(f"Execution timeout for {opp_id}")
            return self._skipped_result(opp_id, opp, 'execution_timeout', t0)

        # Handle exceptions from gather
        if isinstance(buy_result, Exception):
            logger.error(f"Buy leg exception: {buy_result}")
            buy_result = LegResult(buy_platform, None, 0, 0, 0, 'failed')
        if isinstance(sell_result, Exception):
            logger.error(f"Sell leg exception: {sell_result}")
            sell_result = LegResult(sell_platform, None, 0, 0, 0, 'failed')

        # Both filled
        if buy_result.status in ('filled', 'partial') and sell_result.status in ('filled', 'partial'):
            filled = min(buy_result.filled_size, sell_result.filled_size)
            return self._build_result(opp_id, opp, buy_result, sell_result, filled, t0)

        # One failed — hedge the other
        if Config.HEDGE_ENABLED:
            await self._hedge(buy_result, sell_result, ticker, token_id)

        return self._skipped_result(opp_id, opp, 'partial_fill_hedged', t0)

    # ------------------------------------------------------------------ #
    #  Complementary execution (S3/S4)                                     #
    # ------------------------------------------------------------------ #

    async def _execute_complementary(self, opp: Dict, opp_id: str,
                                     t0: float) -> ExecutionResult:
        """Buy on both platforms (YES + NO for guaranteed $1 payout)."""
        strategy = opp.get('strategy', '')
        ticker = opp.get('match_data', {}).get('kalshi_market', {}).get('id', '')
        token_id = opp.get('polymarket_token', '')
        volume = self._capped_volume(opp)

        # Determine which side on each platform
        # S3: Buy Kalshi YES + Buy Poly NO
        # S4: Buy Poly YES + Buy Kalshi NO
        if 'S3' in strategy or 'Kalshi YES' in strategy:
            kalshi_side = 'yes'
            poly_side = 'BUY'  # buying NO token
        else:
            kalshi_side = 'no'
            poly_side = 'BUY'  # buying YES token

        kalshi_price = float(opp.get('kalshi_price', 0))
        poly_price = float(opp.get('polymarket_price', 0))

        is_paper = Config.EXECUTION_MODE != 'live'
        if is_paper:
            return self._paper_result(opp_id, opp, 'kalshi', 'polymarket',
                                      kalshi_price, poly_price, volume, t0)

        # Fire both legs
        kalshi_coro = self._place_kalshi_order(
            ticker, kalshi_side, 'buy', volume, kalshi_price)
        poly_coro = self._place_polymarket_order(
            token_id, poly_side, poly_price, volume)

        try:
            k_result, p_result = await asyncio.wait_for(
                asyncio.gather(kalshi_coro, poly_coro, return_exceptions=True),
                timeout=Config.EXECUTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(f"Complementary execution timeout for {opp_id}")
            return self._skipped_result(opp_id, opp, 'execution_timeout', t0)

        if isinstance(k_result, Exception):
            logger.error(f"Kalshi leg exception: {k_result}")
            k_result = LegResult('kalshi', None, 0, 0, 0, 'failed')
        if isinstance(p_result, Exception):
            logger.error(f"Poly leg exception: {p_result}")
            p_result = LegResult('polymarket', None, 0, 0, 0, 'failed')

        if k_result.status in ('filled', 'partial') and p_result.status in ('filled', 'partial'):
            filled = min(k_result.filled_size, p_result.filled_size)
            return self._build_result(opp_id, opp, k_result, p_result, filled, t0)

        if Config.HEDGE_ENABLED:
            await self._hedge(k_result, p_result, ticker, token_id)

        return self._skipped_result(opp_id, opp, 'partial_fill_hedged', t0)

    # ------------------------------------------------------------------ #
    #  Leg execution helpers                                               #
    # ------------------------------------------------------------------ #

    async def _place_leg(self, platform: str, action: str, ticker: str,
                         token_id: str, price: float, volume: float) -> LegResult:
        """Route a leg to the correct platform executor."""
        if platform == 'kalshi':
            return await self._place_kalshi_order(ticker, 'yes', action, volume, price)
        else:
            side = 'BUY' if action == 'buy' else 'SELL'
            return await self._place_polymarket_order(token_id, side, price, volume)

    async def _place_kalshi_order(self, ticker: str, side: str, action: str,
                                  volume: float, price: float) -> LegResult:
        """Place a Kalshi order and return a LegResult."""
        count = int(volume)
        if count <= 0:
            return LegResult('kalshi', None, 0, 0, 0, 'failed')

        price_cents = max(1, min(99, int(round(price * 100))))
        resp = await self.kalshi.place_order(ticker, side, action, count, price_cents)

        if 'error' in resp:
            return LegResult('kalshi', None, 0, 0, 0, 'failed', resp)

        order = resp.get('order', {})
        order_id = order.get('order_id', '')
        status = order.get('status', '')

        # Kalshi may fill immediately or need polling
        if status in ('executed', 'filled'):
            filled = float(order.get('count_filled', count))
            avg_p = float(order.get('average_fill_price', price_cents)) / 100.0
            fees = FeeModel.kalshi_taker_fee(avg_p, filled)
            return LegResult('kalshi', order_id, filled, avg_p, fees, 'filled', resp)

        # Poll briefly for fill
        filled, avg_p = await self._poll_kalshi_fill(order_id, count, price_cents)
        fees = FeeModel.kalshi_taker_fee(avg_p, filled) if filled > 0 else 0.0
        st = 'filled' if filled >= count else ('partial' if filled > 0 else 'failed')
        return LegResult('kalshi', order_id, filled, avg_p, fees, st, resp)

    async def _poll_kalshi_fill(self, order_id: str, expected: int,
                                price_cents: int) -> tuple:
        """Poll Kalshi order for up to 3 seconds. Returns (filled, avg_price)."""
        for _ in range(6):
            await asyncio.sleep(0.5)
            resp = await self.kalshi.get_order(order_id)
            order = resp.get('order', {})
            status = order.get('status', '')
            filled = float(order.get('count_filled', 0))
            if status in ('executed', 'filled', 'canceled', 'expired'):
                avg_p = float(order.get('average_fill_price', price_cents)) / 100.0
                return filled, avg_p
        return 0, price_cents / 100.0

    async def _place_polymarket_order(self, token_id: str, side: str,
                                      price: float, volume: float) -> LegResult:
        """Place a Polymarket order and return a LegResult."""
        resp = await self.polymarket.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=volume,
            order_type=Config.POLYMARKET_ORDER_TYPE,
            ttl_seconds=Config.KALSHI_ORDER_TTL_SECONDS,
        )

        if isinstance(resp, dict) and resp.get('error'):
            return LegResult('polymarket', None, 0, 0, 0, 'failed', resp)

        order_id = resp.get('orderID', '') if isinstance(resp, dict) else ''
        if not order_id:
            return LegResult('polymarket', None, 0, 0, 0, 'failed', resp)

        # For FOK, the order is either fully filled or fully rejected
        # For GTD/GTC, we need to poll
        if Config.POLYMARKET_ORDER_TYPE == 'FOK':
            # FOK: check if success flag is present
            success = resp.get('success', False) if isinstance(resp, dict) else False
            if success:
                return LegResult('polymarket', order_id, volume, price, 0, 'filled', resp)
            else:
                return LegResult('polymarket', order_id, 0, 0, 0, 'failed', resp)

        # Poll for GTD/GTC fills
        filled, avg_p = await self._poll_polymarket_fill(order_id, volume, price)
        st = 'filled' if filled >= volume * 0.95 else ('partial' if filled > 0 else 'failed')
        return LegResult('polymarket', order_id, filled, avg_p, 0, st, resp)

    async def _poll_polymarket_fill(self, order_id: str, expected: float,
                                    price: float) -> tuple:
        """Poll Polymarket order for up to 3 seconds."""
        for _ in range(6):
            await asyncio.sleep(0.5)
            resp = await self.polymarket.get_order(order_id)
            if isinstance(resp, dict):
                status = resp.get('status', '')
                matched = float(resp.get('size_matched', 0))
                if status in ('matched', 'canceled', 'expired'):
                    return matched, price
        return 0, price

    # ------------------------------------------------------------------ #
    #  Hedging                                                             #
    # ------------------------------------------------------------------ #

    async def _hedge(self, leg_a: LegResult, leg_b: LegResult,
                     ticker: str, token_id: str) -> None:
        """Attempt to unwind a filled leg when the other leg failed."""
        filled_leg = None
        if leg_a.filled_size > 0 and leg_b.filled_size == 0:
            filled_leg = leg_a
        elif leg_b.filled_size > 0 and leg_a.filled_size == 0:
            filled_leg = leg_b
        else:
            return  # Both failed or both partially filled — nothing clean to hedge

        logger.warning(
            f"Hedging: unwinding {filled_leg.filled_size} on {filled_leg.platform} "
            f"(order {filled_leg.order_id})"
        )

        try:
            if filled_leg.platform == 'kalshi':
                # Sell back what we bought
                await self.kalshi.place_order(
                    ticker, 'yes', 'sell',
                    int(filled_leg.filled_size),
                    max(1, int(round(filled_leg.avg_price * 100)) - 1),  # 1c worse for fast fill
                    ttl_seconds=10,
                )
            else:
                await self.polymarket.place_order(
                    token_id, 'SELL', filled_leg.avg_price * 0.99,
                    filled_leg.filled_size, 'FOK',
                )
        except Exception as e:
            logger.error(f"Hedge failed: {e}")

    # ------------------------------------------------------------------ #
    #  Result builders                                                     #
    # ------------------------------------------------------------------ #

    def _capped_volume(self, opp: Dict) -> float:
        """Cap volume to MAX_POSITION_SIZE_USD."""
        volume = float(opp.get('max_tradeable_volume', 0))
        avg_price = max(float(opp.get('kalshi_price', 0.5)), 0.01)
        max_shares = Config.MAX_POSITION_SIZE_USD / avg_price
        return min(volume, max_shares)

    def _build_result(self, opp_id: str, opp: Dict, buy: LegResult,
                      sell: LegResult, filled: float,
                      t0: float) -> ExecutionResult:
        gross = sell.avg_price * filled - buy.avg_price * filled
        net = gross - buy.fees - sell.fees
        self._daily_loss -= net  # net is positive for profit, so subtract
        latency = int((time.time() - t0) * 1000)
        exec_id = f"exec-{uuid.uuid4().hex[:16]}"

        return ExecutionResult(
            opportunity_id=opp_id,
            buy_platform=buy.platform,
            sell_platform=sell.platform,
            requested_volume=float(opp.get('max_tradeable_volume', 0)),
            filled_volume=filled,
            avg_buy_price=buy.avg_price,
            avg_sell_price=sell.avg_price,
            buy_fees=buy.fees,
            sell_fees=sell.fees,
            gross_profit=gross,
            net_profit=net,
            profit_margin=(net / (buy.avg_price * filled)) if filled > 0 else 0,
            latency_ms=latency,
            timestamp=time.time(),
            execution_id=exec_id,
            buy_order_id=buy.order_id,
            sell_order_id=sell.order_id,
            confirmation_source='exchange',
        )

    def _paper_result(self, opp_id: str, opp: Dict, buy_platform: str,
                      sell_platform: str, buy_price: float,
                      sell_price: float, volume: float,
                      t0: float) -> ExecutionResult:
        """Simulate a fill in paper mode — log but don't place real orders."""
        gross = (sell_price - buy_price) * volume
        # Use FeeModel for realistic fee estimates
        buy_fees = FeeModel.kalshi_taker_fee(buy_price, volume) if buy_platform == 'kalshi' else 0
        sell_fees = FeeModel.kalshi_taker_fee(sell_price, volume) if sell_platform == 'kalshi' else 0
        net = gross - buy_fees - sell_fees
        latency = int((time.time() - t0) * 1000)
        exec_id = f"paper-{uuid.uuid4().hex[:16]}"

        logger.info(
            f"[PAPER] {opp.get('strategy', '?')}: "
            f"buy {volume:.0f}x @ {buy_price:.4f} on {buy_platform}, "
            f"sell @ {sell_price:.4f} on {sell_platform} → "
            f"gross=${gross:.2f} net=${net:.2f}"
        )

        return ExecutionResult(
            opportunity_id=opp_id,
            buy_platform=buy_platform,
            sell_platform=sell_platform,
            requested_volume=volume,
            filled_volume=volume,
            avg_buy_price=buy_price,
            avg_sell_price=sell_price,
            buy_fees=buy_fees,
            sell_fees=sell_fees,
            gross_profit=gross,
            net_profit=net,
            profit_margin=(net / (buy_price * volume)) if volume > 0 and buy_price > 0 else 0,
            latency_ms=latency,
            timestamp=time.time(),
            execution_id=exec_id,
            confirmation_source='paper',
        )

    def _skipped_result(self, opp_id: str, opp: Dict, reason: str,
                        t0: float) -> ExecutionResult:
        latency = int((time.time() - t0) * 1000)
        return ExecutionResult(
            opportunity_id=opp_id,
            buy_platform=opp.get('buy_platform', ''),
            sell_platform=opp.get('sell_platform', ''),
            requested_volume=float(opp.get('max_tradeable_volume', 0)),
            filled_volume=0,
            avg_buy_price=0,
            avg_sell_price=0,
            buy_fees=0,
            sell_fees=0,
            gross_profit=0,
            net_profit=0,
            profit_margin=0,
            latency_ms=latency,
            timestamp=time.time(),
            skipped_reason=reason,
        )
