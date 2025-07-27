"""
Comprehensive monitoring and alerting system for the arbitrage detection platform.
Tracks performance metrics, system health, and sends alerts for opportunities.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any, Callable, Set
from datetime import datetime, timedelta
from decimal import Decimal
from dataclasses import dataclass, field
from enum import Enum
from collections import deque, defaultdict
import json
import aiohttp

logger = logging.getLogger(__name__)

class AlertPriority(Enum):
    """Alert priority levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class AlertType(Enum):
    """Types of alerts."""
    OPPORTUNITY = "opportunity"
    SYSTEM_ERROR = "system_error"
    PERFORMANCE = "performance"
    CONNECTION = "connection"
    DATA_QUALITY = "data_quality"

@dataclass
class Alert:
    """Represents a system alert."""
    id: str
    type: AlertType
    priority: AlertPriority
    title: str
    message: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "type": self.type.value,
            "priority": self.priority.value,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "acknowledged": self.acknowledged
        }

@dataclass
class MetricPoint:
    """Single metric data point."""
    timestamp: datetime
    value: float
    tags: Dict[str, str] = field(default_factory=dict)

class MetricAggregator:
    """Aggregates metrics over time windows."""
    
    def __init__(self, window_seconds: int = 300):  # 5 minute default
        self.window_seconds = window_seconds
        self.metrics: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
        
    def record(self, metric_name: str, value: float, tags: Optional[Dict[str, str]] = None):
        """Record a metric value."""
        point = MetricPoint(
            timestamp=datetime.now(),
            value=value,
            tags=tags or {}
        )
        self.metrics[metric_name].append(point)
    
    def get_stats(self, metric_name: str, window_seconds: Optional[int] = None) -> Dict[str, float]:
        """Get statistics for a metric over the time window."""
        if metric_name not in self.metrics:
            return {"count": 0, "sum": 0, "avg": 0, "min": 0, "max": 0, "p95": 0}
        
        window = window_seconds or self.window_seconds
        cutoff_time = datetime.now() - timedelta(seconds=window)
        
        # Filter points within window
        points = [p for p in self.metrics[metric_name] if p.timestamp > cutoff_time]
        
        if not points:
            return {"count": 0, "sum": 0, "avg": 0, "min": 0, "max": 0, "p95": 0}
        
        values = [p.value for p in points]
        values.sort()
        
        return {
            "count": len(values),
            "sum": sum(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "p95": values[int(len(values) * 0.95)] if values else 0,
            "latest": values[-1] if values else 0
        }
    
    def get_rate(self, metric_name: str, window_seconds: Optional[int] = None) -> float:
        """Get rate per second for a counter metric."""
        stats = self.get_stats(metric_name, window_seconds)
        window = window_seconds or self.window_seconds
        return stats["count"] / window if window > 0 else 0

class HealthChecker:
    """Monitors system health across components."""
    
    def __init__(self):
        self.component_status: Dict[str, Dict[str, Any]] = {}
        self.health_checks: Dict[str, Callable] = {}
        self.check_interval = 30  # seconds
        self._check_task = None
        
    def register_component(self, name: str, health_check: Callable):
        """Register a component with its health check function."""
        self.health_checks[name] = health_check
        self.component_status[name] = {
            "status": "unknown",
            "last_check": None,
            "details": {}
        }
    
    async def start(self):
        """Start health monitoring."""
        if self._check_task is None:
            self._check_task = asyncio.create_task(self._health_check_loop())
    
    async def stop(self):
        """Stop health monitoring."""
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
    
    async def _health_check_loop(self):
        """Periodically run health checks."""
        while True:
            try:
                await self.check_all_components()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check loop error: {e}")
                await asyncio.sleep(self.check_interval)
    
    async def check_all_components(self):
        """Run health checks for all components."""
        tasks = []
        for name, check_func in self.health_checks.items():
            task = asyncio.create_task(self._check_component(name, check_func))
            tasks.append(task)
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _check_component(self, name: str, check_func: Callable):
        """Check health of a single component."""
        try:
            start_time = time.time()
            result = await check_func()
            duration = time.time() - start_time
            
            self.component_status[name] = {
                "status": "healthy" if result.get("healthy", False) else "unhealthy",
                "last_check": datetime.now().isoformat(),
                "response_time": duration,
                "details": result
            }
        except Exception as e:
            logger.error(f"Health check failed for {name}: {e}")
            self.component_status[name] = {
                "status": "error",
                "last_check": datetime.now().isoformat(),
                "error": str(e)
            }
    
    def get_overall_health(self) -> Dict[str, Any]:
        """Get overall system health status."""
        total_components = len(self.component_status)
        healthy_components = sum(
            1 for status in self.component_status.values()
            if status.get("status") == "healthy"
        )
        
        overall_status = "healthy"
        if healthy_components < total_components:
            overall_status = "degraded"
        if healthy_components < total_components / 2:
            overall_status = "unhealthy"
        
        return {
            "status": overall_status,
            "healthy_components": healthy_components,
            "total_components": total_components,
            "components": self.component_status,
            "timestamp": datetime.now().isoformat()
        }

class AlertManager:
    """Manages alerts and notifications."""
    
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url
        self.alerts: Dict[str, Alert] = {}
        self.alert_history = deque(maxlen=1000)
        self.alert_handlers: Dict[AlertType, List[Callable]] = defaultdict(list)
        self.rate_limits: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        
        # Alert thresholds
        self.opportunity_threshold = Decimal("0.03")  # 3% profit minimum
        self.high_value_threshold = Decimal("1000")   # $1000+ opportunities
        
    def register_handler(self, alert_type: AlertType, handler: Callable):
        """Register a handler for specific alert types."""
        self.alert_handlers[alert_type].append(handler)
    
    async def create_alert(self, 
                          alert_type: AlertType,
                          priority: AlertPriority,
                          title: str,
                          message: str,
                          metadata: Optional[Dict[str, Any]] = None) -> str:
        """Create and dispatch an alert."""
        # Check rate limits
        if not self._check_rate_limit(alert_type):
            logger.warning(f"Rate limit exceeded for {alert_type}")
            return ""
        
        # Create alert
        alert_id = f"{alert_type.value}_{int(time.time() * 1000)}"
        alert = Alert(
            id=alert_id,
            type=alert_type,
            priority=priority,
            title=title,
            message=message,
            timestamp=datetime.now(),
            metadata=metadata or {}
        )
        
        # Store alert
        self.alerts[alert_id] = alert
        self.alert_history.append(alert)
        
        # Dispatch to handlers
        await self._dispatch_alert(alert)
        
        # Send webhook if configured
        if self.webhook_url and priority in [AlertPriority.HIGH, AlertPriority.CRITICAL]:
            await self._send_webhook(alert)
        
        return alert_id
    
    async def create_opportunity_alert(self, opportunity: Dict[str, Any]) -> Optional[str]:
        """Create alert for arbitrage opportunity."""
        profit_margin = Decimal(str(opportunity.get("profit_margin", 0)))
        total_profit = Decimal(str(opportunity.get("total_profit", 0)))
        
        # Determine priority based on profit
        if profit_margin >= Decimal("0.05") or total_profit >= self.high_value_threshold:
            priority = AlertPriority.HIGH
        elif profit_margin >= self.opportunity_threshold:
            priority = AlertPriority.MEDIUM
        else:
            return None  # Don't alert for small opportunities
        
        # Format message
        strategy = opportunity.get("strategy", "Unknown")
        kalshi_market = opportunity.get("match_data", {}).get("kalshi_market", {}).get("title", "Unknown")
        polymarket_market = opportunity.get("match_data", {}).get("polymarket_market", {}).get("title", "Unknown")
        
        title = f"💰 Arbitrage Opportunity: {profit_margin:.2%} profit"
        message = (
            f"Strategy: {strategy}\n"
            f"Profit Margin: {profit_margin:.2%}\n"
            f"Max Profit: ${total_profit:.2f}\n"
            f"Kalshi: {kalshi_market}\n"
            f"Polymarket: {polymarket_market}"
        )
        
        return await self.create_alert(
            AlertType.OPPORTUNITY,
            priority,
            title,
            message,
            metadata=opportunity
        )
    
    def _check_rate_limit(self, alert_type: AlertType, window_seconds: int = 60, max_alerts: int = 10) -> bool:
        """Check if alert is within rate limits."""
        now = time.time()
        cutoff = now - window_seconds
        
        # Clean old entries
        key = alert_type.value
        while self.rate_limits[key] and self.rate_limits[key][0] < cutoff:
            self.rate_limits[key].popleft()
        
        # Check limit
        if len(self.rate_limits[key]) >= max_alerts:
            return False
        
        # Record new alert
        self.rate_limits[key].append(now)
        return True
    
    async def _dispatch_alert(self, alert: Alert):
        """Dispatch alert to registered handlers."""
        handlers = self.alert_handlers.get(alert.type, [])
        
        for handler in handlers:
            try:
                await handler(alert)
            except Exception as e:
                logger.error(f"Alert handler error: {e}")
    
    async def _send_webhook(self, alert: Alert):
        """Send alert to webhook URL."""
        if not self.webhook_url:
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "text": f"{alert.title}\n{alert.message}",
                    "alert": alert.to_dict()
                }
                
                async with session.post(self.webhook_url, json=payload, timeout=5) as response:
                    if response.status != 200:
                        logger.error(f"Webhook failed with status {response.status}")
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
    
    def acknowledge_alert(self, alert_id: str) -> bool:
        """Acknowledge an alert."""
        if alert_id in self.alerts:
            self.alerts[alert_id].acknowledged = True
            return True
        return False
    
    def get_active_alerts(self, alert_type: Optional[AlertType] = None) -> List[Alert]:
        """Get active (unacknowledged) alerts."""
        alerts = []
        for alert in self.alerts.values():
            if not alert.acknowledged:
                if alert_type is None or alert.type == alert_type:
                    alerts.append(alert)
        
        # Sort by priority and timestamp
        priority_order = {
            AlertPriority.CRITICAL: 0,
            AlertPriority.HIGH: 1,
            AlertPriority.MEDIUM: 2,
            AlertPriority.LOW: 3
        }
        
        alerts.sort(key=lambda a: (priority_order[a.priority], a.timestamp))
        return alerts

class MonitoringSystem:
    """Main monitoring system coordinating all monitoring components."""
    
    def __init__(self, webhook_url: Optional[str] = None):
        self.metrics = MetricAggregator()
        self.health_checker = HealthChecker()
        self.alert_manager = AlertManager(webhook_url)
        
        # Performance tracking
        self.performance_history = deque(maxlen=1000)
        self.opportunity_tracking = defaultdict(list)
        
        # Monitoring configuration
        self.config = {
            "latency_threshold_ms": 100,
            "error_rate_threshold": 0.05,
            "min_success_rate": 0.95,
            "stale_data_threshold_seconds": 30
        }
        
        # Start background monitoring
        self._monitoring_task = None
        
    async def start(self):
        """Start monitoring system."""
        await self.health_checker.start()
        
        if self._monitoring_task is None:
            self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        
        logger.info("Monitoring system started")
    
    async def stop(self):
        """Stop monitoring system."""
        await self.health_checker.stop()
        
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Monitoring system stopped")
    
    async def _monitoring_loop(self):
        """Main monitoring loop checking system metrics."""
        while True:
            try:
                await self._check_system_metrics()
                await asyncio.sleep(30)  # Check every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(30)
    
    async def _check_system_metrics(self):
        """Check system metrics and create alerts if needed."""
        # Check API latency
        api_stats = self.metrics.get_stats("api_latency_ms")
        if api_stats["p95"] > self.config["latency_threshold_ms"]:
            await self.alert_manager.create_alert(
                AlertType.PERFORMANCE,
                AlertPriority.MEDIUM,
                "High API Latency",
                f"95th percentile latency: {api_stats['p95']:.0f}ms",
                metadata=api_stats
            )
        
        # Check error rates
        error_rate = self.metrics.get_rate("api_errors")
        total_rate = self.metrics.get_rate("api_calls")
        
        if total_rate > 0 and error_rate / total_rate > self.config["error_rate_threshold"]:
            await self.alert_manager.create_alert(
                AlertType.SYSTEM_ERROR,
                AlertPriority.HIGH,
                "High Error Rate",
                f"Error rate: {(error_rate/total_rate)*100:.1f}%",
                metadata={"error_rate": error_rate, "total_rate": total_rate}
            )
        
        # Check connection health
        health_status = self.health_checker.get_overall_health()
        if health_status["status"] == "unhealthy":
            await self.alert_manager.create_alert(
                AlertType.CONNECTION,
                AlertPriority.CRITICAL,
                "System Unhealthy",
                f"Only {health_status['healthy_components']}/{health_status['total_components']} components healthy",
                metadata=health_status
            )
    
    # Metric recording methods
    def record_api_call(self, platform: str, duration_ms: float, success: bool):
        """Record API call metrics."""
        self.metrics.record("api_calls", 1, {"platform": platform})
        self.metrics.record("api_latency_ms", duration_ms, {"platform": platform})
        
        if not success:
            self.metrics.record("api_errors", 1, {"platform": platform})
    
    def record_websocket_message(self, platform: str, channel: str):
        """Record WebSocket message receipt."""
        self.metrics.record("websocket_messages", 1, {"platform": platform, "channel": channel})
    
    def record_opportunity(self, opportunity: Dict[str, Any]):
        """Record arbitrage opportunity found."""
        profit_margin = opportunity.get("profit_margin", 0)
        self.metrics.record("opportunities_found", 1)
        self.metrics.record("opportunity_profit_margin", profit_margin * 100)
        
        # Track by market pair
        key = f"{opportunity.get('kalshi_market_id')}_{opportunity.get('polymarket_market_id')}"
        self.opportunity_tracking[key].append({
            "timestamp": datetime.now(),
            "profit_margin": profit_margin
        })
    
    def record_cache_access(self, hit: bool):
        """Record cache hit/miss."""
        if hit:
            self.metrics.record("cache_hits", 1)
        else:
            self.metrics.record("cache_misses", 1)
    
    # Dashboard data methods
    def get_dashboard_data(self) -> Dict[str, Any]:
        """Get comprehensive dashboard data."""
        # System health
        health = self.health_checker.get_overall_health()
        
        # Performance metrics
        api_stats = self.metrics.get_stats("api_latency_ms")
        cache_hits = self.metrics.get_stats("cache_hits")
        cache_misses = self.metrics.get_stats("cache_misses")
        cache_hit_rate = cache_hits["count"] / (cache_hits["count"] + cache_misses["count"]) if (cache_hits["count"] + cache_misses["count"]) > 0 else 0
        
        # Opportunity metrics
        opp_stats = self.metrics.get_stats("opportunities_found", window_seconds=3600)  # Last hour
        profit_stats = self.metrics.get_stats("opportunity_profit_margin", window_seconds=3600)
        
        # Active alerts
        active_alerts = self.alert_manager.get_active_alerts()
        
        return {
            "timestamp": datetime.now().isoformat(),
            "health": health,
            "performance": {
                "api_latency_p95_ms": api_stats["p95"],
                "api_calls_per_minute": self.metrics.get_rate("api_calls") * 60,
                "error_rate": self._calculate_error_rate(),
                "cache_hit_rate": cache_hit_rate,
                "websocket_messages_per_minute": self.metrics.get_rate("websocket_messages") * 60
            },
            "opportunities": {
                "found_last_hour": opp_stats["count"],
                "avg_profit_margin": profit_stats["avg"] / 100 if profit_stats["avg"] else 0,
                "max_profit_margin": profit_stats["max"] / 100 if profit_stats["max"] else 0,
                "active_opportunities": len([a for a in active_alerts if a.type == AlertType.OPPORTUNITY])
            },
            "alerts": {
                "total_active": len(active_alerts),
                "by_priority": self._count_alerts_by_priority(active_alerts),
                "recent": [a.to_dict() for a in active_alerts[:5]]
            }
        }
    
    def _calculate_error_rate(self) -> float:
        """Calculate current error rate."""
        errors = self.metrics.get_stats("api_errors", window_seconds=300)
        calls = self.metrics.get_stats("api_calls", window_seconds=300)
        
        if calls["count"] > 0:
            return errors["count"] / calls["count"]
        return 0.0
    
    def _count_alerts_by_priority(self, alerts: List[Alert]) -> Dict[str, int]:
        """Count alerts by priority."""
        counts = defaultdict(int)
        for alert in alerts:
            counts[alert.priority.value] += 1
        return dict(counts)

# Global monitoring instance
monitoring_system = None

async def get_monitoring_system(webhook_url: Optional[str] = None) -> MonitoringSystem:
    """Get or create monitoring system instance."""
    global monitoring_system
    
    if monitoring_system is None:
        monitoring_system = MonitoringSystem(webhook_url)
        await monitoring_system.start()
    
    return monitoring_system