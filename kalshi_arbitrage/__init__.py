"""
Kalshi-Polymarket Arbitrage Analysis System
Comprehensive prediction market arbitrage detection and analysis
"""

from .config import Config
from .market_analyzer import MarketAnalyzer
from .api_clients import KalshiClient, PolymarketClient

__version__ = "3.0.0"
__all__ = [
    "Config",
    "MarketAnalyzer",
    "KalshiClient",
    "PolymarketClient"
]