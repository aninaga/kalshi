"""Test isolation for the lab substrate.

Several strategy/façade unit tests were authored in isolated worktrees where the
sibling `research.lab.*` modules did not yet exist, so they stub those modules
into ``sys.modules``. Run together (post-integration) that stubbing leaks across
tests and shadows the real modules (e.g. a stub `research.lab.strategy` without
``staleness_min``). This autouse fixture purges the lab modules after every test
so each test re-imports the real modules fresh from disk.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _isolate_lab_modules():
    yield
    for name in [m for m in sys.modules
                 if m == "research.lab" or m.startswith("research.lab.")]:
        del sys.modules[name]
