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
        # Fail LOUD on a corrupt allowlist rather than silently resetting it:
        # a swallowed error here would let the merge below drop every operator
        # approval (review_pairs.py writes them), re-arming pairs a human had
        # adjudicated. Conservative failure direction = refuse to write.
        try:
            existing = json.loads(Path(args.allowlist).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read existing allowlist {args.allowlist}: {exc}\n"
                  f"Refusing to overwrite — operator approvals could be lost. "
                  f"Inspect/repair the file by hand, then re-run.", file=sys.stderr)
            return 2
    denied = existing.get("denied", [])

    # MERGE approved-by-source: the auto-discovered 'genuine' set is UNIONED with
    # the operator approvals already on file (anything whose source isn't this
    # tool's own "auto" — e.g. review_pairs.py's source="review"). Replacing the
    # whole array each run wiped those human approvals. Keyed by (kalshi,
    # polymarket); on a pair present in both, the operator entry wins (it is the
    # more authoritative provenance) and the stale auto entry is dropped.
    def _key(e):
        return (str(e.get("kalshi")), str(e.get("polymarket")))

    operator_approved = [e for e in existing.get("approved", [])
                         if e.get("source") != "auto"]
    operator_keys = {_key(e) for e in operator_approved}
    auto_approved = [
        {"kalshi": r["ktk"], "polymarket": r["pid"], "polarity": r["polarity"],
         "source": "auto",
         "_note": f"net=${r['net']:.2f} edge={r['net_edge']*100:.1f}% "
                  f"{r['kt'][:48]} || {r['pt'][:48]}"}
        for r in found if (str(r["ktk"]), str(r["pid"])) not in operator_keys
    ]
    approved = operator_approved + auto_approved
    Path(args.allowlist).write_text(json.dumps({"approved": approved, "denied": denied}, indent=2))
    print(f"\n=== ALLOWLIST ===\nWrote {len(approved)} approved pairs to {args.allowlist} "
          f"({len(auto_approved)} auto-discovered + {len(operator_approved)} operator-preserved; "
          f"{len(review)} held for review, NOT allowlisted).")
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
