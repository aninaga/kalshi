"""
Balance Manager

Tracks and manages funds across Kalshi and Polymarket.
Handles balance checks, allocation, and rebalancing alerts.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional, Callable, List

logger = logging.getLogger(__name__)


@dataclass
class PlatformBalance:
    """Balance information for a single platform."""
    platform: str
    available: Decimal
    reserved: Decimal  # In open orders
    total: Decimal
    currency: str
    last_updated: datetime

    @property
    def utilization(self) -> Decimal:
        """Percentage of funds in use."""
        if self.total == 0:
            return Decimal("0")
        return (self.reserved / self.total) * 100


@dataclass
class PortfolioSummary:
    """Summary of entire portfolio across platforms."""
    total_value: Decimal
    kalshi_balance: PlatformBalance
    polymarket_balance: PlatformBalance
    allocation_kalshi_pct: Decimal
    allocation_poly_pct: Decimal
    is_balanced: bool
    rebalance_needed: Decimal  # Amount to move (positive = to Kalshi)


class BalanceManager:
    """
    Manages balances and fund allocation across trading platforms.

    Features:
    - Real-time balance tracking
    - Position reservation for pending trades
    - Rebalancing alerts
    - Minimum balance enforcement
    """

    def __init__(
        self,
        kalshi_executor=None,
        polymarket_executor=None,
        target_allocation: Decimal = Decimal("0.5"),  # 50/50 default
        rebalance_threshold: Decimal = Decimal("0.1"),  # 10% drift triggers alert
        min_trade_balance: Decimal = Decimal("100"),  # Minimum per platform
    ):
        self.kalshi = kalshi_executor
        self.polymarket = polymarket_executor

        self.target_allocation = target_allocation
        self.rebalance_threshold = rebalance_threshold
        self.min_trade_balance = min_trade_balance

        # Current state
        self._kalshi_balance: Optional[PlatformBalance] = None
        self._polymarket_balance: Optional[PlatformBalance] = None

        # Reserved amounts (for pending trades)
        self._reserved = {
            "kalshi": Decimal("0"),
            "polymarket": Decimal("0"),
        }

        # Callbacks for alerts
        self._alert_callbacks: List[Callable] = []

        logger.info("BalanceManager initialized")

    def on_alert(self, callback: Callable[[str, Dict], None]):
        """Register callback for balance alerts."""
        self._alert_callbacks.append(callback)

    async def refresh_balances(self) -> PortfolioSummary:
        """Fetch current balances from both platforms."""
        tasks = []

        if self.kalshi:
            tasks.append(self._fetch_kalshi_balance())
        else:
            tasks.append(self._mock_kalshi_balance())

        if self.polymarket:
            tasks.append(self._fetch_polymarket_balance())
        else:
            tasks.append(self._mock_polymarket_balance())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Failed to fetch balance: {result}")
            elif i == 0:
                self._kalshi_balance = result
            else:
                self._polymarket_balance = result

        return self._calculate_summary()

    async def _fetch_kalshi_balance(self) -> PlatformBalance:
        """Fetch balance from Kalshi."""
        try:
            account = await self.kalshi.get_account()
            if account:
                balance = Decimal(str(account.get("balance", 0))) / 100
                return PlatformBalance(
                    platform="kalshi",
                    available=balance - self._reserved["kalshi"],
                    reserved=self._reserved["kalshi"],
                    total=balance,
                    currency="USD",
                    last_updated=datetime.now(timezone.utc),
                )
        except Exception as e:
            logger.error(f"Kalshi balance fetch failed: {e}")

        return self._mock_kalshi_balance()

    async def _fetch_polymarket_balance(self) -> PlatformBalance:
        """Fetch USDC balance from Polymarket/Polygon."""
        try:
            balance = await self.polymarket._get_usdc_balance()
            return PlatformBalance(
                platform="polymarket",
                available=balance - self._reserved["polymarket"],
                reserved=self._reserved["polymarket"],
                total=balance,
                currency="USDC",
                last_updated=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.error(f"Polymarket balance fetch failed: {e}")

        return self._mock_polymarket_balance()

    async def _mock_kalshi_balance(self) -> PlatformBalance:
        """Return mock balance when no executor configured."""
        return PlatformBalance(
            platform="kalshi",
            available=Decimal("0"),
            reserved=Decimal("0"),
            total=Decimal("0"),
            currency="USD",
            last_updated=datetime.now(timezone.utc),
        )

    async def _mock_polymarket_balance(self) -> PlatformBalance:
        """Return mock balance when no executor configured."""
        return PlatformBalance(
            platform="polymarket",
            available=Decimal("0"),
            reserved=Decimal("0"),
            total=Decimal("0"),
            currency="USDC",
            last_updated=datetime.now(timezone.utc),
        )

    def _calculate_summary(self) -> PortfolioSummary:
        """Calculate portfolio summary and check rebalancing needs."""
        kalshi = self._kalshi_balance or PlatformBalance(
            "kalshi", Decimal("0"), Decimal("0"), Decimal("0"), "USD",
            datetime.now(timezone.utc)
        )
        poly = self._polymarket_balance or PlatformBalance(
            "polymarket", Decimal("0"), Decimal("0"), Decimal("0"), "USDC",
            datetime.now(timezone.utc)
        )

        total = kalshi.total + poly.total

        if total == 0:
            return PortfolioSummary(
                total_value=Decimal("0"),
                kalshi_balance=kalshi,
                polymarket_balance=poly,
                allocation_kalshi_pct=Decimal("0"),
                allocation_poly_pct=Decimal("0"),
                is_balanced=True,
                rebalance_needed=Decimal("0"),
            )

        kalshi_pct = kalshi.total / total
        poly_pct = poly.total / total

        # Check if rebalancing needed
        drift = abs(kalshi_pct - self.target_allocation)
        is_balanced = drift <= self.rebalance_threshold

        # Calculate rebalance amount (positive = move to Kalshi)
        target_kalshi = total * self.target_allocation
        rebalance_needed = target_kalshi - kalshi.total

        summary = PortfolioSummary(
            total_value=total,
            kalshi_balance=kalshi,
            polymarket_balance=poly,
            allocation_kalshi_pct=kalshi_pct * 100,
            allocation_poly_pct=poly_pct * 100,
            is_balanced=is_balanced,
            rebalance_needed=rebalance_needed,
        )

        # Send alert if rebalancing needed
        if not is_balanced:
            self._send_alert("rebalance_needed", {
                "drift_pct": float(drift * 100),
                "rebalance_amount": float(abs(rebalance_needed)),
                "direction": "to_kalshi" if rebalance_needed > 0 else "to_polymarket",
            })

        return summary

    def reserve_funds(self, platform: str, amount: Decimal) -> bool:
        """
        Reserve funds for a pending trade.

        Returns True if funds available, False otherwise.
        """
        balance = (
            self._kalshi_balance if platform == "kalshi"
            else self._polymarket_balance
        )

        if not balance:
            return False

        if balance.available >= amount:
            self._reserved[platform] += amount
            if platform == "kalshi" and self._kalshi_balance:
                self._kalshi_balance.available -= amount
                self._kalshi_balance.reserved += amount
            elif self._polymarket_balance:
                self._polymarket_balance.available -= amount
                self._polymarket_balance.reserved += amount
            return True

        return False

    def release_funds(self, platform: str, amount: Decimal):
        """Release reserved funds after trade completion or cancellation."""
        self._reserved[platform] = max(
            Decimal("0"),
            self._reserved[platform] - amount
        )

        if platform == "kalshi" and self._kalshi_balance:
            self._kalshi_balance.available += amount
            self._kalshi_balance.reserved -= amount
        elif self._polymarket_balance:
            self._polymarket_balance.available += amount
            self._polymarket_balance.reserved -= amount

    def can_trade(self, kalshi_amount: Decimal, poly_amount: Decimal) -> bool:
        """Check if we have sufficient funds for a trade."""
        kalshi_ok = (
            self._kalshi_balance and
            self._kalshi_balance.available >= kalshi_amount and
            kalshi_amount >= self.min_trade_balance
        )
        poly_ok = (
            self._polymarket_balance and
            self._polymarket_balance.available >= poly_amount and
            poly_amount >= self.min_trade_balance
        )

        return kalshi_ok and poly_ok

    def get_max_trade_size(self) -> Decimal:
        """Get maximum trade size based on available funds."""
        kalshi_avail = (
            self._kalshi_balance.available if self._kalshi_balance
            else Decimal("0")
        )
        poly_avail = (
            self._polymarket_balance.available if self._polymarket_balance
            else Decimal("0")
        )

        # Limited by smaller balance (need funds on both sides)
        return min(kalshi_avail, poly_avail)

    def _send_alert(self, alert_type: str, data: Dict):
        """Send alert to registered callbacks."""
        for callback in self._alert_callbacks:
            try:
                callback(alert_type, data)
            except Exception as e:
                logger.error(f"Alert callback failed: {e}")

    def get_status(self) -> Dict:
        """Get current balance status."""
        return {
            "kalshi": {
                "available": float(self._kalshi_balance.available) if self._kalshi_balance else 0,
                "reserved": float(self._kalshi_balance.reserved) if self._kalshi_balance else 0,
                "total": float(self._kalshi_balance.total) if self._kalshi_balance else 0,
            },
            "polymarket": {
                "available": float(self._polymarket_balance.available) if self._polymarket_balance else 0,
                "reserved": float(self._polymarket_balance.reserved) if self._polymarket_balance else 0,
                "total": float(self._polymarket_balance.total) if self._polymarket_balance else 0,
            },
            "total_portfolio": float(
                (self._kalshi_balance.total if self._kalshi_balance else Decimal("0")) +
                (self._polymarket_balance.total if self._polymarket_balance else Decimal("0"))
            ),
            "max_trade_size": float(self.get_max_trade_size()),
        }
