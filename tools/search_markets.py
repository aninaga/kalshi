#!/usr/bin/env python3
"""
Script to search for specific market terms in the fetched data.
"""
import json
import os
import re
from typing import List, Dict, Any

def search_in_text(text: str, search_terms: List[str]) -> bool:
    """Check if any search terms appear in the text (case insensitive)."""
    if not text:
        return False
    
    text_lower = text.lower()
    for term in search_terms:
        if term.lower() in text_lower:
            return True
    return False

def search_markets_in_file(filepath: str, search_terms: List[str]) -> List[Dict[str, Any]]:
    """Search for markets containing any of the search terms in a JSON file."""
    matches = []
    
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        
        # Check if it's a scan report
        if 'opportunities' in data:
            # This is a scan report, check opportunities
            for opp in data.get('opportunities', []):
                kalshi_market = opp.get('match_data', {}).get('kalshi_market', {})
                poly_market = opp.get('match_data', {}).get('polymarket_market', {})
                
                kalshi_title = kalshi_market.get('title', '')
                poly_title = poly_market.get('title', '')
                
                if search_in_text(kalshi_title, search_terms) or search_in_text(poly_title, search_terms):
                    matches.append({
                        'file': filepath,
                        'type': 'opportunity',
                        'kalshi_title': kalshi_title,
                        'polymarket_title': poly_title,
                        'data': opp
                    })
        
        # Check if it's raw market data (list of markets)
        elif isinstance(data, list):
            for market in data:
                title = market.get('title', market.get('question', ''))
                if search_in_text(title, search_terms):
                    matches.append({
                        'file': filepath,
                        'type': 'market',
                        'title': title,
                        'platform': market.get('platform', 'unknown'),
                        'data': market
                    })
    
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
    
    return matches

def main():
    # Terms to search for
    search_terms = [
        "Zohran Mamdani", "Mamdani", "zohran", 
        "NYC mayor", "NYC Mayor", "nyc mayor", 
        "New York City mayor", "new york city mayor",
        "mayor", "Mayor"
    ]
    
    print(f"Searching for terms: {search_terms}")
    print("=" * 50)
    
    # Search in market_data directory
    market_data_dir = "market_data"
    if not os.path.exists(market_data_dir):
        print(f"Directory {market_data_dir} not found!")
        return
    
    all_matches = []
    
    # Search in all JSON files
    for filename in os.listdir(market_data_dir):
        if filename.endswith('.json'):
            filepath = os.path.join(market_data_dir, filename)
            matches = search_markets_in_file(filepath, search_terms)
            all_matches.extend(matches)
    
    if not all_matches:
        print("No matches found for the search terms.")
        print("\nLet's check what kind of markets are available...")
        
        # Show sample market titles from recent scan
        recent_scan_files = [f for f in os.listdir(market_data_dir) if f.startswith('scan_report_') and f.endswith('.json')]
        if recent_scan_files:
            recent_file = os.path.join(market_data_dir, sorted(recent_scan_files)[-1])
            print(f"\nChecking recent scan file: {recent_file}")
            
            # Try to run the system to get fresh data
            print("\nTrying to fetch fresh market data...")
            import asyncio
            import sys
            sys.path.append('..')
            
            try:
                from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient
                
                async def quick_check():
                    kalshi_client = KalshiClient()
                    poly_client = PolymarketClient()
                    
                    print("Fetching some Kalshi markets...")
                    kalshi_markets = await kalshi_client.get_all_markets()
                    print(f"Got {len(kalshi_markets)} Kalshi markets")
                    
                    print("Fetching some Polymarket markets...")
                    poly_markets = await poly_client.get_all_markets()
                    print(f"Got {len(poly_markets)} Polymarket markets")
                    
                    # Check for mayor-related markets
                    mayor_markets = []
                    
                    for market in kalshi_markets[:50]:  # Check first 50
                        title = market.get('title', '')
                        if search_in_text(title, ['mayor', 'Mayor', 'NYC', 'nyc', 'New York']):
                            mayor_markets.append({
                                'platform': 'kalshi',
                                'title': title,
                                'ticker': market.get('ticker', ''),
                                'status': market.get('status', '')
                            })
                    
                    for market in poly_markets[:50]:  # Check first 50
                        title = market.get('question', '')
                        if search_in_text(title, ['mayor', 'Mayor', 'NYC', 'nyc', 'New York']):
                            mayor_markets.append({
                                'platform': 'polymarket',
                                'title': title,
                                'id': market.get('id', ''),
                                'active': market.get('active', False)
                            })
                    
                    print(f"\nFound {len(mayor_markets)} mayor-related markets:")
                    for market in mayor_markets:
                        print(f"  {market['platform']}: {market['title']}")
                    
                    return mayor_markets
                
                mayor_markets = asyncio.run(quick_check())
                
            except Exception as e:
                print(f"Error fetching fresh data: {e}")
    else:
        print(f"Found {len(all_matches)} matches:")
        for match in all_matches:
            print(f"\nFile: {match['file']}")
            print(f"Type: {match['type']}")
            if match['type'] == 'opportunity':
                print(f"Kalshi: {match['kalshi_title']}")
                print(f"Polymarket: {match['polymarket_title']}")
            else:
                print(f"Title: {match['title']}")
                print(f"Platform: {match['platform']}")
            print("-" * 30)

if __name__ == "__main__":
    main()