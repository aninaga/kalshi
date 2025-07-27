"""
Simple price discrepancy checker using direct API calls to get actual pricing data.
"""

import asyncio
import aiohttp
import json
from typing import Dict, List, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def get_kalshi_market_sample():
    """Get a sample of Kalshi markets with pricing."""
    url = "https://api.elections.kalshi.com/trade-api/v2/markets"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params={"limit": 50}) as response:
                if response.status == 200:
                    data = await response.json()
                    markets = data.get('markets', [])
                    logger.info(f"Retrieved {len(markets)} Kalshi markets")
                    
                    # Show sample market structure
                    if markets:
                        logger.info("Sample Kalshi market:")
                        sample = markets[0]
                        logger.info(json.dumps({
                            'ticker': sample.get('ticker'),
                            'title': sample.get('title'),
                            'yes_bid': sample.get('yes_bid'),
                            'yes_ask': sample.get('yes_ask'),
                            'no_bid': sample.get('no_bid'),
                            'no_ask': sample.get('no_ask'),
                            'status': sample.get('status')
                        }, indent=2))
                    
                    return markets
                else:
                    logger.error(f"Kalshi API error: {response.status}")
                    return []
        except Exception as e:
            logger.error(f"Error fetching Kalshi markets: {e}")
            return []

async def get_polymarket_market_sample():
    """Get a sample of Polymarket markets with pricing."""
    url = "https://gamma-api.polymarket.com/markets"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params={"limit": 50, "active": "true"}) as response:
                if response.status == 200:
                    markets = await response.json()
                    logger.info(f"Retrieved {len(markets)} Polymarket markets")
                    
                    # Show sample market structure
                    if markets:
                        logger.info("Sample Polymarket market:")
                        sample = markets[0]
                        logger.info(json.dumps({
                            'id': sample.get('id'),
                            'question': sample.get('question'),
                            'outcomes': sample.get('outcomes'),
                            'outcomePrices': sample.get('outcomePrices'),
                            'clobTokenIds': sample.get('clobTokenIds'),
                            'active': sample.get('active')
                        }, indent=2))
                    
                    return markets
                else:
                    logger.error(f"Polymarket API error: {response.status}")
                    return []
        except Exception as e:
            logger.error(f"Error fetching Polymarket markets: {e}")
            return []

def find_similar_markets(kalshi_markets: List[Dict], polymarket_markets: List[Dict]) -> List[Dict]:
    """Find markets that might be similar between platforms."""
    similar_pairs = []
    
    # Simple keyword matching for similar markets
    election_keywords = ['election', 'president', 'trump', 'biden', 'harris', 'vote', 'win']
    
    for k_market in kalshi_markets:
        k_title = k_market.get('title', '').lower()
        
        # Check if it's an election-related market
        if any(keyword in k_title for keyword in election_keywords):
            for p_market in polymarket_markets:
                p_question = p_market.get('question', '').lower()
                
                if any(keyword in p_question for keyword in election_keywords):
                    # Calculate simple similarity
                    k_words = set(k_title.split())
                    p_words = set(p_question.split())
                    
                    if len(k_words & p_words) >= 2:  # At least 2 common words
                        # Extract pricing
                        k_yes_price = None
                        if k_market.get('yes_bid') and k_market.get('yes_ask'):
                            k_yes_price = (k_market['yes_bid'] + k_market['yes_ask']) / 200  # Convert from cents and average
                        
                        p_yes_price = None
                        outcome_prices = p_market.get('outcomePrices', [])
                        if isinstance(outcome_prices, str):
                            try:
                                outcome_prices = json.loads(outcome_prices)
                            except:
                                outcome_prices = []
                        
                        if outcome_prices and len(outcome_prices) > 0:
                            try:
                                p_yes_price = float(outcome_prices[0])
                            except:
                                pass
                        
                        if k_yes_price and p_yes_price:
                            price_diff = abs(k_yes_price - p_yes_price)
                            percent_diff = (price_diff / p_yes_price) * 100 if p_yes_price > 0 else 0
                            
                            similar_pairs.append({
                                'kalshi': {
                                    'ticker': k_market.get('ticker'),
                                    'title': k_market.get('title'),
                                    'yes_price': k_yes_price
                                },
                                'polymarket': {
                                    'id': p_market.get('id'),
                                    'question': p_market.get('question'),
                                    'yes_price': p_yes_price
                                },
                                'price_difference': price_diff,
                                'percent_difference': percent_diff,
                                'common_words': len(k_words & p_words)
                            })
    
    return sorted(similar_pairs, key=lambda x: x['percent_difference'], reverse=True)

async def main():
    """Main analysis function."""
    logger.info("Starting simple price discrepancy analysis...")
    
    # Get market data
    kalshi_markets = await get_kalshi_market_sample()
    polymarket_markets = await get_polymarket_market_sample()
    
    if not kalshi_markets or not polymarket_markets:
        logger.error("Failed to get market data from one or both platforms")
        return
    
    # Find similar markets
    similar_pairs = find_similar_markets(kalshi_markets, polymarket_markets)
    
    logger.info(f"Found {len(similar_pairs)} potentially similar market pairs")
    
    if similar_pairs:
        print("\n" + "="*80)
        print("PRICE DISCREPANCY ANALYSIS RESULTS")
        print("="*80)
        
        total_discrepancies = len(similar_pairs)
        large_discrepancies = [p for p in similar_pairs if p['percent_difference'] > 5]
        medium_discrepancies = [p for p in similar_pairs if 1 <= p['percent_difference'] <= 5]
        small_discrepancies = [p for p in similar_pairs if p['percent_difference'] < 1]
        
        print(f"\nOVERVIEW:")
        print(f"Total market pairs found: {total_discrepancies}")
        print(f"Large discrepancies (>5%): {len(large_discrepancies)}")
        print(f"Medium discrepancies (1-5%): {len(medium_discrepancies)}")
        print(f"Small discrepancies (<1%): {len(small_discrepancies)}")
        
        if total_discrepancies > 0:
            avg_diff = sum(p['percent_difference'] for p in similar_pairs) / len(similar_pairs)
            max_diff = max(p['percent_difference'] for p in similar_pairs)
            print(f"Average price difference: {avg_diff:.2f}%")
            print(f"Maximum price difference: {max_diff:.2f}%")
        
        print(f"\nTOP 10 LARGEST DISCREPANCIES:")
        print("-" * 80)
        
        for i, pair in enumerate(similar_pairs[:10], 1):
            k = pair['kalshi']
            p = pair['polymarket']
            
            print(f"\n{i}. Price Difference: {pair['percent_difference']:.2f}% (${pair['price_difference']:.3f})")
            print(f"   Kalshi:     ${k['yes_price']:.3f} - {k['title']}")
            print(f"   Polymarket: ${p['yes_price']:.3f} - {p['question']}")
            print(f"   Common words: {pair['common_words']}")
    else:
        print("No similar market pairs found with valid pricing data.")

if __name__ == "__main__":
    asyncio.run(main())