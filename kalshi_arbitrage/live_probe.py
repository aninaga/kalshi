"""Shared live cross-venue arbitrage primitives.

Factored out of ``tools/find_live_arb.py`` so the discovery scan, the continuous
capture monitor, and the retroactive backtest all share ONE implementation of:
catalog fetch, synchronized order-book fetch (migrated Kalshi ``orderbook_fp``
schema), and the fee-aware complementary-arb economics. The pure economics
functions take no network and are unit-tested; the network functions are thin
stdlib HTTP (works behind the env's policy with a browser UA).

The economics enforce the discipline the analysis proved is required: a
complementary arb (own outcome-A + outcome-B across venues for < $1) is only
real once it clears the dominant Kalshi taker fee curve 0.07*P*(1-P).
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .config import Config
from .matching import CompositeVerifier, INVERTED
from .mock_execution import FeeModel
from .utils import _BOILERPLATE, clean_title, get_similarity_score

UA = {"User-Agent": getattr(Config, "HTTP_USER_AGENT", None)
      or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
PM_CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

# Two genuinely-complementary outcomes of the same event have ask prices that
# sum to ~$1 (a real cross-venue arb edge is only a few %). Below this floor the
# legs are not complementary — a polarity error / false match — so reject rather
# than report a phantom edge. Genuine arbs sit ~0.94-0.99; phantoms ~0.1-0.4.
_MIN_COMPLEMENTARY_SUM = 0.80


def get(url: str, timeout: int = 25):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Pure economics (no network — unit-tested)                                   #
# --------------------------------------------------------------------------- #

def kalshi_fee_per_contract(price: float) -> float:
    """Kalshi taker fee per contract, $ — the curve 0.07*P*(1-P). Maximal at
    P=0.5 (1.75¢), tiny at the extremes. This is the cost that eats mid-priced
    cross-venue gaps and is why durable arb sits at extreme prices."""
    return 0.07 * price * (1.0 - price)


def walk_complementary(
    ladder_a: List[Tuple[float, float]],
    ladder_b: List[Tuple[float, float]],
    a_is_kalshi: bool,
    b_is_kalshi: bool,
    pm_fee_bps: int = 500,
) -> Optional[Dict]:
    """Walk two ascending ask ladders (outcome A, outcome B on opposite venues),
    accumulating size while each marginal contract is net-positive AFTER fees.

    Returns dict(size, cost, net, avg_a, avg_b, net_edge) or None if no
    net-positive size exists. Mirrors the proven /tmp logic; the FeeModel is the
    single source of truth for both venues' fees.
    """
    qa = [list(x) for x in ladder_a]
    qb = [list(x) for x in ladder_b]
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
        kf = (FeeModel.kalshi_taker_fee(pa, step) if a_is_kalshi else 0.0) \
            + (FeeModel.kalshi_taker_fee(pb, step) if b_is_kalshi else 0.0)
        pf = (FeeModel.polymarket_taker_fee(pa, step, pm_fee_bps) if not a_is_kalshi else 0.0) \
            + (FeeModel.polymarket_taker_fee(pb, step, pm_fee_bps) if not b_is_kalshi else 0.0)
        if (1.0 - pa - pb) * step - kf - pf <= 0:
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
    kf = (FeeModel.kalshi_taker_fee(ca / n, n) if a_is_kalshi else 0.0) \
        + (FeeModel.kalshi_taker_fee(cb / n, n) if b_is_kalshi else 0.0)
    pf = (FeeModel.polymarket_taker_fee(ca / n, n, pm_fee_bps) if not a_is_kalshi else 0.0) \
        + (FeeModel.polymarket_taker_fee(cb / n, n, pm_fee_bps) if not b_is_kalshi else 0.0)
    net = (n - ca - cb) - kf - pf
    return {"size": n, "cost": ca + cb, "net": net, "avg_a": ca / n, "avg_b": cb / n,
            "net_edge": net / (ca + cb) if (ca + cb) else 0.0}


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
    """(yes_ask_ladder, no_ask_ladder) ascending. yes_ask = 1 - best_no_bid."""
    ob = get(f"{KBASE}/markets/{ticker}/orderbook?depth=100")
    fp = (ob or {}).get("orderbook_fp") or {}
    yb = _parse_levels(fp.get("yes_dollars"))
    nb = _parse_levels(fp.get("no_dollars"))
    return sorted((1 - p, s) for p, s in nb), sorted((1 - p, s) for p, s in yb)


def pm_asks(token: str) -> list:
    b = get(f"{PM_CLOB}/book?token_id={token}")
    return sorted((float(a["price"]), float(a["size"])) for a in (b or {}).get("asks", []))


def pair_structures(pair: Dict, pm_fee_bps: int = 500,
                    pm_book_fn=None) -> List[Dict]:
    """Fetch synchronized books for a verified pair ONCE and price every arb
    structure the pair supports:

      - 'comp': cross-venue complementary (buy A + B across venues < $1)
      - 'pm_intra': PM YES-token + NO-token asks sum < $1 (separate books)

    (Kalshi has no intra structure: its YES/NO sides are one unified book, so
    yes_ask + no_ask = 1 + spread >= 1 by construction.)
    """
    out: List[Dict] = []
    kyes, kno = kalshi_book(pair["ktk"])
    toks = {str(t.get("outcome", "")).lower(): t["token_id"] for t in pair["tokens"]}
    yes_t = toks.get("yes") or pair["tokens"][0]["token_id"]
    no_t = toks.get("no") or pair["tokens"][1]["token_id"]
    if str(yes_t).startswith("us:"):
        # Polymarket US pair: unified per-market book (one fetch serves both
        # sides) and no separate token books -> no pm_intra structure. US fees
        # (0.05*C*p*(1-p)) == the PM parabolic model at the default 500 bps,
        # so the leg flags below stay 'PM'.
        from .pmus import pmus_book, us_slug
        pyes, pno = pmus_book(us_slug(yes_t) or "")
        us_pair = True
    else:
        # pm_book_fn lets the monitor serve books from the live WS mirror
        # (REST only on mirror miss/staleness).
        fetch_pm = pm_book_fn or pm_asks
        pyes, pno = fetch_pm(yes_t), fetch_pm(no_t)
        us_pair = False

    # -- cross-venue complementary ------------------------------------------ #
    inv = pair["polarity"] == INVERTED
    pm_A, pm_B = (pno, pyes) if inv else (pyes, pno)  # A = Kalshi-YES outcome
    ka = kyes[0][0] if kyes else 9
    pa = pm_A[0][0] if pm_A else 9
    kb = kno[0][0] if kno else 9
    pb = pm_B[0][0] if pm_B else 9
    a_is_k = ka <= pa
    b_is_k = kb <= pb
    if a_is_k != b_is_k:  # need one leg per venue
        A = kyes if a_is_k else pm_A
        B = kno if b_is_k else pm_B
        # Polarity fail-safe (economics-level, polarity-INDEPENDENT): two
        # genuinely complementary outcomes of the same event have P(A)+P(B)=1,
        # so their ask prices must sum to ~$1 (a real arb edge is only a few
        # %). A sum far below 1 means the legs are NOT complementary — a
        # polarity error / false match would otherwise fabricate a phantom
        # edge. Reject rather than trust the polarity flag alone.
        if A and B and (A[0][0] + B[0][0]) >= _MIN_COMPLEMENTARY_SUM:
            res = walk_complementary(A, B, a_is_k, b_is_k, pm_fee_bps)
            if res:
                res.update({**pair, "a_src": "K" if a_is_k else "P",
                            "b_src": "K" if b_is_k else "P",
                            "strategy": "comp", "key": f"comp:{pair['ktk']}"})
                res["implausible"] = res["net_edge"] > getattr(
                    Config, "MAX_PLAUSIBLE_PROFIT_MARGIN", 0.15)
                out.append(res)

    # -- PM intra (YES + NO tokens < $1) ------------------------------------ #
    if us_pair:
        return out   # unified book: yes_ask + no_ask = 1 + spread, never < 1
    intra = walk_basket([_tag(pyes, False), _tag(pno, False)], pm_fee_bps, payout=1.0)
    if intra:
        intra.update({"strategy": "pm_intra", "key": f"pm_intra:{pair['ktk']}",
                      "ktk": pair["ktk"], "kt": pair.get("pt") or pair.get("kt", ""),
                      "uncertain": False})
        intra["implausible"] = intra["net_edge"] > getattr(
            Config, "MAX_PLAUSIBLE_PROFIT_MARGIN", 0.15)
        out.append(intra)
    return out


def price_pair(pair: Dict, pm_fee_bps: int = 500) -> Optional[Dict]:
    """Back-compat: the cross-venue complementary structure only."""
    for r in pair_structures(pair, pm_fee_bps):
        if r.get("strategy") == "comp":
            return r
    return None


# --------------------------------------------------------------------------- #
#  Catalogs + matching                                                         #
# --------------------------------------------------------------------------- #

def _gamma_to_clob_row(m: Dict) -> Optional[Dict]:
    """Map a Gamma ``/markets`` row to the CLOB sampling-markets shape that
    ``match_pairs`` consumes. Gamma stores outcomes/clobTokenIds as JSON strings;
    only active, non-closed, binary markets map (else None)."""
    if not isinstance(m, dict) or m.get("closed") or not m.get("active"):
        return None
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
        outs = json.loads(m.get("outcomes") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if len(toks) != 2 or len(outs) != 2:
        return None
    return {"condition_id": m.get("conditionId"), "question": m.get("question") or "",
            "description": m.get("description") or "",
            "end_date_iso": m.get("endDate"), "active": True, "closed": False,
            "tokens": [{"token_id": str(toks[0]), "outcome": str(outs[0])},
                       {"token_id": str(toks[1]), "outcome": str(outs[1])}]}


def fetch_polymarket() -> List[Dict]:
    out, cur = [], ""
    while True:
        d = get(f"{PM_CLOB}/sampling-markets" + (f"?next_cursor={cur}" if cur else ""))
        if not d:
            break
        rows = d.get("data", []) if isinstance(d, dict) else d
        out.extend(rows)
        cur = d.get("next_cursor", "") if isinstance(d, dict) else ""
        if not cur or cur == "LTE=" or len(out) > 9000:
            break
    rows = [m for m in out if m.get("active") and not m.get("closed") and len(m.get("tokens") or []) == 2]

    # SUPPLEMENT: /sampling-markets only lists markets in the liquidity-rewards
    # sampling set, and Polymarket drops near-resolved EXTREME-priced markets
    # from it — exactly where durable arb lives. Observed live (2026-06-09): the
    # Musk-trillionaire pair, the largest standing clean edge, vanished from
    # sampling-markets when PM hit 97.75c while the cross-venue gap was still
    # ~8% gross at depth. Top up with the Gamma catalog (top-volume active
    # markets) so those markets stay discoverable.
    # NB: Gamma silently caps ``limit`` at 100 rows/page regardless of what is
    # requested — page by the ACTUAL returned length or the sweep stops at the
    # top-100 (which is exactly how the Musk pair was missed a second time).
    seen = {m.get("condition_id") for m in rows}
    offset, page = 0, 100
    while offset < 4000:
        d = get(f"{GAMMA}/markets?active=true&closed=false&order=volumeNum"
                f"&ascending=false&limit={page}&offset={offset}")
        if not isinstance(d, list) or not d:
            break
        for g in d:
            r = _gamma_to_clob_row(g)
            if r and r["condition_id"] not in seen:
                seen.add(r["condition_id"])
                rows.append(r)
        offset += len(d)
        if len(d) < page:
            break
    return rows


def fetch_kalshi() -> List[Dict]:
    evs, cur, pages = [], "", 0
    while pages < 45:
        d = get(f"{KBASE}/events?limit=200&status=open&with_nested_markets=true"
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
            if any(x in m.get("ticker", "") for x in ("MULTIGAME", "CROSSCATEGORY")):
                continue
            if (f(m, "yes_bid_dollars") > 0 and f(m, "yes_ask_dollars") > 0) \
                    or f(m, "volume_fp") > 0 or f(m, "open_interest_fp") > 0:
                out.append(m)
    return out


_KRULES_CACHE: Dict[str, Dict[str, str]] = {}


def _kalshi_rules(ticker: str) -> Dict[str, str]:
    """Full resolution rules via the single-market GET.

    The Kalshi LIST/events endpoints ship ``rules_primary=''`` — verifying
    against catalog rows alone starves every rules-text gate (deadline,
    exclusion-clause, threshold-comparator) of the Kalshi side, which is how
    the Musk strict-vs-inclusive asymmetry sailed through as "clean". Cached
    per ticker (rules are immutable); a failed fetch is NOT cached so a later
    refresh can retry."""
    if ticker not in _KRULES_CACHE:
        d = get(f"{KBASE}/markets/{ticker}")
        m = (d or {}).get("market") or {}
        if not m:
            return {"rules_primary": "", "rules_secondary": ""}
        _KRULES_CACHE[ticker] = {"rules_primary": m.get("rules_primary") or "",
                                 "rules_secondary": m.get("rules_secondary") or "",
                                 "yes_sub_title": m.get("yes_sub_title") or "",
                                 "no_sub_title": m.get("no_sub_title") or ""}
    return _KRULES_CACHE[ticker]


def match_pairs(pm: List[Dict], ks: List[Dict]) -> List[Dict]:
    """Verified same-event (Kalshi, PM) pairs via similarity + CompositeVerifier."""
    pmp = []
    for m in pm:
        ct = clean_title(m.get("question") or "")
        pmp.append({"id": m.get("condition_id"), "title": m.get("question") or "",
                    "clean": ct, "tokens": m.get("tokens"), "desc": m.get("description") or "",
                    "close": m.get("end_date_iso"),
                    "terms": {t for t in ct.split() if len(t) >= 3} - _BOILERPLATE})
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
        cand = defaultdict(int)
        for t in ({t for t in ct.split() if len(t) >= 3} - _BOILERPLATE):
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
            if key in seen:
                continue
            sim = get_similarity_score(ct, p["clean"])
            if sim < Config.SIMILARITY_THRESHOLD:
                continue
            pd = {"id": p["id"], "title": p["title"], "clean_title": p["clean"],
                  "close_time": p["close"], "raw_data": {"description": p["desc"]}}
            # Catalog rows carry rules_primary='' — backfill the real rules
            # (one cached GET per ticker, only for markets that reach verify)
            # so the rules-text gates see the actual Kalshi side.
            if not kd["raw_data"]["rules_primary"]:
                kd["raw_data"].update(_kalshi_rules(m.get("ticker")))
            verdict = verifier.verify(kd, pd)
            if not verdict.passed:
                continue
            seen.add(key)
            # pdesc is persisted into the watch-list cache: PM's catalogs shed
            # near-resolved markets, so the description is unrecoverable later
            # and reverify_pair needs it to re-run the verifier across restarts.
            pairs.append({"ktk": m.get("ticker"), "kt": title, "pt": p["title"],
                          "tokens": p["tokens"], "pid": p["id"], "pdesc": p["desc"],
                          "sim": round(sim, 4),
                          "polarity": verdict.polarity, "uncertain": verdict.uncertain})
    return _arbitrate_conflicts(pairs)


def _arbitrate_conflicts(pairs: List[Dict]) -> List[Dict]:
    """One binary market equals AT MOST one binary market on the other venue.

    When the same Kalshi ticker (or the same PM condition id) appears in
    multiple verified pairings, at most one of them can be the same event —
    the others are near-miss lookalikes that cleared similarity (live audit
    2026-06-09: Kalshi "Racing Bulls" matched both PM "Racing Bulls" and PM
    "Red Bull Racing"; Kalshi's World Series market and its AL-pennant market
    both matched PM's ALCS market). Keep the highest-similarity pairing per
    market as-is and demote every other pairing to uncertain (held-for-review,
    never auto-clean) rather than dropping it: occasionally BOTH counterparts
    are genuine duplicates of the same event, which review can clear.
    """
    best: Dict[tuple, float] = {}
    for p in pairs:
        for side in ("ktk", "pid"):
            key = (side, p.get(side))
            best[key] = max(best.get(key, 0.0), p.get("sim", 0.0))
    out = []
    for p in pairs:
        demoted = [side for side in ("ktk", "pid")
                   if p.get("sim", 0.0) < best[(side, p.get(side))]]
        if demoted and not p.get("uncertain"):
            p = {**p, "uncertain": True, "conflict": ",".join(demoted)}
        elif demoted:
            p = {**p, "conflict": ",".join(demoted)}
        out.append(p)
    return out


_PDESC_CACHE: Dict[str, str] = {}


def _pm_description(pid: str) -> Optional[str]:
    """PM market description by condition id via Gamma (which still serves
    markets the CLOB catalogs have shed). None on fetch failure — NOT cached,
    so a later refresh can retry."""
    if pid in _PDESC_CACHE:
        return _PDESC_CACHE[pid]
    d = get(f"{GAMMA}/markets?condition_ids={pid}")
    if not isinstance(d, list) or not d or not isinstance(d[0], dict):
        return None
    desc = d[0].get("description") or ""
    _PDESC_CACHE[pid] = desc
    return desc


def reverify_pair(pair: Dict, verifier=None) -> Optional[Dict]:
    """Re-run the CURRENT verifier over a cached watch-list pair.

    Cached pairs carry verdict fields (polarity/uncertain) baked at discovery
    time, so a verifier upgrade (e.g. the resolution-authority gate) never
    reaches them and a stale "clean" label survives restarts indefinitely —
    observed live 2026-06-09 when the WHO-pinned pandemic pair stayed clean
    through a monitor restart because it was retained from cache.

    Rebuilds the verification docs (Kalshi rules re-fetched, cached per
    ticker; PM description from the pair's stored ``pdesc``, else Gamma) and
    returns the pair with refreshed verdict fields, ``None`` if it no longer
    passes the verifier, or the pair UNCHANGED if the docs can't be fetched
    (fail-open: a transient network blip must not relabel or drop the cache).
    """
    pdesc = pair.get("pdesc")
    if pdesc is None:
        pid = str(pair.get("pid") or "")
        if pid.startswith("us:"):
            from .pmus import pmus_description
            pdesc = pmus_description(pid)
        else:
            pdesc = _pm_description(pid)
        if pdesc is None:
            return pair
    krules = _kalshi_rules(pair.get("ktk") or "")
    if not krules.get("rules_primary"):
        # Every live Kalshi market carries rules_primary; empty means the
        # fetch failed — fail open rather than judge on missing evidence.
        return pair
    kt, pt = pair.get("kt") or "", pair.get("pt") or ""
    kd = {"id": pair.get("ktk"), "title": kt, "clean_title": clean_title(kt),
          "raw_data": dict(krules)}
    pd = {"id": pair.get("pid"), "title": pt, "clean_title": clean_title(pt),
          "raw_data": {"description": pdesc}}
    verdict = (verifier or CompositeVerifier()).verify(kd, pd)
    if not verdict.passed:
        return None
    out = dict(pair)
    out["pdesc"] = pdesc
    out["polarity"] = verdict.polarity
    out["uncertain"] = verdict.uncertain
    return out


def to_opportunity(priced: Dict) -> Dict:
    """Map a fee-aware priced pair (from ``price_pair``) to the opportunity dict
    the ArbitrageExecutor consumes, for the COMPLEMENTARY ($1) strategy.

    The two legs are a cross-venue buy of complementary outcomes A and B (A =
    Kalshi-YES outcome). Exactly one leg is on each venue. We resolve which PM
    token to buy and which Kalshi side from the polarity and the chosen sources:
      - a_src=K: buy Kalshi-YES (=A) + buy the PM token for B; kalshi_side='yes'.
      - b_src=K: buy Kalshi-NO  (=B) + buy the PM token for A; kalshi_side='no'.
    PM token of A = yes-token if aligned else no-token; B is its complement.
    The executor reads kalshi_side from the strategy string ('S3'/'Kalshi YES'
    => 'yes', else 'no'), so the strategy label is set to match.
    """
    toks = {str(t.get("outcome", "")).lower(): t["token_id"] for t in priced["tokens"]}
    yes_t = toks.get("yes") or priced["tokens"][0]["token_id"]
    no_t = toks.get("no") or priced["tokens"][1]["token_id"]
    inv = priced["polarity"] == INVERTED
    a_token = no_t if inv else yes_t      # PM token for outcome A (Kalshi-YES outcome)
    b_token = yes_t if inv else no_t      # PM token for outcome B (complement)

    if priced["a_src"] == "K":            # Kalshi leg = A (yes); PM leg = B
        kalshi_price, poly_price, pm_token = priced["avg_a"], priced["avg_b"], b_token
        strategy = "Buy Kalshi YES + Buy Polymarket complement (S3)"
    else:                                 # Kalshi leg = B (no); PM leg = A
        kalshi_price, poly_price, pm_token = priced["avg_b"], priced["avg_a"], a_token
        strategy = "Buy Polymarket + Buy Kalshi NO (S4)"

    return {
        "opportunity_id": f"arb-{priced['ktk']}-{str(pm_token)[:8]}",
        "strategy_type": "complementary",
        "strategy": strategy,
        "total_profit": priced["net"],
        "profit_margin": priced["net_edge"],
        "kalshi_price": round(kalshi_price, 4),
        "polymarket_price": round(poly_price, 4),
        "polymarket_token": pm_token,
        "max_tradeable_volume": priced["size"],
        "match_data": {
            "kalshi_market": {"id": priced["ktk"], "title": priced.get("kt", "")},
            "polymarket_market": {"id": priced["pid"], "title": priced.get("pt", "")},
        },
    }


def discover() -> List[Dict]:
    """Full sweep: fetch all venue catalogs and return verified same-event
    pairs. Polymarket US rows ride the same matcher/verifier/arbitration via
    their catalog-row mapping (synthetic 'us:'-prefixed ids)."""
    ks = fetch_kalshi()
    pairs = match_pairs(fetch_polymarket(), ks)
    try:
        from .pmus import fetch_pmus
        us = fetch_pmus()
        if us:
            pairs = _arbitrate_conflicts(pairs + match_pairs(us, ks))
    except Exception as exc:   # third venue is additive, never fatal
        import sys as _sys
        print(f"pm-us catalog unavailable: {exc}", file=_sys.stderr)
    return pairs


# --------------------------------------------------------------------------- #
#  Structure-complete capture: PM intra-market + event-level dutch arbs        #
#                                                                              #
#  A single Kalshi binary market cannot self-arb (its YES and NO sides are one #
#  unified book: no_ask = 1 - yes_bid, so yes_ask + no_ask = 1 + spread >= 1). #
#  But PM's YES/NO tokens trade in SEPARATE books (sum can transiently dip     #
#  under $1), and multi-outcome EVENTS have a separate book per outcome on     #
#  both venues, so the event-level sum can stray from $1 in either direction:  #
#    - YES-dutch: buy YES on every outcome, cost < $1. Pays exactly $1 only    #
#      if the outcome set is EXHAUSTIVE (gate on a catch-all outcome).         #
#    - NO-dutch:  buy NO on every outcome, cost < N-1. Pays >= N-1 whenever    #
#      AT MOST one outcome happens, i.e. safe under mutual exclusivity alone   #
#      (zero YES outcomes pays N, even better).                                #
#  Full-venue detection is a top-of-book screen over the catalog feeds (zero   #
#  extra calls); precise fee-aware book-walks run only on screen hits.         #
# --------------------------------------------------------------------------- #

def walk_basket(ladders: List[list], pm_fee_bps: int = 500,
                payout: float = 1.0) -> Optional[Dict]:
    """Walk N ascending ask ladders bought SIMULTANEOUSLY (same size per leg),
    accumulating while each marginal contract is net-positive after fees.

    Each ladder level is ``(price, size, is_kalshi)`` so a single leg can mix
    venues (cross-venue-improved dutch merges both venues' asks per outcome).
    ``payout`` is the guaranteed $ per unit basket (1.0 for YES-dutch / intra,
    N-1 for NO-dutch). Returns dict(size, cost, net, net_edge, avg_prices) or
    None if no net-positive size exists.
    """
    qs = [[list(lvl) for lvl in lad] for lad in ladders]
    if not qs or any(not lad for lad in qs):
        return None
    idx = [0] * len(qs)
    n = 0.0
    costs = [0.0] * len(qs)
    total_fees = 0.0   # accumulated marginal fees: exact even for mixed-venue legs
    while all(i < len(lad) for i, lad in zip(idx, qs)):
        levels = [lad[i] for i, lad in zip(idx, qs)]
        step = min(s for _, s, _ in levels)
        if step <= 0:
            for j, (_, s, _) in enumerate(levels):
                if s <= 0:
                    idx[j] += 1
            continue
        fees = sum(
            FeeModel.kalshi_taker_fee(p, step) if is_k
            else FeeModel.polymarket_taker_fee(p, step, pm_fee_bps)
            for p, _, is_k in levels)
        if (payout - sum(p for p, _, _ in levels)) * step - fees <= 0:
            break
        n += step
        total_fees += fees
        for j, lvl in enumerate(levels):
            costs[j] += lvl[0] * step
            lvl[1] -= step
            if lvl[1] <= 1e-9:
                idx[j] += 1
    if n <= 0:
        return None
    cost = sum(costs)
    net = payout * n - cost - total_fees
    if net <= 0:
        return None
    return {"size": n, "cost": cost, "net": net,
            "net_edge": net / cost if cost else 0.0,
            "avg_prices": [c / n for c in costs]}


def _tag(ladder, is_kalshi):
    """Attach a venue flag to a plain (price, size) ladder."""
    return [(p, s, is_kalshi) for p, s in ladder]


def _merge_ladders(*tagged):
    """Merge venue-tagged ladders into one ascending ladder (buy from either)."""
    out = []
    for lad in tagged:
        out.extend(lad or [])
    return sorted(out)


# Exhaustiveness (exactly one outcome must resolve YES) is what makes a
# YES-dutch pay $1. Three signal tiers, learned from a live false positive
# (2026-06-10: "Which AI will be the first to hit 1550?" — outcomes summed to
# $0.83 with an "Other" catch-all, but if NO model ever hits 1550 every
# outcome including Other resolves NO and the basket pays $0; the missing
# $0.17 was the market's probability of exactly that):
#   - range-ladder endpoints ("or above"/"or below") tile the real line of a
#     measured quantity -> exhaustive;
#   - an explicit NONE outcome ("none of the above", "no one") covers the
#     event-never-happens path -> exhaustive;
#   - a SUBJECT catch-all ("any other candidate") only covers subject-space:
#     exhaustive for winner-type contests (someone always wins), but NOT for
#     conditional races ("first to hit X", "will anyone...") where the event
#     itself may never happen.
_ENDPOINT_RX = re.compile(
    r"\bor (above|below|higher|lower|more|less|later|earlier)\b", re.IGNORECASE)
_NONE_OUTCOME_RX = re.compile(
    r"\b(none|no one|nobody|neither|no winner)\b", re.IGNORECASE)
_SUBJ_CATCHALL_RX = re.compile(
    r"\b(another|other|someone else|the field|any other)\b", re.IGNORECASE)
_RACE_RX = re.compile(
    r"\b(first to|to (hit|reach|cross|pass|top|exceed)|will any(one|body)?|"
    r"anyone)\b", re.IGNORECASE)


def _event_exhaustive(event_title: str, outcome_texts: List[str]) -> bool:
    """True only when exactly one outcome must resolve YES (YES-dutch pays $1)."""
    joined = " ".join(outcome_texts)
    if _ENDPOINT_RX.search(joined):
        return True
    if _NONE_OUTCOME_RX.search(joined):
        return True
    return bool(_SUBJ_CATCHALL_RX.search(joined)
                and not _RACE_RX.search(event_title or ""))


def kalshi_event_screens(events: List[Dict], min_gross: float = 0.01) -> List[Dict]:
    """Top-of-book event-dutch screen over the Kalshi events feed (no calls).

    Returns candidate dicts {ev, kind, gross, n, tickers, exhaustive} where
    kind is 'dutch_yes' (sum of yes-asks < 1-min_gross; requires exhaustive
    catch-all) or 'dutch_no' (sum of yes-bids > 1+min_gross; safe under the
    event's mutually_exclusive flag).
    """
    out = []
    for e in events:
        if not e.get("mutually_exclusive"):
            continue
        ms = [m for m in (e.get("markets") or [])
              if str(m.get("status", "active")).lower() in ("", "active", "open", "initialized")]
        if len(ms) < 2:
            continue
        asks, bids, ok = [], [], True
        for m in ms:
            try:
                a = float(m.get("yes_ask_dollars") or 0)
                b = float(m.get("yes_bid_dollars") or 0)
            except (TypeError, ValueError):
                ok = False
                break
            if a <= 0:   # no offer on some outcome -> YES-dutch impossible
                a = 9.0
            asks.append(a)
            bids.append(b)
        if not ok:
            continue
        exhaustive = _event_exhaustive(
            e.get("title", ""),
            [f"{m.get('title','')} {m.get('yes_sub_title','')}" for m in ms])
        tickers = [m.get("ticker") for m in ms]
        ev = e.get("event_ticker") or (tickers[0] if tickers else "")
        if sum(asks) < 1.0 - min_gross and exhaustive:
            out.append({"ev": ev, "kind": "dutch_yes", "gross": 1.0 - sum(asks),
                        "n": len(ms), "tickers": tickers, "exhaustive": True,
                        "title": e.get("title", "")})
        if sum(bids) > 1.0 + min_gross:
            out.append({"ev": ev, "kind": "dutch_no", "gross": sum(bids) - 1.0,
                        "n": len(ms), "tickers": tickers, "exhaustive": exhaustive,
                        "title": e.get("title", "")})
    return out


def price_kalshi_dutch(cand: Dict, pairs_by_ktk: Optional[Dict] = None,
                       pm_fee_bps: int = 500) -> Optional[Dict]:
    """Precise fee-aware pricing of a screened event-dutch candidate.

    Fetches each outcome's Kalshi book; when an outcome has a verified PM pair
    (aligned polarity), the PM ladder is merged in so each leg buys from
    whichever venue is cheaper level-by-level.
    """
    pairs_by_ktk = pairs_by_ktk or {}
    yes_legs, no_legs = [], []
    for tk in cand["tickers"]:
        kyes, kno = kalshi_book(tk)
        if not kyes and not kno:
            return None
        y_lad, n_lad = _tag(kyes, True), _tag(kno, True)
        pair = pairs_by_ktk.get(tk)
        if pair and not pair.get("uncertain"):
            toks = {str(t.get("outcome", "")).lower(): t["token_id"]
                    for t in pair.get("tokens") or []}
            inv = pair.get("polarity") == INVERTED
            yes_t = toks.get("no") if inv else toks.get("yes")
            no_t = toks.get("yes") if inv else toks.get("no")
            if yes_t:
                y_lad = _merge_ladders(y_lad, _tag(pm_asks(yes_t), False))
            if no_t:
                n_lad = _merge_ladders(n_lad, _tag(pm_asks(no_t), False))
        yes_legs.append(y_lad)
        no_legs.append(n_lad)
    if cand["kind"] == "dutch_yes":
        res = walk_basket(yes_legs, pm_fee_bps, payout=1.0)
    else:
        res = walk_basket(no_legs, pm_fee_bps, payout=float(len(no_legs)) - 1.0)
    if not res:
        return None
    res.update({"strategy": cand["kind"], "key": f"{cand['kind']}:{cand['ev']}",
                "kt": cand.get("title") or cand["ev"], "n_legs": cand["n"],
                # NO-dutch is guaranteed by exclusivity alone; YES-dutch relies
                # on the catch-all exhaustiveness heuristic -> review.
                "uncertain": cand["kind"] == "dutch_yes" and not cand.get("exhaustive")})
    res["implausible"] = res["net_edge"] > getattr(Config, "MAX_PLAUSIBLE_PROFIT_MARGIN", 0.15)
    return res


_AT_LEAST_RX = re.compile(r"(\d[\d,]*)\s*\+")


def kalshi_ladder_screens(events: List[Dict], min_gross: float = 0.01) -> List[Dict]:
    """Threshold-monotonicity screen over CUMULATIVE ladders (implication arb).

    Within one event, "N+" outcomes are cumulative: YES(higher N) logically
    implies YES(lower N), so bid(higher) can never validly exceed ask(lower).
    When it does, buying YES(lower) + NO(higher) costs < $1 and pays >= $1 in
    every world — same guarantee class as a NO-dutch, and CLEAN because both
    markets share one event's resolution rules.

    Only NON-mutually-exclusive events qualify: MX range buckets ("3000-4000",
    "5000+") are exclusive, where the implication does not hold.
    """
    out = []
    for e in events:
        if e.get("mutually_exclusive"):
            continue
        # Group rungs by SUBJECT: a player-props event contains many players'
        # ladders, and YES(Brunson 11+) implies nothing about Alvarado. The
        # subject is the outcome text with the threshold removed (fallback:
        # ticker minus its trailing threshold suffix).
        by_subject: Dict[str, list] = {}
        for m in (e.get("markets") or []):
            if str(m.get("status", "active")).lower() not in ("", "active", "open", "initialized"):
                continue
            text = f"{m.get('yes_sub_title','')} {m.get('title','')}"
            mt = _AT_LEAST_RX.search(text)
            if not mt:
                continue
            try:
                thr = float(mt.group(1).replace(",", ""))
                ask = float(m.get("yes_ask_dollars") or 0)
                bid = float(m.get("yes_bid_dollars") or 0)
            except (TypeError, ValueError):
                continue
            subject = re.sub(r"\s+", " ", text.replace(mt.group(0), " ")).strip().lower()
            if not subject:
                subject = re.sub(r"-?\d+$", "", str(m.get("ticker") or ""))
            by_subject.setdefault(subject, []).append((thr, ask, bid, m.get("ticker")))
        best = None
        for rungs in by_subject.values():
            if len(rungs) < 2:
                continue
            rungs.sort()
            for i in range(len(rungs)):
                for j in range(i + 1, len(rungs)):
                    t_lo, ask_lo, _, tk_lo = rungs[i]
                    t_hi, _, bid_hi, tk_hi = rungs[j]
                    if t_hi <= t_lo or ask_lo <= 0 or bid_hi <= 0:
                        continue
                    viol = bid_hi - ask_lo
                    if viol > min_gross and (best is None or viol > best["gross"]):
                        best = {"ev": e.get("event_ticker") or tk_lo, "kind": "ladder",
                                "gross": viol, "n": 2, "buy_yes": tk_lo, "buy_no": tk_hi,
                                "exhaustive": True, "title": e.get("title", "")}
        if best:
            out.append(best)
    return out


def price_kalshi_ladder(cand: Dict, pairs_by_ktk: Optional[Dict] = None,
                        pm_fee_bps: int = 500) -> Optional[Dict]:
    """Precise pricing of a ladder candidate: buy YES(lower rung) + NO(higher
    rung); payout >= $1 in every world. PM ladders merge in when either rung
    has a verified pair (cheapest venue per level)."""
    pairs_by_ktk = pairs_by_ktk or {}

    def _leg(ticker, want_yes):
        kyes, kno = kalshi_book(ticker)
        lad = _tag(kyes if want_yes else kno, True)
        pair = pairs_by_ktk.get(ticker)
        if pair and not pair.get("uncertain"):
            toks = {str(t.get("outcome", "")).lower(): t["token_id"]
                    for t in pair.get("tokens") or []}
            inv = pair.get("polarity") == INVERTED
            side = ("no" if inv else "yes") if want_yes else ("yes" if inv else "no")
            tok = toks.get(side)
            if tok:
                lad = _merge_ladders(lad, _tag(pm_asks(tok), False))
        return lad

    yes_leg = _leg(cand["buy_yes"], True)
    no_leg = _leg(cand["buy_no"], False)
    res = walk_basket([yes_leg, no_leg], pm_fee_bps, payout=1.0)
    if not res:
        return None
    res.update({"strategy": "ladder", "key": f"ladder:{cand['buy_yes']}|{cand['buy_no']}",
                "kt": cand.get("title") or cand["ev"], "n_legs": 2,
                "ktk": cand["buy_yes"], "uncertain": False})
    res["implausible"] = res["net_edge"] > getattr(Config, "MAX_PLAUSIBLE_PROFIT_MARGIN", 0.15)
    return res


def fetch_kalshi_events() -> List[Dict]:
    """Open Kalshi events with nested markets (the dutch screen's input)."""
    evs, cur, pages = [], "", 0
    while pages < 45:
        d = get(f"{KBASE}/events?limit=200&status=open&with_nested_markets=true"
                + (f"&cursor={cur}" if cur else ""))
        if not d:
            break
        evs.extend(d.get("events", []))
        cur = d.get("cursor", "")
        pages += 1
        if not cur or not d.get("events"):
            break
    return evs


def pm_event_screens(min_gross: float = 0.01, max_pages: int = 10) -> List[Dict]:
    """Top-of-book dutch screen over PM neg-risk events via Gamma (paged by
    volume). negRisk events are mutually exclusive by construction; the
    exhaustiveness heuristic gates YES-dutch exactly as on Kalshi."""
    out, offset = [], 0
    for _ in range(max_pages):
        d = get(f"{GAMMA}/events?closed=false&order=volume&ascending=false"
                f"&limit=100&offset={offset}")
        if not isinstance(d, list) or not d:
            break
        for e in d:
            if not e.get("negRisk"):
                continue
            ms = [m for m in (e.get("markets") or [])
                  if m.get("active") and not m.get("closed")]
            if len(ms) < 2:
                continue
            try:
                asks = [float(m.get("bestAsk") or 9) for m in ms]
                bids = [float(m.get("bestBid") or 0) for m in ms]
            except (TypeError, ValueError):
                continue
            exhaustive = _event_exhaustive(
                e.get("title", ""), [m.get("question") or "" for m in ms])
            toks = []
            for m in ms:
                try:
                    toks.append(json.loads(m.get("clobTokenIds") or "[]"))
                except (json.JSONDecodeError, TypeError):
                    toks.append([])
            ev = e.get("slug") or e.get("id") or ""
            if sum(asks) < 1.0 - min_gross and exhaustive:
                out.append({"ev": f"pm:{ev}", "kind": "dutch_yes",
                            "gross": 1.0 - sum(asks), "n": len(ms),
                            "tokens": toks, "exhaustive": True,
                            "title": e.get("title", "")})
            if sum(bids) > 1.0 + min_gross:
                out.append({"ev": f"pm:{ev}", "kind": "dutch_no",
                            "gross": sum(bids) - 1.0, "n": len(ms),
                            "tokens": toks, "exhaustive": exhaustive,
                            "title": e.get("title", "")})
        offset += len(d)
        if len(d) < 100:
            break
    return out


def price_pm_dutch(cand: Dict, pm_fee_bps: int = 500) -> Optional[Dict]:
    """Precise pricing of a PM neg-risk dutch candidate (PM books only)."""
    yes_legs, no_legs = [], []
    for pairids in cand.get("tokens") or []:
        if len(pairids) != 2:
            return None
        yes_legs.append(_tag(pm_asks(str(pairids[0])), False))
        no_legs.append(_tag(pm_asks(str(pairids[1])), False))
    if cand["kind"] == "dutch_yes":
        res = walk_basket(yes_legs, pm_fee_bps, payout=1.0)
    else:
        res = walk_basket(no_legs, pm_fee_bps, payout=float(len(no_legs)) - 1.0)
    if not res:
        return None
    res.update({"strategy": cand["kind"], "key": f"{cand['kind']}:{cand['ev']}",
                "kt": cand.get("title") or cand["ev"], "n_legs": cand["n"],
                "uncertain": cand["kind"] == "dutch_yes" and not cand.get("exhaustive")})
    res["implausible"] = res["net_edge"] > getattr(Config, "MAX_PLAUSIBLE_PROFIT_MARGIN", 0.15)
    return res
