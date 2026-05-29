"""run_backtest — THE security boundary for every backtest the harness runs.

Every Codex worker, every manual experiment, every orchestrator promotion call
flows through this script. The contract is:

    python -m research.agents.tools.run_backtest \\
        --spec-file <path> --split train|val|test --agent-id <str>
        [--mechanistic-writeup <path>] [--unlock-test-token <sha256>]
        [--rng-seed <int>] [--registry-db <path>] [--audit-log <path>]
        [--unlock-log <path>] [--dry-run]

On success the script emits ONE JSON line to stdout (the canonical result
schema below) and exits 0. On any refusal it emits `{"error":..., "reason":...}`
and exits 1.

Defensive layers (in order, any failure → refuse + exit 1):

  1. Argument parse + spec-file existence.
  2. ``StrategySpec.from_json`` + ``StrategySpec.validate``.  The validate()
     call is the canonical live-safety gate; the loop in step 3 duplicates it
     defensively so a future bug in `from_json` cannot bypass the boundary.
  3. Belt-and-suspenders re-check that EVERY feature in `spec.features` has
     ``is_live_safe=True`` via the feature registry. If `validate()` ever
     regresses, this catches it.
  4. Test-split guard: `--unlock-test-token` must equal the sha256 of the
     canonical spec JSON; project-lifetime budget of 5 burns enforced via
     `market_data/test_unlocks.log` (override path with `--unlock-log` for
     testing).
  5. Run the batch via `research.harness.run_batch.run_batch`.
  6. Score with `research.scorer.promotion_gate.evaluate_trial` (skipped on
     dry-run).
  7. Record one INSERT to the registry through `research.registry.api.record_trial`
     (skipped on dry-run).
  8. Append one JSONL row to the audit log (always — even on refusal).

The audit log is append-only; this module never truncates it. Trial-registry
writes happen exclusively through `research.registry.api`; there is no direct
sqlite3 access from this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from research.features.registry import REGISTRY
from research.harness.cost_profile import set_active_profile
from research.harness.strategy_spec import StrategySpec


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY_DB = _REPO_ROOT / "market_data" / "trials.db"
DEFAULT_AUDIT_LOG = _REPO_ROOT / "market_data" / "audit.log"
DEFAULT_UNLOCK_LOG = _REPO_ROOT / "market_data" / "test_unlocks.log"

# Per plan §gate: lifetime budget of 5 test-set unlocks across the project.
TEST_UNLOCK_BUDGET = 5


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _iso_now() -> str:
    """UTC ISO-8601 timestamp with second precision (no microseconds)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_audit_log(
    path: Path,
    *,
    agent_id: str,
    spec_hash: Optional[str],
    split: Optional[str],
    command_argv: List[str],
    exit_code: int,
    refused: bool = False,
    reason: Optional[str] = None,
    trial_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append-only audit log writer.

    Uses `open(path, "a")` line-by-line; never truncates. Each call writes
    exactly one JSON line terminated by ``\n`` so multiple workers can interleave
    without corrupting prior rows.
    """
    row: Dict[str, Any] = {
        "ts": _iso_now(),
        "agent_id": agent_id,
        "spec_hash": spec_hash,
        "split": split,
        "command_argv": list(command_argv),
        "exit_code": int(exit_code),
    }
    if refused:
        row["refused"] = True
    if reason is not None:
        row["reason"] = reason
    if trial_id is not None:
        row["trial_id"] = int(trial_id)
    if extra:
        for k, v in extra.items():
            if k not in row:  # never let extras overwrite core fields
                row[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, separators=(",", ":"), sort_keys=True)
    with path.open("a") as fh:
        fh.write(line + "\n")


def _emit_result(payload: Dict[str, Any]) -> None:
    """Print one JSON line to stdout. Newline-terminated for tool consumers."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _expected_unlock_token(spec: StrategySpec) -> str:
    return hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()


def _count_burns_in_unlock_log(path: Path) -> int:
    """Count rows in the unlock log whose 3rd whitespace-separated token is
    ``burn``. Lines starting with ``#`` (comments) are ignored. Missing file
    is treated as zero burns."""
    if not path.exists():
        return 0
    n = 0
    with path.open() as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            # Expected format: "<iso_ts> <spec_hash> <attempt|burn> <reason...>"
            if len(parts) >= 3 and parts[2].lower() == "burn":
                n += 1
    return n


def _append_burn_to_unlock_log(path: Path, spec_hash: str) -> None:
    """Append a burn row to the unlock log. Append-only; no truncation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(f"{_iso_now()} {spec_hash} burn run_backtest_cli\n")


def _parse_game_id(game_id: str) -> tuple[str, str, str]:
    """Parse a game_id of the form ``YYYY-MM-DD_AWAY_at_HOME``.

    Returns ``(date_iso, away_team, home_team)``. If the format doesn't match
    we fall back to ``(game_id, "", "")`` — the scorer tolerates these strings
    as opaque labels for the bootstrap; missing date/team fields just degrade
    the stability checks rather than crashing.
    """
    # Split on underscore. Defensive: many shapes look correct enough.
    parts = game_id.split("_")
    if len(parts) >= 4 and parts[2] == "at":
        return parts[0], parts[1], parts[3]
    if len(parts) >= 4 and "at" in parts:
        # Tolerate odd layouts; pick the date as the first part.
        i = parts.index("at")
        date = parts[0]
        away = parts[i - 1] if i >= 1 else ""
        home = parts[i + 1] if i + 1 < len(parts) else ""
        return date, away, home
    return game_id, "", ""


def _build_score_arrays(batch_result: Any) -> Dict[str, np.ndarray]:
    """Flatten a BatchResult into the per-trade arrays the scorer wants.

    Reads `batch_result.trials` (list of TrialResult-shaped objects), each with
    `trades` (list of Trade-shaped objects).  Each trade contributes one row of
    (pnl_net, game_id, date, home_team, primary_team).

    The scorer tolerates empty arrays — when a trial yields zero trades we
    return zero-length arrays and `evaluate_trial` will return NaN CIs that
    fail the gate cleanly.
    """
    pnl_nets: List[float] = []
    game_ids: List[str] = []
    dates: List[str] = []
    home_teams: List[str] = []
    primary_teams: List[str] = []
    for trial in getattr(batch_result, "trials", []) or []:
        trades = getattr(trial, "trades", []) or []
        gid = str(getattr(trial, "game_id", ""))
        date_iso, away_team, home_team = _parse_game_id(gid)
        for tr in trades:
            pnl_nets.append(float(getattr(tr, "pnl_net", 0.0)))
            game_ids.append(gid)
            dates.append(date_iso)
            home_teams.append(home_team)
            side = str(getattr(tr, "side", ""))
            if side == "long_home":
                primary_teams.append(home_team)
            elif side == "long_away":
                primary_teams.append(away_team)
            else:
                primary_teams.append("")
    return {
        "pnl_net": np.asarray(pnl_nets, dtype=float),
        "game_id": np.asarray(game_ids, dtype=object),
        "date": np.asarray(dates, dtype=object),
        "home_team": np.asarray(home_teams, dtype=object),
        "primary_team": np.asarray(primary_teams, dtype=object),
    }


def _compute_sharpe_variance_in_registry(db_path: Path | str) -> float:
    """Estimate the registry's cross-trial val Sharpe variance for DSR.

    Per the brief: with fewer than 20 trials, use the placeholder 0.05 to
    avoid DSR-as-hard-veto. With 20+, query the most recent 200 rows and
    take the variance of `val_sharpe_net` (ignoring None / NaN).
    """
    from research.registry.api import count_total_trials, query

    n_total = count_total_trials(db_path=db_path)
    if n_total < 20:
        return 0.05
    rows = query(limit=200, db_path=db_path)
    values: List[float] = []
    for r in rows:
        v = r.val_sharpe_net
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        values.append(f)
    if len(values) < 2:
        return 0.05
    return float(np.var(values))


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_backtest",
        description="The single security boundary for every backtest.",
    )
    p.add_argument(
        "--spec-file",
        required=True,
        help="Path to a JSON file containing the StrategySpec.",
    )
    p.add_argument(
        "--split",
        required=True,
        choices=("train", "val", "test"),
        help="Data split to backtest against.",
    )
    p.add_argument(
        "--agent-id",
        required=True,
        help='Provenance tag. e.g. "codex:abc12345" or "manual:p4_halftime".',
    )
    p.add_argument(
        "--mechanistic-writeup",
        default=None,
        help="Optional path to a markdown file with the mechanistic rationale.",
    )
    p.add_argument(
        "--unlock-test-token",
        default=None,
        help="sha256(spec.to_json()) — required when --split=test.",
    )
    p.add_argument(
        "--rng-seed",
        type=int,
        default=0,
        help="Master RNG seed (per-game seeds are derived).",
    )
    p.add_argument(
        "--registry-db",
        default=None,
        help="Override path to trials.db. If omitted, falls back to the "
        "$RESEARCH_REGISTRY_DB env var, then the canonical "
        "market_data/trials.db. Set the env var to a scratch DB for "
        "speculative search so the canonical multiple-testing N isn't inflated.",
    )
    p.add_argument(
        "--audit-log",
        default=str(DEFAULT_AUDIT_LOG),
        help="Override path to the audit log (for testing).",
    )
    p.add_argument(
        "--unlock-log",
        default=str(DEFAULT_UNLOCK_LOG),
        help="Override path to the test-set unlock log (for testing).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs but skip replay, scoring, and registry write.",
    )
    p.add_argument(
        "--cost-profile",
        default=None,
        choices=("pessimistic", "live_pm", "zero"),
        help="Override the active fill cost profile. If omitted, reads "
        "$RESEARCH_COST_PROFILE; if that is also unset, uses pessimistic.",
    )
    return p


# --------------------------------------------------------------------------- #
# Core flow
# --------------------------------------------------------------------------- #


def _refuse(
    *,
    error: str,
    reason: str,
    audit_path: Path,
    agent_id: str,
    spec_hash: Optional[str],
    split: Optional[str],
    argv: List[str],
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    """Emit refusal JSON, log it, and return exit code 1.

    Audit-log writes happen even on refusal — a worker probing the test-set
    boundary is informative signal the orchestrator wants to see.
    """
    _emit_result({"error": error, "reason": reason})
    try:
        _append_audit_log(
            audit_path,
            agent_id=agent_id,
            spec_hash=spec_hash,
            split=split,
            command_argv=argv,
            exit_code=1,
            refused=True,
            reason=f"{error}: {reason}",
            extra=extra,
        )
    except Exception:
        # If the audit log itself is unwritable, dump traceback to stderr
        # but still surface the refusal (the JSON is already on stdout).
        traceback.print_exc(file=sys.stderr)
    return 1


def run(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    # Use parse_known_args to keep `--dry-run` behavior with extras stable;
    # but we want strict parsing of the contract here.
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits on bad args before we can audit-log. Surface as a
        # refusal and re-raise the original code.
        return int(exc.code) if exc.code is not None else 2

    # The command_argv we record is the actual argv we received. If argv is
    # None, fall back to sys.argv.
    command_argv = list(argv) if argv is not None else list(sys.argv[1:])

    audit_path = Path(args.audit_log)
    # Resolve registry path: explicit flag > $RESEARCH_REGISTRY_DB > canonical.
    registry_db = (
        args.registry_db
        or os.environ.get("RESEARCH_REGISTRY_DB")
        or str(DEFAULT_REGISTRY_DB)
    )
    unlock_log = Path(args.unlock_log)

    # ---- Cost profile resolution: CLI arg > env var > default (pessimistic) ----
    profile_name = args.cost_profile or os.environ.get("RESEARCH_COST_PROFILE")
    if profile_name:
        try:
            set_active_profile(profile_name)
        except (ValueError, TypeError) as exc:
            return _refuse(
                error="invalid_cost_profile",
                reason=f"{type(exc).__name__}: {exc}",
                audit_path=audit_path,
                agent_id=args.agent_id,
                spec_hash=None,
                split=args.split,
                argv=command_argv,
            )

    # ---- Step 1: spec-file existence ----
    spec_path = Path(args.spec_file)
    if not spec_path.exists():
        return _refuse(
            error="spec_file_missing",
            reason=f"spec file not found: {spec_path}",
            audit_path=audit_path,
            agent_id=args.agent_id,
            spec_hash=None,
            split=args.split,
            argv=command_argv,
        )

    # ---- Step 2: load + validate spec ----
    try:
        spec_text = spec_path.read_text()
    except Exception as exc:  # noqa: BLE001 — any read failure is fatal
        return _refuse(
            error="spec_read_error",
            reason=f"{type(exc).__name__}: {exc}",
            audit_path=audit_path,
            agent_id=args.agent_id,
            spec_hash=None,
            split=args.split,
            argv=command_argv,
        )

    try:
        spec = StrategySpec.from_json(spec_text)
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            error="spec_parse_error",
            reason=f"{type(exc).__name__}: {exc}",
            audit_path=audit_path,
            agent_id=args.agent_id,
            spec_hash=None,
            split=args.split,
            argv=command_argv,
        )

    spec_hash = spec.spec_hash()

    try:
        spec.validate()
    except Exception as exc:  # noqa: BLE001 — ValueError is normal here
        return _refuse(
            error="spec_validation_failed",
            reason=f"{type(exc).__name__}: {exc}",
            audit_path=audit_path,
            agent_id=args.agent_id,
            spec_hash=spec_hash,
            split=args.split,
            argv=command_argv,
        )

    # ---- Step 3: belt-and-suspenders live-safety re-check ----
    # validate() already checks this, but a regression in StrategySpec.from_json
    # or feature registry mutation must not bypass the boundary.
    for fname in spec.features:
        try:
            fspec = REGISTRY.get(fname)
        except KeyError as exc:
            return _refuse(
                error="feature_unregistered",
                reason=f"{fname!r} is not in the feature registry",
                audit_path=audit_path,
                agent_id=args.agent_id,
                spec_hash=spec_hash,
                split=args.split,
                argv=command_argv,
            )
        if not fspec.is_live_safe:
            return _refuse(
                error="feature_not_live_safe",
                reason=(
                    f"feature {fname!r} has is_live_safe=False; this is a "
                    "leakage canary. Spec refused."
                ),
                audit_path=audit_path,
                agent_id=args.agent_id,
                spec_hash=spec_hash,
                split=args.split,
                argv=command_argv,
            )

    # ---- Step 4: test-set guard ----
    unlock_token: Optional[str] = args.unlock_test_token
    # Env-var fallback for HUMAN-typed CLI calls only. The CLI arg takes
    # precedence; env vars merely populate it when unset and the hash matches.
    if unlock_token is None:
        env_token = os.environ.get("RESEARCH_UNLOCK_TEST")
        env_spec_hash = os.environ.get("RESEARCH_UNLOCK_SPEC_HASH")
        if env_token and env_spec_hash == spec_hash:
            unlock_token = env_token

    if args.split == "test":
        if unlock_token is None:
            return _refuse(
                error="test_split_requires_unlock_token",
                reason="test split requires --unlock-test-token",
                audit_path=audit_path,
                agent_id=args.agent_id,
                spec_hash=spec_hash,
                split=args.split,
                argv=command_argv,
            )
        expected = _expected_unlock_token(spec)
        if unlock_token != expected:
            return _refuse(
                error="test_unlock_token_mismatch",
                reason="unlock token does not match this spec",
                audit_path=audit_path,
                agent_id=args.agent_id,
                spec_hash=spec_hash,
                split=args.split,
                argv=command_argv,
            )
        # Lifetime budget check — don't increment on refusal.
        burns = _count_burns_in_unlock_log(unlock_log)
        if burns >= TEST_UNLOCK_BUDGET:
            return _refuse(
                error="test_unlock_budget_exhausted",
                reason=(
                    f"test-set unlock budget exhausted "
                    f"({burns}/{TEST_UNLOCK_BUDGET} burned)"
                ),
                audit_path=audit_path,
                agent_id=args.agent_id,
                spec_hash=spec_hash,
                split=args.split,
                argv=command_argv,
            )
        # Append the burn BEFORE the backtest runs so an exception during
        # the run still consumes the unlock budget — preventing retry-loops
        # from probing the test set.
        _append_burn_to_unlock_log(unlock_log, spec_hash)

    # ---- Mechanistic writeup (optional) ----
    mech_text: Optional[str] = None
    if args.mechanistic_writeup is not None:
        mech_path = Path(args.mechanistic_writeup)
        try:
            mech_text = mech_path.read_text()
        except Exception as exc:  # noqa: BLE001
            return _refuse(
                error="mechanistic_writeup_unreadable",
                reason=f"{type(exc).__name__}: {exc}",
                audit_path=audit_path,
                agent_id=args.agent_id,
                spec_hash=spec_hash,
                split=args.split,
                argv=command_argv,
            )

    # ---- Dry-run short-circuit ----
    if args.dry_run:
        payload = {
            "trial_id": None,
            "spec_hash": spec_hash,
            "n_trades": None,
            "pnl_net": None,
            "sharpe_net": None,
            "win_rate": None,
            "gate_passed": None,
            "gate_reasons": [],
        }
        _emit_result(payload)
        _append_audit_log(
            audit_path,
            agent_id=args.agent_id,
            spec_hash=spec_hash,
            split=args.split,
            command_argv=command_argv,
            exit_code=0,
            extra={"dry_run": True},
        )
        return 0

    # ---- Step 5: run the batch ----
    try:
        # Lazy import so dry-run paths don't pay the cost of pulling in pandas
        # via run_batch -> replay.
        from research.harness.run_batch import run_batch  # noqa: PLC0415

        batch_result = run_batch(
            spec,
            args.split,
            rng_seed=int(args.rng_seed),
            unlock_test_token=unlock_token,
        )
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            error="batch_run_failed",
            reason=f"{type(exc).__name__}: {exc}",
            audit_path=audit_path,
            agent_id=args.agent_id,
            spec_hash=spec_hash,
            split=args.split,
            argv=command_argv,
        )

    # ---- Step 6: score ----
    arrays = _build_score_arrays(batch_result)
    n_trades = int(getattr(batch_result, "n_trades_total", 0))
    try:
        from research.registry.api import count_total_trials  # noqa: PLC0415
        from research.scorer.promotion_gate import evaluate_trial  # noqa: PLC0415

        sharpe_variance = _compute_sharpe_variance_in_registry(registry_db)
        # +1 includes this trial as one of the multiple-testing N.
        n_total_in_registry = count_total_trials(db_path=registry_db) + 1
        decision = evaluate_trial(
            val_pnl_per_trade=arrays["pnl_net"],
            val_game_id_per_trade=arrays["game_id"],
            val_date_per_trade=arrays["date"],
            val_home_team_per_trade=arrays["home_team"],
            val_primary_team_per_trade=arrays["primary_team"],
            n_total_trials_in_registry=n_total_in_registry,
            sharpe_variance_in_registry=sharpe_variance,
            rng_seed=int(args.rng_seed),
        )
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            error="scoring_failed",
            reason=f"{type(exc).__name__}: {exc}",
            audit_path=audit_path,
            agent_id=args.agent_id,
            spec_hash=spec_hash,
            split=args.split,
            argv=command_argv,
            extra={"n_trades": n_trades},
        )

    # ---- Step 7: record to registry ----
    try:
        from research.registry.api import record_trial  # noqa: PLC0415

        # record_trial reads decision.dsr_pvalue via getattr — but the scorer
        # exposes ``dsr_p_value``.  Pass a tiny adapter that exposes BOTH names
        # so the registry stores the value rather than NULL. (record_trial
        # itself is fully duck-typed so adding the alias is safe.)
        decision_for_registry = _DecisionAdapter(decision)

        trial_id = record_trial(
            spec=spec,
            agent_id=args.agent_id,
            train_result=batch_result if args.split == "train" else None,
            val_result=batch_result if args.split == "val" else None,
            test_result=batch_result if args.split == "test" else None,
            mechanistic_writeup=mech_text,
            scorer_result=decision_for_registry,
            db_path=registry_db,
        )
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            error="registry_write_failed",
            reason=f"{type(exc).__name__}: {exc}",
            audit_path=audit_path,
            agent_id=args.agent_id,
            spec_hash=spec_hash,
            split=args.split,
            argv=command_argv,
            extra={"n_trades": n_trades, "gate_passed": bool(decision.passed)},
        )

    # ---- Step 8: audit log success + emit result ----
    pnl_net = float(getattr(batch_result, "pnl_net", 0.0))
    sharpe_net = float(getattr(batch_result, "sharpe_per_trade_net", float("nan")))
    win_rate = float(getattr(batch_result, "win_rate", float("nan")))

    payload: Dict[str, Any] = {
        "trial_id": int(trial_id),
        "spec_hash": spec_hash,
        "n_trades": n_trades,
        "pnl_net": pnl_net if pnl_net == pnl_net else None,  # NaN → None
        "sharpe_net": sharpe_net if sharpe_net == sharpe_net else None,
        "win_rate": win_rate if win_rate == win_rate else None,
        "gate_passed": bool(decision.passed),
        "gate_reasons": list(decision.reasons),
    }
    _emit_result(payload)

    _append_audit_log(
        audit_path,
        agent_id=args.agent_id,
        spec_hash=spec_hash,
        split=args.split,
        command_argv=command_argv,
        exit_code=0,
        trial_id=int(trial_id),
        extra={
            "n_trades": n_trades,
            "gate_passed": bool(decision.passed),
        },
    )
    return 0


# --------------------------------------------------------------------------- #
# Decision adapter — surfaces both `dsr_p_value` and `dsr_pvalue` attribute
# names so the registry's duck-typed reader stores the value either way.
# --------------------------------------------------------------------------- #


class _DecisionAdapter:
    """Wrap a PromotionDecision and surface both `dsr_p_value` and `dsr_pvalue`.

    The scorer dataclass field is `dsr_p_value` but the registry reads
    `dsr_pvalue` via `getattr(decision, "dsr_pvalue", None)`. Without this
    adapter the registry would silently store NULL. We never mutate the
    wrapped object, and `record_trial` is fully duck-typed so it accepts
    any object that exposes the right attribute names.
    """

    __slots__ = ("_d",)

    def __init__(self, decision: Any) -> None:
        self._d = decision

    def __getattr__(self, name: str) -> Any:
        if name == "dsr_pvalue":
            return getattr(self._d, "dsr_p_value", None)
        return getattr(self._d, name)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> int:  # pragma: no cover — exercised via subprocess in tests
    return run(sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
