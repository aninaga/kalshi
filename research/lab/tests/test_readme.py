"""Contract test for the research-platform guide (Unit 15).

The README is the self-documenting "how to be an analyst here" guide. This test
asserts it exists and references the key substrate surfaces and non-negotiables,
so the guide cannot silently drift away from the contract. It is a pure
file-content check — no lab modules are imported, so it runs in any worktree.
"""
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"

# Key surfaces / non-negotiables the analyst guide must reference.
REQUIRED_MENTIONS = (
    "Panel",        # the data primitive
    "Strategy",     # the composable idea
    "lab.evaluate", # one-call evaluation -> GateResult
    "hypothesis",   # the dynamic registry
    "test split",   # never read it
    "0.50",         # the fill artifact cautionary tale
)


def test_readme_exists():
    assert README.is_file(), f"missing research-platform guide: {README}"
    assert README.read_text(encoding="utf-8").strip(), "README.md is empty"


@pytest.mark.parametrize("token", REQUIRED_MENTIONS)
def test_readme_mentions_key_surface(token):
    text = README.read_text(encoding="utf-8")
    assert token in text, f"README.md must reference {token!r}"
