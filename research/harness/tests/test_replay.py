"""Tests for the Wave 1 single-game replay engine.

The replay engine is the load-bearing piece of Phase 0 — every backtest,
overfit-canary test, and promotion gate routes through it. These tests
pin down the invariants in the engine's docstring:

1. live-availability enforcement (feature-unavailable -> skip, not crash);
2. no-lookahead (exit uses bar t+1's orderbook, never the entry bar's);
3. at most one open position per game;
4. position state machine None -> OPEN -> CLOSED -> None can re-enter;
5. settlement of still-open positions at end of game.

Plus a reproducibility test so we know a (spec, game_id, seed) tuple
always yields a byte-identical TrialResult.

The tests build a *fake* lake (a tmpdir wired into the lake schema) so
the suite stays hermetic — it does not depend on whatever happens to be
imported in market_data/lake at the time the test runs.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from unittest import mock

import pandas as pd

from research.features.registry import REGISTRY
from research.harness import replay as replay_module
from research.harness.replay import (
    Trade,
    TrialResult,
    replay_game,
    synthesize_orderbook_from_mid,
)
from research.harness.strategy_spec import Condition, SizingRule, StrategySpec
from research.lake import reader as lake_reader
from research.lake.reader import TestSetAccessError


# --------------------------------------------------------------------------- #
# Test fixture: synthesise one game's lake shards in a tmpdir
# --------------------------------------------------------------------------- #


def _make_pbp_df(
    game_id: str,
    n_bars: int = 60,
    start_ts: int = 1_700_000_000,
    minute_step: int = 60,
    home_score_pattern=None,
    away_score_pattern=None,
    elapsed_offset: float = -300.0,
    start_elapsed: float = 0.0,
) -> pd.DataFrame:
    """Build a deterministic pbp DataFrame with home trailing through the game.

    ``elapsed_offset`` controls when ``elapsed_game_sec`` becomes non-null;
    by default the first 5 bars (5*60 = 300s) of warm-up have NaN clocks.
    """
    rows = []
    for i in range(n_bars):
        ts = start_ts + i * minute_step
        # Game clock: ramps up from start_elapsed by 60s per bar after warm-up.
        elapsed = start_elapsed + i * 60.0 + elapsed_offset
        if elapsed < 0:
            elapsed = None
        # Default score evolution: away pulls ahead linearly so the home team is
        # trailing by ~6 points by minute 24 (halftime).
        if home_score_pattern is None:
            home = float(i)
        else:
            home = float(home_score_pattern[i])
        if away_score_pattern is None:
            away = float(i + 6) if i >= 1 else 0.0
        else:
            away = float(away_score_pattern[i])
        margin = home - away
        rows.append(
            dict(
                game_id=game_id,
                date="2025-11-01",
                home_tri="HOM",
                away_tri="AWY",
                minute_ts=ts,
                elapsed_game_sec=elapsed,
                home_score=home,
                away_score=away,
                margin=margin,
                pts_home=0.0,
                pts_away=0.0,
                total=home + away,
                n_subs=0,
                n_star_subs=0,
                lead_changes=0,
                home_stars_on=0,
                away_stars_on=0,
            )
        )
    return pd.DataFrame(rows)


def _make_pm_df(
    game_id: str,
    pbp: pd.DataFrame,
    mid_pattern=None,
) -> pd.DataFrame:
    """Build a Polymarket-ticks DataFrame keyed off the same minute_ts grid.

    ``mid_pattern`` lets the test prescribe a price series; default is 0.45
    everywhere (so the away side, which the home-trailing spec longs, is
    priced at 0.55).
    """
    rows = []
    for i, row in pbp.iterrows():
        if mid_pattern is None:
            mid = 0.45
        else:
            mid = float(mid_pattern[i])
        rows.append(
            dict(
                game_id=game_id,
                minute_ts=row["minute_ts"],
                token_id="tok_home_yes",
                team="home",
                mid=mid,
            )
        )
    return pd.DataFrame(rows)


def _make_games_df(
    game_id: str,
    first_ts: int,
    last_ts: int,
    home_won: bool = False,
    split=None,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            dict(
                game_id=game_id,
                date="2025-11-01",
                home_tri="HOM",
                away_tri="AWY",
                first_ts=first_ts,
                last_ts=last_ts,
                final_home=100.0,
                final_away=110.0,
                home_won=home_won,
                stars_home=None,
                stars_away=None,
                starters_home=None,
                starters_away=None,
                n_subs=0,
                n_lead_changes=0,
                n_runs=0,
                has_espn_wp=False,
                variant=None,
                split=split,
            )
        ]
    )


def _write_fake_lake(
    lake_root: Path,
    game_id: str,
    pbp: pd.DataFrame,
    pm: pd.DataFrame,
    games: pd.DataFrame,
    subs: Optional[pd.DataFrame] = None,
    kalshi: Optional[pd.DataFrame] = None,
) -> None:
    """Persist the four/five shards under the fake lake root."""
    for sub in ("pbp", "polymarket_ticks", "games", "kalshi_ticks", "subs"):
        (lake_root / sub).mkdir(parents=True, exist_ok=True)
    pbp.to_parquet(lake_root / "pbp" / f"{game_id}.parquet", index=False)
    pm.to_parquet(
        lake_root / "polymarket_ticks" / f"{game_id}.parquet", index=False
    )
    games.to_parquet(lake_root / "games" / f"{game_id}.parquet", index=False)
    if kalshi is None:
        kalshi = pd.DataFrame(
            columns=[
                "game_id",
                "minute_ts",
                "series",
                "ticker",
                "team",
                "yes_bid",
                "yes_ask",
                "mid",
                "last",
            ]
        )
    kalshi.to_parquet(
        lake_root / "kalshi_ticks" / f"{game_id}.parquet", index=False
    )
    if subs is None:
        subs = pd.DataFrame(
            columns=[
                "game_id",
                "date",
                "ts",
                "team",
                "player_in",
                "player_out",
                "is_star",
            ]
        )
    subs.to_parquet(lake_root / "subs" / f"{game_id}.parquet", index=False)


class _FakeLakeMixin:
    """Mixin that gives each test a tmpdir-backed lake patched into
    research.lake.schema.LAKE_ROOT and related helpers.

    The patches are applied in setUp and torn down in tearDown. We use
    monkeypatching rather than environment variables because the lake's
    schema constants are read at import time.
    """

    def _setup_fake_lake(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="kalshi_replay_test_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)  # type: ignore[attr-defined]

        # Patch every reference to LAKE_ROOT we know about.
        patcher_schema = mock.patch(
            "research.lake.schema.LAKE_ROOT", tmp
        )
        patcher_schema.start()
        self.addCleanup(patcher_schema.stop)  # type: ignore[attr-defined]

        # The schema's table_dir() reads LAKE_ROOT from the module global,
        # so patching the global is enough.
        return tmp


# --------------------------------------------------------------------------- #
# Strategy-spec helpers
# --------------------------------------------------------------------------- #


def _spec_never_fires() -> StrategySpec:
    return StrategySpec(
        name="never_fires",
        description="entry condition is literally False",
        features=["margin"],
        entry_condition=Condition(expr="False"),
        exit_conditions=[Condition(expr="True")],
        sizing=SizingRule(mode="fixed_contracts", value=5),
        time_stop_sec=240,
        side="long_home",
        venue="polymarket",
    )


def _spec_buy_trailing(
    time_stop_sec: int = 240,
    exit_expr: str = "False",
) -> StrategySpec:
    return StrategySpec(
        name="buy_trailing",
        description="enter when home is trailing by more than 3",
        features=["margin"],
        entry_condition=Condition(
            expr="margin < -3 and pos_side is None"
        ),
        exit_conditions=[Condition(expr=exit_expr)],
        sizing=SizingRule(mode="fixed_contracts", value=5),
        time_stop_sec=time_stop_sec,
        side="long_trailing",
        venue="polymarket",
    )


def _spec_buy_hot() -> StrategySpec:
    """Spec that references recent_run_signed (available_at_offset_sec=240).

    Used to verify that bars before 240s game clock are skipped (not crashed).
    """
    return StrategySpec(
        name="buy_hot",
        description="hot-team long, refuses bars before 240s game clock",
        features=["margin", "recent_run_signed"],
        entry_condition=Condition(
            expr="recent_run_signed > 0 and pos_side is None"
        ),
        exit_conditions=[Condition(expr="True")],
        sizing=SizingRule(mode="fixed_contracts", value=5),
        time_stop_sec=240,
        side="long_hot",
        venue="polymarket",
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


GAME_ID = "2025-11-01_AWY_at_HOM"


class TestNeverFires(unittest.TestCase, _FakeLakeMixin):
    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        pbp = _make_pbp_df(GAME_ID)
        pm = _make_pm_df(GAME_ID, pbp)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_replay_returns_empty_trial_for_never_firing_spec(self) -> None:
        spec = _spec_never_fires()
        result = replay_game(spec, GAME_ID)
        self.assertEqual(result.n_trades, 0)
        self.assertEqual(result.trades, [])
        self.assertEqual(result.pnl_gross, 0.0)
        self.assertEqual(result.pnl_net, 0.0)
        self.assertEqual(result.spec_hash, spec.spec_hash())


class TestTimeStop(unittest.TestCase, _FakeLakeMixin):
    """Spec enters on margin<-3, exit_expr=False so only time-stop can fire."""

    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        # 60 bars; margin is home-away; pbp default makes home trail by ~6
        # from minute 1 onwards. We further nudge so the entry fires at bar
        # 5 (the first bar past warm-up).
        pbp = _make_pbp_df(GAME_ID, n_bars=20)
        pm = _make_pm_df(GAME_ID, pbp)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_replay_time_stop_fires(self) -> None:
        # time_stop_sec=180 -> exit after the first bar that's 3 game-minutes
        # past entry. We use exit_expr="False" so nothing else can close.
        spec = _spec_buy_trailing(time_stop_sec=180, exit_expr="False")
        result = replay_game(spec, GAME_ID)
        self.assertGreater(result.n_trades, 0)
        # All non-settlement closures should be time-stops (no explicit exit).
        non_settle = [
            t for t in result.trades
            if t.exit_reason not in ("settle_at_one", "settle_at_zero")
        ]
        self.assertTrue(non_settle, "expected at least one non-settle trade")
        for t in non_settle:
            self.assertIn(
                t.exit_reason,
                ("time_stop", "end_of_game"),
                f"unexpected exit_reason={t.exit_reason}",
            )

        # Verify the first trade respects the time stop (hold >= 180s).
        first = result.trades[0]
        held = first.exit_ts_game_sec - first.entry_ts_game_sec
        if first.exit_reason == "time_stop":
            self.assertGreaterEqual(held, 180.0)


class TestExplicitExit(unittest.TestCase, _FakeLakeMixin):
    """Spec entering on margin<-3 with exit margin>-1."""

    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        # Craft a score path that swings: home trails by 6 for a while,
        # then catches up to within 1 by bar ~12.
        n = 20
        home = list(range(0, n))  # 0,1,2,...
        # away starts at +6 ahead but slows down — home margin recovers.
        away = []
        for i in range(n):
            if i < 10:
                away.append(home[i] + 6)
            else:
                # Once we cross bar 10, away stops scoring -> margin closes.
                away.append(home[9] + 6)
        pbp = _make_pbp_df(
            GAME_ID,
            n_bars=n,
            home_score_pattern=home,
            away_score_pattern=away,
        )
        pm = _make_pm_df(GAME_ID, pbp)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_replay_position_closes_on_exit_condition(self) -> None:
        spec = _spec_buy_trailing(time_stop_sec=7200, exit_expr="margin > -1")
        result = replay_game(spec, GAME_ID)
        self.assertGreater(result.n_trades, 0)
        # At least one trade must close via explicit_exit. (Others may
        # settle if the last bar still has a position open.)
        explicit = [t for t in result.trades if t.exit_reason == "explicit_exit"]
        self.assertGreater(
            len(explicit), 0, f"no explicit_exit trades: {result.trades}"
        )


class TestNoLookahead(unittest.TestCase, _FakeLakeMixin):
    """Verify (a) entry decisions only see data up through bar t, and
    (b) BOTH entry and exit fills come from bar t+1's orderbook (symmetric
    1-bar execution latency — see the 2026-05-28 latency-asymmetry fix)."""

    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        pbp = _make_pbp_df(GAME_ID, n_bars=15)
        # Construct a PM mid pattern that's distinctive per-bar so we can
        # tell whether the exit price came from bar t+1 (next minute) or t.
        # 0.03/bar step: distinctive per-bar AND larger than the 1¢ half-spread,
        # so the exit-price check can't be confounded by spread when entry and
        # exit fill bars are only one apart (symmetric-latency semantics).
        mid_pattern = [0.40 + 0.03 * i for i in range(15)]
        pm = _make_pm_df(GAME_ID, pbp, mid_pattern=mid_pattern)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_replay_no_lookahead(self) -> None:
        # exit_expr=True so the exit condition fires on the first bar the
        # position exists. With symmetric 1-bar entry+exit latency the entry
        # fill bar and exit fill bar are adjacent -> gap = 1 bar = 60s for
        # mid-game trades. End-of-game trades (last bar has no t+1) are
        # excluded from the gap check because they mark-to-market at the
        # current bar's mid.
        spec = _spec_buy_trailing(time_stop_sec=7200, exit_expr="True")
        result = replay_game(spec, GAME_ID)
        self.assertGreater(result.n_trades, 0)
        # Find an in-game trade (not the end_of_game / settlement one) to
        # verify the t+1 gap. Settlement / end-of-game trades use the
        # current bar's mid by design — see _settle_position / the
        # no-t+1 branch in replay_game.
        in_game = [
            t for t in result.trades
            if t.exit_reason == "explicit_exit"
        ]
        self.assertGreater(
            len(in_game), 0,
            "no in-game explicit_exit trades to check t+1 gap",
        )
        for t in in_game:
            # The exit_ts_wall MUST be after entry_ts_wall.
            self.assertGreater(
                t.exit_ts_wall, t.entry_ts_wall,
                "exit wall-clock must follow entry wall-clock",
            )
            # Symmetric 1-bar execution latency (fixed 2026-05-28): entries now
            # also fill one bar AFTER their signal, exactly like exits. With
            # exit_expr=True the exit fires on the first bar the position
            # exists and executes one bar later, so the entry-fill bar and the
            # exit-fill bar are adjacent -> gap = 1 bar = 60s.
            self.assertEqual(
                t.exit_ts_wall - t.entry_ts_wall, 60,
                "in-game exit should execute one bar after the exit-"
                "condition fires (symmetric t+1 latency)",
            )
            # Verify the exit price comes from the bar AFTER the entry-fill bar
            # (one bar of exit latency), not the entry-fill bar itself. The
            # spec is long_trailing and home is trailing, so side resolved to
            # long_home; entry_price ≈ home_mid + half-spread, exit_price ≈
            # home_mid at the exit bar - half-spread.
            entry_mid_idx = (t.entry_ts_wall - 1_700_000_000) // 60
            exit_mid_idx = entry_mid_idx + 1
            expected_exit_home_mid = 0.40 + 0.03 * exit_mid_idx
            # Allow 1¢ for the bid/ask spread modeling.
            self.assertAlmostEqual(
                t.exit_price, expected_exit_home_mid, delta=0.012,
                msg=(
                    f"exit price {t.exit_price:.4f} should reflect the exit "
                    f"bar's home mid ~ {expected_exit_home_mid:.4f}; pattern leak"
                ),
            )
            # And NOT the entry-fill bar's mid.
            expected_entry_home_mid = 0.40 + 0.03 * entry_mid_idx
            self.assertGreater(
                abs(t.exit_price - expected_entry_home_mid), 0.005,
                msg=(
                    "exit price matches the entry bar's mid — lookahead "
                    "violation suspected."
                ),
            )


class TestSettlement(unittest.TestCase, _FakeLakeMixin):
    """Spec opens a position that never satisfies the exit, so it must
    settle at the end of the game with exit_reason=settle_at_zero/one."""

    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        pbp = _make_pbp_df(GAME_ID, n_bars=15)
        pm = _make_pm_df(GAME_ID, pbp)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
            home_won=False,
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_replay_settles_open_position(self) -> None:
        # No exit ever fires; time_stop > game length; -> settlement.
        spec = _spec_buy_trailing(time_stop_sec=7200, exit_expr="False")
        result = replay_game(spec, GAME_ID)
        self.assertGreater(result.n_trades, 0)
        # The LAST trade should be the one that settled.
        last = result.trades[-1]
        self.assertIn(
            last.exit_reason,
            ("settle_at_one", "settle_at_zero"),
            f"unexpected exit_reason={last.exit_reason}",
        )
        # exit_price must be 0.0 (home didn't win, side=long_trailing=long_away
        # when home margin<0, so the away side won -> settle_at_one).
        # Either way, exit_price ∈ {0.0, 1.0}.
        self.assertIn(last.exit_price, (0.0, 1.0))


class TestFeatureUnavailableSkipsBar(unittest.TestCase, _FakeLakeMixin):
    """recent_run_signed has available_at_offset_sec=240; bars whose
    elapsed_game_sec < 240 must be SKIPPED (not crash)."""

    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        # 8 bars total. With elapsed_offset=-300 default, the first 5 bars
        # have NaN elapsed and get dropped by _build_bars. Bars 5,6,7 have
        # elapsed 0/60/120/180 — i.e. all below the 240s threshold.
        pbp = _make_pbp_df(
            GAME_ID, n_bars=8, elapsed_offset=-300.0
        )
        pm = _make_pm_df(GAME_ID, pbp)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_replay_feature_unavailable_skips_bar(self) -> None:
        spec = _spec_buy_hot()
        result = replay_game(spec, GAME_ID)
        # No trades should have fired (all eligible bars were pre-240).
        self.assertEqual(result.n_trades, 0)
        # We should see "recent_run_signed unavailable" in skipped reasons.
        unavailable_msgs = [
            s for s in result.skipped if "recent_run_signed" in s and "unavailable" in s
        ]
        self.assertGreater(
            len(unavailable_msgs), 0,
            f"expected at least one 'recent_run_signed unavailable' skip; got {result.skipped}"
        )


class TestTestSetRefused(unittest.TestCase, _FakeLakeMixin):
    """A game with split='test' must require allow_test=True to load."""

    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        pbp = _make_pbp_df(GAME_ID, n_bars=10)
        pm = _make_pm_df(GAME_ID, pbp)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
            split="test",
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_replay_test_set_refused(self) -> None:
        spec = _spec_buy_trailing()
        with self.assertRaises(TestSetAccessError):
            replay_game(spec, GAME_ID)

        # ...but allow_test=True works.
        result = replay_game(spec, GAME_ID, allow_test=True)
        self.assertIsInstance(result, TrialResult)


class TestReproducibility(unittest.TestCase, _FakeLakeMixin):
    """Replaying the same (spec, game_id, seed) twice must produce a
    byte-identical TrialResult."""

    def setUp(self) -> None:
        self.lake = self._setup_fake_lake()
        pbp = _make_pbp_df(GAME_ID, n_bars=20)
        pm = _make_pm_df(GAME_ID, pbp)
        games = _make_games_df(
            GAME_ID,
            first_ts=int(pbp["minute_ts"].min()),
            last_ts=int(pbp["minute_ts"].max()),
        )
        _write_fake_lake(self.lake, GAME_ID, pbp, pm, games)

    def test_trial_result_reproducible(self) -> None:
        spec = _spec_buy_trailing(time_stop_sec=180, exit_expr="margin > -1")
        r1 = replay_game(spec, GAME_ID, rng_seed=42)
        r2 = replay_game(spec, GAME_ID, rng_seed=42)
        # Compare as JSON; the dataclass contains nested FillResult so we
        # can't equality-check raw bytes from asdict without converting
        # tuples to lists (which json.dumps does for free).
        self.assertEqual(
            json.dumps(asdict(r1), sort_keys=True, default=str),
            json.dumps(asdict(r2), sort_keys=True, default=str),
        )


if __name__ == "__main__":
    unittest.main()
