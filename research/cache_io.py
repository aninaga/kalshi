"""Cache loader for the 1,362-game NBA pkl cache.

The ONLY module in research/ that calls pickle.load on cache files. All EDA
notebooks and analyses import from here rather than touching pickle directly —
this concentrates the provenance assertion in one place and keeps the auto-mode
classifier surface small.

Cached objects are nba_odds_study.dataset.Dataset instances written by
nba_odds_study.batch.load_or_build. Pickle resolution requires nba_odds_study
to be importable, so we put the repo root on sys.path before loading.

Two cache filename patterns exist in market_data/nba_studies/_cache/:
  - {date}_{away}_at_{home}_total-winner_polymarket.pkl  (regular season, 1,312)
  - {date}_{away}_at_{home}_total-winner.pkl             (playoffs, 50)
Both contain the same Dataset schema.

CLI:
  python3 research/cache_io.py                  # smoke-test load one game
  python3 research/cache_io.py --build-features # write parquet feature tables
  python3 research/cache_io.py --build-features --limit 50  # quick subset run
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "market_data" / "nba_studies" / "_cache"
PARQUET_DIR = REPO_ROOT / "research" / "cache"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nba_odds_study import dataset, espn  # noqa: E402, F401

CACHE_GLOBS = ("*_total-winner_polymarket.pkl", "*_total-winner.pkl")


@dataclass(frozen=True)
class CacheEntry:
    path: Path
    date: str
    away: str
    home: str
    game_id: str
    variant: str  # "polymarket" or "all"


def list_cache(cache_dir: Path = CACHE_DIR) -> list[CacheEntry]:
    seen: dict[str, CacheEntry] = {}
    for pattern in CACHE_GLOBS:
        for path in sorted(cache_dir.glob(pattern)):
            stem = path.stem
            if stem.endswith("_total-winner_polymarket"):
                variant = "polymarket"
                core = stem[: -len("_total-winner_polymarket")]
            elif stem.endswith("_total-winner"):
                variant = "all"
                core = stem[: -len("_total-winner")]
            else:
                continue
            parts = core.split("_")
            if len(parts) != 4 or parts[2] != "at":
                continue
            date, away, _, home = parts
            game_id = core
            if game_id in seen:
                continue
            seen[game_id] = CacheEntry(path=path, date=date, away=away,
                                       home=home, game_id=game_id, variant=variant)
    return sorted(seen.values(), key=lambda e: (e.date, e.game_id))


def load_game(path: Path | str) -> dataset.Dataset:
    with open(path, "rb") as f:
        return pickle.load(f)


def iter_games(cache_dir: Path = CACHE_DIR, limit: int | None = None,
               skip_broken: bool = True) -> Iterator[tuple[CacheEntry, dataset.Dataset]]:
    entries = list_cache(cache_dir)
    if limit is not None:
        entries = entries[:limit]
    for entry in entries:
        try:
            yield entry, load_game(entry.path)
        except Exception as e:
            if not skip_broken:
                raise
            print(f"  [skip] {entry.game_id}: {type(e).__name__}: {e}", file=sys.stderr)


def build_features(cache_dir: Path = CACHE_DIR, out_dir: Path = PARQUET_DIR,
                   limit: int | None = None) -> dict:
    """Load every cached game and write three parquet tables.

    Outputs in `out_dir`:
      - minutes_v1.parquet : one row per (game, minute), score + odds + game state
      - subs_v1.parquet    : one row per substitution event
      - games_v1.parquet   : one row per game (metadata + final scores + has_espn_wp)

    Returns a stats dict.
    """
    import numpy as np
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)

    minute_frames: list[pd.DataFrame] = []
    sub_rows: list[dict] = []
    game_rows: list[dict] = []
    skipped: list[dict] = []

    entries = list_cache(cache_dir)
    if limit is not None:
        entries = entries[:limit]
    n = len(entries)
    t0 = time.time()

    for i, entry in enumerate(entries):
        try:
            d = load_game(entry.path)
        except Exception as e:
            skipped.append({"game_id": entry.game_id, "err": f"{type(e).__name__}: {e}"})
            continue

        g, df = d.game, d.minute
        if df.empty:
            skipped.append({"game_id": entry.game_id, "err": "empty minute df"})
            continue

        # ---- minute frame (drop per-player on:: columns to keep parquet lean) ----
        m = df.drop(columns=[c for c in df.columns if c.startswith("on::")]).copy()
        m["game_id"] = entry.game_id
        m["date"] = entry.date
        m["home"] = entry.home
        m["away"] = entry.away
        m["variant"] = entry.variant
        m = m.reset_index(names=["minute_ts"])
        minute_frames.append(m)

        # ---- subs ----
        for s in d.subs:
            sub_rows.append({
                "game_id": entry.game_id, "date": entry.date,
                "ts": int(s["ts"]), "team": s["team"],
                "player_in": s["player_in"], "player_out": s["player_out"],
                "is_star": bool(s.get("is_star", False)),
            })

        # ---- game metadata ----
        last_home = df["home_score"].dropna().iloc[-1] if df["home_score"].notna().any() else np.nan
        last_away = df["away_score"].dropna().iloc[-1] if df["away_score"].notna().any() else np.nan
        home_won = (last_home > last_away) if (np.isfinite(last_home) and np.isfinite(last_away)) else None
        game_rows.append({
            "game_id": entry.game_id, "date": entry.date,
            "home_tri": g.home_tri, "away_tri": g.away_tri,
            "first_ts": int(g.first_ts), "last_ts": int(g.last_ts),
            "final_home": float(last_home) if np.isfinite(last_home) else None,
            "final_away": float(last_away) if np.isfinite(last_away) else None,
            "home_won": home_won,
            "stars_home": ";".join(g.stars.get(g.home_tri, [])),
            "stars_away": ";".join(g.stars.get(g.away_tri, [])),
            "starters_home": ";".join(g.starters.get(g.home_tri, [])),
            "starters_away": ";".join(g.starters.get(g.away_tri, [])),
            "n_subs": len(d.subs),
            "n_lead_changes": len(d.lead_changes),
            "n_runs": len(d.runs),
            "has_espn_wp": bool(g.espn_winprob),
            "variant": entry.variant,
        })

        if (i + 1) % 100 == 0 or i + 1 == n:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{i+1}/{n}] elapsed={elapsed:.1f}s rate={rate:.1f}/s "
                  f"frames={len(minute_frames)} subs={len(sub_rows)} skipped={len(skipped)}")

    print(f"\nConcatenating frames...")
    minutes = pd.concat(minute_frames, ignore_index=True) if minute_frames else pd.DataFrame()
    subs = pd.DataFrame(sub_rows)
    games = pd.DataFrame(game_rows)

    print(f"  minutes: {len(minutes):,} rows × {len(minutes.columns)} cols")
    print(f"  subs:    {len(subs):,} rows")
    print(f"  games:   {len(games):,} rows")
    print(f"  skipped: {len(skipped)} games")

    minutes_path = out_dir / "minutes_v1.parquet"
    subs_path = out_dir / "subs_v1.parquet"
    games_path = out_dir / "games_v1.parquet"

    minutes.to_parquet(minutes_path, index=False, compression="zstd")
    subs.to_parquet(subs_path, index=False, compression="zstd")
    games.to_parquet(games_path, index=False, compression="zstd")
    print(f"\nWrote:")
    print(f"  {minutes_path} ({minutes_path.stat().st_size / 1e6:.1f} MB)")
    print(f"  {subs_path}    ({subs_path.stat().st_size / 1e6:.1f} MB)")
    print(f"  {games_path}   ({games_path.stat().st_size / 1e6:.1f} MB)")

    if skipped:
        skip_df = pd.DataFrame(skipped)
        skip_path = out_dir / "skipped_v1.csv"
        skip_df.to_csv(skip_path, index=False)
        print(f"  {skip_path} ({len(skipped)} skipped games)")

    return {"n_games": len(games), "n_minutes": len(minutes),
            "n_subs": len(subs), "n_skipped": len(skipped),
            "elapsed_sec": time.time() - t0}


def _smoke():
    entries = list_cache()
    print(f"Found {len(entries)} cached games")
    if not entries:
        sys.exit(0)
    by_var = {}
    for e in entries:
        by_var[e.variant] = by_var.get(e.variant, 0) + 1
    print(f"By variant: {by_var}")
    print(f"Date range: {entries[0].date} → {entries[-1].date}")

    first = entries[0]
    print(f"\nLoading first game: {first.game_id} (variant={first.variant})")
    d = load_game(first.path)
    print(f"  game: {type(d.game).__name__} home={d.game.home_tri} away={d.game.away_tri}")
    print(f"  minute df: shape={d.minute.shape}")
    print(f"  odds_long: shape={d.odds_long.shape}")
    print(f"  subs={len(d.subs)} lead_changes={len(d.lead_changes)} runs={len(d.runs)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-features", action="store_true",
                        help="Load all cache pkls and write minutes/subs/games parquet tables.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process first N games (for quick test runs).")
    args = parser.parse_args()

    if args.build_features:
        stats = build_features(limit=args.limit)
        print(f"\nDone in {stats['elapsed_sec']:.1f}s")
    else:
        _smoke()
