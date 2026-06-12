"""Tests for research.lab.execution — realistic fills on synthetic panels.

Synthetic-fixture only (no real-data cache). Asserts the binding capital-risk
behaviour: fills snap to a REAL listed strike, fill_mid is the real quoted prob
(not 0.50), and both the half-spread and taker fee are applied.
"""
from __future__ import annotations

import numpy as np
import pytest

from research.lab.execution import (
    PM_CURVE_FEE_RATE_BPS,
    PM_FLAT_TAKER_RATE,
    REALISTIC,
    FillModel,
    cost_sweep,
)
from research.lab.types import (
    SPREAD,
    TOTAL,
    WINNER,
    FillResult,
    synthetic_panel,
)


# --------------------------------------------------------------------------- #
# Fee primitives
# --------------------------------------------------------------------------- #


def test_polymarket_fee_is_official_sports_schedule():
    # Official schedule (docs.polymarket.com/trading/fees, verified 2026-06-12):
    # taker = shares x (300bps/10000) x p x (1-p) for sports; NO flat piece.
    # ATM: 0.03 * 0.5 * 0.5 = $0.0075 per share.
    fm = FillModel(venue="polymarket")
    assert fm.fee(0.50) == pytest.approx(0.0075, abs=1e-6)
    # The retired flat-2% fee would have charged >= 1c at ATM — assert gone.
    assert fm.fee(0.50) < 0.01


def test_polymarket_fee_vanishes_at_tails():
    # Parabolic: ~0 at extreme prices. The legacy flat piece did NOT decay and
    # over-charged tails ~14x — the error that over-killed tail-priced families.
    fm = FillModel(venue="polymarket")
    assert fm.fee(0.95) < 0.002
    assert fm.fee(0.50) > fm.fee(0.95)


def test_legacy_fees_flag_reproduces_pre_20260612_memos():
    legacy = FillModel(venue="polymarket", legacy_fees=True)
    fee = legacy.fee(0.50)
    flat = 0.5 * PM_FLAT_TAKER_RATE
    assert fee >= flat                       # flat 2% present under the flag
    assert fee == pytest.approx(0.0178, abs=1e-4)


def test_fee_scales_with_size_and_peaks_atm():
    fm = FillModel(venue="polymarket")
    assert fm.fee(0.50, size=2.0) > fm.fee(0.50, size=1.0)
    assert fm.fee(0.50) > fm.fee(0.10)       # parabolic peaks at 0.5
    assert fm.fee(0.50) > fm.fee(0.90)


def test_kalshi_fee_peaks_near_half():
    fm = FillModel(venue="kalshi")
    assert fm.fee(0.50) >= fm.fee(0.10)
    assert fm.fee(0.50) >= fm.fee(0.90)


def test_unknown_venue_raises():
    with pytest.raises(ValueError):
        FillModel(venue="ftx").fee(0.5)


def test_legacy_constants_still_exported_for_memo_reproduction():
    assert PM_FLAT_TAKER_RATE == 0.02
    assert PM_CURVE_FEE_RATE_BPS == 1000


# --------------------------------------------------------------------------- #
# Fill: snapping + real quoted prob (the core "never 0.50" guarantee)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("market", [TOTAL, SPREAD])
def test_fill_snaps_to_a_real_listed_strike(market):
    panel = synthetic_panel(market=market, seed=3)
    ts = float(panel.minute_ts[panel.n // 2])
    res = REALISTIC.fill(panel, ts, side="over" if market == TOTAL else "long_home")
    assert isinstance(res, FillResult)
    # The chosen strike must be one of the actually listed ladder strikes.
    assert res.strike in set(panel.ladder.keys())


@pytest.mark.parametrize("market", [TOTAL, SPREAD])
def test_fill_mid_is_real_quoted_prob_not_half(market):
    # Run across many minutes/games; the realistic fill must not collapse to 0.50.
    mids = []
    for seed in range(8):
        panel = synthetic_panel(market=market, seed=seed)
        for k in range(5, panel.n, 7):
            ts = float(panel.minute_ts[k])
            res = REALISTIC.fill(panel, ts, side="over" if market == TOTAL else "long_home")
            mids.append(res.fill_mid)
    mids = np.array(mids)
    assert np.all((mids >= 0.0) & (mids <= 1.0))
    # In general the fill is NOT 0.50 — that was the falsely-certifying artifact.
    assert np.mean(np.abs(mids - 0.50) > 1e-6) > 0.5


def test_fill_mid_matches_ladder_interpolation():
    panel = synthetic_panel(market=TOTAL, seed=1)
    ts = float(panel.minute_ts[10])
    res = REALISTIC.fill(panel, ts, side="over")
    # Recompute expected: nearest strike to implied mid at ts, then its quoted prob.
    implied = float(np.interp(ts, panel.minute_ts, panel.mid))
    strikes = np.array(sorted(panel.ladder.keys()))
    exp_strike = float(strikes[int(np.argmin(np.abs(strikes - implied)))])
    exp_p = float(np.interp(ts, panel.minute_ts, panel.ladder[exp_strike]))
    exp_p = min(max(exp_p, 0.01), 0.99)
    assert res.strike == exp_strike
    assert res.fill_mid == pytest.approx(exp_p, abs=1e-9)


def test_short_side_is_complement_of_quoted_prob():
    panel = synthetic_panel(market=TOTAL, seed=2)
    ts = float(panel.minute_ts[12])
    over = REALISTIC.fill(panel, ts, side="over")
    under = REALISTIC.fill(panel, ts, side="under")
    assert over.strike == under.strike  # same listed strike
    assert over.fill_mid + under.fill_mid == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# Fill: half-spread + fee actually applied to all_in_price
# --------------------------------------------------------------------------- #


def test_half_spread_and_fee_applied():
    panel = synthetic_panel(market=TOTAL, seed=5)
    fm = FillModel(half_spread=0.015)
    ts = float(panel.minute_ts[20])
    res = fm.fill(panel, ts, side="over")
    assert res.half_spread == 0.015
    assert res.fee > 0.0
    # all_in_price = fill_mid + half_spread + fee, strictly above the raw mid.
    assert res.all_in_price == pytest.approx(res.fill_mid + res.half_spread + res.fee)
    assert res.all_in_price > res.fill_mid


def test_zero_spread_still_pays_fee():
    panel = synthetic_panel(market=TOTAL, seed=6)
    fm = FillModel(half_spread=0.0)
    ts = float(panel.minute_ts[15])
    res = fm.fill(panel, ts, side="over")
    assert res.half_spread == 0.0
    assert res.fee > 0.0
    assert res.all_in_price > res.fill_mid  # fee alone makes it costly


def test_larger_spread_costs_more():
    panel = synthetic_panel(market=SPREAD, seed=7)
    ts = float(panel.minute_ts[18])
    cheap = FillModel(half_spread=0.01).fill(panel, ts, side="long_home")
    pricey = FillModel(half_spread=0.025).fill(panel, ts, side="long_home")
    assert pricey.all_in_price > cheap.all_in_price


# --------------------------------------------------------------------------- #
# Winner market (no ladder)
# --------------------------------------------------------------------------- #


def test_winner_market_uses_mid_as_price():
    panel = synthetic_panel(market=WINNER, seed=0)
    assert panel.ladder == {}
    ts = float(panel.minute_ts[panel.n // 2])
    res = REALISTIC.fill(panel, ts, side="long_home")
    expected = min(max(float(np.interp(ts, panel.minute_ts, panel.mid)), 0.01), 0.99)
    assert res.fill_mid == pytest.approx(expected, abs=1e-9)
    assert res.fee > 0.0


# --------------------------------------------------------------------------- #
# cost_sweep + REALISTIC default
# --------------------------------------------------------------------------- #


def test_cost_sweep_returns_ascending_cost_models():
    sweep = cost_sweep()
    assert all(isinstance(m, FillModel) for m in sweep)
    spreads = [m.half_spread for m in sweep]
    assert spreads == sorted(spreads)
    # Strictly ascending all-in cost on a fixed fill.
    panel = synthetic_panel(market=TOTAL, seed=9)
    ts = float(panel.minute_ts[10])
    prices = [m.fill(panel, ts, side="over").all_in_price for m in sweep]
    assert prices == sorted(prices)


def test_cost_sweep_sorts_unsorted_input():
    sweep = cost_sweep(values=(0.02, 0.0, 0.01))
    assert [m.half_spread for m in sweep] == [0.0, 0.01, 0.02]


def test_realistic_is_default_polymarket_model():
    assert isinstance(REALISTIC, FillModel)
    assert REALISTIC.venue == "polymarket"
    assert REALISTIC.half_spread == 0.015


def test_fill_skips_nan_quotes_in_ladder():
    # A real panel can carry NaN gaps in a strike's quote; the fill must
    # interpolate over the finite samples (like the ported ffill), not return NaN.
    panel = synthetic_panel(market=TOTAL, seed=4)
    strikes = sorted(panel.ladder.keys())
    chosen = strikes[len(strikes) // 2]
    arr = panel.ladder[chosen].copy()
    arr[10] = np.nan  # punch a hole at the bar we will fill on
    panel.ladder[chosen] = arr
    panel.mid = np.full(panel.n, float(chosen))  # force snapping to `chosen`
    ts = float(panel.minute_ts[10])
    res = REALISTIC.fill(panel, ts, side="over")
    assert res.strike == chosen
    assert np.isfinite(res.fill_mid)


def test_all_nan_quote_raises():
    panel = synthetic_panel(market=TOTAL, seed=4)
    strikes = sorted(panel.ladder.keys())
    chosen = strikes[0]
    panel.ladder = {chosen: np.full(panel.n, np.nan)}
    panel.mid = np.full(panel.n, float(chosen))
    ts = float(panel.minute_ts[5])
    with pytest.raises(ValueError):
        REALISTIC.fill(panel, ts, side="over")


def test_empty_panel_raises():
    panel = synthetic_panel(market=TOTAL, seed=0)
    panel.minute_ts = np.array([], dtype=float)
    panel.elapsed_sec = np.array([], dtype=float)
    with pytest.raises(ValueError):
        REALISTIC.fill(panel, 0.0, side="over")


# --------------------------------------------------------------------------- #
# E2E smoke (synthetic recipe from the task)
# --------------------------------------------------------------------------- #


def test_e2e_smoke_realistic_fill_fields_sane():
    panel = synthetic_panel(market=TOTAL, seed=42)
    assert panel.ladder  # has a real strike ladder
    ts = float(panel.minute_ts[panel.n // 2])
    res = REALISTIC.fill(panel, ts, side="over")
    assert res.strike in set(panel.ladder.keys())
    assert 0.0 <= res.fill_mid <= 1.0
    assert res.fill_mid != 0.5
    assert res.half_spread == 0.015
    assert res.fee > 0.0
    assert 0.0 < res.all_in_price < 1.0
