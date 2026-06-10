#!/usr/bin/env python3
"""Operator review console — clear the held-for-review pile into decisions.

The verifier's uncertain bucket is conservative by design: every flagged pair
is excluded from the clean headline and from the live allowlist until a human
adjudicates it. This console makes that adjudication fast: it walks the
held-for-review pairs (biggest standing money first, using the latest ledger
snapshot), shows both venues' titles + resolution rules side-by-side along
with the EXACT verifier reasons that flagged the pair, and records y/n/skip
into the same allowlist file the monitor and live gate consume.

    python -m tools.review_pairs                       # interactive
    python -m tools.review_pairs --list                # non-interactive dump
    kalshi-arb review --list

Decisions:
  y -> appended to allowlist 'approved' (kalshi/polymarket/polarity)
  n -> appended to allowlist 'denied'  (never auto-resurfaces)
  s/enter -> skip (asked again next session)
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp
from kalshi_arbitrage.matching import CompositeVerifier
from kalshi_arbitrage.utils import clean_title


def _standing_nets(ledger_path: str) -> dict:
    """ktk -> latest standing uncertain net from the newest ledger snapshot."""
    try:
        snaps = [json.loads(l) for l in open(ledger_path)
                 if '"snapshot"' in l]
        if not snaps:
            return {}
        return dict(snaps[-1].get("by_market_uncertain") or {})
    except (OSError, json.JSONDecodeError):
        return {}


def _verdict_for(pair: dict):
    """Re-run the current verifier to get the REASONS (not just the flag)."""
    kt, pt = pair.get("kt") or "", pair.get("pt") or ""
    kd = {"id": pair.get("ktk"), "title": kt, "clean_title": clean_title(kt),
          "raw_data": dict(lp._kalshi_rules(pair.get("ktk") or ""))}
    pd = {"id": pair.get("pid"), "title": pt, "clean_title": clean_title(pt),
          "raw_data": {"description": pair.get("pdesc") or ""}}
    try:
        return CompositeVerifier().verify(kd, pd), kd
    except Exception:
        return None, kd


def _wrap(label: str, text: str, width: int = 96) -> str:
    body = textwrap.fill((text or "(none)").strip(), width=width,
                         initial_indent="    ", subsequent_indent="    ")
    return f"  {label}\n{body}"


def _load_allowlist(path: Path) -> dict:
    if path.exists():
        try:
            d = json.loads(path.read_text())
            d.setdefault("approved", [])
            d.setdefault("denied", [])
            return d
        except (OSError, json.JSONDecodeError):
            pass
    return {"approved": [], "denied": []}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watchlist", default="market_data/shadow/watchlist.json")
    ap.add_argument("--ledger", default="market_data/shadow/ledger.jsonl")
    ap.add_argument("--allowlist", default="market_data/matching/match_allowlist.json")
    ap.add_argument("--list", action="store_true",
                    help="Print the queue (no prompts, no writes).")
    ap.add_argument("--limit", type=int, default=0, help="Review at most N pairs.")
    args = ap.parse_args(argv)

    try:
        pairs = json.loads(Path(args.watchlist).read_text()).get("pairs", [])
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read watchlist: {exc}", file=sys.stderr)
        return 1

    allow_path = Path(args.allowlist)
    allow = _load_allowlist(allow_path)
    decided = {(e.get("kalshi"), e.get("polymarket"))
               for e in allow["approved"] + allow["denied"]}

    nets = _standing_nets(args.ledger)
    queue = sorted(
        (p for p in pairs if p.get("uncertain")
         and (p.get("ktk"), p.get("pid")) not in decided),
        key=lambda p: -nets.get(p.get("ktk"), 0.0))
    if args.limit:
        queue = queue[:args.limit]

    print(f"Held-for-review: {len(queue)} pairs "
          f"(standing ${sum(nets.get(p.get('ktk'), 0.0) for p in queue):,.2f} "
          f"on the latest snapshot)\n")

    n_yes = n_no = 0
    for i, p in enumerate(queue, 1):
        net = nets.get(p.get("ktk"), 0.0)
        verdict, kd = _verdict_for(p)
        print("=" * 100)
        print(f"[{i}/{len(queue)}] standing ${net:,.2f}   {p.get('ktk')}  <->  {p.get('pid')}")
        print(_wrap("KALSHI :", f"{p.get('kt')}"))
        print(_wrap("rules  :", kd["raw_data"].get("rules_primary") or ""))
        print(_wrap("POLYMKT:", f"{p.get('pt')}"))
        print(_wrap("rules  :", (p.get("pdesc") or "")[:700]))
        if verdict is not None:
            flags = [r for r in verdict.reasons
                     if "review" in r or "uncertain" in r or "conflict" in r]
            print(_wrap("flagged:", "; ".join(flags) or
                        "; ".join(verdict.reasons[-2:])))
        if p.get("conflict"):
            print(_wrap("conflict:", f"lower-similarity duplicate on {p['conflict']} "
                                     f"(another pairing of the same market scored higher)"))
        if args.list:
            continue
        try:
            ans = input("  approve as genuine? [y/n/S(kip)/q(uit)] ").strip().lower()
        except EOFError:
            ans = "q"
        if ans == "q":
            break
        if ans == "y":
            allow["approved"].append({"kalshi": p.get("ktk"), "polymarket": p.get("pid"),
                                      "polarity": p.get("polarity"), "source": "review"})
            n_yes += 1
        elif ans == "n":
            allow["denied"].append({"kalshi": p.get("ktk"), "polymarket": p.get("pid"),
                                    "source": "review"})
            n_no += 1

    if not args.list and (n_yes or n_no):
        allow_path.parent.mkdir(parents=True, exist_ok=True)
        allow_path.write_text(json.dumps(allow, indent=2))
        print(f"\nWrote {allow_path}: +{n_yes} approved, +{n_no} denied "
              f"({len(allow['approved'])} approved total).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
