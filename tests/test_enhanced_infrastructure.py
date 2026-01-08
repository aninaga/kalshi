"""
Comprehensive test suite for the enhanced arbitrage infrastructure.
Tests all components including circuit breakers, risk engine, and monitoring.
"""

import asyncio
import pytest
import time
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, AsyncMock

# Import our modules
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.circuit_breaker import (
    CircuitBreaker, CircuitBreakerConfig, CircuitState, 
    CircuitOpenError, CircuitBreakerManager
)
from kalshi_arbitrage.data_normalizer import (
    DataNormalizer, NormalizedMarket, NormalizedOrderBook,
    NormalizedOrderLevel, NormalizedPrice, NormalizedOutcome,
    MarketStatus, OutcomeType
)
from kalshi_arbitrage.risk_engine import (
    SlippageModel, FeeCalculator, RiskEngine,
    MarketImpactModel, ExecutionStrategy, NetworkCondition
)
from kalshi_arbitrage.monitoring import (
    MonitoringSystem, AlertType, AlertPriority,
    MetricAggregator, HealthChecker
)

class TestCircuitBreaker:
    """Test circuit breaker functionality."""
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_normal_operation(self):
        """Test circuit breaker in normal operation."""
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker("test", config)
        
        # Successful calls should work
        async def success_func():
            return "success"
        
        result = await breaker.call(success_func)
        assert result == "success"
        assert breaker.get_state() == CircuitState.CLOSED
        
    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_on_failures(self):
        """Test circuit breaker opens after threshold failures."""
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker("test", config)
        
        # Failing function
        async def fail_func():
            raise Exception("Test failure")
        
        # First 3 failures should open the circuit
        for i in range(3):
            with pytest.raises(Exception):
                await breaker.call(fail_func)
        
        assert breaker.get_state() == CircuitState.OPEN
        
        # Next call should fail immediately
        with pytest.raises(CircuitOpenError):
            await breaker.call(fail_func)
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_recovery(self):
        """Test circuit breaker recovery to half-open and closed."""
        config = CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout=1,  # 1 second
            success_threshold=2
        )
        breaker = CircuitBreaker("test", config)
        
        # Open the circuit
        async def fail_func():
            raise Exception("Fail")
        
        for _ in range(2):
            with pytest.raises(Exception):
                await breaker.call(fail_func)
        
        assert breaker.get_state() == CircuitState.OPEN
        
        # Wait for recovery timeout
        await asyncio.sleep(1.1)
        
        # Should transition to half-open on next call
        async def success_func():
            return "success"
        
        # First success in half-open
        result = await breaker.call(success_func)
        assert result == "success"
        assert breaker.get_state() == CircuitState.HALF_OPEN
        
        # Second success should close the circuit
        result = await breaker.call(success_func)
        assert result == "success"
        assert breaker.get_state() == CircuitState.CLOSED

class TestDataNormalizer:
    """Test data normalization functionality."""
    
    def test_normalize_kalshi_market(self):
        """Test Kalshi market normalization."""
        raw_market = {
            'ticker': 'ELECTION-2024',
            'title': 'Will candidate X win?',
            'status': 'active',
            'yes_price': 65,  # 65 cents
            'no_price': 35,   # 35 cents
            'volume': 10000,
            'close_time': '2024-11-05T00:00:00Z'
        }
        
        normalized = DataNormalizer.normalize_kalshi_market(raw_market)
        
        assert normalized.id == 'ELECTION-2024'
        assert normalized.platform == 'kalshi'
        assert normalized.status == MarketStatus.ACTIVE
        assert normalized.outcome_type == OutcomeType.BINARY
        assert len(normalized.outcomes) == 2
        
        # Check price normalization
        yes_outcome = normalized.get_outcome_by_name("YES")
        assert yes_outcome.price.value == Decimal("0.65")
        
    def test_normalize_polymarket_market(self):
        """Test Polymarket market normalization."""
        raw_market = {
            'id': '0x123abc',
            'question': 'Will ETH exceed $3000?',
            'active': True,
            'outcomes': '["Yes", "No"]',
            'clobTokenIds': '["0xabc", "0xdef"]',
            'outcomePrices': '["0.72", "0.28"]',
            'volume': '50000',
            'endDate': '2024-12-31T23:59:59Z'
        }
        
        normalized = DataNormalizer.normalize_polymarket_market(raw_market)
        
        assert normalized.id == '0x123abc'
        assert normalized.platform == 'polymarket'
        assert normalized.status == MarketStatus.ACTIVE
        assert len(normalized.outcomes) == 2
        
        # Check outcomes
        yes_outcome = next(o for o in normalized.outcomes if o.name == "Yes")
        assert yes_outcome.price.value == Decimal("0.72")
        
    def test_normalize_orderbook(self):
        """Test orderbook normalization."""
        kalshi_orderbook = {
            'yes_bids': [
                {'price': 65, 'size': 100},
                {'price': 64, 'size': 200}
            ],
            'yes_asks': [
                {'price': 66, 'size': 150},
                {'price': 67, 'size': 250}
            ]
        }
        
        normalized = DataNormalizer.normalize_orderbook(kalshi_orderbook, 'kalshi')
        
        assert len(normalized.bids) == 2
        assert len(normalized.asks) == 2
        assert normalized.bids[0].price == Decimal("0.65")
        assert normalized.asks[0].price == Decimal("0.66")
        assert normalized.get_spread() == Decimal("0.01")
        assert normalized.get_mid_price() == Decimal("0.655")

class TestRiskEngine:
    """Test risk engine functionality."""
    
    def test_slippage_model_basic(self):
        """Test basic slippage calculation."""
        model = SlippageModel()
        
        # Create test orderbook
        orderbook = NormalizedOrderBook(
            bids=[
                NormalizedOrderLevel(Decimal("0.50"), Decimal("100")),
                NormalizedOrderLevel(Decimal("0.49"), Decimal("200")),
                NormalizedOrderLevel(Decimal("0.48"), Decimal("300"))
            ],
            asks=[
                NormalizedOrderLevel(Decimal("0.51"), Decimal("100")),
                NormalizedOrderLevel(Decimal("0.52"), Decimal("200")),
                NormalizedOrderLevel(Decimal("0.53"), Decimal("300"))
            ],
            timestamp=datetime.now(timezone.utc)
        )
        
        # Test buy slippage for 150 shares
        slippage = model.estimate_slippage(
            orderbook,
            Decimal("150"),
            "buy",
            ExecutionStrategy.ADAPTIVE
        )
        
        # Should consume first level (100) + part of second (50)
        # Average price = (100*0.51 + 50*0.52) / 150 = 0.513333
        # Base slippage = (0.513333 - 0.51) / 0.51 = 0.00654
        assert slippage.base_slippage < Decimal("0.01")
        assert slippage.total_slippage > slippage.base_slippage
        
    def test_slippage_model_large_order(self):
        """Test slippage for order exceeding liquidity."""
        model = SlippageModel()
        
        # Thin orderbook
        orderbook = NormalizedOrderBook(
            bids=[
                NormalizedOrderLevel(Decimal("0.50"), Decimal("10")),
            ],
            asks=[
                NormalizedOrderLevel(Decimal("0.51"), Decimal("10")),
            ],
            timestamp=datetime.now(timezone.utc)
        )
        
        # Large order
        slippage = model.estimate_slippage(
            orderbook,
            Decimal("1000"),
            "buy"
        )
        
        # Should have meaningful slippage due to thin liquidity
        assert slippage.total_slippage >= Decimal("0.01")
        assert slippage.confidence < Decimal("0.5")
        
    @pytest.mark.asyncio
    async def test_fee_calculator(self):
        """Test fee calculation."""
        calculator = FeeCalculator()
        
        # Test Kalshi (no fees)
        kalshi_fees = await calculator.calculate_total_fees(
            "kalshi",
            Decimal("1000"),
            ExecutionStrategy.AGGRESSIVE
        )
        assert kalshi_fees['total_fee'] == Decimal("0")
        
        # Test Polymarket
        poly_fees = await calculator.calculate_total_fees(
            "polymarket",
            Decimal("1000"),
            ExecutionStrategy.AGGRESSIVE,
            NetworkCondition.MEDIUM
        )
        assert poly_fees['trading_fee'] == Decimal("10")  # 1% of 1000
        assert poly_fees['gas_fee'] > Decimal("0")
        assert poly_fees['total_fee'] > poly_fees['trading_fee']
        
    @pytest.mark.asyncio
    async def test_risk_assessment(self):
        """Test comprehensive risk assessment."""
        engine = RiskEngine()
        
        # Create test opportunity
        opportunity = {
            'opportunity_id': 'test_001',
            'max_tradeable_volume': 500,
            'profit_margin': 0.03,  # 3%
            'strategy': 'Buy Kalshi YES → Sell Polymarket',
            'buy_platform': 'kalshi',
            'sell_platform': 'polymarket'
        }
        
        # Create test orderbooks
        kalshi_ob = NormalizedOrderBook(
            bids=[NormalizedOrderLevel(Decimal("0.48"), Decimal("500"))],
            asks=[NormalizedOrderLevel(Decimal("0.50"), Decimal("500"))],
            timestamp=datetime.now(timezone.utc)
        )
        
        poly_ob = NormalizedOrderBook(
            bids=[NormalizedOrderLevel(Decimal("0.53"), Decimal("500"))],
            asks=[NormalizedOrderLevel(Decimal("0.55"), Decimal("500"))],
            timestamp=datetime.now(timezone.utc)
        )
        
        assessment = await engine.assess_opportunity(
            opportunity,
            kalshi_ob,
            poly_ob
        )
        
        assert assessment.opportunity_id == 'test_001'
        assert assessment.total_risk_score > Decimal("0")
        assert assessment.profit_after_risk > Decimal("0")
        assert len(assessment.recommendations) > 0

class TestMonitoring:
    """Test monitoring system functionality."""
    
    def test_metric_aggregator(self):
        """Test metric aggregation."""
        aggregator = MetricAggregator(window_seconds=60)
        
        # Record some metrics
        for i in range(10):
            aggregator.record("api_calls", 1)
            aggregator.record("api_latency", 50 + i * 10)
        
        # Get stats
        call_stats = aggregator.get_stats("api_calls")
        assert call_stats["count"] == 10
        assert call_stats["sum"] == 10
        
        latency_stats = aggregator.get_stats("api_latency")
        assert latency_stats["count"] == 10
        assert latency_stats["avg"] == 95  # (50+60+...+140)/10
        assert latency_stats["min"] == 50
        assert latency_stats["max"] == 140
        
    @pytest.mark.asyncio
    async def test_alert_manager(self):
        """Test alert creation and management."""
        from kalshi_arbitrage.monitoring import AlertManager
        
        manager = AlertManager()
        
        # Create test alert
        alert_id = await manager.create_alert(
            AlertType.OPPORTUNITY,
            AlertPriority.HIGH,
            "Test Opportunity",
            "3% arbitrage found",
            {"profit": 0.03}
        )
        
        assert alert_id != ""
        
        # Get active alerts
        active = manager.get_active_alerts()
        assert len(active) == 1
        assert active[0].title == "Test Opportunity"
        
        # Test rate limiting
        for i in range(15):
            await manager.create_alert(
                AlertType.SYSTEM_ERROR,
                AlertPriority.LOW,
                f"Error {i}",
                "Test error"
            )
        
        # Should be rate limited
        active = manager.get_active_alerts(AlertType.SYSTEM_ERROR)
        assert len(active) <= 10  # Rate limit