"""DuckDB lake schema for the NBA moneyline research harness.

Defines five parquet-backed external tables under ``market_data/lake/v1/``:

  - ``pbp``              one row per game x wall-clock minute (game state, no leakage)
  - ``kalshi_ticks``     one row per game x minute x Kalshi market (with yes_bid/yes_ask)
  - ``polymarket_ticks`` one row per game x minute x Polymarket token (mid only)
  - ``games``            one row per game (metadata + final score + split label)
  - ``subs``             one row per substitution event

Each table is materialised as one parquet file per ``game_id`` under
``market_data/lake/v1/<table>/<game_id>.parquet`` so DuckDB's parquet glob
reader can prune at the partition level when a query filters by game.

The ``espn_home_winprob`` column is intentionally absent from ``pbp`` — it is
the canonical hard-excluded leakage feature per the plan (Phase 0.6). If you
need a forward-looking probability for evaluation, derive it from the
post-hoc ground-truth (game outcome) in ``games.home_won``; never from
ESPN's win probability stream.

The ``games.split`` column is allocated here but populated by the Wave 0
split-locker (``research/splits/lock.py``). Values: ``'train' | 'val' |
'test' | NULL``. NULL is treated as ``'train'`` by ``reader.py`` until the
split-locker writes assignments.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
LAKE_ROOT = REPO_ROOT / "market_data" / "lake" / "v1"

# Mapping: table name -> dict with parquet glob and column schema (name, duckdb type, nullable).
# The column-list is the source of truth for what we write to parquet in
# ``import_cache.py`` — keep them in sync.
TABLES: dict[str, dict] = {
    "pbp": {
        "subdir": "pbp",
        "columns": [
            ("game_id", "VARCHAR", False),
            ("date", "DATE", False),
            ("home_tri", "VARCHAR", False),
            ("away_tri", "VARCHAR", False),
            ("minute_ts", "BIGINT", False),
            ("elapsed_game_sec", "DOUBLE", True),
            ("home_score", "DOUBLE", True),
            ("away_score", "DOUBLE", True),
            ("margin", "DOUBLE", True),
            ("pts_home", "DOUBLE", True),
            ("pts_away", "DOUBLE", True),
            ("total", "DOUBLE", True),
            ("n_subs", "INTEGER", True),
            ("n_star_subs", "INTEGER", True),
            ("lead_changes", "INTEGER", True),
            ("home_stars_on", "INTEGER", True),
            ("away_stars_on", "INTEGER", True),
        ],
    },
    "kalshi_ticks": {
        "subdir": "kalshi_ticks",
        "columns": [
            ("game_id", "VARCHAR", False),
            ("minute_ts", "BIGINT", False),
            ("series", "VARCHAR", False),
            ("ticker", "VARCHAR", False),
            ("team", "VARCHAR", True),   # tri-code of the side that yes represents; NULL for non-winner
            ("yes_bid", "DOUBLE", True),
            ("yes_ask", "DOUBLE", True),
            ("mid", "DOUBLE", True),
            ("last", "DOUBLE", True),
        ],
    },
    "polymarket_ticks": {
        "subdir": "polymarket_ticks",
        "columns": [
            ("game_id", "VARCHAR", False),
            ("minute_ts", "BIGINT", False),
            ("token_id", "VARCHAR", False),
            ("team", "VARCHAR", True),   # 'home' or 'away' for winner; NULL for non-winner
            ("mid", "DOUBLE", True),
        ],
    },
    "games": {
        "subdir": "games",
        "columns": [
            ("game_id", "VARCHAR", False),
            ("date", "DATE", False),
            ("home_tri", "VARCHAR", False),
            ("away_tri", "VARCHAR", False),
            ("first_ts", "BIGINT", False),
            ("last_ts", "BIGINT", False),
            ("final_home", "DOUBLE", True),
            ("final_away", "DOUBLE", True),
            ("home_won", "BOOLEAN", True),
            ("stars_home", "VARCHAR", True),
            ("stars_away", "VARCHAR", True),
            ("starters_home", "VARCHAR", True),
            ("starters_away", "VARCHAR", True),
            ("n_subs", "INTEGER", True),
            ("n_lead_changes", "INTEGER", True),
            ("n_runs", "INTEGER", True),
            ("has_espn_wp", "BOOLEAN", True),
            ("variant", "VARCHAR", True),
            ("split", "VARCHAR", True),  # 'train' | 'val' | 'test' | NULL (populated by split-locker)
        ],
    },
    "subs": {
        "subdir": "subs",
        "columns": [
            ("game_id", "VARCHAR", False),
            ("date", "DATE", False),
            ("ts", "BIGINT", False),
            ("team", "VARCHAR", False),
            ("player_in", "VARCHAR", True),
            ("player_out", "VARCHAR", True),
            ("is_star", "BOOLEAN", True),
        ],
    },
}


def table_dir(name: str) -> Path:
    """Return the directory holding the parquet shards for ``name``."""
    if name not in TABLES:
        raise KeyError(f"unknown lake table: {name!r}; known: {sorted(TABLES)}")
    return LAKE_ROOT / TABLES[name]["subdir"]


def parquet_glob(name: str) -> str:
    """Return the parquet glob string for the table (used by read_parquet)."""
    return str(table_dir(name) / "*.parquet")


def column_names(name: str) -> list[str]:
    """Return the ordered list of column names for ``name``."""
    return [c[0] for c in TABLES[name]["columns"]]


def create_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create or replace one DuckDB view per table over its parquet glob.

    If a table directory is empty (no parquet files yet), the view is created
    as an empty SELECT with the correct schema so downstream queries don't
    fail with 'no files match glob'. This lets the harness boot before the
    importer has written anything.
    """
    for name, spec in TABLES.items():
        d = table_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        shards = list(d.glob("*.parquet"))
        if shards:
            glob = parquet_glob(name)
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
            )
        else:
            # Empty view with schema preserved so queries don't error.
            cols_sql = ", ".join(
                f"CAST(NULL AS {dt}) AS {nm}" for nm, dt, _ in spec["columns"]
            )
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT {cols_sql} WHERE 1=0"
            )


def lake_root() -> Path:
    """Return ``market_data/lake/v1/``."""
    return LAKE_ROOT
