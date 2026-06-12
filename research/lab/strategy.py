"""research.lab.strategy — the composable ``Strategy`` (Unit 4).

The open replacement for the frozen ``StrategySpec`` DSL: instead of filling an
AST-locked template, an analyst composes three plain callables over a
:class:`~research.lab.types.Panel` —

  * ``entry(panel) -> np.ndarray`` — a boolean mask (length ``n``). The FIRST
    ``True`` bar that also lies in ``[min_elapsed, max_elapsed]`` AND whose quote
    is fresh (staleness ``<= max_stale_min``) fires one trade for that game.
  * ``side(panel, bar) -> str``  — the human side label bet at that bar
    ("over"/"under"/"long_home"/"long_away"/...).
  * ``exit`` — ``"settlement"`` (hold to the final whistle) or a bar-selector
    callable ``exit(panel, entry_bar) -> int`` returning the exit bar index.

Honest execution is built in: the fill is locked one ``entry_latency_min`` later
than the signal bar (the i+1 latency that prevents look-ahead), and the entry is
priced by a realistic ``fill_model`` (snap to a listed strike, fill at the REAL
quoted prob, cross a half-spread, pay the taker fee) — NEVER a 0.50 fill, the
artifact that falsely certified totals (see ``research/TOTALS_REFINE_FINDINGS``).

This generalizes ``build_trades`` from ``research/scripts/totals_alpha.py`` to any
market and any composed signal, returning a :class:`~research.lab.types.Trades`.

Sibling lab modules (e.g. ``lab.execution``) may not be merged in this worktree,
so ``fill_model`` is duck-typed (anything with ``.fill(panel, ts, side) ->
FillResult``) and the realistic default is imported lazily only when needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Union

import numpy as np

from research.lab.types import (
    OVER_SIDES,
    SPREAD,
    TOTAL,
    WINNER,
    Panel,
    Trade,
    Trades,
    is_over_side,
)

# Side labels that mean "bet the outcome ends ABOVE the strike / home wins".
# De-duplicated 2026-06-12: this is now an ALIAS of the single source of truth
# ``research.lab.types.OVER_SIDES`` (its complement ``SHORT_SIDES`` carries
# ``long_away`` etc.). Settlement/exit orientation goes through
# ``is_over_side`` so an UNKNOWN label raises rather than silently settling as
# the away/below bet (audit defect C3). ``_OVER_SIDES`` is kept as a name for
# the existing taxonomy test that imports ``strategy._OVER_SIDES``.
_OVER_SIDES = OVER_SIDES


def staleness_min(panel: Panel) -> np.ndarray:
    """Minutes since ``panel.mid`` last actually changed, per bar.

    Computed inline (no dependency on ``lab.signals``) exactly as the totals
    alpha freshness guard does: the historical ladder is sparse mid-period and
    often flat, so a stale quote vs live game state is a measurement artifact,
    not a tradeable edge — you would fill at the fresh line. Bars indexed by
    minute, so the count is in minutes.
    """
    mid = np.asarray(panel.mid, dtype=float)
    n = len(mid)
    if n == 0:
        return np.zeros(0)
    changed = np.r_[True, np.abs(np.diff(mid)) > 1e-9]
    last_changed = np.maximum.accumulate(np.where(changed, np.arange(n), -1))
    return (np.arange(n) - last_changed).astype(float)


@dataclass
class Strategy:
    """A composable strategy over per-game :class:`Panel`s.

    One trade per game: it fires at the first bar where ``entry(panel)`` is True
    within ``[min_elapsed, max_elapsed]`` and the quote is fresh. The entry is
    filled honestly ``entry_latency_min`` later via ``fill_model`` and held to
    settlement (or to a bar selected by a callable ``exit``).
    """

    name: str
    entry: Callable[[Panel], np.ndarray]
    side: Callable[[Panel, int], str]
    exit: Union[str, Callable[[Panel, int], int]] = "settlement"
    entry_latency_min: float = 1.0
    max_stale_min: float = 2.0
    min_elapsed: float = 600.0
    max_elapsed: float = 2520.0
    # Optional strike pinning: ``pick_strike(panel, signal_bar) -> float|None``
    # locks the fill to the strike the SIGNAL evaluated instead of letting the
    # fill model re-snap at i+1 — between signal and fill the mid can cross a
    # bucket midpoint, flip the snap, and execute the OPPOSITE exposure (the
    # snap-flip hazard; see runs/model_bakeoff_20260609.md). ``None`` return
    # falls back to the fill model's snap. Requires a fill model whose ``fill``
    # accepts a ``strike`` kwarg (``lab.execution.FillModel`` does).
    pick_strike: Optional[Callable[[Panel, int], Optional[float]]] = None
    meta: dict = field(default_factory=dict)

    def run(self, panels: list[Panel], fill_model=None) -> Trades:
        """Backtest over ``panels``; return one :class:`Trade` per qualifying game."""
        if fill_model is None:
            # Lazy import: lab.execution is a sibling unit that may not be merged
            # in this worktree, and we only need it when no model is supplied.
            from research.lab.execution import REALISTIC as fill_model  # noqa: PLC0415

        rows: list[Trade] = []
        for panel in panels:
            trade = self._run_one(panel, fill_model)
            if trade is not None:
                rows.append(trade)
        return Trades(rows=rows)

    # -- internals -----------------------------------------------------------

    def _run_one(self, panel: Panel, fill_model) -> Optional[Trade]:
        n = panel.n
        if n == 0:
            return None

        mask = np.asarray(self.entry(panel), dtype=bool)
        if mask.shape[0] != n:
            raise ValueError(
                f"entry() returned mask of len {mask.shape[0]}, expected {n} "
                f"for game {panel.game_id}")

        elapsed = np.asarray(panel.elapsed_sec, dtype=float)
        ts = np.asarray(panel.minute_ts, dtype=float)
        stale = staleness_min(panel)

        bar = self._first_qualifying_bar(mask, elapsed, stale)
        if bar is None:
            return None

        side = self.side(panel, bar)
        # Honest i+1 latency: lock the fill one latency-step after the signal.
        entry_ts = float(ts[bar] + self.entry_latency_min * 60.0)
        if self.pick_strike is not None:
            # Pass the kwarg only when pinning is requested so duck-typed fill
            # models without ``strike`` support keep working unchanged.
            fill = fill_model.fill(panel, entry_ts, side,
                                   strike=self.pick_strike(panel, bar))
        else:
            fill = fill_model.fill(panel, entry_ts, side)
        if fill is None or not np.isfinite(fill.fill_mid):
            return None

        exit_bar, exit_ts, payoff, exit_cost = self._resolve_exit(
            panel, bar, side, fill, fill_model)

        all_in = fill.all_in_price
        # ``payoff`` is the GROSS settlement/mark value; a non-hold exit also
        # pays the exit-side half-spread + venue fee to close (``exit_cost``).
        # Settlement (hold to the whistle) has no exit transaction -> 0.
        pnl = float(payoff - all_in - exit_cost)

        return Trade(
            game_id=panel.game_id,
            date=panel.date,
            market=panel.market,
            home_team=panel.home_team,
            primary_team=self._primary_team(panel, side),
            side=side,
            entry_ts=entry_ts,
            exit_ts=float(exit_ts),
            entry_price=float(fill.fill_mid),
            payoff=float(payoff),
            pnl=pnl,
            entry_strike=(None if fill.strike is None else float(fill.strike)),
            meta={
                "strategy": self.name,
                "signal_bar": int(bar),
                "exit_bar": int(exit_bar),
                "half_spread": float(fill.half_spread),
                "fee": float(fill.fee),
                "all_in_price": float(all_in),
                "exit_cost": float(exit_cost),
            },
        )

    def _first_qualifying_bar(self, mask, elapsed, stale) -> Optional[int]:
        """Index of the first fresh, in-window bar where the entry mask is True."""
        window = (elapsed >= self.min_elapsed) & (elapsed <= self.max_elapsed)
        fresh = stale <= self.max_stale_min
        qualifying = np.flatnonzero(mask & window & fresh & np.isfinite(elapsed))
        if qualifying.size == 0:
            return None
        return int(qualifying[0])

    def _resolve_exit(self, panel: Panel, entry_bar: int, side: str, fill,
                      fill_model):
        """Return ``(exit_bar, exit_ts, payoff, exit_cost)`` for the exit policy.

        ``exit_cost`` is 0 for a hold-to-settlement exit (no transaction) and
        the exit-side half-spread + venue fee for a callable mark-to-market exit
        (you cross to close). A callable exit MUST select a bar strictly AFTER
        the entry bar (priced one ``entry_latency_min`` after entry); a
        ``exit_bar <= entry_bar`` would mark the close before — or at — the open
        and is rejected (raise) rather than booking a same-bar round trip.
        """
        ts = np.asarray(panel.minute_ts, dtype=float)
        if self.exit == "settlement":
            exit_bar = panel.n - 1
            payoff = self._settlement_payoff(panel, side, fill.strike)
            return exit_bar, ts[exit_bar], payoff, 0.0

        if callable(self.exit):
            raw_exit_bar = int(self.exit(panel, entry_bar))
            exit_bar = min(raw_exit_bar, panel.n - 1)
            # Reject a close at/before the open AFTER clamping to the last bar:
            # an entry on the final bar leaves no future bar to exit into.
            if exit_bar <= entry_bar:
                raise ValueError(
                    f"exit bar {raw_exit_bar} (clamped {exit_bar}) is not after "
                    f"the entry bar {entry_bar} for game {panel.game_id}: a "
                    f"callable exit may not close before/at the open (would book "
                    f"a same-bar or look-ahead round trip)")
            gross = self._exit_price(panel, exit_bar, side, fill.strike)
            # Crossing to CLOSE costs the same half-spread + venue fee as the
            # open (the i+1 latency is already baked into requiring exit>entry).
            exit_cost = self._exit_cost(fill_model, fill, gross)
            return exit_bar, ts[exit_bar], gross, exit_cost

        raise ValueError(
            f"exit must be 'settlement' or a callable, got {self.exit!r}")

    @staticmethod
    def _exit_cost(fill_model, fill, gross: float) -> float:
        """Exit-side half-spread + venue fee charged to CLOSE at ``gross``.

        A real :class:`lab.execution.FillModel` exposes ``half_spread`` and a
        ``fee(price)`` schedule — price the exit on the gross mark, exactly as
        the open was priced. A duck-typed stub fill model (test plumbing) may
        expose neither; fall back to the recorded ENTRY costs so the exit at
        least pays a symmetric half-spread + fee rather than zero.
        """
        hs = getattr(fill_model, "half_spread", None)
        fee_fn = getattr(fill_model, "fee", None)
        if hs is not None and callable(fee_fn):
            return float(hs) + float(fee_fn(gross))
        # Stub fallback: mirror the entry's own half-spread + fee.
        return float(getattr(fill, "half_spread", 0.0)) + float(
            getattr(fill, "fee", 0.0))

    def _settlement_payoff(self, panel: Panel, side: str, strike) -> float:
        """Settlement value in [0, 1] for the bet ``side`` vs the filled strike.

        Winner market settles against ``home_won`` (no strike ladder). Total and
        spread settle against the final outcome vs the locked strike: an "over"
        (or long-home) side pays 1 iff the outcome ended ABOVE the strike.

        ``is_over_side`` raises on an UNKNOWN label rather than silently
        treating it as the away/below bet (audit defect C3).
        """
        bet_above = is_over_side(side)

        if panel.market == WINNER:
            if panel.home_won is None:
                return float("nan")
            home_won = bool(panel.home_won)
            return 1.0 if (home_won == bet_above) else 0.0

        if panel.market == TOTAL:
            outcome = panel.final_total
        elif panel.market == SPREAD:
            outcome = panel.final_margin
        else:  # unknown market: fall back to the margin outcome.
            outcome = panel.final_margin

        if outcome is None or strike is None or not np.isfinite(strike):
            return float("nan")
        ended_above = float(outcome) > float(strike)
        return 1.0 if (ended_above == bet_above) else 0.0

    def _exit_price(self, panel: Panel, exit_bar: int, side: str, strike) -> float:
        """Mark-to-market exit price in [0, 1] at ``exit_bar`` for a non-hold exit.

        Reads the quoted prob of the filled strike from the ladder (the value the
        position is worth if closed), oriented to the side actually held.
        ``is_over_side`` raises on an UNKNOWN label (audit defect C3).
        """
        bet_above = is_over_side(side)

        if panel.market == WINNER:
            prob_home = float(panel.mid[exit_bar])
            return prob_home if bet_above else (1.0 - prob_home)

        if strike is not None and panel.ladder:
            arr = panel.ladder.get(float(strike))
            if arr is not None:
                prob_above = float(np.asarray(arr, dtype=float)[exit_bar])
                return prob_above if bet_above else (1.0 - prob_above)

        # No ladder quote available: fall back to settlement value.
        return self._settlement_payoff(panel, side, strike)

    @staticmethod
    def _primary_team(panel: Panel, side: str) -> str:
        """The team actually bet (for concentration / parity checks).

        Over/long-home sides map to the home team; under/away to the away team.
        Market-agnostic labels (e.g. "over") still resolve to a concrete team so
        the gate's concentration check has a stable key. ``is_over_side`` raises
        on an UNKNOWN label (audit defect C3).
        """
        return panel.home_team if is_over_side(side) else panel.away_team
