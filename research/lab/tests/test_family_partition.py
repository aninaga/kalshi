"""Tests for the per-family DSR partition + Panel.duration_sec threading.

The expansion contract: independent event-class families get independent
multiple-testing hurdles (legacy family-less records conservatively count
toward EVERY family), defaults stay byte-identical to the historical global
behavior, and untimed event classes gracefully no-op the pace/anchoring
signal family while the generic families keep working.
"""
from __future__ import annotations

import json

import numpy as np

from research.lab import governance as G
from research.lab import signals
from research.lab.types import TOTAL, synthetic_panel


def _write_ledger(tmp_path, rows) -> str:
    path = str(tmp_path / "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def _row(hid, *, family=None, market=None, net=4.0, ci_lo=0.5, ci_hi=7.5):
    row = {"hypothesis_id": hid, "verdict": "DEAD",
           "results": {"net_cents_0c": net, "ci_lo": ci_lo, "ci_hi": ci_hi}}
    if family is not None:
        row["family"] = family
    if market is not None:
        row["market"] = market
    return row


def _empty_registry(tmp_path) -> str:
    return str(tmp_path / "hypotheses.jsonl")


# --- trial_count partition ---------------------------------------------------


def test_family_partitions_trial_count(tmp_path):
    led = _write_ledger(tmp_path, [
        _row("n1", family="nba"), _row("n2", family="nba"),
        _row("w1", family="weather"),
    ])
    reg = _empty_registry(tmp_path)
    assert G.trial_count(led, registry_path=reg) == 3                      # global
    assert G.trial_count(led, registry_path=reg, family="nba") == 2
    assert G.trial_count(led, registry_path=reg, family="weather") == 1
    # An unseen family still floors at 1 (the candidate itself is a trial).
    assert G.trial_count(led, registry_path=reg, family="econ") == 1


def test_legacy_rows_count_toward_every_family(tmp_path):
    # Rows with no family key and no resolvable market are CONSERVATIVE:
    # they raise the hurdle for every family.
    led = _write_ledger(tmp_path, [
        _row("legacy1"), _row("legacy2"),
        _row("w1", family="weather"),
    ])
    reg = _empty_registry(tmp_path)
    assert G.trial_count(led, registry_path=reg, family="weather") == 3
    assert G.trial_count(led, registry_path=reg, family="nba") == 2
    assert G.trial_count(led, registry_path=reg) == 3


def test_family_derived_from_market_when_key_absent(tmp_path):
    # A row with no "family" but market="total" resolves to the NBA family via
    # the provider registry, so it does NOT count toward "weather".
    led = _write_ledger(tmp_path, [
        _row("t1", market=TOTAL),
        _row("w1", family="weather"),
    ])
    reg = _empty_registry(tmp_path)
    assert G.trial_count(led, registry_path=reg, family="nba") == 1
    assert G.trial_count(led, registry_path=reg, family="weather") == 1


# --- sharpe variance / governance_params partition ---------------------------


def test_sharpe_variance_partitioned(tmp_path):
    led = _write_ledger(tmp_path, [
        _row("n1", family="nba", net=1.0, ci_lo=0.2, ci_hi=1.8),
        _row("n2", family="nba", net=5.0, ci_lo=1.0, ci_hi=9.0),
        _row("w1", family="weather", net=3.0, ci_lo=-1.0, ci_hi=7.0),
    ])
    reg = _empty_registry(tmp_path)
    # weather alone has <2 usable Sharpe rows -> placeholder 1.0.
    assert G.sharpe_variance(led, registry_path=reg, family="weather") == 1.0
    # nba alone has 2 -> a real (non-placeholder-source) estimate.
    p_nba = G.governance_params(led, registry_path=reg, family="nba")
    assert p_nba["n_trials"] == 2
    assert p_nba["sharpe_variance_source"] == "real-ledger"
    assert p_nba["family"] == "nba"


def test_default_shape_and_counts_unchanged(tmp_path):
    # family=None must reproduce the historical return shape exactly (no
    # "family" key) and the global counts.
    led = _write_ledger(tmp_path, [
        _row("n1", family="nba"), _row("w1", family="weather"),
    ])
    p = G.governance_params(led, registry_path=_empty_registry(tmp_path))
    assert set(p) == {"n_trials", "sharpe_variance",
                      "n_trials_source", "sharpe_variance_source"}
    assert p["n_trials"] == 2


# --- duration_sec threading ---------------------------------------------------


def test_legacy_panel_defaults_to_nba_duration(tmp_path):
    p = synthetic_panel(market=TOTAL, n=24)
    assert p.duration_sec == 2880.0
    proj = signals.pace_projection(p)
    # Beyond the early-game guard the projection is finite and scaled by 2880.
    i = int(np.argmax(p.elapsed_sec > 120.0))
    assert np.isfinite(proj[i:]).all()
    np.testing.assert_allclose(
        proj[i:], np.asarray(p.total, float)[i:] * 2880.0
        / np.asarray(p.elapsed_sec, float)[i:])


def test_custom_duration_scales_pace(tmp_path):
    p = synthetic_panel(market=TOTAL, n=24)
    p.duration_sec = 5400.0  # e.g. soccer
    proj = signals.pace_projection(p)
    i = int(np.argmax(p.elapsed_sec > 120.0))
    np.testing.assert_allclose(
        proj[i:], np.asarray(p.total, float)[i:] * 5400.0
        / np.asarray(p.elapsed_sec, float)[i:])


def test_untimed_panel_pace_is_nan_but_generic_signals_work(tmp_path):
    p = synthetic_panel(market=TOTAL, n=24)
    p.duration_sec = None  # untimed event class (e.g. a CPI print)
    assert np.isnan(signals.pace_projection(p)).all()
    assert np.isnan(signals.anchoring_gap(p)).all()
    # Generic families are unaffected: staleness + implied level still compute.
    assert np.isfinite(signals.staleness_min(p)).all()
    assert np.isfinite(signals.implied_level(p)).all()


def test_untimed_panel_eda_drops_clock_lenses(tmp_path):
    from research.lab import eda

    p = synthetic_panel(market=TOTAL, n=24)
    feats_timed = eda._derived_features(p)
    assert "time_remaining" in feats_timed and "margin_x_timeleft" in feats_timed
    p.duration_sec = None
    feats_untimed = eda._derived_features(p)
    assert "time_remaining" not in feats_untimed
    assert "margin_x_timeleft" not in feats_untimed
    assert "mid_zscore" in feats_untimed  # generic lenses remain
