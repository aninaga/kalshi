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
# 5b. Exactly ONE burn row per sanctioned run + spec_hash dedup
# --------------------------------------------------------------------------- #


def _unlock_rows(path: Path) -> list[list[str]]:
    """Parsed data rows of the unlock log: [iso_ts, spec_hash, kind, *reason]."""
    if not path.exists():
        return []
    return [
        line.split()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestSingleBurnRowAndDedup(unittest.TestCase):
    """One sanctioned test run must consume exactly ONE burn row, and a re-run
    of an already-burned spec_hash must record a non-burn 'attempt' row.

    Uses --dry-run: the test-split guard (token check, budget check, burn
    write) executes BEFORE the dry-run short-circuit, so these tests exercise
    the bookkeeping without needing the lake.
    """

    def test_sanctioned_run_writes_exactly_one_burn_row(self) -> None:
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            argv = env.base_argv(spec, "test", "manual:burn_once") + [
                "--unlock-test-token", _token_for(spec),
                "--dry-run",
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
            rows = _unlock_rows(env.unlock_path)
            burn_rows = [r for r in rows if r[2] == "burn"]
            self.assertEqual(
                len(burn_rows), 1,
                f"exactly one burn row expected, log rows: {rows}",
            )
            self.assertEqual(burn_rows[0][1], spec.spec_hash())
        finally:
            env.cleanup()

    def test_rerun_of_burned_spec_records_attempt_not_second_burn(self) -> None:
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            # Pre-populate: this spec's burn already happened UNDER THE SAME
            # run fingerprint (profile + seed) — only an exact configuration
            # repeat is a free rerun (2026-06-12 loophole closure).
            with env.unlock_path.open("w") as fh:
                fh.write("# format: <iso_ts> <spec_hash> <attempt|burn> <reason>\n")
                fh.write(
                    f"2026-06-11T00:00:00Z {spec.spec_hash()} burn "
                    f"run_backtest_cli profile=pessimistic seed=0\n"
                )
            argv = env.base_argv(spec, "test", "manual:rerun") + [
                "--unlock-test-token", _token_for(spec),
                "--dry-run",
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
            rows = _unlock_rows(env.unlock_path)
            burn_rows = [r for r in rows if r[2] == "burn"]
            attempt_rows = [r for r in rows if r[2] == "attempt"]
            self.assertEqual(len(burn_rows), 1, "rerun must not add a second burn")
            self.assertEqual(len(attempt_rows), 1, "rerun must record an attempt row")
            self.assertEqual(attempt_rows[0][1], spec.spec_hash())
        finally:
            env.cleanup()

    def test_rerun_of_burned_spec_allowed_when_budget_exhausted(self) -> None:
        """A spec whose burn already exists reveals nothing new on re-run, so
        the budget-exhausted refusal must not apply to it."""
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            with env.unlock_path.open("w") as fh:
                fh.write("# format: <iso_ts> <spec_hash> <attempt|burn> <reason>\n")
                for i in range(4):
                    fh.write(f"2026-06-11T00:00:0{i}Z otherhash{i} burn pre_existing\n")
                fh.write(
                    f"2026-06-11T00:00:04Z {spec.spec_hash()} burn "
                    f"run_backtest_cli profile=pessimistic seed=0\n"
                )
            argv = env.base_argv(spec, "test", "manual:rerun_full") + [
                "--unlock-test-token", _token_for(spec),
                "--dry-run",
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
            rows = _unlock_rows(env.unlock_path)
            self.assertEqual(sum(1 for r in rows if r[2] == "burn"), 5)
            self.assertEqual(sum(1 for r in rows if r[2] == "attempt"), 1)
        finally:
            env.cleanup()

    def test_rerun_under_different_profile_is_not_free(self) -> None:
        """LOOPHOLE REGRESSION (2026-06-12 adversarial review): a burned spec
        re-run under a DIFFERENT cost profile reveals NEW information (e.g.
        zero-cost gross test edge) — it must hit the budget path, and with the
        budget exhausted it must be REFUSED, not waved through as a free
        'already burned' rerun."""
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            with env.unlock_path.open("w") as fh:
                fh.write("# format: <iso_ts> <spec_hash> <attempt|burn> <reason>\n")
                for i in range(4):
                    fh.write(f"2026-06-11T00:00:0{i}Z otherhash{i} burn pre_existing\n")
                fh.write(
                    f"2026-06-11T00:00:04Z {spec.spec_hash()} burn "
                    f"run_backtest_cli profile=pessimistic seed=0\n"
                )
            argv = env.base_argv(spec, "test", "manual:rerun_zero_cost") + [
                "--unlock-test-token", _token_for(spec),
                "--cost-profile", "zero",
                "--dry-run",
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertNotEqual(
                exit_code, 0,
                "different-profile rerun with exhausted budget must be refused"
                f" (stdout: {cap.text})")
            self.assertIn("test_unlock_budget_exhausted", cap.text)
            rows = _unlock_rows(env.unlock_path)
            self.assertEqual(
                sum(1 for r in rows if r[2] == "burn"), 5,
                "refusal must not append a burn row")
            self.assertEqual(
                sum(1 for r in rows if r[2] == "attempt"), 0,
                "different-profile rerun must not get a free attempt row")
        finally:
            env.cleanup()

    def test_rerun_under_different_seed_consumes_budget(self) -> None:
        """Different --rng-seed on a burned spec = new information = new burn."""
        spec = _make_valid_spec()
        env = _TmpEnv(spec)
        try:
            with env.unlock_path.open("w") as fh:
                fh.write("# format: <iso_ts> <spec_hash> <attempt|burn> <reason>\n")
                fh.write(
                    f"2026-06-11T00:00:00Z {spec.spec_hash()} burn "
                    f"run_backtest_cli profile=pessimistic seed=0\n"
                )
            argv = env.base_argv(spec, "test", "manual:rerun_seed7") + [
                "--unlock-test-token", _token_for(spec),
                "--rng-seed", "7",
                "--dry-run",
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
            self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
            rows = _unlock_rows(env.unlock_path)
            burn_rows = [r for r in rows if r[2] == "burn"]
            self.assertEqual(
                len(burn_rows), 2,
                "a new-seed rerun must consume a NEW burn (budget permitting)")
            self.assertIn("seed=7", " ".join(burn_rows[-1]))
        finally:
            env.cleanup()


class TestEndToEndSingleBurnWithRunBatch(unittest.TestCase):
    """Full in-process sanctioned test run (replay faked, no lake needed):
    run_backtest writes its ONE burn row and run_batch writes its non-burn
    'attempt' row into the SAME injected unlock log — total burns == 1.
    Regression for the triple-burn defect (run_backtest + run_batch +
    review_cli each appending 'burn')."""

    def test_full_test_run_one_burn_one_attempt(self) -> None:
        import types

        import research.harness.run_batch as rb_module

        spec = _make_valid_spec()
        env = _TmpEnv(spec)

        def fake_replay_game(s, game_id, rng_seed=0, allow_test=False):
            return types.SimpleNamespace(trades=[], skipped=[])

        original_import_replay = rb_module._import_replay
        rb_module._import_replay = lambda: (fake_replay_game, None)
        try:
            argv = env.base_argv(spec, "test", "manual:e2e_single_burn") + [
                "--unlock-test-token", _token_for(spec),
            ]
            with _StdoutCapture() as cap:
                exit_code = cli_run(argv)
        finally:
            rb_module._import_replay = original_import_replay

        try:
            self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
            rows = _unlock_rows(env.unlock_path)
            burn_rows = [r for r in rows if r[2] == "burn"]
            attempt_rows = [r for r in rows if r[2] == "attempt"]
            self.assertEqual(
                len(burn_rows), 1,
                f"one sanctioned run must burn exactly once, rows: {rows}",
            )
            self.assertEqual(burn_rows[0][1], spec.spec_hash())
            self.assertEqual(
                len(attempt_rows), 1,
                f"run_batch must add its attempt row to the same log, rows: {rows}",
            )
            self.assertEqual(attempt_rows[0][1], spec.spec_hash())
            self.assertEqual(attempt_rows[0][3], "run_batch(split=test)")
        finally:
            env.cleanup()


# --------------------------------------------------------------------------- #
# 5c. Extended --cost-profile choices (calibrated_pm / official_2026)
# --------------------------------------------------------------------------- #


class TestCostProfileChoices(unittest.TestCase):
    """The CLI must be able to select the realistic profiles — the promotion
    burn path passes official_2026 by default."""

    def setUp(self) -> None:
        from research.harness.cost_profile import get_active_profile
        self._saved_profile = get_active_profile()

    def tearDown(self) -> None:
        from research.harness.cost_profile import set_active_profile
        set_active_profile(self._saved_profile)

    def test_realistic_profiles_accepted(self) -> None:
        from research.harness.cost_profile import get_active_profile

        for profile in ("official_2026", "calibrated_pm"):
            spec = _make_valid_spec()
            env = _TmpEnv(spec)
            try:
                argv = env.base_argv(spec, "train", "manual:profile") + [
                    "--dry-run", "--cost-profile", profile,
                ]
                with _StdoutCapture() as cap:
                    exit_code = cli_run(argv)
                self.assertEqual(exit_code, 0, f"stdout: {cap.text}")
                self.assertEqual(get_active_profile().name, profile)
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
