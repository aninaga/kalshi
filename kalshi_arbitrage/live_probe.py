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
    pm_fee_bps: int = 1000,
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


def price_pair(pair: Dict, pm_fee_bps: int = 1000) -> Optional[Dict]:
    """Fetch synchronized books for a verified pair and price the cross-venue
    complementary arb. ``pair`` carries ktk, tokens, polarity (from a verdict)."""
    kyes, kno = kalshi_book(pair["ktk"])
    toks = {str(t.get("outcome", "")).lower(): t["token_id"] for t in pair["tokens"]}
    yes_t = toks.get("yes") or pair["tokens"][0]["token_id"]
    no_t = toks.get("no") or pair["tokens"][1]["token_id"]
    pyes, pno = pm_asks(yes_t), pm_asks(no_t)
    inv = pair["polarity"] == INVERTED
    pm_A, pm_B = (pno, pyes) if inv else (pyes, pno)  # A = Kalshi-YES outcome
    ka = kyes[0][0] if kyes else 9
    pa = pm_A[0][0] if pm_A else 9
    kb = kno[0][0] if kno else 9
    pb = pm_B[0][0] if pm_B else 9
    a_is_k = ka <= pa
    b_is_k = kb <= pb
    if a_is_k == b_is_k:  # need one leg per venue
        return None
    A = kyes if a_is_k else pm_A
    B = kno if b_is_k else pm_B
    if not A or not B:
        return None
    # Polarity fail-safe (economics-level, polarity-INDEPENDENT): two genuinely
    # complementary outcomes of the same event have P(A)+P(B)=1, so their ask
    # prices must sum to ~$1 (a real arb edge is only a few %). A sum far below 1
    # means the two legs are NOT complementary — a polarity error or false match
    # would otherwise fabricate a huge phantom edge (e.g. buying NO on both
    # venues sums to ~0.17). Reject rather than trust the polarity flag alone.
    if (A[0][0] + B[0][0]) < _MIN_COMPLEMENTARY_SUM:
        return None
    res = walk_complementary(A, B, a_is_k, b_is_k, pm_fee_bps)
    if not res:
        return None
    res.update({**pair, "a_src": "K" if a_is_k else "P", "b_src": "K" if b_is_k else "P"})
    res["implausible"] = res["net_edge"] > getattr(Config, "MAX_PLAUSIBLE_PROFIT_MARGIN", 0.15)
    return res


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
    """Full sweep: fetch both catalogs and return verified same-event pairs."""
    return match_pairs(fetch_polymarket(), fetch_kalshi())
