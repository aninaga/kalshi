import os
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Configuration for the arbitrage analysis system."""
    
    # Analysis Parameters
    MIN_PROFIT_THRESHOLD = 0.02  # 2% minimum profit threshold
    SIMILARITY_THRESHOLD = 0.55  # 55% similarity for market matching
    
    # API Configuration
    KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
    # Note: Using public endpoints only - no credentials needed for read-only arbitrage detection
    
    POLYMARKET_GAMMA_BASE = "https://gamma-api.polymarket.com"
    POLYMARKET_CLOB_BASE = "https://clob.polymarket.com"
    
    # WebSocket Configuration for Real-time Data Streams (Phase 2)
    WEBSOCKET_CONFIG = {
        'kalshi': {
            'endpoint': 'wss://api.elections.kalshi.com/trade-api/ws/v2',
            'reconnect_interval': 5,
            'heartbeat_interval': 30,
            'max_reconnect_attempts': 10,
            'channels': ['ticker_v2', 'orderbook_delta', 'trade'],
            'enabled': False  # Correct implementation, but API key lacks WebSocket permissions
        },
        'polymarket': {
            'endpoint': 'wss://ws-subscriptions-clob.polymarket.com/ws/market',
            'reconnect_interval': 5,
            'heartbeat_interval': 30,
            'max_reconnect_attempts': 10,
            'channels': ['market', 'user'],
            'auth_token': os.getenv('POLYMARKET_AUTH_TOKEN'),
            'enabled': True
        }
    }
    
    # Real-time Data Stream Settings
    REALTIME_ENABLED = True
    STREAM_BUFFER_SIZE = 1000  # Max messages to buffer during disconnections
    STREAM_FRESHNESS_THRESHOLD = 10  # Seconds before data considered stale
    STREAM_FALLBACK_TO_REST = True  # Fall back to REST if streams fail
    
    # Fee Structure for analysis (more accurate rates)
    KALSHI_FEE_RATE = 0.00  # Kalshi has no trading fees
    POLYMARKET_FEE_RATE = 0.02  # ~2% gas + protocol fees (average estimate)
    POLYMARKET_GAS_FEE = 0.005  # ~0.5% typical gas fee
    POLYMARKET_PROTOCOL_FEE = 0.01  # ~1% protocol fee
    POLYMARKET_SLIPPAGE_TOLERANCE = 0.005  # 0.5% slippage tolerance
    
    # Volume and liquidity thresholds
    MIN_TRADEABLE_VOLUME = 10  # Minimum shares to consider for arbitrage
    MAX_POSITION_SIZE = 10000  # Maximum position size per opportunity
    
    # Slippage calculation parameters
    BASE_SLIPPAGE_RATE = 0.001  # 0.1% base slippage
    SLIPPAGE_IMPACT_FACTOR = 0.01  # Additional slippage per 100% of liquidity consumed
    MAX_SLIPPAGE_RATE = 0.05  # 5% maximum slippage cap
    
    # Scanning Configuration
    SCAN_INTERVAL_SECONDS = 30
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # seconds
    
    # Performance optimization settings
    MAX_CONCURRENT_API_CALLS = 20  # Limit concurrent API calls
    BATCH_PROCESSING_SIZE = 50     # Size of processing batches
    CACHE_CLEANUP_INTERVAL = 300   # Cleanup caches every 5 minutes
    
    # Completeness levels for lossless detection
    COMPLETENESS_LEVELS = {
        'FAST': {
            'max_matches_per_market': 3,
            'max_trades_per_opportunity': 10,
            'cache_ttl_prices': 5,
            'cache_ttl_orderbooks': 10,
            'expected_completeness': 0.95,
            'description': 'Optimized for speed, may miss 5% of opportunities'
        },
        'BALANCED': {
            'max_matches_per_market': 10,
            'max_trades_per_opportunity': 25,
            'cache_ttl_prices': 2,
            'cache_ttl_orderbooks': 5,
            'expected_completeness': 0.99,
            'description': 'Good balance of speed and completeness'
        },
        'LOSSLESS': {
            'max_matches_per_market': float('inf'),
            'max_trades_per_opportunity': float('inf'),
            'cache_ttl_prices': 0,
            'cache_ttl_orderbooks': 0,
            'expected_completeness': 1.0,
            'description': 'Complete analysis, no information loss'
        }
    }
    
    # Default completeness level
    DEFAULT_COMPLETENESS_LEVEL = 'BALANCED'
    
    # Data Storage
    DATA_DIR = "market_data"
    OPPORTUNITIES_FILE = "arbitrage_opportunities.json"
    MARKETS_CACHE_FILE = "markets_cache.json"
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = "arbitrage_analysis.log"
    
    @classmethod
    def setup_logging(cls):
        """Configure logging for the application."""
        os.makedirs(cls.DATA_DIR, exist_ok=True)
        
        logging.basicConfig(
            level=getattr(logging, cls.LOG_LEVEL),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(os.path.join(cls.DATA_DIR, cls.LOG_FILE))
            ]
        )
