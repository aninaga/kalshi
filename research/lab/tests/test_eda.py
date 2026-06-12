"""Tests for research.lab.eda — idea-agnostic diagnostics (no strategy content)."""
from __future__ import annotations

from research.lab import eda
from research.lab.types import TOTAL, WINNER, synthetic_panels


def test_scan_totals_reports_shape():
    rep = eda.scan(synthetic_panels(30, market=TOTAL, signal=0.05), market=TOTAL)
    assert rep["market"] == TOTAL
    assert rep["n_games"] == 30 and rep["n_obs"] > 0
    assert "anchoring_gap" in rep and "staleness_min" in rep
    assert isinstance(rep["notes"], list)


def test_scan_winner_has_calibration_table():
    # mix of outcomes so calibration buckets populate
    panels = synthetic_panels(60, market=WINNER, signal=0.0)
    rep = eda.scan(panels, market=WINNER)
    assert isinstance(rep["calibration"], list)
    # each row is a price bucket with realized vs implied (idea-agnostic)
    for row in rep["calibration"]:
        assert {"lo", "hi", "n", "implied", "realized", "bias"} <= set(row)


def test_scan_empty():
    rep = eda.scan([], market=TOTAL)
    assert rep["n_games"] == 0 and "notes" in rep


def test_scan_contains_no_strategy_recommendation():
    # eda surfaces WHERE the book deviates, never WHAT to trade.
    rep = eda.scan(synthetic_panels(20, market=TOTAL), market=TOTAL)
    blob = str(rep).lower()
    for banned in ("buy", "sell", "fade", "long ", "short ", "bet "):
        assert banned not in blob


# --- EDA v2: derived lenses + regimes ---------------------------------------

def test_derived_features_shapes():
    from research.lab.types import TOTAL, synthetic_panel
    p = synthetic_panel(market=TOTAL, n=48, seed=1)
    feats = eda._derived_features(p)
    assert {"abs_margin", "margin_per_min", "mid_zscore", "margin_x_timeleft"} <= set(feats)
    assert all(len(v) == p.n for v in feats.values())


def test_residual_label_per_market():
    from research.lab.types import SPREAD, TOTAL, WINNER, synthetic_panel
    for m in (WINNER, TOTAL, SPREAD):
        p = synthetic_panel(market=m, n=48, seed=2)
        r = eda._residual_label(p)
        assert r is not None and len(r) == p.n
    # winner residual centers on home_won - mid (can be negative)
    pw = synthetic_panel(market=WINNER, n=48, seed=2)
    assert eda._residual_label(pw).min() < 1.0


def test_lens_informativeness_ranks_by_abs_corr():
    from research.lab.types import TOTAL, synthetic_panels
    coll = eda._collect_rows(synthetic_panels(40, market=TOTAL, signal=0.05), TOTAL)
    assert coll is not None
    X, resid, gap, S = coll
    lenses = eda.lens_informativeness(X, resid)
    assert lenses and {"lens", "corr_with_residual", "residual_spread", "n"} <= set(lenses[0])
    corrs = [abs(L["corr_with_residual"]) for L in lenses]
    assert corrs == sorted(corrs, reverse=True)   # sorted by |corr| desc


def test_regime_scan_returns_residual_stats():
    from research.lab.types import SPREAD, synthetic_panels
    coll = eda._collect_rows(synthetic_panels(60, market=SPREAD, signal=0.05), SPREAD)
    X, resid, gap, S = coll
    regimes = eda.regime_scan(S, resid, gap, k=3)
    assert regimes and all({"regime", "n", "mean_residual"} <= set(g) for g in regimes)


def test_scan_v2_populates_lenses_and_regimes():
    from research.lab.types import TOTAL, synthetic_panels
    rep = eda.scan(synthetic_panels(60, market=TOTAL, signal=0.05), market=TOTAL)
    assert rep["derived_lenses"]      # non-empty widened vocabulary
    assert rep["regimes"]             # unsupervised regimes present
