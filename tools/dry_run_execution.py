#!/usr/bin/env python3
"""Dry-run execution simulator.

Does EVERYTHING a real execution would do — fetch orderbooks, validate
prices, measure latency — but stops right before placing the order.

Answers the key question: do opportunities survive long enough to trade?
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient
from kalshi_arbitrage.market_analyzer import MarketAnalyzer
from kalshi_arbitrage.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

Config.ORDERBOOK_REST_FALLBACK = True


@dataclass
class DryRunResult:
    """Result of a single dry-run execution attempt."""
    opportunity_id: str = ""
    market_name: str = ""
    strategy: str = ""
    similarity: float = 0.0

    # Detection phase
    detect_margin: float = 0.0        # Margin when first detected
    detect_buy_price: float = 0.0
    detect_sell_price: float = 0.0
    detect_volume: float = 0.0

    # Verification phase (re-fetch orderbooks)
    verify_margin: float = 0.0        # Margin after re-fetching
    verify_buy_price: float = 0.0
    verify_sell_price: float = 0.0
    verify_volume: float = 0.0

    # Timing
    detect_time_ms: float = 0.0       # Time to detect opportunity
    refetch_time_ms: float = 0.0      # Time to re-fetch orderbooks
    total_latency_ms: float = 0.0     # Total from scan start to execution-ready

    # Outcome
    survived: bool = False            # Did the opportunity survive re-fetch?
    margin_decay_pct: float = 0.0     # How much margin decayed
    would_profit: float = 0.0        # Profit if we had executed
    kill_reason: str = ""             # Why it died (if it did)


async def refetch_orderbook_kalshi(client: KalshiClient, ticker: str) -> Optional[Dict]:
    """Fresh orderbook fetch (bypasses cache) to simulate execution latency."""
    # Clear this specific ticker from cache to force REST fetch
    client.orderbook_cache.pop(ticker, None)
    client._orderbook_rest_budget = max(client._orderbook_rest_budget, 1)
    return await client.get_market_orderbook(ticker)


async def refetch_orderbook_poly(client: PolymarketClient, market_id: str, token_id: str) -> Optional[Dict]:
    """Fresh orderbook fetch (bypasses cache) to simulate execution latency."""
    client.orderbook_cache.pop(token_id, None)
    client._orderbook_rest_budget = max(client._orderbook_rest_budget, 1)
    return await client.get_market_orderbook(market_id, token_id)


def best_ask(orderbook: Optional[Dict], side: str = "asks") -> Tuple[float, float]:
    """Get best (lowest) ask price and its size."""
    if not orderbook:
        return (0.0, 0.0)
    levels = orderbook.get(side, orderbook.get("yes_asks", []))
    if not levels:
        return (0.0, 0.0)
    best = min(levels, key=lambda x: x.get("price", 999))
    return (best.get("price", 0.0), best.get("size", 0.0))


def best_bid(orderbook: Optional[Dict], side: str = "bids") -> Tuple[float, float]:
    """Get best (highest) bid price and its size."""
    if not orderbook:
        return (0.0, 0.0)
    levels = orderbook.get(side, orderbook.get("yes_bids", []))
    if not levels:
        return (0.0, 0.0)
    best = max(levels, key=lambda x: x.get("price", 0))
    return (best.get("price", 0.0), best.get("size", 0.0))


async def dry_run_opportunity(
    opp: Dict,
    analyzer: MarketAnalyzer,
    scan_start: float,
) -> DryRunResult:
    """Execute a full dry-run for one opportunity."""
    result = DryRunResult()
    result.opportunity_id = opp.get("opportunity_id", "")
    result.market_name = opp.get("market_name", result.opportunity_id.split("_")[0])
    result.strategy = opp.get("strategy", opp.get("strategy_type", ""))
    result.similarity = opp.get("similarity_score", 0.0)

    # Detection phase (from the scan)
    result.detect_margin = opp.get("profit_margin", 0.0)
    result.detect_buy_price = opp.get("weighted_avg_buy_price", 0.0)
    result.detect_sell_price = opp.get("weighted_avg_sell_price", 0.0)
    result.detect_volume = opp.get("max_tradeable_volume", 0.0)
    result.detect_time_ms = (time.time() - scan_start) * 1000

    # --- VERIFICATION PHASE: re-fetch orderbooks like a real execution would ---
    t0 = time.time()

    # Extract identifiers
    oid = result.opportunity_id
    parts = oid.split("_")
    kalshi_ticker = parts[0] if parts else ""

    # Get the polymarket token from the opportunity
    poly_token = opp.get("polymarket_token", "")

    # Re-fetch both orderbooks simultaneously (this is what real execution does)
    k_ob, p_ob = await asyncio.gather(
        refetch_orderbook_kalshi(analyzer.kalshi_client, kalshi_ticker),
        refetch_orderbook_poly(analyzer.polymarket_client, "", poly_token) if poly_token else asyncio.sleep(0),
        return_exceptions=True,
    )

    result.refetch_time_ms = (time.time() - t0) * 1000
    result.total_latency_ms = (time.time() - scan_start) * 1000

    # Handle fetch failures
    if isinstance(k_ob, Exception) or k_ob is None:
        result.kill_reason = "kalshi_orderbook_fetch_failed"
        return result
    if isinstance(p_ob, Exception) or p_ob is None:
        result.kill_reason = "polymarket_orderbook_fetch_failed"
        return result

    # Compute margin with fresh data
    strategy = opp.get("strategy", "")
    is_complementary = "+" in strategy and "$1" in strategy

    if is_complementary:
        # Complementary: buy YES on A + buy NO on B → $1 payout
        k_ask_price, k_ask_size = best_ask(k_ob, "yes_asks")
        p_ask_price, p_ask_size = best_ask(p_ob, "asks")

        if k_ask_price <= 0 or p_ask_price <= 0:
            result.kill_reason = "no_ask_after_refetch"
            return result

        combined_cost = k_ask_price + p_ask_price
        result.verify_buy_price = combined_cost
        result.verify_sell_price = 1.0
        result.verify_margin = (1.0 - combined_cost) / combined_cost if combined_cost > 0 else 0
        result.verify_volume = min(k_ask_size, p_ask_size)
    elif "Buy Kalshi" in strategy and "Sell Polymarket" in strategy:
        # S2: Buy Kalshi YES → Sell Polymarket YES
        k_ask_price, k_ask_size = best_ask(k_ob, "yes_asks")
        p_bid_price, p_bid_size = best_bid(p_ob, "bids")

        if k_ask_price <= 0 or p_bid_price <= 0:
            result.kill_reason = "no_bid_ask_after_refetch"
            return result

        result.verify_buy_price = k_ask_price
        result.verify_sell_price = p_bid_price
        result.verify_margin = (p_bid_price - k_ask_price) / k_ask_price if k_ask_price > 0 else 0
        result.verify_volume = min(k_ask_size, p_bid_size)
    elif "Buy Polymarket" in strategy and "Sell Kalshi" in strategy:
        # S1: Buy Polymarket YES → Sell Kalshi YES
        p_ask_price, p_ask_size = best_ask(p_ob, "asks")
        k_bid_price, k_bid_size = best_bid(k_ob, "yes_bids")

        if p_ask_price <= 0 or k_bid_price <= 0:
            result.kill_reason = "no_bid_ask_after_refetch"
            return result

        result.verify_buy_price = p_ask_price
        result.verify_sell_price = k_bid_price
        result.verify_margin = (k_bid_price - p_ask_price) / p_ask_price if p_ask_price > 0 else 0
        result.verify_volume = min(p_ask_size, k_bid_size)
    else:
        result.kill_reason = f"unknown_strategy: {strategy}"
        return result

    # Determine outcome
    if result.verify_margin > 0.005:  # >0.5% still profitable after fees
        result.survived = True
        result.would_profit = result.verify_margin * min(result.verify_volume, 100)  # cap at $100
    else:
        result.kill_reason = f"margin_gone ({result.verify_margin:+.2%})"

    if result.detect_margin > 0:
        result.margin_decay_pct = (result.detect_margin - result.verify_margin) / result.detect_margin * 100
    return result


async def main():
    analyzer = MarketAnalyzer()

    logger.info("Bootstrapping markets...")
    await analyzer.kalshi_client._discover_markets_via_rest()
    await analyzer.polymarket_client._bootstrap_markets_via_rest()

    kalshi_markets = list(analyzer.kalshi_client.markets_cache.values())
    poly_markets = list(analyzer.polymarket_client.markets_cache.values())

    processed_k = analyzer._process_kalshi_markets(kalshi_markets)
    processed_p = analyzer._process_polymarket_markets(poly_markets)
    analyzer._current_polymarket_markets = processed_p

    logger.info("Matching markets...")
    matches = await analyzer._find_market_matches(processed_k, processed_p)
    logger.info(f"{len(matches)} matched pairs")

    # Initial scan: find all opportunities
    analyzer.kalshi_client._orderbook_rest_budget = 500
    analyzer.polymarket_client._orderbook_rest_budget = 500
    analyzer._scan_orderbook_hits = {"kalshi": 0, "polymarket": 0}
    analyzer._scan_orderbook_misses = {"kalshi": 0, "polymarket": 0}
    analyzer._nearest_misses = []

    scan_start = time.time()
    logger.info("Running opportunity scan...")
    opportunities = await analyzer._calculate_opportunities(matches)
    scan_duration = time.time() - scan_start

    # Sort by profit descending
    opportunities = sorted(opportunities, key=lambda x: x.get("total_profit", 0), reverse=True)
    logger.info(f"Found {len(opportunities)} opportunities in {scan_duration:.1f}s")

    if not opportunities:
        logger.warning("No opportunities found — markets are efficient right now.")
        return

    # --- DRY RUN: verify each opportunity with fresh orderbooks ---
    logger.info("")
    logger.info("=" * 70)
    logger.info("DRY-RUN EXECUTION — re-fetching orderbooks for each opportunity")
    logger.info("=" * 70)

    # Reset budgets for verification phase
    analyzer.kalshi_client._orderbook_rest_budget = 999
    analyzer.polymarket_client._orderbook_rest_budget = 999

    results: List[DryRunResult] = []
    for i, opp in enumerate(opportunities[:30]):  # Top 30 opportunities
        logger.info(f"  Verifying #{i+1}/{min(30, len(opportunities))}...")
        r = await dry_run_opportunity(opp, analyzer, scan_start)
        results.append(r)

    # --- REPORT ---
    survived = [r for r in results if r.survived]
    failed = [r for r in results if not r.survived]

    print()
    print("=" * 70)
    print("DRY-RUN EXECUTION RESULTS")
    print("=" * 70)
    print(f"Scan duration:        {scan_duration:.1f}s")
    print(f"Opportunities found:  {len(opportunities)}")
    print(f"Verified (top 30):    {len(results)}")
    print(f"  SURVIVED:           {len(survived)} ({len(survived)/len(results)*100:.0f}%)")
    print(f"  KILLED:             {len(failed)} ({len(failed)/len(results)*100:.0f}%)")

    if survived:
        avg_latency = sum(r.total_latency_ms for r in survived) / len(survived)
        avg_refetch = sum(r.refetch_time_ms for r in survived) / len(survived)
        avg_decay = sum(r.margin_decay_pct for r in survived) / len(survived)
        total_profit = sum(r.would_profit for r in survived)

        print()
        print("SURVIVING OPPORTUNITIES:")
        print(f"  Avg total latency:  {avg_latency:.0f}ms")
        print(f"  Avg refetch time:   {avg_refetch:.0f}ms")
        print(f"  Avg margin decay:   {avg_decay:.1f}%")
        print(f"  Total would-profit: ${total_profit:.2f} (capped at $100/trade)")
        print()

        for r in survived:
            print(f"  ✓ {r.market_name[:45]:45s}")
            print(f"    Detected: buy@{r.detect_buy_price:.3f} sell@{r.detect_sell_price:.3f} margin={r.detect_margin:+.1%}")
            print(f"    Verified: buy@{r.verify_buy_price:.3f} sell@{r.verify_sell_price:.3f} margin={r.verify_margin:+.1%}")
            print(f"    Latency: {r.total_latency_ms:.0f}ms (refetch: {r.refetch_time_ms:.0f}ms)")
            print(f"    Decay: {r.margin_decay_pct:.1f}% | Would profit: ${r.would_profit:.2f}")
            print()

    if failed:
        print()
        print("KILLED OPPORTUNITIES:")
        from collections import Counter
        kill_reasons = Counter(r.kill_reason for r in failed)
        for reason, count in kill_reasons.most_common():
            print(f"  {count}x {reason}")
        print()
        for r in failed[:10]:
            print(f"  ✗ {r.market_name[:45]:45s} | {r.kill_reason}")
            print(f"    Detected: margin={r.detect_margin:+.1%} | Verified: margin={r.verify_margin:+.1%}")
            print(f"    Latency: {r.total_latency_ms:.0f}ms")

    # Final verdict
    print()
    print("=" * 70)
    if len(survived) == 0:
        print("VERDICT: All opportunities died during verification.")
        print("The market is too efficient — prices move before you can execute.")
    elif len(survived) / len(results) < 0.2:
        print(f"VERDICT: Only {len(survived)/len(results)*100:.0f}% survived. Most are too fleeting.")
        print("You'd need sub-second execution to capture them reliably.")
    elif len(survived) / len(results) < 0.5:
        survival = len(survived)/len(results)*100
        print(f"VERDICT: {survival:.0f}% survival rate. Opportunities are real but fragile.")
        print(f"Expected daily profit (conservative): ${total_profit * 10:.2f}")
    else:
        survival = len(survived)/len(results)*100
        print(f"VERDICT: {survival:.0f}% survival rate. Opportunities are persistent enough to trade.")
        print(f"Expected daily profit (conservative, $100/trade cap): ${total_profit * 20:.2f}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
