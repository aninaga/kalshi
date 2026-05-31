"""Probe: for matched pairs, show the actual cross-venue prices + spread so we
can see whether 0 opportunities is real efficiency or a pricing/threshold issue.

Runs a real scan (with orderbooks) then dumps, for the top matches, the best
Kalshi YES ask/bid vs Polymarket YES ask/bid and the implied arbitrage edge.

    python -m tools.probe_economics
"""

import asyncio

from kalshi_arbitrage.config import Config
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


def _best(levels, side):
    """Best price from a list of {price,size} levels: min ask / max bid."""
    ps = [l.get("price") for l in (levels or []) if l.get("price") is not None]
    if not ps:
        return None
    return min(ps) if side == "ask" else max(ps)


async def main():
    az = MarketAnalyzer()
    await az.initialize()

    kalshi = list(az.kalshi_client.markets_cache.values())
    poly = list(az.polymarket_client.markets_cache.values())
    k = az._process_kalshi_markets(kalshi)
    p = az._process_polymarket_markets(poly)
    az._current_polymarket_markets = p
    matches = await az._find_market_matches(k, p)
    print(f"matches: {len(matches)}\n")

    shown = 0
    for m in sorted(matches, key=lambda x: x.get("similarity_score", 0), reverse=True):
        if shown >= 30:
            break
        km = m["kalshi_market"]; pm = m["polymarket_market"]
        kob = await az.kalshi_client.get_market_orderbook(km["id"])
        # pick the PM YES token
        pm_prices = await az.polymarket_client.get_market_prices(pm["id"])
        yes_tok = None
        for tid, pd in (pm_prices or {}).items():
            if str(pd.get("outcome", "")).lower() in ("yes", "true", "1"):
                yes_tok = tid; break
        pob = await az.polymarket_client.get_market_orderbook(pm["id"], yes_tok) if yes_tok else None
        if not kob or not pob:
            continue
        k_ask = _best(kob.get("yes_asks"), "ask"); k_bid = _best(kob.get("yes_bids"), "bid")
        p_ask = _best(pob.get("asks"), "ask"); p_bid = _best(pob.get("bids"), "bid")
        if None in (k_ask, k_bid, p_ask, p_bid):
            continue
        shown += 1
        # Same-outcome edges (ignoring fees): buy cheap side, sell rich side.
        edge1 = (p_bid - k_ask)   # buy Kalshi YES, sell PM YES
        edge2 = (k_bid - p_ask)   # buy PM YES, sell Kalshi YES
        print(f"[sim={m.get('similarity_score',0):.3f} {m.get('polarity','?')}] "
              f"K:{km.get('title','')[:42]!r} | P:{pm.get('title','')[:42]!r}")
        print(f"    Kalshi YES ask={k_ask:.3f} bid={k_bid:.3f} | "
              f"PM YES ask={p_ask:.3f} bid={p_bid:.3f} | "
              f"edge(buyK/sellP)={edge1:+.3f} edge(buyP/sellK)={edge2:+.3f}")

    await az.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
