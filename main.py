#!/usr/bin/env python3
"""
Production Arbitrage Trading System

WebSocket-powered real-time arbitrage detection and execution
for Kalshi and Polymarket prediction markets.

Usage:
    python main.py                     # Monitor mode (no execution)
    python main.py --execute           # Live trading mode
    python main.py --min-profit 2.5    # Set minimum profit threshold

This system is designed for offshore deployment where prediction
market trading is legal.
"""

import asyncio
import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Setup logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
os.makedirs("market_data", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("market_data/arbitrage.log"),
    ],
)
logger = logging.getLogger(__name__)


def print_banner(mode: str):
    """Print startup banner."""
    mode_display = "LIVE TRADING" if mode == "execute" else "MONITOR ONLY"
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║           ARBITRAGE TRADING SYSTEM v5.0                          ║
║           {mode_display:^42}           ║
╠══════════════════════════════════════════════════════════════════╣
║  Strategies:                                                     ║
║    • Intra-Market:    YES + NO < $1.00 (same platform)          ║
║    • Multi-Outcome:   All outcomes sum < $1.00                  ║
║    • Cross-Platform:  Kalshi vs Polymarket price gaps           ║
╠══════════════════════════════════════════════════════════════════╣
║  Features:                                                       ║
║    • WebSocket real-time feeds    • Atomic execution            ║
║    • Balance management           • Discord/Telegram alerts     ║
║    • Trade persistence            • Circuit breakers            ║
╚══════════════════════════════════════════════════════════════════╝
    """)


class ArbitrageSystem:
    """
    Main arbitrage trading system.

    Coordinates all components: scanner, executors, wallet, persistence, alerts.
    """

    def __init__(self, config: dict):
        self.config = config
        self.execute_trades = config.get("execute", False)
        self.min_profit_pct = Decimal(str(config.get("min_profit_pct", 2.5)))

        # Components (lazy initialized)
        self.scanner = None
        self.kalshi_executor = None
        self.polymarket_executor = None
        self.atomic_executor = None
        self.balance_manager = None
        self.database = None
        self.alerts = None
        self.metrics = None

        # State
        self.running = False
        self.opportunities_log = []

        logger.info(f"ArbitrageSystem initialized (execute={self.execute_trades})")

    async def initialize(self):
        """Initialize all components."""
        logger.info("Initializing system components...")

        # Import components
        from kalshi_arbitrage.realtime_scanner import RealTimeArbitrageScanner
        from kalshi_arbitrage.config import Config
        from kalshi_arbitrage.persistence import Database
        from kalshi_arbitrage.monitoring import AlertManager, MetricsCollector

        # Initialize persistence
        self.database = Database()
        logger.info("Database initialized")

        # Initialize metrics
        self.metrics = MetricsCollector()
        logger.info("Metrics collector initialized")

        # Initialize alerts
        self.alerts = AlertManager(
            webhook_url=os.getenv("ALERT_WEBHOOK_URL"),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        )
        await self.alerts.initialize()
        logger.info("Alert manager initialized")

        # Initialize executors if trading enabled
        if self.execute_trades:
            await self._initialize_executors()

        # Initialize scanner
        scanner_config = Config.ARBITRAGE_CONFIG.copy()
        scanner_config["min_profit_pct"] = float(self.min_profit_pct) / 100
        self.scanner = RealTimeArbitrageScanner(scanner_config)

        # Register opportunity callback
        self.scanner.on_opportunity(self._on_opportunity)

        logger.info("All components initialized")

    async def _initialize_executors(self):
        """Initialize trade executors."""
        from kalshi_arbitrage.executor import (
            KalshiExecutor,
            PolymarketExecutor,
            AtomicExecutor,
        )
        from kalshi_arbitrage.wallet import BalanceManager

        # Kalshi executor
        if os.getenv("KALSHI_API_KEY"):
            self.kalshi_executor = KalshiExecutor()
            if await self.kalshi_executor.initialize():
                logger.info("Kalshi executor ready")
            else:
                logger.warning("Kalshi executor initialization failed")
                self.kalshi_executor = None

        # Polymarket executor
        if os.getenv("POLYMARKET_PRIVATE_KEY"):
            self.polymarket_executor = PolymarketExecutor()
            if await self.polymarket_executor.initialize():
                logger.info("Polymarket executor ready")
            else:
                logger.warning("Polymarket executor initialization failed")
                self.polymarket_executor = None

        # Atomic executor (needs both)
        if self.kalshi_executor and self.polymarket_executor:
            self.atomic_executor = AtomicExecutor(
                self.kalshi_executor,
                self.polymarket_executor,
                max_execution_time_ms=5000,
            )
            logger.info("Atomic executor ready")

        # Balance manager
        self.balance_manager = BalanceManager(
            kalshi_executor=self.kalshi_executor,
            polymarket_executor=self.polymarket_executor,
        )

        # Refresh balances
        summary = await self.balance_manager.refresh_balances()
        logger.info(f"Portfolio: ${float(summary.total_value):.2f}")

    async def start(self):
        """Start the trading system."""
        self.running = True

        # Start scanner
        await self.scanner.start()

        # Send startup alert
        await self.alerts.info(
            "System Started",
            f"Arbitrage system online (mode={'LIVE' if self.execute_trades else 'MONITOR'})",
            {"min_profit": f"{self.min_profit_pct}%"},
        )

    async def stop(self):
        """Stop the trading system."""
        self.running = False

        # Stop scanner
        if self.scanner:
            await self.scanner.stop()

        # Close executors
        if self.kalshi_executor:
            await self.kalshi_executor.close()
        if self.polymarket_executor:
            await self.polymarket_executor.close()

        # Close alerts
        if self.alerts:
            await self.alerts.close()

        # Save final stats
        if self.metrics:
            self.database.set_state("final_metrics", self.metrics.get_summary())

        logger.info("System stopped")

    def _on_opportunity(self, opp):
        """Handle detected opportunity."""
        from kalshi_arbitrage.persistence import Opportunity

        profit_pct = float(opp.profit_percentage)

        # Log to console
        self._print_opportunity(opp)

        # Record metrics
        self.metrics.record_opportunity(opp.arb_type.value, profit_pct)

        # Save to database
        db_opp = Opportunity(
            id=None,
            opportunity_id=opp.opportunity_id,
            arb_type=opp.arb_type.value,
            platform=opp.platform,
            market_id=opp.market_id,
            market_title=opp.market_title,
            profit_pct=profit_pct,
            total_cost=float(opp.total_cost),
            status="detected",
            execution_id=None,
            detected_at=datetime.now(timezone.utc).isoformat(),
            executed_at=None,
        )
        self.database.save_opportunity(db_opp)

        # Track in memory
        self.opportunities_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market": opp.market_title,
            "type": opp.arb_type.value,
            "profit_pct": profit_pct,
            "profit_usd": float(opp.profit),
        })

        # Send alert for significant opportunities
        if profit_pct >= float(self.min_profit_pct):
            asyncio.create_task(
                self.alerts.opportunity_found(
                    opp.market_title,
                    profit_pct,
                    opp.arb_type.value,
                )
            )

            # Execute if enabled and profitable enough
            if self.execute_trades and self.atomic_executor:
                asyncio.create_task(self._execute_opportunity(opp))

    async def _execute_opportunity(self, opp):
        """Execute an arbitrage opportunity."""
        from kalshi_arbitrage.persistence import Trade

        logger.info(f"Executing opportunity: {opp.opportunity_id}")

        # Check if we have sufficient balance
        max_size = self.balance_manager.get_max_trade_size()
        if max_size < 100:
            logger.warning("Insufficient balance for execution")
            return

        # Calculate position size (start small)
        position_size = min(
            int(max_size * Decimal("0.1")),  # 10% of available
            int(self.config.get("max_position_size", 1000)),
        )

        # Execute
        result = await self.atomic_executor.execute_arbitrage(
            opp.to_dict(),
            size=position_size,
            use_fok=True,
        )

        # Record metrics
        self.metrics.record_execution(
            success=result.status.value == "success",
            profit=float(result.actual_profit),
            latency_ms=result.execution_time_ms,
        )

        # Update opportunity status
        self.database.update_opportunity_status(
            opp.opportunity_id,
            status="executed" if result.status.value == "success" else "failed",
            execution_id=result.execution_id,
        )

        # Save trades
        for leg in result.legs:
            trade = Trade(
                id=None,
                execution_id=result.execution_id,
                opportunity_id=opp.opportunity_id,
                platform=leg.platform,
                market_id=leg.market_id,
                side=leg.side,
                action=leg.action,
                size=leg.size,
                price=float(leg.price),
                filled_size=leg.filled_size,
                avg_price=float(leg.avg_price) if leg.avg_price else 0,
                status=leg.status,
                pnl=float(result.actual_profit) / len(result.legs),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.database.save_trade(trade)

        # Send alert
        await self.alerts.execution_complete(
            result.execution_id,
            result.status.value,
            float(result.actual_profit),
        )

        # Refresh balances
        await self.balance_manager.refresh_balances()

    def _print_opportunity(self, opp):
        """Print opportunity to console."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        print()
        print("=" * 60)
        print(f"  ARBITRAGE DETECTED @ {timestamp}")
        print("=" * 60)
        print(f"  Market:   {opp.market_title[:50]}")
        print(f"  Type:     {opp.arb_type.value.upper()}")
        print(f"  Platform: {opp.platform}")
        print()
        print(f"  PROFIT:   {float(opp.profit_percentage):.2f}%")
        print(f"  Cost:     ${float(opp.total_cost):.4f}")
        print(f"  Payout:   ${float(opp.guaranteed_payout):.2f}")
        print()
        print("  Positions:")
        for pos in opp.positions:
            platform = pos.get("platform", opp.platform)
            print(f"    {pos['action'].upper()} {pos['side']} @ ${pos['price']:.4f} on {platform}")
        print("=" * 60)

    def get_status(self) -> dict:
        """Get current system status."""
        scanner_stats = self.scanner.get_stats() if self.scanner else {}
        balance_status = self.balance_manager.get_status() if self.balance_manager else {}
        metrics_summary = self.metrics.get_summary() if self.metrics else {}

        return {
            "running": self.running,
            "mode": "LIVE" if self.execute_trades else "MONITOR",
            "scanner": scanner_stats,
            "balances": balance_status,
            "metrics": metrics_summary,
            "opportunities_count": len(self.opportunities_log),
        }


async def main():
    parser = argparse.ArgumentParser(
        description="Arbitrage Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                     # Monitor mode
  python main.py --execute           # Live trading
  python main.py --min-profit 3.0    # 3% minimum profit
        """,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Enable live trade execution (default: monitor only)",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=float(os.getenv("MIN_PROFIT_PCT", "2.5")),
        help="Minimum profit percentage to execute (default: 2.5)",
    )
    parser.add_argument(
        "--max-position",
        type=int,
        default=int(os.getenv("MAX_POSITION_SIZE", "1000")),
        help="Maximum position size in USD (default: 1000)",
    )
    parser.add_argument(
        "--full-scan-interval",
        type=int,
        default=int(os.getenv("FULL_SCAN_INTERVAL", "60")),
        help="Seconds between full scans (default: 60)",
    )

    args = parser.parse_args()

    # Check for live trading override
    if os.getenv("LIVE_TRADING_ENABLED", "").lower() == "true":
        args.execute = True

    # Print banner
    mode = "execute" if args.execute else "monitor"
    print_banner(mode)

    # Configuration
    config = {
        "execute": args.execute,
        "min_profit_pct": args.min_profit,
        "max_position_size": args.max_position,
        "full_scan_interval": args.full_scan_interval,
    }

    print("Configuration:")
    print(f"  Mode:           {'LIVE TRADING' if args.execute else 'MONITOR ONLY'}")
    print(f"  Min profit:     {args.min_profit}%")
    print(f"  Max position:   ${args.max_position}")
    print(f"  Scan interval:  {args.full_scan_interval}s")
    print()

    if args.execute:
        print("  WARNING: Live trading enabled!")
        print("  Trades will be executed automatically.")
        print()

    # Create system
    system = ArbitrageSystem(config)

    # Initialize
    await system.initialize()

    # Setup shutdown handler
    shutdown_event = asyncio.Event()

    def handle_shutdown(sig, frame):
        print("\n\nShutdown requested...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start system
    await system.start()
    print("System running. Press Ctrl+C to stop.\n")

    # Main loop
    last_full_scan = time.time()
    last_status = 0

    try:
        while not shutdown_event.is_set():
            await asyncio.sleep(1)

            # Status update every 10 seconds
            now = time.time()
            if now - last_status >= 10:
                last_status = now
                status = system.get_status()
                scanner = status.get("scanner", {})

                poly = "+" if scanner.get("polymarket_connected") else "-"
                kalshi = "+" if scanner.get("kalshi_connected") else "-"

                print(
                    f"\r[{datetime.now().strftime('%H:%M:%S')}] "
                    f"Poly[{poly}] Kalshi[{kalshi}] | "
                    f"Updates: {scanner.get('price_updates', 0)} | "
                    f"Opps: {status.get('opportunities_count', 0)}",
                    end="",
                    flush=True,
                )

            # Periodic full scan
            if now - last_full_scan >= args.full_scan_interval:
                print("\n\nRunning full scan...")
                await system.scanner._full_scan()
                last_full_scan = now

    except asyncio.CancelledError:
        pass
    finally:
        await system.stop()

    # Final summary
    print("\n\n" + "=" * 60)
    print("SESSION SUMMARY")
    print("=" * 60)

    status = system.get_status()
    metrics = status.get("metrics", {})

    print(f"Opportunities detected: {metrics.get('opportunities_detected', 0)}")
    print(f"Executions: {metrics.get('executions', {}).get('total', 0)}")
    print(f"Success rate: {metrics.get('executions', {}).get('success_rate', 0):.1f}%")
    print(f"Total profit: ${metrics.get('profit', {}).get('total', 0):.2f}")

    if system.opportunities_log:
        best = max(system.opportunities_log, key=lambda x: x["profit_pct"])
        print(f"\nBest opportunity: {best['profit_pct']:.2f}% on {best['market'][:40]}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
