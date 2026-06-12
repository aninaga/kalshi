"""research.lab.strategies — the ANCHORING / CALIBRATION / REACTION families,
re-expressed as composable ``lab.strategy.Strategy`` objects.

Each module here is a thin, declarative re-statement of a research script that
already ran through the honest promotion gate, ported onto the agentic
substrate (``Panel`` in, ``Strategy`` out, realistic ``FillModel`` by default).
The verdicts that justify (or retract) each family live in the module
docstrings; see ``research/ALPHA_FINDINGS.md`` for the full record.
"""
from __future__ import annotations

__all__ = ["anchoring"]
