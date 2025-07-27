import asyncio
import aiohttp
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional, Set
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import heapq
from .api_clients import KalshiClient, PolymarketClient
from .utils import clean_title, get_similarity_score
from .config import Config
from .websocket_client import RealTimeDataManager, StreamMessage

logger = logging.getLogger(__name__)

class MarketAnalyzer:
    """Comprehensive market analysis system for arbitrage detection."""
    
    def __init__(self):
        self.kalshi_client = KalshiClient()
        self.polymarket_client = PolymarketClient()
        self.last_scan_time = None
        self.market_cache = {
            'kalshi_processed': {},
            'polymarket_processed': {},
            'key_terms_cache': {},
            'similarity_cache': {},
            'orderbook_cache': {},
            'price_cache': {}
        }
        self.opportunities_history = []
        
        # Performance optimization caches
        self._polymarket_term_index = defaultdict(set)  # term -> set of market_ids
        self._similarity_cache = {}  # (title1, title2) -> similarity
        self._orderbook_cache = {}   # market_id -> (orderbook, timestamp)
        self._price_cache = {}       # market_id -> (prices, timestamp)
        
        # Completeness tracking
        self.completeness_level = Config.DEFAULT_COMPLETENESS_LEVEL
        self.completeness_stats = {
            'truncated_matches': 0,
            'truncated_trades': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'estimated_missed_opportunities': 0,
            'data_staleness_warnings': 0
        }
        
        # Dynamic cache TTL based on completeness level
        self._update_cache_settings()
        self._last_cache_cleanup = datetime.now().timestamp()
        
        # Real-time data streaming (Phase 2)
        self.realtime_manager = None
        self.realtime_enabled = Config.REALTIME_ENABLED
        self.stream_data_cache = {
            'live_prices': {},     # platform:market_id -> latest price data
            'live_orderbooks': {}, # platform:market_id -> latest orderbook
            'update_times': {}     # platform:market_id -> last update timestamp
        }
    
    async def initialize(self):
        """Initialize the analyzer with WebSocket connections."""
        logger.info("Initializing Market Analyzer with WebSocket-only mode...")
        
        # Initialize WebSocket clients
        kalshi_success = await self.kalshi_client.initialize()
        polymarket_success = await self.polymarket_client.initialize()
        
        if not kalshi_success and not polymarket_success:
            raise Exception("Failed to initialize both WebSocket clients")
        
        # Load cached data if available
        await self._load_cache()
        
        # Initialize real-time data streams (always enabled in WebSocket-only mode)
        await self._initialize_realtime_streams()
        
        logger.info(f"Market Analyzer initialized successfully with {self.completeness_level} completeness level")
    
    def set_completeness_level(self, level: str) -> None:
        """Set the completeness level for scanning."""
        if level not in Config.COMPLETENESS_LEVELS:
            raise ValueError(f"Invalid completeness level: {level}. Must be one of {list(Config.COMPLETENESS_LEVELS.keys())}")
        
        self.completeness_level = level
        self._update_cache_settings()
        
        level_config = Config.COMPLETENESS_LEVELS[level]
        logger.info(f"Set completeness level to {level}: {level_config['description']}")
        logger.info(f"Expected completeness: {level_config['expected_completeness']:.1%}")
    
    def _update_cache_settings(self) -> None:
        """Update cache TTL settings based on completeness level."""
        level_config = Config.COMPLETENESS_LEVELS[self.completeness_level]
        self._orderbook_cache_ttl = level_config['cache_ttl_orderbooks']
        self._price_cache_ttl = level_config['cache_ttl_prices']
    
    def get_adaptive_limits(self, market_conditions: Dict = None) -> Dict:
        """Get adaptive limits based on completeness level and market conditions."""
        level_config = Config.COMPLETENESS_LEVELS[self.completeness_level]
        
        # Base limits from completeness level
        limits = {
            'max_matches': level_config['max_matches_per_market'],
            'max_trades': level_config['max_trades_per_opportunity']
        }
        
        # Adaptive adjustments based on market conditions
        if market_conditions:
            volatility = market_conditions.get('volatility', 0.02)
            opportunity_density = market_conditions.get('opportunity_density', 0.1)
            
            # Increase limits in high-opportunity environments
            if opportunity_density > 0.2 and self.completeness_level != 'LOSSLESS':
                limits['max_matches'] = min(limits['max_matches'] * 2, 20)
                limits['max_trades'] = min(limits['max_trades'] * 2, 50)
                logger.info(f"Increased limits due to high opportunity density: {opportunity_density:.1%}")
        
        return limits
    
    def reset_completeness_stats(self) -> None:
        """Reset completeness tracking statistics."""
        self.completeness_stats = {
            'truncated_matches': 0,
            'truncated_trades': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'estimated_missed_opportunities': 0,
            'data_staleness_warnings': 0
        }
    
    async def run_full_scan(self) -> Dict[str, Any]:
        """Run a comprehensive scan of all markets and find arbitrage opportunities."""
        scan_start_time = datetime.now()
        logger.info(f"Starting full market scan at {scan_start_time}")
        
        # Fetch all markets from both platforms
        logger.info("Fetching markets from both platforms...")
        kalshi_markets, polymarket_markets = await self._fetch_all_markets()
        
        # Subscribe to WebSocket feeds for real-time data
        if self.realtime_manager:
            logger.info("Subscribing to WebSocket feeds for real-time data...")
            await self._subscribe_to_markets(kalshi_markets, polymarket_markets)
        
        # Process and clean market data
        logger.info("Processing market data...")
        processed_kalshi = self._process_kalshi_markets(kalshi_markets)
        processed_polymarket = self._process_polymarket_markets(polymarket_markets)
        
        # Cache processed Polymarket markets for optimized lookups
        self._current_polymarket_markets = processed_polymarket
        
        # Find potential market matches
        logger.info("Finding market matches...")
        matches = await self._find_market_matches(processed_kalshi, processed_polymarket)
        
        # Calculate arbitrage opportunities
        logger.info("Calculating arbitrage opportunities...")
        opportunities = await self._calculate_opportunities(matches)
        
        # Generate scan report with completeness information
        scan_duration = (datetime.now() - scan_start_time).total_seconds()
        estimated_completeness = self._calculate_estimated_completeness()
        
        scan_report = {
            'timestamp': scan_start_time.isoformat(),
            'duration_seconds': scan_duration,
            'kalshi_markets_count': len(kalshi_markets),
            'polymarket_markets_count': len(polymarket_markets),
            'potential_matches': len(matches),
            'arbitrage_opportunities': len(opportunities),
            'opportunities': opportunities,
            'top_opportunities': sorted(opportunities, 
                                      key=lambda x: x.get('total_profit', x.get('profit_margin', 0)), 
                                      reverse=True)[:10],
            'completeness_info': {
                'level': self.completeness_level,
                'estimated_completeness': estimated_completeness,
                'stats': self.completeness_stats.copy(),
                'level_config': Config.COMPLETENESS_LEVELS[self.completeness_level]
            }
        }
        
        # Store results
        await self._save_scan_results(scan_report)
        self.last_scan_time = scan_start_time
        
        # Periodic cache cleanup
        await self._cleanup_expired_caches()
        
        logger.info(f"Scan completed in {scan_duration:.2f}s - Found {len(opportunities)} opportunities")
        return scan_report
    
    async def _fetch_all_markets(self) -> Tuple[List[Dict], List[Dict]]:
        """Fetch all markets from WebSocket streams."""
        try:
            # In WebSocket mode, markets are populated from live streams
            # Wait for initial market data to be received
            logger.info("Waiting for initial market data from WebSocket streams...")
            await asyncio.sleep(10)  # Allow time for initial data
            
            # Get markets from WebSocket clients
            kalshi_markets = await self.kalshi_client.get_all_markets()
            polymarket_markets = await self.polymarket_client.get_all_markets()
            
            # If no markets received from WebSocket, use cached data
            if not kalshi_markets and hasattr(self, '_cached_kalshi_markets'):
                kalshi_markets = self._cached_kalshi_markets
                logger.info(f"Using {len(kalshi_markets)} cached Kalshi markets")
            
            if not polymarket_markets and hasattr(self, '_cached_polymarket_markets'):
                polymarket_markets = self._cached_polymarket_markets
                logger.info(f"Using {len(polymarket_markets)} cached Polymarket markets")
            
            return kalshi_markets, polymarket_markets
            
        except Exception as e:
            logger.error(f"Error in market fetching: {e}")
            return [], []
    
    async def _subscribe_to_markets(self, kalshi_markets: List[Dict], polymarket_markets: List[Dict]) -> None:
        """Subscribe to WebSocket feeds for active markets to get real-time data."""
        if not self.realtime_manager:
            return
        
        try:
            # Extract market identifiers for subscription
            kalshi_tickers = []
            polymarket_assets = []
            
            # Get active Kalshi market tickers (limit to avoid overwhelming)
            for market in kalshi_markets[:50]:  # Subscribe to top 50 active markets
                if market.get('status') == 'active' and market.get('ticker'):
                    kalshi_tickers.append(market['ticker'])
            
            # Get active Polymarket asset IDs
            for market in polymarket_markets[:50]:  # Subscribe to top 50 active markets
                if market.get('active', True):  # Polymarket markets are generally active
                    for token in market.get('tokens', []):
                        if token.get('token_id'):
                            polymarket_assets.append(token['token_id'])
            
            # Subscribe to markets
            if kalshi_tickers or polymarket_assets:
                await self.realtime_manager.subscribe_to_markets(
                    kalshi_tickers=kalshi_tickers,
                    polymarket_assets=polymarket_assets
                )
                logger.info(f"Subscribed to {len(kalshi_tickers)} Kalshi + {len(polymarket_assets)} Polymarket markets")
            else:
                logger.warning("No markets found to subscribe to")
                
        except Exception as e:
            logger.error(f"Error subscribing to markets: {e}")
    
    def _process_kalshi_markets(self, markets: List[Dict]) -> List[Dict]:
        """Process and standardize Kalshi market data from WebSocket format."""
        processed = []
        
        for market in markets:
            try:
                # Handle both WebSocket and REST API format
                market_id = market.get('ticker') or market.get('id')
                market_title = market.get('title', '')
                
                # Get price data from WebSocket cache if available 
                price_data = {}
                if hasattr(self, 'kalshi_client') and market_id in self.kalshi_client.price_cache:
                    price_data = self.kalshi_client.price_cache[market_id]
                
                processed_market = {
                    'id': market_id,
                    'title': market_title,
                    'clean_title': market.get('clean_title') or clean_title(market_title),
                    'platform': 'kalshi',
                    'status': market.get('status', 'active'),
                    'close_time': market.get('close_time'),
                    'yes_price': price_data.get('yes_bid', 0.5),  # Use WebSocket price data
                    'no_price': 1.0 - price_data.get('yes_bid', 0.5),  # Complement
                    'volume': self._safe_float(market.get('volume', 0)),
                    'open_interest': self._safe_float(market.get('open_interest', 0)),
                    'raw_data': market
                }
                
                # Only include markets with basic data
                if processed_market['id'] and processed_market['title']:
                    processed.append(processed_market)
                    
            except Exception as e:
                logger.debug(f"Error processing Kalshi market {market.get('ticker', 'unknown')}: {e}")
        
        logger.info(f"Processed {len(processed)} Kalshi markets out of {len(markets)} total")
        return processed
    
    def _process_polymarket_markets(self, markets: List[Dict]) -> List[Dict]:
        """Process and standardize Polymarket market data."""
        processed = []
        
        for market in markets:
            try:
                # Parse outcomes from JSON string if present
                outcomes = market.get('outcomes', '[]')
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except json.JSONDecodeError:
                        outcomes = []
                elif not isinstance(outcomes, list):
                    outcomes = []
                
                processed_market = {
                    'id': market.get('id'),
                    'title': market.get('title', market.get('question', market.get('slug', ''))),
                    'clean_title': market.get('clean_title', clean_title(market.get('title', market.get('question', market.get('slug', ''))))),
                    'platform': 'polymarket',
                    'status': market.get('status', 'active'),  # Use status field, default to active
                    'close_time': market.get('endDate'),  # Use 'endDate' instead of 'end_date_iso'
                    'outcomes': outcomes,
                    'volume': self._safe_float(market.get('volume', 0)),
                    'raw_data': market
                }
                
                # Only include active markets with valid outcomes
                if processed_market['status'] == 'active' and processed_market['outcomes']:
                    processed.append(processed_market)
                    
            except Exception as e:
                logger.debug(f"Error processing Polymarket market {market.get('id', 'unknown')}: {e}")
        
        logger.info(f"Processed {len(processed)} Polymarket markets out of {len(markets)} total")
        return processed
    
    async def _find_market_matches(self, kalshi_markets: List[Dict], 
                                  polymarket_markets: List[Dict]) -> List[Dict]:
        """Find potential matches using optimized indexing and search."""
        matches = []
        
        # Pre-filter and build indices
        kalshi_filtered = self._prefilter_markets(kalshi_markets)
        polymarket_filtered = self._prefilter_markets(polymarket_markets)
        
        logger.info(f"Pre-filtered to {len(kalshi_filtered)} Kalshi and {len(polymarket_filtered)} Polymarket markets")
        
        # Build inverted index for Polymarket markets by terms
        self._build_polymarket_term_index(polymarket_filtered)
        
        # Use optimized batch processing with concurrent execution
        batch_size = max(50, len(kalshi_filtered) // 8)  # Larger batches, more workers
        kalshi_batches = [kalshi_filtered[i:i + batch_size] for i in range(0, len(kalshi_filtered), batch_size)]
        
        # Process batches concurrently with optimized matching
        match_tasks = []
        for i, batch in enumerate(kalshi_batches):
            task = asyncio.create_task(self._process_kalshi_batch_optimized(batch, i))
            match_tasks.append(task)
        
        # Collect results concurrently
        batch_results = await asyncio.gather(*match_tasks, return_exceptions=True)
        
        for i, result in enumerate(batch_results):
            if isinstance(result, Exception):
                logger.error(f"Batch {i+1} failed: {result}")
                continue
            matches.extend(result)
            logger.info(f"Completed batch {i+1}/{len(batch_results)} - found {len(result)} matches")
        
        logger.info(f"Found {len(matches)} potential market matches above {Config.SIMILARITY_THRESHOLD} threshold")
        return matches
    
    def _process_kalshi_batch(self, kalshi_batch: List[Dict], polymarket_markets: List[Dict], batch_id: int = 0) -> List[Dict]:
        """Process a batch of Kalshi markets against all Polymarket markets."""
        batch_matches = []
        
        for k_market in kalshi_batch:
            if not k_market.get('clean_title') or not isinstance(k_market.get('clean_title'), str):
                continue
            
            # Fast pre-screening: only compare markets with overlapping key terms
            k_terms = k_market.get('key_terms', set())
            potential_matches = []
            
            for p_market in polymarket_markets:
                if not p_market.get('clean_title') or not isinstance(p_market.get('clean_title'), str):
                    continue
                
                p_terms = p_market.get('key_terms', set())
                
                # Quick term overlap check (much faster than full similarity)
                overlap_count = len(k_terms & p_terms)
                if overlap_count >= 2:  # At least 2 overlapping terms
                    # Prioritize matches with more overlap
                    potential_matches.append((p_market, overlap_count))
            
            # Sort by overlap count and limit to top candidates
            potential_matches.sort(key=lambda x: x[1], reverse=True)
            top_matches = potential_matches[:min(10, len(potential_matches))]  # Max 10 per Kalshi market
            
            # Only run expensive similarity calculation on top pre-screened matches
            for p_market, overlap_count in top_matches:
                # Calculate similarity
                similarity = get_similarity_score(
                    k_market['clean_title'], 
                    p_market['clean_title']
                )
                
                # Ensure similarity is a valid number before comparison
                if isinstance(similarity, (int, float)) and similarity >= Config.SIMILARITY_THRESHOLD:
                    match = {
                        'kalshi_market': k_market,
                        'polymarket_market': p_market,
                        'similarity_score': similarity,
                        'match_confidence': self._calculate_match_confidence(k_market, p_market),
                        'timestamp': datetime.now().isoformat()
                    }
                    batch_matches.append(match)
        
        return batch_matches
    
    def _build_polymarket_term_index(self, polymarket_markets: List[Dict]) -> None:
        """Build inverted index for fast Polymarket market lookup by terms."""
        self._polymarket_term_index.clear()
        
        for market in polymarket_markets:
            # Handle different ID field names for flexibility
            market_id = market.get('id') or market.get('condition_id') or market.get('token_id', 'unknown')
            key_terms = market.get('key_terms', set())
            
            # If no key_terms provided, generate from question/title
            if not key_terms:
                question = market.get('question', market.get('title', ''))
                if question:
                    cleaned_title = clean_title(question)
                    key_terms = set(cleaned_title.split())
            
            for term in key_terms:
                self._polymarket_term_index[term].add(market_id)
    
    async def _process_kalshi_batch_optimized(self, kalshi_batch: List[Dict], batch_id: int = 0) -> List[Dict]:
        """Optimized batch processing using adaptive limits and completeness tracking."""
        batch_matches = []
        
        # Get adaptive limits for current market conditions
        adaptive_limits = self.get_adaptive_limits()
        max_matches = adaptive_limits['max_matches']
        
        # Pre-build Polymarket market lookup for this batch
        polymarket_lookup = {m['id']: m for m in self._get_cached_polymarket_markets()}
        
        for k_market in kalshi_batch:
            if not k_market.get('clean_title') or not isinstance(k_market.get('clean_title'), str):
                continue
            
            k_terms = k_market.get('key_terms', set())
            if len(k_terms) < 2:  # Skip markets with too few terms
                continue
            
            # Fast candidate selection using inverted index
            candidate_market_ids = self._get_candidate_markets(k_terms)
            
            if not candidate_market_ids:
                continue
            
            # Use priority queue to efficiently find top matches
            top_matches = []
            
            for market_id in candidate_market_ids:
                p_market = polymarket_lookup.get(market_id)
                if not p_market or not p_market.get('clean_title'):
                    continue
                
                # Quick term overlap check first
                p_terms = p_market.get('key_terms', set())
                overlap_score = len(k_terms & p_terms) / len(k_terms | p_terms)
                
                if overlap_score < 0.3:  # Quick filter - need at least 30% term overlap
                    continue
                
                # Only calculate expensive similarity for promising candidates
                similarity_key = (k_market['clean_title'], p_market['clean_title'])
                
                if similarity_key in self._similarity_cache:
                    similarity = self._similarity_cache[similarity_key]
                else:
                    similarity = get_similarity_score(k_market['clean_title'], p_market['clean_title'])
                    self._similarity_cache[similarity_key] = similarity
                
                if similarity >= Config.SIMILARITY_THRESHOLD:
                    # Use heap to maintain top matches per Kalshi market (adaptive limit)
                    # Add unique tie-breaker to avoid dict comparison issues
                    heap_item = (similarity, market_id, p_market)
                    if len(top_matches) < max_matches:
                        heapq.heappush(top_matches, heap_item)
                    elif similarity > top_matches[0][0]:
                        heapq.heapreplace(top_matches, heap_item)
                    elif len(top_matches) >= max_matches:
                        # Track truncation for completeness monitoring
                        self.completeness_stats['truncated_matches'] += 1
            
            # Add top matches to results
            for similarity, _, p_market in top_matches:
                match = {
                    'kalshi_market': k_market,
                    'polymarket_market': p_market,
                    'similarity_score': similarity,
                    'match_confidence': self._calculate_match_confidence(k_market, p_market),
                    'timestamp': datetime.now().isoformat()
                }
                batch_matches.append(match)
        
        return batch_matches
    
    def _get_candidate_markets(self, kalshi_terms: Set[str]) -> Set[str]:
        """Get candidate Polymarket markets using inverted index."""
        candidate_scores = defaultdict(int)
        
        # Score candidates by number of overlapping terms
        for term in kalshi_terms:
            for market_id in self._polymarket_term_index.get(term, set()):
                candidate_scores[market_id] += 1
        
        # Return only candidates with at least 2 overlapping terms
        return {market_id for market_id, score in candidate_scores.items() if score >= 2}
    
    def _get_cached_polymarket_markets(self) -> List[Dict]:
        """Get cached Polymarket markets for lookup."""
        # This should return the processed Polymarket markets from the current scan
        # We'll implement this as a simple cache that gets updated during processing
        return getattr(self, '_current_polymarket_markets', [])
    
    def _prefilter_markets(self, markets: List[Dict]) -> List[Dict]:
        """Pre-filter markets to reduce comparison space."""
        filtered = []
        
        for market in markets:
            title = market.get('clean_title', '')
            if not title:
                continue
            
            # Extract key terms (names, places, events)
            key_terms = self._extract_key_terms(title)
            
            # Only include markets with meaningful terms
            if len(key_terms) >= 2:
                market['key_terms'] = key_terms
                filtered.append(market)
        
        return filtered
    
    def _extract_key_terms(self, title: str) -> set:
        """Extract key meaningful terms from market title."""
        # Check cache first
        if title in self.market_cache['key_terms_cache']:
            return self.market_cache['key_terms_cache'][title]
        
        # Common stop words to ignore
        stop_words = {
            'will', 'be', 'the', 'for', 'in', 'on', 'at', 'to', 'of', 'a', 'an', 
            'and', 'or', 'but', 'if', 'then', 'than', 'when', 'where', 'how', 'what',
            'who', 'which', 'that', 'this', 'these', 'those', 'is', 'are', 'was', 
            'were', 'have', 'has', 'had', 'do', 'does', 'did', 'can', 'could', 
            'should', 'would', 'may', 'might', 'must', 'shall', 'as', 'by', 'from'
        }
        
        # Split into words and filter
        words = title.lower().split()
        key_terms = set()
        
        for word in words:
            # Remove punctuation
            word = word.strip('.,!?()[]{}"\':;')
            
            # Keep meaningful terms (length > 2, not stop words, not numbers)
            if (len(word) > 2 and 
                word not in stop_words and 
                not word.isdigit() and 
                word.isalpha()):
                key_terms.add(word)
        
        # Cache the result
        self.market_cache['key_terms_cache'][title] = key_terms
        return key_terms
    
    async def _calculate_opportunities(self, matches: List[Dict]) -> List[Dict]:
        """Calculate arbitrage opportunities with optimized concurrent processing."""
        opportunities = []
        
        # Process opportunities in concurrent batches
        batch_size = 20  # Process 20 matches concurrently
        match_batches = [matches[i:i + batch_size] for i in range(0, len(matches), batch_size)]
        
        for batch_idx, match_batch in enumerate(match_batches):
            try:
                # Process batch concurrently
                batch_tasks = []
                for match in match_batch:
                    task = asyncio.create_task(self._calculate_single_opportunity(match))
                    batch_tasks.append(task)
                
                # Wait for batch completion
                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                
                # Collect successful results
                batch_opportunities = []
                for result in batch_results:
                    if isinstance(result, Exception):
                        logger.debug(f"Opportunity calculation failed: {result}")
                        continue
                    if result:
                        batch_opportunities.append(result)
                
                opportunities.extend(batch_opportunities)
                logger.info(f"Processed batch {batch_idx + 1}/{len(match_batches)} - found {len(batch_opportunities)} opportunities")
                
            except Exception as e:
                logger.error(f"Error processing opportunity batch {batch_idx}: {e}")
        
        logger.info(f"Found {len(opportunities)} arbitrage opportunities")
        return opportunities
    
    async def _calculate_single_opportunity(self, match: Dict) -> Optional[Dict]:
        """Calculate single arbitrage opportunity with caching."""
        try:
            kalshi_market = match['kalshi_market']
            polymarket_market = match['polymarket_market']
            
            # Get Polymarket prices with caching
            polymarket_prices = await self._get_cached_market_prices(polymarket_market['id'])
            
            if not polymarket_prices:
                return None
            
            # Find the best arbitrage opportunity for this match
            return await self._find_best_arbitrage(
                kalshi_market, polymarket_market, polymarket_prices, match
            )
            
        except Exception as e:
            logger.debug(f"Error calculating opportunity for match: {e}")
            return None
    
    async def _find_best_arbitrage(self, kalshi_market: Dict, polymarket_market: Dict,
                                  polymarket_prices: Dict, match: Dict) -> Optional[Dict]:
        """Find the best arbitrage opportunity with accurate volume and slippage calculations."""
        best_opportunity = None
        best_total_profit = 0
        
        # Get Kalshi order book data with caching
        kalshi_orderbook = await self._get_cached_orderbook(kalshi_market['id'], 'kalshi')
        if not kalshi_orderbook:
            return None
            
        # Check each Polymarket token for arbitrage opportunities
        for token_id, price_data in polymarket_prices.items():
            if not price_data:
                continue
                
            # Get Polymarket order book with caching
            polymarket_orderbook = await self._get_cached_orderbook(polymarket_market['id'], 'polymarket', token_id)
            if not polymarket_orderbook:
                continue
            
            # Strategy 1: Buy Kalshi YES, Sell Polymarket
            opportunity_1 = await self._calculate_accurate_arbitrage(
                buy_orderbook=kalshi_orderbook['yes_asks'],
                sell_orderbook=polymarket_orderbook['bids'],
                buy_platform='kalshi',
                sell_platform='polymarket',
                buy_fee_rate=Config.KALSHI_FEE_RATE,
                sell_fee_rate=Config.POLYMARKET_FEE_RATE,
                strategy='Buy Kalshi YES → Sell Polymarket',
                kalshi_market=kalshi_market,
                polymarket_market=polymarket_market,
                polymarket_token=token_id,
                match_data=match
            )
            
            if opportunity_1 and opportunity_1['total_profit'] > best_total_profit:
                best_opportunity = opportunity_1
                best_total_profit = opportunity_1['total_profit']
            
            # Strategy 2: Buy Polymarket, Sell Kalshi YES
            opportunity_2 = await self._calculate_accurate_arbitrage(
                buy_orderbook=polymarket_orderbook['asks'],
                sell_orderbook=kalshi_orderbook['yes_bids'],
                buy_platform='polymarket',
                sell_platform='kalshi',
                buy_fee_rate=Config.POLYMARKET_FEE_RATE,
                sell_fee_rate=Config.KALSHI_FEE_RATE,
                strategy='Buy Polymarket → Sell Kalshi YES',
                kalshi_market=kalshi_market,
                polymarket_market=polymarket_market,
                polymarket_token=token_id,
                match_data=match
            )
            
            if opportunity_2 and opportunity_2['total_profit'] > best_total_profit:
                best_opportunity = opportunity_2
                best_total_profit = opportunity_2['total_profit']
        
        return best_opportunity
    
    async def _get_cached_market_prices(self, market_id: str) -> Optional[Dict]:
        """Get market prices from WebSocket streams only."""
        try:
            # Get prices directly from WebSocket client cache
            prices = await self.polymarket_client.get_market_prices(market_id)
            if prices:
                self.completeness_stats['cache_hits'] += 1
                logger.debug(f"Using WebSocket data for {market_id}")
                return prices
            else:
                self.completeness_stats['cache_misses'] += 1
                logger.debug(f"No WebSocket data available for {market_id}")
                return None
        except Exception as e:
            logger.debug(f"Failed to fetch prices for {market_id}: {e}")
            return None
    
    async def _get_cached_orderbook(self, market_id: str, platform: str, token_id: str = None) -> Optional[Dict]:
        """Get order book from WebSocket streams only."""
        try:
            if platform == 'kalshi':
                orderbook = await self.kalshi_client.get_market_orderbook(market_id)
            else:  # polymarket
                orderbook = await self.polymarket_client.get_market_orderbook(market_id, token_id)
            
            if orderbook:
                self.completeness_stats['cache_hits'] += 1
                return orderbook
            else:
                self.completeness_stats['cache_misses'] += 1
                return None
        except Exception as e:
            logger.debug(f"Failed to fetch orderbook for {platform}:{market_id}: {e}")
            return None
    
    async def _calculate_accurate_arbitrage(self, buy_orderbook: List[Dict], sell_orderbook: List[Dict],
                                          buy_platform: str, sell_platform: str,
                                          buy_fee_rate: float, sell_fee_rate: float,
                                          strategy: str, kalshi_market: Dict, polymarket_market: Dict,
                                          polymarket_token: str, match_data: Dict) -> Optional[Dict]:
        """Calculate accurate arbitrage profit with volume overlap and slippage."""
        if not buy_orderbook or not sell_orderbook:
            return None
            
        # Calculate overlapping tradeable volume
        overlapping_trades = self._calculate_overlapping_volume(
            buy_orderbook, sell_orderbook, buy_fee_rate, sell_fee_rate
        )
        
        if not overlapping_trades:
            return None
            
        # Calculate total profit and other metrics
        total_profit = sum(trade['net_profit'] for trade in overlapping_trades)
        total_volume = sum(trade['volume'] for trade in overlapping_trades)
        weighted_avg_buy_price = sum(trade['buy_price'] * trade['volume'] for trade in overlapping_trades) / total_volume
        weighted_avg_sell_price = sum(trade['sell_price'] * trade['volume'] for trade in overlapping_trades) / total_volume
        profit_margin = total_profit / (weighted_avg_buy_price * total_volume) if total_volume > 0 else 0
        
        # Only return opportunities above threshold
        if profit_margin < Config.MIN_PROFIT_THRESHOLD:
            return None
            
        return {
            'strategy': strategy,
            'buy_platform': buy_platform,
            'sell_platform': sell_platform,
            'total_profit': total_profit,
            'profit_margin': profit_margin,
            'max_tradeable_volume': total_volume,
            'weighted_avg_buy_price': weighted_avg_buy_price,
            'weighted_avg_sell_price': weighted_avg_sell_price,
            'num_trades': len(overlapping_trades),
            'trade_breakdown': overlapping_trades,
            'match_data': match_data,
            'polymarket_token': polymarket_token,
            'opportunity_id': f"{kalshi_market['id']}_{polymarket_market['id']}_{polymarket_token}_{strategy.replace(' ', '_')}",
            'timestamp': datetime.now().isoformat(),
            # Legacy fields for backward compatibility
            'buy_price': weighted_avg_buy_price,
            'sell_price': weighted_avg_sell_price
        }
    
    def _calculate_overlapping_volume(self, buy_orderbook: List[Dict], sell_orderbook: List[Dict],
                                    buy_fee_rate: float, sell_fee_rate: float) -> List[Dict]:
        """Optimized calculation of overlapping tradeable volume at profitable prices."""
        if not buy_orderbook or not sell_orderbook:
            return []
        
        profitable_trades = []
        
        # Pre-sort orderbooks once (buy ascending, sell descending)  
        buy_orders = sorted(buy_orderbook, key=lambda x: x['price'])
        sell_orders = sorted(sell_orderbook, key=lambda x: x['price'], reverse=True)
        
        # Pre-calculate fee multipliers
        buy_fee_multiplier = 1 + buy_fee_rate
        sell_fee_multiplier = 1 - sell_fee_rate
        
        # Fast early termination check
        min_sell_price = sell_orders[0]['price'] * sell_fee_multiplier
        max_buy_price = buy_orders[-1]['price'] * buy_fee_multiplier
        
        if min_sell_price <= max_buy_price:
            return []  # No profitable opportunities possible
        
        buy_index = 0
        sell_index = 0
        
        # Get adaptive limit for trades
        adaptive_limits = self.get_adaptive_limits()
        max_trades = adaptive_limits['max_trades']
        
        while (buy_index < len(buy_orders) and sell_index < len(sell_orders) and 
               len(profitable_trades) < max_trades):
            
            buy_order = buy_orders[buy_index].copy()  # Copy to avoid modifying original
            sell_order = sell_orders[sell_index].copy()
            
            buy_price = buy_order['price']
            sell_price = sell_order['price']
            
            # Quick profitability check before expensive calculations
            quick_profit = sell_price * sell_fee_multiplier - buy_price * buy_fee_multiplier
            if quick_profit <= 0:
                break  # No more profitable trades possible
            
            # Calculate tradeable volume
            tradeable_volume = min(buy_order['size'], sell_order['size'])
            
            # Skip small volumes
            if tradeable_volume < Config.MIN_TRADEABLE_VOLUME:
                if buy_order['size'] <= sell_order['size']:
                    buy_index += 1
                else:
                    sell_index += 1
                continue
            
            # Optimize slippage calculation - use simplified model for speed
            volume_ratio = tradeable_volume / max(buy_order['size'], sell_order['size'], 1)
            slippage_adjustment = Config.BASE_SLIPPAGE_RATE + volume_ratio * Config.SLIPPAGE_IMPACT_FACTOR
            slippage_adjustment = min(slippage_adjustment, Config.MAX_SLIPPAGE_RATE)
            
            # Apply slippage and fees
            final_buy_price = buy_price * (1 + slippage_adjustment) * buy_fee_multiplier
            final_sell_price = sell_price * (1 - slippage_adjustment) * sell_fee_multiplier
            
            # Final profitability check
            if final_sell_price > final_buy_price:
                net_profit = (final_sell_price - final_buy_price) * tradeable_volume
                
                profitable_trades.append({
                    'buy_price': final_buy_price,
                    'sell_price': final_sell_price,
                    'volume': tradeable_volume,
                    'gross_profit': net_profit,  # Simplified - fees already in prices
                    'net_profit': net_profit,
                    'slippage_impact': slippage_adjustment,
                    'buy_fee': final_buy_price * buy_fee_rate * tradeable_volume,
                    'sell_fee': final_sell_price * sell_fee_rate * tradeable_volume
                })
            
            # Update remaining volumes efficiently
            buy_order['size'] -= tradeable_volume
            sell_order['size'] -= tradeable_volume
            
            if buy_order['size'] <= 0:
                buy_index += 1
            if sell_order['size'] <= 0:
                sell_index += 1
        
        # Check if we hit the trade limit and track truncation
        if (buy_index < len(buy_orders) and sell_index < len(sell_orders) and 
            len(profitable_trades) >= max_trades):
            # Estimate how many more trades were possible
            remaining_trades = min(len(buy_orders) - buy_index, len(sell_orders) - sell_index)
            self.completeness_stats['truncated_trades'] += remaining_trades
        
        return profitable_trades
    
    def _get_cached_similarity(self, title1: str, title2: str) -> float:
        """Get similarity score with caching for performance."""
        cache_key = (title1, title2)
        reverse_key = (title2, title1)
        
        # Check cache first
        if cache_key in self._similarity_cache:
            return self._similarity_cache[cache_key]
        elif reverse_key in self._similarity_cache:
            return self._similarity_cache[reverse_key]
        
        # Calculate and cache
        similarity = get_similarity_score(title1, title2)
        self._similarity_cache[cache_key] = similarity
        return similarity
    
    def _calculate_slippage_impact(self, volume: float, buy_order: Dict = None, sell_order: Dict = None) -> float:
        """Calculate slippage impact based on order size and market depth."""
        # Handle both full order mode and simple volume mode for testing
        if buy_order is None and sell_order is None:
            # Simple mode for testing - treat volume as total liquidity
            total_liquidity = volume * 2  # Assume 2x volume as liquidity
            volume_ratio = volume / total_liquidity if total_liquidity > 0 else 1
        else:
            # Full mode with order details
            buy_size = buy_order.get('size', volume) if buy_order else volume
            sell_size = sell_order.get('size', volume) if sell_order else volume
            
            # Calculate slippage as percentage of volume relative to available liquidity
            avg_available_liquidity = (buy_size + sell_size) / 2
            volume_ratio = volume / avg_available_liquidity if avg_available_liquidity > 0 else 1
        
        # Apply progressive slippage using config parameters
        base_slippage = Config.BASE_SLIPPAGE_RATE
        progressive_slippage = volume_ratio * Config.SLIPPAGE_IMPACT_FACTOR
        
        return min(base_slippage + progressive_slippage, Config.MAX_SLIPPAGE_RATE)
    
    async def _get_kalshi_orderbook(self, market_id: str) -> Optional[Dict]:
        """Get Kalshi order book data from WebSocket cache."""
        try:
            # Get orderbook from WebSocket client cache
            orderbook = await self.kalshi_client.get_market_orderbook(market_id)
            if orderbook:
                return orderbook
            
            # Fallback to synthetic orderbook if no real data
            return await self._create_synthetic_kalshi_orderbook(market_id)
            
        except Exception as e:
            logger.debug(f"Failed to fetch Kalshi orderbook for {market_id}: {e}")
            return await self._create_synthetic_kalshi_orderbook(market_id)
    
    async def _create_synthetic_kalshi_orderbook(self, market_id: str) -> Optional[Dict]:
        """Create synthetic orderbook from WebSocket price data if detailed orderbook unavailable."""
        try:
            price_data = await self.kalshi_client.get_market_prices(market_id)
            if not price_data:
                return None
                
            yes_price = price_data.get('yes_price', 0.5)
            no_price = price_data.get('no_price', 0.5)
            
            if not yes_price or not no_price:
                # Default to 50-50 if no price data
                yes_price = 0.5
                no_price = 0.5
                
            # Create synthetic orderbook with spread around mid price
            spread = 0.01  # 1 cent spread
            volume = 100   # Assume 100 shares available at each level
            
            return {
                'yes_asks': [{'price': yes_price + spread/2, 'size': volume}],
                'yes_bids': [{'price': yes_price - spread/2, 'size': volume}],
                'no_asks': [{'price': no_price + spread/2, 'size': volume}],
                'no_bids': [{'price': no_price - spread/2, 'size': volume}]
            }
            
        except Exception as e:
            logger.error(f"Failed to create synthetic Kalshi orderbook for {market_id}: {e}")
            return None
    
    async def _get_polymarket_orderbook(self, token_id: str) -> Optional[Dict]:
        """Get Polymarket order book data from WebSocket cache."""
        try:
            # Get orderbook from WebSocket client cache  
            # Note: For Polymarket, we need to map token_id to market_id
            # This is a simplification - in reality we'd need a mapping
            orderbook = await self.polymarket_client.get_market_orderbook(token_id)
            if orderbook:
                return orderbook
            
            # Fallback to synthetic orderbook
            return await self._create_synthetic_polymarket_orderbook(token_id)
            
        except Exception as e:
            logger.debug(f"Failed to fetch Polymarket orderbook for {token_id}: {e}")
            return await self._create_synthetic_polymarket_orderbook(token_id)
    
    async def _create_synthetic_polymarket_orderbook(self, token_id: str) -> Optional[Dict]:
        """Create synthetic orderbook from WebSocket price data if detailed orderbook unavailable."""
        try:
            # Get price from WebSocket cache
            price_data = await self.polymarket_client.get_market_prices(token_id)
            if not price_data or token_id not in price_data:
                # Default prices if no data available
                buy_price = 0.5
                sell_price = 0.5
            else:
                token_price_data = price_data[token_id]
                buy_price = token_price_data.get('buy_price', 0.5)
                sell_price = token_price_data.get('sell_price', 0.5)
                
            volume = 1000  # Assume 1000 shares available
            
            return {
                'asks': [{'price': sell_price, 'size': volume}],
                'bids': [{'price': buy_price, 'size': volume}]
            }
            
        except Exception as e:
            logger.error(f"Failed to create synthetic Polymarket orderbook for {token_id}: {e}")
            return None
    
    async def _cleanup_expired_caches(self) -> None:
        """Clean up expired cache entries to prevent memory leaks."""
        now = datetime.now().timestamp()
        
        # Only cleanup every 5 minutes
        if now - self._last_cache_cleanup < Config.CACHE_CLEANUP_INTERVAL:
            return
        
        self._last_cache_cleanup = now
        
        # Clean expired order book cache entries
        expired_orderbook_keys = []
        for key, (_, timestamp) in self._orderbook_cache.items():
            if now - timestamp > self._orderbook_cache_ttl:
                expired_orderbook_keys.append(key)
        
        for key in expired_orderbook_keys:
            del self._orderbook_cache[key]
        
        # Clean expired price cache entries
        expired_price_keys = []
        for key, (_, timestamp) in self._price_cache.items():
            if now - timestamp > self._price_cache_ttl:
                expired_price_keys.append(key)
        
        for key in expired_price_keys:
            del self._price_cache[key]
        
        # Clean old similarity cache entries (keep max 10000 entries)
        if len(self._similarity_cache) > 10000:
            # Remove oldest 50% of entries
            items_to_remove = len(self._similarity_cache) // 2
            keys_to_remove = list(self._similarity_cache.keys())[:items_to_remove]
            for key in keys_to_remove:
                del self._similarity_cache[key]
        
        if expired_orderbook_keys or expired_price_keys:
            logger.info(f"Cache cleanup: removed {len(expired_orderbook_keys)} orderbook and {len(expired_price_keys)} price entries")
    
    def _calculate_estimated_completeness(self) -> float:
        """Calculate estimated completeness based on truncation and cache stats."""
        level_config = Config.COMPLETENESS_LEVELS[self.completeness_level]
        base_completeness = level_config['expected_completeness']
        
        # Adjust based on actual truncation
        total_cache_requests = self.completeness_stats['cache_hits'] + self.completeness_stats['cache_misses']
        
        # Penalty for truncation
        truncation_penalty = 0.0
        if self.completeness_stats['truncated_matches'] > 0:
            truncation_penalty += min(0.05, self.completeness_stats['truncated_matches'] * 0.001)
        
        if self.completeness_stats['truncated_trades'] > 0:
            truncation_penalty += min(0.03, self.completeness_stats['truncated_trades'] * 0.0005)
        
        # Penalty for stale data
        staleness_penalty = 0.0
        if total_cache_requests > 0:
            staleness_ratio = self.completeness_stats['data_staleness_warnings'] / total_cache_requests
            staleness_penalty = min(0.02, staleness_ratio * 0.1)
        
        estimated_completeness = max(0.5, base_completeness - truncation_penalty - staleness_penalty)
        
        # Update estimated missed opportunities
        if estimated_completeness < 1.0:
            self.completeness_stats['estimated_missed_opportunities'] = int(
                (1.0 - estimated_completeness) * 100  # Rough estimate
            )
        
        return estimated_completeness
    
    def _calculate_profit_margin(self, buy_price: float, sell_price: float,
                               buy_fee: float, sell_fee: float) -> float:
        """Calculate net profit margin after fees (legacy method for backward compatibility)."""
        if buy_price <= 0 or sell_price <= 0:
            return 0.0
        
        gross_profit = sell_price - buy_price
        total_fees = (buy_price * buy_fee) + (sell_price * sell_fee)
        net_profit = gross_profit - total_fees
        
        return net_profit / buy_price if buy_price > 0 else 0.0
    
    def _extract_kalshi_price(self, market: Dict, side: str) -> Optional[float]:
        """Extract price from Kalshi market data."""
        price_key = f"{side}_price" if side in ['yes', 'no'] else side
        price = market.get(price_key, market.get(f"{side}_ask", market.get(f"{side}_bid")))
        
        if price is None:
            return None
        
        # Convert from cents to decimal if needed
        if price > 1.0:
            price = price / 100.0
        
        return price
    
    def _calculate_match_confidence(self, kalshi_market: Dict, polymarket_market: Dict) -> float:
        """Calculate confidence score for market match quality."""
        confidence = 0.0
        
        # Base similarity score - this should be passed from the match, not from individual markets
        # We'll calculate it here as a fallback
        similarity = get_similarity_score(
            kalshi_market.get('clean_title', ''),
            polymarket_market.get('clean_title', '')
        )
        confidence += similarity * 0.6  # 60% weight for title similarity
        
        # Time proximity (markets that close around the same time are more likely to be equivalent)
        if kalshi_market.get('close_time') and polymarket_market.get('close_time'):
            # Add logic to compare close times
            confidence += 0.2  # Placeholder
        
        # Volume and liquidity indicators
        k_volume = self._safe_float(kalshi_market.get('volume', 0))
        p_volume = self._safe_float(polymarket_market.get('volume', 0))
        if k_volume > 10000 and p_volume > 10000:  # Both have decent volume
            confidence += 0.2
        
        return min(confidence, 1.0)
    
    def _safe_float(self, value) -> float:
        """Safely convert a value to float, handling strings and None."""
        if value is None:
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0
    
    async def _save_scan_results(self, scan_report: Dict):
        """Save scan results to disk for historical analysis."""
        try:
            # Save full report
            timestamp = scan_report['timestamp'].replace(':', '-').split('.')[0]
            filename = f"scan_report_{timestamp}.json"
            filepath = os.path.join(Config.DATA_DIR, filename)
            
            with open(filepath, 'w') as f:
                json.dump(scan_report, f, indent=2, default=str)
            
            # Update opportunities history
            self.opportunities_history.append({
                'timestamp': scan_report['timestamp'],
                'opportunity_count': scan_report['arbitrage_opportunities'],
                'top_profit_margin': scan_report['top_opportunities'][0]['profit_margin'] 
                                   if scan_report['top_opportunities'] else 0
            })
            
            # Save opportunities history
            history_file = os.path.join(Config.DATA_DIR, Config.OPPORTUNITIES_FILE)
            with open(history_file, 'w') as f:
                json.dump(self.opportunities_history, f, indent=2, default=str)
                
        except Exception as e:
            logger.error(f"Error saving scan results: {e}")
    
    async def _load_cache(self):
        """Load cached data from previous runs."""
        try:
            cache_file = os.path.join(Config.DATA_DIR, Config.MARKETS_CACHE_FILE)
            if os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    self.market_cache = json.load(f)
                logger.info("Loaded market cache from previous run")
            
            history_file = os.path.join(Config.DATA_DIR, Config.OPPORTUNITIES_FILE)
            if os.path.exists(history_file):
                with open(history_file, 'r') as f:
                    self.opportunities_history = json.load(f)
                logger.info(f"Loaded {len(self.opportunities_history)} historical opportunity records")
                
        except Exception as e:
            logger.error(f"Error loading cache: {e}")
    
    async def _initialize_realtime_streams(self):
        """Initialize real-time WebSocket data streams (Phase 2)."""
        try:
            logger.info("Initializing real-time data streams...")
            
            # Create real-time data manager with config
            kalshi_config = Config.WEBSOCKET_CONFIG['kalshi']
            polymarket_config = Config.WEBSOCKET_CONFIG['polymarket']
            
            if kalshi_config.get('enabled') or polymarket_config.get('enabled'):
                # Pass Kalshi auth token if available
                kalshi_token = getattr(self.kalshi_client, 'auth_token', None)
                self.realtime_manager = RealTimeDataManager(kalshi_config, polymarket_config, kalshi_token)
                
                # Set up data handlers for live updates
                self.realtime_manager.add_data_handler('price_update', self._handle_realtime_price_update)
                self.realtime_manager.add_data_handler('orderbook_update', self._handle_realtime_orderbook_update)
                self.realtime_manager.add_data_handler('trade_update', self._handle_realtime_trade_update)
                
                # Start the real-time connections
                await self.realtime_manager.start()
                
                logger.info("Real-time data streams initialized successfully")
            else:
                logger.info("Real-time streams disabled in configuration")
                
        except Exception as e:
            logger.error(f"Failed to initialize real-time streams: {e}")
            logger.info("Falling back to REST API polling")
            self.realtime_enabled = False
    
    async def _handle_realtime_price_update(self, message: StreamMessage):
        """Handle real-time price updates from WebSocket streams."""
        market_key = f"{message.platform}:{message.market_id}"
        
        # Update live price cache
        self.stream_data_cache['live_prices'][market_key] = message.data
        self.stream_data_cache['update_times'][market_key] = message.timestamp
        
        # Log high-frequency updates at debug level
        logger.debug(f"Price update for {market_key}: {message.data}")
        
        # Update completeness stats
        self.completeness_stats['cache_hits'] += 1
    
    async def _handle_realtime_orderbook_update(self, message: StreamMessage):
        """Handle real-time orderbook updates from WebSocket streams."""
        market_key = f"{message.platform}:{message.market_id}"
        
        # Update live orderbook cache
        self.stream_data_cache['live_orderbooks'][market_key] = message.data
        self.stream_data_cache['update_times'][market_key] = message.timestamp
        
        # Log orderbook updates at debug level
        logger.debug(f"Orderbook update for {market_key}")
        
        # Update completeness stats
        self.completeness_stats['cache_hits'] += 1
    
    async def _handle_realtime_trade_update(self, message: StreamMessage):
        """Handle real-time trade updates from WebSocket streams."""
        # Log significant trades
        if message.data.get('count', 0) > 1000:  # Log large trades
            logger.info(f"Large trade on {message.platform}:{message.market_id}: {message.data}")
    
    def _get_live_data_freshness(self, platform: str, market_id: str) -> float:
        """Get seconds since last live data update for a market."""
        market_key = f"{platform}:{market_id}"
        last_update = self.stream_data_cache['update_times'].get(market_key)
        if last_update:
            return datetime.now().timestamp() - last_update
        return float('inf')
    
    def _is_live_data_fresh(self, platform: str, market_id: str) -> bool:
        """Check if live data is fresh enough to use."""
        freshness = self._get_live_data_freshness(platform, market_id)
        return freshness <= Config.STREAM_FRESHNESS_THRESHOLD
    
    async def _get_market_prices_realtime(self, platform: str, market_id: str) -> Optional[Dict]:
        """Get market prices from real-time stream if available and fresh."""
        if not self.realtime_enabled or not self.realtime_manager:
            return None
        
        market_key = f"{platform}:{market_id}"
        
        # Check if we have fresh live data
        if self._is_live_data_fresh(platform, market_id):
            live_data = self.stream_data_cache['live_prices'].get(market_key)
            if live_data:
                self.completeness_stats['cache_hits'] += 1
                return live_data
        
        # Data is stale, increment staleness warning
        if market_key in self.stream_data_cache['update_times']:
            self.completeness_stats['data_staleness_warnings'] += 1
            
        return None
    
    def _convert_realtime_orderbook_format(self, realtime_data: Dict, platform: str) -> Dict:
        """Convert real-time orderbook data to standard format."""
        if platform == 'kalshi':
            # Convert Kalshi real-time format to standard format
            return {
                'yes': {
                    'asks': realtime_data.get('yes_asks', []),
                    'bids': realtime_data.get('yes_bids', [])
                },
                'no': {
                    'asks': realtime_data.get('no_asks', []),
                    'bids': realtime_data.get('no_bids', [])
                }
            }
        elif platform == 'polymarket':
            # Convert Polymarket real-time format to standard format
            outcome = realtime_data.get('outcome', 'yes')
            return {
                outcome: {
                    'asks': realtime_data.get('asks', []),
                    'bids': realtime_data.get('bids', [])
                }
            }
        
        return realtime_data
    
    async def _get_market_orderbook_realtime(self, platform: str, market_id: str) -> Optional[Dict]:
        """Get market orderbook from real-time stream if available and fresh."""
        if not self.realtime_enabled or not self.realtime_manager:
            return None
        
        market_key = f"{platform}:{market_id}"
        
        # Check if we have fresh live data
        if self._is_live_data_fresh(platform, market_id):
            live_data = self.stream_data_cache['live_orderbooks'].get(market_key)
            if live_data:
                self.completeness_stats['cache_hits'] += 1
                return live_data
        
        # Data is stale, increment staleness warning
        if market_key in self.stream_data_cache['update_times']:
            self.completeness_stats['data_staleness_warnings'] += 1
            
        return None
    
    def _convert_realtime_orderbook_format(self, realtime_data: Dict, platform: str) -> Dict:
        """Convert real-time orderbook data to standard format."""
        if platform == 'kalshi':
            # Convert Kalshi real-time format to standard format
            return {
                'yes': {
                    'asks': realtime_data.get('yes_asks', []),
                    'bids': realtime_data.get('yes_bids', [])
                },
                'no': {
                    'asks': realtime_data.get('no_asks', []),
                    'bids': realtime_data.get('no_bids', [])
                }
            }
        elif platform == 'polymarket':
            # Convert Polymarket real-time format to standard format
            outcome = realtime_data.get('outcome', 'yes')
            return {
                outcome: {
                    'asks': realtime_data.get('asks', []),
                    'bids': realtime_data.get('bids', [])
                }
            }
        
        return realtime_data