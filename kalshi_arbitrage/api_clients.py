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
        self._rest_discovered_tickers = set()  # Track REST-originated tickers
        self._last_market_cleanup = time.time()
        self._orderbook_rest_semaphore = asyncio.Semaphore(Config.ORDERBOOK_REST_MAX_PER_SECOND)
        self._orderbook_rest_last_call = 0.0
        self._orderbook_rest_session = None
        self._orderbook_rest_budget = Config.ORDERBOOK_REST_BUDGET_PER_SCAN

    def reset_rest_budget(self):
        """Reset per-scan REST orderbook fetch budget."""
        self._orderbook_rest_budget = Config.ORDERBOOK_REST_BUDGET_PER_SCAN

    def _cents_to_dollars(self, value, default: float = 0.5) -> float:
        """Convert Kalshi cent pricing to dollars with safe fallback."""
        if value is None:
            return default
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value / 100.0 if value >= 1 else value

    def _safe_float(self, value: Any) -> Optional[float]:
        """Parse float safely."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _fp(market: Dict[str, Any], base: str, default: float = 0.0) -> float:
        """Read a Kalshi field that migrated to '<base>_fp' / '<base>_dollars'.

        Kalshi deprecated the flat names (volume, yes_bid, last_price, ...) to
        return null and moved the values to fixed-point ('<base>_fp', whole
        counts as strings) and dollar ('<base>_dollars', already 0..1 as strings)
        fields. Tries new names first, falls back to the legacy name, parses
        strings to float, never raises.
        """
        for key in (f"{base}_fp", f"{base}_dollars", base):
            if market.get(key) is not None:
                try:
                    return float(market[key])
                except (TypeError, ValueError):
                    continue
        return default

    def _normalize_kalshi_levels(self, levels: List[Any],
                                 already_dollars: bool = False) -> List[Dict[str, float]]:
        """Normalize Kalshi orderbook levels to {price, size} in dollars.

        ``already_dollars=True`` for the migrated ``orderbook_fp`` arrays whose
        prices are already in dollars (0..1); the legacy flat ``yes``/``no``
        arrays were in cents and still get converted.
        """
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
            if not already_dollars:
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
                # (A4) Bound the subscription to KALSHI_STREAM_SUBSCRIPTION_LIMIT —
                # subscribing to ALL markets risks high memory + feed overload.
                limit = Config.KALSHI_STREAM_SUBSCRIPTION_LIMIT
                tickers = list(self.markets_cache.keys())[:limit]
                await self.websocket_client.subscribe_to_all_markets(channels, market_tickers=tickers)
                logger.info(f"Subscribed to {len(tickers)} Kalshi markets (cap {limit})")

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
                'title': market_ticker,
                'platform': 'kalshi',
                'status': 'active',
                'clean_title': self._clean_title(market_ticker),
                'source': 'websocket',
                'last_updated': time.time()
            }
        else:
            self.markets_cache[market_ticker]['last_updated'] = time.time()

        # Periodically clean up stale WebSocket-only entries
        self._cleanup_stale_markets()
    
    def _cleanup_stale_markets(self):
        """Remove WebSocket-only market entries older than KALSHI_STALE_MARKET_TTL."""
        now = time.time()
        if now - self._last_market_cleanup < Config.KALSHI_MARKET_CLEANUP_INTERVAL:
            return
        self._last_market_cleanup = now
        stale_keys = []
        for ticker, market in self.markets_cache.items():
            if ticker in self._rest_discovered_tickers:
                continue
            if market.get('source') == 'websocket':
                last_updated = market.get('last_updated', 0)
                if now - last_updated > Config.KALSHI_STALE_MARKET_TTL:
                    stale_keys.append(ticker)
        for key in stale_keys:
            del self.markets_cache[key]
        if stale_keys:
            logger.info(f"Kalshi market cache cleanup: removed {len(stale_keys)} stale WebSocket-only entries, {len(self.markets_cache)} remain")

    def _clean_title(self, title: str) -> str:
        """Basic title cleaning."""
        return title.replace('-', ' ').replace('KX', '').lower().strip()

    async def _discover_markets_via_rest(self):
        """Discover all markets via REST API."""
        import aiohttp
        
        try:
            async with aiohttp.ClientSession(headers=Config.default_headers()) as session:
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
                # Loop until the cursor is exhausted; the cap is just a safety
                # bound so a runaway cursor can't spin forever.
                max_pages = Config.KALSHI_MAX_DISCOVERY_PAGES
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
                
                # No silent caps: warn if we stopped at the page cap with more
                # data still pending (discovery TRUNCATED), so it's visible.
                if page_count >= max_pages and cursor:
                    logger.warning(
                        "Kalshi markets pagination hit the %d-page cap (%d markets) "
                        "with a cursor still pending — discovery is TRUNCATED; "
                        "raise max_pages to capture the tail.",
                        max_pages, len(all_markets),
                    )
                # (B8) Record machine-readable catalog status so a partial-universe
                # scan is distinguishable from a true zero-opportunity scan.
                self.catalog_truncated = bool(page_count >= max_pages and cursor)
                self.catalog_pages_fetched = page_count
                logger.info(f"Discovered {len(all_markets)} total Kalshi markets via REST")

                # Process and cache the discovered markets
                for market in all_markets:
                    ticker = market.get('ticker')
                    if ticker:
                        self.discovered_tickers.append(ticker)
                        self._rest_discovered_tickers.add(ticker)
                        self.markets_cache[ticker] = {
                            'id': market.get('id'),
                            'ticker': ticker,
                            'title': market.get('title', ticker),
                            'platform': 'kalshi',
                            'status': market.get('status', 'active'),
                            'close_time': market.get('close_time'),
                            'clean_title': self._clean_title(market.get('title', ticker)),
                            'raw_data': market,
                            'source': 'rest',
                            'last_updated': time.time()
                        }
                
                logger.info(f"Cached {len(self.markets_cache)} Kalshi markets from REST discovery")

                # --- Phase 2: event-based discovery for individual markets ---
                # The default listing is dominated by multi-leg parlays.
                # Discover individual markets by iterating through events.
                await self._discover_individual_markets_via_events(session)

        except Exception as e:
            logger.error(f"Failed to discover Kalshi markets via REST: {e}")
            # Continue with empty cache - WebSocket might still provide some data

    async def _discover_individual_markets_via_events(self, session):
        """Discover individual (non-parlay) markets by querying open events.

        The default /markets listing is dominated by MULTIGAME parlays.
        This method fetches events, extracts unique series tickers, then
        queries markets per series to find individual matchable markets.
        """
        import aiohttp

        if not Config.KALSHI_EVENT_DISCOVERY_ENABLED:
            return

        try:
            # Step 1: Fetch open events (paginated to completion).
            all_events = []
            cursor = None
            timeout = aiohttp.ClientTimeout(total=15)
            for _ in range(Config.KALSHI_MAX_EVENT_PAGES):
                params = {'limit': 200, 'status': 'open'}
                if cursor:
                    params['cursor'] = cursor
                async with session.get(
                    f"{Config.KALSHI_API_BASE}/events", params=params, timeout=timeout
                ) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2)
                        continue
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    events = data.get('events', [])
                    if not events:
                        break
                    all_events.extend(events)
                    cursor = data.get('cursor')
                    if not cursor:
                        break

            logger.info(f"Event-based discovery: found {len(all_events)} open events")

            # Step 2: Extract unique series prefixes (first segment of event_ticker)
            series_set = set()
            for e in all_events:
                et = e.get('event_ticker', '')
                if not et or 'MULTIGAME' in et.upper():
                    continue
                series_set.add(et.split('-')[0])

            logger.info(f"Event-based discovery: {len(series_set)} unique series to query")

            # Step 3: Fetch markets per series with BOUNDED CONCURRENCY (the old
            # sequential 0.3s-per-series loop took ~2min for ~370 series and was
            # the matching bottleneck). A semaphore keeps us under Kalshi's rate
            # limit while running several series in flight.
            sem = asyncio.Semaphore(Config.KALSHI_EVENT_SERIES_CONCURRENCY)
            new_count = 0
            sweep_deadline = time.monotonic() + Config.KALSHI_EVENT_DISCOVERY_BUDGET_SECONDS

            async def _fetch_series(series):
                nonlocal new_count
                if time.monotonic() > sweep_deadline:
                    return  # time-boxed: don't blow the scan budget
                async with sem:
                    if time.monotonic() > sweep_deadline:
                        return
                    for attempt in range(4):
                        try:
                            params = {'limit': 200, 'series_ticker': series}
                            async with session.get(
                                f"{Config.KALSHI_API_BASE}/markets",
                                params=params, timeout=timeout,
                            ) as resp:
                                if resp.status == 429:
                                    await asyncio.sleep(1.5 * (attempt + 1))
                                    continue
                                if resp.status != 200:
                                    return
                                data = await resp.json()
                                break
                        except asyncio.TimeoutError:
                            return
                        except Exception as e:
                            logger.debug(f"Error fetching series {series}: {e}")
                            return
                    else:
                        return  # exhausted retries (kept getting 429)

                    for market in data.get('markets', []):
                        ticker = market.get('ticker', '')
                        if not ticker or 'MULTIGAME' in ticker.upper():
                            continue
                        if ticker in self.markets_cache:
                            continue
                        self.discovered_tickers.append(ticker)
                        self._rest_discovered_tickers.add(ticker)
                        self.markets_cache[ticker] = {
                            'id': market.get('id'),
                            'ticker': ticker,
                            'title': market.get('title', ticker),
                            'platform': 'kalshi',
                            'status': market.get('status', 'active'),
                            'close_time': market.get('close_time'),
                            'clean_title': self._clean_title(market.get('title', ticker)),
                            'raw_data': market,
                            'source': 'rest_event',
                            'last_updated': time.time(),
                        }
                        new_count += 1

            await asyncio.gather(*(_fetch_series(s) for s in sorted(series_set)))

            logger.info(
                f"Event-based discovery added {new_count} individual markets "
                f"(total cache: {len(self.markets_cache)})"
            )
        except Exception as e:
            logger.warning(f"Event-based discovery failed (non-fatal): {e}")

    async def _handle_orderbook_update(self, message):
        """Handle real-time Kalshi orderbook messages.

        (B2) SAFETY: Kalshi's 'orderbook_delta' channel sends an initial snapshot
        then INCREMENTAL deltas, and this client has no snapshot baseline /
        sequence-application logic. Treating a delta as a full book (the previous
        behavior — it overwrote the entire cached book from one delta message)
        produced corrupted best bid/ask and false arbitrage. Until a proper
        snapshot + sequenced-delta state machine exists, we do NOT overwrite the
        executable orderbook cache from these messages; the opportunity path uses
        the REST orderbook fallback (a real, complete book) gated by the B3
        freshness check instead.
        """
        self._orderbook_delta_dropped = getattr(self, '_orderbook_delta_dropped', 0) + 1
        if self._orderbook_delta_dropped <= 3 or self._orderbook_delta_dropped % 500 == 0:
            logger.debug(
                "Kalshi orderbook_delta for %s (seq=%s) not applied as a full book "
                "(no snapshot/seq reconstruction); REST fallback supplies the book.",
                message.market_id, getattr(message, 'sequence', None),
            )
    
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

            async with aiohttp.ClientSession(headers=Config.default_headers()) as session:
                # Kalshi addresses markets by TICKER (our cache key), not the
                # internal numeric id — mirror the orderbook REST fallback below.
                # Using market.get('id') previously suppressed this fallback
                # whenever id was absent and hit the wrong path segment. (A7)
                url = f"{Config.KALSHI_API_BASE}/markets/{market_ticker}"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        market_data = data.get('market', {})

                        # Migrated field names: yes_bid_dollars / yes_ask_dollars
                        # are ALREADY in dollars (0..1) — do NOT divide by 100. The
                        # _fp helper falls back to legacy cents names, which the
                        # >1 guard below normalizes.
                        yes_bid = self._fp(market_data, 'yes_bid', default=0.0)
                        yes_ask = self._fp(market_data, 'yes_ask', default=0.0)
                        if yes_bid > 1:
                            yes_bid = yes_bid / 100.0
                        if yes_ask > 1:
                            yes_ask = yes_ask / 100.0

                        prices = {
                            'yes_price': yes_bid if yes_bid else 0.5,
                            'no_price': (1.0 - yes_ask) if yes_ask else 0.5,
                            'yes_ask': yes_ask if yes_ask else 0.5,
                            'yes_bid': yes_bid if yes_bid else 0.5,
                            'timestamp': time.time(),
                            'source': 'rest_fallback'
                        }
                        
                        # Cache the result
                        self.price_cache[market_ticker] = prices
                        return prices
                    
        except Exception as e:
            logger.warning(f"Failed to fetch prices via REST for {market_ticker}: {e}")
        
        return None
    
    def _book_is_fresh(self, book) -> bool:
        """Reuse a cached book only if recent enough (see PolymarketClient)."""
        if not isinstance(book, dict):
            return False
        ts = book.get('timestamp')
        if ts is None:
            return True
        max_age = getattr(Config, 'ESTIMATED_MAX_ORDERBOOK_AGE_SECONDS', 30)
        return (time.time() - float(ts)) <= max_age

    async def get_market_orderbook(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Get market orderbook from cache, with REST fallback."""
        result = self.orderbook_cache.get(market_ticker)
        if result is not None and self._book_is_fresh(result):
            return result
        # Cache miss or STALE cached book → fetch fresh (budget-limited).
        if Config.ORDERBOOK_REST_FALLBACK and self._orderbook_rest_budget > 0:
            self._orderbook_rest_budget -= 1
            fresh = await self._fetch_orderbook_rest(market_ticker)
            if fresh is not None:
                return fresh
        return result  # fall back to stale rather than nothing

    async def _fetch_orderbook_rest(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Fetch orderbook via REST API as fallback when WebSocket cache misses."""
        import aiohttp
        try:
            async with self._orderbook_rest_semaphore:
                now = time.time()
                elapsed = now - self._orderbook_rest_last_call
                if elapsed < 0.5:  # Max ~2/sec to stay under Kalshi rate limits
                    await asyncio.sleep(0.5 - elapsed)
                self._orderbook_rest_last_call = time.time()

                url = f"{Config.KALSHI_API_BASE}/markets/{market_ticker}/orderbook"
                if self._orderbook_rest_session is None or self._orderbook_rest_session.closed:
                    self._orderbook_rest_session = aiohttp.ClientSession(headers=Config.default_headers())
                async with self._orderbook_rest_session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            normalized = self._parse_orderbook_payload(data)
                            self.orderbook_cache[market_ticker] = normalized
                            return normalized
                        else:
                            logger.debug(f"REST orderbook fetch failed for {market_ticker}: HTTP {resp.status}")
                            return None
        except Exception as e:
            logger.debug(f"REST orderbook fetch error for {market_ticker}: {e}")
            return None

    def _parse_orderbook_payload(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a Kalshi REST orderbook response into yes/no asks+bids (dollars).

        Kalshi migrated the structure to nested fixed-point/dollars:
            data['orderbook']['orderbook_fp'] = {
              'yes_dollars': [[price_dollars_str, size_str], ...],  # YES bids
              'no_dollars':  [[price_dollars_str, size_str], ...],  # NO  bids
            }
        Prices are ALREADY in dollars (0..1) — no /100. A NO bid at X == a YES
        ask at (1-X). Falls back to the legacy flat 'yes'/'no' cents arrays only
        when the new structure is absent (graceful, never crashes).
        """
        orderbook = data.get('orderbook', data) or {}
        ob_fp = orderbook.get('orderbook_fp') or {}
        yes_raw = ob_fp.get('yes_dollars')
        no_raw = ob_fp.get('no_dollars')
        legacy = yes_raw is None and no_raw is None
        if legacy:
            # Pre-migration shape: flat 'yes'/'no' arrays in cents.
            yes_raw = orderbook.get('yes')
            no_raw = orderbook.get('no')

        yes_bids = self._normalize_kalshi_levels(
            yes_raw if isinstance(yes_raw, list) else [], already_dollars=not legacy)
        no_bids = self._normalize_kalshi_levels(
            no_raw if isinstance(no_raw, list) else [], already_dollars=not legacy)

        # Derive YES asks from NO bids: NO bid at X → YES ask at (1-X)
        yes_asks = [{'price': round(1.0 - lvl['price'], 4), 'size': lvl['size']}
                    for lvl in no_bids if lvl['price'] > 0.0]
        # Derive NO asks from YES bids: YES bid at X → NO ask at (1-X)
        no_asks = [{'price': round(1.0 - lvl['price'], 4), 'size': lvl['size']}
                   for lvl in yes_bids if lvl['price'] > 0.0]

        return {
            'yes_asks': yes_asks,
            'yes_bids': yes_bids,
            'no_asks': no_asks,
            'no_bids': no_bids,
            'timestamp': time.time(),
            'source': 'rest_fallback',
        }

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
        self._last_market_refresh = time.time()
        self._orderbook_rest_semaphore = asyncio.Semaphore(Config.ORDERBOOK_REST_MAX_PER_SECOND)
        self._orderbook_rest_last_call = 0.0
        self._orderbook_rest_session = None
        self._orderbook_rest_budget = Config.ORDERBOOK_REST_BUDGET_PER_SCAN
        self.catalog_truncated = False  # set True if REST discovery is incomplete
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
        """Bootstrap market discovery via the CLOB sampling-markets endpoint.

        We use CLOB ``/sampling-markets`` (cursor-paginated) rather than the
        Gamma ``/markets`` endpoint because:
          * Gamma caps ``limit`` at 100 and 422s on deep ``offset`` pagination,
            so it could only ever surface ~100 markets — the root cause of the
            bot seeing 100 markets and finding 0 arbs.
          * sampling-markets returns ONLY active, order-book-enabled markets —
            i.e. the actually-tradeable arbitrage universe (~5,600) — with the
            ``question``/``description``/``end_date_iso``/``tokens`` fields the
            matcher, verifier, and executor all need, in ~1.5s.
        """
        import aiohttp

        try:
            url = f"{Config.POLYMARKET_CLOB_BASE}/sampling-markets"
            all_markets = []
            cursor = ""
            page = 0
            max_pages = Config.POLYMARKET_MAX_DISCOVERY_PAGES

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout, headers=Config.default_headers()) as session:
                while page < max_pages:
                    params = {"next_cursor": cursor} if cursor else {}

                    # Retry/backoff per page; fail LOUD on exhaustion rather than
                    # silently returning a partial catalog as "end of data".
                    payload = None
                    last_err = None
                    for attempt in range(Config.DISCOVERY_FETCH_RETRIES):
                        try:
                            async with session.get(url, params=params) as response:
                                if response.status == 403:
                                    raise RuntimeError(
                                        "Polymarket CLOB 403 (Cloudflare) — check User-Agent header"
                                    )
                                response.raise_for_status()
                                payload = await response.json()
                            break
                        except Exception as e:
                            last_err = e
                            backoff = 2 ** attempt
                            logger.warning(
                                "Polymarket discovery page=%d attempt %d/%d failed: %s; retrying in %ds",
                                page, attempt + 1, Config.DISCOVERY_FETCH_RETRIES, e, backoff,
                            )
                            await asyncio.sleep(backoff)

                    if payload is None:
                        logger.error(
                            "Polymarket discovery FAILED at page=%d after %d retries: %s. "
                            "Catalog INCOMPLETE (%d markets so far).",
                            page, Config.DISCOVERY_FETCH_RETRIES, last_err, len(all_markets),
                        )
                        self.catalog_truncated = True
                        break

                    rows = payload.get("data", []) if isinstance(payload, dict) else []
                    if not rows:
                        break
                    all_markets.extend(rows)
                    page += 1
                    cursor = payload.get("next_cursor", "") if isinstance(payload, dict) else ""
                    # 'LTE=' is CLOB's base64 end-of-list sentinel.
                    if not cursor or cursor == "LTE=":
                        break

                if page >= max_pages:
                    logger.warning(
                        "Polymarket discovery hit the %d-page cap (%d markets); "
                        "raise POLYMARKET_MAX_DISCOVERY_PAGES if the catalog is larger.",
                        max_pages, len(all_markets),
                    )

            if not all_markets:
                logger.error(
                    "No markets from Polymarket CLOB API — discovery failed; matching "
                    "will have nothing to compare against."
                )
                return

            logger.info("Polymarket REST discovery complete: %d markets", len(all_markets))

            # Process and cache markets (CLOB sampling-markets schema).
            for market in all_markets:
                try:
                    if not market.get("enable_order_book") or market.get("closed"):
                        continue
                    market_id = market.get("condition_id")
                    question = market.get("question", "")
                    tokens = market.get("tokens", []) or []
                    if not market_id or not question or not tokens:
                        continue

                    outcomes = [t.get("outcome") for t in tokens]
                    clob_token_ids = [t.get("token_id") for t in tokens]

                    self.markets_cache[market_id] = {
                        "id": market_id,
                        "title": question,
                        "clean_title": self._clean_title(question),
                        "platform": "polymarket",
                        "status": "active",
                        "active": True,
                        "outcomes": outcomes,
                        "clob_token_ids": clob_token_ids,
                        # CLOB exposes resolution rules in 'description' and the
                        # close date in 'end_date_iso' — feed both through so the
                        # ResolutionCriteriaVerifier can use them.
                        "description": market.get("description", ""),
                        "endDate": market.get("end_date_iso"),
                        "raw_data": market,
                    }

                    self.asset_ids.extend(clob_token_ids)

                    for token in tokens:
                        token_id = token.get("token_id")
                        outcome = token.get("outcome")
                        if not token_id:
                            continue
                        price = self._safe_float(token.get("price"))
                        self.token_to_market[token_id] = market_id
                        self.token_outcome_map[token_id] = outcome
                        if not self._is_valid_probability(price):
                            continue
                        # The token 'price' is a last/mid price, NOT an executable
                        # bid/ask — flag executable=False so nothing mistakes
                        # buy==sell for a real spread (real books come from WS/REST).
                        self.price_cache.setdefault(market_id, {})[token_id] = {
                            "outcome": outcome,
                            "buy_price": price,
                            "sell_price": price,
                            "mid_price": price,
                            "last_price": price,
                            "executable": False,
                            "timestamp": time.time(),
                        }

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

        # (B11) a price_change/last echo is a single price, NOT an executable
        # bid/ask — expose it as mid/last and flag executable=False so nothing
        # treats buy_price==sell_price as a real tradeable spread. (Executable
        # arb math reads real orderbook levels, not these fields.)
        self.price_cache[market_id][token_id or 'default'] = {
            'outcome': message.data.get('outcome') or self.token_outcome_map.get(token_id),
            'buy_price': price,
            'sell_price': price,
            'mid_price': price,
            'last_price': price,
            'executable': False,
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
    
    async def refresh_markets(self):
        """Re-fetch the CLOB sampling-markets catalog, upserting into the cache.

        Uses the same CLOB cursor source as the bootstrap (NOT Gamma — which
        caps at 100 and would falsely tombstone the other ~5,500 markets).
        Snapshots the cache before fetching so it can tombstone markets that
        dropped out of the active set, but only after a COMPLETE sweep.
        """
        import aiohttp
        try:
            logger.info("Refreshing Polymarket markets from CLOB sampling-markets...")
            url = f"{Config.POLYMARKET_CLOB_BASE}/sampling-markets"
            cursor = ""
            page = 0
            max_pages = Config.POLYMARKET_MAX_DISCOVERY_PAGES
            added = updated = 0
            seen_ids = set()
            sweep_complete = False
            refresh_start = time.monotonic()
            max_refresh_seconds = 120

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout, headers=Config.default_headers()) as session:
                while page < max_pages:
                    if time.monotonic() - refresh_start > max_refresh_seconds:
                        logger.warning(f"Polymarket refresh timed out after {max_refresh_seconds}s")
                        break
                    params = {"next_cursor": cursor} if cursor else {}
                    try:
                        async with session.get(url, params=params) as response:
                            response.raise_for_status()
                            payload = await response.json()
                    except Exception as e:
                        logger.warning(f"Error during Polymarket refresh page {page}: {e}")
                        break

                    rows = payload.get("data", []) if isinstance(payload, dict) else []
                    if not rows:
                        sweep_complete = True
                        break
                    for market in rows:
                        if not market.get("enable_order_book") or market.get("closed"):
                            continue
                        market_id = market.get("condition_id")
                        question = market.get("question", "")
                        tokens = market.get("tokens", []) or []
                        if not market_id or not question or not tokens:
                            continue
                        seen_ids.add(market_id)
                        is_new = market_id not in self.markets_cache
                        outcomes = [t.get("outcome") for t in tokens]
                        clob_token_ids = [t.get("token_id") for t in tokens]
                        self.markets_cache[market_id] = {
                            "id": market_id, "title": question,
                            "clean_title": self._clean_title(question),
                            "platform": "polymarket", "status": "active", "active": True,
                            "outcomes": outcomes, "clob_token_ids": clob_token_ids,
                            "description": market.get("description", ""),
                            "endDate": market.get("end_date_iso"),
                            "raw_data": market,
                        }
                        for token in tokens:
                            tid = token.get("token_id")
                            if tid:
                                self.token_to_market[tid] = market_id
                                self.token_outcome_map[tid] = token.get("outcome")
                        added += int(is_new)
                        updated += int(not is_new)

                    page += 1
                    cursor = payload.get("next_cursor", "") if isinstance(payload, dict) else ""
                    if not cursor or cursor == "LTE=":
                        sweep_complete = True
                        break

            # Tombstone markets that dropped out of the active set — only after a
            # complete sweep, to avoid mass false tombstoning on a partial fetch.
            tombstoned = 0
            if sweep_complete:
                for _mid, _m in self.markets_cache.items():
                    if (_m.get('platform') == 'polymarket' and _mid not in seen_ids
                            and _m.get('active', True)):
                        _m['active'] = False
                        _m['status'] = 'closed'
                        _m.setdefault('quality_flags', []).append('not_seen_in_latest_refresh')
                        tombstoned += 1
            self._last_market_refresh = time.time()
            logger.info(f"Polymarket refresh complete: {added} new, {updated} updated, "
                        f"{tombstoned} tombstoned, {len(self.markets_cache)} total "
                        f"(sweep_complete={sweep_complete})")
        except Exception as e:
            logger.error(f"Failed to refresh Polymarket markets: {e}")

    def _book_is_fresh(self, book) -> bool:
        """A cached book is reusable only if recent enough for the estimated path.

        Without this, a book cached early in a scan goes stale (>max age) but is
        still RETURNED from cache here, then rejected by the caller's freshness
        gate — yielding None and a phantom 'no orderbook'. Treating a stale
        cached book as a miss forces a fresh REST re-fetch instead.
        """
        if not isinstance(book, dict):
            return False
        ts = book.get('timestamp')
        if ts is None:
            return True
        max_age = getattr(Config, 'ESTIMATED_MAX_ORDERBOOK_AGE_SECONDS', 30)
        return (time.time() - float(ts)) <= max_age

    async def get_market_orderbook(self, market_id: str, token_id: str = None) -> Optional[Dict[str, Any]]:
        """Get market orderbook from cache, with REST fallback."""
        market_orderbooks = self.orderbook_cache.get(market_id, {})
        if token_id:
            result = market_orderbooks.get(token_id) or market_orderbooks.get(self.token_outcome_map.get(token_id))
            if result is not None and self._book_is_fresh(result):
                return result
            # Cache miss or STALE cached book → fetch fresh (budget-limited).
            if Config.ORDERBOOK_REST_FALLBACK and self._orderbook_rest_budget > 0:
                self._orderbook_rest_budget -= 1
                fresh = await self._fetch_orderbook_rest(token_id)
                if fresh is not None:
                    return fresh
            return result  # fall back to the stale book rather than nothing
        if market_orderbooks:
            return market_orderbooks
        # REST fallback for first token if available
        market_data = self.markets_cache.get(market_id)
        if Config.ORDERBOOK_REST_FALLBACK and self._orderbook_rest_budget > 0 and market_data:
            clob_ids = market_data.get('clob_token_ids', [])
            if clob_ids:
                self._orderbook_rest_budget -= 1  # (A6) decrement like the token-specific path
                return await self._fetch_orderbook_rest(clob_ids[0])
        return market_orderbooks

    async def _fetch_orderbook_rest(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Fetch orderbook via CLOB REST API as fallback."""
        import aiohttp
        try:
            async with self._orderbook_rest_semaphore:
                now = time.time()
                elapsed = now - self._orderbook_rest_last_call
                if elapsed < 0.2:
                    await asyncio.sleep(0.2 - elapsed)
                self._orderbook_rest_last_call = time.time()

                url = f"{Config.POLYMARKET_CLOB_BASE}/book"
                params = {"token_id": token_id}
                if self._orderbook_rest_session is None or self._orderbook_rest_session.closed:
                    self._orderbook_rest_session = aiohttp.ClientSession(headers=Config.default_headers())
                async with self._orderbook_rest_session.get(url, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Normalize and store
                            bids = self._normalize_polymarket_levels(data.get('bids', []), side='bids')
                            asks = self._normalize_polymarket_levels(data.get('asks', []), side='asks')
                            orderbook = {
                                'bids': bids, 'asks': asks,
                                'timestamp': time.time(), 'source': 'rest_fallback'
                            }
                            market_id = self.token_to_market.get(token_id)
                            if market_id:
                                if market_id not in self.orderbook_cache:
                                    self.orderbook_cache[market_id] = {}
                                self.orderbook_cache[market_id][token_id] = orderbook
                            return orderbook
                        else:
                            logger.debug(f"REST orderbook fetch failed for Polymarket {token_id}: HTTP {resp.status}")
                            return None
        except Exception as e:
            logger.debug(f"REST orderbook fetch error for Polymarket {token_id}: {e}")
            return None

    def get_data_quality_stats(self) -> Dict[str, int]:
        """Expose Polymarket stream ingest quality counters."""
        return dict(self.stream_quality_stats)
