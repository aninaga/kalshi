#!/usr/bin/env python3
"""Find genuine, fee-clearing cross-venue arbitrage on the LIVE catalogs.

The operator's bridge from "the matcher says these look alike" to "these are
real, risk-free, fee-positive trades you can allowlist and run." Applies the full
discipline end to end (shared with the monitor and backtest via
``kalshi_arbitrage.live_probe``):

  1. Sweep both live catalogs (PM CLOB sampling + Kalshi event discovery).
  2. Match same-event pairs with CompositeVerifier (matching precision, deadline
     congruence, definitional-divergence flagging).
  3. Fetch SYNCHRONIZED live books and price the complementary arb NET OF REAL
     FEES (the Kalshi 0.07*P*(1-P) curve eats mid-priced gaps).
  4. Bucket: GENUINE (auto-allowlistable) / HELD-FOR-REVIEW (uncertain basis
     risk) / REJECTED (implausible edge ⇒ false match or stale leg).
  5. Emit allowlist-ready entries for MATCH_REQUIRE_ALLOWLIST_FOR_LIVE.

No orders are placed; this only finds. Usage:
    python -m tools.find_live_arb --min-net-edge 0.005 --output /tmp/arb.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-net-edge", type=float, default=0.005, help="Min net edge/$1 (0.5%%).")
    ap.add_argument("--min-net-usd", type=float, default=1.0, help="Min net profit per pair ($1).")
    ap.add_argument("--max-net-edge", type=float, default=0.15,
                    help="Reject edge above this as a false match / stale leg (15%%).")
    ap.add_argument("--pm-fee-bps", type=int,
                    default=getattr(lp.Config, "POLYMARKET_ESTIMATED_FEE_RATE_BPS", 1000))
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--output", type=str, default=None, help="Write allowlist JSON of survivors.")
    args = ap.parse_args()

    t0 = time.time()
    print("Fetching live catalogs...", file=sys.stderr)
    pm, ks = lp.fetch_polymarket(), lp.fetch_kalshi()
    print(f"  PM={len(pm)}  Kalshi-liquid-singles={len(ks)}  ({time.time()-t0:.1f}s)", file=sys.stderr)
    pairs = lp.match_pairs(pm, ks)
    print(f"Verified same-event pairs (matcher+congruence): {len(pairs)}", file=sys.stderr)

    found, review, suspect = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(lambda p: lp.price_pair(p, args.pm_fee_bps), pairs):
            if not r or r["net"] < args.min_net_usd or r["net_edge"] < args.min_net_edge:
                continue
            if r["net_edge"] > args.max_net_edge:
                suspect.append(r)
            elif r.get("uncertain"):
                review.append(r)
            else:
                found.append(r)
    found.sort(key=lambda r: -r["net"])
    review.sort(key=lambda r: -r["net"])
    suspect.sort(key=lambda r: -r["net_edge"])

    print(f"\nGENUINE fee-clearing arb candidates (auto-allowlistable): {len(found)} "
          f"(total net ${sum(r['net'] for r in found):.2f})\n")
    print(f"{'net$':>8} {'edge':>6} {'size':>7} {'capital':>9}  legs  market")
    for r in found[:args.limit]:
        print(f"{r['net']:8.2f} {r['net_edge']*100:5.1f}% {r['size']:7.0f} "
              f"{r['cost']:9.2f}  {r['a_src']}/{r['b_src']}  {r['kt'][:44]}")

    if review:
        print(f"\nHELD FOR REVIEW — plausible edge but UNCERTAIN resolution "
              f"(deadline/definition basis-risk; NOT auto-allowlisted): {len(review)} "
              f"(net ${sum(r['net'] for r in review):.2f})")
        for r in review[:args.limit]:
            print(f"  {r['net']:7.2f} {r['net_edge']*100:5.1f}%  {r['kt'][:46]}")
        print("  -> confirm both venues' rules resolve identically before allowlisting.")

    if suspect:
        print(f"\nREJECTED as implausible (edge > {args.max_net_edge*100:.0f}% => likely "
              f"false match / stale leg): {len(suspect)}")
        for r in suspect[:10]:
            print(f"  {r['net_edge']*100:5.1f}% edge  {r['kt'][:50]} || {r['pt'][:40]}")

    if args.output:
        approved = [{"kalshi": r["ktk"], "polymarket": r["pid"], "polarity": r["polarity"],
                     "_note": f"net=${r['net']:.2f} edge={r['net_edge']*100:.1f}% "
                              f"{r['kt'][:50]} || {r['pt'][:50]}"} for r in found]
        Path(args.output).write_text(json.dumps({"approved": approved, "denied": []}, indent=2))
        print(f"\nWrote {len(approved)} allowlist-ready entries to {args.output}", file=sys.stderr)
        print("Review each before enabling live trading (MATCH_REQUIRE_ALLOWLIST_FOR_LIVE).",
              file=sys.stderr)


if __name__ == "__main__":
    main()
