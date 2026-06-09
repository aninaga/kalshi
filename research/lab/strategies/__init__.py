"""research.lab.strategies — ready-made ``Strategy`` factories for the lab.

Each submodule re-expresses a research direction as composable
``lab.strategy.Strategy`` objects. Importing a submodule is cheap: sibling lab
modules (``lab.strategy``, ``lab.signals``, ``lab.execution``) are imported
LAZILY inside the factory functions, so these modules load even in an isolated
worktree where the siblings have not landed yet.

Submodules land as their units merge:
  * ``anchoring``    (Unit 12) — totals / spreads anchoring.
  * ``calibration``  (Unit 13) — calibration / favorite-longshot / totals-extremes
                                 (DEAD; kept as substrate regression demos).
  * ``reactions``    (Unit 14) — sub-latency / fair-value-gap / term-structure / CLV.
"""
from research.lab.strategies.calibration import (  # noqa: F401
    calibration_bucket,
    favorite_longshot,
)

__all__ = ["favorite_longshot", "calibration_bucket"]
