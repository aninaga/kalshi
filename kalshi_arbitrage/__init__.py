"""
Kalshi-Polymarket Arbitrage System
WebSocket-powered real-time arbitrage detection.

Strategies:
- Intra-market (YES + NO < $1)
- Multi-outcome (all outcomes < $1)
- Cross-platform (same market, different prices)
"""

from .config import Config
from .arbitrage_engine import (
    ArbitrageEngine,
    ArbitrageOpportunity,
    ArbitrageType,
)
from .realtime_scanner import RealTimeArbitrageScanner

__version__ = "5.0.0"
__all__ = [
    "Config",
    "ArbitrageEngine",
    "ArbitrageOpportunity",
    "ArbitrageType",
    "RealTimeArbitrageScanner"
]
