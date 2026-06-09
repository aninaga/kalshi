"""research.lab.data — Panel loaders (Unit 1).

Build one :class:`research.lab.types.Panel` per cached game per market
(``winner`` / ``total`` / ``spread``) from the on-disk study cache, reusing
``nba_odds_study`` exactly as ``research/scripts/totals_alpha.py`` and
``spread_alpha.py`` read it.

Design notes
------------
* **Cache-only.** We never trigger a live build (no ESPN / Kalshi / Polymarket
  network calls). The game list is enumerated from the cached pkl *filenames*
  in ``nba_odds_study.batch.CACHE_DIR`` — calling ``schedule.completed_games``
  would hit the network, and games whose cache is absent are simply skipped.
* **Realistic execution substrate.** Each Panel carries the *real* per-minute
  strike ladder (``Panel.ladder``: strike -> P(over/cover) array) so downstream
  ``lab.execution`` fills at a real quoted price, never an assumed 0.50.
* **Split filtering** via ``market_data/splits.json``. ``split`` may be
  ``"train"``/``"val"``/``"test"``/``"nontest"``/``None``. The TEST split is
  loaded ONLY when ``split == "test"`` is passed explicitly; every other value
  (including ``None``) excludes test ids.

Public API (see ``research/lab/CONTRACT.md`` §lab/data.py)::

    load_panels(market, split=None, *, start, end, limit) -> list[Panel]
    load_panel(game, market) -> Panel | None
    available(market) -> int
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from research.lab.types import SPREAD, TOTAL, WINNER, Panel

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# nba_odds_study is imported lazily inside the functions that need it so this
# module imports cleanly in a worktree where the package's heavier deps (e.g.
# matplotlib pulled in by analysis) might be unavailable; see _study().

_SPLITS_PATH = _ROOT / "market_data" / "splits.json"

# Each market is cached under its own KINDS tag; the pkl filename is
# "{date}_{away}_at_{home}_{kinds}_{ptag}.pkl" with kinds sorted-joined and the
# default ptag "all" (see nba_odds_study.batch.load_or_build).
_MARKET_KINDS = {WINNER: {"winner"}, TOTAL: {"total"}, SPREAD: {"spread"}}
_DEFAULT_START = "2025-10-21"
_DEFAULT_END = "2026-06-08"

# Filename: <date>_<away>_at_<home>_<kindtag>_all.pkl
_CACHE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<away>[A-Z]+)_at_(?P<home>[A-Z]+)_"
    r"(?P<kind>[a-z-]+)_all\.pkl$"
)


# --------------------------------------------------------------------------- #
# study-package access (lazy)
# --------------------------------------------------------------------------- #
def _study():
    """Return ``(batch, analysis)`` from nba_odds_study, imported lazily."""
    from nba_odds_study import analysis, batch  # noqa: PLC0415

    return batch, analysis


def _cache_dir() -> str:
    from nba_odds_study import batch  # noqa: PLC0415

    return batch.CACHE_DIR


# --------------------------------------------------------------------------- #
# splits
# --------------------------------------------------------------------------- #
def _gid_of(game: dict) -> str:
    """Canonical game id used by splits.json: ``<date>_<away>_at_<home>``."""
    return f"{game['date']}_{game['away']}_at_{game['home']}"


def _load_splits() -> dict:
    if not _SPLITS_PATH.exists():
        return {}
    return json.loads(_SPLITS_PATH.read_text())


def _split_filter(split: str | None):
    """Return ``(predicate, split_of)`` for the requested split.

    ``predicate(gid) -> bool`` decides inclusion; ``split_of(gid) -> str`` labels
    a game for ``Panel.split``. TEST ids are excluded unless ``split == "test"``.
    """
    splits = _load_splits()
    train = set(splits.get("train_game_ids", []))
    val = set(splits.get("val_game_ids", []))
    test = set(splits.get("test_game_ids", []))

    def split_of(gid: str) -> str:
        if gid in train:
            return "train"
        if gid in val:
            return "val"
        if gid in test:
            return "test"
        return "unknown"

    if split is None:
        # No split filter requested, but NEVER leak the test split implicitly.
        return (lambda gid: gid not in test), split_of
    s = split.lower()
    if s == "train":
        return (lambda gid: gid in train), split_of
    if s == "val":
        return (lambda gid: gid in val), split_of
    if s == "test":
        return (lambda gid: gid in test), split_of
    if s == "nontest":
        return (lambda gid: gid not in test), split_of
    raise ValueError(
        f"unknown split {split!r}; expected one of "
        "'train'/'val'/'test'/'nontest'/None"
    )


# --------------------------------------------------------------------------- #
# cache enumeration (cache-only; no network)
# --------------------------------------------------------------------------- #
def _cached_games(market: str) -> list[dict]:
    """Games with an on-disk pkl for ``market``, parsed from cache filenames.

    Returns ``[{date, away, home}]`` in (date, away, home) order. This avoids
    the network call ``schedule.completed_games`` makes — we only ever surface
    games whose cache already exists.
    """
    if market not in _MARKET_KINDS:
        raise ValueError(f"unknown market {market!r}; expected one of {sorted(_MARKET_KINDS)}")
    kindtag = "-".join(sorted(_MARKET_KINDS[market]))
    cdir = _cache_dir()
    if not os.path.isdir(cdir):
        return []
    games: list[dict] = []
    for fn in os.listdir(cdir):
        m = _CACHE_RE.match(fn)
        if not m or m.group("kind") != kindtag:
            continue
        games.append({"date": m.group("date"), "away": m.group("away"),
                      "home": m.group("home")})
    games.sort(key=lambda g: (g["date"], g["away"], g["home"]))
    return games


# --------------------------------------------------------------------------- #
# ladder reconstruction
# --------------------------------------------------------------------------- #
def _total_ladder(odds_long: pd.DataFrame, index) -> dict:
    """strike -> per-minute P(over) array, blended across platforms.

    The total contract "final total > strike" prices P(over). We pivot each
    platform's total ladder to (minute, strike) and average the platforms.
    """
    sub = odds_long[(odds_long.kind == "total") & odds_long.strike.notna()]
    if sub.empty:
        return {}
    sub = sub.copy()
    sub["minute"] = (sub.ts // 60 * 60).astype(int)
    # last quote per (minute, strike) across platforms, then mean across platforms
    piv = (sub.sort_values("ts")
              .groupby(["minute", "strike"]).prob.mean()
              .unstack("strike"))
    piv = piv.reindex(index).ffill()
    ladder: dict = {}
    for strike in piv.columns:
        arr = piv[strike].to_numpy(dtype=float)
        if np.isfinite(arr).sum() == 0:
            continue
        ladder[float(strike)] = arr
    return ladder


_TICKER_TEAM_RE = re.compile(r"([A-Z]+)")


def _kalshi_team(ticker: str):
    if not ticker:
        return None
    suf = str(ticker).rsplit("-", 1)[-1]
    m = _TICKER_TEAM_RE.match(suf)
    return m.group(1) if m else None


def _spread_ladder(odds_long: pd.DataFrame, home_tri: str, away_tri: str, index):
    """Return ``(ladder, implied_margin)`` for the spread book.

    ``ladder`` maps signed home strike -> per-minute P(home_margin > strike).
    ``implied_margin`` is the per-minute signed strike where that prob crosses
    0.5 (the convenience ``mid``). Both reuse the platform mapping documented in
    ``research/scripts/spread_alpha.py``.
    """
    sub = odds_long[(odds_long.kind == "spread") & odds_long.strike.notna()].copy()
    if sub.empty:
        empty = pd.Series(np.nan, index=index, dtype=float)
        return {}, empty
    sub["minute"] = (sub.ts // 60 * 60).astype(int)

    home_strike = np.full(len(sub), np.nan)
    p_home_over = np.full(len(sub), np.nan)
    plat = sub.platform.to_numpy()
    keys = sub.key.to_numpy()
    strikes = sub.strike.to_numpy(float)
    probs = sub.prob.to_numpy(float)
    for i in range(len(sub)):
        if plat[i] == "kalshi":
            t = _kalshi_team(keys[i])
            if t == home_tri:
                home_strike[i] = strikes[i]
                p_home_over[i] = probs[i]
            elif t == away_tri:
                home_strike[i] = -strikes[i]
                p_home_over[i] = 1.0 - probs[i]
        else:  # polymarket spread stores P(home covers S) -> P(home_margin > -S)
            home_strike[i] = -strikes[i]
            p_home_over[i] = probs[i]
    sub = sub.assign(hs=home_strike, ph=p_home_over).dropna(subset=["hs", "ph"])
    if sub.empty:
        empty = pd.Series(np.nan, index=index, dtype=float)
        return {}, empty

    piv = (sub.sort_values("ts").groupby(["minute", "hs"]).ph.mean().unstack("hs"))
    piv = piv.reindex(index).ffill()
    cols = np.array(sorted(piv.columns))

    ladder: dict = {}
    for strike in cols:
        arr = piv[strike].to_numpy(dtype=float)
        if np.isfinite(arr).sum() == 0:
            continue
        ladder[float(strike)] = arr

    # implied margin: invert the (strike, prob) curve for P=0.5 each minute.
    out = []
    for _, row in piv.iterrows():
        y = row[cols].to_numpy(dtype=float)
        ok = ~np.isnan(y)
        if ok.sum() < 2:
            out.append(np.nan)
            continue
        xs, ys = cols[ok], y[ok]
        order = np.argsort(ys)  # interp needs increasing x (=prob here)
        out.append(float(np.interp(0.5, ys[order], xs[order])))
    return ladder, pd.Series(out, index=index)


# --------------------------------------------------------------------------- #
# per-game Panel construction
# --------------------------------------------------------------------------- #
def load_panel(game: dict, market: str) -> Panel | None:
    """Build one :class:`Panel` for ``game`` and ``market`` from cache.

    ``game`` is ``{date, away, home[, espn_id]}``. Returns ``None`` if the cache
    is absent or the game lacks usable odds for this market. Cache-only: never
    triggers a live build.
    """
    if market not in _MARKET_KINDS:
        raise ValueError(f"unknown market {market!r}; expected one of {sorted(_MARKET_KINDS)}")
    batch, analysis = _study()
    kinds = _MARKET_KINDS[market]
    kindtag = "-".join(sorted(kinds))
    key = f"{game['date']}_{game['away']}_at_{game['home']}_{kindtag}_all.pkl"
    if not os.path.exists(os.path.join(batch.CACHE_DIR, key)):
        return None  # cache-only: never trigger a slow live build
    try:
        d = batch.load_or_build(game, kinds=kinds)
    except Exception:  # noqa: BLE001
        return None

    df = d.minute
    if df is None or not len(df) or not len(d.odds_long):
        return None
    g = d.game
    gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"
    idx = list(df.index)

    minute_ts = np.asarray(idx, dtype=float)
    elapsed = df["elapsed_game_sec"].to_numpy(float)
    margin = df["margin"].to_numpy(float)
    total = df["total"].to_numpy(float)

    final_total = None
    if not df["total"].dropna().empty:
        final_total = float(df["total"].dropna().iloc[-1])
    final_margin = None
    if not df["margin"].dropna().empty:
        final_margin = float(df["margin"].dropna().iloc[-1])
    home_won = None
    if final_margin is not None:
        home_won = float(final_margin > 0)

    ladder: dict = {}
    if market == WINNER:
        wp = pd.concat(
            [df[c] for c in ("kalshi_home_winprob", "pm_home_winprob") if c in df],
            axis=1,
        )
        if wp.shape[1] == 0:
            return None
        mid = wp.mean(axis=1).to_numpy(float)
        if np.isfinite(mid).sum() < 5:
            return None
    elif market == TOTAL:
        imp = pd.concat([analysis._implied_total(d.odds_long, "kalshi", idx),
                         analysis._implied_total(d.odds_long, "polymarket", idx)],
                        axis=1).mean(axis=1)
        if imp.notna().sum() < 5:
            return None
        mid = imp.to_numpy(float)
        ladder = _total_ladder(d.odds_long, idx)
    else:  # SPREAD
        ladder, imp = _spread_ladder(d.odds_long, g.home_tri, g.away_tri, idx)
        if imp.notna().sum() < 5:
            return None
        mid = imp.to_numpy(float)

    # convenience features (pure functions over the frame, no leakage)
    with np.errstate(divide="ignore", invalid="ignore"):
        pace_proj = np.where(elapsed > 120, total * 2880.0 / elapsed, np.nan)
    chg = np.r_[True, np.abs(np.diff(mid)) > 1e-9]
    last_chg = np.maximum.accumulate(np.where(chg, np.arange(len(mid)), -1))
    stale_min = (np.arange(len(mid)) - last_chg).astype(float)
    features = {"pace_proj": pace_proj, "stale_min": stale_min}

    _, split_of = _split_filter(None)
    return Panel(
        game_id=gid, date=game["date"], market=market,
        home_team=g.home_tri, away_team=g.away_tri,
        minute_ts=minute_ts, elapsed_sec=elapsed, margin=margin, total=total,
        mid=np.asarray(mid, float), ladder=ladder, features=features,
        home_won=home_won, final_total=final_total, final_margin=final_margin,
        split=split_of(gid),
    )


# --------------------------------------------------------------------------- #
# bulk loader
# --------------------------------------------------------------------------- #
def load_panels(market: str, split: str | None = None, *,
                start: str = _DEFAULT_START, end: str = _DEFAULT_END,
                limit: int | None = None) -> list[Panel]:
    """Load every cached :class:`Panel` for ``market`` in the ``split`` filter.

    Cache-only and split-safe: enumerates games from the on-disk cache, applies
    the date window ``[start, end]`` and the ``split`` predicate (TEST excluded
    unless ``split == "test"``), then builds one Panel per game (skipping any the
    cache or odds can't support). ``limit`` caps the number of games considered
    *after* filtering, before loading.
    """
    predicate, _ = _split_filter(split)
    games = _cached_games(market)
    games = [g for g in games
             if start <= g["date"] <= end and predicate(_gid_of(g))]
    if limit is not None:
        games = games[:limit]
    panels: list[Panel] = []
    for g in games:
        p = load_panel(g, market)
        if p is not None:
            panels.append(p)
    return panels


def available(market: str) -> int:
    """Count cached games for ``market`` (by pkl presence; no build, no network)."""
    return len(_cached_games(market))
