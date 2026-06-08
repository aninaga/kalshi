#!/usr/bin/env python3
"""Retroactive cross-venue arb backtest — what was capturable over the past N days.

Productionizes the analysis: for each verified same-event pair, pulls minute/hour
price history from both venues (Kalshi candlesticks, day-chunked; Polymarket
prices-history), reconstructs the fee-aware complementary edge over time, and
reports the capturable net per pair (best entry — these lock to resolution, so
you enter each market ONCE at its widest fee-clearing gap). Splits genuine
(auto-allowlistable) from held-for-review (uncertain) and rejects implausible
(>max edge) false matches. Reports PM-fee sensitivity (0% real taker vs the
conservative curve), since that is the swing variable.

Caveats baked into the output: an UPPER BOUND — hourly/minute closes and PM mid
prices slightly overstate vs live asks; current depth is a proxy for historical
depth (unavailable); excludes gas/operational cost.

Usage:
    python -m tools.backtest_arb --days 7 --interval 60
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp
from kalshi_arbitrage.matching import INVERTED


def k_history(ticker: str, days: int, interval_min: int) -> dict:
    """Kalshi 1-min/1-hr candles, day-chunked. -> {minute_bucket: (yes_ask, no_ask)}."""
    series, ser, now = {}, ticker.split("-")[0], int(time.time())
    chunk = 86400 if interval_min <= 1 else days * 86400  # 1-min capped to 1-day windows
    t = now - days * 86400
    while t < now:
        end = min(t + chunk, now)
        c = lp.get(f"{lp.KBASE}/series/{ser}/markets/{ticker}/candlesticks"
                   f"?start_ts={t}&end_ts={end}&period_interval={interval_min}")
        for cd in (c or {}).get("candlesticks", []):
            ya = cd.get("yes_ask", {}).get("close_dollars")
            yb = cd.get("yes_bid", {}).get("close_dollars")
            if ya and yb:
                series[cd["end_period_ts"] // (interval_min * 60)] = (float(ya), 1.0 - float(yb))
        t = end
    return series


def pm_history(token: str, days: int, interval_min: int) -> dict:
    now = int(time.time())
    h = lp.get(f"{lp.PM_CLOB}/prices-history?market={token}"
               f"&startTs={now - days * 86400}&endTs={now}&fidelity={interval_min}")
    return {pt["t"] // (interval_min * 60): float(pt["p"]) for pt in (h or {}).get("history", [])}


def backtest_pair(pair: dict, days: int, interval_min: int, pm_flat: float) -> dict | None:
    km = k_history(pair["ktk"], days, interval_min)
    if not km:
        return None
    toks = {str(t.get("outcome", "")).lower(): t["token_id"] for t in pair["tokens"]}
    yes_t = toks.get("yes") or pair["tokens"][0]["token_id"]
    no_t = toks.get("no") or pair["tokens"][1]["token_id"]
    pa, pb = pm_history(yes_t, days, interval_min), pm_history(no_t, days, interval_min)
    inv = pair["polarity"] == INVERTED
    best_edge = 0.0
    for m, (kya, kna) in km.items():
        A = pa.get(m) if not inv else pb.get(m)
        B = pb.get(m) if not inv else pa.get(m)
        if A is None or B is None:
            continue
        # cheapest A/B source; Kalshi leg pays the fee curve, PM leg the flat.
        ca, a_k = (kya, True) if kya <= A else (A, False)
        cb, b_k = (kna, True) if kna <= B else (B, False)
        if a_k == b_k:  # need cross-venue
            continue
        fee = (lp.kalshi_fee_per_contract(ca) if a_k else ca * pm_flat) \
            + (lp.kalshi_fee_per_contract(cb) if b_k else cb * pm_flat)
        edge = (1.0 - ca - cb) - fee
        best_edge = max(best_edge, edge)
    # size proxy: current live depth at the (current) best entry
    priced = lp.price_pair(pair, int(pm_flat * 200000) if pm_flat else 0)
    size = priced["size"] if priced else 0
    return {**pair, "best_edge": best_edge, "size": size, "net": best_edge * size}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--interval", type=int, default=60, help="Candle minutes (1 or 60).")
    ap.add_argument("--pm-flat", type=float, default=0.0,
                    help="PM flat taker rate (0.0 = real today; 0.02 = conservative).")
    ap.add_argument("--max-net-edge", type=float, default=0.15)
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    print(f"Discovering verified pairs...", file=sys.stderr)
    pairs = lp.discover()
    print(f"  {len(pairs)} pairs; pulling {args.days}d history "
          f"@ {args.interval}min...", file=sys.stderr)

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(lambda p: backtest_pair(p, args.days, args.interval, args.pm_flat), pairs):
            if r and r["best_edge"] > 0 and r["net"] > 0:
                rows.append(r)

    genuine = [r for r in rows if r["best_edge"] <= args.max_net_edge and not r["uncertain"]]
    review = [r for r in rows if r["best_edge"] <= args.max_net_edge and r["uncertain"]]
    suspect = [r for r in rows if r["best_edge"] > args.max_net_edge]
    for grp in (genuine, review):
        grp.sort(key=lambda r: -r["net"])

    print(f"\n=== {args.days}d capturable (PM flat {args.pm_flat*100:.0f}%, UPPER BOUND) ===")
    print(f"GENUINE (auto-allowlistable): {len(genuine)} pairs, "
          f"Σ ~${sum(r['net'] for r in genuine):.0f}")
    for r in genuine[:args.limit]:
        print(f"  ${r['net']:7.0f}  edge {r['best_edge']*100:5.1f}%  size {r['size']:6.0f}  {r['kt'][:44]}")
    print(f"\nHELD FOR REVIEW (basis-risk, confirm rules): {len(review)} pairs, "
          f"Σ ~${sum(r['net'] for r in review):.0f}")
    for r in review[:15]:
        print(f"  ${r['net']:7.0f}  edge {r['best_edge']*100:5.1f}%  {r['kt'][:48]}")
    print(f"\nRejected as implausible (false match / stale): {len(suspect)}")


if __name__ == "__main__":
    main()
