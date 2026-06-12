"""Compatibility alias: this package moved to ``research/nba_odds_study``.

Commit df4e093 ("Separate the two projects: arb bot vs hedge-fund research",
arb line) moved the package; the research line (17 modules: research/lab/data.py
+ research/scripts/*) kept importing it by this historical root name. The two
lines merged on 2026-06-12, which silently broke every root-style import — a
semantic merge conflict git could not see (the move and the imports lived on
disjoint branches).

This shim aliases the root name to the real package, so BOTH import styles
resolve to the SAME module objects:

    from nba_odds_study import analysis            # research line (historical)
    from research.nba_odds_study import analysis   # arb line

Caveat: a process that imports the same SUBMODULE under both styles gets two
module objects for one file (standard Python aliasing limitation). Avoid
mixing styles within one module; new code should prefer the research.* path.
"""
import sys

from research import nba_odds_study as _pkg

sys.modules[__name__] = _pkg
