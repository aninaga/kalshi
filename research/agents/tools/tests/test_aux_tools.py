"""Tests for the three auxiliary agent tools.

Tests:
  1. test_propose_hypothesis_valid_spec
  2. test_propose_hypothesis_leakage_spec_rejected
  3. test_read_my_results_returns_own_trials
  4. test_read_my_results_scope_strict_rejects_bad_id
  5. test_read_literature_empty_dir_returns_empty
  6. test_read_literature_finds_match
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Helper: path to the kvenv Python interpreter
# ---------------------------------------------------------------------------
_PYTHON = Path.home() / "code" / "kvenv" / "bin" / "python3"
if not _PYTHON.exists():
    _PYTHON = Path(sys.executable)

_PROJECT_ROOT = Path(__file__).resolve().parents[4]  # kalshi/


def _run_module(module: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run ``python -m <module> <args>`` from the project root."""
    cmd = [str(_PYTHON), "-m", module] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
    )


# ---------------------------------------------------------------------------
# Shared fixture: a minimal valid StrategySpec JSON
# ---------------------------------------------------------------------------

_VALID_SPEC_DICT = {
    "name": "test_margin_v1",
    "description": "Test spec — long home on large margin.",
    "features": ["margin"],
    "entry_condition": {
        "expr": "margin > 5 and pos_side is None",
        "description": "Home team leading by more than 5 points.",
    },
    "exit_conditions": [
        {"expr": "True", "description": "time-stop only"},
    ],
    "sizing": {
        "mode": "fixed_contracts",
        "value": 5,
        "max_position_contracts": 100,
    },
    "time_stop_sec": 180,
    "side": "long_home",
    "venue": "kalshi",
    "cost_assumption_bps": None,
}

_LEAKAGE_SPEC_DICT = {
    "name": "leakage_test_v1",
    "description": "References espn_home_winprob which is_live_safe=False.",
    "features": ["margin", "espn_home_winprob"],
    "entry_condition": {
        "expr": "espn_home_winprob > 0.75 and pos_side is None",
        "description": "Would trigger on leaked winprob.",
    },
    "exit_conditions": [
        {"expr": "True", "description": "never reached"},
    ],
    "sizing": {
        "mode": "fixed_contracts",
        "value": 5,
        "max_position_contracts": 100,
    },
    "time_stop_sec": 180,
    "side": "long_home",
    "venue": "polymarket",
    "cost_assumption_bps": None,
}


# ---------------------------------------------------------------------------
# Registry helper for read_my_results tests
# ---------------------------------------------------------------------------

def _insert_trial(conn, agent_id: str, spec_name: str = "test_spec") -> None:
    """Insert a minimal trial row into a test registry connection."""
    conn.execute(
        """
        INSERT INTO trials (
            spec_hash, spec_json, spec_name, features_used, agent_id
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            f"hash_{agent_id}_{spec_name}",
            json.dumps({"name": spec_name}),
            spec_name,
            json.dumps(["margin"]),
            agent_id,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. test_propose_hypothesis_valid_spec
# ---------------------------------------------------------------------------

class TestProposeHypothesis(unittest.TestCase):
    def test_propose_hypothesis_valid_spec(self):
        """A known-valid spec exits 0 and returns {valid: true}."""
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            json.dump(_VALID_SPEC_DICT, f)
            spec_path = f.name

        result = _run_module(
            "research.agents.tools.propose_hypothesis",
            ["--spec-file", spec_path],
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"Expected exit 0, got {result.returncode}.\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        data = json.loads(result.stdout)
        self.assertTrue(data["valid"], msg=f"Expected valid=True, got: {data}")
        self.assertIsInstance(data["spec_hash"], str, msg="spec_hash should be a hex string")
        self.assertEqual(data["errors"], [], msg="Expected no errors")

    # 2. test_propose_hypothesis_leakage_spec_rejected
    def test_propose_hypothesis_leakage_spec_rejected(self):
        """A spec with espn_home_winprob (is_live_safe=False) exits 1."""
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            json.dump(_LEAKAGE_SPEC_DICT, f)
            spec_path = f.name

        result = _run_module(
            "research.agents.tools.propose_hypothesis",
            ["--spec-file", spec_path],
        )
        self.assertEqual(
            result.returncode, 1,
            msg=f"Expected exit 1, got {result.returncode}.\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        data = json.loads(result.stdout)
        self.assertFalse(data["valid"], msg=f"Expected valid=False, got: {data}")
        self.assertTrue(
            len(data["errors"]) > 0,
            msg="Expected at least one error message",
        )
        # At least one error must mention the leakage feature name.
        errors_combined = " ".join(data["errors"])
        self.assertIn(
            "espn_home_winprob",
            errors_combined,
            msg=f"Expected errors to mention 'espn_home_winprob'; got: {data['errors']}",
        )


# ---------------------------------------------------------------------------
# 3 & 4. read_my_results tests
# ---------------------------------------------------------------------------

class TestReadMyResults(unittest.TestCase):
    def _make_db(self) -> Path:
        """Create a fresh temp registry DB and return its path."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = Path(tmp.name)

        # Bootstrap schema via the registry's own connect() so it's canonical.
        from research.registry.db import connect
        conn = connect(db_path)
        conn.close()
        return db_path

    def test_read_my_results_returns_own_trials(self):
        """Returns exactly the 2 rows for codex:test123, not the third row."""
        db_path = self._make_db()
        from research.registry.db import connect
        conn = connect(db_path)
        _insert_trial(conn, "codex:test123", "spec_a")
        _insert_trial(conn, "codex:test123", "spec_b")
        _insert_trial(conn, "codex:other99", "spec_c")
        conn.close()

        result = _run_module(
            "research.agents.tools.read_my_results",
            ["--agent-id", "codex:test123", "--registry-db", str(db_path)],
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"Expected exit 0.\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        rows = json.loads(result.stdout)
        self.assertIsInstance(rows, list)
        self.assertEqual(
            len(rows), 2,
            msg=f"Expected 2 rows for codex:test123, got {len(rows)}. rows={rows}",
        )
        agent_ids_in_result = {r.get("id") for r in rows}
        # Verify the spec names belong only to codex:test123.
        spec_names = {r["spec_name"] for r in rows}
        self.assertSetEqual(spec_names, {"spec_a", "spec_b"})

    def test_read_my_results_scope_strict_rejects_bad_id(self):
        """--scope-strict with agent_id 'manual:foo' exits 1."""
        db_path = self._make_db()

        result = _run_module(
            "research.agents.tools.read_my_results",
            [
                "--agent-id", "manual:foo",
                "--scope-strict",
                "--registry-db", str(db_path),
            ],
        )
        self.assertEqual(
            result.returncode, 1,
            msg=(
                f"Expected exit 1 for manual:foo with --scope-strict.\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            ),
        )


# ---------------------------------------------------------------------------
# 5 & 6. read_literature tests
# ---------------------------------------------------------------------------

class TestReadLiterature(unittest.TestCase):
    def test_read_literature_empty_dir_returns_empty(self):
        """An empty papers dir (only .gitkeep) returns an empty JSON array."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Place only a .gitkeep file — matches the real papers/ directory.
            (Path(tmpdir) / ".gitkeep").touch()

            result = _run_module(
                "research.agents.tools.read_literature",
                ["--query", "halftime bounce nba", "--papers-dir", tmpdir],
            )
            self.assertEqual(
                result.returncode, 0,
                msg=f"Expected exit 0.\nstdout={result.stdout}\nstderr={result.stderr}",
            )
            data = json.loads(result.stdout)
            self.assertEqual(data, [], msg=f"Expected empty list, got: {data}")

    def test_read_literature_finds_match(self):
        """A markdown file with 'halftime bounce' is found with score > 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            paper_path = Path(tmpdir) / "nba_notes.md"
            paper_path.write_text(
                textwrap.dedent(
                    """\
                    # NBA Halftime Patterns

                    This note explores the halftime bounce phenomenon in NBA games.
                    Teams that are trailing at halftime often show a momentum
                    reversal — the halftime bounce — in the second half.
                    """
                ),
                encoding="utf-8",
            )

            result = _run_module(
                "research.agents.tools.read_literature",
                [
                    "--query", "halftime bounce",
                    "--papers-dir", tmpdir,
                    "--max-results", "5",
                ],
            )
            self.assertEqual(
                result.returncode, 0,
                msg=f"Expected exit 0.\nstdout={result.stdout}\nstderr={result.stderr}",
            )
            data = json.loads(result.stdout)
            self.assertIsInstance(data, list, msg="Expected a JSON array")
            self.assertGreater(len(data), 0, msg="Expected at least one result")

            top = data[0]
            self.assertGreater(
                top["score"], 0,
                msg=f"Expected score > 0, got {top['score']}",
            )
            self.assertIn(
                "halftime",
                top["snippet"].lower(),
                msg=f"Expected snippet to contain 'halftime'; got: {top['snippet']}",
            )


if __name__ == "__main__":
    unittest.main()
