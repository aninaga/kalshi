#!/usr/bin/env python3
"""Analyze a paper-capture ledger from ``monitor_arb --ledger``.

Closes the validation loop: reads the JSONL of captured gap episodes and reports
the session's realized-capturable P&L, episode/duration distribution, per-market
breakdown, and a run-rate projection — so you can see what the machine actually
harvested over a paper session before risking capital.

Usage:
    python -m tools.analyze_ledger --path /tmp/paper.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def load(path: str) -> list:
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def summarize(rows: list) -> dict:
    total = sum(r.get("peak_net", 0) for r in rows)
    durs = [r.get("open_minutes", 0) for r in rows]
    by_market = defaultdict(lambda: {"n": 0, "net": 0.0})
    for r in rows:
        m = by_market[r.get("market", r.get("ktk", "?"))]
        m["n"] += 1
        m["net"] += r.get("peak_net", 0)
    span = sum(durs) / 60.0  # episode-hours of open gap (not wall-clock)
    buckets = defaultdict(int)
    for d in durs:
        b = ("<2m" if d < 2 else "2-15m" if d < 15 else "15-60m" if d < 60 else ">60m")
        buckets[b] += 1
    return {"episodes": len(rows), "total_net": total, "durations": durs,
            "by_market": by_market, "open_hours": span, "buckets": buckets}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default="market_data/paper_ledger.jsonl")
    ap.add_argument("--session-hours", type=float, default=0,
                    help="Wall-clock hours of the session, for a daily run-rate projection.")
    args = ap.parse_args(argv)

    if not Path(args.path).exists():
        print(f"No ledger at {args.path} (run `kalshi-arb monitor --ledger {args.path}`).",
              file=sys.stderr)
        return 1
    all_rows = load(args.path)
    if not all_rows:
        print("Ledger is empty.", file=sys.stderr)
        return 0
    snaps = [r for r in all_rows if r.get("kind") == "snapshot"]
    rows = [r for r in all_rows if r.get("kind") != "snapshot"]

    print(f"=== capture ledger: {args.path} ===")

    # Time-series snapshots: capturable $ at real depth, sampled over time.
    if snaps:
        import statistics
        nets = [float(r.get("open_net") or 0) for r in snaps]           # clean / risk-free
        unc = [float(r.get("open_net_uncertain") or 0) for r in snaps]  # basis-risk (held-for-review)
        counts = [int(r.get("open_count") or 0) for r in snaps]
        span_h = (snaps[-1].get("epoch", 0) - snaps[0].get("epoch", 0)) / 3600.0
        print(f"snapshots={len(snaps)} over {span_h:.1f}h | CLEAN (risk-free) capturable/snapshot: "
              f"mean=${statistics.mean(nets):.2f} median=${statistics.median(nets):.2f} "
              f"max=${max(nets):.2f} | open arbs: mean={statistics.mean(counts):.1f}")
        if any(unc):
            print(f"  basis-risk (uncertain resolution, NOT risk-free): "
                  f"mean=${statistics.mean(unc):.2f} max=${max(unc):.2f} — confirm rules before trusting")
        # Which markets show up most across snapshots (persistent capturable edge).
        from collections import Counter
        seen = Counter()
        for r in snaps:
            for k in (r.get("by_market") or {}):
                seen[k] += 1
        if seen:
            print("  most-persistent capturable markets (snapshot hit count):")
            for k, n in seen.most_common(10):
                print(f"    {n:4d}/{len(snaps)}  {k}")

    if rows:
        s = summarize(rows)
        print(f"\nepisodes={s['episodes']}  capturable net=${s['total_net']:.2f}  "
              f"(sum of best-entry-per-episode, fee-aware)")
        print("episode duration:", "  ".join(f"{k}={v}" for k, v in sorted(s["buckets"].items())))
        print("top markets by captured net:")
        for mkt, agg in sorted(s["by_market"].items(), key=lambda kv: -kv[1]["net"])[:15]:
            print(f"  ${agg['net']:7.2f}  ({agg['n']:2d} episodes)  {mkt[:50]}")
    else:
        s = {"total_net": 0.0}

    if args.session_hours > 0:
        per_day = s["total_net"] / args.session_hours * 24
        print(f"\nrun-rate: ${s['total_net']:.2f} over {args.session_hours:.1f}h "
              f"=> ~${per_day:.0f}/day projected (NB: capital-locked, marquee-event "
              f"dependent — see analysis; a high-event day, not a steady rate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
