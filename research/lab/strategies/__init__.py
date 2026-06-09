"""research.lab.strategies — concrete Strategy families, expressed on the lab.

Each module here re-expresses a previously hand-cloned evaluator script as
composable ``lab.strategy.Strategy`` objects (entry mask + side label over a
``Panel``), so the lab's realistic execution and promotion gate are applied by
the shared ``Strategy.run`` / ``lab.evaluate`` machinery instead of being
re-implemented per script.

The ``reactions`` family (substitution-latency, fair-value-gap, term-structure,
CLV) is kept as a REGRESSION DEMO of the substrate, not as a live edge: every
mechanism was found DEAD on the 2025-26 season (efficient / inverts
out-of-sample / ~0c tradeable). See ``research/ALPHA_FINDINGS.md``,
``research/CLV_FINDINGS.md`` and ``research/TERM_STRUCTURE_FINDINGS.md``.
"""
