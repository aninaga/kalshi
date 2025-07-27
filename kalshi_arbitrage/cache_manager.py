"""
Redis-based caching layer for high-performance real-time data management.
Provides fast access to market data, orderbooks, and arbitrage opportunities.
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Any, Union, Set, Tuple
from datetime import datetime, timedelta
from decimal import Decimal
import aioredis
from dataclasses import asdict
import msgpack

from .data_normalizer import (
    NormalizedMarket, NormalizedOrderBook, NormalizedPrice,
    NormalizedOutcome, DataNormalizer
)

logger = logging.getLogger(__name__)

class CacheKeyGenerator:
    """Generates consistent cache keys for different data types."""
    
    # Key prefixes
    MARKET = "market"
    ORDERBOOK = "orderbook"
    PRICE = "price"
    OPPORTUNITY = "opportunity"
    SIMILARITY = "similarity"
    MARKET_LIST = "market_list"
    STATS = "stats"
    
    @staticmethod
    def market_key(platform: str, market_id: str) -> str:
        """Generate key for market data."""
        return f"{CacheKeyGenerator.MARKET}:{platform}:{market_id}"
    
    @staticmethod
    def orderbook_key(platform: str, market_id: str, outcome_id: Optional[str] = None) -> str:
        """Generate key for orderbook data."""
        if outcome_id:
            return f"{CacheKeyGenerator.ORDERBOOK}:{platform}:{market_id}:{outcome_id}"
        return f"{CacheKeyGenerator.ORDERBOOK}:{platform}:{market_id}"
    
    @staticmethod
    def price_key(platform: str, market_id: str, outcome_id: Optional[str] = None) -> str:
        """Generate key for price data."""
        if outcome_id:
            return f"{CacheKeyGenerator.PRICE}:{platform}:{market_id}:{outcome_id}"
        return f"{CacheKeyGenerator.PRICE}:{platform}:{market_id}"
    
    @staticmethod
    def opportunity_key(opportunity_id: str) -> str:
        """Generate key for arbitrage opportunity."""
        return f"{CacheKeyGenerator.OPPORTUNITY}:{opportunity_id}"
    
    @staticmethod
    def similarity_key(market1_id: str, market2_id: str) -> str:
        """Generate key for market similarity score."""
        # Sort IDs to ensure consistent key regardless of order
        sorted_ids = sorted([market1_id, market2_id])
        return f"{CacheKeyGenerator.SIMILARITY}:{sorted_ids[0]}:{sorted_ids[1]}"
    
    @staticmethod
    def market_list_key(platform: str, status: Optional[str] = None) -> str:
        """Generate key for market lists."""
        if status:
            return f"{CacheKeyGenerator.MARKET_LIST}:{platform}:{status}"
        return f"{CacheKeyGenerator.MARKET_LIST}:{platform}:all"
    
    @staticmethod
    def stats_key(metric: str) -> str:
        """Generate key for statistics."""
        return f"{CacheKeyGenerator.STATS}:{metric}"

class CacheSerializer:
    """Handles serialization/deserialization for cache storage."""
    
    @staticmethod
    def serialize(data: Any) -> bytes:
        """Serialize data for storage using MessagePack for efficiency."""
        if isinstance(data, (NormalizedMarket, NormalizedOrderBook, NormalizedPrice)):
            # Convert dataclass to dict
            data_dict = data.to_dict()
            data_dict['_type'] = data.__class__.__name__
            return msgpack.packb(data_dict, use_bin_type=True)
        elif isinstance(data, Decimal):
            return msgpack.packb({"_type": "Decimal", "value": str(data)}, use_bin_type=True)
        elif isinstance(data, datetime):
            return msgpack.packb({"_type": "datetime", "value": data.isoformat()}, use_bin_type=True)
        else:
            # Fallback to msgpack for other types
            return msgpack.packb(data, use_bin_type=True)
    
    @staticmethod
    def deserialize(data: bytes) -> Any:
        """Deserialize data from storage."""
        try:
            obj = msgpack.unpackb(data, raw=False)
            
            # Handle special types
            if isinstance(obj, dict) and '_type' in obj:
                type_name = obj['_type']
                
                if type_name == 'NormalizedMarket':
                    # Reconstruct NormalizedMarket
                    # This is simplified - you'd need proper reconstruction
                    return obj
                elif type_name == 'NormalizedOrderBook':
                    return obj
                elif type_name == 'NormalizedPrice':
                    return obj
                elif type_name == 'Decimal':
                    return Decimal(obj['value'])
                elif type_name == 'datetime':
                    return datetime.fromisoformat(obj['value'])
            
            return obj
        except Exception as e:
            logger.error(f"Deserialization error: {e}")
            return None

class RedisCache:
    """Redis-based cache implementation with async support."""
    
    def __init__(self, redis_url: str = "redis://localhost:6379", 
                 db: int = 0, max_connections: int = 50):
        self.redis_url = redis_url
        self.db = db
        self.max_connections = max_connections
        self.redis: Optional[aioredis.Redis] = None
        self._connected = False
        
    async def connect(self):
        """Establish Redis connection."""
        try:
            self.redis = await aioredis.create_redis_pool(
                self.redis_url,
                db=self.db,
                minsize=5,
                maxsize=self.max_connections,
                encoding=None  # Use binary mode for msgpack
            )
            self._connected = True
            logger.info("Connected to Redis cache")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise
    
    async def disconnect(self):
        """Close Redis connection."""
        if self.redis:
            self.redis.close()
            await self.redis.wait_closed()
            self._connected = False
            logger.info("Disconnected from Redis cache")
    
    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        if not self._connected:
            return None
            
        try:
            data = await self.redis.get(key)
            if data:
                return CacheSerializer.deserialize(data)
            return None
        except Exception as e:
            logger.error(f"Cache get error for key {key}: {e}")
            return None
    
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Set value in cache with optional TTL."""
        if not self._connected:
            return False
            
        try:
            serialized = CacheSerializer.serialize(value)
            if ttl:
                await self.redis.setex(key, ttl, serialized)
            else:
                await self.redis.set(key, serialized)
            return True
        except Exception as e:
            logger.error(f"Cache set error for key {key}: {e}")
            return False
    
    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        if not self._connected:
            return False
            
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            logger.error(f"Cache delete error for key {key}: {e}")
            return False
    
    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        if not self._connected:
            return False
            
        try:
            return await self.redis.exists(key) > 0
        except Exception as e:
            logger.error(f"Cache exists error for key {key}: {e}")
            return False
    
    async def get_many(self, keys: List[str]) -> Dict[str, Any]:
        """Get multiple values from cache."""
        if not self._connected or not keys:
            return {}
            
        try:
            values = await self.redis.mget(*keys)
            result = {}
            for key, value in zip(keys, values):
                if value:
                    result[key] = CacheSerializer.deserialize(value)
            return result
        except Exception as e:
            logger.error(f"Cache get_many error: {e}")
            return {}
    
    async def set_many(self, data: Dict[str, Any], ttl: Optional[int] = None) -> bool:
        """Set multiple values in cache."""
        if not self._connected or not data:
            return False
            
        try:
            # Serialize all values
            serialized_data = {}
            for key, value in data.items():
                serialized_data[key] = CacheSerializer.serialize(value)
            
            # Use pipeline for atomic operation
            pipeline = self.redis.pipeline()
            for key, value in serialized_data.items():
                if ttl:
                    pipeline.setex(key, ttl, value)
                else:
                    pipeline.set(key, value)
            
            await pipeline.execute()
            return True
        except Exception as e:
            logger.error(f"Cache set_many error: {e}")
            return False
    
    async def increment(self, key: str, amount: int = 1) -> Optional[int]:
        """Increment counter in cache."""
        if not self._connected:
            return None
            
        try:
            return await self.redis.incrby(key, amount)
        except Exception as e:
            logger.error(f"Cache increment error for key {key}: {e}")
            return None
    
    async def add_to_set(self, key: str, *values: str) -> bool:
        """Add values to a set."""
        if not self._connected:
            return False
            
        try:
            await self.redis.sadd(key, *values)
            return True
        except Exception as e:
            logger.error(f"Cache add_to_set error for key {key}: {e}")
            return False
    
    async def get_set_members(self, key: str) -> Set[str]:
        """Get all members of a set."""
        if not self._connected:
            return set()
            
        try:
            members = await self.redis.smembers(key)
            return {m.decode('utf-8') if isinstance(m, bytes) else m for m in members}
        except Exception as e:
            logger.error(f"Cache get_set_members error for key {key}: {e}")
            return set()
    
    async def zadd(self, key: str, score: float, member: str) -> bool:
        """Add member to sorted set with score."""
        if not self._connected:
            return False
            
        try:
            await self.redis.zadd(key, score, member)
            return True
        except Exception as e:
            logger.error(f"Cache zadd error for key {key}: {e}")
            return False
    
    async def zrange(self, key: str, start: int = 0, stop: int = -1, 
                     withscores: bool = False) -> List[Union[str, Tuple[str, float]]]:
        """Get range from sorted set."""
        if not self._connected:
            return []
            
        try:
            result = await self.redis.zrange(key, start, stop, withscores=withscores)
            if withscores:
                return [(m.decode('utf-8') if isinstance(m, bytes) else m, s) for m, s in result]
            else:
                return [m.decode('utf-8') if isinstance(m, bytes) else m for m in result]
        except Exception as e:
            logger.error(f"Cache zrange error for key {key}: {e}")
            return []

class CacheManager:
    """High-level cache manager for arbitrage system."""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.cache = RedisCache(redis_url)
        self.default_ttls = {
            'market': 300,        # 5 minutes
            'orderbook': 10,      # 10 seconds
            'price': 5,           # 5 seconds
            'opportunity': 60,    # 1 minute
            'similarity': 3600,   # 1 hour
            'market_list': 60,    # 1 minute
            'stats': 30          # 30 seconds
        }
        
    async def initialize(self):
        """Initialize cache connection."""
        await self.cache.connect()
        logger.info("Cache manager initialized")
    
    async def shutdown(self):
        """Shutdown cache connection."""
        await self.cache.disconnect()
        logger.info("Cache manager shutdown")
    
    # Market data methods
    async def get_market(self, platform: str, market_id: str) -> Optional[Dict[str, Any]]:
        """Get market data from cache."""
        key = CacheKeyGenerator.market_key(platform, market_id)
        return await self.cache.get(key)
    
    async def set_market(self, platform: str, market_id: str, 
                        market_data: Union[NormalizedMarket, Dict[str, Any]]) -> bool:
        """Store market data in cache."""
        key = CacheKeyGenerator.market_key(platform, market_id)
        ttl = self.default_ttls['market']
        return await self.cache.set(key, market_data, ttl)
    
    async def get_markets_by_platform(self, platform: str) -> List[Dict[str, Any]]:
        """Get all markets for a platform."""
        key = CacheKeyGenerator.market_list_key(platform)
        market_ids = await self.cache.get_set_members(key)
        
        if not market_ids:
            return []
        
        # Get all market data
        market_keys = [CacheKeyGenerator.market_key(platform, mid) for mid in market_ids]
        markets_data = await self.cache.get_many(market_keys)
        
        return list(markets_data.values())
    
    # Orderbook methods
    async def get_orderbook(self, platform: str, market_id: str, 
                           outcome_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get orderbook data from cache."""
        key = CacheKeyGenerator.orderbook_key(platform, market_id, outcome_id)
        return await self.cache.get(key)
    
    async def set_orderbook(self, platform: str, market_id: str,
                           orderbook_data: Union[NormalizedOrderBook, Dict[str, Any]],
                           outcome_id: Optional[str] = None) -> bool:
        """Store orderbook data in cache."""
        key = CacheKeyGenerator.orderbook_key(platform, market_id, outcome_id)
        ttl = self.default_ttls['orderbook']
        return await self.cache.set(key, orderbook_data, ttl)
    
    # Price methods
    async def get_price(self, platform: str, market_id: str,
                       outcome_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get price data from cache."""
        key = CacheKeyGenerator.price_key(platform, market_id, outcome_id)
        return await self.cache.get(key)
    
    async def set_price(self, platform: str, market_id: str,
                       price_data: Union[NormalizedPrice, Dict[str, Any]],
                       outcome_id: Optional[str] = None) -> bool:
        """Store price data in cache."""
        key = CacheKeyGenerator.price_key(platform, market_id, outcome_id)
        ttl = self.default_ttls['price']
        return await self.cache.set(key, price_data, ttl)
    
    # Opportunity methods
    async def get_opportunity(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        """Get arbitrage opportunity from cache."""
        key = CacheKeyGenerator.opportunity_key(opportunity_id)
        return await self.cache.get(key)
    
    async def set_opportunity(self, opportunity_id: str,
                             opportunity_data: Dict[str, Any]) -> bool:
        """Store arbitrage opportunity in cache."""
        key = CacheKeyGenerator.opportunity_key(opportunity_id)
        ttl = self.default_ttls['opportunity']
        
        # Also add to sorted set for ranking
        score = opportunity_data.get('profit_margin', 0) * 100  # Convert to percentage
        await self.cache.zadd('opportunities:ranked', score, opportunity_id)
        
        return await self.cache.set(key, opportunity_data, ttl)
    
    async def get_top_opportunities(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get top arbitrage opportunities by profit margin."""
        # Get top opportunity IDs from sorted set
        opportunity_ids = await self.cache.zrange(
            'opportunities:ranked', 
            -count, 
            -1, 
            withscores=False
        )
        
        if not opportunity_ids:
            return []
        
        # Get opportunity data
        opportunity_keys = [CacheKeyGenerator.opportunity_key(oid) for oid in opportunity_ids]
        opportunities_data = await self.cache.get_many(opportunity_keys)
        
        # Return in order
        result = []
        for oid in reversed(opportunity_ids):  # Reverse to get highest first
            key = CacheKeyGenerator.opportunity_key(oid)
            if key in opportunities_data:
                result.append(opportunities_data[key])
        
        return result
    
    # Similarity methods
    async def get_similarity(self, market1_id: str, market2_id: str) -> Optional[float]:
        """Get cached similarity score between two markets."""
        key = CacheKeyGenerator.similarity_key(market1_id, market2_id)
        return await self.cache.get(key)
    
    async def set_similarity(self, market1_id: str, market2_id: str,
                            score: float) -> bool:
        """Cache similarity score between two markets."""
        key = CacheKeyGenerator.similarity_key(market1_id, market2_id)
        ttl = self.default_ttls['similarity']
        return await self.cache.set(key, score, ttl)
    
    # Statistics methods
    async def increment_stat(self, metric: str, amount: int = 1) -> Optional[int]:
        """Increment a statistics counter."""
        key = CacheKeyGenerator.stats_key(metric)
        return await self.cache.increment(key, amount)
    
    async def get_stat(self, metric: str) -> int:
        """Get a statistics value."""
        key = CacheKeyGenerator.stats_key(metric)
        value = await self.cache.get(key)
        return int(value) if value else 0
    
    async def get_all_stats(self) -> Dict[str, int]:
        """Get all statistics."""
        stats_metrics = [
            'messages_processed',
            'opportunities_found',
            'cache_hits',
            'cache_misses',
            'api_calls',
            'websocket_messages'
        ]
        
        result = {}
        for metric in stats_metrics:
            result[metric] = await self.get_stat(metric)
        
        return result
    
    # Utility methods
    async def clear_stale_data(self):
        """Clear stale data from cache."""
        # This would be more sophisticated in production
        # For now, Redis TTL handles most cleanup
        logger.info("Clearing stale cache data")
    
    async def warmup_cache(self, markets: List[NormalizedMarket]):
        """Pre-populate cache with market data."""
        logger.info(f"Warming up cache with {len(markets)} markets")
        
        for market in markets:
            await self.set_market(market.platform, market.id, market)
            
            # Add to platform market list
            list_key = CacheKeyGenerator.market_list_key(market.platform)
            await self.cache.add_to_set(list_key, market.id)
        
        logger.info("Cache warmup complete")

# Singleton instance
cache_manager = None

async def get_cache_manager(redis_url: str = "redis://localhost:6379") -> CacheManager:
    """Get or create cache manager instance."""
    global cache_manager
    
    if cache_manager is None:
        cache_manager = CacheManager(redis_url)
        await cache_manager.initialize()
    
    return cache_manager