"""research.lab.strategies.calibration — the price-LEVEL bias family as Strategies.

This module re-expresses the LEVEL-BIAS research family (calibration error /
favorite-longshot / totals-extremes) as composable ``lab.strategy.Strategy``
objects, ported from the original one-off scripts
(``research/scripts/calib_alpha.py`` and ``totals_extremes_alpha.py``).

.. warning::
    **These directions are DEAD.** They are kept ONLY as regression demos of the
    lab substrate — proof that the original mechanism still expresses cleanly as a
    ``Strategy`` — NOT as tradeable edges.

    The most documented inefficiency in betting markets is the favorite-longshot
    bias: longshots overpriced, favorites underpriced, so realized settlement
    rates deviate from the implied PRICE. In sample the bias looks real (home
    teams priced 10-20% won ~18.5%, +3.6pp). **Out of sample it inverts:** the
    per-bin bias flipped sign train->val in 7 of 10 bins, val PnL went to
    -3.83c/ct, and totals-extremes never cleared the ~2-4c realistic cost floor.
    The apparent edge was sampling noise, not a stable mispricing. See
    ``research/ALPHA_FINDINGS.md`` (#3 Calibration / favorite-longshot).

Mechanism (preserved from the originals):
  * **favorite_longshot** — fire at the FIRST in-game bar where the implied price
    enters an extreme tail. A *longshot* (price < ``p_lo``) is presumed
    OVERPRICED, so fade it; a *near-lock* (price > ``p_hi``) is presumed
    UNDERPRICED, so back it. Hold to settlement.
  * **calibration_bucket** — fire at the FIRST bar where the implied price lands
    in a price BUCKET pre-flagged as mispriced beyond ``edge_thresh`` (sign of
    the calibration error ``realized_rate - price`` fixes the side). Hold to
    settlement.

Both operate on a price LEVEL in [0,1]: ``panel.mid`` for the WINNER market
(implied home win prob), or the at-the-money ladder probability for TOTAL /
SPREAD (implied P(over)). Realistic execution (``lab.execution.REALISTIC``) is
the default fill model in ``Strategy.run`` — never a 0.50 fill.

Sibling lab modules (``lab.strategy``, ``lab.signals``) are imported LAZILY
inside the factory functions so this module loads in an isolated worktree.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from research.lab.types import Panel, WINNER

# Default price bins for the bucket-calibration fit (matches calib_alpha.py).
DEFAULT_PRICE_BINS: tuple[float, ...] = (
    0.0, 0.05, 0.10, 0.20, 0.35, 0.5, 0.65, 0.80, 0.90, 0.95, 1.0,
)


# --------------------------------------------------------------------------- #
# Price-level extraction (lazy on lab.signals; safe inline fallback).
# --------------------------------------------------------------------------- #
def _price_level(panel: Panel) -> np.ndarray:
    """The implied PRICE in [0,1] this family bets on, per minute.

    WINNER: ``panel.mid`` is already P(home win) in [0,1]. TOTAL / SPREAD: the
    mid is a points/margin LEVEL, not a price, so we read the at-the-money
    contract price off the ladder (the strike nearest the mid). Uses
    ``lab.signals.implied_level`` when present (it returns ``panel.mid``), else
    falls back to ``panel.mid`` inline so this works standalone.
    """
    if panel.market == WINNER:
        try:
            from research.lab import signals as _signals  # lazy sibling import

            return np.asarray(_signals.implied_level(panel), dtype=float)
        except Exception:  # noqa: BLE001 — sibling may be absent in a worktree
            return np.asarray(panel.mid, dtype=float)

    # TOTAL / SPREAD: pick the ladder strike nearest the implied level per bar.
    ladder = panel.ladder
    if not ladder:
        return np.asarray(panel.mid, dtype=float)
    strikes = np.array(sorted(ladder.keys()), dtype=float)
    probs = np.vstack([np.asarray(ladder[s], dtype=float) for s in strikes])  # (S, n)
    mid = np.asarray(panel.mid, dtype=float)
    # nearest strike per bar, then read that strike's price.
    nearest = np.abs(strikes[:, None] - mid[None, :]).argmin(axis=0)  # (n,)
    return probs[nearest, np.arange(probs.shape[1])]


def _time_mask(panel: Panel, min_elapsed: float, max_elapsed: float) -> np.ndarray:
    e = np.asarray(panel.elapsed_sec, dtype=float)
    return np.isfinite(e) & (e >= min_elapsed) & (e <= max_elapsed)


def _first_only(mask: np.ndarray) -> np.ndarray:
    """Keep only the FIRST True (one trade per game; matches the originals)."""
    out = np.zeros_like(mask, dtype=bool)
    hits = np.flatnonzero(mask)
    if hits.size:
        out[hits[0]] = True
    return out


# --------------------------------------------------------------------------- #
# Factory 1 — favorite / longshot price-extreme bias.
# --------------------------------------------------------------------------- #
def favorite_longshot(
    market: str = WINNER,
    *,
    p_lo: float = 0.10,
    p_hi: float = 0.90,
    min_elapsed: float = 600.0,
    max_elapsed: float = 2520.0,
    name: Optional[str] = None,
):
    """Strategy: fade longshots / back near-locks at extreme implied PRICES.

    Ports ``totals_extremes_alpha.build_trades`` / ``calib_alpha`` to the
    substrate. Entry fires at the FIRST in-game bar (within the elapsed window)
    where the implied price is extreme:

      * ``price < p_lo``  -> *longshot*, presumed OVERPRICED  -> FADE it.
      * ``price > p_hi``  -> *near-lock*, presumed UNDERPRICED -> BACK it.

    Side labels follow the market:

      * WINNER: back -> ``"long_home"``, fade -> ``"long_away"``.
      * TOTAL / SPREAD: back -> ``"over"``, fade -> ``"under"``.

    .. warning::
        DEAD direction (inverts OOS); kept as a substrate regression demo only.
        See module docstring / ``research/ALPHA_FINDINGS.md``.

    Returns a ``lab.strategy.Strategy`` (sibling imported lazily).
    """
    if not 0.0 < p_lo < p_hi < 1.0:
        raise ValueError(f"need 0 < p_lo < p_hi < 1, got p_lo={p_lo}, p_hi={p_hi}")

    back_label = "long_home" if market == WINNER else "over"
    fade_label = "long_away" if market == WINNER else "under"

    def entry(panel: Panel) -> np.ndarray:
        price = _price_level(panel)
        ok = _time_mask(panel, min_elapsed, max_elapsed) & np.isfinite(price)
        extreme = ok & ((price < p_lo) | (price > p_hi))
        return _first_only(extreme)

    def side(panel: Panel, i: int) -> str:
        price = _price_level(panel)
        # near-lock (high price) -> back; longshot (low price) -> fade.
        return back_label if price[i] > p_hi else fade_label

    from research.lab.strategy import Strategy  # lazy sibling import

    return Strategy(
        name=name or f"favorite_longshot[{market},{p_lo:g}/{p_hi:g}]",
        entry=entry,
        side=side,
        exit="settlement",
        min_elapsed=min_elapsed,
        max_elapsed=max_elapsed,
    )


# --------------------------------------------------------------------------- #
# Factory 2 — price-bucket calibration bias.
# --------------------------------------------------------------------------- #
def calibration_bucket(
    market: str = WINNER,
    *,
    bias: Optional[Sequence[float]] = None,
    price_bins: Sequence[float] = DEFAULT_PRICE_BINS,
    edge_thresh: float = 0.03,
    min_elapsed: float = 600.0,
    max_elapsed: float = 2520.0,
    name: Optional[str] = None,
):
    """Strategy: trade pre-flagged mispriced price BUCKETS to settlement.

    Ports ``calib_alpha.fit_bias`` / ``build_trades`` (and the ``p3_bucket_sweep``
    diagnostic) to the substrate. ``bias[b]`` is the pre-registered calibration
    error ``realized_rate - mean_price`` for price bucket ``b`` (typically fit on
    TRAIN only). Entry fires at the FIRST bar (within the elapsed window) whose
    implied price falls in a bucket with ``abs(bias[b]) >= edge_thresh``; the
    SIGN of that bias fixes the side:

      * ``bias > 0`` (UNDERpriced) -> BACK  (WINNER ``"long_home"`` / else ``"over"``).
      * ``bias < 0`` (OVERpriced)  -> FADE  (WINNER ``"long_away"`` / else ``"under"``).

    If ``bias`` is omitted, the favorite-longshot prior is used: the lowest
    bucket(s) are overpriced (bias < 0) and the highest are underpriced
    (bias > 0), so this reduces to the favorite-longshot direction by bucket.

    .. warning::
        DEAD direction. The per-bucket bias is an in-sample mirage that flipped
        sign train->val in 7/10 buckets and inverted to -3.83c/ct OOS. Kept as a
        substrate regression demo only; see ``research/ALPHA_FINDINGS.md``.

    Returns a ``lab.strategy.Strategy`` (sibling imported lazily).
    """
    bins = np.asarray(price_bins, dtype=float)
    if bins.ndim != 1 or bins.size < 2 or not np.all(np.diff(bins) > 0):
        raise ValueError("price_bins must be strictly increasing with >=2 edges")
    n_buckets = bins.size - 1

    if bias is None:
        # Favorite-longshot prior: extremes mispriced, middle calibrated.
        bias_arr = np.zeros(n_buckets, dtype=float)
        bias_arr[0] = -edge_thresh            # cheapest bucket overpriced
        bias_arr[-1] = +edge_thresh           # priciest bucket underpriced
    else:
        bias_arr = np.asarray(bias, dtype=float)
        if bias_arr.shape != (n_buckets,):
            raise ValueError(
                f"bias must have len {n_buckets} (n_bins-1), got {bias_arr.shape}"
            )

    flagged = np.abs(np.nan_to_num(bias_arr, nan=0.0)) >= edge_thresh
    back_label = "long_home" if market == WINNER else "over"
    fade_label = "long_away" if market == WINNER else "under"

    def _bucket(price: np.ndarray) -> np.ndarray:
        return np.clip(np.digitize(price, bins) - 1, 0, n_buckets - 1)

    def entry(panel: Panel) -> np.ndarray:
        price = _price_level(panel)
        ok = _time_mask(panel, min_elapsed, max_elapsed) & np.isfinite(price)
        b = _bucket(price)
        fires = ok & flagged[b]
        return _first_only(fires)

    def side(panel: Panel, i: int) -> str:
        price = _price_level(panel)
        b = int(_bucket(np.asarray([price[i]]))[0])
        return back_label if bias_arr[b] > 0 else fade_label

    from research.lab.strategy import Strategy  # lazy sibling import

    return Strategy(
        name=name or f"calibration_bucket[{market},thr={edge_thresh:g}]",
        entry=entry,
        side=side,
        exit="settlement",
        min_elapsed=min_elapsed,
        max_elapsed=max_elapsed,
    )


__all__ = ["favorite_longshot", "calibration_bucket", "DEFAULT_PRICE_BINS"]
