"""
Atomic Arbitrage Executor

Coordinates execution of both legs of an arbitrage trade.
Implements fail-safe mechanisms to avoid partial fills.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple

from .kalshi_executor import KalshiExecutor, KalshiOrder
from .polymarket_executor import PolymarketExecutor, PolymarketOrder, OrderSide

logger = logging.getLogger(__name__)


class ExecutionStatus(Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCESS = "success"
    PARTIAL = "partial"  # One leg filled, other failed
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class ExecutionLeg:
    """Represents one leg of an arbitrage trade."""
    platform: str  # 'kalshi' or 'polymarket'
    market_id: str
    token_id: Optional[str]  # For Polymarket
    ticker: Optional[str]  # For Kalshi
    side: str  # 'yes' or 'no'
    action: str  # 'buy' or 'sell'
    price: Decimal
    size: int  # Number of contracts/shares

    # Execution results
    order_id: Optional[str] = None
    filled_size: int = 0
    avg_price: Optional[Decimal] = None
    status: str = "pending"


@dataclass
class ExecutionResult:
    """Result of an atomic execution attempt."""
    execution_id: str
    status: ExecutionStatus
    opportunity_id: str
    legs: List[ExecutionLeg]
    total_cost: Decimal = Decimal("0")
    expected_payout: Decimal = Decimal("0")
    actual_profit: Decimal = Decimal("0")
    execution_time_ms: int = 0
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status.value,
            "opportunity_id": self.opportunity_id,
            "total_cost": float(self.total_cost),
            "expected_payout": float(self.expected_payout),
            "actual_profit": float(self.actual_profit),
            "execution_time_ms": self.execution_time_ms,
            "error_message": self.error_message,
            "legs": [
                {
                    "platform": leg.platform,
                    "side": leg.side,
                    "action": leg.action,
                    "price": float(leg.price),
                    "size": leg.size,
                    "filled_size": leg.filled_size,
                    "status": leg.status,
                }
                for leg in self.legs
            ],
            "created_at": self.created_at.isoformat(),
        }


class AtomicExecutor:
    """
    Coordinates atomic execution of arbitrage trades.

    Attempts to execute both legs simultaneously with fail-safe
    mechanisms to minimize risk of partial fills.
    """

    def __init__(
        self,
        kalshi: KalshiExecutor,
        polymarket: PolymarketExecutor,
        max_execution_time_ms: int = 5000,
        max_slippage_pct: Decimal = Decimal("0.5"),
    ):
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.max_execution_time_ms = max_execution_time_ms
        self.max_slippage_pct = max_slippage_pct / 100

        # Execution tracking
        self._execution_counter = 0
        self.active_executions: Dict[str, ExecutionResult] = {}

        # Statistics
        self.stats = {
            "executions_attempted": 0,
            "executions_successful": 0,
            "executions_failed": 0,
            "executions_partial": 0,
            "total_profit": Decimal("0"),
            "total_loss": Decimal("0"),
        }

        # Circuit breaker
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_open_until = 0

        logger.info("AtomicExecutor initialized")

    def _generate_execution_id(self) -> str:
        """Generate unique execution ID."""
        self._execution_counter += 1
        return f"exec_{int(time.time())}_{self._execution_counter:04d}"

    async def execute_arbitrage(
        self,
        opportunity: Dict[str, Any],
        size: int = 100,
        use_fok: bool = True,
    ) -> ExecutionResult:
        """
        Execute an arbitrage opportunity atomically.

        Args:
            opportunity: ArbitrageOpportunity dict with positions
            size: Number of contracts to trade
            use_fok: Use fill-or-kill orders (recommended)

        Returns:
            ExecutionResult with outcome details
        """
        execution_id = self._generate_execution_id()
        start_time = time.time()

        # Check circuit breaker
        if self._circuit_open:
            if time.time() < self._circuit_open_until:
                return ExecutionResult(
                    execution_id=execution_id,
                    status=ExecutionStatus.FAILED,
                    opportunity_id=opportunity.get("opportunity_id", ""),
                    legs=[],
                    error_message="Circuit breaker open - too many failures",
                )
            else:
                self._circuit_open = False
                self._consecutive_failures = 0

        self.stats["executions_attempted"] += 1

        # Parse positions into legs
        legs = self._parse_positions(opportunity, size)
        if not legs:
            return ExecutionResult(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                opportunity_id=opportunity.get("opportunity_id", ""),
                legs=[],
                error_message="Failed to parse opportunity positions",
            )

        result = ExecutionResult(
            execution_id=execution_id,
            status=ExecutionStatus.EXECUTING,
            opportunity_id=opportunity.get("opportunity_id", ""),
            legs=legs,
            expected_payout=Decimal("1.0") * size,
        )

        self.active_executions[execution_id] = result

        try:
            # Execute both legs in parallel with timeout
            if use_fok:
                leg_results = await asyncio.wait_for(
                    self._execute_legs_fok(legs),
                    timeout=self.max_execution_time_ms / 1000
                )
            else:
                leg_results = await asyncio.wait_for(
                    self._execute_legs_parallel(legs),
                    timeout=self.max_execution_time_ms / 1000
                )

            # Evaluate results
            result = self._evaluate_execution(result, leg_results)

        except asyncio.TimeoutError:
            result.status = ExecutionStatus.TIMEOUT
            result.error_message = f"Execution timed out after {self.max_execution_time_ms}ms"
            # Attempt to cancel any pending orders
            await self._cancel_pending_orders(legs)

        except Exception as e:
            result.status = ExecutionStatus.FAILED
            result.error_message = str(e)
            logger.error(f"Execution {execution_id} failed: {e}")

        # Record timing
        result.execution_time_ms = int((time.time() - start_time) * 1000)

        # Update statistics
        self._update_stats(result)

        # Update circuit breaker
        self._update_circuit_breaker(result)

        # Remove from active
        del self.active_executions[execution_id]

        logger.info(
            f"Execution {execution_id} completed: status={result.status.value} "
            f"profit=${float(result.actual_profit):.4f} time={result.execution_time_ms}ms"
        )

        return result

    def _parse_positions(
        self,
        opportunity: Dict[str, Any],
        size: int
    ) -> List[ExecutionLeg]:
        """Parse opportunity positions into execution legs."""
        legs = []

        positions = opportunity.get("positions", [])
        if not positions:
            return legs

        for pos in positions:
            platform = pos.get("platform", opportunity.get("platform", ""))
            if not platform:
                continue

            leg = ExecutionLeg(
                platform=platform,
                market_id=opportunity.get("market_id", ""),
                token_id=pos.get("token_id"),
                ticker=pos.get("ticker"),
                side=pos.get("side", "").lower(),
                action=pos.get("action", "buy").lower(),
                price=Decimal(str(pos.get("price", 0))),
                size=size,
            )
            legs.append(leg)

        return legs

    async def _execute_legs_fok(
        self,
        legs: List[ExecutionLeg]
    ) -> List[Tuple[ExecutionLeg, bool, Optional[Any]]]:
        """
        Execute legs using fill-or-kill orders.
        All orders must fill completely or none do.
        """
        tasks = []

        for leg in legs:
            if leg.platform == "kalshi":
                task = self._execute_kalshi_fok(leg)
            elif leg.platform == "polymarket":
                task = self._execute_polymarket_fok(leg)
            else:
                continue
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        return [
            (legs[i], not isinstance(r, Exception) and r is not None, r)
            for i, r in enumerate(results)
        ]

    async def _execute_kalshi_fok(self, leg: ExecutionLeg) -> Optional[KalshiOrder]:
        """Execute a Kalshi FOK order."""
        if not leg.ticker:
            # Try to derive ticker from market_id
            leg.ticker = leg.market_id.split("|")[0] if "|" in leg.market_id else leg.market_id

        price_cents = int(leg.price * 100)

        return await self.kalshi.place_fok_order(
            ticker=leg.ticker,
            side=leg.side,
            action=leg.action,
            count=leg.size,
            price=price_cents,
        )

    async def _execute_polymarket_fok(self, leg: ExecutionLeg) -> Optional[PolymarketOrder]:
        """Execute a Polymarket FOK order."""
        side = OrderSide.BUY if leg.action == "buy" else OrderSide.SELL

        return await self.polymarket.place_fok_order(
            token_id=leg.token_id or leg.market_id,
            side=side,
            price=leg.price,
            size=Decimal(str(leg.size)),
            market_id=leg.market_id,
        )

    async def _execute_legs_parallel(
        self,
        legs: List[ExecutionLeg]
    ) -> List[Tuple[ExecutionLeg, bool, Optional[Any]]]:
        """
        Execute legs in parallel with limit orders.
        May result in partial fills.
        """
        tasks = []

        for leg in legs:
            if leg.platform == "kalshi":
                task = self._execute_kalshi_limit(leg)
            elif leg.platform == "polymarket":
                task = self._execute_polymarket_limit(leg)
            else:
                continue
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        return [
            (legs[i], not isinstance(r, Exception) and r is not None, r)
            for i, r in enumerate(results)
        ]

    async def _execute_kalshi_limit(self, leg: ExecutionLeg) -> Optional[KalshiOrder]:
        """Execute a Kalshi limit order."""
        if not leg.ticker:
            leg.ticker = leg.market_id.split("|")[0] if "|" in leg.market_id else leg.market_id

        price_cents = int(leg.price * 100)

        return await self.kalshi.place_order(
            ticker=leg.ticker,
            side=leg.side,
            action=leg.action,
            count=leg.size,
            price=price_cents,
            order_type="limit",
            time_in_force="ioc",  # Immediate-or-cancel
        )

    async def _execute_polymarket_limit(self, leg: ExecutionLeg) -> Optional[PolymarketOrder]:
        """Execute a Polymarket limit order."""
        side = OrderSide.BUY if leg.action == "buy" else OrderSide.SELL

        return await self.polymarket.place_order(
            token_id=leg.token_id or leg.market_id,
            side=side,
            price=leg.price,
            size=Decimal(str(leg.size)),
            order_type="IOC",  # Immediate-or-cancel
            market_id=leg.market_id,
        )

    def _evaluate_execution(
        self,
        result: ExecutionResult,
        leg_results: List[Tuple[ExecutionLeg, bool, Optional[Any]]]
    ) -> ExecutionResult:
        """Evaluate execution results and calculate profit/loss."""
        all_success = True
        any_success = False
        total_cost = Decimal("0")

        for leg, success, order in leg_results:
            if success and order:
                any_success = True
                leg.status = "filled"

                # Extract fill info
                if isinstance(order, KalshiOrder):
                    leg.filled_size = order.filled_count or leg.size
                    leg.avg_price = Decimal(str(order.avg_price or order.price)) / 100
                elif isinstance(order, PolymarketOrder):
                    leg.filled_size = int(order.filled_size or order.size)
                    leg.avg_price = order.avg_price or order.price

                leg.order_id = order.order_id
                total_cost += (leg.avg_price or leg.price) * leg.filled_size
            else:
                all_success = False
                leg.status = "failed"

        result.total_cost = total_cost

        if all_success:
            result.status = ExecutionStatus.SUCCESS
            result.actual_profit = result.expected_payout - total_cost
        elif any_success:
            result.status = ExecutionStatus.PARTIAL
            result.error_message = "Partial fill - one leg failed"
            # Calculate potential loss from unhedged position
            result.actual_profit = -total_cost  # Worst case
        else:
            result.status = ExecutionStatus.FAILED
            result.error_message = "All legs failed"

        return result

    async def _cancel_pending_orders(self, legs: List[ExecutionLeg]):
        """Attempt to cancel any pending orders after timeout."""
        for leg in legs:
            if leg.order_id and leg.status == "pending":
                try:
                    if leg.platform == "kalshi":
                        await self.kalshi.cancel_order(leg.order_id)
                    elif leg.platform == "polymarket":
                        await self.polymarket.cancel_order(leg.order_id)
                except Exception as e:
                    logger.error(f"Failed to cancel order {leg.order_id}: {e}")

    def _update_stats(self, result: ExecutionResult):
        """Update execution statistics."""
        if result.status == ExecutionStatus.SUCCESS:
            self.stats["executions_successful"] += 1
            self.stats["total_profit"] += result.actual_profit
        elif result.status == ExecutionStatus.PARTIAL:
            self.stats["executions_partial"] += 1
            self.stats["total_loss"] += abs(result.actual_profit)
        else:
            self.stats["executions_failed"] += 1

    def _update_circuit_breaker(self, result: ExecutionResult):
        """Update circuit breaker state based on execution result."""
        if result.status in (ExecutionStatus.SUCCESS,):
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1

            # Open circuit after 5 consecutive failures
            if self._consecutive_failures >= 5:
                self._circuit_open = True
                self._circuit_open_until = time.time() + 300  # 5 minute cooldown
                logger.warning("Circuit breaker opened due to consecutive failures")

    def get_stats(self) -> Dict[str, Any]:
        """Get execution statistics."""
        return {
            **self.stats,
            "total_profit": float(self.stats["total_profit"]),
            "total_loss": float(self.stats["total_loss"]),
            "net_pnl": float(self.stats["total_profit"] - self.stats["total_loss"]),
            "success_rate": (
                self.stats["executions_successful"] / self.stats["executions_attempted"]
                if self.stats["executions_attempted"] > 0 else 0
            ),
            "circuit_breaker_open": self._circuit_open,
        }
