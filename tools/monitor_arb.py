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


def _merge_watchlists(fresh: list, cached: list, is_alive=None, revalidate=None) -> list:
    """Fresh discovery + previously-known pairs that fell out of the catalogs.

    The PM catalog endpoint (/sampling-markets) silently drops near-resolved
    extreme-priced markets — exactly where durable arb lives — so a verified
    pair must stay watched until its market actually dies, not until the
    catalog forgets it. ``is_alive(pair)`` gates retained-only pairs (None =
    retain unconditionally). ``revalidate(pair)`` re-runs the CURRENT verifier
    over retained-only pairs (fresh pairs already carry a current verdict):
    cached pairs bake their clean/basis-risk labels at discovery time, so
    without this a verifier upgrade never reaches them and a stale "clean"
    survives restarts (None = drop the pair; a dict = refreshed labels)."""
    seen = {(p["ktk"], p["pid"]) for p in fresh}
    merged = list(fresh)
    for p in cached:
        key = (p.get("ktk"), p.get("pid"))
        if key in seen:
            continue
        if is_alive is not None and not is_alive(p):
            continue
        if revalidate is not None:
            p = revalidate(p)
            if p is None:
                continue
        merged.append(p)
        seen.add(key)
    return merged


def _reverify(pair: dict):
    """Refresh a cache-retained pair's verdict under the current verifier
    (module-level so tests can stub it out)."""
    return lp.reverify_pair(pair)


def _kalshi_alive(pair: dict) -> bool:
    """True while the pair's Kalshi market is still open (fail-open on a fetch
    error — dropping a pair on a transient network blip defeats the cache)."""
    d = lp.get(f"{lp.KBASE}/markets/{pair['ktk']}")
    if not d:
        return True
    status = str((d.get("market") or {}).get("status") or "").lower()
    return status in ("", "active", "open", "initialized")


def _load_watchlist(args) -> list:
    approved = None
    if args.allowlist and Path(args.allowlist).exists():
        data = json.loads(Path(args.allowlist).read_text())
        # Allowlist entries carry kalshi+polymarket ids; re-discover to attach
        # live tokens/polarity (cheap relative to the monitor's lifetime).
        approved = {(e["kalshi"], e["polymarket"]) for e in data.get("approved", [])}
        pairs = [p for p in lp.discover() if (p["ktk"], p["pid"]) in approved]
        label = "allowlisted"
    else:
        pairs = lp.discover()
        label = "freshly-discovered verified"

    cache = Path(args.watchlist_cache) if getattr(args, "watchlist_cache", None) else None
    if cache:
        cached = []
        if cache.exists():
            try:
                cached = json.loads(cache.read_text()).get("pairs", [])
            except (json.JSONDecodeError, OSError):
                cached = []
        if approved is not None:
            # Cache retention must not override the operator: a pair removed
            # from the allowlist is dropped even if its market is still alive.
            cached = [p for p in cached if (p.get("ktk"), p.get("pid")) in approved]
        merged = _merge_watchlists(pairs, cached, is_alive=_kalshi_alive,
                                   revalidate=_reverify)
        retained = len(merged) - len(pairs)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": time.time(), "pairs": merged}))
        except OSError as exc:
            print(f"watchlist cache write failed: {exc}", file=sys.stderr)
        print(f"Watch-list: {len(pairs)} {label} pairs"
              f" + {retained} retained from cache", file=sys.stderr)
        return merged

    print(f"Watch-list: {len(pairs)} {label} pairs", file=sys.stderr)
    return pairs


def _start_ws_feed(args, watch):
    """Start the WS book feed (PM public channel; Kalshi when creds exist).
    Failure is non-fatal: the REST sweep covers everything without it."""
    if getattr(args, "no_ws", False) or not watch:
        return None
    try:
        from kalshi_arbitrage import ws_books
        feed = ws_books.WsBookFeed(watch)
        feed.start()
        print(f"WS feed started: {len(feed.token_to_pair)} PM tokens"
              f"{' + kalshi' if feed.kalshi.creds_present() else ' (kalshi dormant: no creds)'}",
              file=sys.stderr)
        return feed
    except Exception as exc:
        print(f"ws feed unavailable: {exc}", file=sys.stderr)
        return None


def _load_screens(args) -> list:
    """Full-venue event-dutch screen: top-of-book over BOTH venues' catalog
    feeds (Kalshi open events + PM neg-risk events), refreshed with the
    watch-list. Returns the 'hot' candidates that get precise fee-aware
    book-walk pricing every poll. Capped to keep per-poll fetches bounded."""
    if getattr(args, "no_event_screens", False):
        return []
    hot = []
    try:
        evs = lp.fetch_kalshi_events()
        hot.extend(lp.kalshi_event_screens(evs))
        hot.extend(lp.kalshi_ladder_screens(evs))
    except Exception as exc:    # screen failure must never kill the monitor
        print(f"kalshi event screen failed: {exc}", file=sys.stderr)
    try:
        hot.extend(lp.pm_event_screens())
    except Exception as exc:
        print(f"pm event screen failed: {exc}", file=sys.stderr)
    hot = [c for c in hot if 2 <= c.get("n", 0) <= 25]   # bounded leg count
    hot.sort(key=lambda c: -c.get("gross", 0.0))
    hot = hot[:12]
    print("Event-dutch screen: " + ("; ".join(
        f"{c['kind']} {c['ev']} (gross ${c['gross']:.2f} x{c['n']})" for c in hot)
        if hot else "no candidates"), file=sys.stderr)
    return hot


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
    ap.add_argument("--no-ws", action="store_true",
                    help="Disable the WebSocket book feed (REST sweep only).")
    ap.add_argument("--no-event-screens", action="store_true",
                    help="Disable the full-venue event-dutch screens (pair "
                         "structures only).")
    ap.add_argument("--watchlist-cache", type=str, default=None,
                    help="Persist verified pairs to this JSON so pairs that drop out "
                         "of the venue catalogs (e.g. PM /sampling-markets sheds "
                         "near-resolved extreme-priced markets) stay watched until "
                         "their Kalshi market actually closes. Survives restarts.")
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
    open_eps: dict = {}      # key -> {start, peak_net, peak_edge, ticks, strategy}
    closed = 0
    captured_total = 0.0
    t_start = time.time()
    last_refresh = t_start
    last_snapshot = t_start
    pairs_by_ktk = {p.get("ktk"): p for p in watch}
    hot = _load_screens(args)   # event-dutch candidates, refreshed with the watch-list
    feed = _start_ws_feed(args, watch)
    us_off = 0                  # round-robin offset over rate-limited US pairs
    # Graceful shutdown: deploy restarts kill the monitor with episodes open in
    # memory — flush them to the ledger instead of losing them. (signal.signal
    # only works on the main thread; tests drive main() in-thread, so guard.)
    stop_flag = {"stop": False}
    try:
        import signal as _signal
        _signal.signal(_signal.SIGTERM, lambda s, f: stop_flag.__setitem__("stop", True))
        _signal.signal(_signal.SIGINT, lambda s, f: stop_flag.__setitem__("stop", True))
    except ValueError:
        pass
    print(f"{'time':<9} {'event':<6} {'net$':>7} {'edge':>6} {'size':>7}  market", flush=True)

    def _pm_books(token):
        """WS-mirror-first PM book fetch (REST on miss/staleness)."""
        if feed is not None:
            m = feed.mirror.get_asks(str(token))
            if m is not None:
                return m
        return lp.pm_asks(token)

    def _price_candidate(c):
        try:
            if c.get("kind") == "ladder":
                return lp.price_kalshi_ladder(c, pairs_by_ktk, args.pm_fee_bps)
            if str(c.get("ev", "")).startswith("pm:"):
                return lp.price_pm_dutch(c, args.pm_fee_bps)
            return lp.price_kalshi_dutch(c, pairs_by_ktk, args.pm_fee_bps)
        except Exception:
            return None

    while not stop_flag["stop"]:
        now = time.time()
        if args.refresh_watchlist and now - last_refresh > args.refresh_watchlist:
            watch = _load_watchlist(args)
            pairs_by_ktk = {p.get("ktk"): p for p in watch}
            hot = _load_screens(args)
            if feed is not None:           # token set changed: rebuild the feed
                feed.stop()
                feed = _start_ws_feed(args, watch)
            last_refresh = now

        # Polymarket US's public tier allows 60 req/min: price at most 15 US
        # pairs per sweep, round-robin, and never spuriously close an episode
        # for a pair that simply wasn't priced this sweep.
        us_watch = [p for p in watch if str(p.get("pid", "")).startswith("us:")]
        rest = [p for p in watch if not str(p.get("pid", "")).startswith("us:")]
        if len(us_watch) > 15:
            sel = [us_watch[(us_off + i) % len(us_watch)] for i in range(15)]
            us_off = (us_off + 15) % len(us_watch)
        else:
            sel = us_watch
        deferred_ktks = {p.get("ktk") for p in us_watch} - {p.get("ktk") for p in sel}

        live = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(
                lambda p: lp.pair_structures(p, args.pm_fee_bps, pm_book_fn=_pm_books),
                rest + sel))
            results.extend([r] for r in ex.map(_price_candidate, hot))
        for rs in results:
            for r in rs or []:
                if (r and r["net"] >= args.min_net_usd and r["net_edge"] >= args.min_net_edge
                        and r["net_edge"] <= args.max_net_edge):
                    live[r.get("key") or r["ktk"]] = r

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        # newly opened / still-open episodes
        for key, r in live.items():
            ep = open_eps.get(key)
            strat = r.get("strategy", "comp")
            if ep is None:
                open_eps[key] = {"start": now, "peak_net": r["net"], "peak_edge": r["net_edge"],
                                 "ticks": 1, "kt": r["kt"], "strategy": strat,
                                 "ktk": r.get("ktk", "")}
                tag = " [BASIS-RISK]" if r.get("uncertain") else ""
                stag = f" <{strat}>" if strat != "comp" else ""
                print(f"{ts:<9} {'OPEN':<6} {r['net']:7.2f} {r['net_edge']*100:5.1f}% "
                      f"{r['size']:7.0f}  {r['kt'][:40]}{tag}{stag}", flush=True)
                if executor is not None and strat == "comp":
                    _fire(r)
            else:
                ep["peak_net"] = max(ep["peak_net"], r["net"])
                ep["peak_edge"] = max(ep["peak_edge"], r["net_edge"])
                ep["ticks"] += 1
        # episodes that just closed (were open, now gone) — except pairs the
        # US rate-limit rotation deferred this sweep (not priced != closed).
        for key in [k for k in open_eps if k not in live
                    and (open_eps[k].get("ktk") or "") not in deferred_ktks]:
            ep = open_eps.pop(key)
            dur = now - ep["start"]
            closed += 1
            captured_total += ep["peak_net"]
            stag = f" <{ep['strategy']}>" if ep.get("strategy", "comp") != "comp" else ""
            print(f"{ts:<9} {'CLOSE':<6} {ep['peak_net']:7.2f} {ep['peak_edge']*100:5.1f}% "
                  f"{'':>7}  {ep['kt'][:40]}{stag} (open {dur/60:.1f}m, {ep['ticks']} ticks)",
                  flush=True)
            _record(args.ledger, {"ts": ts, "ktk": ep.get("ktk") or key, "market": ep["kt"],
                                  "strategy": ep.get("strategy", "comp"), "key": key,
                                  "peak_net": round(ep["peak_net"], 4),
                                  "peak_edge": round(ep["peak_edge"], 5),
                                  "open_minutes": round(dur / 60, 2), "ticks": ep["ticks"]})

        # Periodic time-series snapshot of the CURRENT capturable state — records
        # standing arbs (which never "close") so the dataset accrues continuously.
        if args.snapshot_every and now - last_snapshot >= args.snapshot_every:
            last_snapshot = now
            # Separate clean (risk-free) from basis-risk (uncertain resolution,
            # e.g. arrests) so the headline capturable isn't overstated. The
            # open_net/open_count series keeps its historical meaning (cross-
            # venue complementary only); other structures report by_strategy.
            comp = {k: r for k, r in live.items() if r.get("strategy", "comp") == "comp"}
            other = {k: r for k, r in live.items() if r.get("strategy", "comp") != "comp"}
            clean = {k: r for k, r in comp.items() if not r.get("uncertain")}
            unc = {k: r for k, r in comp.items() if r.get("uncertain")}
            by_strategy = {}
            for k, r in other.items():
                s = by_strategy.setdefault(r["strategy"], {"count": 0, "net": 0.0,
                                                           "net_uncertain": 0.0})
                s["count"] += 1
                s["net" if not r.get("uncertain") else "net_uncertain"] += r["net"]
            for s in by_strategy.values():
                s["net"] = round(s["net"], 4)
                s["net_uncertain"] = round(s["net_uncertain"], 4)
            _record(args.ledger, {
                "ts": ts, "kind": "snapshot", "epoch": round(now, 1),
                "open_count": len(clean),
                "open_net": round(sum(r["net"] for r in clean.values()), 4),
                "open_count_uncertain": len(unc),
                "open_net_uncertain": round(sum(r["net"] for r in unc.values()), 4),
                "by_market": {k: round(r["net"], 2) for k, r in clean.items()},
                "by_market_uncertain": {k: round(r["net"], 2) for k, r in unc.items()},
                "by_strategy": by_strategy,
            })

        if args.duration and now - t_start >= args.duration:
            break
        # Sleep in ~1s slices, draining the WS dirty set: pairs whose books
        # just moved get re-priced immediately (tick-resolution OPENs/peaks;
        # CLOSEs stay sweep-based so episode semantics are unchanged).
        deadline = now + args.interval
        while not stop_flag["stop"]:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if args.duration and time.time() - t_start >= args.duration:
                break
            time.sleep(min(1.0, remaining))
            if feed is None:
                continue
            dirty = feed.dirty_pairs() & set(pairs_by_ktk)
            if not dirty:
                continue
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            for ktk in dirty:
                try:
                    rs = lp.pair_structures(pairs_by_ktk[ktk], args.pm_fee_bps,
                                            pm_book_fn=_pm_books)
                except Exception:
                    continue
                for r in rs or []:
                    if not (r["net"] >= args.min_net_usd
                            and args.min_net_edge <= r["net_edge"] <= args.max_net_edge):
                        continue
                    key = r.get("key") or r["ktk"]
                    live[key] = r
                    ep = open_eps.get(key)
                    if ep is None:
                        open_eps[key] = {"start": time.time(), "peak_net": r["net"],
                                         "peak_edge": r["net_edge"], "ticks": 1,
                                         "kt": r["kt"], "ktk": r.get("ktk", ""),
                                         "strategy": r.get("strategy", "comp")}
                        tag = " [BASIS-RISK]" if r.get("uncertain") else ""
                        print(f"{ts:<9} {'OPEN':<6} {r['net']:7.2f} "
                              f"{r['net_edge']*100:5.1f}% {r['size']:7.0f}  "
                              f"{r['kt'][:40]}{tag} <ws>", flush=True)
                        if executor is not None and r.get("strategy", "comp") == "comp":
                            _fire(r)
                    else:
                        ep["peak_net"] = max(ep["peak_net"], r["net"])
                        ep["peak_edge"] = max(ep["peak_edge"], r["net_edge"])
                        ep["ticks"] += 1

    elapsed = (time.time() - t_start) / 3600.0
    still_open = sum(e["peak_net"] for e in open_eps.values())
    # Flush still-open episodes so bounded runs AND deploy restarts (SIGTERM)
    # are fully recorded.
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    for key, ep in open_eps.items():
        _record(args.ledger, {"ts": ts, "ktk": ep.get("ktk") or key, "market": ep["kt"],
                              "strategy": ep.get("strategy", "comp"), "key": key,
                              "peak_net": round(ep["peak_net"], 4),
                              "peak_edge": round(ep["peak_edge"], 5),
                              "open_minutes": round((time.time() - ep["start"]) / 60, 2),
                              "ticks": ep["ticks"], "still_open": True,
                              "terminated": stop_flag["stop"]})
    if feed is not None:
        feed.stop()
    print(f"\nMonitored {elapsed:.2f}h | episodes closed={closed} open={len(open_eps)} | "
          f"capturable net ~${captured_total + still_open:.2f} "
          f"(best-entry-per-episode, fee-aware @ {args.pm_fee_bps}bps PM)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
