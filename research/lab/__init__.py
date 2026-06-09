"""research.lab — the native-agentic NBA research substrate.

A composable toolkit an analyst-agent imports to ORIGINATE and TEST ideas, with
honest execution and the promotion gate built in. See PLAN.md / CONTRACT.md /
README.md. Only `types` is guaranteed present during parallel development; other
submodules land as their units merge.
"""
from research.lab.types import (  # noqa: F401
    MARKETS, WINNER, TOTAL, SPREAD,
    Panel, Trade, Trades, FillResult, GateResult, Hypothesis,
    synthetic_panel, synthetic_panels,
)

__all__ = [
    "MARKETS", "WINNER", "TOTAL", "SPREAD",
    "Panel", "Trade", "Trades", "FillResult", "GateResult", "Hypothesis",
    "synthetic_panel", "synthetic_panels",
]
