import aiohttp
import asyncio
from .config import Config

class APIClient:
    """Base class for asynchronous API clients."""
    def __init__(self, base_url):
        self.base_url = base_url

    async def get(self, endpoint, params=None, headers=None):
        """Performs an asynchronous GET request."""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{self.base_url}/{endpoint}", params=params, headers=headers) as response:
                    response.raise_for_status()  # Raise an exception for bad status codes
                    return await response.json()
            except aiohttp.ClientError as e:
                print(f"An error occurred: {e}")
                return None

class KalshiAPIClient(APIClient):
    """API client for the Kalshi platform."""
    def __init__(self):
        super().__init__(Config.get_kalshi_base_url())
        self.headers = {"Authorization": f"Bearer {Config.KALSHI_API_KEY}"}

    async def get_markets(self):
        """Fetches all active markets from Kalshi."""
        return await self.get("markets", headers=self.headers)

class PolygonAPIClient(APIClient):
    """API client for the Polygon platform."""
    def __init__(self):
        super().__init__(Config.POLYGON_API_BASE)
        self.params = {"apiKey": Config.POLYGON_API_KEY}

    async def get_market_details(self, ticker):
        """Fetches market details for a given ticker from Polygon."""
        return await self.get(f"v3/reference/tickers/{ticker}", params=self.params)
