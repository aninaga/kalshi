#!/usr/bin/env python3
"""Backtest: compare old (strategies 1-2 only, YES filter, 2% fee) vs new
(strategies 1-4, YES+NO, 1% fee) arbitrage logic against live market data.

Usage:
    python tools/backtest_strategies.py [--limit N] [--threshold T]

This fetches current markets/orderbooks from both platforms, matches them,
and runs all 4 strategies on every matched pair WITHOUT applying the profit
threshold—so you can see the raw margin distribution and how many would have
qualified under the old vs new logic.
"""

import asyncio
import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

# Add parent dir to path so we can import kalshi_arbitrage
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient
from kalshi_arbitrage.market_analyzer import MarketAnalyzer
from kalshi_arbitrage.config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


async def run_backtest(match_limit: int = 0, profit_threshold: float = 0.02):
    """Run side-by-side backtest of old vs new arbitrage logic."""
    analyzer = MarketAnalyzer()

    # --- bootstrap clients (REST only, no websockets needed) ---
    logger.info("Initializing Kalshi client (REST discovery)...")
    await analyzer.kalshi_client._discover_markets_via_rest()
    logger.info(f"Kalshi: {len(analyzer.kalshi_client.markets_cache)} markets")

    logger.info("Initializing Polymarket client (REST bootstrap)...")
    await analyzer.polymarket_client._bootstrap_markets_via_rest()
    logger.info(f"Polymarket: {len(analyzer.polymarket_client.markets_cache)} markets")

    # --- get & match markets ---
    kalshi_markets = list(analyzer.kalshi_client.markets_cache.values())
    polymarket_markets = list(analyzer.polymarket_client.markets_cache.values())

    processed_kalshi = analyzer._process_kalshi_markets(kalshi_markets)
    processed_polymarket = analyzer._process_polymarket_markets(polymarket_markets)
    analyzer._current_polymarket_markets = processed_polymarket

    logger.info("Finding market matches...")
    matches = await analyzer._find_market_matches(processed_kalshi, processed_polymarket)
    logger.info(f"Found {len(matches)} matched pairs")

    if match_limit > 0:
        matches = matches[:match_limit]
        logger.info(f"Limited to {match_limit} matches for backtest")

    # --- reset REST budgets (generous for backtest) ---
    analyzer.kalshi_client._orderbook_rest_budget = 99999
    analyzer.polymarket_client._orderbook_rest_budget = 99999
    Config.ORDERBOOK_REST_FALLBACK = True

    # --- evaluate every match under all 4 strategies ---
    results = []
    strategy_counts_old = defaultdict(int)  # old logic: strategies 1-2, 2% fee, YES only
    strategy_counts_new = defaultdict(int)  # new logic: strategies 1-4, 1% fee, YES+NO
    total_profit_old = 0.0
    total_profit_new = 0.0
    opps_old = 0
    opps_new = 0

    for idx, match in enumerate(matches):
        kalshi_market = match['kalshi_market']
        polymarket_market = match['polymarket_market']
        kalshi_title = kalshi_market.get('title', kalshi_market.get('id', ''))
        poly_title = polymarket_market.get('title', polymarket_market.get('id', ''))

        # Get Polymarket prices
        poly_prices = await analyzer.polymarket_client.get_market_prices(polymarket_market['id'])
        if not poly_prices:
            continue

        # Get Kalshi orderbook
        kalshi_ob = await analyzer.kalshi_client.get_market_orderbook(kalshi_market['id'])
        if not kalshi_ob:
            continue

        # Collect YES/NO Polymarket orderbooks
        yes_token_id = None
        yes_ob = None
        no_token_id = None
        no_ob = None

        for token_id, price_data in poly_prices.items():
            if not price_data:
                continue
            outcome = str(price_data.get('outcome', '')).lower()
            if outcome in {'yes', 'true', '1'}:
                ob = await analyzer.polymarket_client.get_market_orderbook(
                    polymarket_market['id'], token_id)
                if ob:
                    yes_token_id = token_id
                    yes_ob = ob
            elif outcome in {'no', 'false', '0'}:
                ob = await analyzer.polymarket_client.get_market_orderbook(
                    polymarket_market['id'], token_id)
                if ob:
                    no_token_id = token_id
                    no_ob = ob

        pair_result = {
            'kalshi_title': kalshi_title[:60],
            'polymarket_title': poly_title[:60],
            'similarity': match.get('similarity_score', 0),
            'strategies': {},
        }

        def compute_margin_same_outcome(buy_asks, sell_bids, name, fee_rate_buy, fee_rate_sell):
            """Compute best margin for same-outcome strategy."""
            asks = analyzer._normalize_orderbook_levels(buy_asks)
            bids = analyzer._normalize_orderbook_levels(sell_bids)
            if not asks or not bids:
                return None
            best_ask = min(l['price'] for l in asks)
            best_bid = max(l['price'] for l in bids)
            if best_ask <= 0:
                return None
            raw_margin = (best_bid - best_ask) / best_ask
            # Approximate fee-adjusted margin
            effective_cost = best_ask * (1 + fee_rate_buy)
            effective_revenue = best_bid * (1 - fee_rate_sell)
            if effective_cost <= 0:
                return None
            fee_adjusted_margin = (effective_revenue - effective_cost) / effective_cost
            return {
                'strategy': name,
                'best_ask': best_ask,
                'best_bid': best_bid,
                'raw_margin': raw_margin,
                'fee_adjusted_margin': fee_adjusted_margin,
                'buy_fee': fee_rate_buy,
                'sell_fee': fee_rate_sell,
            }

        def compute_margin_complementary(asks_a, asks_b, name, fee_rate_a, fee_rate_b):
            """Compute best margin for complementary strategy."""
            a = analyzer._normalize_orderbook_levels(asks_a)
            b = analyzer._normalize_orderbook_levels(asks_b)
            if not a or not b:
                return None
            best_a = min(l['price'] for l in a)
            best_b = min(l['price'] for l in b)
            combined = best_a + best_b
            if combined <= 0:
                return None
            raw_margin = (1.0 - combined) / combined
            # Fee-adjusted
            total_cost = best_a * (1 + fee_rate_a) + best_b * (1 + fee_rate_b)
            fee_adjusted_margin = (1.0 - total_cost) / total_cost if total_cost > 0 else -1
            return {
                'strategy': name,
                'best_ask_a': best_a,
                'best_ask_b': best_b,
                'combined_cost': combined,
                'raw_margin': raw_margin,
                'fee_adjusted_margin': fee_adjusted_margin,
                'fee_a': fee_rate_a,
                'fee_b': fee_rate_b,
            }

        # --- OLD LOGIC: strategies 1-2, YES only, 2% Poly fee ---
        OLD_POLY_FEE = 0.02

        if yes_ob:
            s1_old = compute_margin_same_outcome(
                kalshi_ob.get('yes_asks', []), yes_ob.get('bids', []),
                'S1: Buy Kalshi YES → Sell Poly YES',
                Config.KALSHI_FEE_RATE, OLD_POLY_FEE)
            s2_old = compute_margin_same_outcome(
                yes_ob.get('asks', []), kalshi_ob.get('yes_bids', []),
                'S2: Buy Poly YES → Sell Kalshi YES',
                OLD_POLY_FEE, Config.KALSHI_FEE_RATE)
            for s in [s1_old, s2_old]:
                if s:
                    pair_result['strategies'][s['strategy'] + ' (OLD)'] = s
                    if s['fee_adjusted_margin'] >= profit_threshold:
                        opps_old += 1
                        total_profit_old += s['fee_adjusted_margin']
                        strategy_counts_old[s['strategy']] += 1

        # --- NEW LOGIC: strategies 1-4, YES+NO, 1% Poly fee ---
        NEW_POLY_FEE = 0.01

        if yes_ob:
            s1_new = compute_margin_same_outcome(
                kalshi_ob.get('yes_asks', []), yes_ob.get('bids', []),
                'S1: Buy Kalshi YES → Sell Poly YES',
                Config.KALSHI_FEE_RATE, NEW_POLY_FEE)
            s2_new = compute_margin_same_outcome(
                yes_ob.get('asks', []), kalshi_ob.get('yes_bids', []),
                'S2: Buy Poly YES → Sell Kalshi YES',
                NEW_POLY_FEE, Config.KALSHI_FEE_RATE)
            for s in [s1_new, s2_new]:
                if s:
                    pair_result['strategies'][s['strategy'] + ' (NEW)'] = s
                    if s['fee_adjusted_margin'] >= profit_threshold:
                        opps_new += 1
                        total_profit_new += s['fee_adjusted_margin']
                        strategy_counts_new[s['strategy']] += 1

        if no_ob:
            s3 = compute_margin_complementary(
                kalshi_ob.get('yes_asks', []), no_ob.get('asks', []),
                'S3: Buy Kalshi YES + Buy Poly NO → $1',
                Config.KALSHI_FEE_RATE, NEW_POLY_FEE)
            if s3:
                pair_result['strategies'][s3['strategy']] = s3
                if s3['fee_adjusted_margin'] >= profit_threshold:
                    opps_new += 1
                    total_profit_new += s3['fee_adjusted_margin']
                    strategy_counts_new[s3['strategy']] += 1

        if yes_ob and kalshi_ob.get('no_asks'):
            s4 = compute_margin_complementary(
                yes_ob.get('asks', []), kalshi_ob.get('no_asks', []),
                'S4: Buy Poly YES + Buy Kalshi NO → $1',
                NEW_POLY_FEE, Config.KALSHI_FEE_RATE)
            if s4:
                pair_result['strategies'][s4['strategy']] = s4
                if s4['fee_adjusted_margin'] >= profit_threshold:
                    opps_new += 1
                    total_profit_new += s4['fee_adjusted_margin']
                    strategy_counts_new[s4['strategy']] += 1

        if pair_result['strategies']:
            results.append(pair_result)

        if (idx + 1) % 50 == 0:
            logger.info(f"Processed {idx + 1}/{len(matches)} pairs...")

    # --- Report ---
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS: OLD LOGIC vs NEW LOGIC")
    print("=" * 80)
    print(f"Matched pairs evaluated: {len(matches)}")
    print(f"Pairs with orderbook data: {len(results)}")
    print(f"Profit threshold: {profit_threshold:.1%}")
    print()

    print("--- OLD LOGIC (Strategies 1-2 only, YES filter, 2% Poly fee) ---")
    print(f"  Opportunities found: {opps_old}")
    if strategy_counts_old:
        for s, c in sorted(strategy_counts_old.items()):
            print(f"    {s}: {c}")
    print()

    print("--- NEW LOGIC (Strategies 1-4, YES+NO, 1% Poly fee) ---")
    print(f"  Opportunities found: {opps_new}")
    if strategy_counts_new:
        for s, c in sorted(strategy_counts_new.items()):
            print(f"    {s}: {c}")
    print()

    delta = opps_new - opps_old
    print(f"DELTA: +{delta} additional opportunities from new logic")
    print()

    # --- Top opportunities by margin (new logic) ---
    all_strategies = []
    for r in results:
        for sname, sdata in r['strategies'].items():
            if '(OLD)' not in sname:
                all_strategies.append({
                    'kalshi': r['kalshi_title'],
                    'poly': r['polymarket_title'],
                    **sdata
                })

    all_strategies.sort(key=lambda x: x['fee_adjusted_margin'], reverse=True)

    print("--- TOP 20 BEST MARGINS (new logic, all 4 strategies) ---")
    for i, s in enumerate(all_strategies[:20]):
        flag = " ★" if s['fee_adjusted_margin'] >= profit_threshold else ""
        if 'combined_cost' in s:
            detail = f"cost={s['combined_cost']:.4f} (a={s['best_ask_a']:.4f}+b={s['best_ask_b']:.4f})"
        else:
            detail = f"ask={s['best_ask']:.4f} bid={s['best_bid']:.4f}"
        print(f"  #{i+1:2d} {s['fee_adjusted_margin']:+.4%} raw={s['raw_margin']:+.4%} "
              f"| {s['strategy'][:45]:45s} | {detail}{flag}")
        print(f"       Kalshi: {s['kalshi']}")
        print(f"       Poly:   {s['poly']}")
    print()

    # --- Margin distribution ---
    print("--- MARGIN DISTRIBUTION (new logic, fee-adjusted) ---")
    bins = [(-1, -0.10), (-0.10, -0.05), (-0.05, -0.02), (-0.02, 0), (0, 0.01), (0.01, 0.02), (0.02, 0.05), (0.05, 1)]
    for lo, hi in bins:
        count = sum(1 for s in all_strategies if lo <= s['fee_adjusted_margin'] < hi)
        bar = '█' * min(count, 60)
        label = f"[{lo:+.0%},{hi:+.0%})" if abs(lo) >= 0.01 else f"[{lo:+.1%},{hi:+.1%})"
        print(f"  {label:>15s}: {count:4d} {bar}")

    # Complementary-only stats
    comp_strategies = [s for s in all_strategies if 'S3' in s['strategy'] or 'S4' in s['strategy']]
    comp_above = [s for s in comp_strategies if s['fee_adjusted_margin'] >= profit_threshold]
    print()
    print(f"--- COMPLEMENTARY STRATEGIES (S3+S4) ---")
    print(f"  Total evaluated: {len(comp_strategies)}")
    print(f"  Above threshold: {len(comp_above)}")
    if comp_strategies:
        best = max(comp_strategies, key=lambda x: x['fee_adjusted_margin'])
        print(f"  Best margin: {best['fee_adjusted_margin']:+.4%} ({best['strategy']})")
        print(f"    Kalshi: {best['kalshi']}")
        print(f"    Poly:   {best['poly']}")

    # --- Save detailed results ---
    output_path = os.path.join(Config.DATA_DIR, 'backtest_results.json')
    os.makedirs(Config.DATA_DIR, exist_ok=True)
    report = {
        'timestamp': datetime.now().isoformat(),
        'matched_pairs': len(matches),
        'pairs_with_data': len(results),
        'profit_threshold': profit_threshold,
        'old_logic': {
            'opportunities': opps_old,
            'strategy_counts': dict(strategy_counts_old),
        },
        'new_logic': {
            'opportunities': opps_new,
            'strategy_counts': dict(strategy_counts_new),
        },
        'delta': delta,
        'top_20_margins': all_strategies[:20],
        'complementary_stats': {
            'total': len(comp_strategies),
            'above_threshold': len(comp_above),
            'best_margin': max((s['fee_adjusted_margin'] for s in comp_strategies), default=None),
        },
        'all_pair_results': results,
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nDetailed results saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description='Backtest old vs new arbitrage logic')
    parser.add_argument('--limit', type=int, default=0,
                        help='Limit number of matched pairs to evaluate (0 = all)')
    parser.add_argument('--threshold', type=float, default=0.02,
                        help='Profit threshold for counting opportunities (default: 0.02)')
    args = parser.parse_args()
    asyncio.run(run_backtest(match_limit=args.limit, profit_threshold=args.threshold))


if __name__ == '__main__':
    main()
