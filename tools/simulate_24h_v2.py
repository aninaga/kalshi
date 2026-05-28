#!/usr/bin/env python3
"""Realistic 24h simulation using 2 independent live scans as data.

Each scan fetches fresh orderbooks (caches cleared between scans).
Realism factors applied per-trade: latency, adverse selection, IOC partial
fills, leg failure + hedge cost, exact platform fees.
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from decimal import Decimal, ROUND_UP, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage.api_clients import KalshiClient, PolymarketClient
from kalshi_arbitrage.market_analyzer import MarketAnalyzer
from kalshi_arbitrage.config import Config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ── Realism knobs ───────────────────────────────────────────────────────
LATENCY_MS_MIN = 100
LATENCY_MS_MAX = 300
ADVERSE_SELECTION_BPS = 15    # 1.5 cents per $1, scales w/ latency
IOC_FILL_RATE_MEAN = 0.85    # Average fill on IOC
IOC_FILL_RATE_STD = 0.08     # Variance
LEG_FAILURE_RATE = 0.05      # One leg fails entirely
HEDGE_COST_RATE = 0.005      # 0.5% to unwind filled leg
SCAN_INTERVAL = 30
SCANS_PER_DAY = 86400 // SCAN_INTERVAL  # 2880


def kalshi_taker_fee(price: float, size: float) -> float:
    p, c = Decimal(str(price)), Decimal(str(size))
    raw = Decimal('0.07') * c * p * (Decimal('1') - p)
    return float((raw * 100).to_integral_value(rounding=ROUND_UP) / Decimal('100'))


def poly_taker_fee(price: float, size: float, bps: int = 200) -> float:
    if bps <= 0:
        return 0.0
    p, c = Decimal(str(price)), Decimal(str(size))
    tv = p * c
    fr = Decimal(bps) / Decimal('4000')
    fee = tv * fr * (p * (Decimal('1') - p)) ** Decimal('2')
    return float(fee.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))


def simulate_trade(asks_a, book_b, is_complementary, strategy, k_title, p_title,
                   fee_a_fn, fee_b_fn) -> Optional[Dict]:
    """Walk two books with full realism. Returns trade dict or None."""
    def parse(levels):
        out = []
        for l in (levels or []):
            try:
                p, s = float(l.get('price', 0)), float(l.get('size', 0))
                if p > 0 and s > 0:
                    out.append((p, s))
            except (TypeError, ValueError):
                continue
        return out

    a = sorted(parse(asks_a))
    if is_complementary:
        b = sorted(parse(book_b))
    else:
        b = sorted(parse(book_b), reverse=True)

    if not a or not b:
        return None

    # Latency & adverse selection
    latency = random.randint(LATENCY_MS_MIN, LATENCY_MS_MAX)
    adv = ADVERSE_SELECTION_BPS * (latency / 200.0) / 10000.0

    # Walk books
    raw_trades = []
    ia = ib = 0
    rem_a = list(a)
    rem_b = list(b)
    sa = [list(x) for x in rem_a]  # mutable sizes
    sb = [list(x) for x in rem_b]

    while ia < len(sa) and ib < len(sb):
        pa, sza = sa[ia]
        pb, szb = sb[ib]

        pa_eff = pa + adv  # Buy worse
        if is_complementary:
            pb_eff = pb + adv
            if pa_eff + pb_eff >= 1.0:
                break
            vol = min(sza, szb)
            if vol < 1:
                if sza <= szb:
                    ia += 1
                else:
                    ib += 1
                continue
            cost = pa_eff * vol + pb_eff * vol
            fee = fee_a_fn(pa_eff, vol) + fee_b_fn(pb_eff, vol)
            net = 1.0 * vol - cost - fee
        else:
            pb_eff = pb - adv  # Sell worse
            if pa_eff >= pb_eff:
                break
            vol = min(sza, szb)
            if vol < 1:
                if sza <= szb:
                    ia += 1
                else:
                    ib += 1
                continue
            cost = pa_eff * vol
            revenue = pb_eff * vol
            fee = fee_a_fn(pa_eff, vol) + fee_b_fn(pb_eff, vol)
            net = revenue - cost - fee

        if net > 0:
            raw_trades.append({'vol': vol, 'net': net, 'fee': fee, 'cost': pa_eff * vol,
                               'pa': pa_eff, 'pb': pb_eff})

        sa[ia][1] -= vol
        sb[ib][1] -= vol
        if sa[ia][1] <= 0:
            ia += 1
        if sb[ib][1] <= 0:
            ib += 1

    if not raw_trades:
        return None

    total_vol = sum(t['vol'] for t in raw_trades)
    total_net_ideal = sum(t['net'] for t in raw_trades)
    total_fee = sum(t['fee'] for t in raw_trades)
    total_cost = sum(t['cost'] for t in raw_trades)

    # IOC fill rate
    fill_rate = min(1.0, max(0.0, random.gauss(IOC_FILL_RATE_MEAN, IOC_FILL_RATE_STD)))
    filled_vol = total_vol * fill_rate

    # Leg failure
    r = random.random()
    if r < 0.01:
        # Both fail — no cost
        return {'strategy': strategy, 'kalshi': k_title, 'poly': p_title,
                'volume': 0, 'net': 0, 'gross': 0, 'fees': 0, 'capital': 0,
                'latency': latency, 'hedge_cost': 0, 'outcome': 'both_failed'}
    elif r < 0.01 + LEG_FAILURE_RATE:
        # One leg fails — hedge the other
        hedge_cost = total_cost * fill_rate * HEDGE_COST_RATE
        return {'strategy': strategy, 'kalshi': k_title, 'poly': p_title,
                'volume': filled_vol, 'net': -hedge_cost, 'gross': 0,
                'fees': total_fee * fill_rate * 0.5, 'capital': total_cost * fill_rate,
                'latency': latency, 'hedge_cost': hedge_cost, 'outcome': 'hedged'}

    # Both legs fill
    net = total_net_ideal * fill_rate
    gross = (total_net_ideal + total_fee) * fill_rate
    fees = total_fee * fill_rate
    capital = total_cost * fill_rate

    return {'strategy': strategy, 'kalshi': k_title, 'poly': p_title,
            'volume': filled_vol, 'net': net, 'gross': gross, 'fees': fees,
            'capital': capital, 'latency': latency, 'hedge_cost': 0,
            'outcome': 'filled' if fill_rate > 0.95 else 'partial'}


async def do_scan(analyzer, matches, scan_label) -> List[Dict]:
    """Run one full scan with fresh orderbooks."""
    trades = []
    for idx, match in enumerate(matches):
        km = match['kalshi_market']
        pm = match['polymarket_market']
        k_title = km.get('title', km.get('id', ''))[:55]
        p_title = pm.get('title', pm.get('id', ''))[:55]

        poly_prices = await analyzer.polymarket_client.get_market_prices(pm['id'])
        if not poly_prices:
            continue
        kalshi_ob = await analyzer.kalshi_client.get_market_orderbook(km['id'])
        if not kalshi_ob:
            continue

        yes_ob = no_ob = None
        for tid, pd in poly_prices.items():
            if not pd:
                continue
            out = str(pd.get('outcome', '')).lower()
            if out in {'yes', 'true', '1'}:
                ob = await analyzer.polymarket_client.get_market_orderbook(pm['id'], tid)
                if ob:
                    yes_ob = ob
            elif out in {'no', 'false', '0'}:
                ob = await analyzer.polymarket_client.get_market_orderbook(pm['id'], tid)
                if ob:
                    no_ob = ob

        kf = kalshi_taker_fee
        pf = poly_taker_fee

        if yes_ob:
            t = simulate_trade(kalshi_ob.get('yes_asks', []), yes_ob.get('bids', []),
                               False, 'S1', k_title, p_title, kf, pf)
            if t: trades.append(t)
            t = simulate_trade(yes_ob.get('asks', []), kalshi_ob.get('yes_bids', []),
                               False, 'S2', k_title, p_title, pf, kf)
            if t: trades.append(t)
        if no_ob:
            t = simulate_trade(kalshi_ob.get('yes_asks', []), no_ob.get('asks', []),
                               True, 'S3', k_title, p_title, kf, pf)
            if t: trades.append(t)
        if yes_ob and kalshi_ob.get('no_asks'):
            t = simulate_trade(yes_ob.get('asks', []), kalshi_ob.get('no_asks', []),
                               True, 'S4', k_title, p_title, pf, kf)
            if t: trades.append(t)

        if (idx + 1) % 200 == 0:
            logger.info(f"  [{scan_label}] {idx+1}/{len(matches)}...")

    return trades


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

    Config.ORDERBOOK_REST_FALLBACK = True

    # ── SCAN 1 ──
    logger.info("=== SCAN 1 (fresh orderbooks) ===")
    analyzer.kalshi_client._orderbook_rest_budget = 99999
    analyzer.polymarket_client._orderbook_rest_budget = 99999
    t1 = time.time()
    trades_1 = await do_scan(analyzer, matches, "Scan 1")
    d1 = time.time() - t1

    # Clear ALL orderbook caches
    analyzer.kalshi_client.orderbook_cache.clear()
    analyzer.polymarket_client.orderbook_cache.clear()

    # Wait for real time to pass so market state changes
    logger.info(f"Scan 1 done ({d1:.0f}s). Waiting 60s for market state to change...")
    await asyncio.sleep(60)

    # ── SCAN 2 ──
    logger.info("=== SCAN 2 (fresh orderbooks) ===")
    analyzer.kalshi_client._orderbook_rest_budget = 99999
    analyzer.polymarket_client._orderbook_rest_budget = 99999
    t2 = time.time()
    trades_2 = await do_scan(analyzer, matches, "Scan 2")
    d2 = time.time() - t2

    # ── Analysis ──
    scans = [
        {'label': 'Scan 1', 'trades': trades_1, 'duration': d1},
        {'label': 'Scan 2', 'trades': trades_2, 'duration': d2},
    ]

    all_trades = trades_1 + trades_2

    print("\n" + "=" * 80)
    print("  24-HOUR REALISTIC SIMULATION (2 independent live scans)")
    print("=" * 80)

    print(f"\n  Realism parameters:")
    print(f"    Latency:           {LATENCY_MS_MIN}-{LATENCY_MS_MAX}ms (random per trade)")
    print(f"    Adverse selection: {ADVERSE_SELECTION_BPS} bps (price moves against us)")
    print(f"    IOC fill rate:     {IOC_FILL_RATE_MEAN:.0%} ± {IOC_FILL_RATE_STD:.0%}")
    print(f"    Leg failure:       {LEG_FAILURE_RATE:.0%} → hedge @ {HEDGE_COST_RATE:.1%} cost")
    print(f"    Scan interval:     {SCAN_INTERVAL}s → {SCANS_PER_DAY} scans/day")

    for s in scans:
        tr = s['trades']
        filled = [t for t in tr if t['outcome'] in ('filled', 'partial')]
        hedged = [t for t in tr if t['outcome'] == 'hedged']
        net = sum(t['net'] for t in tr)
        cap = sum(t['capital'] for t in filled)
        fees = sum(t['fees'] for t in tr)
        print(f"\n  {s['label']} ({s['duration']:.0f}s):")
        print(f"    Profitable trades: {len(filled)}")
        print(f"    Hedge events:      {len(hedged)} (cost ${sum(t['hedge_cost'] for t in hedged):.2f})")
        print(f"    Net profit:        ${net:.2f}")
        print(f"    Fees:              ${fees:.2f}")
        print(f"    Capital:           ${cap:.2f}")

    # Average per scan
    avg_net = sum(sum(t['net'] for t in s['trades']) for s in scans) / len(scans)
    avg_cap = sum(sum(t['capital'] for t in s['trades'] if t['outcome'] in ('filled','partial')) for s in scans) / len(scans)
    avg_fees = sum(sum(t['fees'] for t in s['trades']) for s in scans) / len(scans)
    avg_filled = sum(len([t for t in s['trades'] if t['outcome'] in ('filled','partial')]) for s in scans) / len(scans)
    avg_hedged = sum(len([t for t in s['trades'] if t['outcome'] == 'hedged']) for s in scans) / len(scans)

    # Count unique market pairs across both scans to measure overlap
    pairs_1 = set((t['kalshi'], t['poly']) for t in trades_1 if t['net'] > 0)
    pairs_2 = set((t['kalshi'], t['poly']) for t in trades_2 if t['net'] > 0)
    overlap = len(pairs_1 & pairs_2)
    total_unique = len(pairs_1 | pairs_2)
    if total_unique > 0:
        measured_overlap = overlap / max(len(pairs_1), len(pairs_2), 1)
    else:
        measured_overlap = 0.0
    unique_rate = 1.0 - measured_overlap

    print(f"\n  Overlap analysis:")
    print(f"    Scan 1 profitable pairs: {len(pairs_1)}")
    print(f"    Scan 2 profitable pairs: {len(pairs_2)}")
    print(f"    Overlapping pairs:       {overlap}")
    print(f"    Measured overlap:        {measured_overlap:.0%}")
    print(f"    Unique rate per scan:    {unique_rate:.0%}")

    # 24h projection
    proj_net = avg_net * SCANS_PER_DAY * unique_rate
    proj_cap = avg_cap * SCANS_PER_DAY * unique_rate
    proj_fees = avg_fees * SCANS_PER_DAY * unique_rate
    proj_trades = avg_filled * SCANS_PER_DAY * unique_rate
    proj_hedges = avg_hedged * SCANS_PER_DAY * unique_rate
    roi = (proj_net / proj_cap * 100) if proj_cap > 0 else 0

    print(f"\n  ┌──────────────────────────────────────────────────────┐")
    print(f"  │  24-HOUR PROJECTION (using measured {unique_rate:.0%} unique rate)  │")
    print(f"  ├──────────────────────────────────────────────────────┤")
    print(f"  │  Net profit:       ${proj_net:>11.2f}                    │")
    print(f"  │  Fees:             ${proj_fees:>11.2f}                    │")
    print(f"  │  Capital turnover: ${proj_cap:>11.2f}                    │")
    print(f"  │  Trades:            {proj_trades:>9.0f}                      │")
    print(f"  │  Hedge events:      {proj_hedges:>9.0f}                      │")
    print(f"  │  ROI:               {roi:>8.1f}%                       │")
    print(f"  └──────────────────────────────────────────────────────┘")

    # Strategy breakdown
    by_strat = defaultdict(lambda: {'n': 0, 'net': 0, 'vol': 0, 'cap': 0})
    for t in all_trades:
        if t['outcome'] in ('filled', 'partial'):
            by_strat[t['strategy']]['n'] += 1
            by_strat[t['strategy']]['net'] += t['net']
            by_strat[t['strategy']]['vol'] += t['volume']
            by_strat[t['strategy']]['cap'] += t['capital']

    labels = {'S1': 'Buy Kalshi YES → Sell Poly YES',
              'S2': 'Buy Poly YES → Sell Kalshi YES',
              'S3': 'Buy Kalshi YES + Buy Poly NO → $1',
              'S4': 'Buy Poly YES + Buy Kalshi NO → $1'}
    print(f"\n  Strategy breakdown (across both scans):")
    for sn in ['S1', 'S2', 'S3', 'S4']:
        s = by_strat.get(sn, {'n': 0, 'net': 0, 'vol': 0, 'cap': 0})
        if s['n'] > 0:
            print(f"    {sn} ({labels[sn]})")
            print(f"       Trades: {s['n']}, Net: ${s['net']:.2f}, Vol: {s['vol']:.0f}, Capital: ${s['cap']:.2f}")

    # Top trades
    profitable = sorted([t for t in all_trades if t['net'] > 0], key=lambda x: x['net'], reverse=True)
    if profitable:
        print(f"\n  Top 10 trades:")
        for i, t in enumerate(profitable[:10]):
            r = (t['net'] / t['capital'] * 100) if t['capital'] > 0 else 0
            print(f"    #{i+1:2d} ${t['net']:>7.2f} | {t['volume']:>5.0f} sh | {t['latency']}ms | {r:>5.1f}% ROI | {t['strategy']}")
            print(f"        K: {t['kalshi']}")
            print(f"        P: {t['poly']}")

    # Save
    report = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'scans': [{'label': s['label'], 'duration': s['duration'],
                   'n_trades': len(s['trades']),
                   'net': sum(t['net'] for t in s['trades'])}
                  for s in scans],
        'measured_overlap': measured_overlap,
        'unique_rate': unique_rate,
        'avg_per_scan': {'net': avg_net, 'capital': avg_cap, 'fees': avg_fees, 'trades': avg_filled},
        'projection_24h': {'net': proj_net, 'fees': proj_fees, 'capital': proj_cap,
                           'trades': proj_trades, 'roi_pct': roi},
    }
    out = 'market_data/sim_24h_v2.json'
    os.makedirs('market_data', exist_ok=True)
    with open(out, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Saved to {out}")


asyncio.run(main())
