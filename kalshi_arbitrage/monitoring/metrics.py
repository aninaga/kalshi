"""
Metrics Collector

Collects and exposes metrics for monitoring dashboards.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    """Single metric data point."""
    name: str
    value: float
    tags: Dict[str, str]
    timestamp: float


class MetricsCollector:
    """
    Collects metrics for system monitoring.

    Tracks:
    - Opportunity detection rate
    - Execution success rate
    - Latency metrics
    - Balance metrics
    - PnL metrics
    """

    def __init__(self, retention_seconds: int = 3600):
        self.retention_seconds = retention_seconds

        # Metric storage
        self._metrics: Dict[str, List[MetricPoint]] = defaultdict(list)

        # Counters (don't reset)
        self._counters: Dict[str, float] = defaultdict(float)

        # Gauges (current values)
        self._gauges: Dict[str, float] = {}

        # Histograms (for percentile calculation)
        self._histograms: Dict[str, List[float]] = defaultdict(list)

        logger.info("MetricsCollector initialized")

    def _cleanup_old_metrics(self, name: str):
        """Remove metrics older than retention period."""
        cutoff = time.time() - self.retention_seconds
        self._metrics[name] = [
            m for m in self._metrics[name]
            if m.timestamp > cutoff
        ]

    def record(
        self,
        name: str,
        value: float,
        tags: Optional[Dict[str, str]] = None
    ):
        """Record a metric value."""
        point = MetricPoint(
            name=name,
            value=value,
            tags=tags or {},
            timestamp=time.time(),
        )
        self._metrics[name].append(point)
        self._cleanup_old_metrics(name)

    def increment(self, name: str, value: float = 1.0):
        """Increment a counter."""
        self._counters[name] += value

    def set_gauge(self, name: str, value: float):
        """Set a gauge value."""
        self._gauges[name] = value

    def observe_histogram(self, name: str, value: float):
        """Add observation to histogram."""
        self._histograms[name].append(value)

        # Keep last 1000 observations
        if len(self._histograms[name]) > 1000:
            self._histograms[name] = self._histograms[name][-1000:]

    # Convenience methods for common metrics
    def record_opportunity(self, arb_type: str, profit_pct: float):
        """Record an opportunity detection."""
        self.increment("opportunities_detected")
        self.record("opportunity_profit_pct", profit_pct, {"type": arb_type})
        self.observe_histogram("opportunity_profit_distribution", profit_pct)

    def record_execution(
        self,
        success: bool,
        profit: float,
        latency_ms: int
    ):
        """Record an execution attempt."""
        self.increment("executions_total")
        if success:
            self.increment("executions_success")
            self.increment("total_profit", profit)
        else:
            self.increment("executions_failed")

        self.record("execution_latency_ms", latency_ms)
        self.observe_histogram("execution_latency_distribution", latency_ms)

    def record_price_update(self, platform: str):
        """Record a price update received."""
        self.increment(f"price_updates_{platform}")
        self.increment("price_updates_total")

    def set_balance(self, platform: str, balance: float):
        """Set current balance for a platform."""
        self.set_gauge(f"balance_{platform}", balance)

    def set_connection_status(self, platform: str, connected: bool):
        """Set connection status."""
        self.set_gauge(f"connected_{platform}", 1.0 if connected else 0.0)

    # Retrieval methods
    def get_counter(self, name: str) -> float:
        """Get counter value."""
        return self._counters.get(name, 0.0)

    def get_gauge(self, name: str) -> float:
        """Get gauge value."""
        return self._gauges.get(name, 0.0)

    def get_histogram_percentile(self, name: str, percentile: float) -> float:
        """Get histogram percentile value."""
        values = self._histograms.get(name, [])
        if not values:
            return 0.0

        sorted_values = sorted(values)
        index = int(len(sorted_values) * percentile / 100)
        return sorted_values[min(index, len(sorted_values) - 1)]

    def get_metric_stats(self, name: str) -> Dict:
        """Get statistics for a metric."""
        points = self._metrics.get(name, [])
        if not points:
            return {"count": 0, "avg": 0, "min": 0, "max": 0}

        values = [p.value for p in points]
        return {
            "count": len(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    def get_all_metrics(self) -> Dict:
        """Get all current metrics."""
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {
                name: {
                    "p50": self.get_histogram_percentile(name, 50),
                    "p90": self.get_histogram_percentile(name, 90),
                    "p99": self.get_histogram_percentile(name, 99),
                }
                for name in self._histograms
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_summary(self) -> Dict:
        """Get a summary of key metrics."""
        total_executions = self.get_counter("executions_total")
        success_executions = self.get_counter("executions_success")

        return {
            "opportunities_detected": int(self.get_counter("opportunities_detected")),
            "executions": {
                "total": int(total_executions),
                "success": int(success_executions),
                "failed": int(self.get_counter("executions_failed")),
                "success_rate": (
                    success_executions / total_executions * 100
                    if total_executions > 0 else 0
                ),
            },
            "profit": {
                "total": self.get_counter("total_profit"),
            },
            "price_updates": {
                "total": int(self.get_counter("price_updates_total")),
                "kalshi": int(self.get_counter("price_updates_kalshi")),
                "polymarket": int(self.get_counter("price_updates_polymarket")),
            },
            "balances": {
                "kalshi": self.get_gauge("balance_kalshi"),
                "polymarket": self.get_gauge("balance_polymarket"),
            },
            "connections": {
                "kalshi": bool(self.get_gauge("connected_kalshi")),
                "polymarket": bool(self.get_gauge("connected_polymarket")),
            },
            "latency": {
                "p50_ms": self.get_histogram_percentile("execution_latency_distribution", 50),
                "p90_ms": self.get_histogram_percentile("execution_latency_distribution", 90),
                "p99_ms": self.get_histogram_percentile("execution_latency_distribution", 99),
            },
        }
