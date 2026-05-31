import os
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Configuration for the arbitrage analysis system."""
    
    # Analysis Parameters
    MIN_PROFIT_THRESHOLD = 0.02  # 2% minimum profit threshold
    SIMILARITY_THRESHOLD = 0.85  # 85% similarity for market matching
    
    # API Configuration
    KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
    # Public REST market data needs no credentials. The live Kalshi WebSocket feed
    # DOES require KALSHI_API_KEY + an RSA key (KALSHI_PRIVATE_KEY_PATH or a repo-root
    # kalshi_private_key.pem); without them KalshiClient silently runs REST-only.
    
    POLYMARKET_GAMMA_BASE = "https://gamma-api.polymarket.com"
    POLYMARKET_CLOB_BASE = "https://clob.polymarket.com"

    # Browser User-Agent for ALL outbound HTTP/WS calls. Polymarket's Cloudflare
    # returns HTTP 403 to the default Python/aiohttp UA; a browser UA gets 200.
    # This was the root cause of the bot seeing ~100 Polymarket markets instead
    # of the full ~5,600+ tradeable universe (and therefore finding 0 arbs).
    HTTP_USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    # Fee endpoints (live)
    POLYMARKET_FEE_RATE_ENDPOINT = "https://clob.polymarket.com/fee-rate?token_id={token_id}"
    POLYMARKET_FEE_RATE_TTL_SECONDS = 300

    # Simulation settings (mock execution)
    SIMULATION_ENABLED = False
    SIMULATION_LATENCY_MS = 50
    SIMULATION_MAX_ORDERBOOK_AGE_SECONDS = 1.0
    SIMULATION_REQUIRE_LIVE_ORDERBOOKS = True

    # Confirmed PnL tracking settings
    CONFIRMED_PNL_REQUIRE_SETTLEMENT = True
    CONFIRMED_PNL_INCLUDE_SIMULATION = False

    # Data-quality controls for opportunity estimation
    REQUIRE_REAL_ORDERBOOKS_FOR_ESTIMATED = True
    POLYMARKET_ESTIMATED_FEE_RATE_BPS = 1000
    POLYMARKET_PRICE_MIN = 0.0
    POLYMARKET_PRICE_MAX = 1.0
    # (B3) Drop a cached book older than this from the ESTIMATED-opportunity path
    # (the simulation path uses SIMULATION_MAX_ORDERBOOK_AGE_SECONDS separately).
    ESTIMATED_MAX_ORDERBOOK_AGE_SECONDS = 30
    # (B13) Reject a cross-venue pairing whose two books' timestamps differ by
    # more than this, so both legs reflect a coherent as-of snapshot.
    MAX_CROSS_VENUE_SKEW_SECONDS = 5

    # Stream subscription coverage (bounded to avoid overwhelming feeds)
    KALSHI_STREAM_SUBSCRIPTION_LIMIT = 100
    POLYMARKET_STREAM_SUBSCRIPTION_MARKET_LIMIT = 300
    POLYMARKET_STREAM_SUBSCRIPTION_ASSET_LIMIT = 600
    
    # WebSocket Configuration for Real-time Data Streams (Phase 2)
    WEBSOCKET_CONFIG = {
        'kalshi': {
            'endpoint': 'wss://api.elections.kalshi.com/trade-api/ws/v2',
            'reconnect_interval': 5,
            'heartbeat_interval': 30,
            'max_reconnect_attempts': 10,
            'channels': ['ticker_v2', 'orderbook_delta', 'trade'],
            'enabled': True  # Enable Kalshi WebSocket with proper auth
        },
        'polymarket': {
            'endpoint': 'wss://ws-subscriptions-clob.polymarket.com/ws/market',
            'reconnect_interval': 5,
            'heartbeat_interval': 30,
            'max_reconnect_attempts': 10,
            'max_assets_per_subscribe': 300,
            'channels': ['market', 'user'],
            'auth_token': os.getenv('POLYMARKET_AUTH_TOKEN'),
            'enabled': True
        }
    }
    
    # Real-time Data Stream Settings (WebSocket-only mode)
    REALTIME_ENABLED = True
    STREAM_BUFFER_SIZE = 1000  # Max messages to buffer during disconnections
    STREAM_FRESHNESS_THRESHOLD = 10  # Seconds before data considered stale
    # Allow REST to fill price gaps the WebSocket cache misses. A single batch
    # scan barely warms the WS feed, so WS-only mode starves it; REST fallback
    # (budgeted, matched-pairs only) makes single scans actually see books.
    STREAM_FALLBACK_TO_REST = True

    # Orderbook REST fallback (used when WebSocket cache misses)
    ORDERBOOK_REST_FALLBACK = True
    ORDERBOOK_REST_MAX_PER_SECOND = 3
    # Max REST orderbook fetches per scan. Only matched pairs (post-similarity)
    # need real books — a few hundred at most — so this is sized to cover the
    # matched set rather than the full market universe.
    ORDERBOOK_REST_BUDGET_PER_SCAN = 600

    # Market discovery completeness. Kalshi paginates by cursor; loop until the
    # cursor is exhausted (cap is a safety bound, not the normal stop).
    KALSHI_MAX_DISCOVERY_PAGES = 200  # 200 * 200 = 40k markets ceiling
    # Polymarket discovery: paginate the Gamma catalog to completion.
    POLYMARKET_MAX_DISCOVERY_PAGES = 60  # 60 * 500 = 30k markets ceiling
    # Retry budget for a transient discovery fetch failure before giving up.
    DISCOVERY_FETCH_RETRIES = 4

    # Kalshi market cache management
    KALSHI_STALE_MARKET_TTL = 7200  # 2 hours - evict WS-only markets older than this
    KALSHI_MARKET_CLEANUP_INTERVAL = 1800  # 30 minutes between cleanups

    # Polymarket market refresh
    POLYMARKET_REFRESH_INTERVAL = 3600  # 1 hour between market refreshes

    # Fee Structure for analysis (more accurate rates)
    # Kalshi DOES charge a taker fee (~0.07*C*p*(1-p); see FeeModel.kalshi_taker_fee,
    # which is authoritative). This flat rate is only a coarse fallback — the
    # estimated-opportunity path uses the real fee curve, NOT this 0.0. Do not
    # treat Kalshi as fee-free.
    KALSHI_FEE_RATE = 0.00
    POLYMARKET_FEE_RATE = 0.01  # ~1% conservative fallback (BPS model handles real calculations)
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

    # --- Match verification settings (Phase A) ---
    # Cross-venue matching is purely lexical by default. These gates add
    # semantic safety on top: outcome-polarity resolution and
    # resolution-criteria equivalence. A false match in the EXECUTION path
    # fires two un-hedged legs, so verification is required before live.
    MATCH_VERIFICATION_ENABLED = True
    # Drop a candidate match whose verifier fails (different real-world event,
    # divergent resolution criteria, or unresolvable polarity).
    MATCH_DROP_ON_FAIL = True
    # When polarity can't be resolved, treat the match as failed (vs. passing
    # it through with polarity="unknown"). Conservative default.
    MATCH_REJECT_UNKNOWN_POLARITY = False
    # Live trading only fires on operator-allowlisted pairs (Phase D gate).
    MATCH_REQUIRE_ALLOWLIST_FOR_LIVE = True
    # Max difference between the two venues' close/resolution times.
    MATCH_MAX_CLOSE_TIME_SKEW_HOURS = 24
    # Operator allow/deny list of verified pairs.
    MATCH_ALLOWLIST_FILE = "matching/match_allowlist.json"

    # --- Execution settings ---
    EXECUTION_ENABLED = False  # Master kill switch — must explicitly enable
    EXECUTION_MODE = "paper"   # "paper" (log only) | "live" (real orders)
    KALSHI_ORDER_TTL_SECONDS = 5  # Short-lived limit order = pseudo-IOC
    POLYMARKET_ORDER_TYPE = "FOK"  # Fill-or-Kill
    MAX_POSITION_SIZE_USD = 100.0  # Per-leg capital cap
    MAX_DAILY_LOSS_USD = 50.0  # Stop trading after this cumulative loss
    EXECUTION_TIMEOUT_SECONDS = 10  # Max wait for order API response
    HEDGE_ENABLED = True  # Auto-hedge on partial fill (unwind filled leg)
    MIN_PROFIT_AFTER_FEES_USD = 0.50  # Re-verify profit before firing orders

    # --- Execution hardening (Phase B) ---
    # Distinct Polymarket order TTL (previously the Kalshi TTL was reused — a bug).
    POLYMARKET_ORDER_TTL_SECONDS = 5
    POLYMARKET_FLAT_TAKER_RATE = 0.02  # flat 2% taker piece (see FeeModel)
    # Idempotency + retries for transient order-placement failures.
    EXECUTION_MAX_RETRIES = 2
    EXECUTION_RETRY_BASE_DELAY = 0.5   # seconds; exponential backoff base
    # Fill polling: exponential backoff up to a total budget (replaces fixed 3s).
    FILL_POLL_BUDGET_SECONDS = 15.0
    FILL_POLL_BASE_INTERVAL = 0.25
    FILL_POLL_MAX_INTERVAL = 2.0
    # Hedge / unwind confirmation.
    HEDGE_TIMEOUT_SECONDS = 15
    HEDGE_PRICE_CONCESSION = 0.02      # cross this far through book for a fast unwind
    # Pre-trade risk gate (RiskEngine total_risk_score is 0..100).
    RISK_GATE_ENABLED = True
    MAX_RISK_SCORE = 60.0
    MIN_RISK_CONFIDENCE = 0.3
    # Execution-level circuit breaker (per venue) thresholds.
    EXECUTION_CB_FAILURE_THRESHOLD = 4
    EXECUTION_CB_RECOVERY_TIMEOUT = 120
    # Kill switch: presence of this file (under DATA_DIR) halts all order placement.
    EXECUTION_HALT_SENTINEL = "EXECUTION_HALT"
    # Pre-trade balance check (requires live account credentials).
    REQUIRE_BALANCE_CHECK = True

    # --- Paper-trading validation (Phase C) ---
    # Capture every execution (estimate + realized) for offline analysis.
    EXECUTION_CAPTURE_ENABLED = True
    EXECUTION_CAPTURE_FILE = "executions/executions.jsonl"

    # --- Live pilot (Phase D) ---
    # Extreme caps for the first real-money trades. The staged schedule is the
    # ceiling per leg; raise only after a clean daily reconciliation.
    LIVE_MAX_NOTIONAL_USD = 5.0          # hard per-leg notional clamp in live mode
    LIVE_MAX_CONCURRENT_POSITIONS = 1    # at most one in-flight live trade
    # Drift tolerance the paper run must clear before live (USD per trade).
    PAPER_MAX_EST_VS_REAL_DRIFT_USD = 0.25

    # Data Storage
    DATA_DIR = "market_data"
    OPPORTUNITIES_FILE = "arbitrage_opportunities.json"
    MARKETS_CACHE_FILE = "markets_cache.json"
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = "arbitrage_analysis.log"
    
    @classmethod
    def default_headers(cls, extra: Optional[dict] = None) -> dict:
        """Headers for every outbound HTTP/WS request (always sets User-Agent).

        Pass ``extra`` to merge venue auth headers; the UA is applied first so
        callers can still override it if they ever need to.
        """
        headers = {"User-Agent": cls.HTTP_USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

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
