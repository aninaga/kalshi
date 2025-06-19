#!/usr/bin/env python3
"""
Debug the specific Zohran Mamdani arbitrage detection issue.
"""
import asyncio
import sys
sys.path.append('..')

from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient
from kalshi_arbitrage.utils import clean_title, get_similarity_score
from kalshi_arbitrage.config import Config

async def debug_arbitrage():
    """Debug why the Zohran Mamdani arbitrage isn't being detected."""
    
    print("=== DEBUGGING ZOHRAN MAMDANI ARBITRAGE DETECTION ===\n")
    
    # Known market pairs that should match
    test_cases = [
        {
            'kalshi_ticker': 'KXMAYORNYCNOMD-25-ZM',
            'kalshi_title': 'Will Zohran Mamdani be the nominee for the NYC Mayorship for the Democratic party in 2025?',
            'poly_id': '516792',
            'poly_title': 'Will  Zohran Mamdani win the Democratic Primary for Mayor of New York City?'
        }
    ]
    
    kalshi_client = KalshiClient()
    poly_client = PolymarketClient()
    
    # Try to authenticate (will fail but that's okay)
    await kalshi_client.authenticate()
    
    for case in test_cases:
        print(f"Testing: {case['kalshi_ticker']} vs {case['poly_id']}")
        print(f"Kalshi: {case['kalshi_title']}")
        print(f"Polymarket: {case['poly_title']}")
        
        # Check similarity
        kalshi_clean = clean_title(case['kalshi_title'])
        poly_clean = clean_title(case['poly_title'])
        similarity = get_similarity_score(kalshi_clean, poly_clean)
        
        print(f"\nCleaned titles:")
        print(f"Kalshi: '{kalshi_clean}'")
        print(f"Polymarket: '{poly_clean}'")
        print(f"Similarity: {similarity:.3f} (threshold: {Config.SIMILARITY_THRESHOLD})")
        
        if similarity >= Config.SIMILARITY_THRESHOLD:
            print("✅ Markets would be matched by similarity")
        else:
            print("❌ Markets would NOT be matched by similarity")
        
        # Get Kalshi prices
        print(f"\n--- Kalshi Prices ---")
        try:
            market_details = await kalshi_client.get_market_details(case['kalshi_ticker'])
            if market_details:
                yes_price = market_details.get('yes_price', market_details.get('yes_bid', market_details.get('yes_ask')))
                no_price = market_details.get('no_price', market_details.get('no_bid', market_details.get('no_ask')))
                
                if yes_price is not None:
                    if yes_price > 1.0:
                        yes_price = yes_price / 100.0
                    print(f"YES price: {yes_price:.3f} ({yes_price*100:.1f}%)")
                
                if no_price is not None:
                    if no_price > 1.0:
                        no_price = no_price / 100.0
                    print(f"NO price: {no_price:.3f} ({no_price*100:.1f}%)")
                
                kalshi_yes = yes_price
            else:
                print("Failed to get Kalshi market details")
                kalshi_yes = None
        except Exception as e:
            print(f"Error getting Kalshi prices: {e}")
            kalshi_yes = None
        
        # Get Polymarket prices
        print(f"\n--- Polymarket Prices ---")
        try:
            prices = await poly_client.get_market_prices(case['poly_id'])
            if prices:
                poly_yes = None
                for token_id, price_data in prices.items():
                    if price_data:
                        outcome = price_data.get('outcome', 'Unknown')
                        mid_price = price_data.get('mid_price')
                        buy_price = price_data.get('buy_price')
                        sell_price = price_data.get('sell_price')
                        
                        print(f"Token {token_id} ({outcome}):")
                        if buy_price:
                            print(f"  Buy: {buy_price:.3f} ({buy_price*100:.1f}%)")
                        if sell_price:
                            print(f"  Sell: {sell_price:.3f} ({sell_price*100:.1f}%)")
                        if mid_price:
                            print(f"  Mid: {mid_price:.3f} ({mid_price*100:.1f}%)")
                            
                        # Assume the "Yes" outcome is what we want
                        if 'yes' in outcome.lower() or outcome.lower() == 'true':
                            poly_yes = mid_price
            else:
                print("No prices available from Polymarket")
                poly_yes = None
        except Exception as e:
            print(f"Error getting Polymarket prices: {e}")
            poly_yes = None
        
        # Calculate arbitrage if we have both prices
        if kalshi_yes is not None and poly_yes is not None:
            print(f"\n--- Arbitrage Calculation ---")
            print(f"Kalshi YES: {kalshi_yes:.3f} ({kalshi_yes*100:.1f}%)")
            print(f"Polymarket YES: {poly_yes:.3f} ({poly_yes*100:.1f}%)")
            
            # Strategy 1: Buy Kalshi YES, Sell Polymarket
            gross_profit_1 = poly_yes - kalshi_yes
            kalshi_fee = kalshi_yes * Config.KALSHI_FEE_RATE  # Should be 0
            poly_fee = poly_yes * Config.POLYMARKET_FEE_RATE  # Should be 0.02
            net_profit_1 = gross_profit_1 - kalshi_fee - poly_fee
            profit_margin_1 = net_profit_1 / kalshi_yes if kalshi_yes > 0 else 0
            
            print(f"\nStrategy 1: Buy Kalshi YES ({kalshi_yes:.3f}) → Sell Polymarket ({poly_yes:.3f})")
            print(f"Gross profit: {gross_profit_1:.3f}")
            print(f"Kalshi fee: {kalshi_fee:.3f}")
            print(f"Polymarket fee: {poly_fee:.3f}")
            print(f"Net profit: {net_profit_1:.3f}")
            print(f"Profit margin: {profit_margin_1:.3f} ({profit_margin_1*100:.1f}%)")
            
            if profit_margin_1 >= Config.MIN_PROFIT_THRESHOLD:
                print(f"✅ Strategy 1 meets profit threshold ({Config.MIN_PROFIT_THRESHOLD*100:.1f}%)")
            else:
                print(f"❌ Strategy 1 below profit threshold ({Config.MIN_PROFIT_THRESHOLD*100:.1f}%)")
            
            # Strategy 2: Buy Polymarket, Sell Kalshi YES
            gross_profit_2 = kalshi_yes - poly_yes
            poly_fee_2 = poly_yes * Config.POLYMARKET_FEE_RATE
            kalshi_fee_2 = kalshi_yes * Config.KALSHI_FEE_RATE
            net_profit_2 = gross_profit_2 - poly_fee_2 - kalshi_fee_2
            profit_margin_2 = net_profit_2 / poly_yes if poly_yes > 0 else 0
            
            print(f"\nStrategy 2: Buy Polymarket ({poly_yes:.3f}) → Sell Kalshi YES ({kalshi_yes:.3f})")
            print(f"Gross profit: {gross_profit_2:.3f}")
            print(f"Polymarket fee: {poly_fee_2:.3f}")
            print(f"Kalshi fee: {kalshi_fee_2:.3f}")
            print(f"Net profit: {net_profit_2:.3f}")
            print(f"Profit margin: {profit_margin_2:.3f} ({profit_margin_2*100:.1f}%)")
            
            if profit_margin_2 >= Config.MIN_PROFIT_THRESHOLD:
                print(f"✅ Strategy 2 meets profit threshold ({Config.MIN_PROFIT_THRESHOLD*100:.1f}%)")
            else:
                print(f"❌ Strategy 2 below profit threshold ({Config.MIN_PROFIT_THRESHOLD*100:.1f}%)")
            
            # Overall result
            best_margin = max(profit_margin_1, profit_margin_2)
            if best_margin >= Config.MIN_PROFIT_THRESHOLD:
                print(f"\n🎯 ARBITRAGE OPPORTUNITY DETECTED: {best_margin*100:.1f}%")
            else:
                print(f"\n❌ No profitable arbitrage: Best margin {best_margin*100:.1f}% < threshold {Config.MIN_PROFIT_THRESHOLD*100:.1f}%")
        else:
            print(f"\n❌ Cannot calculate arbitrage - missing prices")
            print(f"Kalshi YES: {kalshi_yes}")
            print(f"Polymarket YES: {poly_yes}")
        
        print("\n" + "="*80 + "\n")

if __name__ == "__main__":
    asyncio.run(debug_arbitrage())