"""live_probe.to_opportunity: priced pair -> executor opportunity dict.

The mapping (which PM token to buy, which Kalshi side, which price on which leg)
is where a wiring bug would silently place an un-hedged trade. These pin all four
combinations of leg-orientation x polarity against the exact fields the executor
reads, plus the ledger-analysis summary.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.live_probe import to_opportunity
from tools.analyze_ledger import summarize


def _priced(a_src, b_src, polarity):
    return {
        "ktk": "KXTEST-26", "pid": "0xcond", "kt": "K title", "pt": "P title",
        "polarity": polarity, "a_src": a_src, "b_src": b_src,
        "avg_a": 0.20, "avg_b": 0.75, "net": 25.0, "net_edge": 0.03, "size": 500,
        "tokens": [{"outcome": "Yes", "token_id": "YES_TOK"},
                   {"outcome": "No", "token_id": "NO_TOK"}],
    }


def test_kalshi_yes_leg_aligned():
    # A on Kalshi (yes), B on PM. Aligned ⇒ B = PM-NO token.
    opp = to_opportunity(_priced("K", "P", "aligned"))
    assert opp["strategy_type"] == "complementary"
    assert "S3" in opp["strategy"]            # executor reads this ⇒ kalshi_side='yes'
    assert opp["polymarket_token"] == "NO_TOK"
    assert opp["kalshi_price"] == 0.20        # avg_a (the Kalshi/A leg)
    assert opp["polymarket_price"] == 0.75     # avg_b (the PM/B leg)
    assert opp["match_data"]["kalshi_market"]["id"] == "KXTEST-26"


def test_kalshi_no_leg_aligned():
    # B on Kalshi (no), A on PM. Aligned ⇒ A = PM-YES token.
    opp = to_opportunity(_priced("P", "K", "aligned"))
    assert "S3" not in opp["strategy"]         # ⇒ kalshi_side='no'
    assert opp["polymarket_token"] == "YES_TOK"
    assert opp["kalshi_price"] == 0.75         # avg_b (Kalshi/B leg)
    assert opp["polymarket_price"] == 0.20     # avg_a (PM/A leg)


def test_inverted_flips_pm_token():
    # Inverted ⇒ A = PM-NO, B = PM-YES.
    opp_k_yes = to_opportunity(_priced("K", "P", "inverted"))
    assert opp_k_yes["polymarket_token"] == "YES_TOK"   # B = PM-YES when inverted
    opp_k_no = to_opportunity(_priced("P", "K", "inverted"))
    assert opp_k_no["polymarket_token"] == "NO_TOK"     # A = PM-NO when inverted


def test_total_profit_carried():
    opp = to_opportunity(_priced("K", "P", "aligned"))
    assert opp["total_profit"] == 25.0 and opp["max_tradeable_volume"] == 500


def test_ledger_summarize():
    rows = [
        {"market": "Musk", "peak_net": 10.0, "open_minutes": 30},
        {"market": "Musk", "peak_net": 5.0, "open_minutes": 1},
        {"market": "French", "peak_net": 8.0, "open_minutes": 90},
    ]
    s = summarize(rows)
    assert s["episodes"] == 3
    assert abs(s["total_net"] - 23.0) < 1e-9
    assert s["by_market"]["Musk"]["n"] == 2 and abs(s["by_market"]["Musk"]["net"] - 15.0) < 1e-9
    assert s["buckets"]["<2m"] == 1 and s["buckets"][">60m"] == 1
