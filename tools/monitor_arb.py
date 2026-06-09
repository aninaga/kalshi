#!/usr/bin/env python3
"""Continuous cross-venue arb capture monitor — the sports-transient engine.

The analysis showed the bulk of a high week's edge comes from TRANSIENT gaps on
liquid markets (Stanley Cup: ~58 net-positive episodes/wk) that open for minutes
and are invisible to a one-shot scan. This monitor is the always-on loop that
harvests them: it polls a watch-list of verified same-event pairs every
``--interval`` seconds, prices each fee-aware (live_probe), and emits an event
the moment a fee-clearing gap opens — tracking each gap as an EPISODE
(open→peak→close) and tallying the capturable net. No orders are placed; this is
the detection/alerting layer that would feed the gated executor.

Watch-list source (in priority): an allowlist JSON (operator-reviewed genuine
pairs), else a fresh full discovery sweep.

Usage:
    python -m tools.monitor_arb --interval 20 --min-net-edge 0.004 --duration 3600
    python -m tools.monitor_arb --allowlist market_data/matching/match_allowlist.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp


def _load_watchlist(args) -> list:
    if args.allowlist and Path(args.allowlist).exists():
        data = json.loads(Path(args.allowlist).read_text())
        # Allowlist entries carry kalshi+polymarket ids; re-discover to attach
        # live tokens/polarity (cheap relative to the monitor's lifetime).
        approved = {(e["kalshi"], e["polymarket"]) for e in data.get("approved", [])}
        pairs = [p for p in lp.discover() if (p["ktk"], p["pid"]) in approved]
        print(f"Watch-list: {len(pairs)} allowlisted pairs", file=sys.stderr)
        return pairs
    pairs = lp.discover()
    print(f"Watch-list: {len(pairs)} freshly-discovered verified pairs", file=sys.stderr)
    return pairs


def _record(ledger_path, rec):
    if not ledger_path:
        return
    with open(ledger_path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=20.0, help="Poll seconds (default 20).")
    ap.add_argument("--min-net-edge", type=float, default=0.004, help="Min net edge/$1 (0.4%%).")
    ap.add_argument("--min-net-usd", type=float, default=0.5, help="Min net profit to report.")
    ap.add_argument("--max-net-edge", type=float, default=0.15, help="Reject implausible (false match).")
    ap.add_argument("--pm-fee-bps", type=int, default=getattr(lp.Config, "POLYMARKET_ESTIMATED_FEE_RATE_BPS", 1000))
    ap.add_argument("--duration", type=float, default=0, help="Seconds to run (0 = until Ctrl-C).")
    ap.add_argument("--allowlist", type=str, default=None)
    ap.add_argument("--ledger", type=str, default=None,
                    help="Append each captured episode (paper fill) as JSONL to this path.")
    ap.add_argument("--snapshot-every", type=float, default=0,
                    help="Also append a time-series SNAPSHOT row (aggregate capturable "
                         "net + per-market) every N seconds — builds a continuous dataset "
                         "even for standing arbs that never close (0 = off).")
    ap.add_argument("--execute", action="store_true",
                    help="Route each newly-opened gap through the ArbitrageExecutor "
                         "(paper unless EXECUTION_MODE=live AND allowlisted AND armed).")
    ap.add_argument("--refresh-watchlist", type=float, default=1800,
                    help="Re-discover the watch-list every N seconds (markets open/close).")
    args = ap.parse_args(argv)

    executor = None
    if args.execute:
        import asyncio
        from kalshi_arbitrage.arbitrage_executor import ArbitrageExecutor
        executor = ArbitrageExecutor()
        _loop = asyncio.new_event_loop()

        def _fire(priced):
            opp = lp.to_opportunity(priced)
            try:
                res = _loop.run_until_complete(executor.execute_arbitrage(opp))
                print(f"           EXEC {opp['opportunity_id']}: "
                      f"{getattr(res, 'status', res)}", flush=True)
            except Exception as exc:  # never let an exec error kill the monitor
                print(f"           EXEC error: {exc}", flush=True)

    watch = _load_watchlist(args)
    if not watch:
        print("Empty watch-list; nothing to monitor.", file=sys.stderr)
        return 0

    import concurrent.futures
    open_eps: dict = {}      # ktk -> {start, peak_net, peak_edge, ticks}
    closed = 0
    captured_total = 0.0
    t_start = time.time()
    last_refresh = t_start
    last_snapshot = t_start
    print(f"{'time':<9} {'event':<6} {'net$':>7} {'edge':>6} {'size':>7}  market", flush=True)

    while True:
        now = time.time()
        if args.refresh_watchlist and now - last_refresh > args.refresh_watchlist:
            watch = _load_watchlist(args)
            last_refresh = now

        live = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            for r in ex.map(lambda p: lp.price_pair(p, args.pm_fee_bps), watch):
                if (r and r["net"] >= args.min_net_usd and r["net_edge"] >= args.min_net_edge
                        and r["net_edge"] <= args.max_net_edge):
                    live[r["ktk"]] = r

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        # newly opened / still-open episodes
        for ktk, r in live.items():
            ep = open_eps.get(ktk)
            if ep is None:
                open_eps[ktk] = {"start": now, "peak_net": r["net"], "peak_edge": r["net_edge"],
                                 "ticks": 1, "kt": r["kt"]}
                tag = " [BASIS-RISK]" if r.get("uncertain") else ""
                print(f"{ts:<9} {'OPEN':<6} {r['net']:7.2f} {r['net_edge']*100:5.1f}% "
                      f"{r['size']:7.0f}  {r['kt'][:40]}{tag}", flush=True)
                if executor is not None:
                    _fire(r)
            else:
                ep["peak_net"] = max(ep["peak_net"], r["net"])
                ep["peak_edge"] = max(ep["peak_edge"], r["net_edge"])
                ep["ticks"] += 1
        # episodes that just closed (were open, now gone)
        for ktk in [k for k in open_eps if k not in live]:
            ep = open_eps.pop(ktk)
            dur = now - ep["start"]
            closed += 1
            captured_total += ep["peak_net"]
            print(f"{ts:<9} {'CLOSE':<6} {ep['peak_net']:7.2f} {ep['peak_edge']*100:5.1f}% "
                  f"{'':>7}  {ep['kt'][:40]} (open {dur/60:.1f}m, {ep['ticks']} ticks)", flush=True)
            _record(args.ledger, {"ts": ts, "ktk": ktk, "market": ep["kt"],
                                  "peak_net": round(ep["peak_net"], 4),
                                  "peak_edge": round(ep["peak_edge"], 5),
                                  "open_minutes": round(dur / 60, 2), "ticks": ep["ticks"]})

        # Periodic time-series snapshot of the CURRENT capturable state — records
        # standing arbs (which never "close") so the dataset accrues continuously.
        if args.snapshot_every and now - last_snapshot >= args.snapshot_every:
            last_snapshot = now
            # Separate clean (risk-free) from basis-risk (uncertain resolution,
            # e.g. arrests) so the headline capturable isn't overstated.
            clean = {k: r for k, r in live.items() if not r.get("uncertain")}
            unc = {k: r for k, r in live.items() if r.get("uncertain")}
            _record(args.ledger, {
                "ts": ts, "kind": "snapshot", "epoch": round(now, 1),
                "open_count": len(clean),
                "open_net": round(sum(r["net"] for r in clean.values()), 4),
                "open_count_uncertain": len(unc),
                "open_net_uncertain": round(sum(r["net"] for r in unc.values()), 4),
                "by_market": {k: round(r["net"], 2) for k, r in clean.items()},
                "by_market_uncertain": {k: round(r["net"], 2) for k, r in unc.items()},
            })

        if args.duration and now - t_start >= args.duration:
            break
        time.sleep(args.interval)

    elapsed = (time.time() - t_start) / 3600.0
    still_open = sum(e["peak_net"] for e in open_eps.values())
    # Flush still-open episodes to the ledger so a bounded run is fully recorded.
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    for ktk, ep in open_eps.items():
        _record(args.ledger, {"ts": ts, "ktk": ktk, "market": ep["kt"],
                              "peak_net": round(ep["peak_net"], 4),
                              "peak_edge": round(ep["peak_edge"], 5),
                              "open_minutes": round((time.time() - ep["start"]) / 60, 2),
                              "ticks": ep["ticks"], "still_open": True})
    print(f"\nMonitored {elapsed:.2f}h | episodes closed={closed} open={len(open_eps)} | "
          f"capturable net ~${captured_total + still_open:.2f} "
          f"(best-entry-per-episode, fee-aware @ {args.pm_fee_bps}bps PM)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
