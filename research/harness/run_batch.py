"""Batch runner — aggregates per-game replay results into portfolio metrics.

Public API
----------
- :func:`run_batch` — run a StrategySpec across all games in a split.
- :func:`load_split`  — return the game_ids list for a split from splits.json.
- :class:`BatchResult` — frozen dataclass carrying portfolio-level metrics.

Test-set safety
---------------
If ``split == "test"``, ``run_batch`` requires a non-None ``unlock_test_token``
equal to ``sha256(spec.to_json())``.  On acceptance it appends a burn record to
``market_data/test_unlocks.log``.  There is no remaining-budget check here; that
is Wave 3's job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import multiprocessing
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from research.harness.strategy_spec import StrategySpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import helper for replay — the replay-engine agent may land in parallel.
# ---------------------------------------------------------------------------

def _import_replay():
    """Import replay_game and TrialResult lazily so import-time errors are
    deferred until the first actual call, not at module import time."""
    try:
        from research.harness.replay import replay_game, TrialResult  # noqa: PLC0415
        return replay_game, TrialResult
    except ImportError as exc:
        raise ImportError(
            "research.harness.replay is not yet available. "
            "Wait for the replay-engine agent to land."
        ) from exc


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPLITS_PATH = _REPO_ROOT / "market_data" / "splits.json"
_VALID_SPLITS = frozenset({"train", "val", "test"})


def load_split(name: Literal["train", "val", "test"]) -> list[str]:
    """Read ``market_data/splits.json`` and return the relevant game_ids list.

    Parameters
    ----------
    name:
        One of ``"train"``, ``"val"``, or ``"test"``.

    Returns
    -------
    list[str]
        Non-empty list of game_id strings.

    Raises
    ------
    ValueError
        If ``name`` is not a recognised split name.
    FileNotFoundError
        If ``market_data/splits.json`` does not exist.
    KeyError
        If the JSON file lacks the expected key.
    """
    if name not in _VALID_SPLITS:
        raise ValueError(
            f"Unknown split {name!r}; must be one of {sorted(_VALID_SPLITS)}"
        )
    path = _SPLITS_PATH
    with path.open() as fh:
        data = json.load(fh)
    key = f"{name}_game_ids"
    return list(data[key])


# ---------------------------------------------------------------------------
# Test-set guard
# ---------------------------------------------------------------------------

def _expected_unlock_token(spec: StrategySpec) -> str:
    return hashlib.sha256(spec.to_json().encode()).hexdigest()


def _check_test_guard(
    spec: StrategySpec,
    unlock_test_token: str | None,
) -> None:
    """Enforce the test-set access gate.  Raises PermissionError on failure."""
    if unlock_test_token is None:
        raise PermissionError(
            "Test-set access requires unlock_test_token = sha256(spec.to_json()). "
            "Refusing to run."
        )
    if unlock_test_token != _expected_unlock_token(spec):
        raise PermissionError(
            f"unlock_test_token mismatch for spec {spec.spec_hash()[:8]}. "
            "Token must be bound to this exact spec."
        )


def _burn_test_unlock(spec: StrategySpec) -> None:
    """Append a burn record to market_data/test_unlocks.log."""
    log_path = _REPO_ROOT / "market_data" / "test_unlocks.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        ts = datetime.now(tz=timezone.utc).isoformat()
        fh.write(f"{ts} {spec.spec_hash()} burn run_batch(split=test)\n")


# ---------------------------------------------------------------------------
# Per-game RNG seed derivation
# ---------------------------------------------------------------------------

def _game_rng_seed(rng_seed: int, game_id: str) -> int:
    """Derive a per-game seed from (rng_seed, game_id) for reproducibility."""
    h = hashlib.sha256(f"{rng_seed}:{game_id}".encode()).digest()
    # Use first 8 bytes as a 64-bit integer; keep it non-negative.
    return int.from_bytes(h[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


# ---------------------------------------------------------------------------
# BatchResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchResult:
    """Portfolio-level metrics aggregated from per-game TrialResult objects."""

    spec_hash: str
    split: Literal["train", "val", "test"]
    n_games: int
    n_games_with_trades: int
    n_trades_total: int
    pnl_gross: float
    pnl_net: float
    notional_traded: float
    mean_pnl_per_trade_gross: float
    mean_pnl_per_trade_net: float
    sharpe_per_trade_net: float        # mean/std of per-trade net PnL; NaN if < 2 trades
    win_rate: float                    # frac of trades with net_pnl > 0; NaN if 0 trades
    avg_holding_sec: float
    n_games_skipped: int               # games where trades==0 AND skipped list non-empty
    skipped_reasons: dict              # top-10 aggregated reason counts
    trials: list                       # full per-game TrialResult objects
    rng_seed: int


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate(
    spec: StrategySpec,
    split: Literal["train", "val", "test"],
    trials: list,
    rng_seed: int,
) -> BatchResult:
    """Build a :class:`BatchResult` from a list of :class:`TrialResult` objects.

    The ``trials`` list may contain a mix of successful TrialResult objects and
    ``(game_id, exception)`` pairs — exceptions were captured per-game and
    recorded as a single-reason skip.
    """
    # Separate successful trials from exception stubs
    good_trials = []
    exception_stubs = []  # list of (game_id, str_reason)
    for item in trials:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], Exception):
            exception_stubs.append(item)
        else:
            good_trials.append(item)

    n_games = len(trials)

    # Flatten all trade PnL series from successful trials
    all_trades_gross: list[float] = []
    all_trades_net: list[float] = []
    all_holding_secs: list[float] = []
    all_notional: list[float] = []

    n_games_with_trades = 0
    n_games_skipped = 0
    reason_counts: dict[str, int] = {}

    for trial in good_trials:
        trades = getattr(trial, "trades", [])
        skipped = getattr(trial, "skipped", [])

        if len(trades) > 0:
            n_games_with_trades += 1
            for t in trades:
                all_trades_gross.append(float(getattr(t, "pnl_gross", 0.0)))
                all_trades_net.append(float(getattr(t, "pnl_net", 0.0)))
                all_holding_secs.append(float(getattr(t, "holding_sec", 0.0)))
                all_notional.append(float(getattr(t, "notional", 0.0)))
        elif len(skipped) > 0:
            # Had entry attempts but couldn't trade
            n_games_skipped += 1
            for reason in skipped:
                r = str(reason)
                reason_counts[r] = reason_counts.get(r, 0) + 1

    # Count exception stubs as skipped games
    for game_id, exc in exception_stubs:
        n_games_skipped += 1
        reason = f"exception:{type(exc).__name__}:{str(exc)[:80]}"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    n_trades_total = len(all_trades_gross)
    pnl_gross = sum(all_trades_gross)
    pnl_net = sum(all_trades_net)
    notional_traded = sum(all_notional)

    # Mean per-trade (across ALL trades, not mean-of-game-means)
    if n_trades_total > 0:
        mean_pnl_per_trade_gross = pnl_gross / n_trades_total
        mean_pnl_per_trade_net = pnl_net / n_trades_total
    else:
        mean_pnl_per_trade_gross = 0.0
        mean_pnl_per_trade_net = 0.0

    # Sharpe: mean / std of per-trade net PnL series (NO annualization)
    if n_trades_total >= 2:
        mu = mean_pnl_per_trade_net
        variance = sum((x - mu) ** 2 for x in all_trades_net) / (n_trades_total - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        sharpe_per_trade_net = mu / std if std > 0 else (
            float("inf") if mu > 0 else (float("-inf") if mu < 0 else 0.0)
        )
    else:
        sharpe_per_trade_net = float("nan")

    # Win rate
    if n_trades_total > 0:
        win_rate = sum(1 for v in all_trades_net if v > 0) / n_trades_total
    else:
        win_rate = float("nan")

    # Average holding time
    avg_holding_sec = (
        sum(all_holding_secs) / len(all_holding_secs)
        if all_holding_secs
        else 0.0
    )

    # Top-10 skipped reasons sorted by count descending
    sorted_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:10]
    skipped_reasons = dict(sorted_reasons)

    return BatchResult(
        spec_hash=spec.spec_hash(),
        split=split,
        n_games=n_games,
        n_games_with_trades=n_games_with_trades,
        n_trades_total=n_trades_total,
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        notional_traded=notional_traded,
        mean_pnl_per_trade_gross=mean_pnl_per_trade_gross,
        mean_pnl_per_trade_net=mean_pnl_per_trade_net,
        sharpe_per_trade_net=sharpe_per_trade_net,
        win_rate=win_rate,
        avg_holding_sec=avg_holding_sec,
        n_games_skipped=n_games_skipped,
        skipped_reasons=skipped_reasons,
        trials=good_trials,
        rng_seed=rng_seed,
    )


# ---------------------------------------------------------------------------
# Worker for multiprocessing.Pool
# ---------------------------------------------------------------------------

def _run_one_game(args: tuple) -> object:
    """Top-level function (pickleable) for running one game.

    Returns either a TrialResult or a (game_id, Exception) stub.
    """
    spec_json, game_id, seed, allow_test = args
    try:
        replay_game, _ = _import_replay()
        spec = StrategySpec.from_json(spec_json)
        return replay_game(spec, game_id, rng_seed=seed, allow_test=allow_test)
    except Exception as exc:  # noqa: BLE001
        logger.warning("replay_game(%r) raised %s: %s", game_id, type(exc).__name__, exc)
        return (game_id, exc)


# ---------------------------------------------------------------------------
# Public run_batch
# ---------------------------------------------------------------------------

def run_batch(
    spec: StrategySpec,
    split: Literal["train", "val", "test"],
    rng_seed: int = 0,
    parallel: int = 1,
    unlock_test_token: str | None = None,
) -> BatchResult:
    """Run the spec across all games in the specified split.

    Parameters
    ----------
    spec:
        The strategy to backtest.
    split:
        One of ``"train"``, ``"val"``, or ``"test"``.
    rng_seed:
        Master RNG seed; per-game seeds are derived from this.
    parallel:
        Number of worker processes.  ``1`` (default) runs serially.
    unlock_test_token:
        Required when ``split == "test"``; must equal
        ``sha256(spec.to_json()).hexdigest()``.

    Returns
    -------
    BatchResult

    Raises
    ------
    ValueError
        If ``split`` is not one of the valid values.
    PermissionError
        If ``split == "test"`` and the unlock token is missing or wrong.
    """
    if split not in _VALID_SPLITS:
        raise ValueError(
            f"Unknown split {split!r}; must be one of {sorted(_VALID_SPLITS)}"
        )

    # Test-set guard
    if split == "test":
        _check_test_guard(spec, unlock_test_token)
        _burn_test_unlock(spec)

    # Load game IDs for this split
    game_ids = load_split(split)

    allow_test = split == "test"
    spec_json = spec.to_json()

    # Build task args list
    task_args = [
        (spec_json, game_id, _game_rng_seed(rng_seed, game_id), allow_test)
        for game_id in game_ids
    ]

    # Execute
    if parallel > 1:
        with multiprocessing.Pool(processes=parallel) as pool:
            raw_results = pool.map(_run_one_game, task_args)
    else:
        raw_results = [_run_one_game(args) for args in task_args]

    return _aggregate(spec, split, raw_results, rng_seed)


__all__ = [
    "BatchResult",
    "load_split",
    "run_batch",
]
