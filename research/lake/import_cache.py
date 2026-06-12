"""One-shot, idempotent importer that builds the DuckDB lake from the pkl cache.

Workflow per game:

  1. Read the game's row from ``research/cache/games_v1.parquet`` (transforms
     to lake ``games`` schema, allocates empty ``split`` column).
  2. Read the game's minutes from ``research/cache/minutes_v1.parquet``
     (drops leakage column ``espn_home_winprob`` and odds/derived columns
     before writing the lake ``pbp`` table).
  3. Read the game's substitutions from ``research/cache/subs_v1.parquet``
     (1:1 transform).
  4. Enumerate Kalshi markets for the game and fetch per-minute candles via
     :func:`nba_odds_study.kalshi_hist.fetch_candles`; **preserve yes_bid and
     yes_ask** (the existing ``dataset._fetch_all_odds`` drops them).
  5. Enumerate Polymarket markets and fetch per-minute history.

Idempotency
-----------
The script writes one parquet per game per table at
``market_data/lake/v1/<table>/<game_id>.parquet``. By default an existing
shard is left alone; pass ``--rebuild`` to overwrite. The decision is made
per (game, table) pair, so a failed Kalshi fetch that left no shard will be
retried on the next run without re-doing the PBP/games/subs writes.

CLI
---
::

    python -m research.lake.import_cache             # build all games
    python -m research.lake.import_cache --limit 5   # build first 5
    python -m research.lake.import_cache --rebuild   # overwrite existing
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.nba_odds_study import kalshi_hist, polymarket_hist  # noqa: E402

from research.cache_io import list_cache, PARQUET_DIR  # noqa: E402
from research.lake.schema import (  # noqa: E402
    LAKE_ROOT,
    TABLES,
    column_names,
    table_dir,
)

LOG = logging.getLogger("research.lake.import_cache")

# Same pre/post window the dataset builder uses.
PRE_GAME_SEC = 1800
POST_GAME_SEC = 900


def _floor_min(ts: int) -> int:
    return int(ts) - int(ts) % 60


def _to_date(date_str: str):
    """Convert YYYY-MM-DD string to a date object."""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _write_shard(name: str, game_id: str, df: pd.DataFrame, rebuild: bool) -> bool:
    """Write a single-game parquet shard. Returns True if written, False if skipped.

    Validates that ``df`` has exactly the columns named in
    :data:`research.lake.schema.TABLES`. Extra columns are dropped silently.
    Missing columns are filled with NaN.
    """
    path = table_dir(name) / f"{game_id}.parquet"
    if path.exists() and not rebuild:
        return False
    cols = column_names(name)
    out = pd.DataFrame()
    for c in cols:
        out[c] = df[c] if c in df.columns else pd.Series([None] * len(df), dtype=object)
    # Make the date column a python date (not string) for downstream type-safety.
    if "date" in out.columns and len(out) > 0:
        out["date"] = pd.to_datetime(out["date"]).dt.date
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False, compression="zstd")
    return True


def _load_existing_pbp() -> pd.DataFrame:
    """Load the existing minutes parquet (the source for the lake ``pbp`` table)."""
    p = PARQUET_DIR / "minutes_v1.parquet"
    if not p.exists():
        raise FileNotFoundError(
            f"required source missing: {p}; run research/cache_io.py --build-features first"
        )
    return pd.read_parquet(p)


def _load_existing_games() -> pd.DataFrame:
    p = PARQUET_DIR / "games_v1.parquet"
    if not p.exists():
        raise FileNotFoundError(f"required source missing: {p}")
    return pd.read_parquet(p)


def _load_existing_subs() -> pd.DataFrame:
    p = PARQUET_DIR / "subs_v1.parquet"
    if not p.exists():
        raise FileNotFoundError(f"required source missing: {p}")
    return pd.read_parquet(p)


def build_pbp_shard(minutes_for_game: pd.DataFrame, game_row: pd.Series) -> pd.DataFrame:
    """Transform ``minutes_v1`` rows for one game into the lake ``pbp`` schema.

    Drops ``espn_home_winprob`` (leakage) and all odds/derived columns. The
    surviving columns are pure game-state (score, margin, pace, lineups).
    """
    df = minutes_for_game.copy()
    df["game_id"] = game_row["game_id"]
    df["date"] = game_row["date"]
    df["home_tri"] = game_row["home_tri"]
    df["away_tri"] = game_row["away_tri"]
    # ``minutes_v1`` has minute_ts (int) already; keep it.
    return df


def build_games_shard(game_row: pd.Series) -> pd.DataFrame:
    """Wrap one row of ``games_v1`` with a NULL ``split`` column."""
    out = pd.DataFrame([game_row.to_dict()])
    out["split"] = None
    return out


def build_subs_shard(subs_for_game: pd.DataFrame) -> pd.DataFrame:
    return subs_for_game.copy()


def fetch_kalshi_ticks(date: str, away_tri: str, home_tri: str,
                       first_ts: int, last_ts: int, game_id: str,
                       winner_only: bool = False) -> pd.DataFrame:
    """Re-fetch Kalshi candles for the game; preserve yes_bid / yes_ask.

    Returns an empty DataFrame (with the right columns) if no markets were
    found or no candles were returned. ``winner_only`` restricts to the
    moneyline (KXNBAGAME winner) markets — the only ones the research features
    consume — making a full-season backfill ~3x faster.
    """
    cols = column_names("kalshi_ticks")
    start = first_ts - PRE_GAME_SEC
    end = last_ts + POST_GAME_SEC
    rows: list[dict] = []
    try:
        markets = kalshi_hist.enumerate_markets(date, away_tri, home_tri)
    except Exception as e:
        LOG.warning("kalshi enumerate_markets failed for %s: %s", game_id, e)
        return pd.DataFrame(columns=cols)
    if winner_only:
        markets = [m for m in markets if m.get("kind") == "winner"]
    for m in markets:
        try:
            candles = kalshi_hist.fetch_candles(m["series"], m["ticker"], start, end)
        except Exception as e:
            LOG.warning("kalshi fetch_candles failed for %s/%s: %s",
                        m.get("series"), m.get("ticker"), e)
            continue
        for c in candles:
            ts = c.get("ts")
            if ts is None:
                continue
            rows.append({
                "game_id": game_id,
                "minute_ts": _floor_min(int(ts)),
                "series": m["series"],
                "ticker": m["ticker"],
                "team": m.get("yes_team"),
                "yes_bid": c.get("yes_bid"),
                "yes_ask": c.get("yes_ask"),
                "mid": c.get("mid"),
                "last": c.get("last"),
            })
    df = pd.DataFrame(rows, columns=cols)
    return df


def fetch_polymarket_ticks(date: str, away_tri: str, home_tri: str,
                           first_ts: int, last_ts: int, game_id: str) -> pd.DataFrame:
    """Re-fetch Polymarket per-minute prices for the game.

    Only mid is recorded — the prices-history endpoint doesn't surface bid/ask
    side. For winner markets the ``team`` column is 'home' (Polymarket's
    primary outcome is the home team) — non-winner kinds are not enumerated
    here, but we keep the column shape for them in case the upstream
    enumerator changes.
    """
    cols = column_names("polymarket_ticks")
    start = first_ts - PRE_GAME_SEC
    end = last_ts + POST_GAME_SEC
    rows: list[dict] = []
    try:
        markets = polymarket_hist.enumerate_markets(date, away_tri, home_tri)
    except Exception as e:
        LOG.warning("polymarket enumerate_markets failed for %s: %s", game_id, e)
        return pd.DataFrame(columns=cols)
    for m in markets:
        token_id = m.get("token_id")
        if not token_id:
            continue
        team = "home" if m.get("kind") == "winner" else None
        try:
            history = polymarket_hist.fetch_history(token_id, start, end)
        except Exception as e:
            LOG.warning("polymarket fetch_history failed for %s/%s: %s",
                        game_id, token_id, e)
            continue
        for h in history:
            ts = h.get("ts")
            if ts is None:
                continue
            rows.append({
                "game_id": game_id,
                "minute_ts": _floor_min(int(ts)),
                "token_id": token_id,
                "team": team,
                "mid": h.get("p"),
            })
    df = pd.DataFrame(rows, columns=cols)
    return df


def import_one(game_row: pd.Series,
               minutes_for_game: pd.DataFrame,
               subs_for_game: pd.DataFrame,
               rebuild: bool,
               refetch_empty_kalshi: bool = False,
               kalshi_winner_only: bool = False) -> dict:
    """Build all five lake shards for one game.

    Returns a dict ``{table: 'written' | 'skipped' | 'failed'}``.

    ``refetch_empty_kalshi``: when True, an existing but EMPTY kalshi shard is
    re-fetched (via the historical-tier fallback) instead of being skipped —
    the surgical way to backfill the ~97% of games whose original live
    enumeration returned nothing, without rebuilding PM/pbp/subs.
    """
    game_id = game_row["game_id"]
    status: dict[str, str] = {}

    # --- pbp / games / subs are pure pandas transforms over existing parquets ---
    try:
        pbp_df = build_pbp_shard(minutes_for_game, game_row)
        status["pbp"] = "written" if _write_shard("pbp", game_id, pbp_df, rebuild) else "skipped"
    except Exception as e:
        LOG.exception("pbp shard failed for %s: %s", game_id, e)
        status["pbp"] = "failed"

    try:
        games_df = build_games_shard(game_row)
        status["games"] = "written" if _write_shard("games", game_id, games_df, rebuild) else "skipped"
    except Exception as e:
        LOG.exception("games shard failed for %s: %s", game_id, e)
        status["games"] = "failed"

    try:
        subs_df = build_subs_shard(subs_for_game)
        status["subs"] = "written" if _write_shard("subs", game_id, subs_df, rebuild) else "skipped"
    except Exception as e:
        LOG.exception("subs shard failed for %s: %s", game_id, e)
        status["subs"] = "failed"

    # --- kalshi / polymarket require network — only fetch if shard missing ---
    kalshi_shard = table_dir("kalshi_ticks") / f"{game_id}.parquet"
    kalshi_rows = -1
    if kalshi_shard.exists():
        try:
            kalshi_rows = len(pd.read_parquet(kalshi_shard, columns=["minute_ts"]))
        except Exception:
            kalshi_rows = -1
    # Skip only when a NON-EMPTY shard already exists (and we're not
    # rebuilding). An existing EMPTY shard is re-fetched when
    # refetch_empty_kalshi is set, so the historical-tier recovery can backfill
    # games whose original live enumeration returned nothing.
    skip_kalshi = (
        kalshi_shard.exists() and not rebuild
        and (kalshi_rows > 0 or not refetch_empty_kalshi)
    )
    if skip_kalshi:
        status["kalshi_ticks"] = "skipped"
    else:
        try:
            df = fetch_kalshi_ticks(
                game_row["date"], game_row["away_tri"], game_row["home_tri"],
                int(game_row["first_ts"]), int(game_row["last_ts"]), game_id,
                winner_only=kalshi_winner_only,
            )
            _write_shard("kalshi_ticks", game_id, df, rebuild=True)
            status["kalshi_ticks"] = f"written ({len(df)} rows)"
        except Exception as e:
            LOG.exception("kalshi_ticks shard failed for %s: %s", game_id, e)
            status["kalshi_ticks"] = "failed"

    pm_shard = table_dir("polymarket_ticks") / f"{game_id}.parquet"
    if pm_shard.exists() and not rebuild:
        status["polymarket_ticks"] = "skipped"
    else:
        try:
            df = fetch_polymarket_ticks(
                game_row["date"], game_row["away_tri"], game_row["home_tri"],
                int(game_row["first_ts"]), int(game_row["last_ts"]), game_id,
            )
            _write_shard("polymarket_ticks", game_id, df, rebuild=True)
            status["polymarket_ticks"] = f"written ({len(df)} rows)"
        except Exception as e:
            LOG.exception("polymarket_ticks shard failed for %s: %s", game_id, e)
            status["polymarket_ticks"] = "failed"

    return status


def run(limit: int | None = None, rebuild: bool = False,
        refetch_empty_kalshi: bool = False,
        kalshi_winner_only: bool = False) -> dict:
    """Import games into the lake. Returns a summary dict for logging."""
    t0 = time.time()

    LOG.info("loading source parquets …")
    minutes_all = _load_existing_pbp()
    games_all = _load_existing_games()
    subs_all = _load_existing_subs()

    # Index subs / minutes by game_id for fast per-game lookup.
    minutes_by_game = {gid: df for gid, df in minutes_all.groupby("game_id")}
    subs_by_game = {gid: df for gid, df in subs_all.groupby("game_id")}

    # Game ordering: chronological so a re-run after time T continues forward
    # rather than re-checking the early games.
    games_all = games_all.sort_values(["date", "game_id"]).reset_index(drop=True)
    if limit is not None:
        games_all = games_all.head(limit)

    LOG.info("importing %d games (rebuild=%s) …", len(games_all), rebuild)

    summary = {"n_games": 0, "kalshi_with_data": 0, "kalshi_empty": 0,
               "polymarket_with_data": 0, "polymarket_empty": 0,
               "errors": [], "elapsed_sec": 0.0}

    for i, (_, row) in enumerate(games_all.iterrows()):
        gid = row["game_id"]
        minutes_g = minutes_by_game.get(gid, pd.DataFrame())
        subs_g = subs_by_game.get(gid, pd.DataFrame())
        if minutes_g.empty:
            LOG.warning("%s has no minutes; skipping (cache_io missed it)", gid)
            summary["errors"].append({"game_id": gid, "err": "no minutes"})
            continue
        status = import_one(row, minutes_g, subs_g, rebuild,
                            refetch_empty_kalshi, kalshi_winner_only)
        summary["n_games"] += 1
        k = status.get("kalshi_ticks", "")
        p = status.get("polymarket_ticks", "")
        if k.startswith("written (0"):
            summary["kalshi_empty"] += 1
        elif k.startswith("written ("):
            summary["kalshi_with_data"] += 1
        if p.startswith("written (0"):
            summary["polymarket_empty"] += 1
        elif p.startswith("written ("):
            summary["polymarket_with_data"] += 1
        LOG.info("[%d/%d] %s → %s", i + 1, len(games_all), gid, status)

    summary["elapsed_sec"] = time.time() - t0
    LOG.info(
        "done: %d games | kalshi data on %d (empty %d) | polymarket data on %d (empty %d) | %.1fs",
        summary["n_games"],
        summary["kalshi_with_data"], summary["kalshi_empty"],
        summary["polymarket_with_data"], summary["polymarket_empty"],
        summary["elapsed_sec"],
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N games (chronological).")
    ap.add_argument("--rebuild", action="store_true",
                    help="Overwrite existing parquet shards instead of skipping.")
    ap.add_argument("--refetch-empty-kalshi", action="store_true",
                    help="Re-fetch kalshi shards that exist but are EMPTY (uses "
                         "the historical-tier fallback to backfill recovery).")
    ap.add_argument("--kalshi-winner-only", action="store_true",
                    help="Fetch only the moneyline (KXNBAGAME winner) Kalshi "
                         "markets — the ones the research features use; ~3x faster.")
    ap.add_argument("--log-level", default="INFO",
                    help="Logging level (default INFO).")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    run(limit=args.limit, rebuild=args.rebuild,
        refetch_empty_kalshi=args.refetch_empty_kalshi,
        kalshi_winner_only=args.kalshi_winner_only)


if __name__ == "__main__":
    main()
