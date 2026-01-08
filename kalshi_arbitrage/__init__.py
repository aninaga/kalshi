"""
Kalshi-Polymarket Arbitrage Analysis System
Comprehensive prediction market arbitrage detection and analysis

Implements multiple arbitrage strategies:
- Intra-market (YES + NO < $1)
- Multi-outcome (all outcomes < $1)
- Cross-platform (same market, different prices)

Production-ready WebSocket-based real-time detection.
"""

from .config import Config
from .market_analyzer import MarketAnalyzer
from .api_clients import KalshiClient, PolymarketClient
from .arbitrage_engine import (
    ArbitrageEngine,
    ArbitrageOpportunity,
    ArbitrageType,
)
from .realtime_scanner import RealTimeArbitrageScanner

__version__ = "4.1.0"
__all__ = [
    "Config",
    "MarketAnalyzer",
    "KalshiClient",
    "PolymarketClient",
    "ArbitrageEngine",
    "ArbitrageOpportunity",
    "ArbitrageType",
    "RealTimeArbitrageScanner"
]