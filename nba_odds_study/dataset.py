"""Join NBA play-by-play with Kalshi/Polymarket odds into one per-minute table.

Everything is keyed on wall-clock time (UTC), floored to the minute — the
binding resolution of the historical odds endpoints. NBA events (scores, subs)
are aggregated into the same minute bins, and on-court lineups are read from the
per-player intervals reconstructed in :mod:`espn`.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import espn, kalshi_hist
from . import polymarket_hist as pm

RUN_THRESHOLD = 8  # unanswered points to count as a scoring run
PRE_GAME_SEC = 1800
POST_GAME_SEC = 900


@dataclass
class Dataset:
    game: espn.GameData
    minute: pd.DataFrame
    odds_long: pd.DataFrame
    subs: list[dict]
    lead_changes: list[int]
    runs: list[dict]


def _floor_min(ts: int) -> int:
    return int(ts) - int(ts) % 60


def _period_base(period: int) -> int:
    return (period - 1) * 720 if period <= 4 else 4 * 720 + (period - 5) * 300


def _elapsed_game_sec(period, clock: str):
    if not period or not clock:
        return None
    try:
        parts = clock.split(":")
        rem = int(parts[0]) * 60 + float(parts[1]) if len(parts) == 2 else float(parts[0])
    except ValueError:
        return None
    qlen = 720 if period <= 4 else 300
    return _period_base(period) + (qlen - rem)


def _series_to_minute(rows, value_key, index) -> pd.Series:
    if not rows:
        return pd.Series(np.nan, index=index, dtype=float)
    s = pd.Series(
        [r[value_key] for r in rows],
        index=[_floor_min(r["ts"]) for r in rows],
        dtype=float,
    )
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.reindex(index).ffill()


def _fetch_all_odds(km, pmk, start, end):
    """Fetch every market's price history concurrently; return long-format rows."""
    rows = []

    def k_job(d):
        out = []
        try:
            candles = kalshi_hist.fetch_candles(d["series"], d["ticker"], start, end)
        except Exception:  # noqa: BLE001
            # Best-effort per market: one throttled candle fetch must not sink
            # the whole game. The PM leg still carries the win-prob signal.
            return out
        for c in candles:
            if c["mid"] is None:
                continue
            out.append({"ts": c["ts"], "platform": "kalshi", "kind": d["kind"],
                        "key": d["ticker"], "team": d.get("yes_team"),
                        "strike": d.get("strike"), "prob": c["mid"]})
        return out

    def p_job(d):
        out = []
        team = None
        if d["kind"] == "winner":
            team = "home"  # primary outcome is the home team
        for h in pm.fetch_history(d["token_id"], start, end):
            out.append({"ts": h["ts"], "platform": "polymarket", "kind": d["kind"],
                        "key": d["token_id"], "team": team,
                        "strike": d.get("strike"), "prob": h["p"]})
        return out

    with ThreadPoolExecutor(max_workers=3) as ex:  # Kalshi rate-limits aggressively
        for res in ex.map(k_job, km):
            rows.extend(res)
    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(p_job, pmk):
            rows.extend(res)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["minute"] = (df["ts"] // 60 * 60).astype(int)
    return df


def _detect_runs(scoring_plays, home_tri, away_tri):
    runs, cur = [], None
    for p in scoring_plays:
        team = p["team"]
        if team not in (home_tri, away_tri):
            continue
        if cur and cur["team"] == team:
            cur["points"] += p["score_value"]
            cur["end_ts"] = p["ts"]
        else:
            if cur and cur["points"] >= RUN_THRESHOLD:
                runs.append(cur)
            cur = {"team": team, "points": p["score_value"],
                   "start_ts": p["ts"], "end_ts": p["ts"]}
    if cur and cur["points"] >= RUN_THRESHOLD:
        runs.append(cur)
    return runs


def _detect_lead_changes(scoring_plays):
    changes, last_sign = [], 0
    for p in scoring_plays:
        margin = p["home_score"] - p["away_score"]
        sign = (margin > 0) - (margin < 0)
        if sign != 0 and last_sign != 0 and sign != last_sign:
            changes.append(p["ts"])
        if sign != 0:
            last_sign = sign
    return changes


def build(date: str, away_tri: str, home_tri: str, kinds: set[str] | None = None,
          platforms: set[str] | None = None, espn_id: str | None = None) -> Dataset:
    eid = espn_id or espn.resolve_event(date, away_tri, home_tri)
    g = espn.fetch_game(eid, date)
    start, end = g.first_ts - PRE_GAME_SEC, g.last_ts + POST_GAME_SEC

    if platforms is None or "kalshi" in platforms:
        try:
            km = kalshi_hist.enumerate_markets(date, away_tri, home_tri)
        except Exception:  # noqa: BLE001
            # Kalshi best-effort: a throttled/missing historical tier must not
            # sink a game Polymarket can still price. kalshi_home_winprob is
            # empty for ~all 2025-26 games anyway; PM is the primary source.
            km = []
    else:
        km = []
    pmk = pm.enumerate_markets(date, away_tri, home_tri) \
        if platforms is None or "polymarket" in platforms else []
    if kinds:
        km = [d for d in km if d["kind"] in kinds]
        pmk = [d for d in pmk if d["kind"] in kinds]
    odds = _fetch_all_odds(km, pmk, start, end)

    # ---- minute grid ----
    lo = _floor_min(min([start] + ([int(odds["ts"].min())] if not odds.empty else [])))
    hi = _floor_min(g.last_ts + 60)
    index = list(range(lo, hi + 60, 60))
    df = pd.DataFrame(index=index)
    df.index.name = "minute"
    df["dt"] = pd.to_datetime(df.index, unit="s", utc=True)
    tip_min = _floor_min(g.first_ts)
    df["game_min"] = (df.index - tip_min) / 60.0

    # ---- score / margin / total / pace ----
    sp = [p for p in g.plays if p["home_score"] is not None]
    score_rows = [{"ts": p["ts"], "home": p["home_score"], "away": p["away_score"]} for p in sp]
    df["home_score"] = _series_to_minute(score_rows, "home", index)
    df["away_score"] = _series_to_minute(score_rows, "away", index)
    df[["home_score", "away_score"]] = df[["home_score", "away_score"]].ffill().fillna(0)
    df["margin"] = df["home_score"] - df["away_score"]
    df["total"] = df["home_score"] + df["away_score"]
    df["pts_home"] = df["home_score"].diff().clip(lower=0).fillna(0)
    df["pts_away"] = df["away_score"].diff().clip(lower=0).fillna(0)

    elapsed_rows = [{"ts": p["ts"], "v": _elapsed_game_sec(p["period"], p["clock"])}
                    for p in g.plays if _elapsed_game_sec(p["period"], p["clock"])]
    df["elapsed_game_sec"] = _series_to_minute(elapsed_rows, "v", index)
    df["proj_total"] = df["total"] * 2880.0 / df["elapsed_game_sec"].where(df["elapsed_game_sec"] > 120)

    # ---- events: subs, lead changes, runs ----
    star_names = set(g.stars[home_tri]) | set(g.stars[away_tri])
    subs = []
    for p in g.subs:
        subs.append({"ts": p["ts"], "team": p["team"], "player_in": p["player_in"],
                     "player_out": p["player_out"],
                     "is_star": p["player_in"] in star_names or p["player_out"] in star_names})
    lead_changes = _detect_lead_changes(sp)
    runs = _detect_runs([p for p in g.plays if p["scoring"]], home_tri, away_tri)

    sub_min = pd.Series([_floor_min(s["ts"]) for s in subs]).value_counts()
    df["n_subs"] = sub_min.reindex(index).fillna(0).astype(int)
    star_sub_min = pd.Series([_floor_min(s["ts"]) for s in subs if s["is_star"]]).value_counts()
    df["n_star_subs"] = star_sub_min.reindex(index).fillna(0).astype(int)
    lc_min = pd.Series([_floor_min(t) for t in lead_changes]).value_counts()
    df["lead_changes"] = lc_min.reindex(index).fillna(0).astype(int)

    # ---- on-court stars per minute ----
    for tri in (home_tri, away_tri):
        for star in g.stars[tri]:
            iv = g.on_intervals.get(star, [])
            df[f"on::{star}"] = [int(espn.is_on(iv, ts)) for ts in index]
    df["home_stars_on"] = df[[f"on::{s}" for s in g.stars[home_tri]]].sum(axis=1)
    df["away_stars_on"] = df[[f"on::{s}" for s in g.stars[away_tri]]].sum(axis=1)

    # ---- odds convenience columns (home win prob etc.) ----
    def pick(platform, kind, **eq):
        if odds.empty:
            return []
        m = (odds["platform"] == platform) & (odds["kind"] == kind)
        for k, v in eq.items():
            m &= odds[k] == v
        sub = odds[m].sort_values("ts")
        return [{"ts": r.ts, "v": r.prob} for r in sub.itertuples()]

    df["kalshi_home_winprob"] = _series_to_minute(pick("kalshi", "winner", team=home_tri), "v", index)
    df["pm_home_winprob"] = _series_to_minute(pick("polymarket", "winner", team="home"), "v", index)
    if g.espn_winprob:
        df["espn_home_winprob"] = _series_to_minute(
            [{"ts": t, "v": p} for t, p in g.espn_winprob], "v", index)

    return Dataset(game=g, minute=df, odds_long=odds, subs=subs,
                   lead_changes=lead_changes, runs=runs)


def in_game(df: pd.DataFrame) -> pd.DataFrame:
    """Rows from tip-off through the final buzzer."""
    return df[(df["game_min"] >= 0) & (df["game_min"] <= df["game_min"].max())]
