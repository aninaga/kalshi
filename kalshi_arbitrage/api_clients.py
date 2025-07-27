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
        
    async def initialize(self) -> bool:
        """Initialize with REST market discovery + WebSocket connection."""
        try:
            # Step 1: Bootstrap market discovery via REST API
            logger.info("Discovering Kalshi markets via REST API...")
            await self._discover_markets_via_rest()
            
            # Step 2: Initialize WebSocket connection
            config = Config.WEBSOCKET_CONFIG['kalshi']
            self.websocket_client = KalshiWebSocketClient(config, self.auth_token)
            
            # Set up data handlers
            self.websocket_client.add_message_handler('ticker', self._handle_price_update)
            self.websocket_client.add_message_handler('orderbook', self._handle_orderbook_update)
            
            await self.websocket_client.connect()
            
            # Subscribe to all markets after connection
            if self.websocket_client.is_connected:
                await self.websocket_client.subscribe_to_all_markets(['ticker_v2'])
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
            'yes_price': message.data.get('yes_bid', 0) / 100.0 if message.data.get('yes_bid') else 0.5,  # Convert cents to dollars
            'no_price': message.data.get('no_bid', 0) / 100.0 if message.data.get('no_bid') else 0.5,
            'yes_ask': message.data.get('yes_ask', 0) / 100.0 if message.data.get('yes_ask') else 0.5,
            'yes_bid': message.data.get('yes_bid', 0) / 100.0 if message.data.get('yes_bid') else 0.5,
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
                
                while page_count < max_pages:
                    params = {'limit': 200}  # Max limit per request
                    if cursor:
                        params['cursor'] = cursor
                    
                    try:
                        # Increased timeout for reliability
                        timeout = aiohttp.ClientTimeout(total=15)  # 15 second timeout per request
                        async with session.get(markets_url, params=params, timeout=timeout) as response:
                            if response.status == 200:
                                data = await response.json()
                                markets = data.get('markets', [])
                                
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
            'yes_asks': message.data.get('yes_asks', []),
            'yes_bids': message.data.get('yes_bids', []),
            'no_asks': message.data.get('no_asks', []),
            'no_bids': message.data.get('no_bids', []),
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
                
                # Subscribe to discovered markets (increased limit for better coverage)
                subscription_limit = min(len(self.asset_ids), 200)  # Increased from 50 to 200
                await self.websocket_client.subscribe_to_markets(self.asset_ids[:subscription_limit])
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
                        
                        try:
                            price = float(price_str)
                            self.price_cache[market_id][token_id] = {
                                'outcome': outcome,
                                'buy_price': price,
                                'sell_price': price,
                                'mid_price': price,
                                'timestamp': time.time()
                            }
                        except (ValueError, TypeError):
                            continue
                
                except Exception as e:
                    logger.debug(f"Error processing Polymarket market: {e}")
                    continue
            
            logger.info(f"Bootstrapped {len(self.markets_cache)} Polymarket markets via REST API")
            logger.info(f"Collected {len(self.asset_ids)} asset IDs for WebSocket subscription")
            
        except Exception as e:
            logger.error(f"Failed to bootstrap Polymarket markets: {e}")
    
    def _clean_title(self, title: str) -> str:
        """Basic title cleaning."""
        if not title:
            return ""
        return title.lower().strip()
    
    async def _handle_price_update(self, message):
        """Handle real-time price updates."""
        market_id = message.market_id
        if market_id not in self.price_cache:
            self.price_cache[market_id] = {}
        
        token_id = message.data.get('outcome', 'default')
        self.price_cache[market_id][token_id] = {
            'outcome': message.data.get('outcome'),
            'buy_price': message.data.get('price'),
            'sell_price': message.data.get('price'),
            'mid_price': message.data.get('price'),
            'timestamp': message.timestamp
        }
    
    async def _handle_orderbook_update(self, message):
        """Handle real-time orderbook updates."""
        market_id = message.market_id
        if market_id not in self.orderbook_cache:
            self.orderbook_cache[market_id] = {}
        
        outcome = message.data.get('outcome', 'default')
        self.orderbook_cache[market_id][outcome] = {
            'bids': message.data.get('bids', []),
            'asks': message.data.get('asks', []),
            'timestamp': message.timestamp
        }
    
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
            return market_orderbooks.get(token_id)
        return market_orderbooks
