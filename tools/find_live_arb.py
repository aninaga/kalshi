#!/usr/bin/env python3
"""Find genuine, fee-clearing cross-venue arbitrage on the LIVE catalogs.

This is the operator's bridge from "the matcher says these look alike" to "these
are real, risk-free, fee-positive trades you can allowlist and run." It applies,
end to end, exactly the discipline the analysis established is required to reach
a real (small) edge instead of the dashboard's fantasy:

  1. Sweep both live catalogs (Polymarket CLOB sampling-markets + Kalshi event
     discovery — the bulk Kalshi listing is ~all parlays).
  2. Match same-event pairs with the production matcher + CompositeVerifier,
     which now includes the ResolutionCongruenceVerifier (rejects deadline /
     basis-risk lookalikes like "before Sep 1" vs "by Dec 31").
  3. Fetch SYNCHRONIZED live order books for each verified pair (the artifact
     killer — unsynchronized books manufacture phantom edges).
  4. Price the complementary arb (buy outcome-A + outcome-B across venues for
     < $1) NET OF REAL FEES — the Kalshi taker curve 0.07*P*(1-P) is the
     dominant cost and eats every mid-priced gap, which is why only extreme-
     priced or wide gaps survive. Uses the repo FeeModel.
  5. Rank survivors and emit allowlist-ready entries.

Pure-stdlib HTTP (works behind the env's network policy with a browser UA), so
it runs without the full async stack. No orders are placed; this only *finds*.

Usage:
    python -m tools.find_live_arb --min-net-edge 0.005 --output /tmp/arb.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.matching import CompositeVerifier, INVERTED
from kalshi_arbitrage.mock_execution import FeeModel
from kalshi_arbitrage.utils import _BOILERPLATE, clean_title, get_similarity_score

UA = {"User-Agent": Config.HTTP_USER_AGENT if getattr(Config, "HTTP_USER_AGENT", None)
      else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
PM_CLOB = "https://clob.polymarket.com"


def _get(url: str, timeout: int = 25):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Catalog fetch                                                               #
# --------------------------------------------------------------------------- #

def fetch_polymarket() -> List[Dict]:
    """Orderbook-enabled PM markets: question, description (rules), tokens."""
    out, cur = [], ""
    while True:
        d = _get(f"{PM_CLOB}/sampling-markets" + (f"?next_cursor={cur}" if cur else ""))
        if not d:
            break
        rows = d.get("data", []) if isinstance(d, dict) else d
        out.extend(rows)
        cur = d.get("next_cursor", "") if isinstance(d, dict) else ""
        if not cur or cur == "LTE=" or len(out) > 9000:
            break
    return [m for m in out if m.get("active") and not m.get("closed") and len(m.get("tokens") or []) == 2]


def fetch_kalshi() -> List[Dict]:
    """Liquid single Kalshi markets via event discovery (skips parlays)."""
    evs, cur, pages = [], "", 0
    while pages < 45:
        d = _get(f"{KBASE}/events?limit=200&status=open&with_nested_markets=true"
                 + (f"&cursor={cur}" if cur else ""))
        if not d:
            break
        evs.extend(d.get("events", []))
        cur = d.get("cursor", "")
        pages += 1
        if not cur or not d.get("events"):
            break

    def f(m, k):
        try:
            return float(m.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    out = []
    for e in evs:
        for m in (e.get("markets") or []):
            tk = m.get("ticker", "")
            if any(x in tk for x in ("MULTIGAME", "CROSSCATEGORY")):
                continue
            liquid = (f(m, "yes_bid_dollars") > 0 and f(m, "yes_ask_dollars") > 0) \
                or f(m, "volume_fp") > 0 or f(m, "open_interest_fp") > 0
            if liquid:
                out.append(m)
    return out


# --------------------------------------------------------------------------- #
#  Order books (synchronized, migrated schema)                                #
# --------------------------------------------------------------------------- #

def _parse_levels(arr):
    out = []
    for p, s in arr or []:
        pf, sf = float(p), float(s)
        if pf > 1.5:  # legacy cents
            pf /= 100.0
        out.append((pf, sf))
    return out


def kalshi_book(ticker: str) -> Tuple[list, list]:
    """(yes_ask_ladder, no_ask_ladder) ascending by price. yes_ask = 1 - no_bid."""
    ob = _get(f"{KBASE}/markets/{ticker}/orderbook?depth=100")
    fp = (ob or {}).get("orderbook_fp") or {}
    yb = _parse_levels(fp.get("yes_dollars"))
    nb = _parse_levels(fp.get("no_dollars"))
    yes_asks = sorted((1 - p, s) for p, s in nb)
    no_asks = sorted((1 - p, s) for p, s in yb)
    return yes_asks, no_asks


def pm_asks(token: str) -> list:
    b = _get(f"{PM_CLOB}/book?token_id={token}")
    return sorted((float(a["price"]), float(a["size"])) for a in (b or {}).get("asks", []))


# --------------------------------------------------------------------------- #
#  Matching                                                                    #
# --------------------------------------------------------------------------- #

def match_pairs(pm: List[Dict], ks: List[Dict]) -> List[Dict]:
    """Verified same-event (Kalshi, PM) pairs via similarity + CompositeVerifier."""
    pmp = []
    for m in pm:
        ct = clean_title(m.get("question") or "")
        pmp.append({
            "id": m.get("condition_id"), "title": m.get("question") or "", "clean": ct,
            "tokens": m.get("tokens"), "desc": m.get("description") or "",
            "close": m.get("end_date_iso"),
            "terms": {t for t in ct.split() if len(t) >= 3} - _BOILERPLATE,
        })
    idx = defaultdict(set)
    for i, p in enumerate(pmp):
        for t in p["terms"]:
            idx[t].add(i)

    verifier = CompositeVerifier()
    pairs, seen = [], set()
    for m in ks:
        title = m.get("title") or ""
        if not title:
            continue
        ct = clean_title(title)
        kterms = {t for t in ct.split() if len(t) >= 3} - _BOILERPLATE
        cand = defaultdict(int)
        for t in kterms:
            for j in idx.get(t, ()):
                cand[j] += 1
        kd = {"id": m.get("ticker"), "title": title, "clean_title": ct,
              "close_time": m.get("close_time"),
              "raw_data": {"yes_sub_title": m.get("yes_sub_title", ""),
                           "no_sub_title": m.get("no_sub_title", ""),
                           "rules_primary": m.get("rules_primary", ""),
                           "rules_secondary": m.get("rules_secondary", "")}}
        for j, sc in sorted(cand.items(), key=lambda x: -x[1])[:8]:
            if sc < 2:
                continue
            p = pmp[j]
            key = (m.get("ticker"), p["id"])
            if key in seen or get_similarity_score(ct, p["clean"]) < Config.SIMILARITY_THRESHOLD:
                continue
            pd = {"id": p["id"], "title": p["title"], "clean_title": p["clean"],
                  "close_time": p["close"], "raw_data": {"description": p["desc"]}}
            verdict = verifier.verify(kd, pd)
            if not verdict.passed:
                continue
            seen.add(key)
            pairs.append({"ktk": m.get("ticker"), "kt": title, "pt": p["title"],
                          "tokens": p["tokens"], "pid": p["id"],
                          "polarity": verdict.polarity, "uncertain": verdict.uncertain})
    return pairs


# --------------------------------------------------------------------------- #
#  Economics (fee-aware complementary arb)                                     #
# --------------------------------------------------------------------------- #

def _walk(A: list, B: list, pm_fee_bps: int, kalshi_in_A: bool, kalshi_in_B: bool):
    """Walk both ask ladders; accumulate while the per-contract trade is net-
    positive AFTER fees. Returns (size, cost, net_profit, avg_a, avg_b)."""
    qa = [list(x) for x in A]
    qb = [list(x) for x in B]
    ia = ib = 0
    n = ca = cb = 0.0
    while ia < len(qa) and ib < len(qb):
        pa, sa = qa[ia]
        pb, sb = qb[ib]
        step = min(sa, sb)
        if step <= 0:
            ia += sa <= 0
            ib += sb <= 0
            continue
        # marginal fee on this step
        kf = (FeeModel.kalshi_taker_fee(pa, step) if kalshi_in_A else 0.0) \
            + (FeeModel.kalshi_taker_fee(pb, step) if kalshi_in_B else 0.0)
        pf = (FeeModel.polymarket_taker_fee(pa, step, pm_fee_bps) if not kalshi_in_A else 0.0) \
            + (FeeModel.polymarket_taker_fee(pb, step, pm_fee_bps) if not kalshi_in_B else 0.0)
        marginal = (1.0 - pa - pb) * step - kf - pf
        if marginal <= 0:
            break
        n += step
        ca += pa * step
        cb += pb * step
        qa[ia][1] -= step
        qb[ib][1] -= step
        if qa[ia][1] <= 1e-9:
            ia += 1
        if qb[ib][1] <= 1e-9:
            ib += 1
    if n <= 0:
        return None
    kf = (FeeModel.kalshi_taker_fee(ca / n, n) if kalshi_in_A else 0.0) \
        + (FeeModel.kalshi_taker_fee(cb / n, n) if kalshi_in_B else 0.0)
    pf = (FeeModel.polymarket_taker_fee(ca / n, n, pm_fee_bps) if not kalshi_in_A else 0.0) \
        + (FeeModel.polymarket_taker_fee(cb / n, n, pm_fee_bps) if not kalshi_in_B else 0.0)
    net = (n - ca - cb) - kf - pf
    return n, ca + cb, net, ca / n, cb / n


def price_pair(p: Dict, pm_fee_bps: int) -> Optional[Dict]:
    kyes, kno = kalshi_book(p["ktk"])
    toks = {str(t.get("outcome", "")).lower(): t["token_id"] for t in p["tokens"]}
    yes_t = toks.get("yes") or p["tokens"][0]["token_id"]
    no_t = toks.get("no") or p["tokens"][1]["token_id"]
    pyes, pno = pm_asks(yes_t), pm_asks(no_t)
    inv = p["polarity"] == INVERTED
    pm_A, pm_B = (pno, pyes) if inv else (pyes, pno)  # A = Kalshi-YES outcome
    ka = kyes[0][0] if kyes else 9
    pa = pm_A[0][0] if pm_A else 9
    kb = kno[0][0] if kno else 9
    pb = pm_B[0][0] if pm_B else 9
    a_is_k = ka <= pa
    b_is_k = kb <= pb
    if a_is_k == b_is_k:  # need one leg per venue for a CROSS-venue arb
        return None
    A = kyes if a_is_k else pm_A
    B = kno if b_is_k else pm_B
    if not A or not B:
        return None
    res = _walk(A, B, pm_fee_bps, a_is_k, b_is_k)
    if not res:
        return None
    n, cost, net, avg_a, avg_b = res
    return {**p, "size": n, "cost": cost, "net": net,
            "net_edge": net / cost if cost else 0,
            "a_src": "K" if a_is_k else "P", "b_src": "K" if b_is_k else "P"}


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-net-edge", type=float, default=0.005,
                    help="Min net edge per $1 of cost after fees (default 0.5%%).")
    ap.add_argument("--min-net-usd", type=float, default=1.0,
                    help="Min total net profit per pair (default $1).")
    ap.add_argument("--max-net-edge", type=float, default=0.15,
                    help="Reject net edge above this as a near-certain false match / "
                         "stale book (default 15%%). Genuine same-event arb on identical "
                         "contracts is sub-15%%; a 30-60%% 'edge' means different "
                         "propositions ('most goals' vs 'a goal') or a stale leg.")
    ap.add_argument("--pm-fee-bps", type=int,
                    default=getattr(Config, "POLYMARKET_ESTIMATED_FEE_RATE_BPS", 1000),
                    help="Polymarket fee bps for the curve piece.")
    ap.add_argument("--limit", type=int, default=40, help="Max candidates to print.")
    ap.add_argument("--output", type=str, default=None,
                    help="Write allowlist-ready JSON of survivors to this path.")
    args = ap.parse_args()

    t0 = time.time()
    print("Fetching live catalogs...", file=sys.stderr)
    pm, ks = fetch_polymarket(), fetch_kalshi()
    print(f"  PM={len(pm)}  Kalshi-liquid-singles={len(ks)}  ({time.time()-t0:.1f}s)", file=sys.stderr)

    pairs = match_pairs(pm, ks)
    print(f"Verified same-event pairs (matcher+congruence): {len(pairs)}", file=sys.stderr)

    import concurrent.futures
    found, suspect, review = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(lambda p: price_pair(p, args.pm_fee_bps), pairs):
            if not r or r["net"] < args.min_net_usd or r["net_edge"] < args.min_net_edge:
                continue
            if r["net_edge"] > args.max_net_edge:
                suspect.append(r)          # implausible → likely false match
            elif r.get("uncertain"):
                review.append(r)           # basis-risk / ambiguous → human/LLM
            else:
                found.append(r)            # genuine, auto-allowlistable
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
        print("  → confirm both venues' rules resolve identically before allowlisting "
              "(or enable the LLM tiebreaker).")

    if suspect:
        print(f"\nREJECTED as implausible (edge > {args.max_net_edge*100:.0f}% → likely "
              f"false match / stale leg, NOT arb): {len(suspect)}")
        for r in suspect[:10]:
            print(f"  {r['net_edge']*100:5.1f}% edge  {r['kt'][:50]} || {r['pt'][:40]}")

    if args.output:
        approved = [{"kalshi": r["ktk"], "polymarket": r["pid"],
                     "polarity": r["polarity"],
                     "_note": f"net=${r['net']:.2f} edge={r['net_edge']*100:.1f}% "
                              f"{r['kt'][:50]} || {r['pt'][:50]}"}
                    for r in found if not r.get("uncertain")]
        Path(args.output).write_text(json.dumps({"approved": approved, "denied": []}, indent=2))
        print(f"\nWrote {len(approved)} allowlist-ready entries to {args.output}", file=sys.stderr)
        print("Review each before enabling live trading (MATCH_REQUIRE_ALLOWLIST_FOR_LIVE).",
              file=sys.stderr)


if __name__ == "__main__":
    main()
