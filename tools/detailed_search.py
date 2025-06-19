#!/usr/bin/env python3
"""
Detailed search through market data to find specific markets.
"""
import json
import asyncio
import sys
import re
from typing import List, Dict, Any

sys.path.append('..')

from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient
from kalshi_arbitrage.utils import clean_title, get_similarity_score

async def search_all_markets():
    """Search through all markets for NYC mayor and Zohran Mamdani."""
    
    print("Initializing API clients...")
    kalshi_client = KalshiClient()
    poly_client = PolymarketClient()
    
    # Try to authenticate with Kalshi
    await kalshi_client.authenticate()
    
    print("Fetching all markets from both platforms...")
    kalshi_markets = await kalshi_client.get_all_markets()
    poly_markets = await poly_client.get_all_markets()
    
    print(f"Kalshi markets: {len(kalshi_markets)}")
    print(f"Polymarket markets: {len(poly_markets)}")
    
    # Search terms
    search_terms = [
        "zohran", "mamdani", "mayor", "nyc", "new york city", "new york"
    ]
    
    print("\nSearching Kalshi markets...")
    kalshi_matches = []
    for market in kalshi_markets:
        title = market.get('title', '').lower()
        ticker = market.get('ticker', '').lower()
        
        for term in search_terms:
            if term in title or term in ticker:
                kalshi_matches.append({
                    'ticker': market.get('ticker', ''),
                    'title': market.get('title', ''),
                    'status': market.get('status', ''),
                    'yes_price': market.get('yes_price'),
                    'no_price': market.get('no_price'),
                    'matched_term': term
                })
                break
    
    print(f"Found {len(kalshi_matches)} Kalshi matches:")
    for match in kalshi_matches:
        print(f"  [{match['ticker']}] {match['title']} (term: {match['matched_term']})")
        if match['yes_price']:
            print(f"    YES: {match['yes_price']}, NO: {match['no_price']}")
    
    print("\nSearching Polymarket markets...")
    poly_matches = []
    for market in poly_markets:
        question = market.get('question', '').lower()
        slug = market.get('slug', '').lower()
        
        for term in search_terms:
            if term in question or term in slug:
                poly_matches.append({
                    'id': market.get('id', ''),
                    'question': market.get('question', ''),
                    'slug': market.get('slug', ''),
                    'active': market.get('active', False),
                    'matched_term': term
                })
                break
    
    print(f"Found {len(poly_matches)} Polymarket matches:")
    for match in poly_matches:
        print(f"  [{match['id']}] {match['question']} (term: {match['matched_term']})")
        print(f"    Slug: {match['slug']}, Active: {match['active']}")
    
    # Now let's try specific searches for the exact markets mentioned
    print("\n" + "="*60)
    print("SPECIFIC SEARCH FOR ZOHRAN MAMDANI NYC MAYOR MARKETS")
    print("="*60)
    
    # Look for exact matches
    zohran_kalshi = []
    zohran_poly = []
    
    for market in kalshi_markets:
        title = market.get('title', '')
        if 'zohran' in title.lower() and 'mamdani' in title.lower():
            zohran_kalshi.append(market)
        elif 'mamdani' in title.lower():
            zohran_kalshi.append(market)
    
    for market in poly_markets:
        question = market.get('question', '')
        if 'zohran' in question.lower() and 'mamdani' in question.lower():
            zohran_poly.append(market)
        elif 'mamdani' in question.lower():
            zohran_poly.append(market)
    
    print(f"\nFound {len(zohran_kalshi)} Kalshi markets mentioning Mamdani:")
    for market in zohran_kalshi:
        title = market.get('title', '')
        ticker = market.get('ticker', '')
        yes_price = market.get('yes_price')
        no_price = market.get('no_price')
        
        print(f"  [{ticker}] {title}")
        if yes_price and no_price:
            print(f"    YES: {yes_price:.3f} ({yes_price*100:.1f}%), NO: {no_price:.3f} ({no_price*100:.1f}%)")
        
        # Get more details if available
        try:
            details = await kalshi_client.get_market_details(ticker)
            if details:
                print(f"    Status: {details.get('status', 'Unknown')}")
                print(f"    Close time: {details.get('close_time', 'Unknown')}")
        except:
            pass
    
    print(f"\nFound {len(zohran_poly)} Polymarket markets mentioning Mamdani:")
    for market in zohran_poly:
        question = market.get('question', '')
        market_id = market.get('id', '')
        active = market.get('active', False)
        
        print(f"  [{market_id}] {question}")
        print(f"    Active: {active}")
        
        # Try to get prices
        try:
            prices = await poly_client.get_market_prices(market_id)
            if prices:
                for token_id, price_data in prices.items():
                    if price_data and price_data.get('mid_price'):
                        outcome = price_data.get('outcome', 'Unknown')
                        mid_price = price_data.get('mid_price')
                        print(f"    {outcome}: {mid_price:.3f} ({mid_price*100:.1f}%)")
        except Exception as e:
            print(f"    Error getting prices: {e}")
    
    # Check if we can match them
    if zohran_kalshi and zohran_poly:
        print("\n" + "="*60)
        print("POTENTIAL MATCHES FOUND - ANALYZING SIMILARITY")
        print("="*60)
        
        for k_market in zohran_kalshi:
            k_title = k_market.get('title', '')
            k_clean = clean_title(k_title)
            
            for p_market in zohran_poly:
                p_title = p_market.get('question', '')
                p_clean = clean_title(p_title)
                
                similarity = get_similarity_score(k_clean, p_clean)
                
                print(f"\nKalshi: {k_title}")
                print(f"Polymarket: {p_title}")
                print(f"Cleaned Kalshi: '{k_clean}'")
                print(f"Cleaned Polymarket: '{p_clean}'")
                print(f"Similarity Score: {similarity:.3f}")
                
                if similarity >= 0.55:  # Default threshold
                    print("*** MATCH FOUND! This should have been detected by the system ***")
                    
                    # Calculate arbitrage
                    k_yes = k_market.get('yes_price', 0)
                    
                    # Get Polymarket prices
                    try:
                        poly_prices = await poly_client.get_market_prices(p_market.get('id', ''))
                        if poly_prices:
                            for token_id, price_data in poly_prices.items():
                                if price_data and price_data.get('mid_price'):
                                    p_price = price_data.get('mid_price')
                                    outcome = price_data.get('outcome', 'Unknown')
                                    
                                    # Calculate potential arbitrage
                                    if k_yes > 0 and p_price > 0:
                                        diff = abs(k_yes - p_price)
                                        profit_pct = (diff / min(k_yes, p_price)) * 100
                                        
                                        print(f"Price difference for {outcome}: {diff:.3f} ({profit_pct:.1f}%)")
                                        
                                        if profit_pct > 2:  # 2% threshold
                                            print(f"*** ARBITRAGE OPPORTUNITY: {profit_pct:.1f}% ***")
                    except Exception as e:
                        print(f"Error getting Polymarket prices: {e}")
                else:
                    print("Below similarity threshold")

if __name__ == "__main__":
    asyncio.run(search_all_markets())