"""
Trade execution module for Kalshi and Polymarket.
Handles order placement, fills, and atomic execution.
"""

from .kalshi_executor import KalshiExecutor
from .polymarket_executor import PolymarketExecutor
from .atomic_executor import AtomicExecutor, ExecutionResult

__all__ = [
    "KalshiExecutor",
    "PolymarketExecutor",
    "AtomicExecutor",
    "ExecutionResult",
]
