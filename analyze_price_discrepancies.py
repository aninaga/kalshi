"""
Analyze price discrepancies between Kalshi and Polymarket for matched markets.
This will show all pricing differences, even those below arbitrage thresholds.
"""

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
import statistics

# Import our modules
from kalshi_arbitrage.market_analyzer import MarketAnalyzer
from kalshi_arbitrage.data_normalizer import DataNormalizer
from kalshi_arbitrage.utils import get_similarity_score

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PriceDiscrepancyAnalyzer:
    """Analyzes price discrepancies between matched markets."""
    
    def __init__(self):
        self.discrepancies = []
        self.matched_pairs = []
        
    def calculate_price_difference(self, kalshi_price: float, poly_price: float) -> Dict[str, float]:
        """Calculate various measures of price difference."""
        if kalshi_price <= 0 or poly_price <= 0:
            return {"raw_diff": 0, "percent_diff": 0, "abs_percent_diff": 0}
        
        raw_diff = kalshi_price - poly_price
        percent_diff = (raw_diff / poly_price) * 100
        abs_percent_diff = abs(percent_diff)
        
        return {
            "raw_diff": raw_diff,
            "percent_diff": percent_diff, 
            "abs_percent_diff": abs_percent_diff
        }
    
    def analyze_market_pair(self, kalshi_market: Dict, poly_market: Dict, similarity: float) -> Optional[Dict]:
        """Analyze price discrepancy for a matched market pair."""
        try:
            # Extract prices
            kalshi_yes = kalshi_market.get('yes_price', 0.5)
            poly_prices = []
            
            # Find YES outcome in Polymarket
            for outcome in poly_market.get('outcomes', []):
                if outcome.lower() in ['yes', 'true', '1']:
                    # Get price from outcome_prices
                    outcome_prices = poly_market.get('outcome_prices', [])
                    clob_token_ids = poly_market.get('clob_token_ids', [])
                    
                    if isinstance(outcome_prices, str):
                        outcome_prices = json.loads(outcome_prices)
                    if isinstance(clob_token_ids, str):
                        clob_token_ids = json.loads(clob_token_ids)
                    
                    outcomes_list = poly_market.get('outcomes', [])
                    if isinstance(outcomes_list, str):
                        outcomes_list = json.loads(outcomes_list)
                    
                    # Find matching outcome index
                    for i, outcome_name in enumerate(outcomes_list):
                        if outcome_name.lower() in ['yes', 'true', '1']:
                            if i < len(outcome_prices):
                                try:
                                    poly_yes = float(outcome_prices[i])
                                    poly_prices.append(poly_yes)
                                    break
                                except (ValueError, TypeError):
                                    continue
            
            if not poly_prices:
                # Try first outcome as default
                outcome_prices = poly_market.get('outcome_prices', [])
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                if outcome_prices and len(outcome_prices) > 0:
                    try:
                        poly_yes = float(outcome_prices[0])
                        poly_prices.append(poly_yes)
                    except (ValueError, TypeError):
                        return None
            
            if not poly_prices:
                return None
            
            poly_yes = poly_prices[0]  # Use first valid price
            
            # Calculate differences
            price_diff = self.calculate_price_difference(kalshi_yes, poly_yes)
            
            return {
                'kalshi_market': {
                    'id': kalshi_market.get('id', 'unknown'),
                    'title': kalshi_market.get('title', 'Unknown')[:100],
                    'yes_price': kalshi_yes
                },
                'poly_market': {
                    'id': poly_market.get('id', 'unknown'),
                    'title': poly_market.get('question', poly_market.get('title', 'Unknown'))[:100],
                    'yes_price': poly_yes
                },
                'similarity_score': similarity,
                'price_difference': price_diff,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.debug(f"Error analyzing market pair: {e}")
            return None
    
    def generate_discrepancy_report(self) -> str:
        """Generate comprehensive discrepancy analysis report."""
        if not self.discrepancies:
            return "No price discrepancies found with valid pricing data."
        
        # Sort by absolute percentage difference
        sorted_discrepancies = sorted(
            self.discrepancies, 
            key=lambda x: x['price_difference']['abs_percent_diff'], 
            reverse=True
        )
        
        # Calculate statistics
        abs_diffs = [d['price_difference']['abs_percent_diff'] for d in self.discrepancies]
        raw_diffs = [d['price_difference']['raw_diff'] for d in self.discrepancies]
        
        stats = {
            'total_pairs': len(self.discrepancies),
            'avg_abs_diff': statistics.mean(abs_diffs),
            'median_abs_diff': statistics.median(abs_diffs),
            'max_abs_diff': max(abs_diffs),
            'min_abs_diff': min(abs_diffs),
            'std_dev': statistics.stdev(abs_diffs) if len(abs_diffs) > 1 else 0
        }
        
        # Distribution analysis
        distribution = {
            '0-1%': sum(1 for d in abs_diffs if d < 1),
            '1-2%': sum(1 for d in abs_diffs if 1 <= d < 2),
            '2-5%': sum(1 for d in abs_diffs if 2 <= d < 5),
            '5-10%': sum(1 for d in abs_diffs if 5 <= d < 10),
            '10%+': sum(1 for d in abs_diffs if d >= 10)
        }
        
        report = f"""
================================================================================
                    PRICE DISCREPANCY ANALYSIS REPORT
================================================================================

OVERVIEW
--------
Total Market Pairs Analyzed: {stats['total_pairs']}
Analysis Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

PRICING STATISTICS
------------------
Average Absolute Difference: {stats['avg_abs_diff']:.2f}%
Median Absolute Difference: {stats['median_abs_diff']:.2f}%
Maximum Difference: {stats['max_abs_diff']:.2f}%
Minimum Difference: {stats['min_abs_diff']:.2f}%
Standard Deviation: {stats['std_dev']:.2f}%

DISTRIBUTION OF DIFFERENCES
---------------------------
0-1%:    {distribution['0-1%']} pairs ({distribution['0-1%']/stats['total_pairs']*100:.1f}%)
1-2%:    {distribution['1-2%']} pairs ({distribution['1-2%']/stats['total_pairs']*100:.1f}%)
2-5%:    {distribution['2-5%']} pairs ({distribution['2-5%']/stats['total_pairs']*100:.1f}%)
5-10%:   {distribution['5-10%']} pairs ({distribution['5-10%']/stats['total_pairs']*100:.1f}%)
10%+:    {distribution['10%+']} pairs ({distribution['10%+']/stats['total_pairs']*100:.1f}%)

TOP 20 LARGEST DISCREPANCIES
-----------------------------"""

        for i, disc in enumerate(sorted_discrepancies[:20], 1):
            kalshi = disc['kalshi_market']
            poly = disc['poly_market']
            diff = disc['price_difference']
            
            report += f"""
{i:2d}. Difference: {diff['abs_percent_diff']:.2f}% ({diff['raw_diff']:+.3f})
    Similarity: {disc['similarity_score']:.1%}
    Kalshi:  ${kalshi['yes_price']:.3f} - {kalshi['title']}
    Polymarket: ${poly['yes_price']:.3f} - {poly['title']}
"""

        report += f"""
================================================================================
        
INSIGHTS
--------
• {distribution['0-1%']} pairs ({distribution['0-1%']/stats['total_pairs']*100:.1f}%) have very tight pricing (<1% difference)
• {distribution['1-2%'] + distribution['2-5%']} pairs have moderate differences (1-5%)
• {distribution['5-10%'] + distribution['10%+']} pairs show significant pricing gaps (>5%)
• Average difference of {stats['avg_abs_diff']:.2f}% suggests markets are reasonably efficient
• Maximum difference of {stats['max_abs_diff']:.2f}% indicates some pricing inefficiencies exist

POTENTIAL REASONS FOR DISCREPANCIES
------------------------------------
• Different user bases and liquidity pools
• Timing differences in price updates
• Market structure differences (fees, UX)
• Different resolution criteria or interpretation
• Imperfect market matching (title similarity vs actual equivalence)
================================================================================"""
        
        return report

async def analyze_price_discrepancies():
    """Run comprehensive price discrepancy analysis."""
    logger.info("Starting price discrepancy analysis...")
    
    analyzer = MarketAnalyzer()
    discrepancy_analyzer = PriceDiscrepancyAnalyzer()
    
    try:
        # Initialize system
        logger.info("Initializing market analyzer...")
        await analyzer.initialize()
        analyzer.set_completeness_level('BALANCED')
        
        # Get markets
        logger.info("Fetching markets from both platforms...")
        scan_result = await analyzer.run_full_scan()
        
        # Get the raw market data before processing
        kalshi_markets, polymarket_markets = await analyzer._fetch_all_markets()
        
        # Process markets
        processed_kalshi = analyzer._process_kalshi_markets(kalshi_markets)
        processed_polymarket = analyzer._process_polymarket_markets(polymarket_markets)
        
        logger.info(f"Analyzing {len(processed_kalshi)} Kalshi and {len(processed_polymarket)} Polymarket markets...")
        
        # Find matches and analyze discrepancies
        pair_count = 0
        for k_market in processed_kalshi[:1000]:  # Limit to first 1000 for performance
            if not k_market.get('clean_title'):
                continue
                
            for p_market in processed_polymarket:
                if not p_market.get('clean_title'):
                    continue
                
                # Calculate similarity
                similarity = get_similarity_score(
                    k_market.get('clean_title', ''),
                    p_market.get('clean_title', '')
                )
                
                # Only analyze pairs with reasonable similarity
                if similarity >= 0.6:  # Lower threshold to catch more pairs
                    discrepancy = discrepancy_analyzer.analyze_market_pair(
                        k_market, p_market, similarity
                    )
                    
                    if discrepancy:
                        discrepancy_analyzer.discrepancies.append(discrepancy)
                        pair_count += 1
                        
                        # Progress logging
                        if pair_count % 100 == 0:
                            logger.info(f"Analyzed {pair_count} market pairs...")
        
        logger.info(f"Found {len(discrepancy_analyzer.discrepancies)} pairs with pricing data")
        
        # Generate and save report
        report = discrepancy_analyzer.generate_discrepancy_report()
        print(report)
        
        # Save detailed results
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        results_file = f"price_discrepancies_{timestamp}.json"
        
        with open(results_file, 'w') as f:
            json.dump({
                'analysis_time': datetime.now().isoformat(),
                'total_discrepancies': len(discrepancy_analyzer.discrepancies),
                'discrepancies': discrepancy_analyzer.discrepancies[:100]  # Top 100
            }, f, indent=2, default=str)
        
        logger.info(f"Detailed results saved to {results_file}")
        
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup
        if hasattr(analyzer, 'kalshi_client') and analyzer.kalshi_client:
            if hasattr(analyzer.kalshi_client, 'websocket_client'):
                await analyzer.kalshi_client.websocket_client.disconnect()
        if hasattr(analyzer, 'polymarket_client') and analyzer.polymarket_client:
            if hasattr(analyzer.polymarket_client, 'websocket_client'):
                await analyzer.polymarket_client.websocket_client.disconnect()

if __name__ == "__main__":
    asyncio.run(analyze_price_discrepancies())