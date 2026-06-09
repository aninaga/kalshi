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
