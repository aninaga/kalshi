"""Contract test for the analyst brief (`analyst_prompt.md`).

This does NOT spawn an agent — it verifies the brief still hands a deployed
analyst the substrate toolkit and the hard laws it must encode. If any of these
keywords disappear, the brief has stopped pointing at the lab or has dropped a
law the analyst relies on.

Tests:
  1. test_brief_exists_and_nonempty — file exists and is substantive.
  2. test_brief_mentions_required_keywords — the toolkit handle
     (research.lab), pre-registration (hypothesis), the judge (gate), the
     off-limits holdout (test split), and the 0.50-fill cautionary tale (0.50)
     all appear verbatim.
"""

from __future__ import annotations

import unittest
from pathlib import Path


_THIS = Path(__file__).resolve()
_WORKERS_DIR = _THIS.parent.parent
_BRIEF = _WORKERS_DIR / "analyst_prompt.md"

# The contract: these substrings must survive any edit to the brief.
_REQUIRED = ["research.lab", "hypothesis", "gate", "test split", "0.50"]


class AnalystBriefContractTests(unittest.TestCase):
    """Static contract checks on the analyst brief."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.brief_text = _BRIEF.read_text(encoding="utf-8")

    def test_brief_exists_and_nonempty(self) -> None:
        self.assertTrue(_BRIEF.exists(), f"analyst brief missing: {_BRIEF}")
        self.assertGreater(
            len(self.brief_text),
            500,
            f"brief is suspiciously short: {len(self.brief_text)} chars",
        )

    def test_brief_mentions_required_keywords(self) -> None:
        missing = [kw for kw in _REQUIRED if kw not in self.brief_text]
        self.assertEqual(
            missing,
            [],
            f"analyst brief is missing required keywords: {missing}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
