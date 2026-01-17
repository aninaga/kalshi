#!/usr/bin/env python3
"""
Kalshi-Polymarket Arbitrage Analysis System
Continuously monitors both platforms for arbitrage opportunities
"""

import asyncio
import argparse
import signal
import sys
from datetime import datetime, timedelta
from kalshi_arbitrage.config import Config
from kalshi_arbitrage.market_analyzer import MarketAnalyzer
import logging

class ArbitrageAnalysisSystem:
    """Main system for continuous arbitrage analysis."""
    
    def __init__(self):
        self.analyzer = MarketAnalyzer()
        self.running = False
        self.scan_count = 0
        self.total_opportunities_found = 0
        self.start_time = None
        
    async def initialize(self):
        """Initialize the analysis system."""
        Config.setup_logging()
        self.logger = logging.getLogger(__name__)
        
        self.logger.info("=" * 60)
        self.logger.info("KALSHI-POLYMARKET ARBITRAGE ANALYSIS SYSTEM")
        self.logger.info("WebSocket-Only Real-Time Data Mode")
        self.logger.info("=" * 60)
        self.logger.info(f"Scan Interval: {Config.SCAN_INTERVAL_SECONDS} seconds")
        self.logger.info(f"Min Profit Threshold: {Config.MIN_PROFIT_THRESHOLD}")
        self.logger.info(f"Similarity Threshold: {Config.SIMILARITY_THRESHOLD}")
        self.logger.info("=" * 60)
        
        await self.analyzer.initialize()
        
    async def run_continuous_analysis(self):
        """Run continuous analysis with 30-second intervals."""
        self.running = True
        self.start_time = datetime.now()
        
        self.logger.info("Starting continuous arbitrage analysis...")
        
        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        try:
            while self.running:
                cycle_start = datetime.now()
                
                # Run full market scan
                try:
                    scan_report = await self.analyzer.run_full_scan()
                    self.scan_count += 1
                    opportunities_found = scan_report['arbitrage_opportunities']
                    self.total_opportunities_found += opportunities_found
                    
                    # Print summary
                    self._print_scan_summary(scan_report, cycle_start)
                    
                    # Print top opportunities if found
                    if scan_report['top_opportunities']:
                        self._print_top_opportunities(scan_report['top_opportunities'])
                    
                except Exception as e:
                    self.logger.error(f"Error during scan cycle: {e}")
                
                # Calculate sleep time to maintain 30-second intervals
                cycle_duration = (datetime.now() - cycle_start).total_seconds()
                sleep_time = max(0, Config.SCAN_INTERVAL_SECONDS - cycle_duration)
                
                if sleep_time > 0:
                    self.logger.info(f"Next scan in {sleep_time:.1f} seconds...")
                    await asyncio.sleep(sleep_time)
                else:
                    self.logger.warning(f"Scan took {cycle_duration:.1f}s - longer than {Config.SCAN_INTERVAL_SECONDS}s interval")
                    
        except Exception as e:
            self.logger.error(f"Fatal error in continuous analysis: {e}")
        finally:
            await self._cleanup()
    
    async def run_single_scan(self):
        """Run a single comprehensive scan and exit."""
        self.logger.info("Running single comprehensive market scan...")
        
        try:
            scan_report = await self.analyzer.run_full_scan()
            self._print_detailed_report(scan_report)
            
        except Exception as e:
            self.logger.error(f"Error during single scan: {e}")
            sys.exit(1)
        finally:
            if hasattr(self, 'analyzer') and self.analyzer:
                await self.analyzer.shutdown()
    
    def _print_scan_summary(self, scan_report: dict, cycle_start: datetime):
        """Print a concise summary of the scan results."""
        duration = scan_report['duration_seconds']
        kalshi_count = scan_report['kalshi_markets_count']
        poly_count = scan_report['polymarket_markets_count']
        matches = scan_report['potential_matches']
        opportunities = scan_report['arbitrage_opportunities']
        
        uptime = datetime.now() - self.start_time
        
        print(f"\n📊 SCAN #{self.scan_count} COMPLETED ({datetime.now().strftime('%H:%M:%S')})")
        print(f"Duration: {duration:.1f}s | Uptime: {uptime}")
        print(f"Markets: Kalshi({kalshi_count}) + Polymarket({poly_count}) = {kalshi_count + poly_count}")
        print(f"Matches: {matches} | Opportunities: {opportunities}")
        
        if opportunities > 0:
            print(f"🚨 {opportunities} ARBITRAGE OPPORTUNITIES DETECTED!")
        else:
            print("✅ No arbitrage opportunities found")
    
    def _print_top_opportunities(self, opportunities: list, max_display: int = 5):
        """Print top arbitrage opportunities."""
        print(f"\n🎯 TOP {min(len(opportunities), max_display)} OPPORTUNITIES:")
        print("-" * 90)
        
        for i, opp in enumerate(opportunities[:max_display], 1):
            kalshi_title = opp['match_data']['kalshi_market']['title'][:50]
            poly_title = opp['match_data']['polymarket_market']['title'][:50]
            profit_margin = opp['profit_margin']
            strategy = opp['strategy']
            similarity = opp['match_data']['similarity_score']
            
            print(f"{i}. {strategy}")
            print(f"   Profit Margin: {profit_margin:.2%} | Similarity: {similarity:.1%}")
            print(f"   Kalshi: {kalshi_title}")
            print(f"   Polymarket: {poly_title}")
            print(f"   Buy: ${opp['buy_price']:.3f} → Sell: ${opp['sell_price']:.3f}")
            
            # Show enhanced profit details if available
            if 'total_profit' in opp:
                total_profit = opp['total_profit']
                max_volume = opp.get('max_tradeable_volume', 0)
                num_trades = opp.get('num_trades', 1)
                
                print(f"   💰 Total Profit: ${total_profit:.2f} | Max Volume: {max_volume:.0f} shares")
                print(f"   📊 {num_trades} profitable price level(s) | Slippage included")
            
            print()
    
    def _print_detailed_report(self, scan_report: dict):
        """Print detailed report for single scan mode."""
        print(f"\n{'='*80}")
        print(f"COMPREHENSIVE ARBITRAGE ANALYSIS REPORT")
        print(f"{'='*80}")
        print(f"Scan Time: {scan_report['timestamp']}")
        print(f"Duration: {scan_report['duration_seconds']:.2f} seconds")
        print(f"Kalshi Markets: {scan_report['kalshi_markets_count']:,}")
        print(f"Polymarket Markets: {scan_report['polymarket_markets_count']:,}")
        print(f"Potential Matches: {scan_report['potential_matches']:,}")
        print(f"Arbitrage Opportunities: {scan_report['arbitrage_opportunities']:,}")
        
        # Show completeness information
        if 'completeness_info' in scan_report:
            completeness = scan_report['completeness_info']
            print(f"Completeness Level: {completeness['level']} ({completeness['estimated_completeness']:.1%})")
            stats = completeness['stats']
            if stats['truncated_matches'] > 0 or stats['truncated_trades'] > 0:
                print(f"Truncated: {stats['truncated_matches']} matches, {stats['truncated_trades']} trades")
            if stats['data_staleness_warnings'] > 0:
                cache_total = stats['cache_hits'] + stats['cache_misses']
                staleness_rate = stats['data_staleness_warnings'] / cache_total if cache_total > 0 else 0
                print(f"Data Staleness: {stats['data_staleness_warnings']} warnings ({staleness_rate:.1%} of requests)")
        
        print(f"{'='*80}")
        
        if scan_report['top_opportunities']:
            self._print_top_opportunities(scan_report['top_opportunities'], max_display=10)
        else:
            print("\n📈 MARKET STATUS: No arbitrage opportunities detected")
            print("This could indicate:")
            print("• Markets are efficiently priced")
            print("• Profit margins below threshold")
            print("• Limited market overlap between platforms")
        
        print(f"\n💾 Full results saved to market_data/ directory")
        print(f"{'='*80}")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False
    
    async def _cleanup(self):
        """Cleanup operations before shutdown."""
        if hasattr(self, 'analyzer') and self.analyzer:
            await self.analyzer.shutdown()
        if self.start_time:
            total_runtime = datetime.now() - self.start_time
            avg_opportunities = self.total_opportunities_found / self.scan_count if self.scan_count > 0 else 0
            
            self.logger.info("=" * 60)
            self.logger.info("SHUTDOWN SUMMARY")
            self.logger.info("=" * 60)
            self.logger.info(f"Total Runtime: {total_runtime}")
            self.logger.info(f"Scans Completed: {self.scan_count}")
            self.logger.info(f"Total Opportunities Found: {self.total_opportunities_found}")
            self.logger.info(f"Average Opportunities per Scan: {avg_opportunities:.1f}")
            self.logger.info("=" * 60)

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Kalshi-Polymarket Arbitrage Analysis System")
    parser.add_argument(
        "--mode", 
        choices=["single", "continuous"], 
        default="continuous",
        help="Analysis mode: single scan or continuous monitoring"
    )
    parser.add_argument(
        "--interval", 
        type=int, 
        default=Config.SCAN_INTERVAL_SECONDS,
        help=f"Scan interval in seconds (default: {Config.SCAN_INTERVAL_SECONDS})"
    )
    parser.add_argument(
        "--threshold", 
        type=float, 
        default=Config.MIN_PROFIT_THRESHOLD,
        help=f"Minimum profit threshold (default: {Config.MIN_PROFIT_THRESHOLD})"
    )
    parser.add_argument(
        "--similarity", 
        type=float, 
        default=Config.SIMILARITY_THRESHOLD,
        help=f"Market similarity threshold (default: {Config.SIMILARITY_THRESHOLD})"
    )
    parser.add_argument(
        "--completeness", 
        choices=list(Config.COMPLETENESS_LEVELS.keys()),
        default=Config.DEFAULT_COMPLETENESS_LEVEL,
        help=f"Completeness level: FAST (95%%), BALANCED (99%%), LOSSLESS (100%%) (default: {Config.DEFAULT_COMPLETENESS_LEVEL})"
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        default=True,
        help="WebSocket streaming is always enabled in this version (WebSocket-only mode)"
    )
    
    args = parser.parse_args()
    
    # Update config with command line arguments
    Config.SCAN_INTERVAL_SECONDS = args.interval
    Config.MIN_PROFIT_THRESHOLD = args.threshold
    Config.SIMILARITY_THRESHOLD = args.similarity
    
    # WebSocket-only mode is always enabled - remove REST fallback option
    Config.REALTIME_ENABLED = True
    Config.STREAM_FALLBACK_TO_REST = False
    
    # Ensure WebSocket connections are enabled (can be controlled via CLI)
    if args.realtime or not hasattr(args, 'realtime'):
        Config.WEBSOCKET_CONFIG['kalshi']['enabled'] = True
        Config.WEBSOCKET_CONFIG['polymarket']['enabled'] = True
    
    # Create and run the analysis system
    system = ArbitrageAnalysisSystem()
    
    async def run_system():
        await system.initialize()
        
        # Set completeness level if specified
        if hasattr(args, 'completeness'):
            system.analyzer.set_completeness_level(args.completeness)
        
        if args.mode == "continuous":
            await system.run_continuous_analysis()
        else:
            await system.run_single_scan()
    
    try:
        asyncio.run(run_system())
    except KeyboardInterrupt:
        print("\n👋 Analysis stopped by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
