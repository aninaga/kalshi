#!/usr/bin/env python3
"""One-command orchestrator: run the cross-venue arb machine (paper-safe).

Ties the operating triad together in a single entrypoint:
  1. DISCOVER  — sweep both live catalogs, verify same-event, price fee-aware,
                 bucket into genuine / held-for-review / rejected.
  2. ALLOWLIST — write the genuine, fee-clearing, congruent pairs to the
                 operator allowlist (live trading needs it; review-bucket pairs
                 are deliberately excluded).
  3. HARVEST   — optionally launch the continuous capture monitor in paper mode
                 with a JSONL ledger, so detected gap episodes are recorded.

Nothing here places real orders. It produces the allowlist + a session plan; the
monitor records simulated (fee-aware) captures. Going live remains a deliberate,
separate step (populate creds, validate the paper ledger, `kalshi-arb live arm`).

Usage:
    python -m tools.run_machine --allowlist market_data/matching/match_allowlist.json
    python -m tools.run_machine --monitor --duration 3600 --ledger /tmp/paper.jsonl
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp


def discover_buckets(pm_fee_bps: int, min_net_usd: float, min_net_edge: float, max_net_edge: float):
    pairs = lp.discover()
    found, review, suspect = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(lambda p: lp.price_pair(p, pm_fee_bps), pairs):
            if not r or r["net"] < min_net_usd or r["net_edge"] < min_net_edge:
                continue
            if r["net_edge"] > max_net_edge:
                suspect.append(r)
            elif r.get("uncertain"):
                review.append(r)
            else:
                found.append(r)
    found.sort(key=lambda r: -r["net"])
    review.sort(key=lambda r: -r["net"])
    return pairs, found, review, suspect


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--allowlist", type=str, default="market_data/matching/match_allowlist.json",
                    help="Where to write the genuine-pair allowlist.")
    ap.add_argument("--pm-fee-bps", type=int,
                    default=getattr(lp.Config, "POLYMARKET_ESTIMATED_FEE_RATE_BPS", 1000))
    ap.add_argument("--min-net-edge", type=float, default=0.005)
    ap.add_argument("--min-net-usd", type=float, default=1.0)
    ap.add_argument("--max-net-edge", type=float, default=0.15)
    ap.add_argument("--monitor", action="store_true", help="After allowlisting, run the paper monitor.")
    ap.add_argument("--duration", type=float, default=0, help="Monitor seconds (with --monitor).")
    ap.add_argument("--ledger", type=str, default=None, help="Monitor paper-fill ledger (JSONL).")
    args = ap.parse_args(argv)

    print("=== DISCOVER ===", file=sys.stderr)
    pairs, found, review, suspect = discover_buckets(
        args.pm_fee_bps, args.min_net_usd, args.min_net_edge, args.max_net_edge)
    gen_net = sum(r["net"] for r in found)
    gen_cap = sum(r["cost"] for r in found)
    print(f"verified={len(pairs)}  genuine={len(found)} (net ${gen_net:.2f}, "
          f"capital ${gen_cap:.0f})  review={len(review)}  rejected={len(suspect)}")
    for r in found[:20]:
        print(f"  ${r['net']:7.2f}  {r['net_edge']*100:4.1f}%  {r['kt'][:50]}")

    # ALLOWLIST — genuine only; review/rejected excluded by design.
    Path(args.allowlist).parent.mkdir(parents=True, exist_ok=True)
    existing = {"approved": [], "denied": []}
    if Path(args.allowlist).exists():
        try:
            existing = json.loads(Path(args.allowlist).read_text())
        except (OSError, json.JSONDecodeError):
            pass
    denied = existing.get("denied", [])
    approved = [{"kalshi": r["ktk"], "polymarket": r["pid"], "polarity": r["polarity"],
                 "_note": f"net=${r['net']:.2f} edge={r['net_edge']*100:.1f}% "
                          f"{r['kt'][:48]} || {r['pt'][:48]}"} for r in found]
    Path(args.allowlist).write_text(json.dumps({"approved": approved, "denied": denied}, indent=2))
    print(f"\n=== ALLOWLIST ===\nWrote {len(approved)} genuine pairs to {args.allowlist} "
          f"({len(review)} held for review, NOT allowlisted).")
    if review:
        print("Review these (confirm both venues resolve identically) before adding:")
        for r in review[:10]:
            print(f"  ${r['net']:6.2f}  {r['kt'][:54]}")

    if not args.monitor:
        print("\nNext: `--monitor` to paper-harvest, or `kalshi-arb live status` when ready to arm.")
        return 0

    print("\n=== HARVEST (paper) ===", file=sys.stderr)
    from tools.monitor_arb import main as monitor_main
    mon_argv = ["--allowlist", args.allowlist, "--pm-fee-bps", str(args.pm_fee_bps)]
    if args.duration:
        mon_argv += ["--duration", str(args.duration)]
    if args.ledger:
        mon_argv += ["--ledger", args.ledger]
    return monitor_main(mon_argv)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
