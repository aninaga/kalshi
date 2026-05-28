"""Contract tests for the Codex worker prompt.

These tests do NOT actually spawn `codex exec` — they verify that the prompt
markdown file contains the keywords, sections, and structural cues that the
orchestrator relies on. If any of these tests fail, the orchestrator's
output-parsing or the worker's behavior-shaping will break in production.

Tests:
  1. test_prompt_md_exists_and_nonempty — file exists and is > 1000 chars.
  2. test_prompt_lists_all_live_safe_features — every live-safe feature name
     appears verbatim in the prompt.
  3. test_prompt_warns_about_forbidden_features — espn_home_winprob appears
     under the FORBIDDEN section.
  4. test_prompt_specifies_result_md_json_block — the prompt contains the
     literal substring used by the orchestrator regex (```json).
  5. test_example_result_parses — example_result.md's JSON block parses
     under the orchestrator's exact regex pattern.
  6. test_prompt_mentions_run_backtest_cli — prompt mentions the
     run_backtest module path and --spec-file flag.
  7. test_prompt_mentions_validation_step — prompt mentions
     propose_hypothesis.
  8. test_prompt_disallows_test_split — prompt explicitly forbids --split
     test (i.e., contains a FORBIDDEN/not-allowed callout near the test
     split mention).
  9. test_writeup_section_present — prompt mentions writeup.md and asks
     for a mechanism explanation.
 10. test_prompt_file_under_size_cap — prompt is under 20 KB.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


_THIS = Path(__file__).resolve()
_WORKERS_DIR = _THIS.parent.parent
_PROMPT = _WORKERS_DIR / "codex_worker_prompt.md"
_EXAMPLE_DIRECTION = _WORKERS_DIR / "example_direction.md"
_EXAMPLE_RESULT = _WORKERS_DIR / "example_result.md"

# Mirror the orchestrator's regex EXACTLY. This is the contract: if the
# orchestrator changes its parser, this regex updates too.
_ORCHESTRATOR_JSON_BLOCK_RE = re.compile(
    r"^```json\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE
)


class PromptContractTests(unittest.TestCase):
    """Static contract checks on the worker prompt and its examples."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt_text = _PROMPT.read_text(encoding="utf-8")
        cls.example_direction_text = _EXAMPLE_DIRECTION.read_text(encoding="utf-8")
        cls.example_result_text = _EXAMPLE_RESULT.read_text(encoding="utf-8")

    # -- 1 ------------------------------------------------------------------
    def test_prompt_md_exists_and_nonempty(self) -> None:
        self.assertTrue(_PROMPT.exists(), f"prompt file missing: {_PROMPT}")
        self.assertGreater(
            len(self.prompt_text),
            1000,
            f"prompt is suspiciously short: {len(self.prompt_text)} chars",
        )

    # -- 2 ------------------------------------------------------------------
    def test_prompt_lists_all_live_safe_features(self) -> None:
        from research.features.registry import REGISTRY

        missing = []
        for name in REGISTRY.list_live_safe():
            if name not in self.prompt_text:
                missing.append(name)
        self.assertEqual(
            missing,
            [],
            f"prompt is missing live-safe feature names: {missing}",
        )

    # -- 3 ------------------------------------------------------------------
    def test_prompt_warns_about_forbidden_features(self) -> None:
        self.assertIn(
            "espn_home_winprob",
            self.prompt_text,
            "prompt must reference the leakage canary by name",
        )
        # The mention must appear under a FORBIDDEN-ish heading so workers
        # don't accidentally read the canary as an example feature.
        forbidden_idx = self.prompt_text.find("FORBIDDEN")
        canary_idx = self.prompt_text.find("espn_home_winprob")
        self.assertGreater(forbidden_idx, -1, "no FORBIDDEN section in prompt")
        # The canary mention should be after at least one FORBIDDEN header.
        self.assertGreater(
            canary_idx,
            forbidden_idx,
            "espn_home_winprob is mentioned before any FORBIDDEN heading; "
            "make sure the canary is clearly under the forbidden list.",
        )

    # -- 4 ------------------------------------------------------------------
    def test_prompt_specifies_result_md_json_block(self) -> None:
        # The orchestrator regex starts each match with the literal "```json"
        # at start-of-line. The prompt must show this fence to the worker.
        self.assertIn(
            "```json",
            self.prompt_text,
            "prompt must show the ```json fence so the worker knows the "
            "result.md JSON-block format.",
        )

    # -- 5 ------------------------------------------------------------------
    def test_example_result_parses(self) -> None:
        match = _ORCHESTRATOR_JSON_BLOCK_RE.search(self.example_result_text)
        self.assertIsNotNone(
            match,
            "example_result.md does not contain a JSON code block that "
            "matches the orchestrator's parser regex",
        )
        # mypy/pyright won't narrow this without a cast.
        assert match is not None
        payload = json.loads(match.group(1))
        self.assertIn("spec_hash", payload)
        self.assertIn("gate_passed", payload)
        self.assertIn("status", payload)
        # Sanity on types so an orchestrator that asserts shape doesn't blow up.
        self.assertIsInstance(payload["spec_hash"], (str, type(None)))
        self.assertIsInstance(payload["gate_passed"], bool)
        self.assertIsInstance(payload["status"], str)

    # -- 6 ------------------------------------------------------------------
    def test_prompt_mentions_run_backtest_cli(self) -> None:
        self.assertIn(
            "research.agents.tools.run_backtest",
            self.prompt_text,
            "prompt must reference the run_backtest module path the worker "
            "will invoke",
        )
        self.assertIn(
            "--spec-file",
            self.prompt_text,
            "prompt must reference the --spec-file flag",
        )

    # -- 7 ------------------------------------------------------------------
    def test_prompt_mentions_validation_step(self) -> None:
        self.assertIn(
            "research.agents.tools.propose_hypothesis",
            self.prompt_text,
            "prompt must instruct the worker to validate via "
            "propose_hypothesis before running the backtest",
        )

    # -- 8 ------------------------------------------------------------------
    def test_prompt_disallows_test_split(self) -> None:
        # The prompt must explicitly refuse `--split test` for workers.
        # Accept either "FORBIDDEN" or "not allowed" near a test-split mention.
        # Locate the test-split keyword first; then check the preceding text
        # has the refusal vocabulary.
        candidates = ["--split test", "test split", "test-split"]
        match_idx = -1
        for kw in candidates:
            i = self.prompt_text.lower().find(kw.lower())
            if i != -1:
                match_idx = i
                break
        self.assertGreater(
            match_idx,
            -1,
            "prompt does not mention 'test split' at all; the worker has "
            "no way to know this is disallowed.",
        )
        # Look in the surrounding window for the refusal vocabulary.
        window_start = max(0, match_idx - 400)
        window_end = min(len(self.prompt_text), match_idx + 400)
        window = self.prompt_text[window_start:window_end].lower()
        self.assertTrue(
            ("forbidden" in window) or ("not allowed" in window),
            "the test-split mention is not flanked by a 'FORBIDDEN' or 'not "
            "allowed' callout; workers may attempt --split test.",
        )

    # -- 9 ------------------------------------------------------------------
    def test_writeup_section_present(self) -> None:
        self.assertIn(
            "writeup.md",
            self.prompt_text,
            "prompt must instruct the worker to produce ./writeup.md",
        )
        self.assertIn(
            "mechanism",
            self.prompt_text.lower(),
            "prompt must ask for a mechanism explanation in the writeup",
        )

    # -- 10 -----------------------------------------------------------------
    def test_prompt_file_under_size_cap(self) -> None:
        size_bytes = _PROMPT.stat().st_size
        self.assertLess(
            size_bytes,
            20 * 1024,
            f"prompt is {size_bytes} bytes — CLI prompt-file limits are "
            "around 20kb; trim it.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
