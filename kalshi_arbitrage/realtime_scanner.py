"""
Real-Time WebSocket Arbitrage Scanner

Production-ready scanner that detects arbitrage opportunities in real-time
by monitoring WebSocket price feeds from both Polymarket and Kalshi.

Key Features:
- Sub-second opportunity detection on price changes
- Maintains in-memory market state for instant analysis
- Callbacks for immediate notification of opportunities
- Automatic reconnection and fault tolerance
- Statistics and performance monitoring

This is designed to be a money-making engine when prediction markets are legal.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Any, Optional, Callable, Set
from dataclasses import dataclass, field
from collections import defaultdict
import aiohttp

from .arbitrage_engine import ArbitrageEngine, ArbitrageOpportunity, ArbitrageType
from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class MarketState:
    """In-memory state for a single market."""
    market_id: str
    platform: str
    title: str
    outcomes: List[str]
    prices: Dict[str, Decimal]  # outcome -> price
    last_update: float
    token_ids: List[str] = field(default_factory=list)
    raw_data: Dict = field(default_factory=dict)


class RealTimeArbitrageScanner:
    """
    Production-ready real-time arbitrage scanner.

    Connects to WebSocket feeds and triggers arbitrage detection
    on every price update for sub-second opportunity detection.
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or Config.ARBITRAGE_CONFIG
        self.engine = ArbitrageEngine(self.config)

        # Market state (in-memory for speed)
        self.markets: Dict[str, MarketState] = {}  # market_id -> state
        self.market_index: Dict[str, Set[str]] = {
            'polymarket': set(),
            'kalshi': set()
        }

        # WebSocket connections
        self.ws_sessions: Dict[str, aiohttp.ClientSession] = {}
        self.ws_connections: Dict[str, aiohttp.ClientWebSocketResponse] = {}
        self.connected: Dict[str, bool] = {'polymarket': False, 'kalshi': False}

        # Callbacks
        self.opportunity_callbacks: List[Callable] = []
        self.price_update_callbacks: List[Callable] = []

        # Statistics
        self.stats = {
            'price_updates': 0,
            'scans_triggered': 0,
            'opportunities_found': 0,
            'last_opportunity': None,
            'start_time': None,
            'errors': 0
        }

        # Control
        self.running = False
        self._reconnect_tasks: Dict[str, asyncio.Task] = {}

        logger.info("RealTimeArbitrageScanner initialized")

    def on_opportunity(self, callback: Callable[[ArbitrageOpportunity], None]):
        """Register callback for when opportunity is detected."""
        self.opportunity_callbacks.append(callback)

    def on_price_update(self, callback: Callable[[str, str, Dict], None]):
        """Register callback for price updates (platform, market_id, prices)."""
        self.price_update_callbacks.append(callback)

    async def start(self):
        """Start the real-time scanner."""
        self.running = True
        self.stats['start_time'] = time.time()

        logger.info("=" * 60)
        logger.info("STARTING REAL-TIME ARBITRAGE SCANNER")
        logger.info("=" * 60)

        # Bootstrap market data via REST first
        await self._bootstrap_markets()

        # Connect to WebSockets
        await asyncio.gather(
            self._connect_polymarket(),
            self._connect_kalshi(),
            return_exceptions=True
        )

        logger.info(f"Scanner active | Markets: Poly={len(self.market_index['polymarket'])}, "
                   f"Kalshi={len(self.market_index['kalshi'])}")

        # Run initial full scan
        await self._full_scan()

    async def stop(self):
        """Stop the scanner and close connections."""
        self.running = False

        # Cancel reconnect tasks
        for task in self._reconnect_tasks.values():
            task.cancel()

        # Close WebSocket connections
        for platform, ws in self.ws_connections.items():
            if ws and not ws.closed:
                await ws.close()

        # Close sessions
        for session in self.ws_sessions.values():
            if session and not session.closed:
                await session.close()

        self._print_final_stats()
        logger.info("Scanner stopped")

    async def _bootstrap_markets(self):
        """Bootstrap market data via REST APIs."""
        logger.info("Bootstrapping market data via REST...")

        await asyncio.gather(
            self._bootstrap_polymarket(),
            self._bootstrap_kalshi(),
            return_exceptions=True
        )

        logger.info(f"Bootstrapped {len(self.markets)} total markets")

    async def _bootstrap_polymarket(self):
        """Fetch all Polymarket markets via REST."""
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"{Config.POLYMARKET_GAMMA_BASE}/markets"
                all_markets = []
                offset = 0

                while True:
                    params = {"active": "true", "closed": "false", "limit": 500, "offset": offset}

                    async with session.get(url, params=params) as response:
                        if response.status != 200:
                            logger.warning(f"Polymarket API returned {response.status}")
                            break

                        markets = await response.json()
                        if not markets:
                            break

                        all_markets.extend(markets)
                        logger.info(f"Polymarket: fetched {len(markets)} markets (total: {len(all_markets)})")

                        if len(markets) < 500:
                            break
                        offset += 500

                # Process markets
                for market in all_markets:
                    await self._process_polymarket_market(market)

                logger.info(f"Polymarket: {len(self.market_index['polymarket'])} markets loaded")

        except Exception as e:
            logger.error(f"Failed to bootstrap Polymarket: {e}")
            self.stats['errors'] += 1

    async def _bootstrap_kalshi(self):
        """Fetch all Kalshi markets via REST."""
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"{Config.KALSHI_API_BASE}/markets"
                all_markets = []
                cursor = None

                while True:
                    params = {"limit": 200}
                    if cursor:
                        params["cursor"] = cursor

                    async with session.get(url, params=params) as response:
                        if response.status != 200:
                            logger.warning(f"Kalshi API returned {response.status}")
                            break

                        data = await response.json()
                        markets = data.get('markets', [])

                        if not markets:
                            break

                        all_markets.extend(markets)
                        logger.info(f"Kalshi: fetched {len(markets)} markets (total: {len(all_markets)})")

                        cursor = data.get('cursor')
                        if not cursor:
                            break

                # Process markets
                for market in all_markets:
                    await self._process_kalshi_market(market)

                logger.info(f"Kalshi: {len(self.market_index['kalshi'])} markets loaded")

        except Exception as e:
            logger.error(f"Failed to bootstrap Kalshi: {e}")
            self.stats['errors'] += 1

    async def _process_polymarket_market(self, market: Dict):
        """Process and store a Polymarket market."""
        try:
            market_id = market.get('id')
            if not market_id:
                return

            outcomes = market.get('outcomes', [])
            outcome_prices = market.get('outcomePrices', [])
            token_ids = market.get('clobTokenIds', [])

            # Parse JSON strings if needed
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)

            # Build price dict
            prices = {}
            for i, (outcome, price_str) in enumerate(zip(outcomes, outcome_prices)):
                try:
                    prices[outcome] = Decimal(str(price_str))
                except:
                    pass

            # Store state
            self.markets[f"poly:{market_id}"] = MarketState(
                market_id=market_id,
                platform='polymarket',
                title=market.get('question', ''),
                outcomes=outcomes,
                prices=prices,
                last_update=time.time(),
                token_ids=token_ids,
                raw_data=market
            )
            self.market_index['polymarket'].add(market_id)

        except Exception as e:
            logger.debug(f"Error processing Polymarket market: {e}")

    async def _process_kalshi_market(self, market: Dict):
        """Process and store a Kalshi market."""
        try:
            ticker = market.get('ticker')
            if not ticker:
                return

            yes_bid = market.get('yes_bid', 0)
            yes_ask = market.get('yes_ask', 0)

            # Kalshi prices are in cents, convert to dollars
            yes_price = Decimal(str(yes_bid / 100)) if yes_bid else Decimal('0.5')
            no_price = Decimal('1') - (Decimal(str(yes_ask / 100)) if yes_ask else Decimal('0.5'))

            self.markets[f"kalshi:{ticker}"] = MarketState(
                market_id=ticker,
                platform='kalshi',
                title=market.get('title', ticker),
                outcomes=['Yes', 'No'],
                prices={'Yes': yes_price, 'No': no_price},
                last_update=time.time(),
                raw_data=market
            )
            self.market_index['kalshi'].add(ticker)

        except Exception as e:
            logger.debug(f"Error processing Kalshi market: {e}")

    async def _connect_polymarket(self):
        """Connect to Polymarket WebSocket."""
        endpoint = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

        try:
            self.ws_sessions['polymarket'] = aiohttp.ClientSession()
            self.ws_connections['polymarket'] = await self.ws_sessions['polymarket'].ws_connect(
                endpoint,
                heartbeat=30
            )
            self.connected['polymarket'] = True
            logger.info("Connected to Polymarket WebSocket")

            # Subscribe to markets
            token_ids = []
            for mid in list(self.market_index['polymarket'])[:200]:  # Limit subscriptions
                state = self.markets.get(f"poly:{mid}")
                if state and state.token_ids:
                    token_ids.extend(state.token_ids)

            if token_ids:
                subscribe_msg = {
                    "assets_ids": token_ids[:500],  # Limit
                    "type": "market"
                }
                await self.ws_connections['polymarket'].send_str(json.dumps(subscribe_msg))
                logger.info(f"Subscribed to {len(token_ids[:500])} Polymarket assets")

            # Start message loop
            asyncio.create_task(self._polymarket_message_loop())

        except Exception as e:
            logger.error(f"Failed to connect to Polymarket WebSocket: {e}")
            self.connected['polymarket'] = False
            self._schedule_reconnect('polymarket')

    async def _connect_kalshi(self):
        """Connect to Kalshi WebSocket."""
        endpoint = "wss://api.elections.kalshi.com/trade-api/ws/v2"

        try:
            self.ws_sessions['kalshi'] = aiohttp.ClientSession()

            # Get auth headers if available
            headers = self._get_kalshi_auth_headers()

            self.ws_connections['kalshi'] = await self.ws_sessions['kalshi'].ws_connect(
                endpoint,
                headers=headers,
                heartbeat=30
            )
            self.connected['kalshi'] = True
            logger.info("Connected to Kalshi WebSocket")

            # Subscribe to all markets
            subscribe_msg = {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": ["ticker_v2"]
                }
            }
            await self.ws_connections['kalshi'].send_str(json.dumps(subscribe_msg))
            logger.info("Subscribed to Kalshi ticker feed")

            # Start message loop
            asyncio.create_task(self._kalshi_message_loop())

        except Exception as e:
            logger.error(f"Failed to connect to Kalshi WebSocket: {e}")
            self.connected['kalshi'] = False
            self._schedule_reconnect('kalshi')

    def _get_kalshi_auth_headers(self) -> Dict[str, str]:
        """Generate Kalshi auth headers if credentials available."""
        api_key = os.getenv('KALSHI_API_KEY')
        if not api_key:
            return {}

        try:
            import base64
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            key_path = os.getenv('KALSHI_PRIVATE_KEY_PATH', 'kalshi_private_key.pem')
            if not os.path.exists(key_path):
                return {}

            with open(key_path, 'rb') as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)

            timestamp = str(int(time.time() * 1000))
            msg = f"{timestamp}GET/trade-api/ws/v2"

            signature = private_key.sign(
                msg.encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256()
            )

            return {
                'KALSHI-ACCESS-KEY': api_key,
                'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode(),
                'KALSHI-ACCESS-TIMESTAMP': timestamp
            }
        except Exception as e:
            logger.debug(f"Could not generate Kalshi auth: {e}")
            return {}

    async def _polymarket_message_loop(self):
        """Process Polymarket WebSocket messages."""
        ws = self.ws_connections.get('polymarket')
        if not ws:
            return

        try:
            async for msg in ws:
                if not self.running:
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_polymarket_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.error(f"Polymarket message loop error: {e}")
        finally:
            self.connected['polymarket'] = False
            if self.running:
                self._schedule_reconnect('polymarket')

    async def _kalshi_message_loop(self):
        """Process Kalshi WebSocket messages."""
        ws = self.ws_connections.get('kalshi')
        if not ws:
            return

        try:
            async for msg in ws:
                if not self.running:
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_kalshi_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.error(f"Kalshi message loop error: {e}")
        finally:
            self.connected['kalshi'] = False
            if self.running:
                self._schedule_reconnect('kalshi')

    async def _handle_polymarket_message(self, raw_data: str):
        """Handle incoming Polymarket message."""
        try:
            data = json.loads(raw_data)
            self.stats['price_updates'] += 1

            # Extract price update
            if 'asset_id' in data or 'market' in data:
                market_id = data.get('market', data.get('condition_id', ''))

                # Update market state
                key = f"poly:{market_id}"
                if key in self.markets:
                    state = self.markets[key]

                    # Update prices from message
                    if 'price' in data:
                        outcome = data.get('outcome', 'Yes')
                        state.prices[outcome] = Decimal(str(data['price']))
                        state.last_update = time.time()

                    # Notify callbacks
                    for cb in self.price_update_callbacks:
                        try:
                            await cb('polymarket', market_id, dict(state.prices))
                        except:
                            pass

                    # Trigger arbitrage check
                    await self._check_arbitrage_for_market('polymarket', market_id)

        except Exception as e:
            logger.debug(f"Error handling Polymarket message: {e}")

    async def _handle_kalshi_message(self, raw_data: str):
        """Handle incoming Kalshi message."""
        try:
            data = json.loads(raw_data)

            # Handle ticker updates
            if data.get('type') == 'ticker_v2':
                self.stats['price_updates'] += 1
                msg = data.get('msg', {})
                ticker = msg.get('market_ticker', '')

                if ticker:
                    key = f"kalshi:{ticker}"

                    # Update or create market state
                    yes_bid = msg.get('yes_bid', 0)
                    yes_ask = msg.get('yes_ask', 0)

                    if key in self.markets:
                        state = self.markets[key]
                        state.prices['Yes'] = Decimal(str(yes_bid / 100)) if yes_bid else state.prices.get('Yes', Decimal('0.5'))
                        state.prices['No'] = Decimal('1') - (Decimal(str(yes_ask / 100)) if yes_ask else Decimal('0.5'))
                        state.last_update = time.time()
                    else:
                        self.markets[key] = MarketState(
                            market_id=ticker,
                            platform='kalshi',
                            title=ticker,
                            outcomes=['Yes', 'No'],
                            prices={
                                'Yes': Decimal(str(yes_bid / 100)) if yes_bid else Decimal('0.5'),
                                'No': Decimal('1') - (Decimal(str(yes_ask / 100)) if yes_ask else Decimal('0.5'))
                            },
                            last_update=time.time()
                        )
                        self.market_index['kalshi'].add(ticker)

                    # Notify callbacks
                    for cb in self.price_update_callbacks:
                        try:
                            await cb('kalshi', ticker, self.markets[key].prices)
                        except:
                            pass

                    # Trigger arbitrage check
                    await self._check_arbitrage_for_market('kalshi', ticker)

        except Exception as e:
            logger.debug(f"Error handling Kalshi message: {e}")

    async def _check_arbitrage_for_market(self, platform: str, market_id: str):
        """Quick arbitrage check for a single market update."""
        self.stats['scans_triggered'] += 1

        key = f"{platform[:4]}:{market_id}"  # poly: or kals:
        if platform == 'kalshi':
            key = f"kalshi:{market_id}"
        else:
            key = f"poly:{market_id}"

        state = self.markets.get(key)
        if not state:
            return

        # Build market dict for engine
        if platform == 'polymarket':
            markets = {
                market_id: {
                    'id': market_id,
                    'title': state.title,
                    'outcomes': state.outcomes,
                    'outcome_prices': [str(state.prices.get(o, '0')) for o in state.outcomes],
                    'clob_token_ids': state.token_ids
                }
            }

            # Check intra-market
            if len(state.outcomes) == 2:
                opps = await self.engine.scan_intra_market_polymarket(markets, {})
            else:
                opps = await self.engine.scan_multi_outcome_polymarket(markets, {})

            await self._handle_opportunities(opps)

        elif platform == 'kalshi':
            markets = {
                market_id: {
                    'id': market_id,
                    'ticker': market_id,
                    'title': state.title
                }
            }
            prices = {
                market_id: {
                    'yes_bid': float(state.prices.get('Yes', 0)),
                    'yes_ask': float(Decimal('1') - state.prices.get('No', Decimal('0.5')))
                }
            }

            opps = await self.engine.scan_intra_market_kalshi(markets, prices)
            await self._handle_opportunities(opps)

    async def _full_scan(self):
        """Run full scan across all markets."""
        logger.info("Running full market scan...")

        # Build market dicts
        poly_markets = {}
        poly_prices = {}
        kalshi_markets = {}
        kalshi_prices = {}

        for key, state in self.markets.items():
            if state.platform == 'polymarket':
                poly_markets[state.market_id] = {
                    'id': state.market_id,
                    'title': state.title,
                    'outcomes': state.outcomes,
                    'outcome_prices': [str(state.prices.get(o, '0')) for o in state.outcomes],
                    'clob_token_ids': state.token_ids
                }
            elif state.platform == 'kalshi':
                kalshi_markets[state.market_id] = {
                    'id': state.market_id,
                    'ticker': state.market_id,
                    'title': state.title
                }
                kalshi_prices[state.market_id] = {
                    'yes_bid': float(state.prices.get('Yes', 0)),
                    'yes_ask': float(Decimal('1') - state.prices.get('No', Decimal('0.5')))
                }

        # Run full scan
        opportunities = await self.engine.scan_all(
            poly_markets, poly_prices, kalshi_markets, kalshi_prices
        )

        await self._handle_opportunities(opportunities)
        logger.info(f"Full scan complete: {len(opportunities)} opportunities found")

    async def _handle_opportunities(self, opportunities: List[ArbitrageOpportunity]):
        """Handle detected opportunities."""
        for opp in opportunities:
            self.stats['opportunities_found'] += 1
            self.stats['last_opportunity'] = opp

            # Log it
            logger.info(f"🚨 OPPORTUNITY: {opp.market_title[:40]} | "
                       f"Profit: {float(opp.profit_percentage):.2f}% | "
                       f"Type: {opp.arb_type.value}")

            # Notify callbacks
            for callback in self.opportunity_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(opp)
                    else:
                        callback(opp)
                except Exception as e:
                    logger.error(f"Opportunity callback error: {e}")

    def _schedule_reconnect(self, platform: str):
        """Schedule reconnection for a platform."""
        if platform in self._reconnect_tasks:
            self._reconnect_tasks[platform].cancel()

        async def reconnect():
            await asyncio.sleep(5)  # Wait before reconnecting
            if self.running:
                if platform == 'polymarket':
                    await self._connect_polymarket()
                elif platform == 'kalshi':
                    await self._connect_kalshi()

        self._reconnect_tasks[platform] = asyncio.create_task(reconnect())

    def _print_final_stats(self):
        """Print final statistics."""
        runtime = time.time() - self.stats['start_time'] if self.stats['start_time'] else 0

        print("\n" + "=" * 60)
        print("FINAL STATISTICS")
        print("=" * 60)
        print(f"Runtime: {runtime:.1f}s")
        print(f"Price updates processed: {self.stats['price_updates']}")
        print(f"Arbitrage scans triggered: {self.stats['scans_triggered']}")
        print(f"Opportunities found: {self.stats['opportunities_found']}")
        print(f"Errors: {self.stats['errors']}")

        if self.stats['last_opportunity']:
            opp = self.stats['last_opportunity']
            print(f"\nLast opportunity:")
            print(f"  {opp.market_title[:50]}")
            print(f"  Profit: {float(opp.profit_percentage):.2f}%")

        print("=" * 60)

    def get_stats(self) -> Dict:
        """Get current statistics."""
        return {
            **self.stats,
            'markets_tracked': len(self.markets),
            'polymarket_connected': self.connected.get('polymarket', False),
            'kalshi_connected': self.connected.get('kalshi', False),
            'uptime': time.time() - self.stats['start_time'] if self.stats['start_time'] else 0
        }


async def run_realtime_scanner():
    """Main entry point for real-time scanner."""
    import signal

    scanner = RealTimeArbitrageScanner()

    # Register opportunity callback
    def on_opportunity(opp: ArbitrageOpportunity):
        print(f"\n{'='*60}")
        print(f"🚨 ARBITRAGE OPPORTUNITY DETECTED!")
        print(f"{'='*60}")
        print(f"Market: {opp.market_title}")
        print(f"Type: {opp.arb_type.value}")
        print(f"Profit: {float(opp.profit_percentage):.2f}%")
        print(f"Cost: ${float(opp.total_cost):.4f} → Payout: ${float(opp.guaranteed_payout):.2f}")
        print(f"Positions:")
        for pos in opp.positions:
            print(f"  - {pos.get('action')} {pos.get('side')} @ ${pos.get('price'):.4f}")
        print(f"{'='*60}\n")

    scanner.on_opportunity(on_opportunity)

    # Handle shutdown
    def shutdown(sig, frame):
        print("\nShutting down...")
        asyncio.create_task(scanner.stop())

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start scanner
    await scanner.start()

    # Keep running
    while scanner.running:
        await asyncio.sleep(1)

        # Periodic full scan every 60 seconds
        if int(time.time()) % 60 == 0:
            await scanner._full_scan()


if __name__ == '__main__':
    asyncio.run(run_realtime_scanner())
