"""Tests for research.lab.audit — point-in-time / lookahead auditor.

All tests run on synthetic fixtures only (no real-data cache required). They
cover: the static source check, the dynamic no-lookahead perturbation test on
known-good vs deliberately-leaky signals, the truncate_panel helper, and the
report aggregator.
"""
from __future__ import annotations

import numpy as np
import pytest

from research.lab import audit, signals
from research.lab.types import TOTAL, SPREAD, Panel, synthetic_panel


# --------------------------------------------------------------------------- #
# truncate_panel
# --------------------------------------------------------------------------- #

def test_truncate_preserves_past_and_drops_future():
    p = synthetic_panel(market=TOTAL, n=48, seed=1)
    t = 20
    sub = audit.truncate_panel(p, t)

    assert sub.n == t + 1
    # bars 0..t preserved exactly across every per-bar array
    for name in ("minute_ts", "elapsed_sec", "margin", "total", "mid"):
        np.testing.assert_array_equal(
            np.asarray(getattr(sub, name)), np.asarray(getattr(p, name))[: t + 1])
    # ladder + features sliced too
    for k, arr in sub.ladder.items():
        assert len(arr) == t + 1
        np.testing.assert_array_equal(arr, np.asarray(p.ladder[k])[: t + 1])
    for k, arr in sub.features.items():
        assert len(arr) == t + 1


def test_truncate_neutralizes_outcome_fields():
    p = synthetic_panel(market=TOTAL, n=48, seed=2)
    assert p.final_total is not None  # sanity: original has a real outcome
    sub = audit.truncate_panel(p, 10)
    assert np.isnan(sub.home_won)
    assert np.isnan(sub.final_total)
    assert np.isnan(sub.final_margin)


def test_truncate_does_not_mutate_original():
    p = synthetic_panel(market=TOTAL, n=48, seed=3)
    orig_total = float(p.final_total)
    orig_mid = np.asarray(p.mid).copy()
    audit.truncate_panel(p, 5)
    assert float(p.final_total) == orig_total
    np.testing.assert_array_equal(np.asarray(p.mid), orig_mid)


def test_truncate_clamps_out_of_range():
    p = synthetic_panel(market=TOTAL, n=12, seed=4)
    assert audit.truncate_panel(p, 999).n == p.n
    assert audit.truncate_panel(p, -5).n == 1


# --------------------------------------------------------------------------- #
# audit_signal_no_lookahead — known-good signals pass
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fn,market", [
    (signals.staleness_min, TOTAL),
    (signals.anchoring_gap, TOTAL),
    (signals.pace_projection, TOTAL),
    (signals.implied_level, SPREAD),
    (lambda p: signals.zscore(np.asarray(p.mid, float), 5), TOTAL),
    (lambda p: signals.rolling(p, "mid", 5), TOTAL),
])
def test_known_good_signals_are_as_of_correct(fn, market):
    p = synthetic_panel(market=market, n=48, seed=7)
    res = audit.audit_signal_no_lookahead(fn, p)
    assert res["passed"], res["violations"]
    assert res["cut_points_tested"] >= 1


# --------------------------------------------------------------------------- #
# audit_signal_no_lookahead — leaky signals are CAUGHT
# --------------------------------------------------------------------------- #

def test_outcome_constant_signal_is_caught():
    """A signal that returns the final total at every bar peeks at the outcome.

    On a truncated panel the outcome is neutralized to NaN, so past bars move
    from final_total -> NaN, exposing the leak.
    """
    p = synthetic_panel(market=TOTAL, n=48, seed=8)

    def leaky(panel):
        return np.full(panel.n, float(panel.final_total))

    res = audit.audit_signal_no_lookahead(leaky, p)
    assert res["passed"] is False
    assert res["violations"]


def test_forward_looking_rolling_is_caught():
    """A FORWARD rolling mean (uses future bars) changes past bars on truncation."""
    p = synthetic_panel(market=TOTAL, n=48, seed=9)

    def forward_mean(panel):
        mid = np.asarray(panel.mid, float)
        out = np.full(panel.n, np.nan)
        for i in range(panel.n):
            out[i] = mid[i:].mean()  # looks AHEAD from bar i
        return out

    res = audit.audit_signal_no_lookahead(forward_mean, p)
    assert res["passed"] is False
    assert any("LOOKAHEAD" in v for v in res["violations"])


def test_reversed_cumsum_is_caught():
    p = synthetic_panel(market=TOTAL, n=48, seed=10)

    def rev_cumsum(panel):
        return np.cumsum(np.asarray(panel.total, float)[::-1])[::-1]

    res = audit.audit_signal_no_lookahead(rev_cumsum, p)
    assert res["passed"] is False
    assert res["violations"]


# --------------------------------------------------------------------------- #
# audit_callable_source — static check
# --------------------------------------------------------------------------- #

def test_source_check_flags_outcome_reference():
    def leaky(panel):
        return np.full(panel.n, panel.final_margin)

    flags = audit.audit_callable_source(leaky)
    assert flags
    assert any("final_margin" in f for f in flags)


def test_source_check_flags_home_won():
    def leaky(panel):
        return np.full(panel.n, float(panel.home_won))

    flags = audit.audit_callable_source(leaky)
    assert any("home_won" in f for f in flags)


def test_source_check_passes_clean_signal():
    assert audit.audit_callable_source(signals.staleness_min) == []
    assert audit.audit_callable_source(signals.anchoring_gap) == []


def test_source_check_robust_when_source_unavailable():
    # builtins have no retrievable Python source -> a NOTE, not a crash
    flags = audit.audit_callable_source(len)
    assert len(flags) == 1
    assert "source unavailable" in flags[0]


# --------------------------------------------------------------------------- #
# audit_report — aggregation
# --------------------------------------------------------------------------- #

def test_report_aggregates_pass_and_fail():
    p = synthetic_panel(market=TOTAL, n=48, seed=11)

    def leaky(panel):
        return np.full(panel.n, float(panel.final_total))

    report = audit.audit_report(p, {
        "staleness_min": signals.staleness_min,
        "anchoring_gap": signals.anchoring_gap,
        "leaky_outcome": leaky,
    })
    assert report["n_signals"] == 3
    assert report["all_passed"] is False
    assert "leaky_outcome" in report["failed_signals"]
    assert "staleness_min" not in report["failed_signals"]
    assert report["n_passed"] == 2
    assert report["n_failed"] == 1
    # leaky signal carries both dynamic violations and a static source flag
    assert report["signals"]["leaky_outcome"]["source_flags"]


def test_report_all_clean():
    p = synthetic_panel(market=TOTAL, n=48, seed=12)
    report = audit.audit_report(p, {
        "staleness_min": signals.staleness_min,
        "anchoring_gap": signals.anchoring_gap,
        "implied_level": signals.implied_level,
    })
    assert report["all_passed"] is True
    assert report["failed_signals"] == []
