"""Public API for the append-only trial registry.

Exposes four write paths and two read paths:

- :func:`record_trial`: append one row. INSERT-only, defensively checked.
- :func:`record_claude_review`: the ONLY sanctioned UPDATE path. Touches
  the four ``claude_review*`` columns on the latest matching row.
- :func:`query`: generic read with optional filters.
- :func:`count_total_trials`: denominator for the DSR scorer.
- :func:`query_promotable_candidates`: promotion CLI feed.

The append-only invariant is enforced by:
  1. All write-path SQL is constructed in this module — never from caller
     input — and runs through :func:`_assert_insert_only` /
     :func:`_assert_review_only` before execution.
  2. Caller-provided strings (spec_json, mechanistic_writeup, etc.) are
     bound via ``?`` parameters, never concatenated into the SQL.
  3. The schema lives in :mod:`research.registry.db`; this module never
     issues DDL beyond what :func:`connect` does on bootstrap.

See module docstring of :mod:`research.registry.db` for the why.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from research.registry.db import connect

if TYPE_CHECKING:
    # Typed-import only — these modules may not be importable when this
    # one loads (PromotionDecision lives in a sibling package being built
    # in parallel; StrategySpec / BatchResult are stable but kept lazy to
    # keep import-time cheap and to avoid pulling in numpy/pandas on
    # registry-only callers).
    from research.harness.run_batch import BatchResult
    from research.harness.strategy_spec import StrategySpec
    from research.scorer.promotion_gate import PromotionDecision


# --------------------------------------------------------------------------- #
# TrialRow — frozen read-side projection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrialRow:
    """Read-side projection of a trials row. Immutable.

    Only the columns hot-pathed by current callers are exposed as named
    attributes; full-row inspection is rare and can use the raw row
    factory directly. If you find yourself wanting more fields here,
    add them — but think about whether the new caller is doing something
    that belongs at the registry layer or at the analysis layer.
    """

    id: int
    spec_hash: str
    spec_name: str
    agent_id: str
    train_pnl_net: float | None
    val_pnl_net: float | None
    val_sharpe_net: float | None
    val_n_trades: int | None
    test_pnl_net: float | None
    scorer_promotion_gate_passed: bool | None
    scorer_block_bootstrap_ci_lo: float | None
    claude_review: str | None
    ts_created: int


# Columns selected by the read API, in order. Centralised so TrialRow's
# constructor and the SELECT list stay in lockstep.
_TRIAL_ROW_COLUMNS: tuple[str, ...] = (
    "id",
    "spec_hash",
    "spec_name",
    "agent_id",
    "train_pnl_net",
    "val_pnl_net",
    "val_sharpe_net",
    "val_n_trades",
    "test_pnl_net",
    "scorer_promotion_gate_passed",
    "scorer_block_bootstrap_ci_lo",
    "claude_review",
    "ts_created",
)
_TRIAL_ROW_SELECT = ", ".join(_TRIAL_ROW_COLUMNS)


def _row_to_trial(row: sqlite3.Row) -> TrialRow:
    """Build a :class:`TrialRow` from a sqlite3.Row.

    The ``gate_passed`` column is stored as INTEGER (0/1/NULL); coerce to
    ``bool | None`` here so callers never see the underlying int.
    """
    gate_raw = row["scorer_promotion_gate_passed"]
    gate: bool | None
    if gate_raw is None:
        gate = None
    else:
        gate = bool(gate_raw)
    return TrialRow(
        id=int(row["id"]),
        spec_hash=str(row["spec_hash"]),
        spec_name=str(row["spec_name"]),
        agent_id=str(row["agent_id"]),
        train_pnl_net=row["train_pnl_net"],
        val_pnl_net=row["val_pnl_net"],
        val_sharpe_net=row["val_sharpe_net"],
        val_n_trades=row["val_n_trades"],
        test_pnl_net=row["test_pnl_net"],
        scorer_promotion_gate_passed=gate,
        scorer_block_bootstrap_ci_lo=row["scorer_block_bootstrap_ci_lo"],
        claude_review=row["claude_review"],
        ts_created=int(row["ts_created"]),
    )


# --------------------------------------------------------------------------- #
# Write-path SQL safety
# --------------------------------------------------------------------------- #

# Regex matches the forbidden statement keywords as whole words. We anchor
# with word boundaries so "UPDATED_AT" in a comment wouldn't trip it — but
# in practice our SQL strings have neither comments nor identifier-like
# tokens beyond the column names already in EXPECTED_COLUMNS.
_FORBIDDEN_KEYWORDS_RX = re.compile(
    r"\b(UPDATE|DELETE|REPLACE|DROP|ALTER|TRUNCATE)\b",
    re.IGNORECASE,
)

# Columns that may appear in record_claude_review's SET clause. Anything
# else in the UPDATE statement triggers an assertion failure.
_REVIEW_COLUMNS: frozenset[str] = frozenset({
    "claude_review",
    "claude_review_ts",
    "claude_review_model",
})


def _assert_insert_only(sql: str) -> None:
    """Defence-in-depth: ensure the record_trial SQL is an INSERT and
    contains no UPDATE/DELETE/REPLACE/DROP/ALTER/TRUNCATE tokens.

    Raises ``RuntimeError`` if either check fails. The string is built in
    this module; an assertion failure here means a code change introduced
    a regression — it should never trip on user input.
    """
    stripped = sql.lstrip()
    if not stripped.upper().startswith("INSERT INTO TRIALS"):
        raise RuntimeError(
            f"record_trial SQL must start with 'INSERT INTO trials'; got: "
            f"{stripped[:60]!r}"
        )
    if _FORBIDDEN_KEYWORDS_RX.search(sql):
        raise RuntimeError(
            "record_trial SQL contains forbidden mutation keyword; "
            "append-only invariant violated."
        )


def _assert_review_only(sql: str) -> None:
    """Defence-in-depth: ensure record_claude_review's SQL is the exact
    UPDATE we expect, touching only the four review columns.

    Specifically requires:
      - statement begins with ``UPDATE trials SET claude_review`` (case-insensitive);
      - SET clause references only the columns in :data:`_REVIEW_COLUMNS`;
      - no second mutation keyword appears anywhere outside ``UPDATE``.
    """
    stripped = sql.lstrip()
    if not stripped.upper().startswith("UPDATE TRIALS SET CLAUDE_REVIEW"):
        raise RuntimeError(
            f"record_claude_review SQL must begin with "
            f"'UPDATE trials SET claude_review'; got: {stripped[:60]!r}"
        )
    # Forbid DELETE / REPLACE / DROP / ALTER / TRUNCATE; UPDATE itself is
    # allowed exactly once at the start.
    upper = sql.upper()
    upper_after_update = upper.replace("UPDATE", "", 1)  # drop the leading UPDATE
    if _FORBIDDEN_KEYWORDS_RX.search(upper_after_update):
        raise RuntimeError(
            "record_claude_review SQL contains forbidden mutation keyword "
            "after the leading UPDATE; append-only invariant violated."
        )
    # Pull the SET clause (between SET and WHERE) and verify each assigned
    # column is in our allowed set.
    m = re.search(r"\bSET\b(.*?)\bWHERE\b", sql, re.IGNORECASE | re.DOTALL)
    if m is None:
        raise RuntimeError(
            "record_claude_review SQL is missing a WHERE clause."
        )
    set_clause = m.group(1)
    # Each assignment is ``col = ?`` or ``col=?``; pick out the LHS column names.
    assigned = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=", set_clause)
    bad = [c for c in assigned if c.lower() not in _REVIEW_COLUMNS]
    if bad:
        raise RuntimeError(
            f"record_claude_review SET clause assigns to disallowed "
            f"column(s) {bad}; only {sorted(_REVIEW_COLUMNS)} are allowed."
        )


# --------------------------------------------------------------------------- #
# Helpers for marshalling BatchResult / PromotionDecision into row values
# --------------------------------------------------------------------------- #


def _batch_metrics(result: "BatchResult | None") -> dict[str, Any]:
    """Pluck the five summary columns from a BatchResult.

    The keys here intentionally lack split prefixes — the caller prefixes
    them with ``train_``/``val_``/``test_`` when building the row.
    """
    if result is None:
        return {
            "pnl_gross": None,
            "pnl_net": None,
            "sharpe_net": None,
            "n_trades": None,
            "win_rate": None,
        }
    # Duck-typed reads (the brief says do not isinstance-check).
    return {
        "pnl_gross": _as_float_or_none(getattr(result, "pnl_gross", None)),
        "pnl_net": _as_float_or_none(getattr(result, "pnl_net", None)),
        "sharpe_net": _as_float_or_none(getattr(result, "sharpe_per_trade_net", None)),
        "n_trades": _as_int_or_none(getattr(result, "n_trades_total", None)),
        "win_rate": _as_float_or_none(getattr(result, "win_rate", None)),
    }


def _as_float_or_none(v: Any) -> float | None:
    """Convert ``v`` to float, but pass None / NaN through as None.

    SQLite stores NaN as a REAL but querying it back is platform-flaky —
    NULL is the cleaner sentinel for "no value".
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # math.isnan would import math; cheap enough to inline.
    if f != f:  # NaN check
        return None
    return f


def _as_int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _scorer_fields(decision: "PromotionDecision | None") -> dict[str, Any]:
    """Pluck the scorer columns from a PromotionDecision-shaped object.

    Duck-typed: we read attributes by name and tolerate missing ones. The
    Wave 2 scorer agent owns the canonical shape; this layer doesn't import
    it at runtime so we can land independently.
    """
    if decision is None:
        return {
            "scorer_block_bootstrap_ci_lo": None,
            "scorer_block_bootstrap_ci_hi": None,
            "scorer_dsr": None,
            "scorer_dsr_pvalue": None,
            "scorer_promotion_gate_passed": None,
            "scorer_promotion_gate_reasons": None,
        }
    reasons = getattr(decision, "reasons", None)
    if reasons is None:
        reasons_json = None
    else:
        reasons_json = json.dumps(list(reasons))
    passed = getattr(decision, "passed", None)
    if passed is None:
        passed_int: int | None = None
    else:
        passed_int = 1 if bool(passed) else 0
    return {
        "scorer_block_bootstrap_ci_lo": _as_float_or_none(
            getattr(decision, "block_bootstrap_ci_lo", None)
        ),
        "scorer_block_bootstrap_ci_hi": _as_float_or_none(
            getattr(decision, "block_bootstrap_ci_hi", None)
        ),
        "scorer_dsr": _as_float_or_none(getattr(decision, "dsr", None)),
        "scorer_dsr_pvalue": _as_float_or_none(
            getattr(decision, "dsr_pvalue", None)
        ),
        "scorer_promotion_gate_passed": passed_int,
        "scorer_promotion_gate_reasons": reasons_json,
    }


# --------------------------------------------------------------------------- #
# record_trial — the append path
# --------------------------------------------------------------------------- #


# Ordered tuple of (column, ?-binding) — defining this once keeps the SQL
# and the values list in lockstep. ``id`` and ``ts_created`` are omitted —
# they default to AUTOINCREMENT / strftime('%s','now').
_INSERT_COLUMNS: tuple[str, ...] = (
    "spec_hash",
    "spec_json",
    "spec_name",
    "features_used",
    "agent_id",
    "parent_trial_id",
    "train_pnl_gross",
    "train_pnl_net",
    "train_sharpe_net",
    "train_n_trades",
    "train_win_rate",
    "val_pnl_gross",
    "val_pnl_net",
    "val_sharpe_net",
    "val_n_trades",
    "val_win_rate",
    "test_pnl_gross",
    "test_pnl_net",
    "test_sharpe_net",
    "test_n_trades",
    "test_win_rate",
    "scorer_block_bootstrap_ci_lo",
    "scorer_block_bootstrap_ci_hi",
    "scorer_dsr",
    "scorer_dsr_pvalue",
    "scorer_promotion_gate_passed",
    "scorer_promotion_gate_reasons",
    "mechanistic_writeup",
    "codex_worker_id",
    "cost_usd_estimate",
)


def _build_insert_sql() -> str:
    """Compose the canonical INSERT statement. Called once per record_trial
    call so the safety assertion runs against the live string."""
    cols = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
    return f"INSERT INTO trials ({cols}) VALUES ({placeholders})"


def record_trial(
    spec: "StrategySpec",
    agent_id: str,
    train_result: "BatchResult | None" = None,
    val_result: "BatchResult | None" = None,
    test_result: "BatchResult | None" = None,
    mechanistic_writeup: str | None = None,
    parent_trial_id: int | None = None,
    codex_worker_id: str | None = None,
    cost_usd_estimate: float | None = None,
    scorer_result: "PromotionDecision | None" = None,
    db_path: Path | str | None = None,
) -> int:
    """Append one row to the trials table. Returns the assigned id.

    The SQL string is built in this module and validated by
    :func:`_assert_insert_only` before execution — no UPDATE/DELETE/REPLACE
    is reachable from this path. All caller-provided strings (spec_json,
    mechanistic_writeup, ...) are bound via ``?`` parameters; SQL injection
    via, e.g., a malicious ``spec_json`` is impossible.

    Parameters
    ----------
    spec:
        Must expose ``spec_hash()``, ``to_json()``, ``name``, and ``features``
        (the StrategySpec contract). Duck-typed; no isinstance check.
    agent_id:
        Provenance tag. ``"manual:<filename>"`` for ad-hoc human-driven
        trials, ``"codex:<worker_id>"`` for autoresearcher workers,
        ``"orchestrator"`` for promotion-pipeline writes.
    train_result, val_result, test_result:
        BatchResult-shaped objects (or None). We read ``pnl_gross``,
        ``pnl_net``, ``sharpe_per_trade_net``, ``n_trades_total``, ``win_rate``.
    scorer_result:
        PromotionDecision-shaped object (or None). We read ``passed``,
        ``reasons``, ``dsr``, ``dsr_pvalue``, ``block_bootstrap_ci_lo``,
        ``block_bootstrap_ci_hi``.
    """
    # Duck-typed reads on the spec.
    spec_hash = spec.spec_hash()
    spec_json = spec.to_json()
    spec_name = spec.name
    features_used = json.dumps(list(spec.features))

    train = _batch_metrics(train_result)
    val = _batch_metrics(val_result)
    test = _batch_metrics(test_result)
    scorer = _scorer_fields(scorer_result)

    row_values: tuple[Any, ...] = (
        spec_hash,
        spec_json,
        spec_name,
        features_used,
        agent_id,
        parent_trial_id,
        train["pnl_gross"],
        train["pnl_net"],
        train["sharpe_net"],
        train["n_trades"],
        train["win_rate"],
        val["pnl_gross"],
        val["pnl_net"],
        val["sharpe_net"],
        val["n_trades"],
        val["win_rate"],
        test["pnl_gross"],
        test["pnl_net"],
        test["sharpe_net"],
        test["n_trades"],
        test["win_rate"],
        scorer["scorer_block_bootstrap_ci_lo"],
        scorer["scorer_block_bootstrap_ci_hi"],
        scorer["scorer_dsr"],
        scorer["scorer_dsr_pvalue"],
        scorer["scorer_promotion_gate_passed"],
        scorer["scorer_promotion_gate_reasons"],
        mechanistic_writeup,
        codex_worker_id,
        _as_float_or_none(cost_usd_estimate),
    )

    sql = _build_insert_sql()
    _assert_insert_only(sql)

    conn = connect(db_path)
    try:
        cur = conn.execute(sql, row_values)
        conn.commit()
        # lastrowid is the just-assigned autoincrement id. We coerce to int
        # so callers don't have to worry about the underlying SQLite type.
        new_id = cur.lastrowid
        if new_id is None:
            raise RuntimeError("INSERT succeeded but lastrowid is None")
        return int(new_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# record_claude_review — the ONLY sanctioned UPDATE path
# --------------------------------------------------------------------------- #


def _build_review_update_sql() -> str:
    """Compose the canonical UPDATE statement. Constant string, but
    routed through a builder so :func:`_assert_review_only` runs on the
    live value (catches accidental edits that diverge from the asserts)."""
    return (
        "UPDATE trials "
        "SET claude_review = ?, claude_review_ts = ?, claude_review_model = ? "
        "WHERE id = ?"
    )


def record_claude_review(
    spec_hash: str,
    review_text: str,
    review_model: str,
    db_path: Path | str | None = None,
) -> int:
    """Attach an Opus adversarial review to the latest trial for a spec_hash.

    THE ONLY SANCTIONED MUTATION PATH. Updates ``claude_review``,
    ``claude_review_ts``, and ``claude_review_model`` on the MOST RECENT
    trial row (by ``ts_created``, breaking ties by ``id``) whose
    ``claude_review IS NULL``.

    Returns the id of the row that was updated.

    Raises ``ValueError`` if no matching row exists, or if the most recent
    row for this spec_hash already has a review (idempotency: we don't
    silently overwrite Claude's prior verdict).
    """
    sql = _build_review_update_sql()
    _assert_review_only(sql)

    now_ts = int(time.time())

    conn = connect(db_path)
    try:
        # Find the latest row for this spec_hash, irrespective of review
        # status, so we can distinguish "no rows at all" from "newest row
        # already reviewed" — those raise different messages.
        row = conn.execute(
            "SELECT id, claude_review FROM trials "
            "WHERE spec_hash = ? "
            "ORDER BY ts_created DESC, id DESC LIMIT 1;",
            (spec_hash,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"no matching row for spec_hash={spec_hash!r}"
            )
        if row["claude_review"] is not None:
            raise ValueError(
                f"review already present on most recent trial for "
                f"spec_hash={spec_hash!r} (row id={row['id']})"
            )
        row_id = int(row["id"])
        conn.execute(sql, (review_text, now_ts, review_model, row_id))
        conn.commit()
        return row_id
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# query — the generic read path
# --------------------------------------------------------------------------- #


def query(
    *,
    spec_hash: str | None = None,
    agent_id: str | None = None,
    gate_passed: bool | None = None,
    since_ts: int | None = None,
    limit: int = 1000,
    db_path: Path | str | None = None,
) -> list[TrialRow]:
    """Generic read API. Returns up to ``limit`` matching rows ordered by
    ``ts_created`` DESC (ties broken by ``id`` DESC, so the read is fully
    deterministic when two rows land in the same second).

    All filters are AND-combined. ``None`` means "do not filter on this
    column".
    """
    where_clauses: list[str] = []
    params: list[Any] = []
    if spec_hash is not None:
        where_clauses.append("spec_hash = ?")
        params.append(spec_hash)
    if agent_id is not None:
        where_clauses.append("agent_id = ?")
        params.append(agent_id)
    if gate_passed is not None:
        where_clauses.append("scorer_promotion_gate_passed = ?")
        params.append(1 if gate_passed else 0)
    if since_ts is not None:
        where_clauses.append("ts_created >= ?")
        params.append(int(since_ts))
    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    sql = (
        f"SELECT {_TRIAL_ROW_SELECT} FROM trials "
        f"{where_sql} "
        f"ORDER BY ts_created DESC, id DESC LIMIT ?"
    )
    params.append(int(limit))

    conn = connect(db_path)
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    return [_row_to_trial(r) for r in rows]


# --------------------------------------------------------------------------- #
# count_total_trials — the DSR denominator
# --------------------------------------------------------------------------- #


def count_total_trials(db_path: Path | str | None = None) -> int:
    """Total trials in the registry.

    Used by the deflated-Sharpe-ratio scorer as the trial-count denominator.
    INCLUDES failed trials — that's the whole point of the append-only
    invariant.
    """
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM trials;").fetchone()
    finally:
        conn.close()
    return int(row["n"])


# --------------------------------------------------------------------------- #
# query_promotable_candidates — promotion CLI feed
# --------------------------------------------------------------------------- #


def query_promotable_candidates(
    db_path: Path | str | None = None,
    limit: int = 50,
) -> list[TrialRow]:
    """Trials that passed the scorer gate, lack a Claude review, and have
    not yet been promoted (test_pnl_net IS NULL). Ordered by val_sharpe_net
    DESC so the best-looking candidates surface first.

    The promotion CLI consumes this.
    """
    sql = (
        f"SELECT {_TRIAL_ROW_SELECT} FROM trials "
        "WHERE scorer_promotion_gate_passed = 1 "
        "  AND claude_review IS NULL "
        "  AND test_pnl_net IS NULL "
        "ORDER BY val_sharpe_net DESC, id DESC LIMIT ?"
    )
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    finally:
        conn.close()
    return [_row_to_trial(r) for r in rows]


def query_pending_promotion(
    db_path: Path | str | None = None,
    limit: int = 50,
) -> list[TrialRow]:
    """Trials that passed the scorer gate, have a Claude review, and have
    NOT yet been promoted to test (test_pnl_net IS NULL).

    These are the candidates the promotion CLI ``--list`` shows: the human
    reviewer has already seen Opus's adversarial verdict and must decide
    whether to burn a test-set unlock.

    Ordered by val_sharpe_net DESC so the most promising candidates surface
    first.
    """
    sql = (
        f"SELECT {_TRIAL_ROW_SELECT} FROM trials "
        "WHERE scorer_promotion_gate_passed = 1 "
        "  AND claude_review IS NOT NULL "
        "  AND test_pnl_net IS NULL "
        "ORDER BY val_sharpe_net DESC, id DESC LIMIT ?"
    )
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    finally:
        conn.close()
    return [_row_to_trial(r) for r in rows]


def query_promoted(
    db_path: Path | str | None = None,
    limit: int = 200,
) -> list[TrialRow]:
    """Trials that have been promoted to the test set (test_pnl_net IS NOT NULL).

    Ordered by ts_created DESC. Used by the ``--history`` subcommand.
    """
    sql = (
        f"SELECT {_TRIAL_ROW_SELECT} FROM trials "
        "WHERE test_pnl_net IS NOT NULL "
        "ORDER BY ts_created DESC, id DESC LIMIT ?"
    )
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    finally:
        conn.close()
    return [_row_to_trial(r) for r in rows]


__all__ = [
    "TrialRow",
    "count_total_trials",
    "query",
    "query_pending_promotion",
    "query_promotable_candidates",
    "query_promoted",
    "record_claude_review",
    "record_trial",
]
