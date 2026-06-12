"""Unified command-line entry point for the Kalshi-Polymarket arb bot.

One front door for every tool so a local run is `kalshi-arb <command>` instead of
hunting for `python -m ...` paths. Imports are lazy (per subcommand) so `--help`
and offline commands stay fast and don't require the heavy deps.

Commands:
  doctor        Check connectivity to both venues (the User-Agent / 403 gotcha).
  scan          Run the analyzer (single or continuous) — full paper pipeline.
  diagnose      Inspect matches / cross-venue economics for a scan.
  backtest      Matching precision/recall gate against a labeled set.
  analyze-paper Summarize a captured paper run (drift, hedge rate, polarity).
  readiness     Live-pilot go/no-go checklist.
  live          Arm / disarm / status the live-trading lock (post-validation).
"""

from __future__ import annotations

import argparse
import sys


def _cmd_doctor(args) -> int:
    """Connectivity self-test: confirms both venues are reachable with our UA."""
    import asyncio
    import aiohttp
    from .config import Config

    checks = [
        ("Kalshi REST markets", f"{Config.KALSHI_API_BASE}/markets?limit=1"),
        ("Kalshi REST events", f"{Config.KALSHI_API_BASE}/events?limit=1"),
        ("Polymarket Gamma", f"{Config.POLYMARKET_GAMMA_BASE}/markets?limit=1"),
        ("Polymarket CLOB sampling", f"{Config.POLYMARKET_CLOB_BASE}/sampling-markets"),
    ]

    async def run() -> int:
        ok_all = True
        # First, the explicit no-UA vs UA contrast on Polymarket so the 403
        # gotcha is visible if it ever regresses.
        pm = f"{Config.POLYMARKET_GAMMA_BASE}/markets?limit=1"
        async with aiohttp.ClientSession() as bare:
            try:
                async with bare.get(pm) as r:
                    print(f"[info] Polymarket Gamma WITHOUT User-Agent -> HTTP {r.status} "
                          f"({'expected 403 — Cloudflare' if r.status == 403 else 'unexpected'})")
            except Exception as e:
                print(f"[info] Polymarket Gamma WITHOUT User-Agent -> error {e}")

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=Config.default_headers()) as s:
            for name, url in checks:
                try:
                    async with s.get(url) as r:
                        good = r.status == 200
                        ok_all = ok_all and good
                        print(f"[{'OK ' if good else 'FAIL'}] {name:28} HTTP {r.status}")
                except Exception as e:
                    ok_all = False
                    print(f"[FAIL] {name:28} {type(e).__name__}: {e}")

        print("\n" + ("✅ All venues reachable." if ok_all else
                      "❌ Some checks failed — see above. (Behind a corporate proxy/VPN? "
                      "Polymarket sits behind Cloudflare.)"))
        return 0 if ok_all else 1

    return asyncio.run(run())


def _cmd_scan(args) -> int:
    """Delegate to the analyzer's argv-based main (kept as the source of truth)."""
    from arbitrage_analyzer import main as analyzer_main
    # Rebuild argv for the analyzer's own parser.
    argv = ["--mode", args.mode, "--completeness", args.completeness]
    if args.interval is not None:
        argv += ["--interval", str(args.interval)]
    if args.max_scans:
        argv += ["--max-scans", str(args.max_scans)]
    if args.threshold is not None:
        argv += ["--threshold", str(args.threshold)]
    if args.similarity is not None:
        argv += ["--similarity", str(args.similarity)]
    old = sys.argv
    sys.argv = ["arbitrage_analyzer"] + argv
    try:
        analyzer_main()
        return 0
    finally:
        sys.argv = old


def _cmd_diagnose(args) -> int:
    import asyncio
    if args.what == "matches":
        from tools.diagnose_matches import main as m
        asyncio.run(m(args.limit))
    elif args.what == "overlap":
        from tools.probe_overlap import main as m
        asyncio.run(m())
    else:  # economics
        from tools.probe_economics import main as m
        asyncio.run(m())
    return 0


def _cmd_backtest(args) -> int:
    from .matching import CompositeVerifier, load_labeled_pairs
    from .validation.matching.gate import MatchingGate
    import os

    if not os.path.exists(args.labeled):
        print(f"No labeled pairs at {args.labeled}. Capture+label some first "
              f"(see kalshi_arbitrage/validation/matching/dataset.py).")
        return 2
    pairs = load_labeled_pairs(args.labeled)
    decision = MatchingGate(min_precision=args.min_precision).evaluate(CompositeVerifier(), pairs)
    print(decision.summary())
    return 0 if decision.passed else 1


def _cmd_analyze_paper(args) -> int:
    from .validation.paper.analyze_paper_run import main as m
    argv = [args.path]
    for flag in ("source", "since", "strategy"):
        val = getattr(args, flag, None)
        if val is not None:
            argv += [f"--{flag}", str(val)]
    return m(argv)


def _cmd_readiness(args) -> int:
    from .validation.pilot.live_readiness_checklist import main as m
    return m([])


def _cmd_live(args) -> int:
    from .execution.live_lock import main as m
    return m([args.action])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kalshi-arb",
        description="Kalshi-Polymarket arbitrage bot — unified CLI.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check venue connectivity (User-Agent / 403)").set_defaults(func=_cmd_doctor)

    s = sub.add_parser("scan", help="run the analyzer (full paper pipeline)")
    s.add_argument("--mode", choices=["single", "continuous"], default="single")
    s.add_argument("--completeness", choices=["FAST", "BALANCED", "LOSSLESS"], default="BALANCED")
    s.add_argument("--interval", type=int, default=None, help="seconds between scans (continuous)")
    s.add_argument("--max-scans", type=int, default=0)
    s.add_argument("--threshold", type=float, default=None, help="min profit threshold")
    s.add_argument("--similarity", type=float, default=None, help="match similarity threshold")
    s.set_defaults(func=_cmd_scan)

    d = sub.add_parser("diagnose", help="inspect matches / economics")
    d.add_argument("what", choices=["matches", "overlap", "economics"], default="matches", nargs="?")
    d.add_argument("--limit", type=int, default=40)
    d.set_defaults(func=_cmd_diagnose)

    b = sub.add_parser("backtest", help="matching precision/recall gate")
    b.add_argument("--labeled", default="market_data/matching/labeled_pairs.jsonl")
    b.add_argument("--min-precision", type=float, default=0.99)
    b.set_defaults(func=_cmd_backtest)

    a = sub.add_parser("analyze-paper", help="summarize a captured paper run")
    a.add_argument("--path", default="market_data/executions/executions.jsonl")
    a.add_argument("--source", default=None,
                   help="filter by confirmation_source (e.g. 'paper' for real paper fills)")
    a.add_argument("--since", type=float, default=None, help="only records ts >= this epoch")
    a.add_argument("--strategy", default=None, help="filter by strategy_type")
    a.set_defaults(func=_cmd_analyze_paper)

    sub.add_parser("readiness", help="live-pilot go/no-go checklist", add_help=False)
    sub.add_parser("reconcile", help="reconcile live fills -> authoritative locked-in/settled P&L", add_help=False)

    lv = sub.add_parser("live", help="arm/disarm/status the live-trading lock")
    lv.add_argument("action", choices=["status", "arm", "disarm"], default="status", nargs="?")
    lv.set_defaults(func=_cmd_live)

    # --- live cross-venue arb tools (share kalshi_arbitrage.live_probe) --- #
    # Registered for top-level --help discoverability; main() routes them to the
    # tool's own argparse BEFORE this parser runs (see _PASSTHROUGH), so flags
    # like "--duration 60" pass through verbatim. Run `kalshi-arb <cmd> --help`.
    sub.add_parser("find-arb", help="find genuine fee-clearing live arb -> allowlist", add_help=False)
    sub.add_parser("monitor", help="continuous fee-aware capture monitor (paper)", add_help=False)
    sub.add_parser("backtest-arb", help="retroactive capturable arb over the past N days", add_help=False)
    sub.add_parser("analyze-ledger", help="summarize a monitor --ledger paper session", add_help=False)
    sub.add_parser("machine", help="one-command: discover -> allowlist -> paper-harvest", add_help=False)
    sub.add_parser("review", help="walk held-for-review pairs -> allow/deny decisions", add_help=False)

    return p


# Passthrough subcommands → their tool's main(argv). Routed BEFORE argparse so
# the tool's own flags (incl. leading "--opt") reach it verbatim — argparse
# REMAINDER drops a leading optional, so we bypass it for these.
_PASSTHROUGH = {
    "find-arb": "tools.find_live_arb",
    "monitor": "tools.monitor_arb",
    "backtest-arb": "tools.backtest_arb",
    "machine": "tools.run_machine",
    "review": "tools.review_pairs",
    "analyze-ledger": "tools.analyze_ledger",
    "readiness": "kalshi_arbitrage.validation.pilot.live_readiness_checklist",
    "reconcile": "kalshi_arbitrage.reconcile",
}


def main(argv=None) -> int:
    import sys as _sys
    argv = _sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in _PASSTHROUGH:
        import importlib
        return importlib.import_module(_PASSTHROUGH[argv[0]]).main(argv[1:]) or 0
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
