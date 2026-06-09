"""research.lab.session — the ergonomic ``Lab`` façade (Unit 7).

This is the single import an analyst-agent reaches for. ``Lab`` is a thin,
honest-by-default orchestrator that wires the substrate's stages together::

    Panel(s)  --strategy.run-->  Trades  --evaluate-->  GateResult

Each method LAZILY imports its sibling lab module (``research.lab.data``,
``research.lab.strategy``, ``research.lab.evaluate``, ``research.lab.hypothesis``,
``research.lab.execution``) the first time it is needed, so this module imports
cleanly even when those siblings have not yet landed — only ``research.lab.types``
is required at import time.

The headline convenience is :meth:`Lab.backtest`, which composes the full
pipeline in order::

    load(market, split)  ->  strategy.run(panels, fill_model)  ->  evaluate(trades)

Realistic execution is the DEFAULT: the session carries a single ``FillModel``
(``research.lab.execution.REALISTIC``) and threads it through every backtest so
nothing silently assumes a 0.50 fill.

See ``research/lab/CONTRACT.md`` §lab/session.py for the public API.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from research.lab.types import GateResult, Hypothesis, Panel, Trades


def _default_fill_model() -> Any:
    """Lazily fetch ``research.lab.execution.REALISTIC`` (the realistic default).

    Imported on demand so the façade has no import-time dependency on the
    execution sibling. ``Lab.fill_model`` resolves to this when left unset.
    """
    from research.lab import execution  # lazy: sibling may land after this module

    return execution.REALISTIC


@dataclass
class Lab:
    """Composable, honest-by-default session over the NBA research substrate.

    ``fill_model`` is the realistic execution model threaded through every
    backtest. Leave it ``None`` to defer to ``research.lab.execution.REALISTIC``
    (resolved lazily on first use), or inject your own ``FillModel`` (e.g. a
    cost-sweep variant) to override.
    """

    fill_model: Optional[Any] = None

    # -- resolution helpers -------------------------------------------------
    def _resolve_fill_model(self) -> Any:
        """Return the active fill model, defaulting to the realistic instance."""
        if self.fill_model is None:
            self.fill_model = _default_fill_model()
        return self.fill_model

    # -- data ---------------------------------------------------------------
    def load(self, market: str, split: Optional[str] = None, **kw: Any) -> list[Panel]:
        """Load ``Panel``s for ``market`` and ``split`` via ``lab.data``.

        Delegates to :func:`research.lab.data.load_panels`. ``split`` follows the
        data contract (``"train"``/``"val"``/``"test"``/``"nontest"``/``None``);
        the test split is never read unless explicitly requested.
        """
        from research.lab import data

        return data.load_panels(market, split, **kw)

    # -- strategy -----------------------------------------------------------
    def strategy(
        self,
        name: str,
        entry: "Callable[[Panel], Any]",
        side: "Callable[[Panel, int], str]",
        **kw: Any,
    ) -> "Strategy":
        """Build a :class:`~research.lab.types.Strategy` via ``lab.strategy``.

        Delegates to :class:`research.lab.strategy.Strategy`, passing through any
        extra knobs (``exit``, ``entry_latency_min``, ``max_stale_min``,
        ``min_elapsed``, ``max_elapsed``, ...).
        """
        from research.lab import strategy as strategy_mod

        return strategy_mod.Strategy(name=name, entry=entry, side=side, **kw)

    # -- evaluate -----------------------------------------------------------
    def evaluate(self, trades: Trades, **kw: Any) -> GateResult:
        """Run the promotion gate on ``trades`` via ``lab.evaluate``.

        Delegates to :func:`research.lab.evaluate.evaluate`. The session's
        ``fill_model`` is supplied as the default unless the caller overrides it.
        """
        from research.lab import evaluate as evaluate_mod

        kw.setdefault("fill_model", self._resolve_fill_model())
        return evaluate_mod.evaluate(trades, **kw)

    # -- backtest (the headline composition) --------------------------------
    def backtest(
        self,
        strategy: "Strategy",
        market: str,
        split: str = "nontest",
    ) -> GateResult:
        """Compose ``load -> strategy.run -> evaluate`` in order.

        1. ``load(market, split)`` — realistic panels for the split (defaults to
           ``"nontest"``; never the test split).
        2. ``strategy.run(panels, fill_model=...)`` — one trade per qualifying
           game, filled via the session's realistic ``FillModel``.
        3. ``evaluate(trades, fill_model=...)`` — the promotion gate.
        """
        fill_model = self._resolve_fill_model()
        panels = self.load(market, split)
        trades = strategy.run(panels, fill_model=fill_model)
        return self.evaluate(trades)

    # -- hypothesis registry ------------------------------------------------
    def register(self, hypothesis: Hypothesis) -> Hypothesis:
        """Register a :class:`~research.lab.types.Hypothesis` via ``lab.hypothesis``.

        Delegates to :func:`research.lab.hypothesis.register` (append-only,
        de-duplicating registry).
        """
        from research.lab import hypothesis as hypothesis_mod

        return hypothesis_mod.register(hypothesis)

    def open_hypotheses(self) -> list[Hypothesis]:
        """Return the open hypotheses via :func:`research.lab.hypothesis.open_hypotheses`."""
        from research.lab import hypothesis as hypothesis_mod

        return hypothesis_mod.open_hypotheses()


def lab() -> Lab:
    """Return a default :class:`Lab` session (realistic execution by default)."""
    return Lab()
