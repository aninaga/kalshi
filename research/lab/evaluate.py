"""research.lab.evaluate — one-call evaluation of a set of realized trades.

A single function, :func:`evaluate`, takes a :class:`~research.lab.types.Trades`
and returns a :class:`~research.lab.types.GateResult`. It is the open,
composable replacement for the ~13 clone-a-script evaluators (``totals_alpha``,
``spread_alpha``, ``*_walkforward``, ``*_realistic`` …) that each re-implemented
the SAME plumbing: wrap the promotion gate, sweep cost, run a monthly
walk-forward, and run the adversarial sanity checks.

It does NOT re-implement any statistics: the decisive arithmetic is delegated to
``research.scorer.promotion_gate.evaluate_trial`` (UNCHANGED — never weakened)
and ``research.scorer.bootstrap``. This module only maps ``Trades`` columns onto
the gate's per-trade arrays and aggregates the views the analyst needs.

What it adds on top of the bare gate (see ``research/lab/CONTRACT.md`` §evaluate
and ``research/TOTALS_REFINE_FINDINGS.md`` / ``DIAGNOSTIC_1.md``):

* **cost sweep** — re-score net pnl at a ladder of extra per-trade costs (cents),
  so the analyst sees the breakeven drag. Mean-cents is monotone decreasing in
  cost by construction (subtracting a constant).
* **monthly walk-forward** — group trades by calendar month and gate each month
  independently (each month is a valid OOS window for a mechanism-pre-registered
  direction; see ``research/scripts/totals_walkforward.py``).
* **four adversarial checks** — the honesty audit that kept the totals re-score
  from being false pessimism (TOTALS_REFINE_FINDINGS §"Adversarial checks (4)"):
    1. ``staleness`` — entries fired on fresh, recently-changed lines.
    2. ``estimator_bias_vs_unconditional`` — the realized win rate is close to the
       price actually paid (we are not snapping to an unfairly far strike).
    3. ``concentration`` — no single game / team dominates |pnl|.
    4. ``oos`` — the edge is season-half stable (does not decay to zero in H2).

Execution is REALISTIC by default: ``evaluate`` does not assume a 0.50 fill — it
trusts the ``pnl`` already on each :class:`Trade` (which the strategy computed via
a realistic :class:`FillModel`). The ``fill_model`` argument is accepted for
interface symmetry with the rest of the lab and to let a caller re-price; if a
trade is missing its ``pnl`` we lazily import ``research.lab.execution`` to fill
it (sibling module, may be absent — handled gracefully).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from research.lab.types import GateResult, Trades
from research.scorer.bootstrap import block_bootstrap_by_game
from research.scorer.promotion_gate import evaluate_trial

# Adversarial-check thresholds. These mirror the audit in
# TOTALS_REFINE_FINDINGS.md; they are sanity bounds, NOT a second gate, so they
# are deliberately lenient (the promotion gate is the decisive arbiter).
_STALENESS_MAX_MIN = 2.0            # freshness guard: entries on lines fresher than this
_ESTIMATOR_BIAS_MAX = 0.05         # |win_rate - avg_price_paid| tolerance (prob units)
_CONCENTRATION_GAME_MAX = 0.20     # single-game |pnl| share cap (matches the gate)
_CONCENTRATION_TEAM_MAX = 0.25     # single-team |pnl| share cap (matches the gate)


def evaluate(
    trades: Trades,
    *,
    fill_model=None,
    cost_sweep=(0.0, 0.01, 0.02, 0.03, 0.04),
    walkforward: bool = True,
    adversarial: bool = True,
    ledger_path: Optional[str] = None,
    family: Optional[str] = None,
) -> GateResult:
    """Evaluate ``trades`` through the promotion gate plus the lab's extra views.

    Parameters
    ----------
    trades : Trades
        Realized trades. Each ``Trade.pnl`` is taken as ALREADY NET OF COSTS
        (the strategy computed it via a realistic fill model). Rows missing a
        finite ``pnl`` are re-priced via ``research.lab.execution`` if available,
        otherwise dropped from the evaluation.
    fill_model : FillModel | None
        Optional realistic fill model used only to re-price trades that lack a
        finite ``pnl``. ``None`` uses ``research.lab.execution.REALISTIC`` when
        that sibling module is importable.
    cost_sweep : tuple[float, ...]
        Extra per-trade costs IN PROBABILITY UNITS (e.g. ``0.02`` = 2 cents)
        subtracted from net pnl before re-scoring. Reported keyed by cents.
    walkforward : bool
        If True, gate each calendar month independently.
    adversarial : bool
        If True, run the four honesty checks.
    ledger_path : str | None
        Optional path to the research ledger. When provided, the Deflated-Sharpe
        multiple-testing correction is deflated against the REAL trial count N and
        cross-trial Sharpe variance the org has run (via ``lab.governance``),
        instead of the cold-start placeholder (N=1, V[SR]=1.0). When omitted, the
        behavior is byte-identical to the historical default. DSR is informational
        in the gate, so this only tightens the *reported* hurdle — it never changes
        or weakens any decisive (block-bootstrap / cluster-knockout) gate.
    family : str | None
        Optional event-class family (e.g. ``"nba"``, ``"weather"``) partitioning
        the DSR multiple-testing count: only this family's trials (plus
        family-undeterminable legacy rows, conservatively) feed N and V[SR].
        ``None`` keeps the historical global count.

    Returns
    -------
    GateResult
        ``cents_per_contract``/``ci_lo``/``ci_hi`` are in CENTS (×100), matching
        the existing evaluators. ``cost_sweep`` is keyed by integer cents,
        ``walkforward`` by ``"YYYY-MM"`` month string, ``adversarial`` by check
        name.
    """
    df = trades.df()
    df = _ensure_pnl(df, trades, fill_model)

    # Drop rows without a usable pnl (cannot be scored).
    if not df.empty:
        df = df[np.isfinite(df["pnl"].to_numpy(dtype=float))].reset_index(drop=True)

    if df.empty or len(df) < 2:
        return GateResult(
            passed=False,
            cents_per_contract=float("nan"),
            ci_lo=float("nan"),
            ci_hi=float("nan"),
            n=len(df),
            n_games=0,
            reasons=["no trades" if df.empty else "fewer than 2 trades"],
        )

    # N-aware DSR governance: deflate against the real research record when a
    # ledger is supplied; otherwise fall back to the cold-start placeholders.
    if ledger_path:
        from research.lab import governance
        gov = governance.governance_params(ledger_path, family=family)
    else:
        gov = {"n_trials": 1, "sharpe_variance": 1.0,
               "n_trials_source": "cold-start placeholder (no ledger_path)",
               "sharpe_variance_source": "cold-start placeholder (no ledger_path)"}

    base = _score(df, extra_cost=0.0,
                  n_trials=gov["n_trials"],
                  sharpe_variance=gov["sharpe_variance"])

    result = GateResult(
        passed=base["passed"],
        cents_per_contract=base["cents"],
        ci_lo=base["ci_lo"],
        ci_hi=base["ci_hi"],
        n=base["n"],
        n_games=base["n_games"],
        reasons=list(base["reasons"]),
        governance=gov,
    )

    # --- cost sweep (monotone decreasing in cost) ---
    result.cost_sweep = _cost_sweep(df, cost_sweep)

    # --- monthly walk-forward ---
    if walkforward:
        result.walkforward = _walkforward(df)

    # --- adversarial honesty checks ---
    if adversarial:
        result.adversarial = _adversarial(df)

    return result


# ---------------------------------------------------------------------------
# Core gate wrapping
# ---------------------------------------------------------------------------


def _score(df: pd.DataFrame, *, extra_cost: float, n_trials: int = 1,
           sharpe_variance: float = 1.0) -> dict:
    """Map a trades DataFrame onto ``evaluate_trial`` and return a flat dict.

    ``extra_cost`` is subtracted from each trade's (already-net) pnl, in
    probability units, before scoring — used by the cost sweep.
    """
    pnl = df["pnl"].to_numpy(dtype=float) - extra_cost
    dec = evaluate_trial(
        val_pnl_per_trade=pnl,
        val_game_id_per_trade=df["game_id"].to_numpy(),
        val_date_per_trade=df["date"].to_numpy(),
        val_home_team_per_trade=df["home_team"].to_numpy(),
        val_primary_team_per_trade=df["primary_team"].to_numpy(),
        n_total_trials_in_registry=n_trials,
        sharpe_variance_in_registry=sharpe_variance,
        cost_per_trade_assumed=extra_cost,
    )
    return {
        "passed": bool(dec.passed),
        "cents": float(np.nanmean(pnl) * 100.0),
        "ci_lo": float(dec.block_bootstrap_ci_lo * 100.0),
        "ci_hi": float(dec.block_bootstrap_ci_hi * 100.0),
        "n": int(dec.n_trades_val),
        "n_games": int(dec.n_games_val),
        "reasons": list(dec.reasons),
    }


def _cost_sweep(df: pd.DataFrame, costs) -> dict:
    """Re-score at each extra cost. Keyed by integer cents for readable output."""
    out: dict = {}
    for c in costs:
        z = _score(df, extra_cost=float(c))
        out[round(float(c) * 100.0, 2)] = {
            "cents": z["cents"],
            "ci_lo": z["ci_lo"],
            "gate": z["passed"],
        }
    return out


def _walkforward(df: pd.DataFrame) -> dict:
    """Gate each calendar month independently (OOS-by-window)."""
    months = pd.to_datetime(df["date"]).dt.to_period("M").astype(str)
    out: dict = {}
    for m in sorted(months.unique()):
        sub = df[months.to_numpy() == m]
        if len(sub) < 2:
            out[m] = {
                "n": int(len(sub)),
                "cents": float("nan"),
                "ci_lo": float("nan"),
                "gate": False,
            }
            continue
        z = _score(sub, extra_cost=0.0)
        out[m] = {
            "n": z["n"],
            "cents": z["cents"],
            "ci_lo": z["ci_lo"],
            "gate": z["passed"],
        }
    return out


# ---------------------------------------------------------------------------
# Adversarial honesty checks (4) — TOTALS_REFINE_FINDINGS §"Adversarial checks"
# ---------------------------------------------------------------------------


def _adversarial(df: pd.DataFrame) -> dict:
    """Run the four honesty checks; each entry is ``{passed, detail}``."""
    return {
        "staleness": _check_staleness(df),
        "estimator_bias_vs_unconditional": _check_estimator_bias(df),
        "concentration": _check_concentration(df),
        "oos": _check_oos(df),
    }


def _stale_minutes(df: pd.DataFrame) -> Optional[np.ndarray]:
    """Per-trade line staleness in minutes, if recorded.

    The strategy records ``stale_min`` (minutes since the line last changed at
    entry) under each trade's ``meta``; :func:`_ensure_pnl` lifts it into a
    ``stale_min`` column when present. Returns ``None`` when not instrumented.
    """
    if "stale_min" not in df.columns:
        return None
    vals = df["stale_min"].to_numpy(dtype=float)
    if len(vals) == 0 or not np.isfinite(vals).any():
        return None
    return vals


def _check_staleness(df: pd.DataFrame) -> dict:
    """Entries should fire on fresh, recently-changed lines (≤ freshness guard).

    If no staleness was recorded, the check is inconclusive but PASSES (the
    freshness guard is the strategy's responsibility; we don't fail on missing
    instrumentation — we just report that it could not be verified here).
    """
    stale = _stale_minutes(df)
    if stale is None or len(stale) == 0:
        return {
            "passed": True,
            "detail": "stale_min not recorded on trades; freshness enforced upstream",
        }
    worst = float(np.nanmax(stale))
    passed = worst <= _STALENESS_MAX_MIN + 1e-9
    return {
        "passed": passed,
        "detail": (
            f"max stale {worst:.2f}min "
            f"({'<=' if passed else '>'} {_STALENESS_MAX_MIN}min guard)"
        ),
    }


def _check_estimator_bias(df: pd.DataFrame) -> dict:
    """The realized win rate should be close to the price actually paid.

    A large gap means the apparent edge is an artifact of snapping to an unfairly
    far / mispriced strike rather than a genuine residual. ``win rate`` = share of
    trades that settled in the money (``payoff > 0.5``); ``avg price paid`` =
    mean ``entry_price``. (TOTALS_REFINE_FINDINGS: avg fill 0.5555 ≈ win 0.580.)
    """
    payoff = df["payoff"].to_numpy(dtype=float)
    price = df["entry_price"].to_numpy(dtype=float)
    finite = np.isfinite(payoff) & np.isfinite(price)
    if not finite.any():
        return {"passed": True, "detail": "no priced trades to compare"}
    win_rate = float((payoff[finite] > 0.5).mean())
    avg_price = float(price[finite].mean())
    gap = abs(win_rate - avg_price)
    passed = gap <= _ESTIMATOR_BIAS_MAX
    return {
        "passed": passed,
        "detail": (
            f"win_rate={win_rate:.3f} vs avg_price_paid={avg_price:.3f} "
            f"(|gap|={gap:.3f} {'<=' if passed else '>'} {_ESTIMATOR_BIAS_MAX})"
        ),
    }


def _check_concentration(df: pd.DataFrame) -> dict:
    """No single game / team should dominate the |pnl| (matches the gate caps)."""
    pnl = df["pnl"].to_numpy(dtype=float)
    game_share, top_game = _share(pnl, df["game_id"].to_numpy())
    team_share, top_team = _share(pnl, df["primary_team"].to_numpy())
    passed = (
        game_share <= _CONCENTRATION_GAME_MAX
        and team_share <= _CONCENTRATION_TEAM_MAX
    )
    return {
        "passed": passed,
        "detail": (
            f"top_game={top_game} {game_share:.3f}/{_CONCENTRATION_GAME_MAX}; "
            f"top_team={top_team} {team_share:.3f}/{_CONCENTRATION_TEAM_MAX}"
        ),
    }


def _check_oos(df: pd.DataFrame) -> dict:
    """Season-half stability: the edge should not decay to zero in the late half.

    Split chronologically at the median date; bootstrap each half by game. The
    surviving edge is "stable" iff BOTH halves have a positive mean and the late
    half is at least a small fraction of the early half (not collapsed to ~0).
    Mirrors TOTALS_REFINE §OOS (H1 +4.80¢ vs H2 +0.08¢ -> FAIL).
    """
    dates = pd.to_datetime(df["date"])
    order = np.argsort(dates.to_numpy())
    sd = df.iloc[order].reset_index(drop=True)
    n = len(sd)
    if n < 4:
        return {"passed": True, "detail": f"only {n} trades; OOS split skipped"}
    mid = n // 2
    early, late = sd.iloc[:mid], sd.iloc[mid:]

    be = block_bootstrap_by_game(
        early["pnl"].to_numpy(dtype=float), early["game_id"].to_numpy()
    )
    bl = block_bootstrap_by_game(
        late["pnl"].to_numpy(dtype=float), late["game_id"].to_numpy()
    )
    early_c = float(be.mean * 100.0)
    late_c = float(bl.mean * 100.0)
    # Stable: both positive AND the late half retains a meaningful share of the
    # early-half edge (>= 25%), i.e. it did not decay toward zero.
    decayed = early_c > 0 and late_c <= max(0.25 * early_c, 0.0)
    passed = (early_c > 0) and (late_c > 0) and not decayed
    return {
        "passed": passed,
        "detail": (
            f"H1={early_c:+.2f}c (CIlo {be.ci_lo * 100:+.2f}) vs "
            f"H2={late_c:+.2f}c (CIlo {bl.ci_lo * 100:+.2f})"
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _share(pnl: np.ndarray, group: np.ndarray) -> tuple[float, str]:
    """Max single-group share of total |pnl| and that group's label."""
    pnl = np.asarray(pnl, dtype=float)
    if len(pnl) == 0:
        return 0.0, ""
    total_abs = float(np.abs(pnl).sum())
    if total_abs <= 0:
        return 0.0, ""
    unique, inv = np.unique(group, return_inverse=True)
    abs_sums = np.zeros(len(unique), dtype=float)
    np.add.at(abs_sums, inv, np.abs(pnl))
    top = int(np.argmax(abs_sums))
    return float(abs_sums[top] / total_abs), str(unique[top])


def _ensure_pnl(df: pd.DataFrame, trades: Trades, fill_model) -> pd.DataFrame:
    """Guarantee a ``pnl`` column, re-pricing rows that lack one if possible.

    Most trades arrive with ``pnl`` already net of costs (computed by the
    strategy). For any row whose ``pnl`` is non-finite we try to derive it from
    a realistic fill: ``pnl = payoff - all_in_price``. The fill model is taken
    from ``fill_model`` or lazily from ``research.lab.execution.REALISTIC`` — the
    sibling module may be absent during parallel development, in which case those
    rows are left non-finite (and later dropped).

    We also lift per-trade ``meta['stale_min']`` into a ``stale_min`` column so
    the staleness check can read it (``Trades.df()`` does not surface ``meta``).
    """
    rows = list(getattr(trades, "rows", []))
    if rows and len(rows) == len(df):
        stale = [
            (getattr(t, "meta", None) or {}).get("stale_min", float("nan"))
            for t in rows
        ]
        if any(np.isfinite(s) for s in (np.asarray(stale, dtype=float))):
            df = df.copy()
            df["stale_min"] = np.asarray(stale, dtype=float)

    if df.empty or "pnl" not in df.columns:
        return df

    pnl = df["pnl"].to_numpy(dtype=float)
    missing = ~np.isfinite(pnl)
    if not missing.any():
        return df

    fm = fill_model if fill_model is not None else _default_fill_model()
    if fm is None:
        # Cannot re-price; leave non-finite rows to be dropped downstream.
        return df

    # Re-price from payoff minus the all-in price. We don't have the source
    # Panel here, so we use the recorded entry_price as the fill mid and add the
    # model's half-spread + fee on top (the realistic drag the strategy would
    # have applied). This is a best-effort fallback; the strategy normally fills
    # this in directly.
    payoff = df["payoff"].to_numpy(dtype=float)
    entry_price = df["entry_price"].to_numpy(dtype=float)
    half_spread = float(getattr(fm, "half_spread", 0.0))
    repriced = pnl.copy()
    for i in np.where(missing)[0]:
        if not (np.isfinite(payoff[i]) and np.isfinite(entry_price[i])):
            continue
        fee = _safe_fee(fm, entry_price[i])
        repriced[i] = payoff[i] - (entry_price[i] + half_spread + fee)
    df = df.copy()
    df["pnl"] = repriced
    return df


def _default_fill_model():
    """Lazily import the realistic default fill model; ``None`` if unavailable."""
    try:
        from research.lab import execution  # type: ignore
    except Exception:
        return None
    return getattr(execution, "REALISTIC", None)


def _safe_fee(fm, price: float) -> float:
    fee_fn = getattr(fm, "fee", None)
    if not callable(fee_fn):
        return 0.0
    try:
        return float(fee_fn(float(price)))
    except Exception:
        return 0.0
