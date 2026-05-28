"""Unit tests for the Claude adversarial reviewer.

Each test gets its own tempfile-backed sqlite registry and a tempdir for
packet output; we never write to the real ``market_data/trials.db`` or
``market_data/review_packets/`` from tests.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from research.agents.reviewers.claude_review import (
    DEFAULT_PROMPT_PATH,
    record_review_text,
    review_trial,
)
from research.harness.strategy_spec import Condition, SizingRule, StrategySpec
from research.registry.api import record_trial


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_spec(name: str = "test_spec", value: float = 5.0) -> StrategySpec:
    """Build a minimal StrategySpec that passes validation. ``value`` is
    bumped to vary spec_hash when the same ``name`` is reused across rows
    (sibling-count tests).
    """
    return StrategySpec(
        name=name,
        description=f"test fixture {name}",
        features=["margin", "pm_implied_wp"],
        entry_condition=Condition(expr="margin < 0", description=""),
        exit_conditions=[Condition(expr="True", description="")],
        sizing=SizingRule(mode="fixed_contracts", value=value),
        time_stop_sec=60,
        side="long_trailing",
        venue="polymarket",
    )


class _TmpEnvMixin(unittest.TestCase):
    """Gives each test a fresh sqlite DB and a clean packet directory.

    Mirrors the registry tests' tempfile-handling discipline: open + close
    immediately so sqlite can own the path, unlink the placeholder so
    ``connect()`` exercises the schema-bootstrap path.
    """

    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".sqlite3", prefix="reviewer_test_")
        os.close(fd)
        os.unlink(path)
        self.db_path: str = path
        self.packet_dir: Path = Path(tempfile.mkdtemp(prefix="reviewer_pkt_"))

    def tearDown(self) -> None:
        # DB plus its WAL sidecars.
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = self.db_path + suffix
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        # Packet directory and its contents.
        if self.packet_dir.exists():
            for f in self.packet_dir.iterdir():
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
            try:
                self.packet_dir.rmdir()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# review_trial — dry-run and file-output
# --------------------------------------------------------------------------- #


class TestReviewTrialDryRun(_TmpEnvMixin):
    def test_review_trial_dry_run_returns_status(self) -> None:
        """A trial exists; dry_run=True returns status dict with no file IO."""
        spec = _make_spec()
        record_trial(
            spec=spec,
            agent_id="codex:abc12345",
            mechanistic_writeup="trail-by-8 spec, fades on continuation pace",
            db_path=self.db_path,
        )

        result = review_trial(
            spec.spec_hash(),
            db_path=self.db_path,
            dry_run=True,
            packet_dir=self.packet_dir,
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["spec_hash"], spec.spec_hash())
        self.assertGreater(result["packet_size"], 0)
        # No file should have been written.
        self.assertEqual(list(self.packet_dir.iterdir()), [])


class TestReviewTrialWritesPacket(_TmpEnvMixin):
    def test_review_trial_writes_packet(self) -> None:
        """Non-dry-run writes a markdown file containing the spec JSON and
        the mechanistic writeup substrings."""
        spec = _make_spec()
        writeup = "score-run momentum, buy-hot direction; mechanism: clutch-shooting persistence"
        record_trial(
            spec=spec,
            agent_id="codex:def67890",
            mechanistic_writeup=writeup,
            db_path=self.db_path,
        )

        result = review_trial(
            spec.spec_hash(),
            db_path=self.db_path,
            dry_run=False,
            packet_dir=self.packet_dir,
        )

        self.assertEqual(result["status"], "pending_human_spawn")
        packet_path = Path(result["packet_path"])
        self.assertTrue(packet_path.exists(), "packet markdown not written")
        self.assertTrue(packet_path.name.startswith(spec.spec_hash()))
        self.assertTrue(packet_path.name.endswith(".md"))

        content = packet_path.read_text(encoding="utf-8")
        # The writeup must appear verbatim.
        self.assertIn(writeup, content)
        # Spec fields must appear — name, side, venue, feature names.
        self.assertIn(spec.name, content)
        self.assertIn("polymarket", content)
        self.assertIn("long_trailing", content)
        self.assertIn("margin", content)
        # The spec_hash should appear in the header section.
        self.assertIn(spec.spec_hash(), content)


# --------------------------------------------------------------------------- #
# record_review_text — idempotency and missing-row error
# --------------------------------------------------------------------------- #


class TestRecordReviewText(_TmpEnvMixin):
    def test_record_review_text_idempotent(self) -> None:
        """Second call against an already-reviewed row raises ValueError —
        confirms the registry's idempotency carries through the wrapper."""
        spec = _make_spec()
        record_trial(spec=spec, agent_id="manual:idem", db_path=self.db_path)

        row_id = record_review_text(
            spec.spec_hash(),
            "first review text",
            db_path=self.db_path,
        )
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)

        with self.assertRaises(ValueError) as ctx:
            record_review_text(
                spec.spec_hash(),
                "second review text",
                db_path=self.db_path,
            )
        self.assertIn("review already present", str(ctx.exception))

    def test_record_review_text_invalid_spec_hash(self) -> None:
        """A spec_hash with no matching trial row raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            record_review_text(
                "deadbeef" * 8,  # 64 hex chars, but no row for it
                "review text",
                db_path=self.db_path,
            )
        self.assertIn("no matching row", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Sibling-failure count in the packet
# --------------------------------------------------------------------------- #


class TestReviewPacketSiblingCount(_TmpEnvMixin):
    def test_review_packet_contains_sibling_count(self) -> None:
        """Insert one passing candidate and 2 failed siblings (same spec_name,
        different spec_hash, gate_passed=0). The packet should reflect
        sibling_failed_count >= 2."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeFailDecision:
            passed: bool = False
            reasons: tuple[str, ...] = ("ci_lo_below_zero",)
            block_bootstrap_ci_lo: float | None = -0.01
            block_bootstrap_ci_hi: float | None = 0.02
            dsr: float | None = None
            dsr_pvalue: float | None = None

        # The candidate-under-review.
        candidate = _make_spec(name="family_x", value=5.0)
        record_trial(
            spec=candidate,
            agent_id="codex:cand",
            mechanistic_writeup="candidate writeup",
            db_path=self.db_path,
        )

        # Two failed siblings: same spec_name, different value → different
        # spec_hash, with gate_passed=0 via the scorer_result.
        sib_a = _make_spec(name="family_x", value=3.0)
        sib_b = _make_spec(name="family_x", value=7.0)
        record_trial(
            spec=sib_a,
            agent_id="codex:sib_a",
            scorer_result=FakeFailDecision(),
            db_path=self.db_path,
        )
        record_trial(
            spec=sib_b,
            agent_id="codex:sib_b",
            scorer_result=FakeFailDecision(),
            db_path=self.db_path,
        )

        # Sanity: the three specs really do have distinct hashes.
        self.assertNotEqual(candidate.spec_hash(), sib_a.spec_hash())
        self.assertNotEqual(candidate.spec_hash(), sib_b.spec_hash())

        result = review_trial(
            candidate.spec_hash(),
            db_path=self.db_path,
            dry_run=True,
            packet_dir=self.packet_dir,
        )
        # Direct count is exposed in the status dict.
        self.assertGreaterEqual(result["sibling_failed_count"], 2)

        # And the markdown packet (which we'd have written if not dry_run)
        # should also surface this fact. Re-run non-dry to inspect.
        result_w = review_trial(
            candidate.spec_hash(),
            db_path=self.db_path,
            dry_run=False,
            packet_dir=self.packet_dir,
        )
        content = Path(result_w["packet_path"]).read_text(encoding="utf-8")
        self.assertIn("siblings", content.lower())
        # The literal count of 2 must appear somewhere in the survivorship
        # section. We tolerate ">= 2" exact-match because that's what the
        # renderer prints.
        self.assertRegex(content, r"siblings.*:\s*2")


# --------------------------------------------------------------------------- #
# prompt.md required sections
# --------------------------------------------------------------------------- #


class TestPromptMdRequiredSections(unittest.TestCase):
    def test_prompt_md_has_required_sections(self) -> None:
        """The reviewer prompt must include all 8 failure-mode section
        headers plus the Recommendation header. Loose substring match — we
        don't pin exact wording so the prompt can evolve."""
        self.assertTrue(
            DEFAULT_PROMPT_PATH.exists(),
            f"reviewer prompt missing at {DEFAULT_PROMPT_PATH}",
        )
        prompt = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")

        required_substrings = [
            "Mechanism",
            "Multiple-testing",
            "Regime",
            "Threshold",
            "Cost-model",
            "Leakage",
            "Concentration",
            "Survivorship",
            "Recommendation",
        ]
        for needle in required_substrings:
            with self.subTest(section=needle):
                self.assertIn(
                    needle,
                    prompt,
                    f"prompt.md missing required section keyword {needle!r}",
                )


if __name__ == "__main__":
    unittest.main()
