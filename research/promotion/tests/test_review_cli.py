"""Tests for research.promotion.review_cli.

Covers all 9 required test cases:
1. test_list_returns_promotable
2. test_list_empty
3. test_inspect_existing_spec_hash
4. test_inspect_missing_spec_hash
5. test_unlock_budget_initial
6. test_unlock_budget_after_burns
7. test_burn_unlock_refused_no_budget
8. test_burn_unlock_requires_confirmation
9. test_burn_unlock_with_yes_proceeds
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from research.promotion.review_cli import (
    _TEST_UNLOCKS_LOG,
    cmd_burn_unlock,
    cmd_inspect,
    cmd_list,
    cmd_unlock_budget,
    cmd_history,
    _count_burns,
    _remaining_unlocks,
)
from research.registry.db import connect as _db_connect


# ---------------------------------------------------------------------------
# Helpers for building minimal in-memory trial rows
# ---------------------------------------------------------------------------

_MINIMAL_SPEC_JSON = json.dumps(
    {
        "cost_assumption_bps": 25,
        "description": "Buy the hot team after a score run.",
        "entry_condition": {"description": "", "expr": "buy_hot > 0"},
        "exit_conditions": [{"description": "", "expr": "elapsed_game_sec > 1200"}],
        "features": ["buy_hot"],
        "name": "c_score_run_buy_hot_v1",
        "side": "long_hot",
        "sizing": {"max_position_contracts": 100, "mode": "fixed_contracts", "value": 5.0},
        "time_stop_sec": 900,
        "venue": "kalshi",
    },
    sort_keys=True,
    separators=(",", ":"),
)

_MINIMAL_SPEC_HASH = hashlib.sha256(_MINIMAL_SPEC_JSON.encode()).hexdigest()


def _insert_trial(
    conn: sqlite3.Connection,
    *,
    spec_hash: str = _MINIMAL_SPEC_HASH,
    spec_json: str = _MINIMAL_SPEC_JSON,
    spec_name: str = "c_score_run_buy_hot_v1",
    agent_id: str = "test_agent",
    gate_passed: int | None = 1,
    claude_review: str | None = None,
    test_pnl_net: float | None = None,
    val_sharpe_net: float | None = 0.245,
    val_n_trades: int | None = 587,
    mechanistic_writeup: str | None = "Hot team buys into score runs.",
    ts_offset: int = 0,
) -> int:
    """Insert one row into the trials table and return its rowid."""
    ts = int(time.time()) + ts_offset
    cur = conn.execute(
        "INSERT INTO trials "
        "  (spec_hash, spec_json, spec_name, features_used, agent_id, "
        "   val_sharpe_net, val_n_trades, test_pnl_net, "
        "   scorer_promotion_gate_passed, claude_review, mechanistic_writeup, "
        "   ts_created) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?);",
        (
            spec_hash,
            spec_json,
            spec_name,
            '["buy_hot"]',
            agent_id,
            val_sharpe_net,
            val_n_trades,
            test_pnl_net,
            gate_passed,
            claude_review,
            mechanistic_writeup,
            ts,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _tmp_db() -> tuple[Path, sqlite3.Connection]:
    """Create a fresh temp-file registry DB and return (path, connection)."""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    path = Path(tf.name)
    conn = _db_connect(path)
    return path, conn


def _tmp_log() -> Path:
    """Create a temp file to use as test_unlocks.log."""
    tf = tempfile.NamedTemporaryFile(
        suffix=".log", delete=False, mode="w", encoding="utf-8"
    )
    tf.write("# format: <iso_ts> <spec_hash> <attempt|burn> <reason>\n")
    tf.close()
    return Path(tf.name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListReturnsPromotable(unittest.TestCase):
    """test_list_returns_promotable: only the one passing candidate appears."""

    def setUp(self) -> None:
        self.db_path, conn = _tmp_db()
        # Trial 1: gate=1, claude_review set, test_pnl_net=NULL → promotable
        _insert_trial(
            conn,
            spec_name="good_trial",
            gate_passed=1,
            claude_review="Recommendation: PROMOTE — edge looks real.",
            test_pnl_net=None,
            ts_offset=0,
        )
        # Trial 2: gate=0 → not promotable
        _insert_trial(
            conn,
            spec_hash="aaa" + "0" * 61,
            spec_name="failed_gate",
            gate_passed=0,
            claude_review="review",
            test_pnl_net=None,
            ts_offset=1,
        )
        # Trial 3: gate=1, claude_review set, test_pnl_net set → already promoted
        _insert_trial(
            conn,
            spec_hash="bbb" + "0" * 61,
            spec_name="already_promoted",
            gate_passed=1,
            claude_review="review",
            test_pnl_net=0.123,
            ts_offset=2,
        )
        conn.close()

    def test_list_shows_only_promotable(self) -> None:
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cmd_list(db_path=self.db_path)
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("good_trial", output)
        self.assertNotIn("failed_gate", output)
        self.assertNotIn("already_promoted", output)

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)


class TestListEmpty(unittest.TestCase):
    """test_list_empty: empty registry → 'no candidates' message, exit 0."""

    def setUp(self) -> None:
        self.db_path, conn = _tmp_db()
        conn.close()

    def test_empty_prints_no_candidates(self) -> None:
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cmd_list(db_path=self.db_path)
        self.assertEqual(rc, 0)
        self.assertIn("no candidates", buf.getvalue())

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)


class TestInspectExistingSpecHash(unittest.TestCase):
    """test_inspect_existing_spec_hash: prints spec JSON, writeup, claude_review."""

    def setUp(self) -> None:
        self.db_path, conn = _tmp_db()
        _insert_trial(
            conn,
            spec_hash=_MINIMAL_SPEC_HASH,
            spec_json=_MINIMAL_SPEC_JSON,
            spec_name="c_score_run_buy_hot_v1",
            claude_review="Recommendation: PROMOTE — solid block-bootstrap CI.",
            mechanistic_writeup="Hot team buys into score runs.",
        )
        conn.close()

    def test_inspect_prints_expected_sections(self) -> None:
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cmd_inspect(_MINIMAL_SPEC_HASH, db_path=self.db_path)
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        # Must include spec JSON section
        self.assertIn("SPEC JSON", output)
        # Must include the spec name in the JSON
        self.assertIn("c_score_run_buy_hot_v1", output)
        # Must include writeup
        self.assertIn("Hot team buys into score runs", output)
        # Must include the claude_review text
        self.assertIn("PROMOTE", output)

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)


class TestInspectMissingSpecHash(unittest.TestCase):
    """test_inspect_missing_spec_hash: --inspect <bogus_hash> exits 1 with 'not found'."""

    def setUp(self) -> None:
        self.db_path, conn = _tmp_db()
        conn.close()

    def test_inspect_bogus_exits_1(self) -> None:
        err_buf = StringIO()
        with patch("sys.stderr", err_buf):
            rc = cmd_inspect("deadbeefdeadbeef0000", db_path=self.db_path)
        self.assertEqual(rc, 1)
        self.assertIn("not found", err_buf.getvalue())

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)


class TestUnlockBudgetInitial(unittest.TestCase):
    """test_unlock_budget_initial: empty log → 'Remaining unlocks: 5 / 5'."""

    def setUp(self) -> None:
        self.log_path = _tmp_log()

    def test_initial_budget(self) -> None:
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cmd_unlock_budget(log_path=self.log_path)
        self.assertEqual(rc, 0)
        self.assertIn("5 / 5", buf.getvalue())

    def tearDown(self) -> None:
        self.log_path.unlink(missing_ok=True)


class TestUnlockBudgetAfterBurns(unittest.TestCase):
    """test_unlock_budget_after_burns: 2 burns → 'Remaining unlocks: 3 / 5'."""

    def setUp(self) -> None:
        self.log_path = _tmp_log()
        with self.log_path.open("a") as fh:
            fh.write("2026-05-28T10:00:00Z abc123 burn promotion_cli\n")
            fh.write("2026-05-28T11:00:00Z def456 burn promotion_cli\n")

    def test_budget_after_two_burns(self) -> None:
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cmd_unlock_budget(log_path=self.log_path)
        self.assertEqual(rc, 0)
        self.assertIn("3 / 5", buf.getvalue())

    def tearDown(self) -> None:
        self.log_path.unlink(missing_ok=True)


class TestBurnUnlockRefusedNoBudget(unittest.TestCase):
    """test_burn_unlock_refused_no_budget: 5 burns in log → --burn-unlock exits 1."""

    def setUp(self) -> None:
        self.log_path = _tmp_log()
        self.db_path, conn = _tmp_db()
        _insert_trial(
            conn,
            spec_hash=_MINIMAL_SPEC_HASH,
            claude_review="Recommendation: PROMOTE.",
        )
        conn.close()
        # Saturate the budget
        with self.log_path.open("a") as fh:
            for i in range(5):
                fh.write(f"2026-05-28T{10+i:02d}:00:00Z hash{i} burn promotion_cli\n")

    def test_refuses_when_no_budget(self) -> None:
        err_buf = StringIO()
        with patch("sys.stderr", err_buf):
            rc = cmd_burn_unlock(
                _MINIMAL_SPEC_HASH,
                yes=True,
                log_path=self.log_path,
                db_path=self.db_path,
            )
        self.assertEqual(rc, 1)
        self.assertIn("no unlock budget", err_buf.getvalue())

    def tearDown(self) -> None:
        self.log_path.unlink(missing_ok=True)
        self.db_path.unlink(missing_ok=True)


class TestBurnUnlockRequiresConfirmation(unittest.TestCase):
    """test_burn_unlock_requires_confirmation: not typing 'BURN' → exits 1 (declined)."""

    def setUp(self) -> None:
        self.log_path = _tmp_log()
        self.db_path, conn = _tmp_db()
        _insert_trial(
            conn,
            spec_hash=_MINIMAL_SPEC_HASH,
            claude_review="Recommendation: PROMOTE.",
        )
        conn.close()

    def test_wrong_confirmation_exits_1(self) -> None:
        with patch("builtins.input", return_value="no"):
            rc = cmd_burn_unlock(
                _MINIMAL_SPEC_HASH,
                yes=False,
                log_path=self.log_path,
                db_path=self.db_path,
            )
        self.assertEqual(rc, 1)
        # No burn should have been appended
        self.assertEqual(_count_burns(self.log_path), 0)

    def test_empty_confirmation_exits_1(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            rc = cmd_burn_unlock(
                _MINIMAL_SPEC_HASH,
                yes=False,
                log_path=self.log_path,
                db_path=self.db_path,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(_count_burns(self.log_path), 0)

    def tearDown(self) -> None:
        self.log_path.unlink(missing_ok=True)
        self.db_path.unlink(missing_ok=True)


class TestBurnUnlockWithYesProceeds(unittest.TestCase):
    """test_burn_unlock_with_yes_proceeds:
    --yes bypasses confirmation, mocks run_backtest subprocess, verifies:
    - unlock token == sha256(spec_json).hexdigest()
    - the subprocess is pointed at the SAME unlock log (--unlock-log) and the
      overridden registry DB (--registry-db)
    - the burn defaults to the realistic official_2026 cost profile
    - EXACTLY ONE burn row total (written by the run_backtest subprocess) plus
      a non-burn promotion_cli 'attempt' row. The pre-fix behavior appended a
      second 'burn' row here, so one sanctioned run cost 2-3 budget units.
    """

    def setUp(self) -> None:
        self.log_path = _tmp_log()
        self.db_path, conn = _tmp_db()
        _insert_trial(
            conn,
            spec_hash=_MINIMAL_SPEC_HASH,
            spec_json=_MINIMAL_SPEC_JSON,
            claude_review="Recommendation: PROMOTE — edge looks real.",
        )
        conn.close()

        # Expected unlock token
        self.expected_token = hashlib.sha256(
            _MINIMAL_SPEC_JSON.encode("utf-8")
        ).hexdigest()

    def _fake_subprocess(self, captured_cmd: list[list[str]]):
        """Return a subprocess.run stand-in that mimics the real run_backtest
        boundary: it appends THE single burn row to the log given via
        --unlock-log, then reports metrics on stdout."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(
            {"test_pnl_net": 0.0312, "test_sharpe_net": 0.412, "test_n_trades": 201}
        )
        mock_result.stderr = ""

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            captured_cmd.append(cmd)
            log = Path(cmd[cmd.index("--unlock-log") + 1])
            token_spec_hash = _MINIMAL_SPEC_HASH
            with log.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"2026-06-12T00:00:00Z {token_spec_hash} burn run_backtest_cli\n"
                )
            return mock_result

        return fake_run

    def test_proceeds_with_yes(self) -> None:
        captured_cmd: list[list[str]] = []

        with patch("subprocess.run", side_effect=self._fake_subprocess(captured_cmd)):
            rc = cmd_burn_unlock(
                _MINIMAL_SPEC_HASH,
                yes=True,
                log_path=self.log_path,
                db_path=self.db_path,
            )

        self.assertEqual(rc, 0)

        # Verify unlock token in the subprocess command.
        self.assertTrue(captured_cmd, "subprocess.run was never called")
        cmd_args = captured_cmd[0]
        self.assertIn("--unlock-test-token", cmd_args)
        token_idx = cmd_args.index("--unlock-test-token") + 1
        self.assertEqual(cmd_args[token_idx], self.expected_token)

        # The subprocess must write its burn into the SAME log we count.
        self.assertIn("--unlock-log", cmd_args)
        self.assertEqual(
            cmd_args[cmd_args.index("--unlock-log") + 1], str(self.log_path)
        )

        # The overridden registry DB must be plumbed through too.
        self.assertIn("--registry-db", cmd_args)
        self.assertEqual(
            cmd_args[cmd_args.index("--registry-db") + 1], str(self.db_path)
        )

        # The burn must default to the realistic official-venue-fees profile,
        # NOT the fictional 'pessimistic' default.
        self.assertIn("--cost-profile", cmd_args)
        self.assertEqual(
            cmd_args[cmd_args.index("--cost-profile") + 1], "official_2026"
        )

        # EXACTLY one burn row (the subprocess's) — not a second one from the
        # promotion CLI.
        self.assertEqual(_count_burns(self.log_path), 1)

        # And one non-burn attempt row attributed to promotion_cli.
        attempt_rows = [
            line.split()
            for line in self.log_path.read_text().splitlines()
            if line.strip()
            and not line.startswith("#")
            and line.split()[2] == "attempt"
        ]
        self.assertEqual(len(attempt_rows), 1)
        self.assertEqual(attempt_rows[0][1], _MINIMAL_SPEC_HASH)
        self.assertEqual(attempt_rows[0][3], "promotion_cli")

    def test_custom_cost_profile_passed_through(self) -> None:
        captured_cmd: list[list[str]] = []

        with patch("subprocess.run", side_effect=self._fake_subprocess(captured_cmd)):
            rc = cmd_burn_unlock(
                _MINIMAL_SPEC_HASH,
                yes=True,
                log_path=self.log_path,
                db_path=self.db_path,
                cost_profile="calibrated_pm",
            )

        self.assertEqual(rc, 0)
        cmd_args = captured_cmd[0]
        self.assertEqual(
            cmd_args[cmd_args.index("--cost-profile") + 1], "calibrated_pm"
        )

    def tearDown(self) -> None:
        self.log_path.unlink(missing_ok=True)
        self.db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
