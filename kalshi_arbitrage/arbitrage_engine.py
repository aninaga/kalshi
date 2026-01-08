"""
Comprehensive Arbitrage Engine for Prediction Markets

Implements multiple arbitrage strategies:
1. Intra-market arbitrage (YES + NO < $1 on same market)
2. Multi-outcome arbitrage (all outcomes sum < $1)
3. Cross-platform arbitrage (same market different prices across platforms)
4. Dutch-book detection (probability inconsistencies)

Based on real-world strategies that have extracted $40M+ from prediction markets.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json

logger = logging.getLogger(__name__)


class ArbitrageType(Enum):
    """Types of arbitrage opportunities."""
    INTRA_MARKET = "intra_market"          # YES + NO < $1 on same market
    MULTI_OUTCOME = "multi_outcome"         # All outcomes sum < $1
    CROSS_PLATFORM = "cross_platform"       # Same market, different platforms
    DUTCH_BOOK = "dutch_book"               # Probability inconsistencies


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""
    opportunity_id: str
    arb_type: ArbitrageType
    platform: str                           # Primary platform or "cross-platform"
    market_id: str
    market_title: str

    # Financial details
    total_cost: Decimal                     # Cost to buy all positions
    guaranteed_payout: Decimal              # Guaranteed return
    profit: Decimal                         # Guaranteed profit
    profit_percentage: Decimal              # ROI percentage

    # Position details
    positions: List[Dict[str, Any]]         # What to buy

    # Risk assessment
    liquidity_score: Decimal                # 0-1, how much liquidity available
    execution_risk: str                     # low, medium, high
    max_profitable_size: Decimal            # Max $ before slippage kills profit

    # Metadata
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'opportunity_id': self.opportunity_id,
            'arb_type': self.arb_type.value,
            'platform': self.platform,
            'market_id': self.market_id,
            'market_title': self.market_title,
            'total_cost': float(self.total_cost),
            'guaranteed_payout': float(self.guaranteed_payout),
            'profit': float(self.profit),
            'profit_percentage': float(self.profit_percentage),
            'positions': self.positions,
            'liquidity_score': float(self.liquidity_score),
            'execution_risk': self.execution_risk,
            'max_profitable_size': float(self.max_profitable_size),
            'detected_at': self.detected_at.isoformat(),
        }


class ArbitrageEngine:
    """
    High-performance arbitrage detection engine.

    Scans for multiple types of arbitrage opportunities across prediction markets.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

        # Minimum profit thresholds (after fees)
        self.min_profit_pct = Decimal(str(self.config.get('min_profit_pct', '0.005')))  # 0.5% minimum
        self.min_profit_abs = Decimal(str(self.config.get('min_profit_abs', '0.01')))   # $0.01 minimum

        # Fee structures
        self.fees = {
            'polymarket': {
                'taker_fee': Decimal('0.00'),       # 0% base (but gas costs)
                'gas_estimate': Decimal('0.005'),   # ~0.5% gas estimate
                'total': Decimal('0.005')
            },
            'kalshi': {
                'taker_fee': Decimal('0.00'),       # 0% trading fee
                'settlement_fee': Decimal('0.00'),
                'total': Decimal('0.00')
            }
        }

        # Statistics
        self.stats = {
            'scans_completed': 0,
            'opportunities_found': 0,
            'total_profit_potential': Decimal('0'),
            'by_type': {t.value: 0 for t in ArbitrageType}
        }

        logger.info("ArbitrageEngine initialized with min_profit_pct=%.2f%%",
                   float(self.min_profit_pct) * 100)

    async def scan_all(self,
                       polymarket_markets: Dict[str, Any],
                       polymarket_prices: Dict[str, Any],
                       kalshi_markets: Dict[str, Any],
                       kalshi_prices: Dict[str, Any]) -> List[ArbitrageOpportunity]:
        """
        Run all arbitrage scans and return combined opportunities.
        """
        opportunities = []

        # Run scans in parallel
        results = await asyncio.gather(
            self.scan_intra_market_polymarket(polymarket_markets, polymarket_prices),
            self.scan_multi_outcome_polymarket(polymarket_markets, polymarket_prices),
            self.scan_intra_market_kalshi(kalshi_markets, kalshi_prices),
            self.scan_cross_platform(polymarket_markets, polymarket_prices,
                                    kalshi_markets, kalshi_prices),
            return_exceptions=True
        )

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Scan {i} failed: {result}")
            elif result:
                opportunities.extend(result)

        # Sort by profit percentage
        opportunities.sort(key=lambda x: x.profit_percentage, reverse=True)

        self.stats['scans_completed'] += 1
        self.stats['opportunities_found'] += len(opportunities)

        return opportunities

    async def scan_intra_market_polymarket(self,
                                           markets: Dict[str, Any],
                                           prices: Dict[str, Any]) -> List[ArbitrageOpportunity]:
        """
        Scan for intra-market arbitrage on Polymarket.

        Strategy: Find markets where YES + NO < $1.00
        Buy both sides, guaranteed $1 payout, profit = $1 - cost
        """
        opportunities = []

        for market_id, market in markets.items():
            try:
                outcomes = market.get('outcomes', [])
                outcome_prices = market.get('outcome_prices', [])
                clob_token_ids = market.get('clob_token_ids', [])

                # Need exactly 2 outcomes for binary market
                if len(outcomes) != 2 or len(outcome_prices) != 2:
                    continue

                # Get prices - try cache first, then market data
                market_prices = prices.get(market_id, {})

                yes_price = None
                no_price = None

                # Try to get prices from the price cache
                for token_id, price_data in market_prices.items():
                    outcome = price_data.get('outcome', '').lower()
                    price = price_data.get('buy_price') or price_data.get('mid_price')
                    if price is not None:
                        if outcome in ['yes', 'y', '1']:
                            yes_price = Decimal(str(price))
                        elif outcome in ['no', 'n', '0']:
                            no_price = Decimal(str(price))

                # Fallback to outcome_prices from market data
                if yes_price is None or no_price is None:
                    try:
                        prices_list = [Decimal(str(p)) for p in outcome_prices if p]
                        if len(prices_list) >= 2:
                            # Assume first is YES, second is NO
                            yes_price = prices_list[0]
                            no_price = prices_list[1]
                    except (ValueError, TypeError):
                        continue

                if yes_price is None or no_price is None:
                    continue

                # Calculate arbitrage
                total_cost = yes_price + no_price
                guaranteed_payout = Decimal('1.00')

                # Apply fees
                fee = self.fees['polymarket']['total']
                effective_cost = total_cost * (1 + fee)

                if effective_cost >= guaranteed_payout:
                    continue  # No profit

                profit = guaranteed_payout - effective_cost
                profit_pct = profit / effective_cost

                # Check minimum thresholds
                if profit_pct < self.min_profit_pct:
                    continue
                if profit < self.min_profit_abs:
                    continue

                # Found opportunity!
                opp = ArbitrageOpportunity(
                    opportunity_id=f"poly_intra_{market_id}_{datetime.now().strftime('%H%M%S')}",
                    arb_type=ArbitrageType.INTRA_MARKET,
                    platform="polymarket",
                    market_id=market_id,
                    market_title=market.get('title', 'Unknown'),
                    total_cost=effective_cost,
                    guaranteed_payout=guaranteed_payout,
                    profit=profit,
                    profit_percentage=profit_pct * 100,
                    positions=[
                        {'side': 'YES', 'price': float(yes_price), 'action': 'BUY'},
                        {'side': 'NO', 'price': float(no_price), 'action': 'BUY'}
                    ],
                    liquidity_score=Decimal('0.8'),  # TODO: calculate from orderbook
                    execution_risk='low',
                    max_profitable_size=Decimal('1000')  # TODO: calculate from depth
                )

                opportunities.append(opp)
                self.stats['by_type'][ArbitrageType.INTRA_MARKET.value] += 1

                logger.info(f"🎯 Intra-market arb found: {market.get('title', 'Unknown')[:50]} "
                           f"| YES={yes_price:.3f} + NO={no_price:.3f} = {total_cost:.3f} "
                           f"| Profit: {profit_pct*100:.2f}%")

            except Exception as e:
                logger.debug(f"Error processing market {market_id}: {e}")
                continue

        return opportunities

    async def scan_multi_outcome_polymarket(self,
                                            markets: Dict[str, Any],
                                            prices: Dict[str, Any]) -> List[ArbitrageOpportunity]:
        """
        Scan for multi-outcome arbitrage on Polymarket.

        Strategy: Find markets where sum of all outcome prices < $1.00
        Buy one of each outcome, guaranteed $1 payout, profit = $1 - total cost
        """
        opportunities = []

        for market_id, market in markets.items():
            try:
                outcomes = market.get('outcomes', [])
                outcome_prices = market.get('outcome_prices', [])

                # Need 3+ outcomes for multi-outcome (binary handled above)
                if len(outcomes) < 3:
                    continue

                # Get prices for all outcomes
                all_prices = []
                market_prices = prices.get(market_id, {})

                # Try to get from price cache
                if market_prices:
                    for token_id, price_data in market_prices.items():
                        price = price_data.get('buy_price') or price_data.get('mid_price')
                        if price is not None:
                            all_prices.append(Decimal(str(price)))

                # Fallback to outcome_prices
                if len(all_prices) != len(outcomes):
                    all_prices = []
                    try:
                        for p in outcome_prices:
                            if p is not None:
                                all_prices.append(Decimal(str(p)))
                    except (ValueError, TypeError):
                        continue

                if len(all_prices) != len(outcomes):
                    continue

                # Calculate arbitrage
                total_cost = sum(all_prices)
                guaranteed_payout = Decimal('1.00')

                # Apply fees (per outcome purchase)
                fee = self.fees['polymarket']['total']
                effective_cost = total_cost * (1 + fee)

                if effective_cost >= guaranteed_payout:
                    continue

                profit = guaranteed_payout - effective_cost
                profit_pct = profit / effective_cost

                if profit_pct < self.min_profit_pct:
                    continue
                if profit < self.min_profit_abs:
                    continue

                # Found opportunity!
                positions = [
                    {'side': outcomes[i], 'price': float(all_prices[i]), 'action': 'BUY'}
                    for i in range(len(outcomes))
                ]

                opp = ArbitrageOpportunity(
                    opportunity_id=f"poly_multi_{market_id}_{datetime.now().strftime('%H%M%S')}",
                    arb_type=ArbitrageType.MULTI_OUTCOME,
                    platform="polymarket",
                    market_id=market_id,
                    market_title=market.get('title', 'Unknown'),
                    total_cost=effective_cost,
                    guaranteed_payout=guaranteed_payout,
                    profit=profit,
                    profit_percentage=profit_pct * 100,
                    positions=positions,
                    liquidity_score=Decimal('0.7'),
                    execution_risk='medium',  # More legs = more risk
                    max_profitable_size=Decimal('500')
                )

                opportunities.append(opp)
                self.stats['by_type'][ArbitrageType.MULTI_OUTCOME.value] += 1

                logger.info(f"🎯 Multi-outcome arb found: {market.get('title', 'Unknown')[:50]} "
                           f"| {len(outcomes)} outcomes, total={total_cost:.3f} "
                           f"| Profit: {profit_pct*100:.2f}%")

            except Exception as e:
                logger.debug(f"Error processing multi-outcome market {market_id}: {e}")
                continue

        return opportunities

    async def scan_intra_market_kalshi(self,
                                       markets: Dict[str, Any],
                                       prices: Dict[str, Any]) -> List[ArbitrageOpportunity]:
        """
        Scan for intra-market arbitrage on Kalshi.

        Strategy: Find markets where YES_ASK + NO_ASK < $1.00
        """
        opportunities = []

        for market_id, market in markets.items():
            try:
                # Get prices from cache
                market_prices = prices.get(market_id) or prices.get(market.get('ticker'))
                if not market_prices:
                    continue

                # Kalshi has yes/no structure
                yes_ask = market_prices.get('yes_ask')
                yes_bid = market_prices.get('yes_bid')

                if yes_ask is None:
                    continue

                yes_ask = Decimal(str(yes_ask))

                # NO ask = 1 - YES bid (or approximate)
                if yes_bid is not None:
                    no_ask = Decimal('1.0') - Decimal(str(yes_bid))
                else:
                    # Approximate
                    no_ask = Decimal('1.0') - yes_ask + Decimal('0.02')  # Add spread estimate

                # Calculate arbitrage
                total_cost = yes_ask + no_ask
                guaranteed_payout = Decimal('1.00')

                # Kalshi has no fees
                effective_cost = total_cost

                if effective_cost >= guaranteed_payout:
                    continue

                profit = guaranteed_payout - effective_cost
                profit_pct = profit / effective_cost

                if profit_pct < self.min_profit_pct:
                    continue

                opp = ArbitrageOpportunity(
                    opportunity_id=f"kalshi_intra_{market_id}_{datetime.now().strftime('%H%M%S')}",
                    arb_type=ArbitrageType.INTRA_MARKET,
                    platform="kalshi",
                    market_id=market_id,
                    market_title=market.get('title', market_id),
                    total_cost=effective_cost,
                    guaranteed_payout=guaranteed_payout,
                    profit=profit,
                    profit_percentage=profit_pct * 100,
                    positions=[
                        {'side': 'YES', 'price': float(yes_ask), 'action': 'BUY'},
                        {'side': 'NO', 'price': float(no_ask), 'action': 'BUY'}
                    ],
                    liquidity_score=Decimal('0.8'),
                    execution_risk='low',
                    max_profitable_size=Decimal('1000')
                )

                opportunities.append(opp)
                self.stats['by_type'][ArbitrageType.INTRA_MARKET.value] += 1

                logger.info(f"🎯 Kalshi intra-market arb: {market.get('title', market_id)[:50]} "
                           f"| YES={yes_ask:.3f} + NO={no_ask:.3f} = {total_cost:.3f} "
                           f"| Profit: {profit_pct*100:.2f}%")

            except Exception as e:
                logger.debug(f"Error processing Kalshi market {market_id}: {e}")
                continue

        return opportunities

    async def scan_cross_platform(self,
                                  poly_markets: Dict[str, Any],
                                  poly_prices: Dict[str, Any],
                                  kalshi_markets: Dict[str, Any],
                                  kalshi_prices: Dict[str, Any]) -> List[ArbitrageOpportunity]:
        """
        Scan for cross-platform arbitrage between Polymarket and Kalshi.

        Strategy: Find same market on both platforms with price difference
        Buy YES on cheaper platform, buy NO on other (or use bid/ask spreads)
        """
        opportunities = []

        # Import similarity function
        from .utils import get_similarity_score

        # Build index for faster matching
        poly_by_title = {}
        for mid, market in poly_markets.items():
            title = market.get('title', '').lower().strip()
            if title:
                poly_by_title[title] = (mid, market)

        for kalshi_id, kalshi_market in kalshi_markets.items():
            try:
                kalshi_title = kalshi_market.get('title', '').lower().strip()
                if not kalshi_title:
                    continue

                # Find matching Polymarket market
                best_match = None
                best_score = 0

                for poly_title, (poly_id, poly_market) in poly_by_title.items():
                    score = get_similarity_score(kalshi_title, poly_title)
                    if score > best_score and score >= 0.70:  # Higher threshold for cross-platform
                        best_score = score
                        best_match = (poly_id, poly_market)

                if not best_match:
                    continue

                poly_id, poly_market = best_match

                # Get prices from both platforms
                kalshi_price_data = kalshi_prices.get(kalshi_id) or kalshi_prices.get(kalshi_market.get('ticker'))
                poly_price_data = poly_prices.get(poly_id)

                if not kalshi_price_data or not poly_price_data:
                    continue

                # Extract YES prices
                kalshi_yes_bid = kalshi_price_data.get('yes_bid')
                kalshi_yes_ask = kalshi_price_data.get('yes_ask')

                if kalshi_yes_bid is None or kalshi_yes_ask is None:
                    continue

                kalshi_yes_bid = Decimal(str(kalshi_yes_bid))
                kalshi_yes_ask = Decimal(str(kalshi_yes_ask))

                # Get Polymarket YES price
                poly_yes_price = None
                poly_no_price = None

                for token_id, price_info in poly_price_data.items():
                    outcome = price_info.get('outcome', '').lower()
                    price = price_info.get('buy_price') or price_info.get('mid_price')
                    if price:
                        if outcome in ['yes', 'y', '1']:
                            poly_yes_price = Decimal(str(price))
                        elif outcome in ['no', 'n', '0']:
                            poly_no_price = Decimal(str(price))

                # Fallback to outcome_prices
                if poly_yes_price is None:
                    outcome_prices = poly_market.get('outcome_prices', [])
                    if len(outcome_prices) >= 2:
                        try:
                            poly_yes_price = Decimal(str(outcome_prices[0]))
                            poly_no_price = Decimal(str(outcome_prices[1]))
                        except (ValueError, TypeError):
                            continue

                if poly_yes_price is None:
                    continue

                # Check for arbitrage opportunities
                # Strategy 1: Buy Kalshi YES + Buy Polymarket NO
                if poly_no_price:
                    total_cost = kalshi_yes_ask + poly_no_price * (1 + self.fees['polymarket']['total'])
                    if total_cost < Decimal('1.00'):
                        profit = Decimal('1.00') - total_cost
                        profit_pct = profit / total_cost

                        if profit_pct >= self.min_profit_pct:
                            opp = ArbitrageOpportunity(
                                opportunity_id=f"cross_{kalshi_id}_{poly_id}_{datetime.now().strftime('%H%M%S')}",
                                arb_type=ArbitrageType.CROSS_PLATFORM,
                                platform="cross-platform",
                                market_id=f"{kalshi_id}|{poly_id}",
                                market_title=f"{kalshi_market.get('title', kalshi_id)[:40]}",
                                total_cost=total_cost,
                                guaranteed_payout=Decimal('1.00'),
                                profit=profit,
                                profit_percentage=profit_pct * 100,
                                positions=[
                                    {'platform': 'kalshi', 'side': 'YES', 'price': float(kalshi_yes_ask), 'action': 'BUY'},
                                    {'platform': 'polymarket', 'side': 'NO', 'price': float(poly_no_price), 'action': 'BUY'}
                                ],
                                liquidity_score=Decimal('0.6'),
                                execution_risk='medium',
                                max_profitable_size=Decimal('500')
                            )
                            opportunities.append(opp)
                            self.stats['by_type'][ArbitrageType.CROSS_PLATFORM.value] += 1

                            logger.info(f"🎯 Cross-platform arb: {kalshi_market.get('title', '')[:40]} "
                                       f"| Kalshi YES={kalshi_yes_ask:.3f} + Poly NO={poly_no_price:.3f} "
                                       f"| Profit: {profit_pct*100:.2f}%")

                # Strategy 2: Buy Polymarket YES + Buy Kalshi NO
                kalshi_no_ask = Decimal('1.00') - kalshi_yes_bid  # Approximate
                total_cost = poly_yes_price * (1 + self.fees['polymarket']['total']) + kalshi_no_ask

                if total_cost < Decimal('1.00'):
                    profit = Decimal('1.00') - total_cost
                    profit_pct = profit / total_cost

                    if profit_pct >= self.min_profit_pct:
                        opp = ArbitrageOpportunity(
                            opportunity_id=f"cross_{poly_id}_{kalshi_id}_{datetime.now().strftime('%H%M%S')}",
                            arb_type=ArbitrageType.CROSS_PLATFORM,
                            platform="cross-platform",
                            market_id=f"{poly_id}|{kalshi_id}",
                            market_title=f"{poly_market.get('title', poly_id)[:40]}",
                            total_cost=total_cost,
                            guaranteed_payout=Decimal('1.00'),
                            profit=profit,
                            profit_percentage=profit_pct * 100,
                            positions=[
                                {'platform': 'polymarket', 'side': 'YES', 'price': float(poly_yes_price), 'action': 'BUY'},
                                {'platform': 'kalshi', 'side': 'NO', 'price': float(kalshi_no_ask), 'action': 'BUY'}
                            ],
                            liquidity_score=Decimal('0.6'),
                            execution_risk='medium',
                            max_profitable_size=Decimal('500')
                        )
                        opportunities.append(opp)

                        logger.info(f"🎯 Cross-platform arb: {poly_market.get('title', '')[:40]} "
                                   f"| Poly YES={poly_yes_price:.3f} + Kalshi NO={kalshi_no_ask:.3f} "
                                   f"| Profit: {profit_pct*100:.2f}%")

            except Exception as e:
                logger.debug(f"Error in cross-platform scan for {kalshi_id}: {e}")
                continue

        return opportunities

    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        return {
            'scans_completed': self.stats['scans_completed'],
            'opportunities_found': self.stats['opportunities_found'],
            'total_profit_potential': float(self.stats['total_profit_potential']),
            'by_type': self.stats['by_type'],
            'config': {
                'min_profit_pct': float(self.min_profit_pct) * 100,
                'min_profit_abs': float(self.min_profit_abs)
            }
        }

    def reset_stats(self):
        """Reset statistics."""
        self.stats = {
            'scans_completed': 0,
            'opportunities_found': 0,
            'total_profit_potential': Decimal('0'),
            'by_type': {t.value: 0 for t in ArbitrageType}
        }


class RealTimeArbitrageScanner:
    """
    Real-time arbitrage scanner optimized for speed.

    Maintains in-memory price snapshots and triggers scans on price updates.
    """

    def __init__(self, engine: ArbitrageEngine):
        self.engine = engine
        self.price_snapshots = {
            'polymarket': {},
            'kalshi': {}
        }
        self.market_snapshots = {
            'polymarket': {},
            'kalshi': {}
        }
        self.callbacks = []
        self.running = False

    def on_opportunity(self, callback):
        """Register callback for when opportunity is found."""
        self.callbacks.append(callback)

    def update_prices(self, platform: str, market_id: str, prices: Dict[str, Any]):
        """Update price snapshot and trigger scan if needed."""
        self.price_snapshots[platform][market_id] = prices

    def update_market(self, platform: str, market_id: str, market: Dict[str, Any]):
        """Update market snapshot."""
        self.market_snapshots[platform][market_id] = market

    async def quick_scan(self, platform: str, market_id: str) -> List[ArbitrageOpportunity]:
        """
        Quick scan for a single market update.
        Used for real-time price updates.
        """
        opportunities = []

        if platform == 'polymarket':
            market = self.market_snapshots['polymarket'].get(market_id)
            prices = {market_id: self.price_snapshots['polymarket'].get(market_id, {})}

            if market:
                # Check intra-market
                opps = await self.engine.scan_intra_market_polymarket(
                    {market_id: market}, prices
                )
                opportunities.extend(opps)

                # Check multi-outcome if applicable
                if len(market.get('outcomes', [])) >= 3:
                    opps = await self.engine.scan_multi_outcome_polymarket(
                        {market_id: market}, prices
                    )
                    opportunities.extend(opps)

        elif platform == 'kalshi':
            market = self.market_snapshots['kalshi'].get(market_id)
            prices = {market_id: self.price_snapshots['kalshi'].get(market_id, {})}

            if market:
                opps = await self.engine.scan_intra_market_kalshi(
                    {market_id: market}, prices
                )
                opportunities.extend(opps)

        # Notify callbacks
        for opp in opportunities:
            for callback in self.callbacks:
                try:
                    await callback(opp)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

        return opportunities

    async def full_scan(self) -> List[ArbitrageOpportunity]:
        """Run full scan across all cached data."""
        return await self.engine.scan_all(
            self.market_snapshots['polymarket'],
            self.price_snapshots['polymarket'],
            self.market_snapshots['kalshi'],
            self.price_snapshots['kalshi']
        )
