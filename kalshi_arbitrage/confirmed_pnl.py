import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


@dataclass
class FillRecord:
    """One confirmed fill event for a buy or sell leg."""

    fill_id: str
    order_id: Optional[str]
    trade_id: Optional[str]
    platform: str
    market_id: Optional[str]
    token_id: Optional[str]
    price: float
    size: float
    fee: float
    timestamp: float
    source: str = "exchange"

    def notional(self) -> float:
        return max(self.price, 0.0) * max(self.size, 0.0)


@dataclass
class ExecutionRecord:
    """Two-legged execution lifecycle for one arbitrage opportunity."""

    execution_id: str
    opportunity_id: str
    buy_platform: str
    sell_platform: str
    created_at: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    buy_fills: List[FillRecord] = field(default_factory=list)
    sell_fills: List[FillRecord] = field(default_factory=list)
    buy_confirmed_at: Optional[float] = None
    sell_confirmed_at: Optional[float] = None
    settlement_id: Optional[str] = None
    settled_at: Optional[float] = None

    def total_buy_size(self) -> float:
        return sum(max(fill.size, 0.0) for fill in self.buy_fills)

    def total_sell_size(self) -> float:
        return sum(max(fill.size, 0.0) for fill in self.sell_fills)

    def matched_size(self) -> float:
        return min(self.total_buy_size(), self.total_sell_size())

    def total_buy_notional(self) -> float:
        return sum(fill.notional() for fill in self.buy_fills)

    def total_sell_notional(self) -> float:
        return sum(fill.notional() for fill in self.sell_fills)

    def total_buy_fees(self) -> float:
        return sum(max(fill.fee, 0.0) for fill in self.buy_fills)

    def total_sell_fees(self) -> float:
        return sum(max(fill.fee, 0.0) for fill in self.sell_fills)

    def has_confirmed_legs(self) -> bool:
        return self.buy_confirmed_at is not None and self.sell_confirmed_at is not None

    def is_settled(self) -> bool:
        return self.settled_at is not None

    def has_simulation_source(self) -> bool:
        all_fills = self.buy_fills + self.sell_fills
        return any((fill.source or "").lower() == "simulation" for fill in all_fills)

    def realized_pnl(self) -> float:
        """Realized PnL on matched volume only, with pro-rated fees."""
        matched = self.matched_size()
        if matched <= 0.0:
            return 0.0

        buy_size = self.total_buy_size()
        sell_size = self.total_sell_size()
        if buy_size <= 0.0 or sell_size <= 0.0:
            return 0.0

        buy_notional = self.total_buy_notional() * (matched / buy_size)
        sell_notional = self.total_sell_notional() * (matched / sell_size)
        buy_fees = self.total_buy_fees() * (matched / buy_size)
        sell_fees = self.total_sell_fees() * (matched / sell_size)
        return sell_notional - buy_notional - buy_fees - sell_fees


class ConfirmedPnLTracker:
    """Tracks execution lifecycle and computes settled confirmed PnL."""

    def __init__(self, allow_simulated_confirmed: bool = False, max_trade_tape_events: int = 5000):
        self.allow_simulated_confirmed = bool(allow_simulated_confirmed)
        self.executions: Dict[str, ExecutionRecord] = {}
        self._fill_ids: Set[str] = set()
        self.trade_tape: Deque[Dict[str, Any]] = deque(maxlen=max_trade_tape_events)

    def register_execution(
        self,
        opportunity_id: str,
        buy_platform: str,
        sell_platform: str,
        execution_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[float] = None,
    ) -> str:
        execution_id = execution_id or f"exec-{uuid.uuid4().hex}"
        if execution_id in self.executions:
            existing = self.executions[execution_id]
            if metadata:
                existing.metadata.update(metadata)
            return execution_id

        self.executions[execution_id] = ExecutionRecord(
            execution_id=execution_id,
            opportunity_id=opportunity_id,
            buy_platform=buy_platform,
            sell_platform=sell_platform,
            created_at=created_at if created_at is not None else time.time(),
            metadata=metadata or {},
        )
        return execution_id

    def record_fill(
        self,
        execution_id: str,
        leg: str,
        fill_id: Optional[str],
        platform: str,
        price: float,
        size: float,
        fee: float,
        order_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        market_id: Optional[str] = None,
        token_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        source: str = "exchange",
        auto_confirm: bool = False,
    ) -> bool:
        execution = self.executions.get(execution_id)
        if not execution:
            raise KeyError(f"Unknown execution_id: {execution_id}")

        if leg not in {"buy", "sell"}:
            raise ValueError(f"Unsupported leg '{leg}'")

        size = max(_safe_float(size), 0.0)
        price = max(_safe_float(price), 0.0)
        fee = max(_safe_float(fee), 0.0)
        if size <= 0.0:
            return False

        fill_id = fill_id or f"fill-{uuid.uuid4().hex}"
        if fill_id in self._fill_ids:
            return False

        fill = FillRecord(
            fill_id=fill_id,
            order_id=order_id,
            trade_id=trade_id,
            platform=platform,
            market_id=market_id,
            token_id=token_id,
            price=price,
            size=size,
            fee=fee,
            timestamp=timestamp if timestamp is not None else time.time(),
            source=source,
        )
        if leg == "buy":
            execution.buy_fills.append(fill)
        else:
            execution.sell_fills.append(fill)

        self._fill_ids.add(fill_id)

        if auto_confirm:
            self.mark_leg_confirmed(execution_id, leg, confirmed_at=fill.timestamp)

        return True

    def mark_leg_confirmed(
        self,
        execution_id: str,
        leg: str,
        confirmed_at: Optional[float] = None,
    ) -> None:
        execution = self.executions.get(execution_id)
        if not execution:
            raise KeyError(f"Unknown execution_id: {execution_id}")
        ts = confirmed_at if confirmed_at is not None else time.time()

        if leg == "buy":
            execution.buy_confirmed_at = ts
        elif leg == "sell":
            execution.sell_confirmed_at = ts
        else:
            raise ValueError(f"Unsupported leg '{leg}'")

    def mark_settled(
        self,
        execution_id: str,
        settlement_id: Optional[str] = None,
        settled_at: Optional[float] = None,
        require_confirmed_legs: bool = True,
    ) -> bool:
        execution = self.executions.get(execution_id)
        if not execution:
            raise KeyError(f"Unknown execution_id: {execution_id}")

        if require_confirmed_legs and not execution.has_confirmed_legs():
            return False

        execution.settlement_id = settlement_id or execution.settlement_id or f"settle-{uuid.uuid4().hex}"
        execution.settled_at = settled_at if settled_at is not None else time.time()
        return True

    def record_execution_receipt(
        self,
        opportunity_id: str,
        buy_platform: str,
        sell_platform: str,
        buy_price: float,
        buy_size: float,
        buy_fee: float,
        sell_price: float,
        sell_size: float,
        sell_fee: float,
        execution_id: Optional[str] = None,
        buy_order_id: Optional[str] = None,
        sell_order_id: Optional[str] = None,
        buy_fill_id: Optional[str] = None,
        sell_fill_id: Optional[str] = None,
        buy_trade_id: Optional[str] = None,
        sell_trade_id: Optional[str] = None,
        settlement_id: Optional[str] = None,
        market_id: Optional[str] = None,
        token_id: Optional[str] = None,
        source: str = "exchange",
        metadata: Optional[Dict[str, Any]] = None,
        mark_confirmed: bool = True,
        mark_settled: bool = True,
        timestamp: Optional[float] = None,
    ) -> str:
        execution_id = self.register_execution(
            opportunity_id=opportunity_id,
            buy_platform=buy_platform,
            sell_platform=sell_platform,
            execution_id=execution_id,
            metadata=metadata,
            created_at=timestamp,
        )

        self.record_fill(
            execution_id=execution_id,
            leg="buy",
            fill_id=buy_fill_id,
            platform=buy_platform,
            price=buy_price,
            size=buy_size,
            fee=buy_fee,
            order_id=buy_order_id,
            trade_id=buy_trade_id,
            market_id=market_id,
            token_id=token_id,
            timestamp=timestamp,
            source=source,
            auto_confirm=mark_confirmed,
        )
        self.record_fill(
            execution_id=execution_id,
            leg="sell",
            fill_id=sell_fill_id,
            platform=sell_platform,
            price=sell_price,
            size=sell_size,
            fee=sell_fee,
            order_id=sell_order_id,
            trade_id=sell_trade_id,
            market_id=market_id,
            token_id=token_id,
            timestamp=timestamp,
            source=source,
            auto_confirm=mark_confirmed,
        )

        if mark_confirmed:
            self.mark_leg_confirmed(execution_id, "buy", confirmed_at=timestamp)
            self.mark_leg_confirmed(execution_id, "sell", confirmed_at=timestamp)

        if mark_settled:
            self.mark_settled(
                execution_id,
                settlement_id=settlement_id,
                settled_at=timestamp,
                require_confirmed_legs=mark_confirmed,
            )

        return execution_id

    def ingest_trade_tape_event(
        self,
        platform: str,
        market_id: str,
        data: Dict[str, Any],
        timestamp: Optional[float] = None,
    ) -> None:
        self.trade_tape.append(
            {
                "platform": platform,
                "market_id": market_id,
                "data": data,
                "timestamp": timestamp if timestamp is not None else time.time(),
            }
        )

    def reconcile_leg_from_trade_tape(
        self,
        execution_id: str,
        leg: str,
        platform: str,
        market_id: Optional[str],
        max_age_seconds: float = 120.0,
        price_tolerance: float = 0.005,
        size_tolerance: float = 0.25,
    ) -> bool:
        execution = self.executions.get(execution_id)
        if not execution:
            return False

        fills = execution.buy_fills if leg == "buy" else execution.sell_fills
        if not fills:
            return False

        expected_price = sum(fill.price * fill.size for fill in fills) / max(sum(fill.size for fill in fills), 1e-9)
        expected_size = sum(fill.size for fill in fills)
        now = time.time()

        for event in reversed(self.trade_tape):
            if now - event["timestamp"] > max_age_seconds:
                break
            if event["platform"] != platform:
                continue
            if market_id and event["market_id"] != market_id:
                continue

            data = event.get("data", {})
            trade_price = _safe_float(data.get("price"))
            trade_size = _safe_float(data.get("count", data.get("size", 0.0)))
            if trade_price <= 0.0 or trade_size <= 0.0:
                continue

            if abs(trade_price - expected_price) > price_tolerance:
                continue

            relative_size_error = abs(trade_size - expected_size) / max(expected_size, 1.0)
            if relative_size_error > size_tolerance:
                continue

            self.mark_leg_confirmed(execution_id, leg, confirmed_at=event["timestamp"])
            return True

        return False

    def _is_counted_execution(self, execution: ExecutionRecord, since_timestamp: Optional[float] = None) -> bool:
        if not execution.is_settled() or not execution.has_confirmed_legs():
            return False
        if since_timestamp is not None and (execution.settled_at or 0.0) < since_timestamp:
            return False
        if (not self.allow_simulated_confirmed) and execution.has_simulation_source():
            return False
        return True

    def summarize(self, scan_interval_seconds: float, since_timestamp: Optional[float] = None) -> Dict[str, Any]:
        interval = max(_safe_float(scan_interval_seconds, 1.0), 1.0)
        scans_per_hour = 3600.0 / interval

        settled_window = [
            execution
            for execution in self.executions.values()
            if self._is_counted_execution(execution, since_timestamp=since_timestamp)
        ]

        settled_all = [
            execution
            for execution in self.executions.values()
            if self._is_counted_execution(execution, since_timestamp=None)
        ]

        window_realized = float(sum(execution.realized_pnl() for execution in settled_window))
        cumulative_realized = float(sum(execution.realized_pnl() for execution in settled_all))

        profitable_window_count = sum(1 for execution in settled_window if execution.realized_pnl() > 0.0)
        pending_count = sum(1 for execution in self.executions.values() if not execution.is_settled())

        return {
            "realized_pnl_usd": window_realized,
            "realized_pnl_per_hour_usd": window_realized * scans_per_hour,
            "realized_pnl_per_day_usd": window_realized * scans_per_hour * 24.0,
            "realized_pnl_opportunity_count": profitable_window_count,
            "settled_execution_count": len(settled_window),
            "pending_execution_count": pending_count,
            "cumulative_realized_pnl_usd": cumulative_realized,
            "cumulative_settled_execution_count": len(settled_all),
            "counting_simulated_confirmations": bool(self.allow_simulated_confirmed),
        }
