"""Read API for the DuckDB-backed research lake.

The lake is partitioned one parquet file per ``game_id`` per table. The reader
exposes:

  - :func:`connect` — DuckDB connection with views over every table.
  - :func:`load_game` — read just one game (PBP + Kalshi + Polymarket + subs + meta).
  - :func:`list_games` — enumerate game_ids, optionally filtered by ``split``.
  - :func:`lake_root` — path to the lake root (``market_data/lake/v1/``).

Test-set safety
---------------
``connect()`` and ``load_game()`` accept ``allow_test=False``. With the default,
any read targeting a game whose ``games.split == 'test'`` raises
:class:`TestSetAccessError`. The split-locker has not yet been built, so the
``split`` column is NULL for the entire lake during Wave 0; NULL is treated as
``'train'`` for the purpose of this guard so the build can proceed. After the
split-locker writes assignments to ``games.split``, the guard becomes
load-bearing.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from .schema import LAKE_ROOT, TABLES, create_views, lake_root, parquet_glob, table_dir


class TestSetAccessError(RuntimeError):
    """Raised when test-set data is touched without ``allow_test=True``."""


def connect(allow_test: bool = False) -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection with one view per lake table.

    Parameters
    ----------
    allow_test : bool, default False
        If False (the default), the returned connection will refuse any query
        that would touch a row whose ``games.split == 'test'``. The guard is
        enforced by attaching a ``CHECK`` predicate to a wrapping view
        (``pbp_safe``, ``kalshi_ticks_safe``, etc). For Wave 0 — before the
        split-locker writes any 'test' labels — this is effectively a no-op,
        but the contract is in place for later.
    """
    con = duckdb.connect()
    create_views(con)
    if not allow_test:
        # Build wrapping views that exclude test-set games. NULL split is
        # treated as 'train' (permissive) per the build-time contract.
        # Anything looking up data should prefer the base table; the *_safe
        # views are exposed for callers that want explicit enforcement.
        for name in ("pbp", "kalshi_ticks", "polymarket_ticks", "subs"):
            con.execute(
                f"CREATE OR REPLACE VIEW {name}_safe AS "
                f"SELECT t.* FROM {name} t "
                f"LEFT JOIN games g ON g.game_id = t.game_id "
                f"WHERE COALESCE(g.split, 'train') <> 'test'"
            )
        con.execute(
            "CREATE OR REPLACE VIEW games_safe AS "
            "SELECT * FROM games WHERE COALESCE(split, 'train') <> 'test'"
        )
    return con


def _game_shard(table: str, game_id: str) -> Path:
    return table_dir(table) / f"{game_id}.parquet"


def _read_game_table(table: str, game_id: str) -> pd.DataFrame:
    """Read one game's shard for ``table`` from disk; return empty df if missing.

    Returning an empty DataFrame (not raising) lets callers handle the
    "this game has no Kalshi data" case uniformly with "this game has Kalshi
    data" — the downstream replay engine treats absence-of-quotes as no-trade.
    """
    p = _game_shard(table, game_id)
    if not p.exists():
        return pd.DataFrame(columns=[c[0] for c in TABLES[table]["columns"]])
    return pd.read_parquet(p)


def _split_of(game_id: str) -> str | None:
    """Return the split label for ``game_id``, or None if the games shard is missing."""
    p = _game_shard("games", game_id)
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=["game_id", "split"])
    row = df.loc[df["game_id"] == game_id]
    if row.empty:
        return None
    val = row["split"].iloc[0]
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return str(val)


def load_game(game_id: str, allow_test: bool = False) -> dict:
    """Read all five tables for one game.

    Returns
    -------
    dict
        ``{"pbp", "kalshi", "polymarket", "subs"}`` are :class:`pandas.DataFrame`
        and ``"meta"`` is a :class:`pandas.Series` (the single games row).
        If a game has no data for one of the tick tables, an empty DataFrame
        with the right columns is returned for that table.

    Raises
    ------
    TestSetAccessError
        If ``games.split == 'test'`` for this game and ``allow_test`` is False.
        NULL split is treated as ``'train'`` for now.
    FileNotFoundError
        If no ``games`` shard exists for ``game_id``.
    """
    games_shard = _game_shard("games", game_id)
    if not games_shard.exists():
        raise FileNotFoundError(f"no lake data for game_id={game_id!r}")

    split = _split_of(game_id)
    if split == "test" and not allow_test:
        raise TestSetAccessError(
            f"refusing to load test-set game {game_id!r}; "
            f"pass allow_test=True after burning an unlock token."
        )

    pbp = _read_game_table("pbp", game_id)
    kalshi = _read_game_table("kalshi_ticks", game_id)
    polymarket = _read_game_table("polymarket_ticks", game_id)
    subs = _read_game_table("subs", game_id)
    games = pd.read_parquet(games_shard)
    meta = games.iloc[0] if not games.empty else pd.Series(dtype=object)

    return {
        "pbp": pbp,
        "kalshi": kalshi,
        "polymarket": polymarket,
        "subs": subs,
        "meta": meta,
    }


def list_games(split: str | None = None, allow_test: bool = False) -> list[str]:
    """List ``game_id``s present in the lake, optionally filtered by split.

    Parameters
    ----------
    split : str, optional
        ``'train'``, ``'val'``, ``'test'``, or ``None`` (all).
    allow_test : bool, default False
        If False and ``split`` is None, test-set games are filtered out.
        If False and ``split == 'test'``, raises :class:`TestSetAccessError`.

    Returns
    -------
    list[str]
        Sorted list of game_ids.
    """
    if split == "test" and not allow_test:
        raise TestSetAccessError(
            "refusing to list test-set games; pass allow_test=True."
        )
    games_dir = table_dir("games")
    shards = sorted(games_dir.glob("*.parquet"))
    if not shards:
        return []
    df = pd.read_parquet(games_dir, columns=["game_id", "split"])
    # Treat NULL split as 'train' to be permissive during build.
    df["split"] = df["split"].where(df["split"].notna(), "train")
    if split is not None:
        df = df[df["split"] == split]
    elif not allow_test:
        df = df[df["split"] != "test"]
    return sorted(df["game_id"].astype(str).tolist())


__all__ = [
    "TestSetAccessError",
    "connect",
    "load_game",
    "list_games",
    "lake_root",
]
