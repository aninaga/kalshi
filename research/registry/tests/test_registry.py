"""Unit tests for the append-only trial registry.

Each test gets its own tempfile-backed sqlite DB so the suite is order-
independent and leaves no artefacts behind. We never write to the real
``market_data/trials.db`` from tests.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from research.harness.strategy_spec import Condition, SizingRule, StrategySpec
from research.registry.api import (
    count_total_trials,
    query,
    query_promotable_candidates,
    record_claude_review,
    record_trial,
)
from research.registry.db import EXPECTED_COLUMNS, connect


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_spec(name: str = "test_spec") -> StrategySpec:
    """Build a minimal StrategySpec that passes validation."""
    return StrategySpec(
        name=name,
        description=f"test fixture {name}",
        features=["margin", "pm_implied_wp"],
        entry_condition=Condition(expr="margin < 0", description=""),
        exit_conditions=[Condition(expr="True", description="")],
        sizing=SizingRule(mode="fixed_contracts", value=5),
        time_stop_sec=60,
        side="long_trailing",
        venue="polymarket",
    )


@dataclass(frozen=True)
class FakeBatchResult:
    """Minimal duck-typed stand-in for harness.run_batch.BatchResult.

    The registry only reads five attributes off whatever it's handed —
    these tests pass a tiny dataclass rather than constructing a real
    BatchResult (which would need to be aggregated from per-game results
    we don't have in scope here)."""

    pnl_gross: float
    pnl_net: float
    sharpe_per_trade_net: float
    n_trades_total: int
    win_rate: float


@dataclass(frozen=True)
class FakeDecision:
    """Minimal duck-typed stand-in for PromotionDecision."""

    passed: bool
    reasons: tuple[str, ...]
    dsr: float | None = None
    dsr_pvalue: float | None = None
    block_bootstrap_ci_lo: float | None = None
    block_bootstrap_ci_hi: float | None = None


class _TmpDBMixin(unittest.TestCase):
    """Gives each test a fresh sqlite DB path that's auto-cleaned up.

    NamedTemporaryFile(delete=False) is used because sqlite3 needs to
    open the path itself; we close the tempfile handle and rely on
    tearDown for cleanup. WAL mode produces ``-wal`` / ``-shm`` sidecar
    files that we clean too.
    """

    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".sqlite3", prefix="registry_test_")
        os.close(fd)
        # Remove the placeholder so connect() sees a fresh file (the schema
        # bootstrap code path is what we want to exercise).
        os.unlink(path)
        self.db_path: str = path

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = self.db_path + suffix
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- #
# Schema / connection
# --------------------------------------------------------------------------- #


class TestConnect(_TmpDBMixin):
    def test_connect_creates_schema_on_fresh_db(self) -> None:
        """A fresh file gets a trials table with the expected columns."""
        conn = connect(self.db_path)
        try:
            # The table exists.
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trials';"
            ).fetchone()
            self.assertIsNotNone(row, "trials table not created")

            # Column set matches EXPECTED_COLUMNS exactly, in order.
            info = conn.execute("PRAGMA table_info(trials);").fetchall()
            actual = tuple(r["name"] for r in info)
            self.assertEqual(actual, EXPECTED_COLUMNS)
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# record_trial
# --------------------------------------------------------------------------- #


class TestRecordTrial(_TmpDBMixin):
    def test_record_trial_appends_row(self) -> None:
        spec = _make_spec()
        trial_id = record_trial(
            spec=spec,
            agent_id="manual:test",
            mechanistic_writeup="why this might work",
            db_path=self.db_path,
        )
        self.assertIsInstance(trial_id, int)
        self.assertGreater(trial_id, 0)

        rows = query(spec_hash=spec.spec_hash(), db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, trial_id)
        self.assertEqual(rows[0].spec_name, "test_spec")
        self.assertEqual(rows[0].agent_id, "manual:test")
        self.assertIsNone(rows[0].claude_review)
        self.assertIsNone(rows[0].scorer_promotion_gate_passed)

    def test_record_trial_multiple_runs_same_spec(self) -> None:
        """Same spec_hash → two rows with distinct ids (no UNIQUE constraint)."""
        spec = _make_spec()
        id_a = record_trial(spec=spec, agent_id="manual:a", db_path=self.db_path)
        id_b = record_trial(spec=spec, agent_id="manual:b", db_path=self.db_path)
        self.assertNotEqual(id_a, id_b)

        rows = query(spec_hash=spec.spec_hash(), db_path=self.db_path)
        self.assertEqual(len(rows), 2)
        # Ordered DESC by ts_created, ties broken by id DESC.
        self.assertEqual(rows[0].id, id_b)
        self.assertEqual(rows[1].id, id_a)

    def test_record_trial_no_update_path(self) -> None:
        """Malicious spec_json content cannot become an executed UPDATE.

        We construct a fake spec whose to_json() returns SQL-injection-ish
        text. The registry must store it as literal text in the spec_json
        column — never execute it.
        """
        injection_payload = (
            "'); UPDATE trials SET spec_name='pwned' WHERE 1=1; --"
        )

        @dataclass(frozen=True)
        class _MaliciousSpec:
            name: str = "innocuous"
            features: tuple[str, ...] = ("margin",)
            _hash: str = "abc123" + "0" * 58

            def spec_hash(self) -> str:
                return self._hash

            def to_json(self) -> str:
                # Return the attack payload as if it were JSON. The registry
                # has to bind it via ? — if it concatenated, the UPDATE here
                # would mutate spec_name on a sibling row.
                return injection_payload

        # First, plant a real row so the malicious UPDATE would have something
        # to corrupt if injection succeeded.
        good_spec = _make_spec()
        record_trial(
            spec=good_spec, agent_id="manual:good", db_path=self.db_path
        )

        bad_spec = _MaliciousSpec()
        record_trial(spec=bad_spec, agent_id="manual:bad", db_path=self.db_path)

        # The good row's spec_name is unchanged.
        rows = query(spec_hash=good_spec.spec_hash(), db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].spec_name, "test_spec")

        # The bad row's spec_json is stored as literal text — we can read it
        # back via a direct connection (TrialRow doesn't expose spec_json).
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT spec_json FROM trials WHERE spec_hash = ?",
                (bad_spec._hash,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["spec_json"], injection_payload)


# --------------------------------------------------------------------------- #
# record_claude_review
# --------------------------------------------------------------------------- #


class TestRecordClaudeReview(_TmpDBMixin):
    def test_record_claude_review_updates_latest(self) -> None:
        """When multiple rows share a spec_hash, the review attaches to the
        most recent one (by ts_created DESC, id DESC)."""
        spec = _make_spec()
        old_id = record_trial(
            spec=spec, agent_id="manual:old", db_path=self.db_path
        )
        new_id = record_trial(
            spec=spec, agent_id="manual:new", db_path=self.db_path
        )

        updated_id = record_claude_review(
            spec.spec_hash(), "looks fine", "opus-4.7", db_path=self.db_path
        )
        self.assertEqual(updated_id, new_id)
        self.assertNotEqual(updated_id, old_id)

        # Verify by direct read.
        conn = connect(self.db_path)
        try:
            rows = {
                r["id"]: r
                for r in conn.execute(
                    "SELECT id, claude_review, claude_review_model FROM trials "
                    "WHERE spec_hash = ?",
                    (spec.spec_hash(),),
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(rows[new_id]["claude_review"], "looks fine")
        self.assertEqual(rows[new_id]["claude_review_model"], "opus-4.7")
        self.assertIsNone(rows[old_id]["claude_review"])

    def test_record_claude_review_idempotent(self) -> None:
        """Second call against an already-reviewed row raises ValueError."""
        spec = _make_spec()
        record_trial(spec=spec, agent_id="manual:test", db_path=self.db_path)
        record_claude_review(
            spec.spec_hash(), "first review", "opus-4.7", db_path=self.db_path
        )
        with self.assertRaises(ValueError) as ctx:
            record_claude_review(
                spec.spec_hash(),
                "second review",
                "opus-4.7",
                db_path=self.db_path,
            )
        self.assertIn("review already present", str(ctx.exception))

    def test_record_claude_review_requires_existing_row(self) -> None:
        """No row for the given spec_hash → ValueError."""
        with self.assertRaises(ValueError) as ctx:
            record_claude_review(
                "deadbeef" * 8,
                "review",
                "opus-4.7",
                db_path=self.db_path,
            )
        self.assertIn("no matching row", str(ctx.exception))


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #


class TestQuery(_TmpDBMixin):
    def test_query_by_spec_hash(self) -> None:
        """3 rows, 2 spec_hashes: query by one returns exactly the matches."""
        spec_a = _make_spec(name="spec_a")
        spec_b = _make_spec(name="spec_b")
        id_a1 = record_trial(spec=spec_a, agent_id="m:1", db_path=self.db_path)
        id_a2 = record_trial(spec=spec_a, agent_id="m:2", db_path=self.db_path)
        id_b1 = record_trial(spec=spec_b, agent_id="m:3", db_path=self.db_path)

        rows_a = query(spec_hash=spec_a.spec_hash(), db_path=self.db_path)
        self.assertEqual(len(rows_a), 2)
        self.assertEqual({r.id for r in rows_a}, {id_a1, id_a2})

        rows_b = query(spec_hash=spec_b.spec_hash(), db_path=self.db_path)
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0].id, id_b1)

    def test_query_gate_passed_filter(self) -> None:
        """Mixed gate_passed values; filter to passing only."""
        spec = _make_spec()
        pass_decision = FakeDecision(passed=True, reasons=())
        fail_decision = FakeDecision(passed=False, reasons=("ci_lo_below_zero",))

        id_pass = record_trial(
            spec=spec,
            agent_id="m:pass",
            scorer_result=pass_decision,
            db_path=self.db_path,
        )
        id_fail = record_trial(
            spec=_make_spec("fail_spec"),
            agent_id="m:fail",
            scorer_result=fail_decision,
            db_path=self.db_path,
        )
        # Also a row with no scorer result — gate_passed IS NULL; should NOT
        # show up in either gate_passed=True or gate_passed=False filter.
        record_trial(
            spec=_make_spec("noscorer"),
            agent_id="m:none",
            db_path=self.db_path,
        )

        pass_rows = query(gate_passed=True, db_path=self.db_path)
        self.assertEqual([r.id for r in pass_rows], [id_pass])
        self.assertTrue(pass_rows[0].scorer_promotion_gate_passed)

        fail_rows = query(gate_passed=False, db_path=self.db_path)
        self.assertEqual([r.id for r in fail_rows], [id_fail])
        self.assertFalse(fail_rows[0].scorer_promotion_gate_passed)


# --------------------------------------------------------------------------- #
# count_total_trials
# --------------------------------------------------------------------------- #


class TestCountTotalTrials(_TmpDBMixin):
    def test_count_total_trials_increments(self) -> None:
        spec = _make_spec()
        n0 = count_total_trials(db_path=self.db_path)
        self.assertEqual(n0, 0)
        record_trial(spec=spec, agent_id="manual:t", db_path=self.db_path)
        n1 = count_total_trials(db_path=self.db_path)
        self.assertEqual(n1, n0 + 1)


# --------------------------------------------------------------------------- #
# query_promotable_candidates
# --------------------------------------------------------------------------- #


class TestQueryPromotableCandidates(_TmpDBMixin):
    def test_query_promotable_candidates(self) -> None:
        """A passing, unreviewed, un-tested trial is promotable; once
        reviewed it drops off the list."""
        spec = _make_spec()
        pass_decision = FakeDecision(
            passed=True,
            reasons=(),
            block_bootstrap_ci_lo=0.01,
            block_bootstrap_ci_hi=0.05,
        )
        # val_result drives the ORDER BY.
        val_result = FakeBatchResult(
            pnl_gross=10.0,
            pnl_net=8.0,
            sharpe_per_trade_net=1.4,
            n_trades_total=42,
            win_rate=0.55,
        )

        trial_id = record_trial(
            spec=spec,
            agent_id="m:cand",
            val_result=val_result,
            scorer_result=pass_decision,
            db_path=self.db_path,
        )

        promotable = query_promotable_candidates(db_path=self.db_path)
        self.assertEqual(len(promotable), 1)
        self.assertEqual(promotable[0].id, trial_id)

        record_claude_review(
            spec.spec_hash(), "approved", "opus-4.7", db_path=self.db_path
        )
        promotable_after = query_promotable_candidates(db_path=self.db_path)
        self.assertEqual(promotable_after, [])


if __name__ == "__main__":
    unittest.main()
