"""
Enhanced WebSocket clients with circuit breakers, comprehensive error handling,
and advanced connection management for reliable real-time data streaming.
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
from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenError, circuit_breaker_manager
from .websocket_client import StreamMessage, WebSocketManager

logger = logging.getLogger(__name__)

class ConnectionHealth:
    """Tracks connection health metrics."""
    
    def __init__(self, window_size: int = 300):  # 5 minute window
        self.window_size = window_size
        self.latency_samples = deque(maxlen=1000)
        self.message_timestamps = deque(maxlen=10000)
        self.error_timestamps = deque(maxlen=1000)
        self.last_heartbeat = time.time()
        self.connection_start = time.time()
        
    def record_message(self, latency: float = None):
        """Record successful message receipt."""
        self.message_timestamps.append(time.time())
        if latency is not None:
            self.latency_samples.append(latency)
    
    def record_error(self):
        """Record error occurrence."""
        self.error_timestamps.append(time.time())
    
    def get_metrics(self) -> Dict[str, Any]:
        """Calculate health metrics."""
        now = time.time()
        window_start = now - self.window_size
        
        # Message rate
        recent_messages = [ts for ts in self.message_timestamps if ts > window_start]
        message_rate = len(recent_messages) / self.window_size if recent_messages else 0
        
        # Error rate
        recent_errors = [ts for ts in self.error_timestamps if ts > window_start]
        error_rate = len(recent_errors) / len(recent_messages) if recent_messages else 0
        
        # Latency stats
        avg_latency = sum(self.latency_samples) / len(self.latency_samples) if self.latency_samples else 0
        p95_latency = sorted(self.latency_samples)[int(len(self.latency_samples) * 0.95)] if self.latency_samples else 0
        
        # Connection uptime
        uptime = now - self.connection_start
        
        return {
            "message_rate_per_sec": message_rate,
            "error_rate": error_rate,
            "avg_latency_ms": avg_latency * 1000,
            "p95_latency_ms": p95_latency * 1000,
            "uptime_seconds": uptime,
            "total_messages": len(self.message_timestamps),
            "total_errors": len(self.error_timestamps),
            "health_score": self._calculate_health_score(message_rate, error_rate, avg_latency)
        }
    
    def _calculate_health_score(self, message_rate: float, error_rate: float, avg_latency: float) -> float:
        """Calculate overall health score (0-100)."""
        # Base score
        score = 100.0
        
        # Penalize low message rate
        if message_rate < 0.1:  # Less than 1 message per 10 seconds
            score -= 30
        elif message_rate < 1:  # Less than 1 message per second
            score -= 10
            
        # Penalize high error rate
        score -= min(50, error_rate * 100)
        
        # Penalize high latency
        if avg_latency > 1:  # Over 1 second
            score -= 20
        elif avg_latency > 0.5:  # Over 500ms
            score -= 10
            
        return max(0, score)

class EnhancedWebSocketManager(WebSocketManager):
    """Enhanced WebSocket manager with circuit breakers and advanced error handling."""
    
    def __init__(self, platform: str, endpoint: str, config: Dict):
        super().__init__(platform, endpoint, config)
        
        # Circuit breaker configuration
        cb_config = CircuitBreakerConfig(
            failure_threshold=config.get('circuit_breaker_failures', 5),
            recovery_timeout=config.get('circuit_breaker_timeout', 60),
            success_threshold=config.get('circuit_breaker_successes', 3)
        )
        self.circuit_breaker = circuit_breaker_manager.get_or_create(
            f"{platform}_websocket", 
            cb_config
        )
        
        # Connection health tracking
        self.health_tracker = ConnectionHealth()
        
        # Enhanced retry configuration
        self.max_retry_delay = config.get('max_retry_delay', 300)  # 5 minutes max
        self.retry_jitter = config.get('retry_jitter', 0.1)  # 10% jitter
        
        # Message validation
        self.validate_messages = config.get('validate_messages', True)
        self.max_message_age = config.get('max_message_age', 60)  # Reject messages older than 60s
        
        # Connection quality monitoring
        self.min_health_score = config.get('min_health_score', 50)
        self.health_check_interval = config.get('health_check_interval', 30)
        self._health_check_task = None
        
    async def connect(self):
        """Enhanced connection with circuit breaker protection."""
        try:
            # Use circuit breaker for connection
            await self.circuit_breaker.call(self._connect_internal)
            
            # Start health monitoring
            if self._health_check_task is None:
                self._health_check_task = asyncio.create_task(self._health_monitor())
                
        except CircuitOpenError:
            logger.error(f"Circuit breaker OPEN for {self.platform}, skipping connection")
            raise
        except Exception as e:
            logger.error(f"Failed to connect to {self.platform}: {e}")
            raise
    
    async def _connect_internal(self):
        """Internal connection logic wrapped by circuit breaker."""
        if self.session is None:
            self.session = aiohttp.ClientSession()
            
        logger.info(f"Connecting to {self.platform} WebSocket at {self.endpoint}")
        
        # Add connection timeout and proper error handling
        try:
            self.websocket = await asyncio.wait_for(
                self.session.ws_connect(
                    self.endpoint,
                    heartbeat=self.config.get('heartbeat_interval', 30),
                    timeout=aiohttp.ClientTimeout(total=30),
                    autoclose=True,
                    autoping=True
                ),
                timeout=self.config.get('connection_timeout', 30)
            )
            
            self.is_connected = True
            self.reconnect_attempts = 0
            self.last_heartbeat = time.time()
            self.health_tracker.connection_start = time.time()
            
            logger.info(f"Successfully connected to {self.platform} WebSocket")
            
            # Start message handling task
            asyncio.create_task(self._enhanced_message_loop())
            
        except asyncio.TimeoutError:
            raise ConnectionError(f"Connection timeout for {self.platform}")
        except Exception as e:
            raise ConnectionError(f"Failed to establish WebSocket connection: {e}")
    
    async def _enhanced_message_loop(self):
        """Enhanced message loop with comprehensive error handling."""
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        try:
            async for msg in self.websocket:
                try:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_text_message(msg.data)
                        consecutive_errors = 0  # Reset on success
                        
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WebSocket error: {self.websocket.exception()}")
                        self.health_tracker.record_error()
                        consecutive_errors += 1
                        
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        logger.warning(f"WebSocket closed: {msg}")
                        break
                        
                    elif msg.type == aiohttp.WSMsgType.PONG:
                        # Handle pong messages for latency tracking
                        latency = time.time() - self.last_heartbeat
                        self.health_tracker.record_message(latency)
                        
                    # Check for excessive consecutive errors
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(f"Too many consecutive errors ({consecutive_errors}), closing connection")
                        break
                        
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    self.health_tracker.record_error()
                    consecutive_errors += 1
                    
        except Exception as e:
            logger.error(f"Fatal error in message loop for {self.platform}: {e}")
        finally:
            self.is_connected = False
            await self._handle_disconnection()
    
    async def _handle_text_message(self, raw_data: str):
        """Handle text message with validation and timing."""
        receive_time = time.time()
        
        try:
            # Parse JSON
            data = json.loads(raw_data)
            
            # Validate and parse message
            message = await self._parse_and_validate_message(data, receive_time)
            
            if message:
                # Record successful message
                self.health_tracker.record_message()
                self.stats['messages_received'] += 1
                self.stats['last_message_time'] = datetime.now()
                
                # Add to queue for offline resilience
                self.message_queue.append(message)
                
                # Dispatch to handlers
                await self._dispatch_to_handlers(message)
                
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from {self.platform}: {e}")
            self.health_tracker.record_error()
        except Exception as e:
            logger.error(f"Failed to process message from {self.platform}: {e}")
            self.health_tracker.record_error()
    
    async def _parse_and_validate_message(self, data: Dict, receive_time: float) -> Optional[StreamMessage]:
        """Parse and validate message with timing checks."""
        try:
            # Parse platform-specific message
            message = await self._parse_message(data)
            
            if not message:
                return None
                
            # Validate message age if configured
            if self.validate_messages and self.max_message_age > 0:
                message_age = receive_time - message.timestamp
                if message_age > self.max_message_age:
                    logger.warning(f"Rejecting stale message from {self.platform}, age: {message_age:.2f}s")
                    return None
                    
            return message
            
        except Exception as e:
            logger.error(f"Message validation failed: {e}")
            return None
    
    async def _dispatch_to_handlers(self, message: StreamMessage):
        """Dispatch message to handlers with error isolation."""
        handlers = self.message_handlers.get(message.channel, [])
        
        if not handlers:
            return
            
        # Run handlers concurrently with error isolation
        tasks = []
        for handler in handlers:
            task = asyncio.create_task(self._run_handler_safe(handler, message))
            tasks.append(task)
            
        # Wait for all handlers to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Count successful handlers
        successful = sum(1 for r in results if not isinstance(r, Exception))
        if successful > 0:
            self.stats['messages_processed'] += 1
    
    async def _run_handler_safe(self, handler: Callable, message: StreamMessage):
        """Run handler with timeout and error handling."""
        try:
            # Add timeout to prevent hanging handlers
            await asyncio.wait_for(
                handler(message),
                timeout=self.config.get('handler_timeout', 5)
            )
        except asyncio.TimeoutError:
            logger.error(f"Handler timeout for {message.channel} on {self.platform}")
        except Exception as e:
            logger.error(f"Handler error for {message.channel} on {self.platform}: {e}")
    
    async def _handle_disconnection(self):
        """Enhanced disconnection handling."""
        logger.warning(f"Handling disconnection for {self.platform}")
        
        # Clean up resources
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()
            
        # Check health before reconnection
        health_metrics = self.health_tracker.get_metrics()
        if health_metrics['health_score'] < self.min_health_score:
            logger.warning(f"Poor connection health for {self.platform} (score: {health_metrics['health_score']}), delaying reconnection")
            await asyncio.sleep(30)  # Extra delay for poor connections
            
        # Proceed with reconnection
        await self._handle_reconnection()
    
    async def _handle_reconnection(self):
        """Enhanced reconnection with jitter and circuit breaker awareness."""
        if self.circuit_breaker.get_state().value == "open":
            logger.info(f"Circuit breaker OPEN for {self.platform}, skipping reconnection")
            return
            
        if self.reconnect_attempts >= self.config.get('max_reconnect_attempts', 10):
            logger.error(f"Max reconnection attempts reached for {self.platform}")
            return
            
        self.reconnect_attempts += 1
        self.stats['reconnections'] += 1
        
        # Calculate backoff with jitter
        base_delay = min(self.max_retry_delay, 2 ** self.reconnect_attempts)
        jitter = base_delay * self.retry_jitter * (2 * asyncio.create_task(self._random()) - 1)
        backoff_time = base_delay + jitter
        
        logger.info(f"Reconnecting to {self.platform} in {backoff_time:.1f}s (attempt {self.reconnect_attempts})")
        await asyncio.sleep(backoff_time)
        
        try:
            await self.connect()
        except Exception as e:
            logger.error(f"Reconnection failed for {self.platform}: {e}")
    
    async def _random(self) -> float:
        """Async-safe random number generator."""
        import random
        return random.random()
    
    async def _health_monitor(self):
        """Monitor connection health and take corrective actions."""
        while self.is_connected:
            try:
                await asyncio.sleep(self.health_check_interval)
                
                # Get health metrics
                metrics = self.health_tracker.get_metrics()
                health_score = metrics['health_score']
                
                logger.debug(f"{self.platform} health score: {health_score}, metrics: {metrics}")
                
                # Take action if health is poor
                if health_score < self.min_health_score:
                    logger.warning(f"Poor health detected for {self.platform} (score: {health_score})")
                    
                    # Consider reconnecting if health is very poor
                    if health_score < 20:
                        logger.error(f"Critical health for {self.platform}, forcing reconnection")
                        await self.disconnect()
                        break
                        
            except Exception as e:
                logger.error(f"Error in health monitor for {self.platform}: {e}")
    
    async def send_message(self, message: Dict) -> bool:
        """Send message with circuit breaker protection."""
        if not self.is_connected or not self.websocket:
            return False
            
        try:
            await self.circuit_breaker.call(
                self._send_message_internal,
                message
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send message to {self.platform}: {e}")
            return False
    
    async def _send_message_internal(self, message: Dict):
        """Internal message sending logic."""
        message_str = json.dumps(message)
        await self.websocket.send_str(message_str)
        logger.debug(f"Sent message to {self.platform}: {message_str[:100]}...")
    
    def get_enhanced_stats(self) -> Dict[str, Any]:
        """Get enhanced statistics including health metrics."""
        base_stats = self.get_stats()
        health_metrics = self.health_tracker.get_metrics()
        circuit_breaker_stats = self.circuit_breaker.get_stats()
        
        return {
            **base_stats,
            "health": health_metrics,
            "circuit_breaker": circuit_breaker_stats
        }

class RateLimiter:
    """Rate limiter for API calls and WebSocket messages."""
    
    def __init__(self, rate: int, per: float):
        """
        Initialize rate limiter.
        
        Args:
            rate: Number of allowed calls
            per: Time period in seconds
        """
        self.rate = rate
        self.per = per
        self.calls = deque()
        self._lock = asyncio.Lock()
        
    async def acquire(self):
        """Acquire permission to make a call."""
        async with self._lock:
            now = time.time()
            
            # Remove old calls outside the window
            cutoff = now - self.per
            while self.calls and self.calls[0] < cutoff:
                self.calls.popleft()
            
            # Check if we can make a call
            if len(self.calls) >= self.rate:
                # Calculate wait time
                oldest_call = self.calls[0]
                wait_time = self.per - (now - oldest_call) + 0.01
                await asyncio.sleep(wait_time)
                
                # Recursive call after waiting
                return await self.acquire()
            
            # Record this call
            self.calls.append(now)

class MessageDeduplicator:
    """Deduplicates messages to prevent processing duplicates."""
    
    def __init__(self, ttl: int = 60):
        """
        Initialize deduplicator.
        
        Args:
            ttl: Time to live for message hashes in seconds
        """
        self.ttl = ttl
        self.seen_messages = {}
        self._cleanup_interval = 30
        self._cleanup_task = None
        
    async def start(self):
        """Start the cleanup task."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
    
    async def stop(self):
        """Stop the cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
    
    def is_duplicate(self, message: StreamMessage) -> bool:
        """Check if message is a duplicate."""
        # Create message hash
        message_hash = self._hash_message(message)
        
        # Check if we've seen it
        if message_hash in self.seen_messages:
            return True
            
        # Record new message
        self.seen_messages[message_hash] = time.time()
        return False
    
    def _hash_message(self, message: StreamMessage) -> str:
        """Create a hash for the message."""
        # Include key fields that identify unique messages
        key_data = f"{message.platform}:{message.channel}:{message.market_id}:{message.timestamp}"
        if message.sequence is not None:
            key_data += f":{message.sequence}"
        return key_data
    
    async def _cleanup_loop(self):
        """Periodically clean up old message hashes."""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                self._cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in deduplicator cleanup: {e}")
    
    def _cleanup(self):
        """Remove old message hashes."""
        now = time.time()
        cutoff = now - self.ttl
        
        # Remove old entries
        self.seen_messages = {
            hash_: timestamp 
            for hash_, timestamp in self.seen_messages.items()
            if timestamp > cutoff
        }