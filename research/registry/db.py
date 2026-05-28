"""SQLite schema and connection management for the trial registry.

This module owns the on-disk format of ``market_data/trials.db`` and the
single-statement migration that brings a fresh database up to that schema.

Append-only invariant
---------------------
The registry is the source of truth for every backtest the autoresearch
system ever runs. The DSR scorer's denominator is the total number of
trials, so any silent UPDATE/DELETE path would let the orchestrator
"forget" failing trials and overstate significance. This module therefore
DOES NOT expose any UPDATE/DELETE helpers — the only sanctioned mutation
is :func:`research.registry.api.record_claude_review`, which writes the
four ``claude_review*`` columns and nothing else.

WAL journaling is enabled in :func:`connect` so Phase 1 Codex worker
subprocesses can write while the orchestrator reads aggregate stats.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


# The canonical database path, relative to the project root (cwd at runtime).
DEFAULT_DB_PATH = Path("market_data/trials.db")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

# Single source of truth for the trials table. Edit here, not in api.py.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_name TEXT NOT NULL,
    features_used TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    parent_trial_id INTEGER,
    train_pnl_gross REAL,
    train_pnl_net REAL,
    train_sharpe_net REAL,
    train_n_trades INTEGER,
    train_win_rate REAL,
    val_pnl_gross REAL,
    val_pnl_net REAL,
    val_sharpe_net REAL,
    val_n_trades INTEGER,
    val_win_rate REAL,
    test_pnl_gross REAL,
    test_pnl_net REAL,
    test_sharpe_net REAL,
    test_n_trades INTEGER,
    test_win_rate REAL,
    scorer_block_bootstrap_ci_lo REAL,
    scorer_block_bootstrap_ci_hi REAL,
    scorer_dsr REAL,
    scorer_dsr_pvalue REAL,
    scorer_promotion_gate_passed INTEGER,
    scorer_promotion_gate_reasons TEXT,
    mechanistic_writeup TEXT,
    claude_review TEXT,
    claude_review_ts INTEGER,
    claude_review_model TEXT,
    codex_worker_id TEXT,
    cost_usd_estimate REAL,
    ts_created INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_trials_spec_hash ON trials(spec_hash);",
    "CREATE INDEX IF NOT EXISTS ix_trials_ts ON trials(ts_created);",
    "CREATE INDEX IF NOT EXISTS ix_trials_gate ON trials(scorer_promotion_gate_passed);",
)


# Ordered tuple of columns that MUST exist (and must match by name) for an
# existing database to be considered compatible. We refuse to auto-migrate;
# Phase 0 ships with no ALTER TABLE infrastructure.
EXPECTED_COLUMNS: tuple[str, ...] = (
    "id",
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
    "claude_review",
    "claude_review_ts",
    "claude_review_model",
    "codex_worker_id",
    "cost_usd_estimate",
    "ts_created",
)


# --------------------------------------------------------------------------- #
# Connection helper
# --------------------------------------------------------------------------- #


def _resolve_db_path(db_path: Path | str | None) -> Path:
    """Normalise the user-supplied path argument to a ``Path``."""
    if db_path is None:
        return DEFAULT_DB_PATH
    return Path(db_path)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection to the trial registry.

    Default path is ``market_data/trials.db`` (relative to the cwd, which
    is the project root at runtime). The parent directory is created if
    missing.

    On first call against a fresh file, the trials table and its indices
    are created. On subsequent calls against an existing file we compare
    the column set to :data:`EXPECTED_COLUMNS` and raise ``RuntimeError``
    if they diverge — Phase 0 has no ALTER TABLE infrastructure, so any
    schema drift requires a manual migration.

    ``PRAGMA journal_mode=WAL`` is set on every connection so Phase 1
    Codex worker subprocesses can write while the orchestrator reads.
    """
    path = _resolve_db_path(db_path)
    # Ensure parent directory exists — the registry is a Phase 0 artefact
    # and may run before any other code touches market_data/.
    path.parent.mkdir(parents=True, exist_ok=True)

    # ``isolation_level=None`` would put sqlite3 in autocommit mode; we WANT
    # the default deferred-transaction behaviour so callers control commits.
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    # WAL: enables concurrent reads during writes. Idempotent — sqlite returns
    # the current mode if already set. We discard the row but keep the cursor
    # close call cheap.
    conn.execute("PRAGMA journal_mode=WAL;").fetchone()

    # Schema bootstrap / check. Done in a short transaction so concurrent
    # callers don't race the CREATE.
    _ensure_schema(conn)

    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the trials table if absent, or verify it matches expectations.

    Raises ``RuntimeError("schema mismatch — manual migration required")``
    if an existing trials table has a different column set than
    :data:`EXPECTED_COLUMNS`.
    """
    # Cheap probe: does the table exist?
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trials';"
    ).fetchone()

    if row is None:
        # Fresh file — create everything.
        conn.execute(SCHEMA_SQL)
        for stmt in INDEX_SQL:
            conn.execute(stmt)
        conn.commit()
        return

    # Existing table — verify columns match. ``PRAGMA table_info`` returns
    # one row per column with ordinal in field 0 and name in field 1.
    info = conn.execute("PRAGMA table_info(trials);").fetchall()
    actual = tuple(r["name"] for r in info)
    if actual != EXPECTED_COLUMNS:
        raise RuntimeError(
            "schema mismatch — manual migration required. "
            f"expected columns {EXPECTED_COLUMNS}, "
            f"found {actual}"
        )
    # Indices are idempotent — make sure they exist even on an old DB that
    # was created before we added one of them.
    for stmt in INDEX_SQL:
        conn.execute(stmt)
    conn.commit()


__all__ = [
    "DEFAULT_DB_PATH",
    "EXPECTED_COLUMNS",
    "INDEX_SQL",
    "SCHEMA_SQL",
    "connect",
]
