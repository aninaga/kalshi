#!/usr/bin/env python3
"""
Real-Time Arbitrage Scanner - Production Entry Point

Connects to Polymarket and Kalshi via WebSockets and detects
arbitrage opportunities in real-time as prices change.

Usage:
    python realtime_arb.py                    # Run with defaults
    python realtime_arb.py --min-profit 1.0   # 1% minimum profit
    python realtime_arb.py --alert-sound      # Play sound on opportunity

This is designed to be a money-making engine.
"""

import asyncio
import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('market_data/realtime_arb.log')
    ]
)
logger = logging.getLogger(__name__)

# Ensure market_data directory exists
os.makedirs('market_data', exist_ok=True)


def print_banner():
    """Print startup banner."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║         REAL-TIME ARBITRAGE SCANNER v4.0                     ║
║         WebSocket-Powered Opportunity Detection              ║
╠══════════════════════════════════════════════════════════════╣
║  Strategies:                                                 ║
║    • Intra-Market:    YES + NO < $1.00                      ║
║    • Multi-Outcome:   All outcomes sum < $1.00              ║
║    • Cross-Platform:  Kalshi vs Polymarket price gaps       ║
╠══════════════════════════════════════════════════════════════╣
║  Data Source: Live WebSocket feeds                          ║
║  Detection Speed: Sub-second                                ║
╚══════════════════════════════════════════════════════════════╝
    """)


async def main():
    parser = argparse.ArgumentParser(description='Real-Time Arbitrage Scanner')
    parser.add_argument('--min-profit', type=float, default=0.5,
                       help='Minimum profit percentage (default: 0.5)')
    parser.add_argument('--alert-sound', action='store_true',
                       help='Play sound on opportunity detection')
    parser.add_argument('--save-opportunities', action='store_true', default=True,
                       help='Save opportunities to JSON file')
    parser.add_argument('--full-scan-interval', type=int, default=60,
                       help='Seconds between full scans (default: 60)')
    args = parser.parse_args()

    print_banner()

    # Import after banner for faster startup feel
    from kalshi_arbitrage.realtime_scanner import RealTimeArbitrageScanner
    from kalshi_arbitrage.arbitrage_engine import ArbitrageOpportunity
    from kalshi_arbitrage.config import Config

    # Configure
    config = Config.ARBITRAGE_CONFIG.copy()
    config['min_profit_pct'] = args.min_profit / 100

    print(f"Configuration:")
    print(f"  • Minimum profit: {args.min_profit}%")
    print(f"  • Full scan interval: {args.full_scan_interval}s")
    print(f"  • Save opportunities: {args.save_opportunities}")
    print()

    # Create scanner
    scanner = RealTimeArbitrageScanner(config)

    # Track opportunities
    opportunities_log = []

    # Opportunity callback
    def on_opportunity(opp: ArbitrageOpportunity):
        timestamp = datetime.now().strftime('%H:%M:%S')

        print()
        print("🚨" * 20)
        print(f"  ARBITRAGE OPPORTUNITY DETECTED @ {timestamp}")
        print("🚨" * 20)
        print()
        print(f"  Market:  {opp.market_title}")
        print(f"  Type:    {opp.arb_type.value.upper()}")
        print(f"  Platform: {opp.platform}")
        print()
        print(f"  💰 PROFIT: {float(opp.profit_percentage):.2f}%")
        print(f"  Cost:    ${float(opp.total_cost):.4f}")
        print(f"  Payout:  ${float(opp.guaranteed_payout):.2f}")
        print(f"  Profit:  ${float(opp.profit):.4f}")
        print()
        print("  Positions to take:")
        for pos in opp.positions:
            platform = pos.get('platform', opp.platform)
            print(f"    → {pos['action']} {pos['side']} @ ${pos['price']:.4f} on {platform}")
        print()
        print(f"  Risk: {opp.execution_risk}")
        print(f"  Max size: ${float(opp.max_profitable_size):.0f}")
        print()
        print("=" * 60)

        # Play sound if enabled
        if args.alert_sound:
            try:
                print('\a')  # Terminal bell
            except:
                pass

        # Save to log
        if args.save_opportunities:
            opportunities_log.append({
                'timestamp': datetime.now().isoformat(),
                'market': opp.market_title,
                'type': opp.arb_type.value,
                'profit_pct': float(opp.profit_percentage),
                'profit_usd': float(opp.profit),
                'positions': opp.positions
            })

            # Write to file
            try:
                with open('market_data/opportunities_realtime.json', 'w') as f:
                    json.dump(opportunities_log[-100:], f, indent=2)  # Keep last 100
            except:
                pass

    scanner.on_opportunity(on_opportunity)

    # Status display
    last_status_time = 0

    def print_status():
        nonlocal last_status_time
        now = time.time()
        if now - last_status_time < 10:  # Every 10 seconds
            return
        last_status_time = now

        stats = scanner.get_stats()
        poly_status = "🟢" if stats['polymarket_connected'] else "🔴"
        kalshi_status = "🟢" if stats['kalshi_connected'] else "🔴"

        print(f"\r[{datetime.now().strftime('%H:%M:%S')}] "
              f"Poly {poly_status} | Kalshi {kalshi_status} | "
              f"Updates: {stats['price_updates']} | "
              f"Scans: {stats['scans_triggered']} | "
              f"Opps: {stats['opportunities_found']}", end='', flush=True)

    # Handle shutdown
    shutdown_event = asyncio.Event()

    def handle_shutdown(sig, frame):
        print("\n\nShutting down gracefully...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start scanner
    print("Starting scanner...")
    await scanner.start()

    print("\nScanner running. Press Ctrl+C to stop.\n")

    # Main loop
    last_full_scan = time.time()

    try:
        while not shutdown_event.is_set():
            await asyncio.sleep(1)
            print_status()

            # Periodic full scan
            if time.time() - last_full_scan > args.full_scan_interval:
                print("\n\nRunning periodic full scan...")
                await scanner._full_scan()
                last_full_scan = time.time()

    except asyncio.CancelledError:
        pass
    finally:
        await scanner.stop()

    # Final summary
    print("\n\n" + "=" * 60)
    print("SESSION SUMMARY")
    print("=" * 60)
    stats = scanner.get_stats()
    print(f"Total runtime: {stats['uptime']:.0f}s")
    print(f"Price updates: {stats['price_updates']}")
    print(f"Scans triggered: {stats['scans_triggered']}")
    print(f"Opportunities found: {stats['opportunities_found']}")

    if opportunities_log:
        print(f"\nBest opportunity:")
        best = max(opportunities_log, key=lambda x: x['profit_pct'])
        print(f"  {best['market'][:50]}")
        print(f"  Profit: {best['profit_pct']:.2f}%")

    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
