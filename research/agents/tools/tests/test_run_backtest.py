"""Tests for run_backtest — the security boundary CLI.

Design constraints:
  - Every test that writes anything points the CLI at temp files via the
    `--registry-db`, `--audit-log`, and `--unlock-log` overrides; we never
    touch the real `market_data/trials.db` or `market_data/audit.log`.
  - The real lake is required for the "actually runs" tests (test #6 and
    the smoke path). Tests that only exercise the boundary use --dry-run or
    invalid specs so they don't need the lake.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from research.agents.tools.run_backtest import run as cli_run
from research.harness.strategy_spec import Condition, SizingRule, StrategySpec
from research.registry.api import count_total_trials, query


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_valid_spec(**overrides) -> StrategySpec:
    """Minimal valid spec using only live-safe features."""
    defaults = dict(
        name="cli_test_spec",
        description="cli test fixture",
        features=["margin", "pm_implied_wp"],
        entry_condition=Condition(
            expr="elapsed_game_sec >= 1440 and abs(margin) >= 1 and pos_side is None",
            description="halftime small-margin entry",
        ),
        exit_conditions=[Condition(expr="True", description="time-stop only")],
        sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
        time_stop_sec=240,
        side="long_trailing",
        venue="polymarket",
    )
    defaults.update(overrides)
    return StrategySpec(**defaults)


def _make_leakage_spec() -> StrategySpec:
    """Spec that references the leakage canary feature (is_live_safe=False).

    StrategySpec.validate() raises here, but the constructor itself doesn't —
    we need the constructor to succeed so the CLI is the rejector under test.
    """
    return StrategySpec(
        name="cli_leakage_canary",
        description="references espn_home_winprob",
        features=["margin", "espn_home_winprob"],
        entry_condition=Condition(
            expr="espn_home_winprob > 0.5 and pos_side is None",
            description="leakage canary entry",
        ),
        exit_conditions=[Condition(expr="True", description="never reached")],
        sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
        time_stop_sec=180,
        side="long_home",
        venue="polymarket",
    )


def _token_for(spec: StrategySpec) -> str:
    return hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()


class _TmpEnv:
    """Tempfile bundle: spec.json + registry.db + audit.log + unlock.log."""

    def __init__(self, spec: StrategySpec) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="run_backtest_test_")
        self.spec_path = Path(self.tmpdir, "spec.json")
        self.spec_path.write_text(spec.to_json())
        # Sqlite needs the file to NOT exist for the schema bootstrap path.
        self.db_path = Path(self.tmpdir, "trials.db")
        # Audit / unlock logs may or may not exist; we don't pre-create them.
        self.audit_path = Path(self.tmpdir, "audit.log")
        self.unlock_path = Path(self.tmpdir, "test_unlocks.log")

    def base_argv(self, spec: StrategySpec, split: str, agent_id: str) -> list[str]:
        return [
            "--spec-file", str(self.spec_path),
            "--split", split,
            "--agent-id", agent_id,
            "--registry-db", str(self.db_path),
            "--audit-log", str(self.audit_path),
            "--unlock-log", str(self.unlock_path),
        ]

    def cleanup(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                Path(str(self.db_path) + suffix).unlink()
            except FileNotFoundError:
                pass
        for p in (self.spec_path, self.audit_path, self.unlock_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _StdoutCapture:
    """Context manager that captures sys.stdout into an in-memory buffer."""

    def __init__(self) -> None:
        import io
        self._buf = io.StringIO()
        self._saved = None

    def __enter__(self) -> "_StdoutCapture":
        import sys
        self._saved = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import sys
        sys.stdout = self._saved

    @property
    def text(self) -> str:
        return self._buf.getvalue()


def _last_stdout_json(text: str) -> dict:
    """Parse the final non-empty line of stdout as JSON."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise AssertionError("no stdout produced")
    return json.loads(lines[-1])


def _audit_rows(path: Path) -> list[dict]:
    """Read all JSONL rows from the audit log. Missing file → []."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- #
# 1. dry-run on valid spec
# --------------------------------------------------------------------------- #


class TestDryRunValidSpec(unittest.TestCase):

    def test_dry_run_valid_spec(self) -> None:
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            argv = env.base_argv(spec, "train", "manual:dry") + ["--dry-run"]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 0)
            payload = _last_stdout_json(cap.text)
            self.assertEqual(payload["spec_hash"], spec.spec_hash())
            self.assertIsNone(payload["gate_passed"])
            # n_trades / pnl_net are None on dry-run
            self.assertIsNone(payload["n_trades"])
            # An audit row was appended.
            rows = _audit_rows(env.audit_path)
            self.assertGreaterEqual(len(rows), 1)
            self.assertEqual(rows[-1]["spec_hash"], spec.spec_hash())
            self.assertEqual(rows[-1]["exit_code"], 0)
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 2. leakage spec refused (defense-in-depth)
# --------------------------------------------------------------------------- #


class TestLeakageRefused(unittest.TestCase):

    def test_leakage_spec_refused(self) -> None:
        spec = _make_leakage_spec()
        env = _TmpEnv(spec)
        try:
            argv = env.base_argv(spec, "train", "manual:leakage") + ["--dry-run"]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 1)
            payload = _last_stdout_json(cap.text)
            self.assertIn("error", payload)
            self.assertIn("reason", payload)
            # The leakage feature name must appear in either the error or
            # reason — strategy validate() mentions it, and our defense-in-
            # depth check would also mention it.
            blob = (payload.get("reason", "") + " " + payload.get("error", ""))
            self.assertIn("espn_home_winprob", blob)
            rows = _audit_rows(env.audit_path)
            self.assertEqual(rows[-1]["refused"], True)
            self.assertEqual(rows[-1]["exit_code"], 1)
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 3. --split test without --unlock-test-token
# --------------------------------------------------------------------------- #


class TestTestSplitNoToken(unittest.TestCase):

    def test_test_split_no_token_refused(self) -> None:
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            # Make sure env vars don't accidentally satisfy the gate.
            env_backup = {
                k: os.environ.pop(k, None)
                for k in ("RESEARCH_UNLOCK_TEST", "RESEARCH_UNLOCK_SPEC_HASH")
            }
            try:
                argv = env.base_argv(spec, "test", "manual:probe")
                with _StdoutCapture() as cap:
                    exit_code = cli_run(argv)
                self.assertEqual(exit_code, 1)
                payload = _last_stdout_json(cap.text)
                self.assertIn("error", payload)
                self.assertIn("unlock", payload["error"].lower() + payload["reason"].lower())
                rows = _audit_rows(env.audit_path)
                self.assertTrue(rows[-1].get("refused"))
                self.assertEqual(rows[-1]["split"], "test")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 4. --split test with wrong unlock token
# --------------------------------------------------------------------------- #


class TestTestSplitWrongToken(unittest.TestCase):

    def test_test_split_wrong_token_refused(self) -> None:
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            argv = env.base_argv(spec, "test", "manual:wrong") + [
                "--unlock-test-token", "bogus" + "0" * 59,
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 1)
            payload = _last_stdout_json(cap.text)
            self.assertIn("error", payload)
            blob = (payload["reason"] + " " + payload["error"]).lower()
            self.assertIn("token", blob)
            # No burn should have been recorded.
            self.assertFalse(
                env.unlock_path.exists()
                or any(
                    "burn" in line for line in env.unlock_path.read_text().splitlines()
                )
                if env.unlock_path.exists()
                else False,
                "wrong-token attempt must not consume the unlock budget",
            )
            rows = _audit_rows(env.audit_path)
            self.assertTrue(rows[-1].get("refused"))
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 5. Test-split unlock budget exhausted (5 burns already)
# --------------------------------------------------------------------------- #


class TestUnlockBudgetExhausted(unittest.TestCase):

    def test_test_split_unlock_budget_check(self) -> None:
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            # Pre-populate the unlock log with 5 burns. Use different fake
            # spec hashes so the count includes everything per project lifetime.
            with env.unlock_path.open("w") as fh:
                fh.write("# format: <iso_ts> <spec_hash> <attempt|burn> <reason>\n")
                for i in range(5):
                    fh.write(
                        f"2026-05-28T12:00:0{i}Z fakehash{i} burn pre_existing\n"
                    )

            token = _token_for(spec)
            argv = env.base_argv(spec, "test", "manual:budget") + [
                "--unlock-test-token", token,
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 1)
            payload = _last_stdout_json(cap.text)
            self.assertIn("error", payload)
            blob = (payload["reason"] + " " + payload["error"]).lower()
            self.assertIn("budget", blob)
            # The pre-existing burns are untouched; no new burn appended.
            n_burns = sum(
                1 for line in env.unlock_path.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
                and line.strip().split()[2].lower() == "burn"
            )
            self.assertEqual(n_burns, 5, "must not add a burn on refusal")
            rows = _audit_rows(env.audit_path)
            self.assertTrue(rows[-1].get("refused"))
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 6. Train split records a real trial
# --------------------------------------------------------------------------- #


class TestTrainSplitRecordsTrial(unittest.TestCase):

    def test_train_split_records_trial(self) -> None:
        # The full run requires the replay engine + lake. Skip if either
        # isn't available — the test should still be informative in the
        # CI matrix that does have them.
        try:
            from research.harness.replay import replay_game  # noqa: F401
        except ImportError:
            self.skipTest("research.harness.replay not available")
        # Cheap lake sanity probe: at least one game from the train split
        # should be loadable. If the lake is empty we cannot exercise this
        # case but should not fail.
        try:
            from research.harness.run_batch import load_split  # noqa: PLC0415
            from research.lake.reader import list_games  # noqa: PLC0415
            lake_ids = set(list_games(split="train"))
        except Exception:
            self.skipTest("lake not available")
        split_ids = load_split("train")
        usable = [g for g in split_ids if g in lake_ids]
        if not usable:
            self.skipTest("no train-split games present in the lake")

        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            before = count_total_trials(db_path=str(env.db_path))
            argv = env.base_argv(spec, "train", "manual:train_records") + [
                "--rng-seed", "7",
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
            payload = _last_stdout_json(cap.text)
            self.assertEqual(payload["spec_hash"], spec.spec_hash())
            self.assertIsNotNone(payload["trial_id"])
            self.assertIn("gate_passed", payload)

            # Registry count incremented by exactly 1.
            after = count_total_trials(db_path=str(env.db_path))
            self.assertEqual(after, before + 1)
            rows = query(spec_hash=spec.spec_hash(), db_path=str(env.db_path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].agent_id, "manual:train_records")

            # Audit log success row.
            audit_rows = _audit_rows(env.audit_path)
            self.assertTrue(any(
                r.get("exit_code") == 0 and r.get("trial_id") == payload["trial_id"]
                for r in audit_rows
            ))
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 7. Audit log always appended even on failure
# --------------------------------------------------------------------------- #


class TestAuditLogAlwaysAppended(unittest.TestCase):

    def test_audit_log_always_appended_even_on_failure(self) -> None:
        """Pre-existing audit rows are preserved; refusal appends a new row."""
        spec = _make_leakage_spec()
        env = _TmpEnv(spec)
        try:
            # Pre-existing content must survive the refusal.
            env.audit_path.write_text(
                json.dumps({"ts": "pre", "marker": "DO_NOT_DELETE"}) + "\n"
            )
            argv = env.base_argv(spec, "train", "manual:leakage_audit") + ["--dry-run"]
            with _StdoutCapture():
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 1)
            rows = _audit_rows(env.audit_path)
            self.assertGreaterEqual(len(rows), 2)
            self.assertEqual(rows[0].get("marker"), "DO_NOT_DELETE")
            # The refusal row carries refused=True.
            self.assertTrue(rows[-1].get("refused"))
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 8. Dry-run does NOT record a trial
# --------------------------------------------------------------------------- #


class TestDryRunNoTrial(unittest.TestCase):

    def test_dry_run_does_not_record_trial(self) -> None:
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            before = count_total_trials(db_path=str(env.db_path))
            argv = env.base_argv(spec, "train", "manual:dry_no_record") + ["--dry-run"]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
            after = count_total_trials(db_path=str(env.db_path))
            self.assertEqual(after, before, "dry-run must not record a trial")
        finally:
            env.cleanup()


if __name__ == "__main__":
    unittest.main()
