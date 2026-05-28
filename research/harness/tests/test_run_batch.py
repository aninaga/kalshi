"""Tests for research.harness.run_batch.

Design constraints
------------------
- Imports from ``research.harness.replay`` are done lazily inside test methods
  so that import-time failures only surface when those tests actually run.
- Tests that exercise ``run_batch`` against the real lake only rely on games
  present in the lake at test time.  They derive the expected ``n_games`` from
  ``load_split()`` intersected with ``list_games()``, not from a hard-coded
  count.
- All test-set unlock tests monkey-patch ``run_batch._burn_test_unlock`` to
  write to a temp file so the real ``market_data/test_unlocks.log`` is not
  polluted.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import types
import unittest
import unittest.mock as mock
from pathlib import Path


# ---------------------------------------------------------------------------
# Helper: build a minimal valid StrategySpec
# ---------------------------------------------------------------------------

def _make_spec(**overrides):
    """Return a minimal valid StrategySpec using only live-safe features."""
    from research.harness.strategy_spec import Condition, SizingRule, StrategySpec

    defaults = dict(
        name="test_halftime_spec",
        description="buy trailing at halftime, time-stop 4 min",
        features=["margin", "pm_implied_wp"],
        entry_condition=Condition(
            expr="elapsed_game_sec >= 1440 and abs(margin) > 0 and pos_side is None",
            description="halftime entry",
        ),
        exit_conditions=[Condition(expr="True", description="time-stop only")],
        sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
        time_stop_sec=240,
        side="long_trailing",
        venue="polymarket",
    )
    defaults.update(overrides)
    return StrategySpec(**defaults)


def _token_for(spec) -> str:
    return hashlib.sha256(spec.to_json().encode()).hexdigest()


# ---------------------------------------------------------------------------
# 1. load_split returns non-empty lists
# ---------------------------------------------------------------------------

class TestLoadSplit(unittest.TestCase):

    def test_load_split_returns_lists(self):
        from research.harness.run_batch import load_split
        import json
        splits_path = Path("market_data/splits.json")
        with splits_path.open() as fh:
            data = json.load(fh)

        for name in ("train", "val", "test"):
            result = load_split(name)
            self.assertIsInstance(result, list)
            self.assertGreater(len(result), 0, f"split {name!r} returned empty list")
            expected = data[f"{name}_game_ids"]
            self.assertEqual(result, list(expected))

    def test_load_split_rejects_bad_name(self):
        from research.harness.run_batch import load_split
        with self.assertRaises(ValueError) as ctx:
            load_split("foo")  # type: ignore[arg-type]
        self.assertIn("foo", str(ctx.exception))


# ---------------------------------------------------------------------------
# 2. run_batch on train/val with real lake (graceful skips)
# ---------------------------------------------------------------------------

class TestRunBatchOnLake(unittest.TestCase):
    """These tests call replay_game for real.  They only pass if replay.py
    exists and the lake has at least some games populated."""

    @classmethod
    def _lake_game_ids_for_split(cls, split: str) -> list[str]:
        """Game IDs that are both in splits.json AND present in the lake."""
        from research.harness.run_batch import load_split
        try:
            from research.lake.reader import list_games
            lake_ids = set(list_games(split=split))
        except Exception:
            lake_ids = set()
        split_ids = load_split(split)
        return [g for g in split_ids if g in lake_ids]

    def _check_split(self, split: str) -> None:
        """Common assertion logic for train / val."""
        # Import lazily — if replay.py doesn't exist, skip the test.
        try:
            from research.harness.replay import replay_game  # noqa: F401
        except ImportError:
            self.skipTest("research.harness.replay not yet available")

        from research.harness.run_batch import load_split, run_batch

        spec = _make_spec()
        result = run_batch(spec, split)  # type: ignore[arg-type]

        # n_games must equal the full split length, not just the lake subset.
        expected_n = len(load_split(split))
        self.assertEqual(result.n_games, expected_n)

        # Basic sanity on types / ranges
        self.assertIsInstance(result.n_games_with_trades, int)
        self.assertIsInstance(result.n_trades_total, int)
        self.assertGreaterEqual(result.n_games_with_trades, 0)
        self.assertGreaterEqual(result.n_trades_total, 0)
        self.assertGreaterEqual(result.n_games_skipped, 0)
        self.assertIsInstance(result.skipped_reasons, dict)
        self.assertIsInstance(result.trials, list)
        self.assertEqual(result.rng_seed, 0)

        # win_rate is NaN when 0 trades, otherwise in [0, 1]
        if result.n_trades_total > 0:
            self.assertFalse(math.isnan(result.win_rate))
            self.assertGreaterEqual(result.win_rate, 0.0)
            self.assertLessEqual(result.win_rate, 1.0)

    def test_run_batch_train_works_on_lake_subset(self):
        self._check_split("train")

    def test_run_batch_val_works(self):
        self._check_split("val")


# ---------------------------------------------------------------------------
# 3. Test-set access control
# ---------------------------------------------------------------------------

class TestTestSetGuard(unittest.TestCase):

    def test_run_batch_test_refused_without_token(self):
        """run_batch(spec, 'test') raises PermissionError with no token."""
        from research.harness.run_batch import run_batch
        spec = _make_spec()
        with self.assertRaises(PermissionError) as ctx:
            run_batch(spec, "test")
        msg = str(ctx.exception)
        self.assertIn("unlock_test_token", msg)

    def test_run_batch_test_refused_wrong_token(self):
        """run_batch(spec, 'test', unlock_test_token='bogus') raises PermissionError."""
        from research.harness.run_batch import run_batch
        spec = _make_spec()
        with self.assertRaises(PermissionError) as ctx:
            run_batch(spec, "test", unlock_test_token="bogus")
        msg = str(ctx.exception)
        self.assertIn("mismatch", msg.lower())

    def test_run_batch_test_accepted_with_correct_token(self):
        """Correct token is accepted; burn is appended to (temp) log file."""
        # Import lazily
        try:
            from research.harness.replay import replay_game  # noqa: F401
        except ImportError:
            self.skipTest("research.harness.replay not yet available")

        import research.harness.run_batch as rb_module

        spec = _make_spec()
        token = _token_for(spec)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Monkey-patch _burn_test_unlock to write to tmp_path
            original_burn = rb_module._burn_test_unlock

            def fake_burn(s):
                from datetime import datetime, timezone
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                with tmp_path.open("a") as fh:
                    ts = datetime.now(tz=timezone.utc).isoformat()
                    fh.write(f"{ts} {s.spec_hash()} burn run_batch(split=test)\n")

            rb_module._burn_test_unlock = fake_burn
            try:
                result = rb_module.run_batch(
                    spec, "test", unlock_test_token=token
                )
            finally:
                rb_module._burn_test_unlock = original_burn

            # Burn record must exist
            content = tmp_path.read_text()
            self.assertIn(spec.spec_hash(), content)
            self.assertIn("burn", content)

            # Result must be a BatchResult
            from research.harness.run_batch import BatchResult
            self.assertIsInstance(result, BatchResult)
            self.assertEqual(result.split, "test")

        finally:
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Aggregation correctness with hand-constructed fake TrialResults
# ---------------------------------------------------------------------------

class TestBatchResultAggregation(unittest.TestCase):
    """Build fake TrialResult-like objects and verify _aggregate output."""

    def _fake_trade(self, gross: float, net: float, holding_sec: float, notional: float):
        t = types.SimpleNamespace(
            pnl_gross=gross,
            pnl_net=net,
            holding_sec=holding_sec,
            notional=notional,
        )
        return t

    def _fake_trial(self, trades, skipped=None):
        return types.SimpleNamespace(
            trades=trades,
            skipped=skipped if skipped is not None else [],
        )

    def test_batch_result_aggregation(self):
        from research.harness.run_batch import _aggregate

        spec = _make_spec()

        # Three fake trials:
        # Trial A: 2 trades (wins), no skips
        t1 = self._fake_trade(gross=0.10, net=0.08, holding_sec=120.0, notional=5.0)
        t2 = self._fake_trade(gross=0.05, net=0.04, holding_sec=60.0, notional=5.0)
        trial_a = self._fake_trial([t1, t2])

        # Trial B: 1 trade (loss), no skips
        t3 = self._fake_trade(gross=-0.03, net=-0.04, holding_sec=90.0, notional=5.0)
        trial_b = self._fake_trial([t3])

        # Trial C: 0 trades, 1 skipped reason
        trial_c = self._fake_trial([], skipped=["no_fill"])

        trials = [trial_a, trial_b, trial_c]

        result = _aggregate(spec, "train", trials, rng_seed=42)

        # n_games
        self.assertEqual(result.n_games, 3)

        # n_games_with_trades: A and B
        self.assertEqual(result.n_games_with_trades, 2)

        # n_trades_total: 3
        self.assertEqual(result.n_trades_total, 3)

        # pnl_gross: 0.10 + 0.05 + (-0.03) = 0.12
        self.assertAlmostEqual(result.pnl_gross, 0.12, places=9)

        # pnl_net: 0.08 + 0.04 + (-0.04) = 0.08
        self.assertAlmostEqual(result.pnl_net, 0.08, places=9)

        # notional_traded: 5+5+5 = 15
        self.assertAlmostEqual(result.notional_traded, 15.0, places=9)

        # mean_pnl_per_trade_gross: 0.12 / 3 = 0.04
        self.assertAlmostEqual(result.mean_pnl_per_trade_gross, 0.12 / 3, places=9)

        # mean_pnl_per_trade_net: 0.08 / 3
        self.assertAlmostEqual(result.mean_pnl_per_trade_net, 0.08 / 3, places=9)

        # win_rate: 2 wins (t1=0.08>0, t2=0.04>0) out of 3 trades = 2/3
        self.assertAlmostEqual(result.win_rate, 2 / 3, places=9)

        # avg_holding_sec: (120 + 60 + 90) / 3 = 90
        self.assertAlmostEqual(result.avg_holding_sec, 90.0, places=9)

        # n_games_skipped: only trial_c (0 trades, 1 reason)
        self.assertEqual(result.n_games_skipped, 1)

        # skipped_reasons: {"no_fill": 1}
        self.assertEqual(result.skipped_reasons, {"no_fill": 1})

        # rng_seed preserved
        self.assertEqual(result.rng_seed, 42)

        # spec_hash matches
        self.assertEqual(result.spec_hash, spec.spec_hash())

        # sharpe: mean/std on [0.08, 0.04, -0.04]
        net_vals = [0.08, 0.04, -0.04]
        mu = sum(net_vals) / 3
        var = sum((x - mu) ** 2 for x in net_vals) / 2
        std = math.sqrt(var)
        expected_sharpe = mu / std
        self.assertAlmostEqual(result.sharpe_per_trade_net, expected_sharpe, places=6)

    def test_aggregation_no_trades(self):
        """Zero-trade batch yields NaN sharpe, NaN win_rate, zero pnl."""
        from research.harness.run_batch import _aggregate

        spec = _make_spec()
        # Two trials, each with no trades and no skips (no-signal games)
        trial_x = self._fake_trial([], skipped=[])
        trial_y = self._fake_trial([], skipped=[])
        result = _aggregate(spec, "train", [trial_x, trial_y], rng_seed=0)

        self.assertEqual(result.n_trades_total, 0)
        self.assertEqual(result.n_games_with_trades, 0)
        self.assertEqual(result.n_games_skipped, 0)
        self.assertTrue(math.isnan(result.sharpe_per_trade_net))
        self.assertTrue(math.isnan(result.win_rate))
        self.assertAlmostEqual(result.pnl_gross, 0.0)
        self.assertAlmostEqual(result.pnl_net, 0.0)


# ---------------------------------------------------------------------------
# 5. Per-game exception isolation
# ---------------------------------------------------------------------------

class TestPerGameExceptionIsolation(unittest.TestCase):

    def test_per_game_exception_isolated(self):
        """If replay_game raises on one game, the batch still completes."""
        import research.harness.run_batch as rb_module
        from research.harness.run_batch import load_split, BatchResult

        # We need at least 2 game IDs in train to test isolation
        train_ids = load_split("train")
        if len(train_ids) < 2:
            self.skipTest("Need at least 2 train games for this test")

        spec = _make_spec()

        # Identify the first game_id; we'll force it to raise.
        bad_game_id = train_ids[0]

        call_log = []

        def fake_replay_game(s, game_id, rng_seed=0, allow_test=False):
            call_log.append(game_id)
            if game_id == bad_game_id:
                raise RuntimeError("simulated replay failure")
            # Return a minimal TrialResult-like namespace
            return types.SimpleNamespace(trades=[], skipped=[])

        # Monkey-patch _import_replay so _run_one_game uses our fake
        original_import_replay = rb_module._import_replay

        def patched_import_replay():
            return fake_replay_game, None

        rb_module._import_replay = patched_import_replay
        try:
            result = rb_module.run_batch(spec, "train")
        finally:
            rb_module._import_replay = original_import_replay

        # The batch must complete (no exception propagated)
        self.assertIsInstance(result, BatchResult)

        # n_games must equal full split
        self.assertEqual(result.n_games, len(train_ids))

        # The failed game must be recorded as skipped
        self.assertGreaterEqual(result.n_games_skipped, 1)
        # Some exception reason key must mention the bad game or RuntimeError
        all_reasons = " ".join(result.skipped_reasons.keys())
        self.assertTrue(
            "RuntimeError" in all_reasons or "simulated" in all_reasons
        )


if __name__ == "__main__":
    unittest.main()
