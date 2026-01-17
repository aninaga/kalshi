"""
Sophisticated risk engine with slippage modeling, market impact analysis,
and dynamic fee calculation for accurate profit estimation.
"""

import asyncio
import logging
import math
from typing import Dict, List, Optional, Any, Tuple, Union
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
from collections import deque

from .data_normalizer import (
    NormalizedMarket, NormalizedOrderBook, NormalizedOrderLevel,
    NormalizedPrice, DataNormalizer
)

logger = logging.getLogger(__name__)

class ExecutionStrategy(Enum):
    """Types of execution strategies."""
    AGGRESSIVE = "aggressive"    # Take liquidity, cross spread
    PASSIVE = "passive"          # Provide liquidity, limit orders
    ADAPTIVE = "adaptive"        # Mix based on conditions

class NetworkCondition(Enum):
    """Network congestion levels."""
    LOW = "low"          # Normal conditions
    MEDIUM = "medium"    # Moderate congestion
    HIGH = "high"        # High congestion
    CRITICAL = "critical" # Extreme congestion

@dataclass
class MarketImpactModel:
    """Model for estimating market impact of trades."""
    
    # Base impact parameters
    linear_impact: Decimal = Decimal("0.0001")    # 1 bps per 1% of volume
    square_root_impact: Decimal = Decimal("0.001") # Square root model coefficient
    temporary_impact_decay: Decimal = Decimal("0.5") # Decay rate for temp impact
    
    # Market-specific adjustments
    liquidity_factor: Decimal = Decimal("1.0")
    volatility_factor: Decimal = Decimal("1.0")
    
    def calculate_impact(self, trade_size: Decimal, market_depth: Decimal,
                        volatility: Optional[Decimal] = None) -> Decimal:
        """
        Calculate expected market impact using hybrid model.
        
        Impact = (linear * trade_size/depth + sqrt_coeff * sqrt(trade_size/depth)) * factors
        """
        if market_depth <= 0:
            return Decimal("0.05")  # 5% max impact for zero depth
        
        size_ratio = trade_size / market_depth
        
        # Linear component
        linear_component = self.linear_impact * size_ratio
        
        # Square root component (Almgren-Chriss model)
        sqrt_component = self.square_root_impact * Decimal(math.sqrt(float(size_ratio)))
        
        # Total permanent impact
        permanent_impact = linear_component + sqrt_component
        
        # Apply market factors
        if volatility:
            self.volatility_factor = Decimal("1") + volatility
        
        total_impact = permanent_impact * self.liquidity_factor * self.volatility_factor
        
        # Cap at reasonable maximum
        return min(total_impact, Decimal("0.05"))  # 5% max

@dataclass
class SlippageEstimate:
    """Detailed slippage estimation result."""
    base_slippage: Decimal
    market_impact: Decimal
    timing_risk: Decimal
    total_slippage: Decimal
    confidence: Decimal
    breakdown: Dict[str, Any] = field(default_factory=dict)

class SlippageModel:
    """
    Advanced slippage model incorporating multiple factors:
    - Order book depth and shape
    - Historical volatility
    - Time of day patterns
    - Market microstructure
    """
    
    def __init__(self):
        self.impact_model = MarketImpactModel()
        self.volatility_window = 300  # 5 minutes for volatility calc
        self.price_history = {}  # market_id -> deque of (timestamp, price)
        
        # Microstructure parameters
        self.min_tick_size = Decimal("0.01")  # 1 cent minimum
        self.typical_spread = Decimal("0.02")  # 2 cent typical spread
        
    def estimate_slippage(self, 
                         orderbook: NormalizedOrderBook,
                         trade_size: Decimal,
                         side: str,  # "buy" or "sell"
                         execution_strategy: ExecutionStrategy = ExecutionStrategy.ADAPTIVE,
                         market_volatility: Optional[Decimal] = None) -> SlippageEstimate:
        """
        Estimate slippage for a given trade size and orderbook.
        
        Args:
            orderbook: Current order book state
            trade_size: Size of the trade
            side: Buy or sell side
            execution_strategy: How aggressively to execute
            market_volatility: Optional volatility estimate
            
        Returns:
            Detailed slippage estimate
        """
        # Get relevant order book side
        if side == "buy":
            levels = orderbook.asks  # Buying from asks
        else:
            levels = orderbook.bids  # Selling to bids
            
        if not levels:
            # No liquidity - maximum slippage
            return SlippageEstimate(
                base_slippage=Decimal("0.05"),
                market_impact=Decimal("0.05"),
                timing_risk=Decimal("0.01"),
                total_slippage=Decimal("0.10"),
                confidence=Decimal("0.1")
            )
        
        # Calculate base slippage from order book walk
        base_slippage, depth_consumed = self._walk_orderbook(levels, trade_size)
        
        # Calculate market depth for impact model
        total_depth = sum(level.size for level in levels[:10])  # Top 10 levels
        
        # Estimate market impact
        market_impact = self.impact_model.calculate_impact(
            trade_size, 
            total_depth,
            market_volatility
        )
        
        # Estimate timing risk (opportunity cost during execution)
        timing_risk = self._estimate_timing_risk(
            trade_size,
            total_depth,
            market_volatility,
            execution_strategy
        )
        
        # Adjust for execution strategy
        strategy_multiplier = self._get_strategy_multiplier(execution_strategy)
        
        # Total slippage
        total_slippage = (base_slippage + market_impact + timing_risk) * strategy_multiplier
        
        # Estimate confidence based on orderbook quality
        confidence = self._estimate_confidence(orderbook, trade_size, depth_consumed)
        
        return SlippageEstimate(
            base_slippage=base_slippage,
            market_impact=market_impact,
            timing_risk=timing_risk,
            total_slippage=total_slippage,
            confidence=confidence,
            breakdown={
                "depth_consumed": float(depth_consumed),
                "levels_touched": len([l for l in levels if depth_consumed > 0]),
                "strategy_multiplier": float(strategy_multiplier),
                "total_depth": float(total_depth)
            }
        )
    
    def _walk_orderbook(self, levels: List[NormalizedOrderLevel], 
                       trade_size: Decimal) -> Tuple[Decimal, Decimal]:
        """Walk through orderbook levels to calculate average execution price."""
        if not levels:
            return Decimal("0.05"), Decimal("0")
        
        remaining_size = trade_size
        volume_weighted_price = Decimal("0")
        total_cost = Decimal("0")
        depth_consumed = Decimal("0")
        
        best_price = levels[0].price
        
        for level in levels:
            if remaining_size <= 0:
                break
                
            # How much can we fill at this level
            fill_size = min(remaining_size, level.size)
            
            # Add to weighted average
            total_cost += fill_size * level.price
            depth_consumed += fill_size
            remaining_size -= fill_size
        
        if depth_consumed > 0:
            avg_price = total_cost / depth_consumed
            slippage = abs(avg_price - best_price) / best_price
        else:
            slippage = Decimal("0.05")  # Max slippage if no depth
        
        # If we couldn't fill the full size, treat as maximum slippage
        if depth_consumed < trade_size:
            slippage = max(slippage, Decimal("0.05"))
            
        return slippage, depth_consumed
    
    def _estimate_timing_risk(self, trade_size: Decimal, market_depth: Decimal,
                             volatility: Optional[Decimal], 
                             strategy: ExecutionStrategy) -> Decimal:
        """Estimate risk from adverse price movement during execution."""
        # Estimate execution time based on size and strategy
        if strategy == ExecutionStrategy.AGGRESSIVE:
            execution_time = Decimal("5")  # 5 seconds for aggressive
        elif strategy == ExecutionStrategy.PASSIVE:
            execution_time = Decimal("30")  # 30 seconds for passive
        else:
            execution_time = Decimal("15")  # 15 seconds for adaptive
            
        # Adjust for size relative to depth
        if market_depth > 0:
            size_factor = min(trade_size / market_depth, Decimal("1"))
            execution_time *= (Decimal("1") + size_factor)
        
        # Use volatility to estimate potential adverse movement
        if volatility is None:
            volatility = Decimal("0.001")  # 0.1% per minute default
            
        # Convert to per-second volatility
        per_second_vol = volatility / Decimal("60")
        
        # Expected adverse movement (assuming 50% chance of adverse direction)
        timing_risk = per_second_vol * Decimal(math.sqrt(float(execution_time))) * Decimal("0.5")
        
        return timing_risk
    
    def _get_strategy_multiplier(self, strategy: ExecutionStrategy) -> Decimal:
        """Get slippage multiplier based on execution strategy."""
        if strategy == ExecutionStrategy.AGGRESSIVE:
            return Decimal("1.2")  # 20% higher slippage for aggressive
        elif strategy == ExecutionStrategy.PASSIVE:
            return Decimal("0.8")  # 20% lower slippage for passive
        else:
            return Decimal("1.0")  # Baseline for adaptive
    
    def _estimate_confidence(self, orderbook: NormalizedOrderBook,
                           trade_size: Decimal, depth_consumed: Decimal) -> Decimal:
        """Estimate confidence in slippage estimate based on orderbook quality."""
        confidence = Decimal("1.0")
        
        # Reduce confidence if orderbook is thin
        if len(orderbook.bids) < 5 or len(orderbook.asks) < 5:
            confidence *= Decimal("0.8")
        
        # Reduce confidence if we consume too much depth
        if depth_consumed > 0 and trade_size > 0:
            fill_ratio = depth_consumed / trade_size
            if fill_ratio < Decimal("0.5"):  # Couldn't fill half the order
                confidence *= Decimal("0.5")
        
        # Reduce confidence for old data
        if orderbook.timestamp.tzinfo is None:
            now = datetime.now()
        else:
            now = datetime.now(orderbook.timestamp.tzinfo)
        age_seconds = (now - orderbook.timestamp).total_seconds()
        if age_seconds > 10:
            confidence *= Decimal("0.9")
        if age_seconds > 30:
            confidence *= Decimal("0.7")
            
        return max(confidence, Decimal("0.1"))
    
    def update_price_history(self, market_id: str, price: Decimal, timestamp: datetime):
        """Update price history for volatility calculation."""
        if market_id not in self.price_history:
            self.price_history[market_id] = deque(maxlen=1000)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        self.price_history[market_id].append((timestamp, price))
    
    def calculate_volatility(self, market_id: str) -> Decimal:
        """Calculate recent price volatility."""
        if market_id not in self.price_history or len(self.price_history[market_id]) < 2:
            return Decimal("0.001")  # Default 0.1% volatility
        
        prices = self.price_history[market_id]
        current_time = datetime.now(timezone.utc)
        cutoff_time = current_time - timedelta(seconds=self.volatility_window)
        
        # Filter recent prices
        recent_prices = [(t, p) for t, p in prices if t > cutoff_time]
        
        if len(recent_prices) < 2:
            return Decimal("0.001")
        
        # Calculate returns
        returns = []
        for i in range(1, len(recent_prices)):
            prev_price = recent_prices[i-1][1]
            curr_price = recent_prices[i][1]
            if prev_price > 0:
                ret = (curr_price - prev_price) / prev_price
                returns.append(float(ret))
        
        if not returns:
            return Decimal("0.001")
        
        # Calculate standard deviation of returns
        std_dev = Decimal(str(np.std(returns))) if len(returns) > 1 else Decimal("0.001")
        
        # Annualize (assuming crypto markets trade 24/7)
        annualization_factor = Decimal(str(math.sqrt(365 * 24 * 60 * 60 / self.volatility_window)))
        
        return std_dev * annualization_factor

class FeeCalculator:
    """
    Dynamic fee calculation based on network conditions and platform specifics.
    """
    
    def __init__(self):
        # Base fee structures
        self.kalshi_fees = {
            'maker': Decimal("0"),
            'taker': Decimal("0"),
            'withdrawal': Decimal("0")
        }
        
        self.polymarket_fees = {
            'maker': Decimal("0.01"),    # 1% maker fee
            'taker': Decimal("0.01"),    # 1% taker fee  
            'gas_base': Decimal("0.005"), # 0.5% base gas fee
            'withdrawal': Decimal("0.002") # 0.2% withdrawal
        }
        
        # Network condition monitoring
        self.gas_price_history = deque(maxlen=100)
        self.last_gas_update = None
        
    async def calculate_total_fees(self, 
                                  platform: str,
                                  trade_size: Decimal,
                                  execution_strategy: ExecutionStrategy,
                                  network_condition: Optional[NetworkCondition] = None) -> Dict[str, Decimal]:
        """
        Calculate total fees including dynamic gas costs.
        
        Returns:
            Dictionary with fee breakdown
        """
        if platform == "kalshi":
            return await self._calculate_kalshi_fees(trade_size, execution_strategy)
        elif platform == "polymarket":
            return await self._calculate_polymarket_fees(
                trade_size, 
                execution_strategy, 
                network_condition
            )
        else:
            raise ValueError(f"Unknown platform: {platform}")
    
    async def _calculate_kalshi_fees(self, trade_size: Decimal, 
                                   execution_strategy: ExecutionStrategy) -> Dict[str, Decimal]:
        """Calculate Kalshi fees (currently zero)."""
        return {
            'trading_fee': Decimal("0"),
            'total_fee': Decimal("0"),
            'fee_percentage': Decimal("0")
        }
    
    async def _calculate_polymarket_fees(self, 
                                       trade_size: Decimal,
                                       execution_strategy: ExecutionStrategy,
                                       network_condition: Optional[NetworkCondition]) -> Dict[str, Decimal]:
        """Calculate Polymarket fees including dynamic gas costs."""
        # Determine if maker or taker based on strategy
        if execution_strategy == ExecutionStrategy.PASSIVE:
            trading_fee_rate = self.polymarket_fees['maker']
        else:
            trading_fee_rate = self.polymarket_fees['taker']
        
        trading_fee = trade_size * trading_fee_rate
        
        # Calculate dynamic gas fee
        gas_fee = await self._calculate_gas_fee(trade_size, network_condition)
        
        total_fee = trading_fee + gas_fee
        fee_percentage = total_fee / trade_size if trade_size > 0 else Decimal("0")
        
        return {
            'trading_fee': trading_fee,
            'gas_fee': gas_fee,
            'total_fee': total_fee,
            'fee_percentage': fee_percentage,
            'network_condition': network_condition.value if network_condition else "unknown"
        }
    
    async def _calculate_gas_fee(self, trade_size: Decimal,
                               network_condition: Optional[NetworkCondition]) -> Decimal:
        """Calculate dynamic gas fee based on network conditions."""
        base_gas = self.polymarket_fees['gas_base']
        
        # Adjust for network conditions
        if network_condition == NetworkCondition.LOW:
            multiplier = Decimal("0.5")  # 50% of base
        elif network_condition == NetworkCondition.MEDIUM:
            multiplier = Decimal("1.0")  # 100% of base
        elif network_condition == NetworkCondition.HIGH:
            multiplier = Decimal("2.0")  # 200% of base
        elif network_condition == NetworkCondition.CRITICAL:
            multiplier = Decimal("5.0")  # 500% of base
        else:
            multiplier = Decimal("1.0")  # Default to base
        
        # Gas fee is proportional to trade size but with diminishing impact
        size_factor = Decimal("1") + (trade_size / Decimal("10000")).ln() / Decimal("10")
        size_factor = max(size_factor, Decimal("1"))
        
        gas_fee = base_gas * multiplier * size_factor * trade_size
        
        return gas_fee
    
    async def estimate_network_condition(self) -> NetworkCondition:
        """Estimate current network conditions based on recent activity."""
        # In production, this would query actual network metrics
        # For now, return a simulated condition
        
        # Simulate time-based patterns (higher congestion during peak hours)
        hour = datetime.now().hour
        
        if 14 <= hour <= 22:  # 2 PM - 10 PM UTC (peak US hours)
            return NetworkCondition.MEDIUM
        elif 8 <= hour <= 14:  # 8 AM - 2 PM UTC
            return NetworkCondition.LOW
        else:
            return NetworkCondition.LOW

@dataclass
class RiskAssessment:
    """Comprehensive risk assessment for an arbitrage opportunity."""
    opportunity_id: str
    total_risk_score: Decimal  # 0-100, lower is better
    execution_risk: Decimal
    market_risk: Decimal
    platform_risk: Decimal
    profit_after_risk: Decimal
    confidence: Decimal
    recommendations: List[str]
    risk_breakdown: Dict[str, Any]

class RiskEngine:
    """
    Main risk engine combining all risk models for comprehensive analysis.
    """
    
    def __init__(self):
        self.slippage_model = SlippageModel()
        self.fee_calculator = FeeCalculator()
        self.risk_history = deque(maxlen=1000)
        
        # Risk thresholds
        self.max_position_size = Decimal("10000")  # $10k max per position
        self.max_risk_score = Decimal("50")  # Maximum acceptable risk score
        
    async def assess_opportunity(self,
                               opportunity: Dict[str, Any],
                               kalshi_orderbook: Optional[NormalizedOrderBook],
                               polymarket_orderbook: Optional[NormalizedOrderBook]) -> RiskAssessment:
        """
        Perform comprehensive risk assessment of an arbitrage opportunity.
        """
        opportunity_id = opportunity.get('opportunity_id', 'unknown')
        
        # Extract trade details
        trade_size = Decimal(str(opportunity.get('max_tradeable_volume', 100)))
        profit_margin = Decimal(str(opportunity.get('profit_margin', 0)))
        strategy = opportunity.get('strategy', '')
        
        # Determine buy/sell platforms
        buy_platform = opportunity.get('buy_platform')
        sell_platform = opportunity.get('sell_platform')
        
        # Get orderbooks for each side
        buy_orderbook = kalshi_orderbook if buy_platform == 'kalshi' else polymarket_orderbook
        sell_orderbook = polymarket_orderbook if sell_platform == 'polymarket' else kalshi_orderbook
        
        # Calculate execution risk (slippage on both sides)
        execution_risk = await self._calculate_execution_risk(
            buy_orderbook, 
            sell_orderbook, 
            trade_size
        )
        
        # Calculate market risk (adverse price movement)
        market_risk = await self._calculate_market_risk(opportunity)
        
        # Calculate platform risk (operational risks)
        platform_risk = self._calculate_platform_risk(buy_platform, sell_platform)
        
        # Total risk score (weighted average)
        total_risk_score = (
            execution_risk * Decimal("0.5") +
            market_risk * Decimal("0.3") + 
            platform_risk * Decimal("0.2")
        )
        
        # Calculate risk-adjusted profit
        risk_adjustment = Decimal("1") - (total_risk_score / Decimal("100"))
        profit_after_risk = profit_margin * risk_adjustment
        
        # Generate recommendations
        recommendations = self._generate_recommendations(
            total_risk_score,
            execution_risk,
            market_risk,
            platform_risk,
            trade_size
        )
        
        # Confidence based on data quality
        confidence = self._calculate_confidence(buy_orderbook, sell_orderbook)
        
        return RiskAssessment(
            opportunity_id=opportunity_id,
            total_risk_score=total_risk_score,
            execution_risk=execution_risk,
            market_risk=market_risk,
            platform_risk=platform_risk,
            profit_after_risk=profit_after_risk,
            confidence=confidence,
            recommendations=recommendations,
            risk_breakdown={
                'execution_risk_details': {
                    'buy_slippage': float(execution_risk * Decimal("0.01")),
                    'sell_slippage': float(execution_risk * Decimal("0.01"))
                },
                'market_risk_details': {
                    'volatility': float(market_risk * Decimal("0.001")),
                    'correlation': 0.95  # High correlation between platforms
                },
                'platform_risk_details': {
                    'settlement_delay': '1-3 days',
                    'counterparty_risk': 'low'
                }
            }
        )
    
    async def _calculate_execution_risk(self, 
                                      buy_orderbook: Optional[NormalizedOrderBook],
                                      sell_orderbook: Optional[NormalizedOrderBook],
                                      trade_size: Decimal) -> Decimal:
        """Calculate execution risk from slippage on both sides."""
        risk_score = Decimal("0")
        
        # Buy side slippage
        if buy_orderbook:
            buy_slippage = self.slippage_model.estimate_slippage(
                buy_orderbook,
                trade_size,
                "buy",
                ExecutionStrategy.ADAPTIVE
            )
            risk_score += buy_slippage.total_slippage * Decimal("100")
        else:
            risk_score += Decimal("25")  # High risk if no orderbook
        
        # Sell side slippage
        if sell_orderbook:
            sell_slippage = self.slippage_model.estimate_slippage(
                sell_orderbook,
                trade_size,
                "sell",
                ExecutionStrategy.ADAPTIVE
            )
            risk_score += sell_slippage.total_slippage * Decimal("100")
        else:
            risk_score += Decimal("25")  # High risk if no orderbook
        
        return min(risk_score, Decimal("50"))  # Cap at 50
    
    async def _calculate_market_risk(self, opportunity: Dict[str, Any]) -> Decimal:
        """Calculate market risk from volatility and correlation."""
        # Base market risk
        risk_score = Decimal("10")
        
        # Adjust for profit margin (lower margins = higher risk)
        profit_margin = Decimal(str(opportunity.get('profit_margin', 0)))
        if profit_margin < Decimal("0.02"):  # Less than 2%
            risk_score += Decimal("20")
        elif profit_margin < Decimal("0.05"):  # Less than 5%
            risk_score += Decimal("10")
        
        return min(risk_score, Decimal("30"))  # Cap at 30
    
    def _calculate_platform_risk(self, buy_platform: str, sell_platform: str) -> Decimal:
        """Calculate platform-specific operational risks."""
        risk_score = Decimal("5")  # Base platform risk
        
        # Add risk for cross-platform settlement
        if buy_platform != sell_platform:
            risk_score += Decimal("5")
        
        # Platform-specific risks
        if "polymarket" in [buy_platform, sell_platform]:
            risk_score += Decimal("3")  # Blockchain settlement risk
        
        return min(risk_score, Decimal("20"))  # Cap at 20
    
    def _calculate_confidence(self, buy_orderbook: Optional[NormalizedOrderBook],
                            sell_orderbook: Optional[NormalizedOrderBook]) -> Decimal:
        """Calculate confidence in risk assessment."""
        confidence = Decimal("1.0")
        
        # Reduce for missing orderbooks
        if not buy_orderbook:
            confidence *= Decimal("0.7")
        if not sell_orderbook:
            confidence *= Decimal("0.7")
        
        # Reduce for stale data
        if buy_orderbook:
            if buy_orderbook.timestamp.tzinfo is None:
                now = datetime.now()
            else:
                now = datetime.now(buy_orderbook.timestamp.tzinfo)
            age = (now - buy_orderbook.timestamp).total_seconds()
            if age > 30:
                confidence *= Decimal("0.8")
        
        return max(confidence, Decimal("0.3"))
    
    def _generate_recommendations(self, total_risk: Decimal, exec_risk: Decimal,
                                market_risk: Decimal, platform_risk: Decimal,
                                trade_size: Decimal) -> List[str]:
        """Generate actionable recommendations based on risk assessment."""
        recommendations = []
        
        if total_risk > Decimal("40"):
            recommendations.append("⚠️ High risk - consider reducing position size")
        
        if exec_risk > Decimal("30"):
            recommendations.append("📊 High execution risk - use limit orders or split execution")
        
        if market_risk > Decimal("20"):
            recommendations.append("📈 High market risk - monitor for volatility spikes")
        
        if platform_risk > Decimal("15"):
            recommendations.append("🏦 Elevated platform risk - ensure quick settlement")
        
        if trade_size > self.max_position_size:
            recommendations.append(f"💰 Reduce position size to ${self.max_position_size}")
        
        if not recommendations:
            recommendations.append("✅ Risk levels acceptable - proceed with standard execution")
        
        return recommendations
