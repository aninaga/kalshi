"""Deflated Sharpe Ratio (López de Prado 2014).

Reference: López de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting
for Selection Bias, Backtest Overfitting, and Non-Normality." Journal of
Portfolio Management, 40(5), 94–107.

The DSR adjusts an observed Sharpe ratio for (a) the number of trials that
were attempted (multiple-testing / selection bias), (b) non-normality of the
return distribution (skewness, excess kurtosis), and (c) the finite-sample
uncertainty in the Sharpe estimate.

In this project DSR is **informational**, not decisive: the promotion gate's
real teeth are block-bootstrap-by-game and cluster knockouts. DSR is reported
to provide a multiple-testing correction sanity check.
"""
from __future__ import annotations

import math

from scipy.stats import norm


_EULER_GAMMA = 0.5772156649015329  # Euler–Mascheroni constant


def _safe_inv_norm_cdf(p: float) -> float:
    """Inverse normal CDF clipped away from {0, 1} to avoid +/-inf."""
    eps = 1e-12
    p = min(max(p, eps), 1.0 - eps)
    return float(norm.ppf(p))


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """Expected maximum Sharpe ratio across `n_trials` i.i.d. trials with
    cross-trial Sharpe variance `sharpe_variance`.

    Bailey & López de Prado (2014) approximation:

        E[max SR] ≈ sqrt(V[SR]) * (
            (1 - γ) * Φ⁻¹(1 - 1/N) + γ * Φ⁻¹(1 - 1/(N·e))
        )

    where γ is the Euler–Mascheroni constant and N = n_trials.

    For N ≤ 1 the multiple-testing correction is meaningless; return 0 so the
    PSR collapses to the un-deflated PSR (mean vs zero).
    """
    if n_trials <= 1 or sharpe_variance <= 0:
        return 0.0
    n = float(n_trials)
    term1 = (1.0 - _EULER_GAMMA) * _safe_inv_norm_cdf(1.0 - 1.0 / n)
    term2 = _EULER_GAMMA * _safe_inv_norm_cdf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(sharpe_variance) * (term1 + term2)


def probabilistic_sharpe(
    sharpe: float,
    benchmark_sharpe: float,
    n_observations: int,
    skewness: float = 0.0,
    excess_kurtosis: float = 0.0,
) -> float:
    """Probabilistic Sharpe Ratio (Bailey & López de Prado 2012).

    Probability that the true Sharpe exceeds `benchmark_sharpe` given the
    observed `sharpe` over `n_observations` returns, accounting for skewness
    and excess kurtosis of the return distribution.

        PSR = Φ( (SR - SR*) · sqrt(T - 1) /
                 sqrt(1 - skew·SR + (kurt - 1)/4 · SR²) )

    where T = n_observations and SR* = benchmark_sharpe.

    For very small n_observations or a degenerate denominator we degrade
    gracefully to PSR=0.5 (no information).
    """
    if n_observations <= 1:
        return 0.5
    denom_inner = 1.0 - skewness * sharpe + (excess_kurtosis - 1.0) / 4.0 * sharpe * sharpe
    # Numerical floor: denom_inner must be > 0 for the PSR to be defined.
    # If it's not (extreme skew/kurt combo), fall back to no-information.
    if denom_inner <= 0:
        return 0.5
    z = (sharpe - benchmark_sharpe) * math.sqrt(n_observations - 1) / math.sqrt(denom_inner)
    return float(norm.cdf(z))


def deflated_sharpe(
    sharpe: float,
    n_trials: int,
    n_observations: int,
    sharpe_variance: float | None = None,
    skewness: float = 0.0,
    excess_kurtosis: float = 0.0,
) -> tuple[float, float]:
    """Deflated Sharpe Ratio (López de Prado 2014).

    Returns (dsr, p_value) where:
      - dsr is the PSR with benchmark = expected_max_sharpe(n_trials, V[SR]).
      - p_value = 1 - dsr (probability the observed Sharpe is no better than
        the best-of-N null).

    Parameters
    ----------
    sharpe : float
        Observed Sharpe ratio of THIS trial (mean / std of per-period returns).
        Non-annualized; whatever frequency the caller's `n_observations`
        counted matches.
    n_trials : int
        Total trials in the registry (NOT just this strategy class). When the
        registry is empty/has only this trial, multiple-testing correction is
        meaningless — pass n_trials=1 and the function collapses to PSR with
        benchmark=0.
    n_observations : int
        Number of per-period returns underlying this trial's Sharpe (e.g. the
        per-trade PnL count).
    sharpe_variance : float | None
        Variance of Sharpe ratios across the registry. None → 1.0 as a
        placeholder; the promotion gate caller should pass the actual
        cross-trial variance from `registry.api.query()`. This affects how
        large the expected_max_sharpe inflation gets — bigger variance means a
        higher hurdle.
    skewness, excess_kurtosis : float
        Higher moments of the per-period return distribution. Default to
        normal (0, 0).

    References
    ----------
    López de Prado, M. (2014). "The Deflated Sharpe Ratio." Journal of
    Portfolio Management, 40(5), 94–107.
    """
    if sharpe_variance is None:
        sharpe_variance = 1.0
    sr_star = expected_max_sharpe(n_trials, sharpe_variance)
    psr = probabilistic_sharpe(
        sharpe=sharpe,
        benchmark_sharpe=sr_star,
        n_observations=n_observations,
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
    )
    dsr = psr
    p_value = 1.0 - dsr
    return float(dsr), float(p_value)
