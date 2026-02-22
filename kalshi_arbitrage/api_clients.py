import asyncio
import json
import logging
import os
import time
from typing import Optional, Dict, List, Any
from .config import Config
from .websocket_client import RealTimeDataManager, KalshiWebSocketClient, PolymarketWebSocketClient

logger = logging.getLogger(__name__)

class KalshiClient:
    """Hybrid Kalshi client: REST market discovery + WebSocket real-time data."""
    
    def __init__(self):
        self.websocket_client = None
        self.auth_token = None
        self.markets_cache = {}
        self.price_cache = {}
        self.orderbook_cache = {}
        self.discovered_tickers = []  # Store all discovered market tickers
    
    def _cents_to_dollars(self, value, default: float = 0.5) -> float:
        """Convert Kalshi cent pricing to dollars with safe fallback."""
        if value is None:
            return default
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value / 100.0 if value > 1 else value

    def _safe_float(self, value: Any) -> Optional[float]:
        """Parse float safely."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    
    def _normalize_kalshi_levels(self, levels: List[Any]) -> List[Dict[str, float]]:
        """Normalize Kalshi orderbook levels to {price, size} in dollars."""
        normalized = []
        if not levels:
            return normalized
        for level in levels:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]
            elif isinstance(level, dict):
                price = level.get('price')
                size = level.get('size') if level.get('size') is not None else level.get('quantity')
            else:
                continue
            try:
                price = float(price)
                size = float(size)
            except (TypeError, ValueError):
                continue
            price = self._cents_to_dollars(price, default=0.0)
            normalized.append({'price': price, 'size': size})
        return normalized
        
    async def initialize(self) -> bool:
        """Initialize with REST market discovery + WebSocket connection."""
        try:
            # Step 1: Bootstrap market discovery via REST API
            logger.info("Discovering Kalshi markets via REST API...")
            await self._discover_markets_via_rest()

            # Step 2: Initialize WebSocket connection (requires auth)
            if not os.getenv('KALSHI_API_KEY'):
                logger.warning("KALSHI_API_KEY not set; skipping Kalshi WebSocket connection (REST-only mode)")
                logger.info(f"Kalshi client initialized with {len(self.markets_cache)} markets discovered via REST")
                return True

            config = Config.WEBSOCKET_CONFIG['kalshi']
            self.websocket_client = KalshiWebSocketClient(config, self.auth_token)

            # Set up data handlers
            self.websocket_client.add_message_handler('ticker', self._handle_price_update)
            self.websocket_client.add_message_handler('orderbook', self._handle_orderbook_update)

            await self.websocket_client.connect()

            # Subscribe to all markets after connection
            if self.websocket_client.is_connected:
                channels = Config.WEBSOCKET_CONFIG.get('kalshi', {}).get('channels', ['ticker_v2'])
                await self.websocket_client.subscribe_to_all_markets(channels)
                logger.info("Subscribed to all Kalshi markets")

            logger.info(f"Kalshi client initialized with {len(self.markets_cache)} markets discovered via REST")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Kalshi client: {e}")
            return False
    
    async def _handle_price_update(self, message):
        """Handle real-time price updates."""
        market_ticker = message.data.get('market_ticker')
        if not market_ticker:
            return
            
        # Store price data
        self.price_cache[market_ticker] = {
            'yes_price': self._cents_to_dollars(message.data.get('yes_bid')),
            'no_price': self._cents_to_dollars(message.data.get('no_bid')),
            'yes_ask': self._cents_to_dollars(message.data.get('yes_ask')),
            'yes_bid': self._cents_to_dollars(message.data.get('yes_bid')),
            'timestamp': message.timestamp
        }
        
        # Also store basic market info
        if market_ticker not in self.markets_cache:
            self.markets_cache[market_ticker] = {
                'id': message.market_id,
                'ticker': market_ticker,
                'title': market_ticker,  # We'll use ticker as title for now
                'platform': 'kalshi',
                'status': 'active',
                'clean_title': self._clean_title(market_ticker)
            }
    
    def _clean_title(self, title: str) -> str:
        """Basic title cleaning."""
        return title.replace('-', ' ').replace('KX', '').lower().strip()
    
    async def _discover_markets_via_rest(self):
        """Discover all markets via REST API."""
        import aiohttp
        
        try:
            async with aiohttp.ClientSession() as session:
                # Get all events first
                events_url = f"{Config.KALSHI_API_BASE}/events"
                async with session.get(events_url) as response:
                    if response.status == 200:
                        events_data = await response.json()
                        events = events_data.get('events', [])
                        logger.info(f"Discovered {len(events)} Kalshi events")
                    else:
                        logger.warning(f"Failed to fetch Kalshi events: {response.status}")
                        events = []
                
                # Get markets for each event (or use global markets endpoint)
                markets_url = f"{Config.KALSHI_API_BASE}/markets"
                all_markets = []
                
                # Paginate through markets with reasonable limit for performance
                cursor = None
                page_count = 0
                max_pages = 50  # Reasonable limit: 50 pages * 200 = 10,000 markets max
                rate_limit_retries = 0
                max_rate_limit_retries = 8
                timeout = aiohttp.ClientTimeout(total=15)  # 15 second timeout per request
                
                while page_count < max_pages:
                    params = {'limit': 200}  # Max limit per request
                    if cursor:
                        params['cursor'] = cursor
                    
                    try:
                        async with session.get(markets_url, params=params, timeout=timeout) as response:
                            if response.status == 200:
                                data = await response.json()
                                markets = data.get('markets', [])
                                rate_limit_retries = 0
                                
                                if not markets:
                                    logger.info(f"No more markets found after page {page_count}")
                                    break
                                    
                                all_markets.extend(markets)
                                cursor = data.get('cursor')
                                page_count += 1
                                logger.info(f"Fetched page {page_count}, {len(markets)} markets, total: {len(all_markets)}")
                                
                                # Break if no cursor (end of data)
                                if not cursor:
                                    break
                            elif response.status == 429:
                                rate_limit_retries += 1
                                if rate_limit_retries > max_rate_limit_retries:
                                    logger.warning(
                                        "Kalshi markets pagination hit repeated rate limits; "
                                        f"stopping after {max_rate_limit_retries} retries"
                                    )
                                    break
                                retry_after_header = response.headers.get('Retry-After')
                                retry_after = self._safe_float(retry_after_header)
                                backoff_seconds = retry_after if retry_after and retry_after > 0 else min(30.0, float(2 ** rate_limit_retries))
                                logger.warning(
                                    f"Kalshi markets rate-limited (429); retrying page {page_count + 1} "
                                    f"in {backoff_seconds:.1f}s (retry {rate_limit_retries}/{max_rate_limit_retries})"
                                )
                                await asyncio.sleep(backoff_seconds)
                                continue
                            else:
                                logger.warning(f"Failed to fetch Kalshi markets: {response.status}")
                                break
                    except asyncio.TimeoutError:
                        logger.warning(f"Timeout on page {page_count + 1}, retrying once...")
                        # Retry once before giving up
                        try:
                            async with session.get(markets_url, params=params, timeout=timeout) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    markets = data.get('markets', [])
                                    if markets:
                                        all_markets.extend(markets)
                                        cursor = data.get('cursor')
                                        page_count += 1
                                        logger.info(f"Retry successful: page {page_count}, total: {len(all_markets)}")
                                        if not cursor:
                                            break
                                        continue
                        except Exception as retry_e:
                            logger.warning(f"Retry failed: {retry_e}")
                        logger.info(f"Stopping pagination after timeout, captured {len(all_markets)} markets")
                        break
                    except Exception as e:
                        logger.warning(f"Error fetching page {page_count + 1}: {e}, continuing...")
                        break
                
                logger.info(f"Discovered {len(all_markets)} total Kalshi markets via REST")
                
                # Process and cache the discovered markets
                for market in all_markets:
                    ticker = market.get('ticker')
                    if ticker:
                        self.discovered_tickers.append(ticker)
                        self.markets_cache[ticker] = {
                            'id': market.get('id'),
                            'ticker': ticker,
                            'title': market.get('title', ticker),
                            'platform': 'kalshi',
                            'status': market.get('status', 'active'),
                            'close_time': market.get('close_time'),
                            'clean_title': self._clean_title(market.get('title', ticker)),
                            'raw_data': market
                        }
                
                logger.info(f"Cached {len(self.markets_cache)} Kalshi markets from REST discovery")
                
        except Exception as e:
            logger.error(f"Failed to discover Kalshi markets via REST: {e}")
            # Continue with empty cache - WebSocket might still provide some data
    
    async def _handle_orderbook_update(self, message):
        """Handle real-time orderbook updates."""
        market_id = message.market_id
        self.orderbook_cache[market_id] = {
            'yes_asks': self._normalize_kalshi_levels(message.data.get('yes_asks', [])),
            'yes_bids': self._normalize_kalshi_levels(message.data.get('yes_bids', [])),
            'no_asks': self._normalize_kalshi_levels(message.data.get('no_asks', [])),
            'no_bids': self._normalize_kalshi_levels(message.data.get('no_bids', [])),
            'timestamp': message.timestamp
        }
    
    async def get_all_markets(self) -> List[Dict[str, Any]]:
        """Get all markets from REST discovery + WebSocket data."""
        # Return markets from cache (populated by REST discovery and enhanced by WebSocket)
        markets = list(self.markets_cache.values())
        logger.info(f"Returning {len(markets)} Kalshi markets from hybrid REST+WebSocket cache")
        return markets
    
    async def get_market_details(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Get market details from cache."""
        return self.markets_cache.get(market_ticker)
    
    async def get_market_prices(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Get market prices from cache or REST fallback."""
        # First try WebSocket cache
        cached_prices = self.price_cache.get(market_ticker)
        if cached_prices:
            return cached_prices
        
        # Fallback to REST API for missing prices
        return await self._fetch_prices_via_rest(market_ticker)
    
    async def _fetch_prices_via_rest(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Fetch market prices via REST API as fallback."""
        import aiohttp
        
        try:
            market = self.markets_cache.get(market_ticker)
            if not market:
                return None
            
            market_id = market.get('id')
            if not market_id:
                return None
            
            async with aiohttp.ClientSession() as session:
                # Get market data including current prices
                url = f"{Config.KALSHI_API_BASE}/markets/{market_id}"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        market_data = data.get('market', {})
                        
                        # Extract price information
                        yes_bid = market_data.get('yes_bid', 0)
                        yes_ask = market_data.get('yes_ask', 0) 
                        
                        prices = {
                            'yes_price': yes_bid / 100.0 if yes_bid else 0.5,
                            'no_price': (100 - yes_ask) / 100.0 if yes_ask else 0.5,
                            'yes_ask': yes_ask / 100.0 if yes_ask else 0.5,
                            'yes_bid': yes_bid / 100.0 if yes_bid else 0.5,
                            'timestamp': time.time(),
                            'source': 'rest_fallback'
                        }
                        
                        # Cache the result
                        self.price_cache[market_ticker] = prices
                        return prices
                    
        except Exception as e:
            logger.warning(f"Failed to fetch prices via REST for {market_ticker}: {e}")
        
        return None
    
    async def get_market_orderbook(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Get market orderbook from cache."""
        return self.orderbook_cache.get(market_ticker)

class PolymarketClient:
    """Hybrid Polymarket client: REST bootstrap + WebSocket real-time data."""
    
    def __init__(self):
        self.websocket_client = None
        self.markets_cache = {}
        self.price_cache = {}
        self.orderbook_cache = {}
        self.asset_ids = []  # Store asset IDs for WebSocket subscription
        self.token_to_market = {}
        self.token_outcome_map = {}
        self.stream_quality_stats = {
            'price_updates_received': 0,
            'price_updates_accepted': 0,
            'price_updates_dropped': 0,
            'orderbook_updates_received': 0,
            'orderbook_updates_accepted': 0,
            'orderbook_updates_dropped': 0,
            'orphan_token_updates': 0,
            'invalid_price_updates': 0,
            'invalid_orderbook_levels': 0,
            'invalid_orderbook_updates': 0,
            'normalized_orderbook_levels': 0,
        }

    def _safe_float(self, value: Any) -> Optional[float]:
        """Parse a float safely."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _is_valid_probability(self, value: float) -> bool:
        """Check if probability is in [0, 1]."""
        return (
            value is not None
            and Config.POLYMARKET_PRICE_MIN <= value <= Config.POLYMARKET_PRICE_MAX
        )

    def _normalize_polymarket_levels(self, levels: List[Any], side: str) -> List[Dict[str, float]]:
        """Normalize Polymarket orderbook levels to {price, size} floats."""
        normalized = []
        if not levels:
            return normalized
        for level in levels:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]
            elif isinstance(level, dict):
                price = level.get('price')
                size = level.get('size') if level.get('size') is not None else level.get('quantity')
            else:
                self.stream_quality_stats['invalid_orderbook_levels'] += 1
                continue
            price = self._safe_float(price)
            size = self._safe_float(size)
            if not self._is_valid_probability(price) or size is None or size <= 0:
                self.stream_quality_stats['invalid_orderbook_levels'] += 1
                continue
            normalized.append({'price': price, 'size': size})
            self.stream_quality_stats['normalized_orderbook_levels'] += 1
        reverse = side == 'bids'
        normalized.sort(key=lambda level: level['price'], reverse=reverse)
        return normalized

    def _resolve_market_id(self, market_id: str, token_id: Optional[str]) -> Optional[str]:
        """Resolve market_id from token_id if needed."""
        if market_id:
            return market_id
        if token_id and token_id in self.token_to_market:
            return self.token_to_market[token_id]
        return None
        
    async def initialize(self) -> bool:
        """Initialize with REST bootstrap + WebSocket connection."""
        try:
            # Step 1: Bootstrap markets via REST API
            logger.info("Bootstrapping Polymarket markets via REST API...")
            await self._bootstrap_markets_via_rest()
            
            # Step 2: Initialize WebSocket connection if we have asset IDs
            if self.asset_ids:
                config = Config.WEBSOCKET_CONFIG['polymarket']
                self.websocket_client = PolymarketWebSocketClient(config)
                
                # Set up data handlers
                self.websocket_client.add_message_handler('price', self._handle_price_update)
                self.websocket_client.add_message_handler('orderbook', self._handle_orderbook_update)
                
                await self.websocket_client.connect()
                
                # Subscribe to discovered markets with configurable bounded coverage.
                unique_assets = list(dict.fromkeys(asset_id for asset_id in self.asset_ids if asset_id))
                subscription_limit = min(len(unique_assets), Config.POLYMARKET_STREAM_SUBSCRIPTION_ASSET_LIMIT)
                await self.websocket_client.subscribe_to_markets(unique_assets[:subscription_limit])
                logger.info(f"Subscribed to {subscription_limit} Polymarket assets via WebSocket")
            
            logger.info(f"Polymarket client initialized with {len(self.markets_cache)} markets")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            return False
    
    async def _bootstrap_markets_via_rest(self):
        """Bootstrap market discovery using REST API with full pagination."""
        import aiohttp
        import json
        
        try:
            # Use Polymarket Gamma API to discover markets with pagination
            url = f"{Config.POLYMARKET_GAMMA_BASE}/markets"
            all_markets = []
            offset = 0
            limit = 500  # Max limit per request
            
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while True:
                    params = {
                        "active": "true",
                        "closed": "false", 
                        "limit": limit,
                        "offset": offset
                    }
                    
                    try:
                        async with session.get(url, params=params) as response:
                            response.raise_for_status()
                            markets_data = await response.json()
                        
                        if not markets_data or not isinstance(markets_data, list) or len(markets_data) == 0:
                            break
                        
                        all_markets.extend(markets_data)
                        logger.info(f"Fetched {len(markets_data)} markets at offset {offset}, total: {len(all_markets)}")
                        
                        # If we got fewer than the limit, we've reached the end
                        if len(markets_data) < limit:
                            break
                            
                        offset += limit
                        
                    except Exception as e:
                        logger.warning(f"Error fetching markets at offset {offset}: {e}")
                        break
            
            if not all_markets:
                logger.warning("No markets returned from Polymarket REST API")
                return
            
            markets_data = all_markets
            
            # Process and cache markets
            for market in markets_data:
                try:
                    # Parse market data
                    market_id = market.get('id')
                    question = market.get('question', '')
                    outcomes = market.get('outcomes', '[]')
                    clob_token_ids = market.get('clobTokenIds', '[]')
                    outcome_prices = market.get('outcomePrices', '[]')
                    
                    # Parse JSON strings if needed
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(clob_token_ids, str):
                        clob_token_ids = json.loads(clob_token_ids)
                    if isinstance(outcome_prices, str):
                        outcome_prices = json.loads(outcome_prices)
                    
                    if not all([market_id, question, outcomes, clob_token_ids]):
                        continue
                    
                    # Store market data
                    self.markets_cache[market_id] = {
                        'id': market_id,
                        'title': question,
                        'clean_title': self._clean_title(question),
                        'platform': 'polymarket',
                        'status': 'active',
                        'active': True,  # Explicitly set as active since we filter for active markets
                        'outcomes': outcomes,
                        'clob_token_ids': clob_token_ids,
                        'outcome_prices': outcome_prices,
                        'raw_data': market
                    }
                    
                    # Store asset IDs for WebSocket subscription
                    self.asset_ids.extend(clob_token_ids)
                    
                    # Store initial price data
                    for i, (outcome, price_str, token_id) in enumerate(zip(outcomes, outcome_prices, clob_token_ids)):
                        if market_id not in self.price_cache:
                            self.price_cache[market_id] = {}
                        
                        price = self._safe_float(price_str)
                        if not self._is_valid_probability(price):
                            continue
                        self.price_cache[market_id][token_id] = {
                            'outcome': outcome,
                            'buy_price': price,
                            'sell_price': price,
                            'mid_price': price,
                            'timestamp': time.time()
                        }
                        self.token_to_market[token_id] = market_id
                        self.token_outcome_map[token_id] = outcome
                
                except Exception as e:
                    logger.debug(f"Error processing Polymarket market: {e}")
                    continue
            
            self.asset_ids = list(dict.fromkeys(asset_id for asset_id in self.asset_ids if asset_id))
            logger.info(f"Bootstrapped {len(self.markets_cache)} Polymarket markets via REST API")
            logger.info(f"Collected {len(self.asset_ids)} unique asset IDs for WebSocket subscription")
            
        except Exception as e:
            logger.error(f"Failed to bootstrap Polymarket markets: {e}")
    
    def _clean_title(self, title: str) -> str:
        """Basic title cleaning."""
        if not title:
            return ""
        return title.lower().strip()
    
    async def _handle_price_update(self, message):
        """Handle real-time price updates."""
        self.stream_quality_stats['price_updates_received'] += 1
        token_id = message.data.get('asset_id') or message.data.get('token_id') or message.data.get('outcome')
        market_id = self._resolve_market_id(message.market_id, token_id)
        if not market_id:
            self.stream_quality_stats['price_updates_dropped'] += 1
            self.stream_quality_stats['orphan_token_updates'] += 1
            return

        price = self._safe_float(message.data.get('price'))
        if not self._is_valid_probability(price):
            self.stream_quality_stats['price_updates_dropped'] += 1
            self.stream_quality_stats['invalid_price_updates'] += 1
            return

        if market_id not in self.price_cache:
            self.price_cache[market_id] = {}

        self.price_cache[market_id][token_id or 'default'] = {
            'outcome': message.data.get('outcome') or self.token_outcome_map.get(token_id),
            'buy_price': price,
            'sell_price': price,
            'mid_price': price,
            'timestamp': message.timestamp
        }
        self.stream_quality_stats['price_updates_accepted'] += 1
    
    async def _handle_orderbook_update(self, message):
        """Handle real-time orderbook updates."""
        self.stream_quality_stats['orderbook_updates_received'] += 1
        token_id = message.data.get('asset_id') or message.data.get('token_id') or message.data.get('outcome')
        market_id = self._resolve_market_id(message.market_id, token_id)
        if not market_id:
            self.stream_quality_stats['orderbook_updates_dropped'] += 1
            self.stream_quality_stats['orphan_token_updates'] += 1
            return
        if market_id not in self.orderbook_cache:
            self.orderbook_cache[market_id] = {}

        outcome = message.data.get('outcome') or self.token_outcome_map.get(token_id) or 'default'
        bids = self._normalize_polymarket_levels(message.data.get('bids', []), side='bids')
        asks = self._normalize_polymarket_levels(message.data.get('asks', []), side='asks')
        if not bids and not asks:
            self.stream_quality_stats['orderbook_updates_dropped'] += 1
            self.stream_quality_stats['invalid_orderbook_updates'] += 1
            return

        orderbook = {
            'bids': bids,
            'asks': asks,
            'timestamp': message.timestamp
        }
        # Store by token_id for analyzer, and also by outcome for fallback
        self.orderbook_cache[market_id][token_id or outcome] = orderbook
        if token_id and outcome and outcome not in self.orderbook_cache[market_id]:
            self.orderbook_cache[market_id][outcome] = orderbook
        self.stream_quality_stats['orderbook_updates_accepted'] += 1
    
    async def get_all_markets(self, include_closed: bool = False) -> List[Dict[str, Any]]:
        """Get all markets from bootstrap + WebSocket data."""
        # Return cached markets from bootstrap process
        markets = list(self.markets_cache.values())
        logger.info(f"Returning {len(markets)} Polymarket markets from bootstrap cache")
        return markets
    
    async def get_market_prices(self, market_id: str) -> Dict[str, Optional[float]]:
        """Get market prices from cache."""
        return self.price_cache.get(market_id, {})
    
    async def get_market_orderbook(self, market_id: str, token_id: str = None) -> Optional[Dict[str, Any]]:
        """Get market orderbook from cache."""
        market_orderbooks = self.orderbook_cache.get(market_id, {})
        if token_id:
            return market_orderbooks.get(token_id) or market_orderbooks.get(self.token_outcome_map.get(token_id))
        return market_orderbooks

    def get_data_quality_stats(self) -> Dict[str, int]:
        """Expose Polymarket stream ingest quality counters."""
        return dict(self.stream_quality_stats)
