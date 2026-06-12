"""Diagnostic: run discovery + matching and dump matched pairs + sample titles.

Helps answer "why so few matches?" once data is flowing.

    python -m tools.diagnose_matches [N]
"""

import asyncio
import sys

from kalshi_arbitrage.market_analyzer import MarketAnalyzer


async def main(limit=40):
    analyzer = MarketAnalyzer()
    kc = analyzer.kalshi_client
    pc = analyzer.polymarket_client

    await kc._discover_markets_via_rest()  # bulk (capped) + event-based discovery
    await pc._bootstrap_markets_via_rest()

    kalshi = list(kc.markets_cache.values())
    poly = list(pc.markets_cache.values())
    print(f"Kalshi markets: {len(kalshi)}  Polymarket markets: {len(poly)}")

    k_proc = analyzer._process_kalshi_markets(kalshi)
    p_proc = analyzer._process_polymarket_markets(poly)
    # The matcher's inverted-index lookup reads this (set in run_full_scan).
    analyzer._current_polymarket_markets = p_proc
    print(f"Processed: Kalshi {len(k_proc)}  Polymarket {len(p_proc)}\n")

    print("=== sample KALSHI titles ===")
    for m in k_proc[:15]:
        print("  K:", repr(m.get("title", ""))[:80], "| id:", m.get("id"))
    print("\n=== sample POLYMARKET titles ===")
    for m in p_proc[:15]:
        print("  P:", repr(m.get("title", ""))[:80])

    matches = await analyzer._find_market_matches(k_proc, p_proc)
    print(f"\nPotential matches: {len(matches)}\n")
    matches.sort(key=lambda m: m.get("similarity_score", 0), reverse=True)
    for m in matches[:limit]:
        k = m["kalshi_market"]; p = m["polymarket_market"]
        print(f"[{m.get('similarity_score',0):.3f} {m.get('polarity','?'):8}] "
              f"K:{k.get('title','')[:55]!r}  P:{p.get('title','')[:55]!r}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 40))
