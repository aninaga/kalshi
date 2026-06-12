"""Tests for research.lab.strategy.

These run standalone using ONLY research.lab.types + synthetic fixtures + a STUB
fill_model (the sibling lab.execution unit may not be merged in this worktree).
The stub returns a fixed FillResult so the trade plumbing — one trade/game, honest
i+1 latency, freshness filtering, window filtering, settlement payoff — is what is
under test, not realistic pricing.
"""
from __future__ import annotations

import numpy as np
import pytest

from research.lab.strategy import Strategy, staleness_min
from research.lab.types import (
    FillResult,
    Panel,
    TOTAL,
    WINNER,
    synthetic_panel,
    synthetic_panels,
)


class StubFill:
    """Duck-typed fill_model: records calls, returns a fixed FillResult."""

    def __init__(self, fill_mid=0.40, strike=100.0, half_spread=0.015, fee=0.01):
        self._res = FillResult(strike=strike, fill_mid=fill_mid,
                               half_spread=half_spread, fee=fee)
        self.calls = []  # list of (game_id, ts, side)

    def fill(self, panel, ts, side):
        self.calls.append((panel.game_id, ts, side))
        return self._res


def _always_enter(panel):
    return np.ones(panel.n, dtype=bool)


def _over(panel, bar):
    return "over"


# ---------------------------------------------------------------------------

def test_one_trade_per_game():
    panels = synthetic_panels(n_games=5, market=TOTAL)
    strat = Strategy(name="t", entry=_always_enter, side=_over,
                     min_elapsed=600.0, max_elapsed=2520.0)
    trades = strat.run(panels, fill_model=StubFill())
    assert len(trades) == 5
    # exactly one trade per distinct game
    gids = [t.game_id for t in trades.rows]
    assert len(set(gids)) == 5


def test_honest_latency_entry_after_signal():
    panel = synthetic_panel(market=TOTAL)
    stub = StubFill()
    strat = Strategy(name="t", entry=_always_enter, side=_over,
                     entry_latency_min=1.0, min_elapsed=600.0)
    trades = strat.run([panel], fill_model=stub)
    assert len(trades) == 1
    trade = trades.rows[0]

    # the signal bar is the first in-window, fresh, True bar
    stale = staleness_min(panel)
    window = (panel.elapsed_sec >= 600.0) & (panel.elapsed_sec <= 2520.0)
    signal_bar = int(np.flatnonzero(window & (stale <= 2.0))[0])
    signal_ts = float(panel.minute_ts[signal_bar])

    # honest i+1: the fill ts is strictly after the signal ts
    assert trade.entry_ts > signal_ts
    assert trade.entry_ts == pytest.approx(signal_ts + 60.0)
    # the fill_model was called at the latency-shifted ts, not the signal ts
    assert stub.calls[0][1] == pytest.approx(signal_ts + 60.0)


def test_freshness_filtering_skips_stale_bars():
    panel = synthetic_panel(market=TOTAL)
    n = panel.n
    # Restrict the entry window so bar 0 (always fresh) cannot fire, then make
    # the line freeze through the early part of the window so those in-window
    # bars go stale, with a fresh step partway through. Only the step bar (and
    # the one right after, still within max_stale_min) should qualify.
    window_lo = panel.elapsed_sec[5]          # window starts at bar 5
    fresh_bar = n - 3                          # the line steps here -> fresh
    mid = panel.mid.copy()
    mid[1:fresh_bar] = mid[0]                  # flat from bar 1 -> stale builds
    mid[fresh_bar:] = mid[0] + 5.0            # a real step -> fresh again
    panel.mid = mid

    stub = StubFill()
    strat = Strategy(name="t", entry=_always_enter, side=_over,
                     max_stale_min=2.0, min_elapsed=window_lo, max_elapsed=1e9)
    trades = strat.run([panel], fill_model=stub)
    assert len(trades) == 1
    # Every in-window bar before fresh_bar is stale (> 2 min since bar-0 change),
    # so the first qualifying bar must be the fresh step bar itself.
    chosen_bar = trades.rows[0].meta["signal_bar"]
    assert chosen_bar == fresh_bar


def test_window_filtering_respects_min_max_elapsed():
    panel = synthetic_panel(market=TOTAL)
    # Force the window to a narrow late band; entry must land inside it.
    lo, hi = 1500.0, 1800.0
    strat = Strategy(name="t", entry=_always_enter, side=_over,
                     min_elapsed=lo, max_elapsed=hi, max_stale_min=1e9)
    trades = strat.run([panel], fill_model=StubFill())
    assert len(trades) == 1
    bar = trades.rows[0].meta["signal_bar"]
    assert lo <= panel.elapsed_sec[bar] <= hi


def test_no_trade_when_entry_never_true():
    panels = synthetic_panels(n_games=3, market=TOTAL)
    strat = Strategy(name="t", entry=lambda p: np.zeros(p.n, dtype=bool),
                     side=_over)
    trades = strat.run(panels, fill_model=StubFill())
    assert len(trades) == 0
    assert trades.empty


def test_settlement_payoff_total_over():
    panel = synthetic_panel(market=TOTAL)
    # Strike below the final total => an "over" bet settles to 1.0.
    strike = panel.final_total - 5.0
    strat = Strategy(name="t", entry=_always_enter, side=_over, min_elapsed=600.0)
    trades = strat.run([panel], fill_model=StubFill(strike=strike))
    t = trades.rows[0]
    assert t.payoff == 1.0
    assert t.entry_strike == pytest.approx(strike)
    # pnl = payoff - all_in (mid + half_spread + fee)
    expected_pnl = 1.0 - (0.40 + 0.015 + 0.01)
    assert t.pnl == pytest.approx(expected_pnl)


def test_settlement_payoff_total_under():
    panel = synthetic_panel(market=TOTAL)
    strike = panel.final_total - 5.0  # final > strike => "under" loses
    strat = Strategy(name="t", entry=_always_enter,
                     side=lambda p, b: "under", min_elapsed=600.0)
    trades = strat.run([panel], fill_model=StubFill(strike=strike))
    assert trades.rows[0].payoff == 0.0


def test_winner_market_settles_against_home_won():
    panel = synthetic_panel(market=WINNER, home_won=1.0)
    strat = Strategy(name="t", entry=_always_enter,
                     side=lambda p, b: "long_home", min_elapsed=600.0)
    trades = strat.run([panel], fill_model=StubFill(strike=None))
    assert trades.rows[0].payoff == 1.0  # bet home, home won
    # Betting away on the same game loses.
    strat_away = Strategy(name="t", entry=_always_enter,
                          side=lambda p, b: "long_away", min_elapsed=600.0)
    trades_away = strat_away.run([panel], fill_model=StubFill(strike=None))
    assert trades_away.rows[0].payoff == 0.0


def test_primary_team_orientation():
    panel = synthetic_panel(market=TOTAL)
    over = Strategy(name="t", entry=_always_enter, side=_over, min_elapsed=600.0)
    under = Strategy(name="t", entry=_always_enter,
                     side=lambda p, b: "under", min_elapsed=600.0)
    t_over = over.run([panel], fill_model=StubFill()).rows[0]
    t_under = under.run([panel], fill_model=StubFill()).rows[0]
    assert t_over.primary_team == panel.home_team
    assert t_under.primary_team == panel.away_team


def test_callable_exit_marks_to_market():
    panel = synthetic_panel(market=TOTAL)
    # Exit at a fixed mid-game bar; payoff should come from the ladder quote,
    # not settlement, and exit_ts must match that bar.
    exit_bar = panel.n - 5
    strat = Strategy(name="t", entry=_always_enter, side=_over,
                     exit=lambda p, b: exit_bar, min_elapsed=600.0)
    strike = sorted(panel.ladder.keys())[len(panel.ladder) // 2]
    trades = strat.run([panel], fill_model=StubFill(strike=strike))
    t = trades.rows[0]
    assert t.meta["exit_bar"] == exit_bar
    assert t.exit_ts == pytest.approx(float(panel.minute_ts[exit_bar]))
    expected = float(panel.ladder[strike][exit_bar])
    assert 0.0 <= t.payoff <= 1.0
    assert t.payoff == pytest.approx(expected)


def test_entry_mask_wrong_length_raises():
    panel = synthetic_panel(market=TOTAL)
    bad = Strategy(name="t", entry=lambda p: np.ones(p.n + 3, dtype=bool),
                   side=_over)
    with pytest.raises(ValueError):
        bad.run([panel], fill_model=StubFill())


def test_staleness_min_counts_minutes_since_change():
    panel = synthetic_panel(market=TOTAL)
    mid = panel.mid.copy()
    mid[:] = mid[0]
    mid[2:] = mid[0] + 1.0  # a single step change at bar 2 (level holds after)
    panel.mid = mid
    stale = staleness_min(panel)
    assert stale[0] == 0.0
    assert stale[1] == 1.0  # one bar since the (bar-0) change
    assert stale[2] == 0.0  # changed here
    assert stale[3] == 1.0  # one bar since the (bar-2) change
    assert stale[4] == 2.0


def test_smoke_returns_trades_of_expected_length():
    # E2E synthetic smoke: trivial Strategy on synthetic_panels -> Trades.
    panels = synthetic_panels(n_games=12, market=TOTAL, signal=8.0)
    strat = Strategy(name="smoke", entry=_always_enter, side=_over)
    trades = strat.run(panels, fill_model=StubFill())
    assert len(trades) == 12
    df = trades.df()
    assert len(df) == 12
    assert set(["game_id", "entry_ts", "exit_ts", "entry_price", "payoff"]).issubset(df.columns)


def test_side_taxonomy_consistent_with_execution():
    """fill (execution._SHORT_SIDES) and settlement (strategy._OVER_SIDES) must
    agree on every side's direction — a mismatch silently inverts spread P&L
    (regression: cover_home/cover_away were in neither set; found by the agent test)."""
    from research.lab import execution, strategy
    over, short = strategy._OVER_SIDES, execution._SHORT_SIDES
    assert over.isdisjoint(short)                      # no side both above and below
    assert "cover_home" in over and "cover_home" not in short   # long/above
    assert "cover_away" in short and "cover_away" not in over    # short/below


def test_spread_cover_home_fill_and_settlement_align():
    """cover_home = bet final home margin > strike: fill is long (p_over), and a
    game that finishes above the strike must settle as a WIN."""
    from research.lab import execution
    from research.lab.types import SPREAD, synthetic_panel
    p = synthetic_panel(market=SPREAD, n=48, seed=3)
    p.final_margin = max(p.ladder) + 50.0              # finishes above EVERY listed strike
    s = Strategy(name="t", entry=lambda pan: pan.elapsed_sec >= 0,
                 side=lambda pan, i: "cover_home", min_elapsed=0, max_elapsed=3000,
                 max_stale_min=10_000)
    trades = s.run([p], fill_model=execution.FillModel(half_spread=0.0))
    assert len(trades) == 1
    t = trades.rows[0]
    # cover_home bets "final home margin > filled strike": fill long, settle above.
    assert t.payoff == float(p.final_margin > t.entry_strike) == 1.0
    assert 0.0 < t.entry_price < 1.0                   # filled long at the real mid, not 0.50
