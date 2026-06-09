"""Shared live-probe economics: fee-aware complementary-arb pricing.

These pin the pure (no-network) economics that the discovery scan, the capture
monitor, and the backtest all share, so the three tools can never drift on what
counts as a real, fee-clearing edge.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.live_probe import kalshi_fee_per_contract, walk_complementary


def test_kalshi_fee_curve_peaks_midprice():
    # 0.07*P*(1-P): maximal at 0.5 (1.75c), ~0 at the extremes — the reason
    # durable arb sits at extreme prices, not mid.
    assert abs(kalshi_fee_per_contract(0.5) - 0.0175) < 1e-9
    assert kalshi_fee_per_contract(0.05) < kalshi_fee_per_contract(0.5)
    assert kalshi_fee_per_contract(0.95) < kalshi_fee_per_contract(0.5)


def _lvl(price, size):
    return [(price, size)]


def test_extreme_price_gap_clears_fee():
    # Buy A (PM, no fee) @ 0.06 + B (Kalshi) @ 0.90 = 0.96 < 1. Kalshi leg at 0.90
    # has a tiny fee (0.07*0.9*0.1=0.0063) → net positive.
    res = walk_complementary(_lvl(0.06, 100), _lvl(0.90, 100),
                             a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is not None and res["net"] > 0
    assert res["size"] == 100


def test_midprice_gap_eaten_by_fee():
    # A (PM) @ 0.49 + B (Kalshi) @ 0.50 = 0.99 → 1c gross, but Kalshi fee at 0.50
    # is 1.75c > 1c → net negative → no size.
    res = walk_complementary(_lvl(0.49, 100), _lvl(0.50, 100),
                             a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is None


def test_walk_stops_when_marginal_turns_negative():
    # Level 1 clears (PM 0.05 + Kalshi 0.90), level 2 doesn't (Kalshi 0.97 → sum
    # 1.02). Size capped at the first level.
    A = [(0.05, 50), (0.05, 50)]
    B = [(0.90, 50), (0.97, 50)]
    res = walk_complementary(A, B, a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is not None and res["size"] == 50


def test_no_arb_when_sum_exceeds_one():
    res = walk_complementary(_lvl(0.60, 100), _lvl(0.60, 100),
                             a_is_kalshi=False, b_is_kalshi=True, pm_fee_bps=0)
    assert res is None


def test_price_pair_polarity_failsafe(monkeypatch):
    # Same live books, two polarities: ALIGNED is a genuine arb (legs sum ~0.93),
    # INVERTED buys the same real outcome on both venues (legs sum ~0.17) — the
    # phantom the auditor demonstrated. The complementary-sum floor must reject
    # the phantom while keeping the genuine one. (Guards the silent-polarity bug.)
    from kalshi_arbitrage import live_probe as lp
    # Kalshi: yes_ask 0.89, no_ask 0.13.  PM: yes-token ask 0.97, no-token ask 0.04.
    monkeypatch.setattr(lp, "kalshi_book", lambda tk: ([(0.89, 500)], [(0.13, 500)]))
    monkeypatch.setattr(lp, "pm_asks",
                        lambda tok: [(0.97, 500)] if tok == "y" else [(0.04, 500)])
    pair = {"ktk": "K", "polarity": "aligned",
            "tokens": [{"outcome": "Yes", "token_id": "y"}, {"outcome": "No", "token_id": "n"}]}
    genuine = lp.price_pair(pair, pm_fee_bps=0)
    assert genuine is not None and genuine["net"] > 0      # real arb survives

    phantom = lp.price_pair({**pair, "polarity": "inverted"}, pm_fee_bps=0)
    assert phantom is None                                  # phantom rejected by the floor
