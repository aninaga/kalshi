#!/usr/bin/env python3
"""
Check the actual values of relevant Polymarket fields.
"""
import asyncio
import aiohttp
import json

async def check_fields():
    """Check the actual values of price-related fields in Polymarket."""
    
    base_url = "https://gamma-api.polymarket.com"
    market_id = "516792"  # Zohran Mamdani market
    
    async with aiohttp.ClientSession() as session:
        url = f"{base_url}/markets/{market_id}"
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                
                print("=== RELEVANT PRICE FIELDS ===")
                
                # Check outcomes
                outcomes = data.get('outcomes')
                print(f"outcomes: {outcomes}")
                print(f"outcomes type: {type(outcomes)}")
                
                # Check outcome prices
                outcome_prices = data.get('outcomePrices')
                print(f"outcomePrices: {outcome_prices}")
                print(f"outcomePrices type: {type(outcome_prices)}")
                
                # Check CLOB token IDs
                clob_token_ids = data.get('clobTokenIds')
                print(f"clobTokenIds: {clob_token_ids}")
                print(f"clobTokenIds type: {type(clob_token_ids)}")
                
                # Check other price fields
                print(f"lastTradePrice: {data.get('lastTradePrice')}")
                print(f"bestBid: {data.get('bestBid')}")
                print(f"bestAsk: {data.get('bestAsk')}")
                print(f"spread: {data.get('spread')}")
                
                # Try to parse outcomes if it's a string
                if isinstance(outcomes, str):
                    try:
                        outcomes_parsed = json.loads(outcomes)
                        print(f"outcomes parsed: {outcomes_parsed}")
                    except:
                        print("Could not parse outcomes as JSON")
                
                # Try to parse outcome prices if it's a string  
                if isinstance(outcome_prices, str):
                    try:
                        prices_parsed = json.loads(outcome_prices)
                        print(f"outcomePrices parsed: {prices_parsed}")
                    except:
                        print("Could not parse outcomePrices as JSON")
                
                # Try to parse CLOB token IDs if it's a string
                if isinstance(clob_token_ids, str):
                    try:
                        clob_parsed = json.loads(clob_token_ids)
                        print(f"clobTokenIds parsed: {clob_parsed}")
                    except:
                        print("Could not parse clobTokenIds as JSON")

if __name__ == "__main__":
    asyncio.run(check_fields())