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


def _episode_key(r: dict):
    """Stable identity for an episode across restarts. ``key`` is the monitor's
    structure key; fall back to ktk/market so older rows (pre run_id) still
    group. The restart-duplication bug re-flushes the SAME standing arb under
    the SAME key from a fresh process, so keying on this (not run_id) is what
    lets us collapse the duplicates."""
    return r.get("key") or r.get("ktk") or r.get("market") or "?"


def _dedup_still_open(rows: list) -> list:
    """Collapse restart-duplicated still_open flushes.

    ``open_eps`` lives only in monitor process memory, so every restart re-opens
    each standing arb and, on the next shutdown, flushes it again as a fresh
    still_open row. Summing peak_net over all of them counts the same standing
    position once per restart (the 42-duplicates-of-21-markets bug). Keep ONE
    row per episode key — the max peak_net of the chain — so a standing arb is
    counted once, not once per restart."""
    best: dict = {}
    for r in rows:
        k = _episode_key(r)
        cur = best.get(k)
        if cur is None or r.get("peak_net", 0) > cur.get("peak_net", 0):
            best[k] = r
    return list(best.values())


def _bucket(d: float) -> str:
    return "<2m" if d < 2 else "2-15m" if d < 15 else "15-60m" if d < 60 else ">60m"


def _by_market(rows: list) -> dict:
    agg = defaultdict(lambda: {"n": 0, "net": 0.0})
    for r in rows:
        m = agg[r.get("market", r.get("ktk", "?"))]
        m["n"] += 1
        m["net"] += r.get("peak_net", 0)
    return agg


def summarize(rows: list) -> dict:
    """Honest breakdown of capture episodes.

    Splits the ledger into three NON-overlapping buckets so the headline number
    is never a fiction:

      * ``clean_closed``  — genuinely closed (not still_open) AND not basis-risk.
                            The ONLY bucket that is realized, risk-free capturable.
      * ``basis_risk``    — uncertain resolution (e.g. arrest markets); held for
                            review, never risk-free, never projected.
      * ``standing``      — still_open at shutdown; re-flushed every restart, so
                            DEDUPED per episode key. Reported as a mass like a
                            snapshot, never as realized P&L and never projected.

    ``total_net`` / ``episodes`` / ``by_market`` / ``buckets`` keep their old
    raw-sum meaning (back-compat with callers/tests) but the projection in
    ``main`` is driven off ``clean_closed`` only.
    """
    still_open = [r for r in rows if r.get("still_open")]
    terminal = [r for r in rows if not r.get("still_open")]

    standing = _dedup_still_open(still_open)
    standing_clean = [r for r in standing if not r.get("uncertain")]
    standing_unc = [r for r in standing if r.get("uncertain")]

    clean_closed = [r for r in terminal if not r.get("uncertain")]
    basis_risk_closed = [r for r in terminal if r.get("uncertain")]

    durs = [r.get("open_minutes", 0) for r in rows]
    buckets = defaultdict(int)
    for d in durs:
        buckets[_bucket(d)] += 1

    return {
        # --- back-compat (raw, undeduped) ---
        "episodes": len(rows),
        "total_net": sum(r.get("peak_net", 0) for r in rows),
        "durations": durs,
        "by_market": _by_market(rows),
        "open_hours": sum(durs) / 60.0,
        "buckets": buckets,
        # --- honest, deduped, non-overlapping buckets ---
        "clean_closed": {
            "n": len(clean_closed),
            "net": sum(r.get("peak_net", 0) for r in clean_closed),
            "by_market": _by_market(clean_closed),
        },
        "basis_risk": {
            "n": len(basis_risk_closed) + len(standing_unc),
            "net": sum(r.get("peak_net", 0) for r in basis_risk_closed)
            + sum(r.get("peak_net", 0) for r in standing_unc),
        },
        "standing": {
            # deduped: one row per episode key, max peak_net of the chain.
            "n_raw": len(still_open),
            "n": len(standing),
            "net": sum(r.get("peak_net", 0) for r in standing_clean),
            "net_uncertain": sum(r.get("peak_net", 0) for r in standing_unc),
        },
    }


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
        cc = s["clean_closed"]
        br = s["basis_risk"]
        st = s["standing"]
        print(f"\nepisode rows={s['episodes']}")
        # The ONLY honest realized number: genuinely closed AND risk-free.
        print(f"  CLEAN-CLOSED (realized, risk-free): {cc['n']} episodes, "
              f"${cc['net']:.2f}")
        # Basis-risk: held-for-review, NOT risk-free, never projected.
        print(f"  BASIS-RISK (uncertain resolution, NOT risk-free): {br['n']} "
              f"episodes, ${br['net']:.2f} — confirm rules before trusting")
        # Standing (still_open): deduped per episode key so restart-duplicated
        # flushes of the same standing arb count once, reported as MASS not P&L.
        if st["n_raw"]:
            print(f"  STANDING (still-open mass; {st['n_raw']} raw flushes "
                  f"deduped to {st['n']} positions): ${st['net']:.2f} clean"
                  + (f" + ${st['net_uncertain']:.2f} basis-risk" if st["net_uncertain"] else "")
                  + " — open, not captured; never projected")
        print("episode duration:", "  ".join(f"{k}={v}" for k, v in sorted(s["buckets"].items())))
        print("top CLEAN-CLOSED markets by captured net:")
        cc_by_market = cc["by_market"]
        if cc_by_market:
            for mkt, agg in sorted(cc_by_market.items(), key=lambda kv: -kv[1]["net"])[:15]:
                print(f"  ${agg['net']:7.2f}  ({agg['n']:2d} episodes)  {mkt[:50]}")
        else:
            print("  (none — no genuinely closed, risk-free episodes)")
    else:
        s = {"clean_closed": {"net": 0.0}}

    if args.session_hours > 0:
        # NEVER project from the raw peak_net sum (it double-counts restart-
        # duplicated still_open flushes and launders basis-risk into the
        # headline). Project ONLY from clean-closed realized capture.
        clean_net = s["clean_closed"]["net"]
        per_day = clean_net / args.session_hours * 24
        print(f"\nrun-rate (CLEAN-CLOSED only): ${clean_net:.2f} over "
              f"{args.session_hours:.1f}h => ~${per_day:.0f}/day projected "
              f"(NB: capital-locked, marquee-event dependent — a high-event day, "
              f"not a steady rate; basis-risk and standing positions excluded).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
