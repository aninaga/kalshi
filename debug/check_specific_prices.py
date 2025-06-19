#!/usr/bin/env python3
"""
Check specific Zohran Mamdani market prices to understand the arbitrage opportunity.
"""
import asyncio
import sys
sys.path.append('..')

from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient

async def check_prices():
    """Check prices for the specific Zohran Mamdani markets."""
    
    kalshi_client = KalshiClient()
    poly_client = PolymarketClient()
    
    print("=== CHECKING ZOHRAN MAMDANI MARKET PRICES ===")
    
    # Specific Kalshi markets for Zohran Mamdani
    kalshi_markets = [
        "KXMAYORNYCNOMD-25-ZM",  # Will Zohran Mamdani be the nominee for NYC Mayorship for Democratic party?
        "KXENDORSEBERNIEZOHRAN-25JUN25"  # Will Bernie Sanders endorse Zohran Mamdani?
    ]
    
    # Specific Polymarket markets for Zohran Mamdani
    polymarket_markets = [
        "516792",  # Will Zohran Mamdani win the Democratic Primary for Mayor of New York City?
        "538932",  # Will Zohran Mamdani win the 2025 NYC mayoral election?
        "551980",  # Will Zohran Mamdani win second place in the 2025 NYC mayoral election?
        "552014",  # Will Zohran Mamdani finish third in the 2025 New York City Mayoral Democratic Primary?
        "552033",  # Zohran Mamdani gets the most first-choice votes in the 2025 New York City Mayoral Democratic Primary?
    ]
    
    print("\n=== KALSHI MARKETS ===")
    
    # Check Kalshi prices (try both authenticated and unauthenticated)
    await kalshi_client.authenticate()
    
    for ticker in kalshi_markets:
        try:
            # Get market details
            market_details = await kalshi_client.get_market_details(ticker)
            if market_details:
                print(f"\n[{ticker}] {market_details.get('title', 'No title')}")
                print(f"  Status: {market_details.get('status', 'Unknown')}")
                print(f"  Close time: {market_details.get('close_time', 'Unknown')}")
                
                # Try to get pricing data
                yes_price = market_details.get('yes_price', market_details.get('yes_bid', market_details.get('yes_ask')))
                no_price = market_details.get('no_price', market_details.get('no_bid', market_details.get('no_ask')))
                
                if yes_price is not None:
                    # Convert from cents to decimal if needed
                    if yes_price > 1.0:
                        yes_price = yes_price / 100.0
                    print(f"  YES price: {yes_price:.3f} ({yes_price*100:.1f}%)")
                
                if no_price is not None:
                    # Convert from cents to decimal if needed  
                    if no_price > 1.0:
                        no_price = no_price / 100.0
                    print(f"  NO price: {no_price:.3f} ({no_price*100:.1f}%)")
                
                # Also check if there are other price fields
                for key, value in market_details.items():
                    if 'price' in key.lower() or 'bid' in key.lower() or 'ask' in key.lower():
                        print(f"  {key}: {value}")
                        
            else:
                print(f"\n[{ticker}] Could not fetch market details")
                
        except Exception as e:
            print(f"\n[{ticker}] Error: {e}")
    
    print("\n=== POLYMARKET MARKETS ===")
    
    # Check Polymarket prices
    for market_id in polymarket_markets:
        try:
            # Get market details
            market_response = await poly_client.get(f"markets/{market_id}")
            if market_response:
                question = market_response.get('question', 'No question')
                print(f"\n[{market_id}] {question}")
                print(f"  Active: {market_response.get('active', False)}")
                print(f"  End date: {market_response.get('endDate', 'Unknown')}")
                
                # Get current prices
                prices = await poly_client.get_market_prices(market_id)
                if prices:
                    for token_id, price_data in prices.items():
                        if price_data:
                            outcome = price_data.get('outcome', 'Unknown')
                            buy_price = price_data.get('buy_price')
                            sell_price = price_data.get('sell_price')  
                            mid_price = price_data.get('mid_price')
                            
                            print(f"  Token {token_id} ({outcome}):")
                            if buy_price is not None:
                                print(f"    Buy: {buy_price:.3f} ({buy_price*100:.1f}%)")
                            if sell_price is not None:
                                print(f"    Sell: {sell_price:.3f} ({sell_price*100:.1f}%)")
                            if mid_price is not None:
                                print(f"    Mid: {mid_price:.3f} ({mid_price*100:.1f}%)")
                else:
                    print("  No prices available")
            else:
                print(f"\n[{market_id}] Could not fetch market details")
                
        except Exception as e:
            print(f"\n[{market_id}] Error: {e}")
    
    print("\n=== ANALYSIS ===")
    print("Based on the user's claim of 23% Kalshi vs 21% Polymarket:")
    print("- This suggests a 2% price difference")
    print("- After accounting for fees (0% Kalshi, 2% Polymarket), this might still be profitable")
    print("- The system should detect this if both markets are accessible")
    print("\nPossible reasons why this isn't being detected:")
    print("1. Kalshi authentication failures preventing price retrieval")
    print("2. Market matching algorithm not finding the right equivalent markets")
    print("3. Minimum profit threshold (2%) filtering out the opportunity")
    print("4. Timing - prices may have changed since the observation")

if __name__ == "__main__":
    asyncio.run(check_prices())