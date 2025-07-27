"""
Circuit breaker pattern implementation for WebSocket connections.
Provides fault tolerance and automatic recovery for streaming connections.
"""

import asyncio
import time
import logging
from enum import Enum
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failure threshold exceeded, blocking calls
    HALF_OPEN = "half_open"  # Testing if service recovered

@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""
    failure_threshold: int = 5          # Failures before opening circuit
    recovery_timeout: int = 60          # Seconds before trying half-open
    success_threshold: int = 3          # Successes in half-open before closing
    monitoring_window: int = 300        # Window for failure rate calculation (seconds)
    failure_rate_threshold: float = 0.5 # Failure rate to trigger open state
    
@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker monitoring."""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    circuit_opens: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    failure_timestamps: list = field(default_factory=list)
    
class CircuitBreaker:
    """
    Circuit breaker pattern for protecting WebSocket connections.
    
    Features:
    - Automatic failure detection and recovery
    - Configurable thresholds and timeouts
    - Detailed statistics and monitoring
    - Async-friendly design
    """
    
    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitState.CLOSED
        self.stats = CircuitBreakerStats()
        self._state_changed_at = time.time()
        self._lock = asyncio.Lock()
        self._on_state_change_handlers = []
        
    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute a function through the circuit breaker.
        
        Args:
            func: Async function to execute
            *args, **kwargs: Arguments to pass to the function
            
        Returns:
            Function result if successful
            
        Raises:
            CircuitOpenError: If circuit is open
            Original exception: If function fails
        """
        async with self._lock:
            # Check if circuit should be opened based on failure rate
            self._check_failure_rate()
            
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self._transition_to_half_open()
                else:
                    raise CircuitOpenError(f"Circuit breaker '{self.name}' is OPEN")
        
        # Execute the function
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure()
            raise
        finally:
            duration = time.time() - start_time
            logger.debug(f"Circuit breaker '{self.name}' call took {duration:.3f}s")
    
    async def _on_success(self):
        """Handle successful call."""
        async with self._lock:
            self.stats.total_calls += 1
            self.stats.successful_calls += 1
            self.stats.consecutive_successes += 1
            self.stats.consecutive_failures = 0
            self.stats.last_success_time = time.time()
            
            if self.state == CircuitState.HALF_OPEN:
                if self.stats.consecutive_successes >= self.config.success_threshold:
                    self._transition_to_closed()
    
    async def _on_failure(self):
        """Handle failed call."""
        async with self._lock:
            self.stats.total_calls += 1
            self.stats.failed_calls += 1
            self.stats.consecutive_failures += 1
            self.stats.consecutive_successes = 0
            self.stats.last_failure_time = time.time()
            self.stats.failure_timestamps.append(time.time())
            
            # Clean old failure timestamps
            cutoff_time = time.time() - self.config.monitoring_window
            self.stats.failure_timestamps = [
                ts for ts in self.stats.failure_timestamps if ts > cutoff_time
            ]
            
            if self.state == CircuitState.HALF_OPEN:
                self._transition_to_open()
            elif self.state == CircuitState.CLOSED:
                if self.stats.consecutive_failures >= self.config.failure_threshold:
                    self._transition_to_open()
    
    def _check_failure_rate(self):
        """Check if failure rate exceeds threshold."""
        if not self.stats.failure_timestamps:
            return
            
        cutoff_time = time.time() - self.config.monitoring_window
        recent_failures = [
            ts for ts in self.stats.failure_timestamps if ts > cutoff_time
        ]
        
        if len(recent_failures) >= self.config.failure_threshold:
            # Calculate failure rate in the monitoring window
            window_start = time.time() - self.config.monitoring_window
            total_in_window = sum(
                1 for ts in self.stats.failure_timestamps 
                if ts > window_start
            )
            
            if total_in_window > 0:
                failure_rate = len(recent_failures) / total_in_window
                if failure_rate >= self.config.failure_rate_threshold:
                    if self.state == CircuitState.CLOSED:
                        self._transition_to_open()
    
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        return (time.time() - self._state_changed_at) >= self.config.recovery_timeout
    
    def _transition_to_closed(self):
        """Transition to CLOSED state."""
        logger.info(f"Circuit breaker '{self.name}' transitioning to CLOSED")
        self.state = CircuitState.CLOSED
        self._state_changed_at = time.time()
        self.stats.consecutive_failures = 0
        self._notify_state_change(CircuitState.CLOSED)
    
    def _transition_to_open(self):
        """Transition to OPEN state."""
        logger.warning(f"Circuit breaker '{self.name}' transitioning to OPEN")
        self.state = CircuitState.OPEN
        self._state_changed_at = time.time()
        self.stats.circuit_opens += 1
        self._notify_state_change(CircuitState.OPEN)
    
    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state."""
        logger.info(f"Circuit breaker '{self.name}' transitioning to HALF_OPEN")
        self.state = CircuitState.HALF_OPEN
        self._state_changed_at = time.time()
        self.stats.consecutive_successes = 0
        self._notify_state_change(CircuitState.HALF_OPEN)
    
    def _notify_state_change(self, new_state: CircuitState):
        """Notify handlers of state change."""
        for handler in self._on_state_change_handlers:
            try:
                handler(self.name, new_state)
            except Exception as e:
                logger.error(f"Error in state change handler: {e}")
    
    def add_state_change_handler(self, handler: Callable):
        """Add a handler to be called on state changes."""
        self._on_state_change_handlers.append(handler)
    
    def get_state(self) -> CircuitState:
        """Get current circuit state."""
        return self.state
    
    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        success_rate = 0.0
        if self.stats.total_calls > 0:
            success_rate = self.stats.successful_calls / self.stats.total_calls
            
        return {
            "name": self.name,
            "state": self.state.value,
            "total_calls": self.stats.total_calls,
            "successful_calls": self.stats.successful_calls,
            "failed_calls": self.stats.failed_calls,
            "success_rate": success_rate,
            "consecutive_failures": self.stats.consecutive_failures,
            "consecutive_successes": self.stats.consecutive_successes,
            "circuit_opens": self.stats.circuit_opens,
            "last_failure": datetime.fromtimestamp(self.stats.last_failure_time).isoformat()
                           if self.stats.last_failure_time else None,
            "last_success": datetime.fromtimestamp(self.stats.last_success_time).isoformat()
                           if self.stats.last_success_time else None,
        }
    
    def reset(self):
        """Manually reset the circuit breaker."""
        logger.info(f"Manually resetting circuit breaker '{self.name}'")
        self.state = CircuitState.CLOSED
        self._state_changed_at = time.time()
        self.stats.consecutive_failures = 0
        self.stats.consecutive_successes = 0

class CircuitOpenError(Exception):
    """Exception raised when circuit is open."""
    pass

class CircuitBreakerManager:
    """
    Manages multiple circuit breakers for different services.
    """
    
    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}
        
    def get_or_create(self, name: str, config: Optional[CircuitBreakerConfig] = None) -> CircuitBreaker:
        """Get existing circuit breaker or create new one."""
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, config)
        return self._breakers[name]
    
    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all circuit breakers."""
        return {
            name: breaker.get_stats() 
            for name, breaker in self._breakers.items()
        }
    
    def reset_all(self):
        """Reset all circuit breakers."""
        for breaker in self._breakers.values():
            breaker.reset()
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get overall health status of all services."""
        total_breakers = len(self._breakers)
        open_breakers = sum(
            1 for b in self._breakers.values() 
            if b.get_state() == CircuitState.OPEN
        )
        half_open_breakers = sum(
            1 for b in self._breakers.values() 
            if b.get_state() == CircuitState.HALF_OPEN
        )
        
        return {
            "total_services": total_breakers,
            "healthy_services": total_breakers - open_breakers,
            "degraded_services": half_open_breakers,
            "failed_services": open_breakers,
            "health_percentage": ((total_breakers - open_breakers) / total_breakers * 100)
                                if total_breakers > 0 else 100
        }

# Global circuit breaker manager instance
circuit_breaker_manager = CircuitBreakerManager()