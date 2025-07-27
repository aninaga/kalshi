"""
Unified data normalization layer for standardizing market data across platforms.
Provides consistent data structures for analysis regardless of source.
"""

import json
import logging
from typing import Dict, List, Optional, Any, Union, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

logger = logging.getLogger(__name__)

class MarketStatus(Enum):
    """Standardized market status across platforms."""
    ACTIVE = "active"
    CLOSED = "closed"
    SETTLED = "settled"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"

class OrderSide(Enum):
    """Standardized order side."""
    BUY = "buy"
    SELL = "sell"
    BID = "bid"
    ASK = "ask"

class OutcomeType(Enum):
    """Types of market outcomes."""
    BINARY = "binary"      # Yes/No
    CATEGORICAL = "categorical"  # Multiple exclusive outcomes
    SCALAR = "scalar"      # Numeric range

@dataclass
class NormalizedPrice:
    """Standardized price information."""
    value: Decimal
    volume: Optional[Decimal] = None
    timestamp: Optional[datetime] = None
    source: Optional[str] = None
    
    def to_float(self) -> float:
        """Convert to float for calculations."""
        return float(self.value)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "value": float(self.value),
            "volume": float(self.volume) if self.volume else None,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "source": self.source
        }

@dataclass
class NormalizedOrderLevel:
    """Standardized order book level."""
    price: Decimal
    size: Decimal
    count: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "price": float(self.price),
            "size": float(self.size),
            "count": self.count
        }

@dataclass
class NormalizedOrderBook:
    """Standardized order book."""
    bids: List[NormalizedOrderLevel]
    asks: List[NormalizedOrderLevel]
    timestamp: datetime
    sequence: Optional[int] = None
    
    def get_best_bid(self) -> Optional[NormalizedOrderLevel]:
        """Get best bid (highest price)."""
        return self.bids[0] if self.bids else None
    
    def get_best_ask(self) -> Optional[NormalizedOrderLevel]:
        """Get best ask (lowest price)."""
        return self.asks[0] if self.asks else None
    
    def get_spread(self) -> Optional[Decimal]:
        """Get bid-ask spread."""
        best_bid = self.get_best_bid()
        best_ask = self.get_best_ask()
        if best_bid and best_ask:
            return best_ask.price - best_bid.price
        return None
    
    def get_mid_price(self) -> Optional[Decimal]:
        """Get mid price."""
        best_bid = self.get_best_bid()
        best_ask = self.get_best_ask()
        if best_bid and best_ask:
            return (best_bid.price + best_ask.price) / 2
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "bids": [level.to_dict() for level in self.bids],
            "asks": [level.to_dict() for level in self.asks],
            "timestamp": self.timestamp.isoformat(),
            "sequence": self.sequence,
            "spread": float(self.get_spread()) if self.get_spread() else None,
            "mid_price": float(self.get_mid_price()) if self.get_mid_price() else None
        }

@dataclass
class NormalizedOutcome:
    """Standardized market outcome."""
    id: str
    name: str
    price: Optional[NormalizedPrice] = None
    orderbook: Optional[NormalizedOrderBook] = None
    probability: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price.to_dict() if self.price else None,
            "orderbook": self.orderbook.to_dict() if self.orderbook else None,
            "probability": float(self.probability) if self.probability else None,
            "volume": float(self.volume) if self.volume else None
        }

@dataclass
class NormalizedMarket:
    """Standardized market data structure."""
    id: str
    platform: str
    title: str
    status: MarketStatus
    outcome_type: OutcomeType
    outcomes: List[NormalizedOutcome]
    close_time: Optional[datetime] = None
    volume: Optional[Decimal] = None
    liquidity: Optional[Decimal] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        """Initialize metadata if not provided."""
        if self.metadata is None:
            self.metadata = {}
    
    def get_outcome_by_name(self, name: str) -> Optional[NormalizedOutcome]:
        """Get outcome by name (case-insensitive)."""
        name_lower = name.lower()
        for outcome in self.outcomes:
            if outcome.name.lower() == name_lower:
                return outcome
        return None
    
    def get_binary_prices(self) -> Optional[Tuple[Decimal, Decimal]]:
        """Get YES/NO prices for binary markets."""
        if self.outcome_type != OutcomeType.BINARY:
            return None
            
        yes_outcome = self.get_outcome_by_name("yes")
        no_outcome = self.get_outcome_by_name("no")
        
        if yes_outcome and yes_outcome.price and no_outcome and no_outcome.price:
            return (yes_outcome.price.value, no_outcome.price.value)
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "platform": self.platform,
            "title": self.title,
            "status": self.status.value,
            "outcome_type": self.outcome_type.value,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "volume": float(self.volume) if self.volume else None,
            "liquidity": float(self.liquidity) if self.liquidity else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": self.metadata
        }

class DataNormalizer:
    """Normalizes data from different platforms into standardized format."""
    
    @staticmethod
    def normalize_decimal(value: Union[str, float, int, Decimal], 
                         decimals: int = 6) -> Decimal:
        """Convert value to Decimal with specified precision."""
        if value is None:
            return Decimal("0")
        
        if isinstance(value, Decimal):
            return value.quantize(Decimal(f"0.{'0' * decimals}"), rounding=ROUND_HALF_UP)
        
        try:
            decimal_value = Decimal(str(value))
            return decimal_value.quantize(Decimal(f"0.{'0' * decimals}"), rounding=ROUND_HALF_UP)
        except Exception as e:
            logger.warning(f"Failed to convert {value} to Decimal: {e}")
            return Decimal("0")
    
    @staticmethod
    def normalize_timestamp(timestamp: Union[str, int, float, datetime]) -> Optional[datetime]:
        """Convert various timestamp formats to datetime."""
        if timestamp is None:
            return None
            
        if isinstance(timestamp, datetime):
            return timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
        
        try:
            # Try parsing ISO format
            if isinstance(timestamp, str):
                return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            
            # Try parsing Unix timestamp (seconds or milliseconds)
            if isinstance(timestamp, (int, float)):
                if timestamp > 1e10:  # Milliseconds
                    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                else:  # Seconds
                    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    
        except Exception as e:
            logger.warning(f"Failed to parse timestamp {timestamp}: {e}")
            return None
    
    @classmethod
    def normalize_kalshi_market(cls, raw_market: Dict[str, Any]) -> NormalizedMarket:
        """Normalize Kalshi market data."""
        try:
            # Extract basic info
            market_id = raw_market.get('ticker', raw_market.get('id', 'unknown'))
            title = raw_market.get('title', market_id)
            
            # Determine status
            status_str = raw_market.get('status', 'unknown').lower()
            status_map = {
                'active': MarketStatus.ACTIVE,
                'closed': MarketStatus.CLOSED,
                'settled': MarketStatus.SETTLED,
                'finalized': MarketStatus.SETTLED,
                'suspended': MarketStatus.SUSPENDED
            }
            status = status_map.get(status_str, MarketStatus.UNKNOWN)
            
            # Parse timestamps
            close_time = cls.normalize_timestamp(raw_market.get('close_time'))
            created_at = cls.normalize_timestamp(raw_market.get('created_time'))
            
            # Create binary outcomes for Kalshi (always YES/NO)
            yes_price = raw_market.get('yes_price', raw_market.get('yes_bid'))
            no_price = raw_market.get('no_price', raw_market.get('no_bid'))
            
            # Convert prices from cents to decimal if needed
            if yes_price and yes_price > 1:
                yes_price = yes_price / 100
            if no_price and no_price > 1:
                no_price = no_price / 100
            
            outcomes = []
            
            # YES outcome
            yes_outcome = NormalizedOutcome(
                id=f"{market_id}_yes",
                name="YES",
                price=NormalizedPrice(
                    value=cls.normalize_decimal(yes_price or 0.5),
                    timestamp=datetime.now(timezone.utc),
                    source="kalshi"
                ) if yes_price is not None else None,
                volume=cls.normalize_decimal(raw_market.get('yes_volume', 0))
            )
            outcomes.append(yes_outcome)
            
            # NO outcome
            no_outcome = NormalizedOutcome(
                id=f"{market_id}_no",
                name="NO",
                price=NormalizedPrice(
                    value=cls.normalize_decimal(no_price or 0.5),
                    timestamp=datetime.now(timezone.utc),
                    source="kalshi"
                ) if no_price is not None else None,
                volume=cls.normalize_decimal(raw_market.get('no_volume', 0))
            )
            outcomes.append(no_outcome)
            
            # Create normalized market
            return NormalizedMarket(
                id=market_id,
                platform="kalshi",
                title=title,
                status=status,
                outcome_type=OutcomeType.BINARY,
                outcomes=outcomes,
                close_time=close_time,
                volume=cls.normalize_decimal(raw_market.get('volume', 0)),
                liquidity=cls.normalize_decimal(raw_market.get('open_interest', 0)),
                created_at=created_at,
                updated_at=datetime.now(timezone.utc),
                metadata={
                    "raw_status": raw_market.get('status'),
                    "event_ticker": raw_market.get('event_ticker'),
                    "result": raw_market.get('result')
                }
            )
            
        except Exception as e:
            logger.error(f"Failed to normalize Kalshi market: {e}")
            raise
    
    @classmethod
    def normalize_polymarket_market(cls, raw_market: Dict[str, Any]) -> NormalizedMarket:
        """Normalize Polymarket market data."""
        try:
            # Extract basic info
            market_id = raw_market.get('id', 'unknown')
            title = raw_market.get('question', raw_market.get('title', market_id))
            
            # Status is usually active for Polymarket
            status = MarketStatus.ACTIVE if raw_market.get('active', True) else MarketStatus.CLOSED
            
            # Parse timestamps
            close_time = cls.normalize_timestamp(raw_market.get('endDate', raw_market.get('end_date_iso')))
            created_at = cls.normalize_timestamp(raw_market.get('createdAt'))
            
            # Parse outcomes
            outcomes_data = raw_market.get('outcomes', [])
            if isinstance(outcomes_data, str):
                outcomes_data = json.loads(outcomes_data)
            
            # Parse token IDs and prices
            token_ids = raw_market.get('clobTokenIds', raw_market.get('clob_token_ids', []))
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
                
            outcome_prices = raw_market.get('outcomePrices', [])
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)
            
            # Create outcomes
            outcomes = []
            for i, (outcome_name, token_id) in enumerate(zip(outcomes_data, token_ids)):
                price_value = None
                if i < len(outcome_prices):
                    try:
                        price_value = float(outcome_prices[i])
                    except (ValueError, TypeError):
                        pass
                
                outcome = NormalizedOutcome(
                    id=token_id,
                    name=outcome_name,
                    price=NormalizedPrice(
                        value=cls.normalize_decimal(price_value),
                        timestamp=datetime.now(timezone.utc),
                        source="polymarket"
                    ) if price_value is not None else None
                )
                outcomes.append(outcome)
            
            # Determine outcome type
            if len(outcomes) == 2 and set(o.name.upper() for o in outcomes) == {"YES", "NO"}:
                outcome_type = OutcomeType.BINARY
            else:
                outcome_type = OutcomeType.CATEGORICAL
            
            # Create normalized market
            return NormalizedMarket(
                id=market_id,
                platform="polymarket",
                title=title,
                status=status,
                outcome_type=outcome_type,
                outcomes=outcomes,
                close_time=close_time,
                volume=cls.normalize_decimal(raw_market.get('volume', 0)),
                liquidity=cls.normalize_decimal(raw_market.get('liquidity', 0)),
                created_at=created_at,
                updated_at=datetime.now(timezone.utc),
                metadata={
                    "slug": raw_market.get('slug'),
                    "tags": raw_market.get('tags', []),
                    "group_id": raw_market.get('groupId')
                }
            )
            
        except Exception as e:
            logger.error(f"Failed to normalize Polymarket market: {e}")
            raise
    
    @classmethod
    def normalize_orderbook(cls, raw_orderbook: Dict[str, Any], 
                           platform: str) -> NormalizedOrderBook:
        """Normalize order book data from either platform."""
        try:
            bids = []
            asks = []
            
            if platform == "kalshi":
                # Kalshi format: yes_bids, yes_asks, no_bids, no_asks
                for bid in raw_orderbook.get('yes_bids', []):
                    price = bid.get('price', 0)
                    if price > 1:  # Convert from cents
                        price = price / 100
                    bids.append(NormalizedOrderLevel(
                        price=cls.normalize_decimal(price),
                        size=cls.normalize_decimal(bid.get('size', 0)),
                        count=bid.get('count')
                    ))
                
                for ask in raw_orderbook.get('yes_asks', []):
                    price = ask.get('price', 0)
                    if price > 1:  # Convert from cents
                        price = price / 100
                    asks.append(NormalizedOrderLevel(
                        price=cls.normalize_decimal(price),
                        size=cls.normalize_decimal(ask.get('size', 0)),
                        count=ask.get('count')
                    ))
                    
            elif platform == "polymarket":
                # Polymarket format: bids, asks arrays
                for bid in raw_orderbook.get('bids', []):
                    bids.append(NormalizedOrderLevel(
                        price=cls.normalize_decimal(bid.get('price', 0)),
                        size=cls.normalize_decimal(bid.get('size', 0))
                    ))
                
                for ask in raw_orderbook.get('asks', []):
                    asks.append(NormalizedOrderLevel(
                        price=cls.normalize_decimal(ask.get('price', 0)),
                        size=cls.normalize_decimal(ask.get('size', 0))
                    ))
            
            # Sort bids descending, asks ascending
            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)
            
            return NormalizedOrderBook(
                bids=bids,
                asks=asks,
                timestamp=cls.normalize_timestamp(raw_orderbook.get('timestamp')) or datetime.now(timezone.utc),
                sequence=raw_orderbook.get('sequence', raw_orderbook.get('seq'))
            )
            
        except Exception as e:
            logger.error(f"Failed to normalize orderbook: {e}")
            raise
    
    @classmethod
    def normalize_price_update(cls, raw_update: Dict[str, Any], 
                              platform: str) -> Dict[str, NormalizedPrice]:
        """Normalize real-time price updates."""
        try:
            prices = {}
            
            if platform == "kalshi":
                # Extract YES price
                yes_bid = raw_update.get('yes_bid')
                if yes_bid is not None:
                    if yes_bid > 1:  # Convert from cents
                        yes_bid = yes_bid / 100
                    prices['yes'] = NormalizedPrice(
                        value=cls.normalize_decimal(yes_bid),
                        timestamp=datetime.now(timezone.utc),
                        source="kalshi_stream"
                    )
                
                # Extract NO price (can be derived from YES)
                if yes_bid is not None:
                    prices['no'] = NormalizedPrice(
                        value=cls.normalize_decimal(1 - yes_bid),
                        timestamp=datetime.now(timezone.utc),
                        source="kalshi_stream"
                    )
                    
            elif platform == "polymarket":
                # Polymarket sends individual outcome prices
                price = raw_update.get('price')
                outcome = raw_update.get('outcome', 'unknown')
                
                if price is not None:
                    prices[outcome.lower()] = NormalizedPrice(
                        value=cls.normalize_decimal(price),
                        timestamp=cls.normalize_timestamp(raw_update.get('timestamp')) or datetime.now(timezone.utc),
                        source="polymarket_stream"
                    )
            
            return prices
            
        except Exception as e:
            logger.error(f"Failed to normalize price update: {e}")
            raise

class MarketMatcher:
    """Enhanced market matching with normalized data."""
    
    @staticmethod
    def calculate_similarity(market1: NormalizedMarket, market2: NormalizedMarket) -> float:
        """
        Calculate similarity between two normalized markets.
        
        Returns:
            Similarity score between 0 and 1
        """
        # Can't match markets from same platform
        if market1.platform == market2.platform:
            return 0.0
        
        # Basic title similarity (you can enhance this with better NLP)
        title_similarity = MarketMatcher._title_similarity(market1.title, market2.title)
        
        # Outcome type must match
        if market1.outcome_type != market2.outcome_type:
            return title_similarity * 0.5  # Penalize but don't eliminate
        
        # Time proximity bonus
        time_bonus = 0.0
        if market1.close_time and market2.close_time:
            time_diff = abs((market1.close_time - market2.close_time).total_seconds())
            if time_diff < 3600:  # Within 1 hour
                time_bonus = 0.1
            elif time_diff < 86400:  # Within 1 day
                time_bonus = 0.05
        
        # Volume/liquidity bonus (active markets)
        activity_bonus = 0.0
        if market1.volume and market2.volume:
            min_volume = min(float(market1.volume), float(market2.volume))
            if min_volume > 10000:
                activity_bonus = 0.05
        
        # Combine scores
        total_score = title_similarity + time_bonus + activity_bonus
        return min(total_score, 1.0)
    
    @staticmethod
    def _title_similarity(title1: str, title2: str) -> float:
        """Calculate title similarity using simple word overlap."""
        # Convert to lowercase and split
        words1 = set(title1.lower().split())
        words2 = set(title2.lower().split())
        
        # Remove common words
        stop_words = {'the', 'will', 'be', 'to', 'of', 'and', 'a', 'in', 'is', 'it', 'for', 'on', 'at'}
        words1 = words1 - stop_words
        words2 = words2 - stop_words
        
        if not words1 or not words2:
            return 0.0
        
        # Calculate Jaccard similarity
        intersection = len(words1 & words2)
        union = len(words1 | words2)
        
        return intersection / union if union > 0 else 0.0