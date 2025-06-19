#!/usr/bin/env python3
"""
Test script demonstrating lossless arbitrage detection capabilities
"""

import asyncio
import sys
sys.path.append('..')

from kalshi_arbitrage.market_analyzer import MarketAnalyzer
from kalshi_arbitrage.config import Config

async def test_completeness_levels():
    """Test different completeness levels."""
    print("🎯 Testing Lossless Arbitrage Detection - Phase 1")
    print("=" * 60)
    
    analyzer = MarketAnalyzer()
    await analyzer.initialize()
    
    # Test all completeness levels
    levels = ['FAST', 'BALANCED', 'LOSSLESS']
    
    for level in levels:
        print(f"\n🚀 Testing {level} Completeness Level")
        print("-" * 40)
        
        # Set completeness level
        analyzer.set_completeness_level(level)
        analyzer.reset_completeness_stats()
        
        # Get adaptive limits
        limits = analyzer.get_adaptive_limits()
        level_config = Config.COMPLETENESS_LEVELS[level]
        
        print(f"Description: {level_config['description']}")
        print(f"Expected Completeness: {level_config['expected_completeness']:.1%}")
        print(f"Max Matches per Market: {limits['max_matches']}")
        print(f"Max Trades per Opportunity: {limits['max_trades']}")
        print(f"Cache TTL - Prices: {analyzer._price_cache_ttl}s, Orderbooks: {analyzer._orderbook_cache_ttl}s")
        
        # Test volume overlap calculation with limits
        buy_orderbook = [
            {'price': 0.45, 'size': 100},
            {'price': 0.46, 'size': 200}, 
            {'price': 0.47, 'size': 150},
            {'price': 0.48, 'size': 300},
            {'price': 0.49, 'size': 250}
        ]
        
        sell_orderbook = [
            {'price': 0.58, 'size': 80},
            {'price': 0.57, 'size': 120},
            {'price': 0.56, 'size': 200},
            {'price': 0.55, 'size': 180},
            {'price': 0.54, 'size': 220}
        ]
        
        trades = analyzer._calculate_overlapping_volume(buy_orderbook, sell_orderbook, 0.0, 0.02)
        total_profit = sum(t['net_profit'] for t in trades)
        total_volume = sum(t['volume'] for t in trades)
        
        print(f"Sample Calculation:")
        print(f"  Trades Found: {len(trades)}")
        print(f"  Total Profit: ${total_profit:.2f}")
        print(f"  Total Volume: {total_volume:.0f} shares")
        print(f"  Truncated Trades: {analyzer.completeness_stats['truncated_trades']}")
        
        estimated_completeness = analyzer._calculate_estimated_completeness()
        print(f"  Estimated Completeness: {estimated_completeness:.1%}")

async def test_adaptive_limits():
    """Test adaptive limits based on market conditions."""
    print(f"\n\n🧠 Testing Adaptive Limits")
    print("-" * 40)
    
    analyzer = MarketAnalyzer()
    await analyzer.initialize()
    analyzer.set_completeness_level('BALANCED')
    
    # Test different market conditions
    conditions = [
        {'volatility': 0.01, 'opportunity_density': 0.05, 'name': 'Low Activity'},
        {'volatility': 0.03, 'opportunity_density': 0.15, 'name': 'Normal Activity'},
        {'volatility': 0.05, 'opportunity_density': 0.25, 'name': 'High Activity'}
    ]
    
    for condition in conditions:
        limits = analyzer.get_adaptive_limits(condition)
        print(f"{condition['name']} (vol: {condition['volatility']:.1%}, opp: {condition['opportunity_density']:.1%}):")
        print(f"  Max Matches: {limits['max_matches']}")
        print(f"  Max Trades: {limits['max_trades']}")

async def demonstrate_completeness_tracking():
    """Demonstrate completeness tracking capabilities."""
    print(f"\n\n📊 Demonstrating Completeness Tracking")
    print("-" * 40)
    
    analyzer = MarketAnalyzer()
    await analyzer.initialize()
    
    # Simulate some operations to populate stats
    analyzer.completeness_stats['cache_hits'] = 150
    analyzer.completeness_stats['cache_misses'] = 50
    analyzer.completeness_stats['data_staleness_warnings'] = 15
    analyzer.completeness_stats['truncated_matches'] = 25
    analyzer.completeness_stats['truncated_trades'] = 8
    
    estimated_completeness = analyzer._calculate_estimated_completeness()
    
    print("Simulated Scan Statistics:")
    print(f"  Cache Hit Rate: {analyzer.completeness_stats['cache_hits']/(analyzer.completeness_stats['cache_hits']+analyzer.completeness_stats['cache_misses']):.1%}")
    print(f"  Data Staleness Rate: {analyzer.completeness_stats['data_staleness_warnings']/200:.1%}")
    print(f"  Truncated Matches: {analyzer.completeness_stats['truncated_matches']}")
    print(f"  Truncated Trades: {analyzer.completeness_stats['truncated_trades']}")
    print(f"  Estimated Completeness: {estimated_completeness:.1%}")
    print(f"  Estimated Missed Opportunities: {analyzer.completeness_stats['estimated_missed_opportunities']}")

if __name__ == "__main__":
    async def main():
        await test_completeness_levels()
        await test_adaptive_limits() 
        await demonstrate_completeness_tracking()
        
        print(f"\n\n✅ Phase 1 Lossless Features Demonstrated!")
        print("🚀 Key Achievements:")
        print("   • Configurable completeness levels (FAST/BALANCED/LOSSLESS)")
        print("   • Adaptive limits based on market conditions")
        print("   • Comprehensive completeness tracking and monitoring")
        print("   • Opportunity cost estimation for truncated results")
        print("   • Cache staleness detection and warnings")
        print("   • Command-line completeness level selection")
    
    asyncio.run(main())