import aiohttp
import asyncio
import json
import logging
import os
import time
import base64
from typing import Optional, Dict, List, Any
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from .config import Config

logger = logging.getLogger(__name__)

class APIClient:
    """Base class for async API clients with retry logic."""
    
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.timeout = aiohttp.ClientTimeout(total=timeout)
    
    async def _request_with_retry(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Make request with retry logic."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        
        for attempt in range(Config.MAX_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.request(method, url, **kwargs) as response:
                        response.raise_for_status()
                        return await response.json()
            except aiohttp.ClientError as e:
                if attempt == Config.MAX_RETRIES - 1:
                    logger.error(f"API request failed after {Config.MAX_RETRIES} attempts for {url}: {e}")
                    return None
                else:
                    logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}, retrying...")
                    await asyncio.sleep(Config.RETRY_DELAY)
            except Exception as e:
                logger.error(f"Unexpected error for {url}: {e}")
                return None
    
    async def get(self, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """GET request wrapper with retry logic."""
        return await self._request_with_retry("GET", endpoint, **kwargs)
    
    async def post(self, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """POST request wrapper with retry logic."""
        return await self._request_with_retry("POST", endpoint, **kwargs)

class KalshiClient(APIClient):
    """Kalshi API client with optional authentication for WebSocket access."""
    
    def __init__(self):
        super().__init__(Config.KALSHI_API_BASE)
        self.session_headers = {}
        self.auth_token = None
        self.user_id = None
    
    def _sign_pss_text(self, private_key, text: str) -> str:
        """Sign text using RSA-PSS with SHA256."""
        message = text.encode('utf-8')
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')
    
    def _get_auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """Generate authentication headers for RSA-signed requests."""
        kalshi_api_key = os.getenv('KALSHI_API_KEY')
        kalshi_private_key = os.getenv('KALSHI_PRIVATE_KEY')
        
        if not kalshi_api_key or not kalshi_private_key:
            return {}
        
        try:
            # Load private key
            private_key = serialization.load_pem_private_key(
                kalshi_private_key.encode('utf-8'),
                password=None
            )
            
            # Create timestamp
            timestamp = str(int(time.time() * 1000))
            
            # Create message to sign: timestamp + method + path
            msg_string = timestamp + method + path
            
            # Sign the message
            signature = self._sign_pss_text(private_key, msg_string)
            
            logger.debug(f"Auth details - Method: {method}, Path: {path}, Message: {msg_string[:50]}...")
            
            return {
                'KALSHI-ACCESS-KEY': kalshi_api_key,
                'KALSHI-ACCESS-SIGNATURE': signature,
                'KALSHI-ACCESS-TIMESTAMP': timestamp
            }
        except Exception as e:
            logger.error(f"Error creating auth headers: {e}")
            return {}
    
    async def authenticate(self) -> bool:
        """Authenticate with Kalshi API using RSA private key and get WebSocket token."""
        kalshi_api_key = os.getenv('KALSHI_API_KEY')
        kalshi_private_key = os.getenv('KALSHI_PRIVATE_KEY')
        
        if kalshi_api_key and kalshi_private_key:
            try:
                # Test with a simpler public endpoint first
                logger.info("Testing Kalshi RSA authentication...")
                response = await self._kalshi_request_with_retry('GET', 'portfolio')
                if response:
                    # Create a simple access token (for WebSocket auth)
                    self.auth_token = kalshi_api_key  # Use API key as token for now
                    logger.info("Successfully configured Kalshi RSA authentication")
                    return True
                else:
                    logger.warning("Kalshi RSA authentication failed, using public endpoints")
                    return False
            except Exception as e:
                logger.warning(f"Kalshi authentication error: {e}, using public endpoints")
                return False
        else:
            logger.info("Using Kalshi public endpoints (no authentication required)")
            return True
    
    async def _kalshi_request_with_retry(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Make Kalshi request with RSA authentication."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        path = f"/trade-api/v2/{endpoint.lstrip('/')}"
        
        # Add authentication headers if available
        auth_headers = self._get_auth_headers(method, path)
        if 'headers' not in kwargs:
            kwargs['headers'] = {}
        kwargs['headers'].update(auth_headers)
        
        for attempt in range(Config.MAX_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.request(method, url, **kwargs) as response:
                        response.raise_for_status()
                        return await response.json()
            except aiohttp.ClientError as e:
                if attempt == Config.MAX_RETRIES - 1:
                    logger.error(f"API request failed after {Config.MAX_RETRIES} attempts for {url}: {e}")
                    return None
                else:
                    logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}, retrying...")
                    await asyncio.sleep(Config.RETRY_DELAY)
            except Exception as e:
                logger.error(f"Unexpected error for {url}: {e}")
                return None
    
    async def get_all_markets(self) -> List[Dict[str, Any]]:
        """Fetch ALL active markets from Kalshi with pagination."""
        all_markets = []
        cursor = None
        
        while True:
            try:
                params = {"status": "open", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                
                response = await self.get("markets", 
                                        params=params)
                
                if not response:
                    break
                
                markets = response.get("markets", [])
                all_markets.extend(markets)
                
                # Check for pagination
                cursor = response.get("cursor")
                if not cursor or not markets:
                    break
                    
                logger.info(f"Fetched {len(markets)} markets, total: {len(all_markets)}")
                
            except Exception as e:
                logger.error(f"Error fetching Kalshi markets: {e}")
                break
        
        logger.info(f"Total Kalshi markets fetched: {len(all_markets)}")
        return all_markets
    
    async def get_market_details(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Get detailed information for a specific market."""
        try:
            response = await self.get(f"markets/{market_ticker}", 
                                    headers=self.session_headers)
            return response.get("market") if response else None
        except Exception as e:
            logger.error(f"Error fetching details for market {market_ticker}: {e}")
            return None

class PolymarketClient(APIClient):
    """Comprehensive Polymarket client for full market capture."""
    
    def __init__(self):
        super().__init__(Config.POLYMARKET_GAMMA_BASE)
        self.clob_base = Config.POLYMARKET_CLOB_BASE
    
    async def get_all_markets(self, include_closed: bool = False) -> List[Dict[str, Any]]:
        """Fetch ALL markets from Polymarket with pagination."""
        all_markets = []
        offset = 0
        limit = 100
        
        while True:
            try:
                params = {
                    "active": "true",
                    "closed": str(include_closed).lower(),
                    "limit": limit,
                    "offset": offset
                }
                
                response = await self.get("markets", params=params)
                
                if not response or not isinstance(response, list):
                    break
                
                if not response:  # Empty response means no more markets
                    break
                
                all_markets.extend(response)
                logger.info(f"Fetched {len(response)} markets, total: {len(all_markets)}")
                
                # If we got fewer markets than the limit, we've reached the end
                if len(response) < limit:
                    break
                
                offset += limit
                
            except Exception as e:
                logger.error(f"Error fetching Polymarket markets: {e}")
                break
        
        logger.info(f"Total Polymarket markets fetched: {len(all_markets)}")
        return all_markets
    
    async def get_market_prices(self, market_id: str) -> Dict[str, Optional[float]]:
        """Get current prices for all tokens in a market."""
        prices = {}
        
        try:
            # Get market details
            market_response = await self.get(f"markets/{market_id}")
            if not market_response:
                return prices
            
            # Parse outcomes and prices from the market response
            outcomes = market_response.get('outcomes')
            outcome_prices = market_response.get('outcomePrices')
            clob_token_ids = market_response.get('clobTokenIds')
            
            # Parse JSON strings if needed
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = []
            
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = []
            
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except json.JSONDecodeError:
                    clob_token_ids = []
            
            # Ensure we have matching arrays
            if not all([outcomes, outcome_prices, clob_token_ids]) or \
               not (len(outcomes) == len(outcome_prices) == len(clob_token_ids)):
                logger.warning(f"Mismatched data arrays for market {market_id}")
                return prices
            
            # Create price data for each outcome
            for i, (outcome, price_str, token_id) in enumerate(zip(outcomes, outcome_prices, clob_token_ids)):
                try:
                    mid_price = float(price_str)
                    
                    # Try to get more precise bid/ask prices from CLOB if available
                    buy_price = await self._get_token_price(token_id, "BUY")
                    sell_price = await self._get_token_price(token_id, "SELL")
                    
                    # Use CLOB prices if available, otherwise use the market price
                    if buy_price is not None and sell_price is not None:
                        actual_mid = (buy_price + sell_price) / 2
                    else:
                        actual_mid = mid_price
                        buy_price = mid_price  # Fallback
                        sell_price = mid_price  # Fallback
                    
                    prices[token_id] = {
                        'outcome': outcome,
                        'buy_price': buy_price,
                        'sell_price': sell_price,
                        'mid_price': actual_mid
                    }
                    
                except (ValueError, TypeError) as e:
                    logger.warning(f"Could not parse price for outcome {outcome}: {price_str}")
                    continue
                
        except Exception as e:
            logger.error(f"Error fetching prices for market {market_id}: {e}")
        
        return prices
    
    async def _get_token_price(self, token_id: str, side: str) -> Optional[float]:
        """Get price for a specific token and side."""
        url = f"{self.clob_base}/price"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                params = {"token_id": token_id, "side": side}
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    result = await response.json()
                    price_str = result.get("price")
                    return float(price_str) if price_str else None
        except Exception as e:
            logger.debug(f"Failed to fetch {side} price for token {token_id}: {e}")
            return None
