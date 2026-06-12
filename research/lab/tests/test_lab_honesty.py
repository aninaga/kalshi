"""research.lab honesty regressions — audit defect C3 + exit-cost + repro freeze.

Covers the lab-honesty lane fixes:
  * C3 side-label taxonomy — ``long_away`` is now a SHORT side everywhere, the
    sets are disjoint, and an UNKNOWN label fails loud (no default-LONG).
  * Callable exits charge the exit-side half-spread + venue fee and reject an
    exit at/before the entry bar.
  * The nba_odds_study cache content-hash freeze (sidecar write / drift check).
"""
from __future__ import annotations

import numpy as np
import pytest

from research.lab import execution, strategy
from research.lab.strategy import Strategy
from research.lab.types import (
    KNOWN_SIDES,
    OVER_SIDES,
    SHORT_SIDES,
    TOTAL,
    WINNER,
    is_over_side,
    is_short_side,
    synthetic_panel,
)


# --------------------------------------------------------------------------- #
# C3 — side-label taxonomy: one source of truth, disjoint, long_away is short
# --------------------------------------------------------------------------- #


def test_taxonomy_is_disjoint_and_single_source():
    # Disjoint: no label is both an over and a short side.
    assert OVER_SIDES.isdisjoint(SHORT_SIDES)
    assert KNOWN_SIDES == OVER_SIDES | SHORT_SIDES
    # The three modules all AGREE on the canonical sets (no independent copy
    # with a different gap). Equality, not identity: the lab test suite purges
    # research.lab.* between tests, so the frozenset OBJECT can differ across
    # reloads even though the content is the single source of truth.
    assert strategy._OVER_SIDES == OVER_SIDES
    assert execution._SHORT_SIDES == SHORT_SIDES
    from research.lab import paper
    assert paper._OVER_SIDES == OVER_SIDES


def test_long_away_is_a_short_side_not_in_neither_set():
    """The exact C3 gap: ``long_away`` was in NEITHER set. It must now be SHORT
    (bets the away winner = complement of P(home))."""
    assert "long_away" in SHORT_SIDES
    assert "long_away" not in OVER_SIDES
    assert is_short_side("long_away") is True
    assert is_over_side("long_away") is False
    # Its long counterpart stays an over side.
    assert is_over_side("long_home") is True


def test_is_short_side_raises_on_unknown_label():
    with pytest.raises(ValueError):
        is_short_side("totally_made_up")
    with pytest.raises(ValueError):
        is_over_side("also_made_up")
    # Whitespace/case are tolerated (normalised), not treated as unknown.
    assert is_over_side("  LONG_HOME ") is True
    assert is_short_side("Long_Away") is True


def test_long_away_fill_and_settlement_align_no_free_edge():
    """E2E with the REAL FillModel: a deep home longshot faded via ``long_away``
    must price as the AWAY FAVORITE (~0.92) and lose money on the rare game it
    drops. Pre-fix it filled at the home/longshot price (~0.08) yet still settled
    as the away win — paying ~0.08 for a contract worth ~1.0, a fabricated
    ~+0.84/contract edge. The fix kills that wedge: fill and settlement now agree
    on the away orientation, so the per-contract edge collapses toward 0."""
    fm = execution.FillModel(venue="kalshi", half_spread=0.015)

    def fade(home_won):
        panel = synthetic_panel(market=WINNER, seed=7, home_won=home_won)
        panel.mid = np.full(panel.n, 0.08)               # home is a deep longshot
        s = Strategy(name="fade", entry=lambda p: np.ones(p.n, dtype=bool),
                     side=lambda p, i: "long_away", exit="settlement",
                     min_elapsed=0.0, max_elapsed=1e9, max_stale_min=1e9)
        return s.run([panel], fill_model=fm).rows[0]

    win = fade(home_won=0.0)    # away wins -> long_away pays 1
    loss = fade(home_won=1.0)   # away loses -> long_away pays 0

    # Always filled at the AWAY favorite price (1 - 0.08), never the longshot.
    assert win.entry_price == pytest.approx(0.92, abs=1e-9)
    assert loss.entry_price == pytest.approx(0.92, abs=1e-9)
    assert win.payoff == 1.0 and loss.payoff == 0.0

    # The fabricated edge is gone: paying ~0.945 all-in for a ~0.92-likely win is
    # a tiny per-contract number, NOT ~+0.84. The probability-weighted edge at the
    # quoted 92% win rate is ~0 (it is a fair favorite minus costs).
    ev = 0.92 * win.pnl + 0.08 * loss.pnl
    assert abs(ev) < 0.05            # near-zero, decisively far from +0.84
    assert win.pnl < 0.10            # the winning leg is a small favorite payout


# --------------------------------------------------------------------------- #
# Callable exit — charges exit-side costs and forbids exit <= entry
# --------------------------------------------------------------------------- #


def _flat_total_panel(prob: float = 0.40, n: int = 48, seed: int = 1):
    """A TOTAL panel with ONE flat strike at a fixed quote so the exit mark is
    a known constant; the implied mid is pinned to that strike so snapping is
    deterministic."""
    panel = synthetic_panel(market=TOTAL, seed=seed, n=n)
    strike = sorted(panel.ladder.keys())[len(panel.ladder) // 2]
    panel.ladder = {strike: np.full(n, prob)}
    panel.mid = np.full(n, float(strike))
    return panel, strike, prob


def test_callable_exit_pnl_drops_by_exactly_the_exit_costs():
    """An early (callable) exit pays the exit-side half-spread + venue fee. The
    same trade settled-vs-exited at the SAME gross value must differ in pnl by
    EXACTLY those exit costs (the bug: callable exits paid zero exit-side cost)."""
    panel, strike, prob = _flat_total_panel(prob=0.40)
    fm = execution.FillModel(venue="kalshi", half_spread=0.015)

    entry_kw = dict(name="t", entry=lambda p: np.ones(p.n, dtype=bool),
                    side=lambda p, i: "over", min_elapsed=0.0, max_elapsed=1e9,
                    max_stale_min=1e9, pick_strike=lambda p, i: strike)

    exit_bar = panel.n - 3
    exited = Strategy(exit=lambda p, b: exit_bar, **entry_kw).run(
        [panel], fill_model=fm).rows[0]

    # Gross exit mark equals the flat ladder quote for an "over" hold.
    gross = prob
    assert exited.payoff == pytest.approx(gross, abs=1e-9)

    # The exit cost recorded in meta is the exit-side half-spread + venue fee on
    # the gross mark, and pnl is net of it.
    expected_exit_cost = fm.half_spread + fm.fee(gross)
    assert exited.meta["exit_cost"] == pytest.approx(expected_exit_cost, abs=1e-12)
    assert exited.pnl == pytest.approx(
        exited.payoff - exited.meta["all_in_price"] - expected_exit_cost,
        abs=1e-12)

    # Compare to the SAME trade if the exit cost were (wrongly) zero: pnl must be
    # LOWER by exactly the exit cost.
    pnl_if_free_exit = exited.payoff - exited.meta["all_in_price"]
    assert pnl_if_free_exit - exited.pnl == pytest.approx(
        expected_exit_cost, abs=1e-12)
    assert expected_exit_cost > 0.0


def test_settlement_exit_pays_no_exit_cost():
    """Hold-to-settlement has no closing transaction -> exit_cost is exactly 0."""
    panel, strike, _ = _flat_total_panel(prob=0.40)
    fm = execution.FillModel(venue="kalshi", half_spread=0.015)
    t = Strategy(name="t", entry=lambda p: np.ones(p.n, dtype=bool),
                 side=lambda p, i: "over", exit="settlement", min_elapsed=0.0,
                 max_elapsed=1e9, max_stale_min=1e9,
                 pick_strike=lambda p, i: strike).run([panel], fill_model=fm).rows[0]
    assert t.meta["exit_cost"] == 0.0
    assert t.pnl == pytest.approx(t.payoff - t.meta["all_in_price"], abs=1e-12)


def test_callable_exit_before_or_at_entry_raises():
    """A callable exit may not close before/at the open (same-bar or look-ahead
    round trip). It must RAISE, not silently book a zero-hold trade."""
    panel, strike, _ = _flat_total_panel(prob=0.40)
    fm = execution.FillModel(venue="kalshi", half_spread=0.015)
    kw = dict(name="t", entry=lambda p: np.ones(p.n, dtype=bool),
              side=lambda p, i: "over", min_elapsed=0.0, max_elapsed=1e9,
              max_stale_min=1e9, pick_strike=lambda p, i: strike)

    # Exit at bar 0 while entry fires at bar 0 -> exit == entry -> raise.
    with pytest.raises(ValueError):
        Strategy(exit=lambda p, b: b, **kw).run([panel], fill_model=fm)
    # Exit strictly before entry -> raise.
    with pytest.raises(ValueError):
        Strategy(exit=lambda p, b: max(b - 1, 0), **kw).run([panel], fill_model=fm)


def test_callable_exit_after_entry_is_accepted():
    panel, strike, _ = _flat_total_panel(prob=0.40)
    fm = execution.FillModel(venue="kalshi", half_spread=0.015)
    t = Strategy(name="t", entry=lambda p: np.ones(p.n, dtype=bool),
                 side=lambda p, i: "over", exit=lambda p, b: b + 5,
                 min_elapsed=0.0, max_elapsed=1e9, max_stale_min=1e9,
                 pick_strike=lambda p, i: strike).run([panel], fill_model=fm).rows[0]
    assert t.meta["exit_bar"] > t.meta["signal_bar"]


# --------------------------------------------------------------------------- #
# Data-repro freeze — content hash + sidecar (nba_odds_study.cache_hash)
# --------------------------------------------------------------------------- #


class _FakeDataset:
    """Minimal stand-in carrying just the two hashed frames."""

    def __init__(self, odds_long, minute):
        self.odds_long = odds_long
        self.minute = minute


def _toy_dataset(seed: int = 0):
    import pandas as pd
    rng = np.random.default_rng(seed)
    odds = pd.DataFrame({
        "ts": [0, 60, 120, 60, 120],
        "platform": ["kalshi", "kalshi", "kalshi", "pm", "pm"],
        "kind": ["total"] * 5,
        "strike": [220.0, 220.0, 220.0, 221.0, 221.0],
        "prob": rng.random(5).round(4),
    })
    minute = pd.DataFrame({"margin": [1.0, 2.0, 3.0], "total": [10.0, 20.0, 30.0]},
                          index=[0, 60, 120])
    return _FakeDataset(odds, minute)


def test_content_hash_is_stable_and_order_invariant():
    from research.nba_odds_study import cache_hash
    d = _toy_dataset(seed=1)
    h1 = cache_hash.content_hash(d)
    # Recompute -> identical.
    assert cache_hash.content_hash(d) == h1
    # Row/column reorder of odds_long must NOT change the hash (canonicalised).
    shuffled = d.odds_long.sample(frac=1.0, random_state=3)[
        ["prob", "ts", "kind", "platform", "strike"]]
    d2 = _FakeDataset(shuffled.reset_index(drop=True), d.minute)
    assert cache_hash.content_hash(d2) == h1


def test_content_hash_changes_when_a_quote_changes():
    from research.nba_odds_study import cache_hash
    d = _toy_dataset(seed=2)
    h1 = cache_hash.content_hash(d)
    d.odds_long.loc[0, "prob"] = float(d.odds_long.loc[0, "prob"]) + 0.01
    assert cache_hash.content_hash(d) != h1


def test_sidecar_roundtrip_and_no_drift(tmp_path):
    from research.nba_odds_study import cache_hash
    pkl = tmp_path / "g.pkl"
    pkl.write_bytes(b"unused")
    d = _toy_dataset(seed=4)
    digest = cache_hash.write_sidecar(str(pkl), d)
    assert cache_hash.sidecar_path(str(pkl)).endswith(".sha256")
    assert cache_hash.read_sidecar(str(pkl)) == digest
    ok, recorded, current = cache_hash.check_sidecar(str(pkl), d)
    assert ok and recorded == current == digest


def test_check_sidecar_warns_on_drift_but_never_raises(tmp_path):
    from research.nba_odds_study import cache_hash
    pkl = tmp_path / "g.pkl"
    pkl.write_bytes(b"unused")
    d = _toy_dataset(seed=5)
    cache_hash.write_sidecar(str(pkl), d)
    # Mutate the content (simulate a re-fetch restating a quote).
    d.odds_long.loc[1, "prob"] = float(d.odds_long.loc[1, "prob"]) + 0.05
    with pytest.warns(UserWarning):
        ok, recorded, current = cache_hash.check_sidecar(str(pkl), d, warn=True)
    assert ok is False
    assert recorded != current            # drift detected
    # No sidecar yet -> ok=True (nothing to drift from), no warning.
    pkl2 = tmp_path / "fresh.pkl"
    pkl2.write_bytes(b"unused")
    ok2, recorded2, _ = cache_hash.check_sidecar(str(pkl2), d, warn=True)
    assert ok2 is True and recorded2 is None
