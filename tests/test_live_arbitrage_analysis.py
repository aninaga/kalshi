"""
Live arbitrage analysis test - finds real profit opportunities between Kalshi and Polymarket.
This test connects to actual APIs and calculates real arbitrage opportunities.
"""

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional
import sys
import os

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.market_analyzer import MarketAnalyzer
from kalshi_arbitrage.risk_engine import RiskEngine, ExecutionStrategy, NetworkCondition
from kalshi_arbitrage.data_normalizer import DataNormalizer
from kalshi_arbitrage.monitoring import get_monitoring_system

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ArbitrageAnalysisReport:
    """Collects and formats arbitrage analysis results."""
    
    def __init__(self):
        self.start_time = datetime.now()
        self.opportunities_found = []
        self.total_opportunities = 0
        self.profitable_opportunities = 0
        self.market_stats = {
            'kalshi_markets': 0,
            'polymarket_markets': 0,
            'matched_pairs': 0
        }
        self.profit_distribution = {
            '0-1%': 0,
            '1-2%': 0,
            '2-3%': 0,
            '3-5%': 0,
            '5%+': 0
        }
        self.max_profit_opportunity = None
        self.total_potential_profit = Decimal("0")
        
    def add_opportunity(self, opportunity: Dict[str, Any], risk_adjusted: bool = False):
        """Add an analyzed opportunity to the report."""
        self.total_opportunities += 1
        
        profit_margin = Decimal(str(opportunity.get('profit_margin', 0)))
        total_profit = Decimal(str(opportunity.get('total_profit', 0)))
        
        if profit_margin > 0:
            self.profitable_opportunities += 1
            self.opportunities_found.append(opportunity)
            self.total_potential_profit += total_profit
            
            # Update distribution
            if profit_margin < Decimal("0.01"):
                self.profit_distribution['0-1%'] += 1
            elif profit_margin < Decimal("0.02"):
                self.profit_distribution['1-2%'] += 1
            elif profit_margin < Decimal("0.03"):
                self.profit_distribution['2-3%'] += 1
            elif profit_margin < Decimal("0.05"):
                self.profit_distribution['3-5%'] += 1
            else:
                self.profit_distribution['5%+'] += 1
            
            # Track max profit
            if self.max_profit_opportunity is None or profit_margin > Decimal(str(self.max_profit_opportunity.get('profit_margin', 0))):
                self.max_profit_opportunity = opportunity
    
    def generate_report(self) -> str:
        """Generate a formatted report of the analysis."""
        duration = (datetime.now() - self.start_time).total_seconds()
        
        report = f"""
================================================================================
                    ARBITRAGE ANALYSIS REPORT
================================================================================

Analysis Duration: {duration:.1f} seconds
Analysis Time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}

MARKET COVERAGE
--------------
Kalshi Markets Analyzed: {self.market_stats['kalshi_markets']}
Polymarket Markets Analyzed: {self.market_stats['polymarket_markets']}
Matched Market Pairs: {self.market_stats['matched_pairs']}

OPPORTUNITY SUMMARY
------------------
Total Opportunities Found: {self.total_opportunities}
Profitable Opportunities: {self.profitable_opportunities}
Success Rate: {(self.profitable_opportunities/self.total_opportunities*100) if self.total_opportunities > 0 else 0:.1f}%

PROFIT DISTRIBUTION
------------------
0-1%:   {self.profit_distribution['0-1%']} opportunities
1-2%:   {self.profit_distribution['1-2%']} opportunities
2-3%:   {self.profit_distribution['2-3%']} opportunities
3-5%:   {self.profit_distribution['3-5%']} opportunities
5%+:    {self.profit_distribution['5%+']} opportunities

FINANCIAL SUMMARY
----------------
Total Potential Profit: ${self.total_potential_profit:.2f}
Average Profit per Opportunity: ${(self.total_potential_profit/self.profitable_opportunities) if self.profitable_opportunities > 0 else 0:.2f}
"""

        if self.max_profit_opportunity:
            opp = self.max_profit_opportunity
            report += f"""
BEST OPPORTUNITY
---------------
Profit Margin: {opp['profit_margin']:.2%}
Total Profit: ${opp['total_profit']:.2f}
Strategy: {opp['strategy']}
Kalshi Market: {opp.get('match_data', {}).get('kalshi_market', {}).get('title', 'Unknown')}
Polymarket: {opp.get('match_data', {}).get('polymarket_market', {}).get('title', 'Unknown')}
Max Volume: {opp['max_tradeable_volume']} shares
"""

        # Add top 5 opportunities
        if self.opportunities_found:
            report += "\nTOP 5 OPPORTUNITIES\n------------------\n"
            sorted_opps = sorted(self.opportunities_found, 
                               key=lambda x: x.get('profit_margin', 0), 
                               reverse=True)[:5]
            
            for i, opp in enumerate(sorted_opps, 1):
                kalshi_title = opp.get('match_data', {}).get('kalshi_market', {}).get('title', 'Unknown')[:50]
                poly_title = opp.get('match_data', {}).get('polymarket_market', {}).get('title', 'Unknown')[:50]
                
                report += f"""
{i}. Profit: {opp['profit_margin']:.2%} (${opp['total_profit']:.2f})
   Strategy: {opp['strategy']}
   Kalshi: {kalshi_title}...
   Polymarket: {poly_title}...
   Volume: {opp['max_tradeable_volume']} shares
"""

        report += "\n================================================================================"
        return report

async def analyze_live_markets():
    """Perform live market analysis and find arbitrage opportunities."""
    logger.info("Starting live arbitrage analysis...")
    
    # Initialize components
    analyzer = MarketAnalyzer()
    risk_engine = RiskEngine()
    report = ArbitrageAnalysisReport()
    
    try:
        # Initialize analyzer
        logger.info("Initializing market analyzer...")
        await analyzer.initialize()
        
        # Set completeness level for thorough analysis
        analyzer.set_completeness_level('BALANCED')
        
        # Run full market scan
        logger.info("Scanning all markets for arbitrage opportunities...")
        scan_result = await analyzer.run_full_scan()
        
        # Update market stats
        report.market_stats['kalshi_markets'] = scan_result['kalshi_markets_count']
        report.market_stats['polymarket_markets'] = scan_result['polymarket_markets_count']
        report.market_stats['matched_pairs'] = scan_result['potential_matches']
        
        # Process opportunities
        opportunities = scan_result.get('opportunities', [])
        logger.info(f"Found {len(opportunities)} raw opportunities, analyzing risk...")
        
        # Analyze each opportunity with risk engine
        for opp in opportunities:
            try:
                # For this test, we'll use synthetic orderbooks since we may not have live orderbook data
                # In production, these would come from the WebSocket streams
                
                # Add basic risk-adjusted metrics
                profit_margin = Decimal(str(opp.get('profit_margin', 0)))
                
                # Simple risk adjustment for the test
                # Assume 0.5% slippage on each side and Polymarket fees
                kalshi_slippage = Decimal("0.005")
                polymarket_slippage = Decimal("0.005")
                polymarket_fees = Decimal("0.02")  # 2% total fees
                
                # Adjust profit margin
                risk_adjusted_margin = profit_margin - kalshi_slippage - polymarket_slippage - polymarket_fees
                
                if risk_adjusted_margin > 0:
                    opp['risk_adjusted_margin'] = float(risk_adjusted_margin)
                    opp['profit_margin'] = float(risk_adjusted_margin)  # Use risk-adjusted for reporting
                    
                    # Recalculate total profit
                    volume = Decimal(str(opp.get('max_tradeable_volume', 0)))
                    avg_price = Decimal(str(opp.get('weighted_avg_buy_price', 0.5)))
                    opp['total_profit'] = float(risk_adjusted_margin * volume * avg_price)
                    
                    report.add_opportunity(opp, risk_adjusted=True)
                    
            except Exception as e:
                logger.error(f"Error processing opportunity: {e}")
                continue
        
        # Generate and return report
        final_report = report.generate_report()
        print(final_report)
        
        # Save detailed results
        results_file = f"arbitrage_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(results_file, 'w') as f:
            json.dump({
                'summary': {
                    'total_opportunities': report.total_opportunities,
                    'profitable_opportunities': report.profitable_opportunities,
                    'total_potential_profit': str(report.total_potential_profit),
                    'market_stats': report.market_stats,
                    'profit_distribution': report.profit_distribution
                },
                'opportunities': report.opportunities_found[:20]  # Top 20
            }, f, indent=2, default=str)
        
        logger.info(f"Detailed results saved to {results_file}")
        
        return report
        
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        raise
    finally:
        # Cleanup
        if hasattr(analyzer, 'kalshi_client') and analyzer.kalshi_client:
            if hasattr(analyzer.kalshi_client, 'websocket_client'):
                await analyzer.kalshi_client.websocket_client.disconnect()
        if hasattr(analyzer, 'polymarket_client') and analyzer.polymarket_client:
            if hasattr(analyzer.polymarket_client, 'websocket_client'):
                await analyzer.polymarket_client.websocket_client.disconnect()

async def test_specific_market_pair():
    """Test analysis on a specific known market pair for detailed examination."""
    logger.info("Testing specific market pair analysis...")
    
    # Example: Election markets that likely exist on both platforms
    test_markets = [
        {
            'kalshi': {
                'id': 'USPREZ-2024',
                'title': 'Who will win the 2024 US Presidential Election?',
                'yes_price': 0.52,
                'no_price': 0.48,
                'yes_ask': 0.53,
                'yes_bid': 0.51,
                'volume': 100000
            },
            'polymarket': {
                'id': '0xabc123',
                'title': 'Will the Democratic candidate win the 2024 US Presidential Election?',
                'yes_price': 0.49,
                'no_price': 0.51,
                'yes_ask': 0.50,
                'yes_bid': 0.48,
                'volume': 500000
            }
        }
    ]
    
    risk_engine = RiskEngine()
    
    for pair in test_markets:
        # Calculate raw arbitrage
        kalshi_yes = Decimal(str(pair['kalshi']['yes_price']))
        poly_yes = Decimal(str(pair['polymarket']['yes_price']))
        
        # Strategy 1: Buy Polymarket YES, Sell Kalshi YES
        if kalshi_yes > poly_yes:
            raw_profit = kalshi_yes - poly_yes
            
            # Apply fees and slippage
            poly_buy_price = Decimal(str(pair['polymarket']['yes_ask']))
            kalshi_sell_price = Decimal(str(pair['kalshi']['yes_bid']))
            
            # Polymarket fees (2% total)
            poly_cost = poly_buy_price * Decimal("1.02")
            
            # Net profit
            net_profit = kalshi_sell_price - poly_cost
            profit_margin = net_profit / poly_cost if poly_cost > 0 else Decimal("0")
            
            print(f"""
Specific Market Analysis:
------------------------
Kalshi: {pair['kalshi']['title']}
Polymarket: {pair['polymarket']['title']}

Raw Price Difference: {raw_profit:.1%}
Buy Polymarket @ ${poly_buy_price:.3f} (+ 2% fees = ${poly_cost:.3f})
Sell Kalshi @ ${kalshi_sell_price:.3f}
Net Profit Margin: {profit_margin:.2%}
Profit per $1000: ${profit_margin * 1000:.2f}
""")

if __name__ == "__main__":
    # Run the analysis
    print("Starting Kalshi-Polymarket Arbitrage Analysis...")
    print("=" * 80)
    
    # Test infrastructure components first
    print("\n1. Testing Infrastructure Components...")
    import pytest
    pytest.main([__file__, "-v", "-k", "test_", "--tb=short"])
    
    # Run live analysis
    print("\n2. Running Live Market Analysis...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Run specific market test
        loop.run_until_complete(test_specific_market_pair())
        
        # Run full market analysis
        report = loop.run_until_complete(analyze_live_markets())
        
    except KeyboardInterrupt:
        print("\nAnalysis interrupted by user")
    except Exception as e:
        print(f"\nAnalysis error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        loop.close()