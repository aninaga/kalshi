"""Probe: do Kalshi and Polymarket actually have overlapping markets, and what
similarity do equivalent pairs score? Pinpoints whether 0 matches is a real
data gap or a too-strict matcher.

    python -m tools.probe_overlap
"""

import asyncio
from collections import Counter

from kalshi_arbitrage.market_analyzer import MarketAnalyzer
from kalshi_arbitrage.utils import clean_title, get_similarity_score


async def main():
    az = MarketAnalyzer()
    kc, pc = az.kalshi_client, az.polymarket_client
    await kc._discover_markets_via_rest()
    await pc._bootstrap_markets_via_rest()

    k = az._process_kalshi_markets(list(kc.markets_cache.values()))
    p = az._process_polymarket_markets(list(pc.markets_cache.values()))
    print(f"Kalshi processed: {len(k)}  Polymarket processed: {len(p)}")

    # Non-parlay Kalshi singles only.
    k_singles = [m for m in k if ',' not in m.get('title', '')
                 and 'MULTIGAME' not in (m.get('id') or '').upper()]
    print(f"Kalshi non-parlay singles: {len(k_singles)}")

    KEYS = ["governor", "senate", "president", "bitcoin", "ethereum", "election",
            "nominee", "fed", "rate", "shutdown", "trump", "mayor"]
    print("\n=== topic coverage (markets whose title contains the term) ===")
    for term in KEYS:
        nk = sum(1 for m in k_singles if term in m.get('title', '').lower())
        npm = sum(1 for m in p if term in m.get('title', '').lower())
        print(f"  {term:10} Kalshi={nk:4}  Polymarket={npm:4}")

    # For a few PM markets, find the best-similarity Kalshi single.
    print("\n=== best Kalshi match for sample PM markets ===")
    sample = [m for m in p if any(t in m.get('title', '').lower()
              for t in ("governor", "senate", "bitcoin", "president"))][:12]
    for pm in sample:
        pt = pm.get('clean_title') or clean_title(pm.get('title', ''))
        best = max(
            k_singles,
            key=lambda km: get_similarity_score(km.get('clean_title') or clean_title(km.get('title', '')), pt),
            default=None,
        )
        if best:
            bt = best.get('clean_title') or clean_title(best.get('title', ''))
            score = get_similarity_score(bt, pt)
            print(f"  [{score:.3f}] PM:{pm.get('title','')[:48]!r}")
            print(f"          -> K:{best.get('title','')[:48]!r}")


if __name__ == "__main__":
    asyncio.run(main())
