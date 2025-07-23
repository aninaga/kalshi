"""
WebSocket clients for real-time data streams from Kalshi and Polymarket.
Implements Phase 2 of lossless arbitrage detection: eliminating cache staleness.
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Callable, Optional, Any, Set
from datetime import datetime, timedelta
from dataclasses import dataclass
import aiohttp
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

@dataclass
class StreamMessage:
    """Represents a streaming message from either platform."""
    platform: str
    channel: str
    market_id: str
    data: Dict[str, Any]
    timestamp: float
    sequence: Optional[int] = None

class WebSocketManager:
    """Base class for WebSocket connection management."""
    
    def __init__(self, platform: str, endpoint: str, config: Dict):
        self.platform = platform
        self.endpoint = endpoint
        self.config = config
        self.session = None
        self.websocket = None
        self.is_connected = False
        self.reconnect_attempts = 0
        self.last_heartbeat = time.time()
        self.message_handlers = defaultdict(list)
        self.subscribed_markets = set()
        self.message_queue = deque(maxlen=1000)  # Buffer for offline periods
        self.stats = {
            'messages_received': 0,
            'messages_processed': 0,
            'reconnections': 0,
            'last_message_time': None
        }
        
    async def connect(self):
        """Establish WebSocket connection."""
        if self.session is None:
            self.session = aiohttp.ClientSession()
            
        try:
            logger.info(f"Connecting to {self.platform} WebSocket at {self.endpoint}")
            self.websocket = await self.session.ws_connect(
                self.endpoint,
                heartbeat=self.config.get('heartbeat_interval', 30),
                timeout=aiohttp.ClientTimeout(total=30)
            )
            self.is_connected = True
            self.reconnect_attempts = 0
            self.last_heartbeat = time.time()
            logger.info(f"Successfully connected to {self.platform} WebSocket")
            
            # Start message handling task
            asyncio.create_task(self._message_loop())
            
        except Exception as e:
            logger.error(f"Failed to connect to {self.platform} WebSocket: {e}")
            await self._handle_reconnection()
    
    async def disconnect(self):
        """Gracefully disconnect from WebSocket."""
        self.is_connected = False
        if self.websocket:
            await self.websocket.close()
        if self.session:
            await self.session.close()
        logger.info(f"Disconnected from {self.platform} WebSocket")
    
    async def _message_loop(self):
        """Main message processing loop."""
        try:
            async for msg in self.websocket:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                    self.stats['messages_received'] += 1
                    self.stats['last_message_time'] = datetime.now()
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {self.websocket.exception()}")
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    logger.warning(f"WebSocket closed: {msg}")
                    break
        except Exception as e:
            logger.error(f"Error in message loop for {self.platform}: {e}")
        finally:
            self.is_connected = False
            await self._handle_reconnection()
    
    async def _handle_message(self, raw_data: str):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(raw_data)
            message = await self._parse_message(data)
            if message:
                # Add to queue for offline resilience
                self.message_queue.append(message)
                
                # Dispatch to handlers
                for handler in self.message_handlers[message.channel]:
                    try:
                        await handler(message)
                        self.stats['messages_processed'] += 1
                    except Exception as e:
                        logger.error(f"Error in message handler: {e}")
        except Exception as e:
            logger.error(f"Failed to parse message from {self.platform}: {e}")
    
    async def _parse_message(self, data: Dict) -> Optional[StreamMessage]:
        """Parse platform-specific message format. Override in subclasses."""
        raise NotImplementedError
    
    async def _handle_reconnection(self):
        """Handle reconnection logic with exponential backoff."""
        if self.reconnect_attempts >= self.config.get('max_reconnect_attempts', 10):
            logger.error(f"Max reconnection attempts reached for {self.platform}")
            return
            
        self.reconnect_attempts += 1
        self.stats['reconnections'] += 1
        backoff_time = min(300, 2 ** self.reconnect_attempts)  # Max 5 minutes
        
        logger.info(f"Reconnecting to {self.platform} in {backoff_time} seconds (attempt {self.reconnect_attempts})")
        await asyncio.sleep(backoff_time)
        await self.connect()
    
    def add_message_handler(self, channel: str, handler: Callable):
        """Add a message handler for a specific channel."""
        self.message_handlers[channel].append(handler)
    
    def get_stats(self) -> Dict:
        """Get connection and processing statistics."""
        return {
            'platform': self.platform,
            'is_connected': self.is_connected,
            'reconnect_attempts': self.reconnect_attempts,
            'subscribed_markets': len(self.subscribed_markets),
            'queued_messages': len(self.message_queue),
            **self.stats
        }

class KalshiWebSocketClient(WebSocketManager):
    """Kalshi-specific WebSocket client implementation."""
    
    def __init__(self, config: Dict, auth_token: Optional[str] = None):
        endpoint = config.get('endpoint', 'wss://api.elections.kalshi.com/trade-api/ws/v2')
        super().__init__('kalshi', endpoint, config)
        self.command_id = 0
        self.pending_commands = {}
        self.auth_token = auth_token
        
    def _get_kalshi_auth_headers(self) -> Dict[str, str]:
        """Generate Kalshi RSA authentication headers for WebSocket connection."""
        import os
        import time
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        
        kalshi_api_key = os.getenv('KALSHI_API_KEY')
        kalshi_private_key = os.getenv('KALSHI_PRIVATE_KEY')
        
        if not kalshi_api_key or not kalshi_private_key:
            return {}
        
        try:
            # Load private key
            private_key = serialization.load_pem_private_key(
                kalshi_private_key.encode('utf-8'),
                password=None
            )
            
            # Create timestamp
            timestamp = str(int(time.time() * 1000))
            
            # Create message to sign: timestamp + method + path
            # For WebSocket, we sign the connection request
            method = "GET"
            path = "/trade-api/ws/v2"  # Back to full path
            msg_string = timestamp + method + path
            
            # Sign the message using RSA-PSS
            message = msg_string.encode('utf-8')
            signature = private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )
            signature_b64 = base64.b64encode(signature).decode('utf-8')
            
            logger.debug(f"Kalshi WS auth - Method: {method}, Path: {path}, Timestamp: {timestamp}")
            
            return {
                'Content-Type': 'application/json',
                'KALSHI-ACCESS-KEY': kalshi_api_key,
                'KALSHI-ACCESS-SIGNATURE': signature_b64,
                'KALSHI-ACCESS-TIMESTAMP': timestamp
            }
        except Exception as e:
            logger.error(f"Error creating Kalshi WebSocket auth headers: {e}")
            return {}
    
    async def connect(self):
        """Establish WebSocket connection with RSA authentication."""
        if self.session is None:
            self.session = aiohttp.ClientSession()
            
        try:
            logger.info(f"Connecting to {self.platform} WebSocket at {self.endpoint}")
            
            # Generate Kalshi RSA authentication headers
            headers = self._get_kalshi_auth_headers()
            if headers:
                logger.info("Using RSA authentication for Kalshi WebSocket")
            else:
                logger.warning("No Kalshi authentication credentials available")
            
            self.websocket = await self.session.ws_connect(
                self.endpoint,
                headers=headers,
                heartbeat=self.config.get('heartbeat_interval', 30),
                timeout=aiohttp.ClientTimeout(total=30)
            )
            self.is_connected = True
            self.reconnect_attempts = 0
            self.last_heartbeat = time.time()
            logger.info(f"Successfully connected to {self.platform} WebSocket")
            
            # Start message handling task
            asyncio.create_task(self._message_loop())
            
        except Exception as e:
            if hasattr(e, 'status') and e.status == 401:
                logger.error(f"Kalshi WebSocket 401 Unauthorized - API key may not have WebSocket permissions")
                logger.error(f"Auth headers used: {[k for k in headers.keys()] if 'headers' in locals() else 'No headers'}")
            else:
                logger.error(f"Failed to connect to {self.platform} WebSocket: {e}")
            await self._handle_reconnection()
    
    async def _parse_message(self, data: Dict) -> Optional[StreamMessage]:
        """Parse Kalshi WebSocket message format."""
        # Handle command responses
        if 'id' in data and data['id'] in self.pending_commands:
            command_info = self.pending_commands.pop(data['id'])
            logger.debug(f"Received response for command {data['id']}: {command_info}")
            return None
        
        # Handle streaming updates
        if 'msg' in data:
            msg_type = data['msg']
            
            if msg_type == 'orderbook_delta':
                return StreamMessage(
                    platform='kalshi',
                    channel='orderbook',
                    market_id=data.get('market_ticker', ''),
                    data={
                        'yes_bids': data.get('yes', {}).get('bid', []),
                        'yes_asks': data.get('yes', {}).get('ask', []),
                        'no_bids': data.get('no', {}).get('bid', []),
                        'no_asks': data.get('no', {}).get('ask', []),
                        'sequence': data.get('seq', None)
                    },
                    timestamp=time.time(),
                    sequence=data.get('seq')
                )
            
            elif msg_type == 'ticker_v2':
                return StreamMessage(
                    platform='kalshi',
                    channel='ticker',
                    market_id=data.get('market_ticker', ''),
                    data={
                        'yes_bid': data.get('yes_bid'),
                        'yes_ask': data.get('yes_ask'),
                        'no_bid': data.get('no_bid'),
                        'no_ask': data.get('no_ask'),
                        'last_price': data.get('last_price'),
                        'volume': data.get('volume')
                    },
                    timestamp=time.time()
                )
            
            elif msg_type == 'trade':
                return StreamMessage(
                    platform='kalshi',
                    channel='trade',
                    market_id=data.get('market_ticker', ''),
                    data={
                        'side': data.get('side'),
                        'count': data.get('count'),
                        'price': data.get('price'),
                        'taker_side': data.get('taker_side'),
                        'trade_time': data.get('ts')
                    },
                    timestamp=time.time()
                )
        
        return None
    
    async def subscribe_to_markets(self, market_tickers: List[str], channels: List[str] = None):
        """Subscribe to real-time updates for specific markets."""
        if channels is None:
            channels = ['ticker_v2', 'orderbook_delta', 'trade']
        
        for channel in channels:
            command = {
                "id": self._get_next_command_id(),
                "cmd": "subscribe",
                "params": {
                    "channels": [channel],
                    "market_tickers": market_tickers
                }
            }
            
            await self._send_command(command)
            self.subscribed_markets.update(market_tickers)
            logger.info(f"Subscribed to {channel} for {len(market_tickers)} Kalshi markets")
    
    async def subscribe_to_all_markets(self, channels: List[str] = None):
        """Subscribe to all active markets."""
        if channels is None:
            channels = ['ticker_v2']  # Start with ticker only to avoid overwhelming
        
        for channel in channels:
            command = {
                "id": self._get_next_command_id(),
                "cmd": "subscribe",
                "params": {
                    "channels": [channel]
                }
            }
            
            await self._send_command(command)
            logger.info(f"Subscribed to {channel} for all Kalshi markets")
    
    async def _send_command(self, command: Dict):
        """Send command to Kalshi WebSocket."""
        if not self.is_connected or not self.websocket:
            raise ConnectionError("WebSocket not connected")
        
        command_id = command['id']
        self.pending_commands[command_id] = {
            'command': command,
            'sent_time': time.time()
        }
        
        await self.websocket.send_str(json.dumps(command))
        logger.debug(f"Sent command {command_id}: {command['cmd']}")
    
    def _get_next_command_id(self) -> int:
        """Get next sequential command ID."""
        self.command_id += 1
        return self.command_id

class PolymarketWebSocketClient(WebSocketManager):
    """Polymarket-specific WebSocket client implementation."""
    
    def __init__(self, config: Dict):
        endpoint = config.get('endpoint', 'wss://ws-subscriptions.polymarket.com/')
        super().__init__('polymarket', endpoint, config)
        self.auth_token = config.get('auth_token')
    
    async def connect(self):
        """Connect with Polymarket-specific authentication."""
        await super().connect()
        
        # Send authentication after connection
        if self.auth_token:
            await self._authenticate()
    
    async def _authenticate(self):
        """Send authentication message to Polymarket."""
        auth_message = {
            "auth": self.auth_token,
            "type": "MARKET"
        }
        await self.websocket.send_str(json.dumps(auth_message))
        logger.info("Sent authentication to Polymarket WebSocket")
    
    async def _parse_message(self, data: Dict) -> Optional[StreamMessage]:
        """Parse Polymarket WebSocket message format."""
        # Handle market data updates
        if 'event_type' in data:
            event_type = data['event_type']
            
            if event_type == 'price_change':
                return StreamMessage(
                    platform='polymarket',
                    channel='price',
                    market_id=data.get('market_id', ''),
                    data={
                        'price': data.get('price'),
                        'outcome': data.get('outcome'),
                        'timestamp': data.get('timestamp')
                    },
                    timestamp=time.time()
                )
            
            elif event_type == 'order_book_update':
                return StreamMessage(
                    platform='polymarket',
                    channel='orderbook',
                    market_id=data.get('market_id', ''),
                    data={
                        'bids': data.get('bids', []),
                        'asks': data.get('asks', []),
                        'outcome': data.get('outcome'),
                        'sequence': data.get('sequence')
                    },
                    timestamp=time.time(),
                    sequence=data.get('sequence')
                )
        
        return None
    
    async def subscribe_to_markets(self, asset_ids: List[str]):
        """Subscribe to market updates for specific assets."""
        subscription_message = {
            "assets_ids": asset_ids,
            "type": "MARKET"
        }
        
        await self.websocket.send_str(json.dumps(subscription_message))
        self.subscribed_markets.update(asset_ids)
        logger.info(f"Subscribed to {len(asset_ids)} Polymarket assets")

class RealTimeDataManager:
    """Manages real-time data streams from both platforms."""
    
    def __init__(self, kalshi_config: Dict, polymarket_config: Dict, kalshi_auth_token: Optional[str] = None):
        self.kalshi_config = kalshi_config
        self.polymarket_config = polymarket_config
        
        kalshi_enabled = kalshi_config.get('enabled', False)
        polymarket_enabled = polymarket_config.get('enabled', False)
        
        logger.info(f"WebSocket config - Kalshi enabled: {kalshi_enabled}, Polymarket enabled: {polymarket_enabled}")
        
        self.kalshi_client = KalshiWebSocketClient(kalshi_config, kalshi_auth_token) if kalshi_enabled else None
        self.polymarket_client = PolymarketWebSocketClient(polymarket_config) if polymarket_enabled else None
        self.data_handlers = defaultdict(list)
        self.live_prices = {}  # market_id -> latest price data
        self.live_orderbooks = {}  # market_id -> latest orderbook
        self.last_update_times = {}  # market_id -> timestamp
        
    async def start(self):
        """Start enabled WebSocket connections."""
        logger.info("Starting real-time data streams...")
        
        # Set up data handlers
        self._setup_handlers()
        
        # Connect to enabled platforms only
        connections = []
        if self.kalshi_client:
            connections.append(self.kalshi_client.connect())
        if self.polymarket_client:
            connections.append(self.polymarket_client.connect())
            
        if connections:
            await asyncio.gather(*connections)
        
        enabled_platforms = []
        if self.kalshi_client:
            enabled_platforms.append("Kalshi")
        if self.polymarket_client:
            enabled_platforms.append("Polymarket")
            
        logger.info(f"Real-time data streams active for: {', '.join(enabled_platforms) if enabled_platforms else 'No platforms enabled'}")
    
    async def stop(self):
        """Stop all WebSocket connections."""
        disconnections = []
        if self.kalshi_client:
            disconnections.append(self.kalshi_client.disconnect())
        if self.polymarket_client:
            disconnections.append(self.polymarket_client.disconnect())
            
        if disconnections:
            await asyncio.gather(*disconnections)
        logger.info("Real-time data streams stopped")
    
    def _setup_handlers(self):
        """Set up message handlers for enabled platforms."""
        # Kalshi handlers
        if self.kalshi_client:
            self.kalshi_client.add_message_handler('ticker', self._handle_price_update)
            self.kalshi_client.add_message_handler('orderbook', self._handle_orderbook_update)
            self.kalshi_client.add_message_handler('trade', self._handle_trade_update)
        
        # Polymarket handlers
        if self.polymarket_client:
            self.polymarket_client.add_message_handler('price', self._handle_price_update)
            self.polymarket_client.add_message_handler('orderbook', self._handle_orderbook_update)
    
    async def _handle_price_update(self, message: StreamMessage):
        """Handle real-time price updates."""
        market_key = f"{message.platform}:{message.market_id}"
        self.live_prices[market_key] = message.data
        self.last_update_times[market_key] = message.timestamp
        
        # Notify handlers
        for handler in self.data_handlers['price_update']:
            await handler(message)
    
    async def _handle_orderbook_update(self, message: StreamMessage):
        """Handle real-time orderbook updates."""
        market_key = f"{message.platform}:{message.market_id}"
        self.live_orderbooks[market_key] = message.data
        self.last_update_times[market_key] = message.timestamp
        
        # Notify handlers
        for handler in self.data_handlers['orderbook_update']:
            await handler(message)
    
    async def _handle_trade_update(self, message: StreamMessage):
        """Handle real-time trade updates."""
        # Notify handlers
        for handler in self.data_handlers['trade_update']:
            await handler(message)
    
    def add_data_handler(self, event_type: str, handler: Callable):
        """Add handler for specific data events."""
        self.data_handlers[event_type].append(handler)
    
    def get_live_price(self, platform: str, market_id: str) -> Optional[Dict]:
        """Get the latest live price for a market."""
        market_key = f"{platform}:{market_id}"
        return self.live_prices.get(market_key)
    
    def get_live_orderbook(self, platform: str, market_id: str) -> Optional[Dict]:
        """Get the latest live orderbook for a market."""
        market_key = f"{platform}:{market_id}"
        return self.live_orderbooks.get(market_key)
    
    def get_data_freshness(self, platform: str, market_id: str) -> Optional[float]:
        """Get seconds since last update for a market."""
        market_key = f"{platform}:{market_id}"
        last_update = self.last_update_times.get(market_key)
        if last_update:
            return time.time() - last_update
        return None
    
    def get_connection_stats(self) -> Dict:
        """Get statistics for both connections."""
        stats = {
            'live_markets': len(self.live_prices),
            'data_handlers': {k: len(v) for k, v in self.data_handlers.items()}
        }
        if self.kalshi_client:
            stats['kalshi'] = self.kalshi_client.get_stats()
        if self.polymarket_client:
            stats['polymarket'] = self.polymarket_client.get_stats()
        return stats
    
    async def subscribe_to_markets(self, kalshi_tickers: List[str] = None, polymarket_assets: List[str] = None):
        """Subscribe to specific markets on both platforms."""
        tasks = []
        
        if kalshi_tickers:
            tasks.append(self.kalshi_client.subscribe_to_markets(kalshi_tickers))
        
        if polymarket_assets:
            tasks.append(self.polymarket_client.subscribe_to_markets(polymarket_assets))
        
        if tasks:
            await asyncio.gather(*tasks)
            logger.info(f"Subscribed to {len(kalshi_tickers or [])} Kalshi + {len(polymarket_assets or [])} Polymarket markets")