"""Tests for research.lab.data (Unit 1 — Panel loaders).

These run standalone against only ``research.lab.types`` + synthetic fixtures +
this module's own logic. The ``nba_odds_study`` cache layer is mocked (the data
cache is absent in the worktree); a real-cache test SKIPS when the cache is
empty. We assert the split-safety invariant especially hard: the TEST split is
never loaded unless ``split == "test"`` is passed explicitly.
"""
from __future__ import annotations

import os
import unittest
from dataclasses import dataclass
from unittest import mock

import numpy as np
import pandas as pd

from research.lab import data
from research.lab.types import (
    SPREAD,
    TOTAL,
    WINNER,
    Panel,
    synthetic_panel,
    synthetic_panels,
)


# --------------------------------------------------------------------------- #
# synthetic fixtures: shape / contract assertions (no real data)
# --------------------------------------------------------------------------- #
class TestSyntheticContract(unittest.TestCase):
    """A loaded Panel must satisfy the shared Panel contract. We use the
    fixture panels as the canonical shape reference our loader must match."""

    def test_synthetic_panel_shapes_align(self) -> None:
        for market in (WINNER, TOTAL, SPREAD):
            p = synthetic_panel(market=market, n=48)
            self.assertIsInstance(p, Panel)
            self.assertEqual(p.market, market)
            n = p.n
            for arr in (p.minute_ts, p.elapsed_sec, p.margin, p.total, p.mid):
                self.assertEqual(len(arr), n)
            # ladder arrays (when present) are index-aligned by minute.
            for strike, probs in p.ladder.items():
                self.assertIsInstance(strike, float)
                self.assertEqual(len(probs), n)

    def test_synthetic_winner_has_no_ladder(self) -> None:
        self.assertEqual(synthetic_panel(market=WINNER).ladder, {})

    def test_synthetic_total_and_spread_have_ladder(self) -> None:
        self.assertTrue(synthetic_panel(market=TOTAL).ladder)
        self.assertTrue(synthetic_panel(market=SPREAD).ladder)

    def test_synthetic_panels_distinct_ids(self) -> None:
        ps = synthetic_panels(n_games=5, market=TOTAL)
        self.assertEqual(len({p.game_id for p in ps}), 5)


# --------------------------------------------------------------------------- #
# split filtering — the safety-critical invariant
# --------------------------------------------------------------------------- #
_FAKE_SPLITS = {
    "train_game_ids": ["2025-10-21_AAA_at_BBB"],
    "val_game_ids": ["2025-11-01_CCC_at_DDD"],
    "test_game_ids": ["2025-12-01_EEE_at_FFF"],
}


class TestSplitFilter(unittest.TestCase):
    def setUp(self) -> None:
        self._patch = mock.patch.object(data, "_load_splits", return_value=_FAKE_SPLITS)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.train = "2025-10-21_AAA_at_BBB"
        self.val = "2025-11-01_CCC_at_DDD"
        self.test = "2025-12-01_EEE_at_FFF"
        self.unknown = "2099-01-01_XXX_at_YYY"

    def test_train_only(self) -> None:
        pred, _ = data._split_filter("train")
        self.assertTrue(pred(self.train))
        self.assertFalse(pred(self.val))
        self.assertFalse(pred(self.test))

    def test_val_only(self) -> None:
        pred, _ = data._split_filter("val")
        self.assertTrue(pred(self.val))
        self.assertFalse(pred(self.train))
        self.assertFalse(pred(self.test))

    def test_nontest_excludes_test(self) -> None:
        pred, _ = data._split_filter("nontest")
        self.assertTrue(pred(self.train))
        self.assertTrue(pred(self.val))
        self.assertTrue(pred(self.unknown))
        self.assertFalse(pred(self.test))

    def test_none_excludes_test_implicitly(self) -> None:
        """The crux: None must NOT leak the test split."""
        pred, _ = data._split_filter(None)
        self.assertTrue(pred(self.train))
        self.assertTrue(pred(self.val))
        self.assertFalse(pred(self.test))

    def test_test_loaded_only_when_explicit(self) -> None:
        pred, _ = data._split_filter("test")
        self.assertTrue(pred(self.test))
        self.assertFalse(pred(self.train))

    def test_case_insensitive(self) -> None:
        self.assertTrue(data._split_filter("TRAIN")[0](self.train))
        self.assertTrue(data._split_filter("NonTest")[0](self.unknown))

    def test_unknown_split_raises(self) -> None:
        with self.assertRaises(ValueError):
            data._split_filter("holdout")

    def test_split_of_labels_correctly(self) -> None:
        _, split_of = data._split_filter(None)
        self.assertEqual(split_of(self.train), "train")
        self.assertEqual(split_of(self.val), "val")
        self.assertEqual(split_of(self.test), "test")
        self.assertEqual(split_of(self.unknown), "unknown")


# --------------------------------------------------------------------------- #
# cache enumeration (no network; filename parsing only)
# --------------------------------------------------------------------------- #
class TestCacheEnumeration(unittest.TestCase):
    def _with_cache(self, files):
        cdir = mock.patch.object(data, "_cache_dir", return_value="/fake/cache")
        isdir = mock.patch("os.path.isdir", return_value=True)
        listdir = mock.patch("os.listdir", return_value=files)
        return cdir, isdir, listdir

    def test_parses_only_matching_market(self) -> None:
        files = [
            "2025-10-21_AAA_at_BBB_total_all.pkl",
            "2025-10-22_CCC_at_DDD_total_all.pkl",
            "2025-10-21_AAA_at_BBB_spread_all.pkl",
            "2025-10-21_AAA_at_BBB_winner_all.pkl",
            "not_a_match.pkl",
            "2025-10-21_AAA_at_BBB_total_kalshi.pkl",  # non-default ptag: ignored
        ]
        c, i, ll = self._with_cache(files)
        with c, i, ll:
            tot = data._cached_games(TOTAL)
            spr = data._cached_games(SPREAD)
            win = data._cached_games(WINNER)
        self.assertEqual(len(tot), 2)
        self.assertEqual(len(spr), 1)
        self.assertEqual(len(win), 1)
        self.assertEqual(tot[0], {"date": "2025-10-21", "away": "AAA", "home": "BBB"})

    def test_sorted_deterministically(self) -> None:
        files = [
            "2025-10-22_ZZZ_at_AAA_total_all.pkl",
            "2025-10-21_AAA_at_BBB_total_all.pkl",
        ]
        c, i, ll = self._with_cache(files)
        with c, i, ll:
            tot = data._cached_games(TOTAL)
        self.assertEqual([g["date"] for g in tot], ["2025-10-21", "2025-10-22"])

    def test_missing_cache_dir_returns_empty(self) -> None:
        with mock.patch.object(data, "_cache_dir", return_value="/nope"), \
             mock.patch("os.path.isdir", return_value=False):
            self.assertEqual(data._cached_games(TOTAL), [])

    def test_available_counts_cached_games(self) -> None:
        files = ["2025-10-21_AAA_at_BBB_total_all.pkl",
                 "2025-10-22_CCC_at_DDD_total_all.pkl"]
        c, i, ll = self._with_cache(files)
        with c, i, ll:
            self.assertEqual(data.available(TOTAL), 2)

    def test_unknown_market_raises(self) -> None:
        with self.assertRaises(ValueError):
            data._cached_games("moneyline")


# --------------------------------------------------------------------------- #
# ladder reconstruction (pure helpers over an odds_long frame)
# --------------------------------------------------------------------------- #
class TestLadderHelpers(unittest.TestCase):
    def test_total_ladder_blends_and_aligns(self) -> None:
        idx = [0, 60, 120]
        odds = pd.DataFrame([
            {"platform": "kalshi", "kind": "total", "strike": 220.0, "prob": 0.6, "ts": 0, "key": "k"},
            {"platform": "polymarket", "kind": "total", "strike": 220.0, "prob": 0.4, "ts": 0, "key": "p"},
            {"platform": "kalshi", "kind": "total", "strike": 230.0, "prob": 0.3, "ts": 60, "key": "k"},
            {"platform": "kalshi", "kind": "winner", "strike": np.nan, "prob": 0.5, "ts": 0, "key": "k"},
        ])
        lad = data._total_ladder(odds, idx)
        self.assertIn(220.0, lad)
        self.assertIn(230.0, lad)
        self.assertEqual(len(lad[220.0]), len(idx))
        # platform blend at t=0 for strike 220: mean(0.6, 0.4) = 0.5
        self.assertAlmostEqual(lad[220.0][0], 0.5)

    def test_total_ladder_empty_when_no_total(self) -> None:
        odds = pd.DataFrame([
            {"platform": "kalshi", "kind": "winner", "strike": np.nan, "prob": 0.5, "ts": 0, "key": "k"},
        ])
        self.assertEqual(data._total_ladder(odds, [0]), {})

    def test_spread_ladder_signs_and_crosses(self) -> None:
        idx = [0, 60]
        # home favorite: kalshi "HOM wins by over 5" P=0.6 -> (+5, 0.6);
        # away "AWY wins by over 3" P=0.2 -> (-3, 0.8). Crossing 0.5 lands
        # between -3 and +5, on the home side of zero given these probs.
        odds = pd.DataFrame([
            {"platform": "kalshi", "kind": "spread", "strike": 5.0, "prob": 0.6,
             "ts": 0, "key": "KXNBASPREAD-HOM5"},
            {"platform": "kalshi", "kind": "spread", "strike": 3.0, "prob": 0.2,
             "ts": 0, "key": "KXNBASPREAD-AWY3"},
        ])
        lad, imp = data._spread_ladder(odds, "HOM", "AWY", idx)
        self.assertIn(5.0, lad)       # +5 home strike
        self.assertIn(-3.0, lad)      # away strike mapped to home frame
        self.assertEqual(len(lad[5.0]), len(idx))
        self.assertTrue(np.isfinite(imp.iloc[0]))

    def test_spread_ladder_empty_when_no_spread(self) -> None:
        odds = pd.DataFrame([
            {"platform": "kalshi", "kind": "total", "strike": 220.0, "prob": 0.5, "ts": 0, "key": "k"},
        ])
        lad, imp = data._spread_ladder(odds, "HOM", "AWY", [0])
        self.assertEqual(lad, {})
        self.assertTrue(imp.isna().all())


# --------------------------------------------------------------------------- #
# load_panel / load_panels against a MOCKED study (no real cache)
# --------------------------------------------------------------------------- #
@dataclass
class _FakeGame:
    home_tri: str
    away_tri: str


@dataclass
class _FakeDataset:
    game: _FakeGame
    minute: pd.DataFrame
    odds_long: pd.DataFrame


def _fake_minute(n=20):
    ts = np.arange(n, dtype=int) * 60
    elapsed = np.linspace(0, 2820, n)
    home = np.cumsum(np.abs(np.linspace(1, 3, n))).round()
    away = np.cumsum(np.abs(np.linspace(1, 2, n))).round()
    df = pd.DataFrame({
        "elapsed_game_sec": elapsed,
        "home_score": home,
        "away_score": away,
        "margin": home - away,
        "total": home + away,
        "kalshi_home_winprob": np.clip(np.linspace(0.4, 0.9, n), 0.02, 0.98),
        "pm_home_winprob": np.clip(np.linspace(0.45, 0.85, n), 0.02, 0.98),
    }, index=ts)
    return df


def _fake_total_odds(n=20):
    rows = []
    for t in np.arange(n, dtype=int) * 60:
        for strike, prob in ((210.0, 0.65), (220.0, 0.5), (230.0, 0.35)):
            rows.append({"platform": "kalshi", "kind": "total", "strike": strike,
                         "prob": prob, "ts": int(t), "key": "k"})
    return pd.DataFrame(rows)


class TestLoadPanelMocked(unittest.TestCase):
    """Drive load_panel with a fully mocked nba_odds_study study layer."""

    def _mock_study(self, dataset):
        batch = mock.MagicMock()
        batch.CACHE_DIR = "/fake/cache"
        batch.load_or_build.return_value = dataset

        # reuse the REAL _implied_total so the total path is exercised honestly
        from nba_odds_study import analysis as _real_analysis  # noqa: PLC0415
        analysis = mock.MagicMock()
        analysis._implied_total.side_effect = _real_analysis._implied_total
        return batch, analysis

    def test_total_panel_built(self) -> None:
        ds = _FakeDataset(_FakeGame("BBB", "AAA"), _fake_minute(), _fake_total_odds())
        batch, analysis = self._mock_study(ds)
        with mock.patch.object(data, "_study", return_value=(batch, analysis)), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(data, "_load_splits", return_value=_FAKE_SPLITS):
            p = data.load_panel({"date": "2025-10-21", "away": "AAA", "home": "BBB"}, TOTAL)
        self.assertIsInstance(p, Panel)
        self.assertEqual(p.market, TOTAL)
        self.assertEqual(p.game_id, "2025-10-21_AAA_at_BBB")
        self.assertEqual(p.n, 20)
        self.assertEqual(len(p.mid), p.n)
        self.assertTrue(p.ladder)                  # real ladder, not assumed 0.50
        for probs in p.ladder.values():
            self.assertEqual(len(probs), p.n)
        self.assertIsNotNone(p.final_total)
        self.assertIn(p.home_won, (0.0, 1.0))

    def test_winner_panel_has_no_ladder(self) -> None:
        ds = _FakeDataset(_FakeGame("BBB", "AAA"), _fake_minute(),
                          pd.DataFrame([{"platform": "kalshi", "kind": "winner",
                                         "strike": np.nan, "prob": 0.5, "ts": 0, "key": "k"}]))
        batch, analysis = self._mock_study(ds)
        with mock.patch.object(data, "_study", return_value=(batch, analysis)), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(data, "_load_splits", return_value=_FAKE_SPLITS):
            p = data.load_panel({"date": "2025-10-21", "away": "AAA", "home": "BBB"}, WINNER)
        self.assertIsInstance(p, Panel)
        self.assertEqual(p.ladder, {})
        self.assertEqual(len(p.mid), p.n)
        self.assertTrue(np.isfinite(p.mid).all())

    def test_absent_cache_returns_none(self) -> None:
        batch, analysis = self._mock_study(None)
        with mock.patch.object(data, "_study", return_value=(batch, analysis)), \
             mock.patch("os.path.exists", return_value=False):
            p = data.load_panel({"date": "2025-10-21", "away": "AAA", "home": "BBB"}, TOTAL)
        self.assertIsNone(p)
        batch.load_or_build.assert_not_called()    # cache-only: no live build

    def test_load_panel_unknown_market_raises(self) -> None:
        with self.assertRaises(ValueError):
            data.load_panel({"date": "2025-10-21", "away": "AAA", "home": "BBB"}, "moneyline")


class TestLoadPanelsMocked(unittest.TestCase):
    def test_returns_list_of_panels_and_respects_split(self) -> None:
        # two cached games: one train, one test. With split=None, test excluded.
        cached = [
            {"date": "2025-10-21", "away": "AAA", "home": "BBB"},   # train
            {"date": "2025-12-01", "away": "EEE", "home": "FFF"},   # test
        ]

        def fake_load_panel(game, market):
            gid = f"{game['date']}_{game['away']}_at_{game['home']}"
            p = synthetic_panel(game_id=gid, market=market)
            p.date = game["date"]
            return p

        with mock.patch.object(data, "_cached_games", return_value=cached), \
             mock.patch.object(data, "load_panel", side_effect=fake_load_panel), \
             mock.patch.object(data, "_load_splits", return_value=_FAKE_SPLITS):
            none_panels = data.load_panels(TOTAL, split=None)
            test_panels = data.load_panels(TOTAL, split="test")
            train_panels = data.load_panels(TOTAL, split="train")

        self.assertIsInstance(none_panels, list)
        self.assertTrue(all(isinstance(p, Panel) for p in none_panels))
        none_ids = {p.game_id for p in none_panels}
        self.assertIn("2025-10-21_AAA_at_BBB", none_ids)
        self.assertNotIn("2025-12-01_EEE_at_FFF", none_ids)   # test never leaks
        self.assertEqual({p.game_id for p in test_panels}, {"2025-12-01_EEE_at_FFF"})
        self.assertEqual({p.game_id for p in train_panels}, {"2025-10-21_AAA_at_BBB"})

    def test_limit_and_date_window(self) -> None:
        cached = [
            {"date": "2025-10-21", "away": "AAA", "home": "BBB"},
            {"date": "2025-11-01", "away": "CCC", "home": "DDD"},
            {"date": "2026-07-01", "away": "GGG", "home": "HHH"},  # out of window
        ]

        def fake_load_panel(game, market):
            gid = f"{game['date']}_{game['away']}_at_{game['home']}"
            return synthetic_panel(game_id=gid, market=market)

        with mock.patch.object(data, "_cached_games", return_value=cached), \
             mock.patch.object(data, "load_panel", side_effect=fake_load_panel), \
             mock.patch.object(data, "_load_splits", return_value=_FAKE_SPLITS):
            windowed = data.load_panels(TOTAL, split="nontest")
            limited = data.load_panels(TOTAL, split="nontest", limit=1)

        # 2026-07-01 is past the default end window and excluded.
        self.assertEqual(len(windowed), 2)
        self.assertEqual(len(limited), 1)

    def test_skips_games_that_fail_to_load(self) -> None:
        cached = [{"date": "2025-10-21", "away": "AAA", "home": "BBB"},
                  {"date": "2025-11-01", "away": "CCC", "home": "DDD"}]

        def fake_load_panel(game, market):
            if game["away"] == "AAA":
                return None  # cache present but unusable -> skipped
            return synthetic_panel(game_id="ok", market=market)

        with mock.patch.object(data, "_cached_games", return_value=cached), \
             mock.patch.object(data, "load_panel", side_effect=fake_load_panel), \
             mock.patch.object(data, "_load_splits", return_value=_FAKE_SPLITS):
            panels = data.load_panels(TOTAL, split="nontest")
        self.assertEqual(len(panels), 1)


# --------------------------------------------------------------------------- #
# real-cache smoke test — SKIPPED when the data cache is empty (worktree default)
# --------------------------------------------------------------------------- #
def _cache_is_empty() -> bool:
    try:
        cdir = data._cache_dir()
    except Exception:  # noqa: BLE001
        return True
    return (not os.path.isdir(cdir)) or len(os.listdir(cdir)) == 0


class TestRealCacheSmoke(unittest.TestCase):
    @unittest.skipIf(_cache_is_empty(), "data cache absent (expected in worktree)")
    def test_load_panels_real_cache(self) -> None:
        for market in (WINNER, TOTAL, SPREAD):
            n = data.available(market)
            self.assertGreaterEqual(n, 0)
            panels = data.load_panels(market, split="train", limit=3)
            self.assertIsInstance(panels, list)
            for p in panels:
                self.assertIsInstance(p, Panel)
                self.assertEqual(p.market, market)
                self.assertEqual(len(p.mid), p.n)
                self.assertEqual(p.split, "train")    # never the test split
                for probs in p.ladder.values():
                    self.assertEqual(len(probs), p.n)


if __name__ == "__main__":
    unittest.main()
