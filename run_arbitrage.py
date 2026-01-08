#!/usr/bin/env python3
"""
Comprehensive Arbitrage Scanner for Prediction Markets

Scans for ALL types of profitable arbitrage opportunities:
1. Intra-market (YES + NO < $1)
2. Multi-outcome (all outcomes < $1)
3. Cross-platform (Kalshi vs Polymarket)

Based on strategies that have extracted $40M+ from prediction markets.
"""

import asyncio
import argparse
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient
from kalshi_arbitrage.arbitrage_engine import (
    ArbitrageEngine, RealTimeArbitrageScanner, ArbitrageOpportunity, ArbitrageType
)

# Setup logging
Config.setup_logging()
logger = logging.getLogger(__name__)


class ArbitrageScanner:
    """Main arbitrage scanning system."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or Config.ARBITRAGE_CONFIG
        self.kalshi_client = KalshiClient()
        self.polymarket_client = PolymarketClient()
        self.engine = ArbitrageEngine(self.config)
        self.scanner = RealTimeArbitrageScanner(self.engine)
        self.running = False
        self.scan_count = 0
        self.total_opportunities = 0
        self.best_opportunity: Optional[ArbitrageOpportunity] = None
        self.all_opportunities = []

    async def initialize(self) -> bool:
        """Initialize API clients."""
        logger.info("=" * 60)
        logger.info("COMPREHENSIVE ARBITRAGE SCANNER")
        logger.info("Strategies: Intra-market, Multi-outcome, Cross-platform")
        logger.info("=" * 60)

        try:
            # Initialize clients (with fallback for network issues)
            kalshi_ok = False
            poly_ok = False

            try:
                kalshi_ok = await self.kalshi_client.initialize()
                logger.info(f"Kalshi: {'✓' if kalshi_ok else '✗'} "
                           f"({len(self.kalshi_client.markets_cache)} markets)")
            except Exception as e:
                logger.warning(f"Kalshi initialization failed: {e}")

            try:
                poly_ok = await self.polymarket_client.initialize()
                logger.info(f"Polymarket: {'✓' if poly_ok else '✗'} "
                           f"({len(self.polymarket_client.markets_cache)} markets)")
            except Exception as e:
                logger.warning(f"Polymarket initialization failed: {e}")

            if not kalshi_ok and not poly_ok:
                logger.error("Failed to initialize any API client")
                return False

            # Update scanner with market data
            for mid, market in self.polymarket_client.markets_cache.items():
                self.scanner.update_market('polymarket', mid, market)
            for mid, market in self.kalshi_client.markets_cache.items():
                self.scanner.update_market('kalshi', mid, market)

            # Update scanner with price data
            for mid, prices in self.polymarket_client.price_cache.items():
                self.scanner.update_prices('polymarket', mid, prices)
            for mid, prices in self.kalshi_client.price_cache.items():
                self.scanner.update_prices('kalshi', mid, prices)

            logger.info("Scanner initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Initialization error: {e}")
            return False

    async def run_single_scan(self) -> list:
        """Run a single comprehensive scan."""
        logger.info("-" * 60)
        logger.info(f"Starting scan #{self.scan_count + 1}")
        start_time = datetime.now(timezone.utc)

        # Run full scan
        opportunities = await self.engine.scan_all(
            self.polymarket_client.markets_cache,
            self.polymarket_client.price_cache,
            self.kalshi_client.markets_cache,
            self.kalshi_client.price_cache
        )

        scan_time = (datetime.now(timezone.utc) - start_time).total_seconds()
        self.scan_count += 1
        self.total_opportunities += len(opportunities)
        self.all_opportunities.extend(opportunities)

        # Track best opportunity
        if opportunities:
            best = max(opportunities, key=lambda x: x.profit_percentage)
            if not self.best_opportunity or best.profit_percentage > self.best_opportunity.profit_percentage:
                self.best_opportunity = best

        # Print results
        self._print_scan_results(opportunities, scan_time)

        return opportunities

    def _print_scan_results(self, opportunities: list, scan_time: float):
        """Print formatted scan results."""
        print("\n" + "=" * 60)
        print(f"SCAN RESULTS | {datetime.now().strftime('%H:%M:%S')}")
        print(f"Scan time: {scan_time:.2f}s | Markets scanned: "
              f"Poly={len(self.polymarket_client.markets_cache)}, "
              f"Kalshi={len(self.kalshi_client.markets_cache)}")
        print("=" * 60)

        if not opportunities:
            print("\n❌ No arbitrage opportunities found above threshold")
            print(f"   Threshold: {self.config.get('min_profit_pct', 0.005) * 100:.2f}%")
            print("\n   This is normal - markets are mostly efficient.")
            print("   Opportunities appear during:")
            print("   - Breaking news events")
            print("   - Market opens/closes")
            print("   - High volatility periods")
        else:
            print(f"\n🎯 FOUND {len(opportunities)} OPPORTUNITIES!\n")

            # Group by type
            by_type = {}
            for opp in opportunities:
                t = opp.arb_type.value
                if t not in by_type:
                    by_type[t] = []
                by_type[t].append(opp)

            for arb_type, opps in by_type.items():
                print(f"\n--- {arb_type.upper()} ({len(opps)}) ---")
                for opp in sorted(opps, key=lambda x: x.profit_percentage, reverse=True)[:5]:
                    print(f"\n  📈 {opp.market_title[:50]}...")
                    print(f"     Platform: {opp.platform}")
                    print(f"     Cost: ${float(opp.total_cost):.4f} → Payout: ${float(opp.guaranteed_payout):.2f}")
                    print(f"     Profit: ${float(opp.profit):.4f} ({float(opp.profit_percentage):.2f}%)")
                    print(f"     Risk: {opp.execution_risk}")

                    # Show positions
                    print("     Positions:")
                    for pos in opp.positions[:4]:
                        platform = pos.get('platform', opp.platform)
                        print(f"       - {pos['action']} {pos['side']} @ ${pos['price']:.4f} ({platform})")

        # Stats
        stats = self.engine.get_stats()
        print("\n" + "-" * 60)
        print("ENGINE STATS:")
        print(f"  Scans: {stats['scans_completed']} | Total opps: {stats['opportunities_found']}")
        print(f"  By type: {json.dumps(stats['by_type'])}")
        print("-" * 60 + "\n")

    async def run_continuous(self, interval: int = 30):
        """Run continuous scanning."""
        self.running = True
        logger.info(f"Starting continuous scan (interval: {interval}s)")
        logger.info("Press Ctrl+C to stop\n")

        while self.running:
            try:
                await self.run_single_scan()

                # Save opportunities to file
                if self.all_opportunities:
                    self._save_opportunities()

                # Wait for next scan
                if self.running:
                    logger.info(f"Next scan in {interval} seconds...")
                    await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scan error: {e}")
                await asyncio.sleep(5)

        self._print_final_summary()

    def _save_opportunities(self):
        """Save opportunities to JSON file."""
        try:
            data = {
                'scan_time': datetime.now(timezone.utc).isoformat(),
                'scan_count': self.scan_count,
                'total_opportunities': len(self.all_opportunities),
                'opportunities': [opp.to_dict() for opp in self.all_opportunities[-100:]]  # Keep last 100
            }
            with open('market_data/opportunities.json', 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save opportunities: {e}")

    def _print_final_summary(self):
        """Print final summary when stopping."""
        print("\n" + "=" * 60)
        print("FINAL SUMMARY")
        print("=" * 60)
        print(f"Total scans: {self.scan_count}")
        print(f"Total opportunities found: {self.total_opportunities}")

        if self.best_opportunity:
            print(f"\nBest opportunity found:")
            print(f"  Market: {self.best_opportunity.market_title[:50]}")
            print(f"  Type: {self.best_opportunity.arb_type.value}")
            print(f"  Profit: {float(self.best_opportunity.profit_percentage):.2f}%")

        stats = self.engine.get_stats()
        print(f"\nOpportunities by type:")
        for t, count in stats['by_type'].items():
            if count > 0:
                print(f"  {t}: {count}")

        print("=" * 60)

    def stop(self):
        """Stop the scanner."""
        self.running = False


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Comprehensive Arbitrage Scanner')
    parser.add_argument('--mode', choices=['single', 'continuous'], default='continuous',
                       help='Scan mode (default: continuous)')
    parser.add_argument('--interval', type=int, default=30,
                       help='Scan interval in seconds (default: 30)')
    parser.add_argument('--min-profit', type=float, default=0.5,
                       help='Minimum profit percentage (default: 0.5)')
    args = parser.parse_args()

    # Configure
    config = Config.ARBITRAGE_CONFIG.copy()
    config['min_profit_pct'] = args.min_profit / 100

    # Create scanner
    scanner = ArbitrageScanner(config)

    # Handle shutdown
    def signal_handler(sig, frame):
        print("\n\nShutting down...")
        scanner.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Initialize
    if not await scanner.initialize():
        logger.error("Failed to initialize scanner")
        sys.exit(1)

    # Run
    if args.mode == 'single':
        await scanner.run_single_scan()
    else:
        await scanner.run_continuous(args.interval)


if __name__ == '__main__':
    asyncio.run(main())
