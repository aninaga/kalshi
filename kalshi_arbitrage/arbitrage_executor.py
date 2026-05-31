"""Arbitrage execution orchestrator (Phase B).

Thin layer on top of the general-purpose execution engine
(``kalshi_arbitrage.execution``). It builds two ``OrderRequest`` legs, fires
them simultaneously through the ``ExecutionEngine`` (kill switch + per-venue
circuit breaker + idempotent retries), and — when the legs don't fill equally —
runs a **partial-fill-aware hedge that confirms its unwind** and trips the kill
switch if the unwind can't complete.

Arb-specific concerns only live here; all the reusable machinery is in
``execution/``.
"""

import asyncio
import logging
import time
import uuid
from typing import Dict, Optional, Tuple

from .config import Config
from .confirmed_pnl import ConfirmedPnLTracker
from .execution import (
    KALSHI,
    POLYMARKET,
    STATUS_FILLED,
    STATUS_PARTIAL,
    ExecutionEngine,
    KalshiGateway,
    KillSwitch,
    OrderOutcome,
    OrderRequest,
    PolymarketGateway,
)
from .execution.capture import ExecutionCapture
from .execution.simulated_gateway import SimulatedGateway
from .matching import AllowlistVerifier
from .mock_execution import ExecutionResult, FeeModel, PolymarketFeeClient
from .risk_engine import RiskEngine

logger = logging.getLogger(__name__)

_EPS = 1e-6


class ArbitrageExecutor:
    """Orchestrates simultaneous execution of two-legged arbitrage trades."""

    def __init__(self, pnl_tracker: Optional[ConfirmedPnLTracker] = None):
        self.kalshi_gateway = KalshiGateway()
        self.polymarket_gateway = PolymarketGateway()
        self.pm_fee_client = self.polymarket_gateway.fee_client
        self.kill_switch = KillSwitch.instance()
        # Real gateways place orders (lock-gated); simulated gateways fill
        # without any API call. Paper mode uses the simulated set so the FULL
        # execution path runs end-to-end with zero chance of a real order.
        self._real_gateways = {KALSHI: self.kalshi_gateway, POLYMARKET: self.polymarket_gateway}
        self._sim_gateways = {
            KALSHI: SimulatedGateway(KALSHI),
            POLYMARKET: SimulatedGateway(POLYMARKET, fee_client=self.pm_fee_client),
        }
        active = self._real_gateways if Config.EXECUTION_MODE == "live" else self._sim_gateways
        # dict(...) COPY: the engine owns its own gateway dict so _sync can
        # clear/refill it without mutating the canonical _real/_sim sources.
        self.engine = ExecutionEngine(dict(active), kill_switch=self.kill_switch)
        self.risk_engine = RiskEngine()
        self.pnl_tracker = pnl_tracker
        self.capture = ExecutionCapture() if Config.EXECUTION_CAPTURE_ENABLED else None
        self.allowlist = AllowlistVerifier()
        self._daily_loss = 0.0
        self._daily_reset_date: Optional[str] = None
        self._live_in_flight = 0  # concurrent live executions

    async def close(self) -> None:
        await self.kalshi_gateway.close()
        await self.polymarket_gateway.close()
        for gw in self._sim_gateways.values():
            await gw.close()

    def _sync_engine_gateways(self) -> None:
        """Point the engine at real gateways in live mode, simulated otherwise.

        Updates the gateway dict in place (only when the engine is our real
        ExecutionEngine — tests that swap in a fake engine are left untouched).
        """
        target = self._real_gateways if Config.EXECUTION_MODE == "live" else self._sim_gateways
        gws = getattr(self.engine, "gateways", None)
        if isinstance(gws, dict):
            gws.clear()
            gws.update(target)

    # ------------------------------------------------------------------ #
    #  Main entry point                                                    #
    # ------------------------------------------------------------------ #

    async def execute_arbitrage(self, opportunity: Dict) -> ExecutionResult:
        """Public entry point: run the trade and capture the result (Phase C)."""
        result, hedge_info = await self._run_arbitrage(opportunity)
        if self.capture is not None:
            try:
                self.capture.record(opportunity, result, hedge=hedge_info)
            except Exception as exc:
                logger.error("Execution capture failed: %s", exc)
        return result

    async def _run_arbitrage(self, opportunity: Dict) -> Tuple[ExecutionResult, Optional[Dict]]:
        opp_id = opportunity.get('opportunity_id', f'opp-{uuid.uuid4().hex[:8]}')
        t0 = time.time()

        rejection = await self._pre_flight_checks(opportunity)
        if rejection:
            return self._skipped_result(opp_id, opportunity, rejection, t0), None

        # Both paper and live run the SAME orchestration; only the gateways
        # differ (simulated vs real). Paper therefore exercises leg building,
        # the engine (idempotency/circuit-breaker/kill-switch), the hedge, and
        # capture — everything except a real order. Keep the active engine's
        # gateways in sync with the current mode (tests flip the mode at runtime).
        self._sync_engine_gateways()

        legs = self._build_legs(opportunity)
        if legs is None:
            return self._skipped_result(opp_id, opportunity, 'unbuildable_legs', t0), None
        buy_req, sell_req = legs

        self._live_in_flight += 1
        try:
            buy_out, sell_out = await asyncio.wait_for(
                asyncio.gather(
                    self.engine.execute(buy_req, stable_key=f"{opp_id}:buy"),
                    self.engine.execute(sell_req, stable_key=f"{opp_id}:sell"),
                    return_exceptions=True,
                ),
                timeout=Config.EXECUTION_TIMEOUT_SECONDS + Config.FILL_POLL_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("Execution timeout for %s", opp_id)
            return self._skipped_result(opp_id, opportunity, 'execution_timeout', t0), None
        finally:
            self._live_in_flight -= 1

        buy_out = self._coerce_outcome(buy_out, buy_req)
        sell_out = self._coerce_outcome(sell_out, sell_req)

        matched = min(buy_out.filled_size, sell_out.filled_size)
        imbalance = abs(buy_out.filled_size - sell_out.filled_size)

        # Hedge any imbalance (including partial/partial) and confirm the unwind.
        hedge_info = None
        if imbalance > _EPS and Config.HEDGE_ENABLED:
            hedge_info = await self._hedge(buy_req, buy_out, sell_req, sell_out)

        if matched > _EPS:
            result = self._build_result(opp_id, opportunity, buy_out, sell_out,
                                        matched, t0, hedge_info)
            return result, hedge_info

        reason = 'partial_fill_hedged' if imbalance > _EPS else 'no_fill'
        return self._skipped_result(opp_id, opportunity, reason, t0), hedge_info

    # ------------------------------------------------------------------ #
    #  Pre-flight checks                                                   #
    # ------------------------------------------------------------------ #

    async def _pre_flight_checks(self, opp: Dict) -> Optional[str]:
        """Return a rejection reason string, or None if checks pass."""
        active, why = self.kill_switch.is_active()
        # In paper mode the master flag being off is expected — only the
        # explicit sentinel / tripped flag should block a paper simulation.
        if active and (Config.EXECUTION_MODE == 'live' or why != 'execution_disabled'):
            return f'halted:{why}'

        self._maybe_reset_daily_loss()
        if self._daily_loss >= Config.MAX_DAILY_LOSS_USD:
            return 'daily_loss_limit'

        total_profit = float(opp.get('total_profit', 0))
        if total_profit < Config.MIN_PROFIT_AFTER_FEES_USD:
            return 'below_min_profit'

        # Risk gate (uses normalized books if the caller attached them).
        if Config.RISK_GATE_ENABLED:
            reason = await self._risk_gate(opp)
            if reason:
                return reason

        # Live-only pilot gates.
        if Config.EXECUTION_MODE == 'live':
            # Allowlist gate: the pilot only fires on operator-approved pairs.
            if Config.MATCH_REQUIRE_ALLOWLIST_FOR_LIVE:
                match = opp.get('match_data', {}) or {}
                k_id = match.get('kalshi_market', {}).get('id')
                p_id = opp.get('polymarket_token') or match.get('polymarket_market', {}).get('id')
                if not self.allowlist.is_allowlisted(k_id, p_id):
                    return 'not_allowlisted'

            # Concurrency cap.
            if self._live_in_flight >= Config.LIVE_MAX_CONCURRENT_POSITIONS:
                return 'max_concurrent_positions'

            # Balance gate (best-effort — can't verify → warn, proceed).
            if Config.REQUIRE_BALANCE_CHECK:
                reason = await self._balance_gate(opp)
                if reason:
                    return reason

        return None

    async def _risk_gate(self, opp: Dict) -> Optional[str]:
        try:
            books = opp.get('risk_orderbooks') or (None, None)
            kalshi_nob, poly_nob = books
            assessment = await self.risk_engine.assess_opportunity(opp, kalshi_nob, poly_nob)
            if float(assessment.total_risk_score) > Config.MAX_RISK_SCORE:
                logger.warning("Risk gate rejected %s: score=%.1f",
                               opp.get('opportunity_id'), float(assessment.total_risk_score))
                return 'risk_too_high'
            if float(assessment.confidence) < Config.MIN_RISK_CONFIDENCE:
                return 'low_risk_confidence'
        except Exception as exc:  # never let the risk model crash execution
            logger.error("Risk gate error (proceeding): %s", exc)
        return None

    async def _balance_gate(self, opp: Dict) -> Optional[str]:
        try:
            volume = self._capped_volume(opp)
            buy_platform = opp.get('buy_platform', '')
            buy_price = self._buy_price(opp)
            notional = volume * buy_price
            gateway = self.kalshi_gateway if buy_platform == 'kalshi' else self.polymarket_gateway
            balance = await gateway.get_balance()
            if balance is not None and balance < notional:
                logger.warning("Balance gate: need $%.2f have $%.2f on %s",
                               notional, balance, buy_platform)
                return 'insufficient_balance'
        except Exception as exc:
            logger.error("Balance gate error (proceeding): %s", exc)
        return None

    def _maybe_reset_daily_loss(self):
        today = time.strftime('%Y-%m-%d')
        if self._daily_reset_date != today:
            self._daily_loss = 0.0
            self._daily_reset_date = today

    # ------------------------------------------------------------------ #
    #  Leg construction                                                    #
    # ------------------------------------------------------------------ #

    def _buy_price(self, opp: Dict) -> float:
        return (float(opp.get('kalshi_price', 0)) if opp.get('buy_platform') == 'kalshi'
                else float(opp.get('polymarket_price', 0)))

    def _build_legs(self, opp: Dict) -> Optional[Tuple[OrderRequest, OrderRequest]]:
        """Translate an opportunity into two venue-agnostic OrderRequests."""
        ticker = opp.get('match_data', {}).get('kalshi_market', {}).get('id', '')
        token_id = opp.get('polymarket_token', '')
        volume = self._capped_volume(opp)
        if volume <= 0:
            return None

        strategy_type = opp.get('strategy_type', 'same_outcome')
        kalshi_price = float(opp.get('kalshi_price', 0))
        poly_price = float(opp.get('polymarket_price', 0))

        if strategy_type == 'complementary':
            # Both legs are buys of complementary outcomes → guaranteed $1.
            strategy = opp.get('strategy', '')
            kalshi_side = 'yes' if ('S3' in strategy or 'Kalshi YES' in strategy) else 'no'
            buy_req = OrderRequest(
                venue=KALSHI, action='buy', size=volume, limit_price=kalshi_price,
                ticker=ticker, outcome_side=kalshi_side, tif='IOC',
                meta={'opportunity_id': opp.get('opportunity_id'), 'leg': 'kalshi'},
            )
            sell_req = OrderRequest(
                venue=POLYMARKET, action='buy', size=volume, limit_price=poly_price,
                token_id=token_id, tif=Config.POLYMARKET_ORDER_TYPE,
                meta={'opportunity_id': opp.get('opportunity_id'), 'leg': 'polymarket'},
            )
            return buy_req, sell_req

        # Same-outcome: buy on one venue, sell on the other.
        buy_platform = opp.get('buy_platform', '')
        if buy_platform == 'kalshi':
            buy_req = OrderRequest(
                venue=KALSHI, action='buy', size=volume, limit_price=kalshi_price,
                ticker=ticker, outcome_side='yes', tif='IOC',
                meta={'opportunity_id': opp.get('opportunity_id'), 'leg': 'kalshi_buy'},
            )
            sell_req = OrderRequest(
                venue=POLYMARKET, action='sell', size=volume, limit_price=poly_price,
                token_id=token_id, tif=Config.POLYMARKET_ORDER_TYPE,
                meta={'opportunity_id': opp.get('opportunity_id'), 'leg': 'poly_sell'},
            )
        else:
            buy_req = OrderRequest(
                venue=POLYMARKET, action='buy', size=volume, limit_price=poly_price,
                token_id=token_id, tif=Config.POLYMARKET_ORDER_TYPE,
                meta={'opportunity_id': opp.get('opportunity_id'), 'leg': 'poly_buy'},
            )
            sell_req = OrderRequest(
                venue=KALSHI, action='sell', size=volume, limit_price=kalshi_price,
                ticker=ticker, outcome_side='yes', tif='IOC',
                meta={'opportunity_id': opp.get('opportunity_id'), 'leg': 'kalshi_sell'},
            )
        return buy_req, sell_req

    @staticmethod
    def _coerce_outcome(result, req: OrderRequest) -> OrderOutcome:
        if isinstance(result, OrderOutcome):
            return result
        if isinstance(result, Exception):
            logger.error("Leg %s raised: %s", req.venue, result)
        return OrderOutcome.failed(req.venue, req.size, "leg_exception")

    # ------------------------------------------------------------------ #
    #  Partial-fill-aware hedge with confirmed unwind                      #
    # ------------------------------------------------------------------ #

    async def _hedge(self, buy_req: OrderRequest, buy_out: OrderOutcome,
                     sell_req: OrderRequest, sell_out: OrderOutcome) -> Dict:
        """Unwind the *imbalance* between the two legs and confirm it filled.

        Unlike the old hedge (which only acted when one leg was exactly zero),
        this handles any partial/partial mismatch: it computes the over-filled
        quantity, fires the opposite order for exactly that size at a price
        concession for a fast fill, and polls it to a terminal state. If the
        unwind cannot complete, it records the residual exposure and trips the
        kill switch — an un-hedged position must stop all further trading.
        """
        imbalance = buy_out.filled_size - sell_out.filled_size
        qty = abs(imbalance)
        if qty <= _EPS:
            return {'hedged': True, 'residual': 0.0}

        # The over-filled leg is the one we must unwind back toward neutral.
        over_req, over_out = (buy_req, buy_out) if imbalance > 0 else (sell_req, sell_out)
        unwind_req = self._build_unwind_request(over_req, over_out, qty)

        logger.warning("Hedging imbalance of %.4f via %s %s on %s",
                       qty, unwind_req.action, unwind_req.venue,
                       unwind_req.ticker or unwind_req.token_id)

        outcome = await self.engine.execute(
            unwind_req, stable_key=f"{over_req.meta.get('opportunity_id')}:hedge")
        residual = qty - outcome.filled_size

        if residual > max(_EPS, qty * 0.01):
            logger.critical("UNWIND INCOMPLETE: residual %.4f on %s — tripping kill switch",
                            residual, unwind_req.venue)
            self.kill_switch.trip(f"unwind_failed:{over_req.meta.get('opportunity_id')}")
            self._record_residual_exposure(over_req, residual, over_out.avg_price)

        return {
            'hedged': residual <= max(_EPS, qty * 0.01),
            'residual': residual,
            'unwind_order_id': outcome.order_id,
            'unwind_filled': outcome.filled_size,
        }

    def _build_unwind_request(self, req: OrderRequest, out: OrderOutcome,
                              qty: float) -> OrderRequest:
        """Opposite of ``req`` for ``qty`` units, priced to cross for a fast fill."""
        opposite = 'sell' if req.action == 'buy' else 'buy'
        concession = Config.HEDGE_PRICE_CONCESSION
        ref = out.avg_price or req.limit_price
        # To fill fast: a sell crosses down, a buy crosses up.
        price = ref - concession if opposite == 'sell' else ref + concession
        price = min(0.99, max(0.01, price))
        return OrderRequest(
            venue=req.venue, action=opposite, size=qty, limit_price=price,
            ticker=req.ticker, outcome_side=req.outcome_side, token_id=req.token_id,
            tif='FOK' if req.venue == POLYMARKET else 'IOC',
            ttl_seconds=Config.HEDGE_TIMEOUT_SECONDS,
            meta={**req.meta, 'hedge': True},
        )

    def _record_residual_exposure(self, req: OrderRequest, qty: float, price: float) -> None:
        if self.pnl_tracker is None:
            return
        try:
            self.pnl_tracker.record_execution_receipt(
                opportunity_id=req.meta.get('opportunity_id', 'unknown'),
                buy_platform=req.venue, sell_platform=req.venue,
                buy_price=price, buy_size=qty, buy_fee=0.0,
                sell_price=0.0, sell_size=0.0, sell_fee=0.0,
                source='exchange',
                metadata={'residual_exposure': True, 'reason': 'unwind_failed'},
                mark_confirmed=False, mark_settled=False,
            )
        except Exception as exc:
            logger.error("Failed to record residual exposure: %s", exc)

    # ------------------------------------------------------------------ #
    #  Result builders                                                     #
    # ------------------------------------------------------------------ #

    def _capped_volume(self, opp: Dict) -> float:
        volume = float(opp.get('max_tradeable_volume', 0))
        avg_price = max(self._buy_price(opp), 0.01)
        notional_cap = Config.MAX_POSITION_SIZE_USD
        # Live pilot: an even tighter staged notional clamp on real money.
        if Config.EXECUTION_MODE == 'live':
            notional_cap = min(notional_cap, Config.LIVE_MAX_NOTIONAL_USD)
        return min(volume, notional_cap / avg_price)

    def _build_result(self, opp_id: str, opp: Dict, buy: OrderOutcome,
                      sell: OrderOutcome, filled: float, t0: float,
                      hedge_info: Optional[Dict]) -> ExecutionResult:
        gross = sell.avg_price * filled - buy.avg_price * filled
        net = gross - buy.fees - sell.fees
        self._daily_loss -= net  # net positive → reduces daily loss
        latency = int((time.time() - t0) * 1000)
        # Paper fills carry source 'paper'; only a real (live) fill is 'exchange'.
        is_live = Config.EXECUTION_MODE == 'live'
        prefix = 'exec' if is_live else 'paper'
        exec_id = f"{prefix}-{uuid.uuid4().hex[:16]}"

        return ExecutionResult(
            opportunity_id=opp_id,
            buy_platform=buy.venue,
            sell_platform=sell.venue,
            requested_volume=float(opp.get('max_tradeable_volume', 0)),
            filled_volume=filled,
            avg_buy_price=buy.avg_price,
            avg_sell_price=sell.avg_price,
            buy_fees=buy.fees,
            sell_fees=sell.fees,
            gross_profit=gross,
            net_profit=net,
            profit_margin=(net / (buy.avg_price * filled)) if filled > 0 and buy.avg_price > 0 else 0,
            latency_ms=latency,
            timestamp=time.time(),
            execution_id=exec_id,
            buy_order_id=buy.order_id,
            sell_order_id=sell.order_id,
            confirmation_source='exchange' if is_live else 'paper',
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
