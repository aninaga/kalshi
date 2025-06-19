#!/usr/bin/env python3
"""
Test Polymarket API directly to understand the market structure.
"""
import asyncio
import aiohttp
import json

async def test_polymarket_api():
    """Test Polymarket API to understand market structure."""
    
    base_url = "https://gamma-api.polymarket.com"
    clob_base = "https://clob.polymarket.com"
    
    # Test market ID for Zohran Mamdani
    market_id = "516792"
    
    print(f"Testing Polymarket API for market {market_id}")
    print("="*60)
    
    # Test 1: Get market details from gamma API
    print("\n1. Testing market details from gamma API...")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{base_url}/markets/{market_id}"
            print(f"URL: {url}")
            
            async with session.get(url) as response:
                print(f"Status: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    print("Market data structure:")
                    print(json.dumps(data, indent=2)[:1000] + "..." if len(json.dumps(data, indent=2)) > 1000 else json.dumps(data, indent=2))
                    
                    # Check for tokens
                    if 'tokens' in data:
                        print(f"\nFound {len(data['tokens'])} tokens:")
                        for token in data['tokens']:
                            print(f"  Token ID: {token.get('token_id')}, Outcome: {token.get('outcome')}")
                    else:
                        print("\nNo 'tokens' field found in market data")
                        print("Available fields:", list(data.keys()))
                else:
                    print(f"Error: {response.status}")
                    print(await response.text())
    except Exception as e:
        print(f"Error testing gamma API: {e}")
    
    # Test 2: Try to get market from different endpoint
    print("\n2. Testing markets list endpoint...")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{base_url}/markets"
            params = {"id": market_id}
            print(f"URL: {url}?id={market_id}")
            
            async with session.get(url, params=params) as response:
                print(f"Status: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, list) and len(data) > 0:
                        market = data[0]
                        print("Market data from list endpoint:")
                        print(json.dumps(market, indent=2)[:1000] + "..." if len(json.dumps(market, indent=2)) > 1000 else json.dumps(market, indent=2))
                        
                        # Check for tokens
                        if 'tokens' in market:
                            print(f"\nFound {len(market['tokens'])} tokens:")
                            for token in market['tokens']:
                                print(f"  Token ID: {token.get('token_id')}, Outcome: {token.get('outcome')}")
                        else:
                            print("\nNo 'tokens' field found")
                            print("Available fields:", list(market.keys()))
                    else:
                        print("No market data returned or empty list")
                else:
                    print(f"Error: {response.status}")
    except Exception as e:
        print(f"Error testing markets list: {e}")
    
    # Test 3: Check if we can manually construct token IDs or find price endpoints
    print("\n3. Testing CLOB API for prices...")
    try:
        # Try to get book/orderbook data
        async with aiohttp.ClientSession() as session:
            url = f"{clob_base}/book"
            print(f"URL: {url}")
            
            # We need a token ID, let's see if we can find it somewhere
            # For now, let's just try with a sample token ID format
            test_token_ids = [
                f"{market_id}_0",
                f"{market_id}_1", 
                market_id,
                # Common Polymarket token ID patterns
                "21742633143463906290569050155826241533067272736897614950488156847949938836455",
                "40615537626906848220375549436282421563968863824208458446966095994998632157709"
            ]
            
            for token_id in test_token_ids:
                try:
                    params = {"token_id": token_id}
                    async with session.get(url, params=params) as response:
                        if response.status == 200:
                            data = await response.json()
                            print(f"Success with token_id {token_id}:")
                            print(json.dumps(data, indent=2)[:500] + "...")
                            break
                        else:
                            print(f"Token {token_id}: Status {response.status}")
                except Exception as e:
                    print(f"Token {token_id}: Error {e}")
                    
    except Exception as e:
        print(f"Error testing CLOB API: {e}")
    
    # Test 4: Check the simplified market endpoint
    print("\n4. Testing simplified endpoint...")
    try:
        async with aiohttp.ClientSession() as session:
            # Try different market endpoints
            endpoints = [
                f"{base_url}/markets/{market_id}",
                f"{base_url}/market/{market_id}",
                f"{base_url}/events/{market_id}",
            ]
            
            for endpoint in endpoints:
                try:
                    print(f"Trying: {endpoint}")
                    async with session.get(endpoint) as response:
                        print(f"  Status: {response.status}")
                        if response.status == 200:
                            data = await response.json()
                            print(f"  Keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                            if isinstance(data, dict) and 'tokens' in data:
                                print(f"  Found tokens!")
                                break
                except Exception as e:
                    print(f"  Error: {e}")
    except Exception as e:
        print(f"Error in simplified test: {e}")

if __name__ == "__main__":
    asyncio.run(test_polymarket_api())