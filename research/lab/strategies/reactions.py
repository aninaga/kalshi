"""research.lab.strategies.reactions — the REACTION family as lab Strategies.

This module ports four hand-cloned evaluator scripts into composable
``lab.strategy.Strategy`` objects so they run through the substrate's shared
realistic execution + promotion gate instead of bespoke plumbing:

  * ``substitution_latency()`` — port of ``research/scripts/sub_alpha.py``.
    Mechanism: when a lineup changes (star/starter out or in) the book re-rates on
    the scoreboard before fully marking the lineup change, so the team's win-prob
    contract keeps drifting in the fair direction (out -> down, in -> up). Bet
    WITH ("follow") or AGAINST ("fade") that drift.
  * ``fair_value_gap()`` — port of ``research/scripts/fairvalue_alpha.py``.
    Mechanism: fit an empirical fair P(home win | margin, elapsed) surface on
    train; trade reversion when the market deviates from it (rich -> short home,
    cheap -> long home).
  * ``term_structure_rv()`` — port of ``research/scripts/term_structure_alpha.py``.
    Mechanism: a random-walk model ties margin + total-implied remaining pace to a
    model win prob ``mwp = Phi(M / (k*sqrt(R)))``; trade the winner leg when it
    disagrees with the model (model says leader underpriced -> buy leader).
  * ``closing_line_value()`` — port of ``research/scripts/clv_alpha.py``.
    Mechanism: early-pregame drift (p_obs - p_open) should CONTINUE and predict
    the settled outcome; rising home -> buy home.

ALL FOUR ARE DEAD — kept here only as regression demos of the lab substrate, not
as tradeable edges:

  * substitution-latency: the residual under-reaction is real but ~+0.2c/contract,
    an order of magnitude below the cost floor. EFFICIENT / DEAD.
    (``research/ALPHA_FINDINGS.md`` §1.)
  * fair-value-gap: the market's deviations from score+clock do NOT revert (it
    knows more than margin/time); dead in AND out of sample.
    (``research/ALPHA_FINDINGS.md`` §2.)
  * term-structure RV: the total leg adds ~0 win-relevant info beyond the margin
    (corr(pace_only, resid)=0.011); inverts out-of-sample. DEAD.
    (``research/TERM_STRUCTURE_FINDINGS.md``.)
  * closing-line-value: positive in-sample, INVERTS out-of-sample (+3.2c train ->
    -2.7c val), ~0c on the full population. DEAD.
    (``research/CLV_FINDINGS.md``.)

Conventions (per ``research/lab/CONTRACT.md``):
  * Shared dataclasses are imported from ``research.lab.types``.
  * Sibling lab modules (``lab.strategy``, ``lab.signals``) are imported LAZILY
    inside the factory functions, because they may not be merged in this worktree.
  * Entry/side are pure functions over a ``Panel`` (numpy); realistic execution is
    the lab default (never a 0.50 fill) — these factories never set an entry price.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from research.lab.types import Panel

# --------------------------------------------------------------------------- #
# Small numeric helpers (inline so this module is standalone-importable).
# --------------------------------------------------------------------------- #

# Margin buckets, copied verbatim from fairvalue_alpha.py so the ported
# mechanism is faithful in its bucketing. (Freshness is enforced by the shared
# Strategy.max_stale_min guard, so no staleness helper is reimplemented here.)
_MARGIN_BINS = np.array([-100, -20, -15, -11, -8, -5, -3, -1, 1, 3, 5, 8, 11, 15, 20, 100])


def _fair_wp(panel: Panel) -> np.ndarray:
    """Fair P(home win) per minute, bucketed by current margin.

    A self-contained, panel-local proxy for the train-fit fair surface from
    fairvalue_alpha.py: a monotone map from the margin bucket to a fair win prob
    (more home lead -> higher fair WP). The production strategy fits this surface
    on TRAIN via ``lab.signals`` / ``lab.data``; it is computed inline here so the
    DEAD mechanism is testable without real data and without reading the test split.
    """
    margin = np.asarray(panel.margin, float)
    mi = np.clip(np.digitize(margin, _MARGIN_BINS) - 1, 0, len(_MARGIN_BINS) - 2)
    frac = mi / (len(_MARGIN_BINS) - 2)          # 0 (deep away lead) .. 1 (deep home lead)
    return np.clip(0.02 + 0.96 * frac, 0.02, 0.98)


# --------------------------------------------------------------------------- #
# Factory functions — each returns a lab.strategy.Strategy.
# --------------------------------------------------------------------------- #

def fair_value_gap(
    *,
    gap_thresh: float = 0.06,
    min_elapsed: float = 360.0,
    max_elapsed: float = 2520.0,
    max_stale_min: float = 2.0,
    name: str = "reactions.fair_value_gap",
):
    """Fair-value-gap mean-reversion on the WINNER contract (DEAD — efficient).

    Port of ``research/scripts/fairvalue_alpha.py``. ``gap_t = market_wp - fair_wp``;
    when the market is too high on the home team (``gap >= +gap_thresh``) we bet
    reversion DOWN (short home); too low (``gap <= -gap_thresh``) -> long home.

    DEAD: the market's deviations from score+clock do NOT revert (it prices
    lineups/fouls/momentum beyond margin & time). +0.04c train / -0.15c val gross,
    gate FAIL in and out of sample. Kept as a substrate regression demo.
    """
    from research.lab.strategy import Strategy  # lazy: sibling may be unmerged

    def _gap(panel: Panel) -> np.ndarray:
        market = np.asarray(panel.mid, float)
        fair = _fair_wp(panel)
        return market - fair

    def entry(panel: Panel) -> np.ndarray:
        gap = _gap(panel)
        return np.isfinite(gap) & (np.abs(gap) >= gap_thresh)

    def side(panel: Panel, i: int) -> str:
        gap = _gap(panel)
        # market too high on home -> revert down -> short home; else long home.
        return "short_home" if gap[i] >= gap_thresh else "long_home"

    return Strategy(
        name=name, entry=entry, side=side,
        min_elapsed=min_elapsed, max_elapsed=max_elapsed, max_stale_min=max_stale_min,
    )


def term_structure_rv(
    *,
    tau: float = 0.05,
    k: float = 1.30,
    min_elapsed: float = 600.0,
    max_elapsed: float = 2520.0,
    max_stale_min: float = 2.0,
    name: str = "reactions.term_structure_rv",
):
    """Winner<->total term-structure relative value on the WINNER leg (DEAD).

    Port of ``research/scripts/term_structure_alpha.py``. Random-walk model win
    prob ``mwp = Phi(M / (k*sqrt(R)))`` with ``R`` the implied remaining scoring
    (read from the total leg via ``panel.features['rem_pts']`` when present, else a
    flat fallback). Signal ``gap = mwp - market_wp``: ``gap > +tau`` => model says
    the leader is underpriced => BUY leader (long home); ``gap < -tau`` => short.

    DEAD: the total leg adds ~0 win-relevant info beyond the margin trajectory
    (corr(pace_only, resid)=0.011); the winner book is better calibrated than the
    model (log-loss 0.440 vs 0.516) and the signal INVERTS out-of-sample
    (+1.26c train -> -1.56c val). Kept as a substrate regression demo.
    """
    from research.lab.strategy import Strategy  # lazy: sibling may be unmerged

    def _norm_cdf(x: np.ndarray) -> np.ndarray:
        # standard-normal CDF without a scipy dependency (erf via numpy).
        try:
            from scipy.stats import norm  # type: ignore
            return norm.cdf(x)
        except Exception:  # noqa: BLE001 - scipy optional in some worktrees
            from math import erf, sqrt
            vec = np.vectorize(lambda v: 0.5 * (1.0 + erf(v / sqrt(2.0))))
            return vec(np.asarray(x, float))

    def _gap(panel: Panel) -> np.ndarray:
        margin = np.asarray(panel.margin, float)
        # remaining implied scoring: prefer the joined total leg if available.
        rem = panel.features.get("rem_pts")
        if rem is None:
            rem = np.full(len(margin), 80.0)
        rem = np.clip(np.asarray(rem, float), 1.0, None)
        mwp = np.clip(_norm_cdf(margin / (k * np.sqrt(rem))), 1e-4, 1 - 1e-4)
        market = np.asarray(panel.mid, float)
        return mwp - market

    def entry(panel: Panel) -> np.ndarray:
        gap = _gap(panel)
        return np.isfinite(gap) & (np.abs(gap) >= tau)

    def side(panel: Panel, i: int) -> str:
        gap = _gap(panel)
        # gap > 0: model says home/leader underpriced -> buy home.
        return "long_home" if gap[i] > 0 else "short_home"

    return Strategy(
        name=name, entry=entry, side=side,
        min_elapsed=min_elapsed, max_elapsed=max_elapsed, max_stale_min=max_stale_min,
    )


def closing_line_value(
    *,
    thresh: float = 0.02,
    side_mode: str = "continuation",
    min_elapsed: float = 0.0,
    max_elapsed: float = 2880.0,
    max_stale_min: float = 1e9,
    name: str = "reactions.closing_line_value",
):
    """Pre-game closing-line-value drift continuation on the WINNER leg (DEAD).

    Port of ``research/scripts/clv_alpha.py`` (a PRE-GAME regime). ``drift =
    p_obs - p_open`` where ``p_open`` is the first observed mid and ``p_obs`` the
    running mid; fires when ``|drift| >= thresh``. ``side_mode='continuation'``:
    drift>0 (home rising) => buy home; ``'reversion'`` is the diagnostic
    complement. Here ``panel.mid`` is the pregame mid series and elapsed indexes
    the pregame timeline; staleness/elapsed guards are relaxed accordingly.

    DEAD: positive in-sample, INVERTS out-of-sample (+3.16c train -> -2.70c val at
    0c), ~0c on the full ex-test population (+0.13c at 2c, CI lo -3.05c), gate FAIL
    on every cut and observation window. Kept as a substrate regression demo.
    """
    if side_mode not in ("continuation", "reversion"):
        raise ValueError(f"side_mode must be 'continuation' or 'reversion', got {side_mode!r}")
    from research.lab.strategy import Strategy  # lazy: sibling may be unmerged

    def _drift(panel: Panel) -> np.ndarray:
        mid = np.asarray(panel.mid, float)
        if len(mid) == 0:
            return mid
        finite = mid[np.isfinite(mid)]
        p_open = float(finite[0]) if len(finite) else np.nan
        return mid - p_open

    def entry(panel: Panel) -> np.ndarray:
        drift = _drift(panel)
        return np.isfinite(drift) & (np.abs(drift) >= thresh)

    def side(panel: Panel, i: int) -> str:
        drift = _drift(panel)
        home_rising = drift[i] > 0
        long_home = home_rising if side_mode == "continuation" else (not home_rising)
        return "long_home" if long_home else "short_home"

    return Strategy(
        name=name, entry=entry, side=side,
        min_elapsed=min_elapsed, max_elapsed=max_elapsed, max_stale_min=max_stale_min,
    )


def substitution_latency(
    *,
    sub_feature: str = "sub_signed",
    side_mode: str = "follow",
    min_elapsed: float = 0.0,
    max_elapsed: float = 2760.0,
    max_stale_min: float = 2.0,
    name: str = "reactions.substitution_latency",
):
    """Substitution-latency drift on the WINNER contract (DEAD — efficient).

    Port of ``research/scripts/sub_alpha.py``. A substitution event is carried in
    ``panel.features[sub_feature]`` as a signed marker on the bar of the sub: ``>0``
    for a fair-value-UP event (star/starter IN for the home side), ``<0`` for a
    fair-value-DOWN event (star/starter OUT), ``0`` otherwise. ``side_mode='follow'``
    bets the book UNDER-reacts (trade WITH the fair direction); ``'fade'`` bets it
    OVER-reacts.

    DEAD: the score-removed residual under-reaction is statistically real but
    ~+0.2c/contract gross — an order of magnitude below the ~2c cost floor, and
    net-negative after costs. EFFICIENT / DEAD. Kept as a substrate regression demo.
    """
    if side_mode not in ("follow", "fade"):
        raise ValueError(f"side_mode must be 'follow' or 'fade', got {side_mode!r}")
    from research.lab.strategy import Strategy  # lazy: sibling may be unmerged

    def _sub(panel: Panel) -> np.ndarray:
        s = panel.features.get(sub_feature)
        if s is None:
            return np.zeros(panel.n)
        return np.asarray(s, float)

    def entry(panel: Panel) -> np.ndarray:
        s = _sub(panel)
        return np.isfinite(s) & (s != 0.0)

    def side(panel: Panel, i: int) -> str:
        s = _sub(panel)
        d_fair = 1.0 if s[i] > 0 else -1.0          # in -> up, out -> down
        pos = d_fair if side_mode == "follow" else -d_fair
        return "long_home" if pos > 0 else "short_home"

    return Strategy(
        name=name, entry=entry, side=side,
        min_elapsed=min_elapsed, max_elapsed=max_elapsed, max_stale_min=max_stale_min,
    )


# Convenience registry: the dead reaction family, for the lab/CLI to enumerate.
REACTIONS: dict[str, Callable[..., object]] = {
    "fair_value_gap": fair_value_gap,
    "term_structure_rv": term_structure_rv,
    "closing_line_value": closing_line_value,
    "substitution_latency": substitution_latency,
}

__all__ = [
    "fair_value_gap",
    "term_structure_rv",
    "closing_line_value",
    "substitution_latency",
    "REACTIONS",
]
