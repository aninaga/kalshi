"""research.lab.providers.weather — Kalshi daily high-temperature family.

The walking-skeleton vertical for the beyond-NBA expansion: daily "Highest
temperature in <city>" events (series ``KXHIGH*``), chosen because they have
objective settlement, a dense bucketed strike ladder with REAL two-sided
quotes in the candlestick history (measured spreads — not assumed), and high
event volume (one event per city per day, settled history back to 2024).

Contract model
--------------
Each event lists ~6 bucket binaries (e.g. "86° or below", "87° to 88°", ...,
"95° or above"). The provider converts the bucket book into the lab's Panel
idiom:

* ``ladder``: cumulative boundary -> per-minute P(high > boundary), computed
  from the side of the book with the FEWEST legs (sum of bucket mids above the
  boundary, or 1 − sum below). Interior boundaries are multi-leg synthetics —
  the measured per-minute half-spread is exported as
  ``features["half_spread"]`` so analysts can charge an HONEST (>= measured)
  spread via ``FillModel(half_spread=..., venue="kalshi")``; never assume the
  1.5c NBA default is enough here.
* ``mid``: the implied high temperature (the 0.5 crossing of the cumulative
  curve, interpolated) — the analogue of the NBA total's implied level.
* ``final_total`` AND ``final_margin`` both carry the realized temperature
  representative (the settled YES bucket's midpoint), so settlement works for
  any strategy regardless of which outcome field the core consults for a
  non-NBA market string. Any in-bucket representative settles every boundary
  comparison exactly.
* ``home_team`` and ``away_team`` are both the city code, so the gate's
  concentration / cluster checks key by city.
* Timed event: ``elapsed_sec`` is seconds since the first quote and
  ``duration_sec`` the event's quote-window length. There is no accumulating
  score, so ``margin``/``total`` are NaN and pace-style signals correctly
  never fire.

Splits live in ``market_data/weather/splits.json`` (same schema as the NBA
``splits.json``); the TEST split is excluded unless requested explicitly.

CLI (cache build + splits lock; network — operator-run, not test-run)::

    python -m research.lab.providers.weather --build --cities NYC,CHI,MIA,AUS,DEN \
        --max-events 250
    python -m research.lab.providers.weather --make-splits
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from research.lab.types import Panel

TEMP = "temp"  # market string this family owns: P(daily high > boundary)

# City code -> Kalshi series ticker (daily high temperature).
CITY_SERIES = {
    "NYC": "KXHIGHNY",
    "CHI": "KXHIGHCHI",
    "MIA": "KXHIGHMIA",
    "AUS": "KXHIGHAUS",
    "DEN": "KXHIGHDEN",
}

_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _ROOT / "market_data" / "weather" / "_cache"
SPLITS_PATH = _ROOT / "market_data" / "weather" / "splits.json"

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")


def event_date(event_ticker: str) -> Optional[str]:
    """``YYYY-MM-DD`` parsed from a ticker like ``KXHIGHNY-26JUN07``."""
    m = _TICKER_DATE_RE.search(event_ticker or "")
    if not m:
        return None
    yy, mon, dd = m.groups()
    mm = _MONTHS.get(mon)
    if not mm:
        return None
    return f"20{yy}-{mm:02d}-{int(dd):02d}"


def _city_of(event_ticker: str) -> Optional[str]:
    base = (event_ticker or "").split("-", 1)[0]
    for city, series in CITY_SERIES.items():
        if base in (series, series.removeprefix("KX")):
            return city
    return None


# --------------------------------------------------------------------------- #
# bucket geometry
# --------------------------------------------------------------------------- #
def _bucket_bounds(m: dict):
    """``(lo, hi, rep)`` for one bucket market dict (half-open real bounds).

    Kalshi convention (verified on live data): ``cap``-only = "<cap" (e.g.
    cap=87 -> "86° or below"); ``floor``+``cap`` = "floor..cap" inclusive;
    ``floor``-only = ">floor" (floor=94 -> "95° or above"). Integer-reported
    temperatures put the real boundaries at the half-degrees.
    """
    f, c = m.get("floor"), m.get("cap")
    if f is None and c is not None:
        return -np.inf, float(c) - 0.5, float(c) - 1.0
    if f is not None and c is not None:
        return float(f) - 0.5, float(c) + 0.5, (float(f) + float(c)) / 2.0
    if f is not None:
        return float(f) + 0.5, np.inf, float(f) + 1.0
    return None


def _split_filter(split: Optional[str]):
    """(predicate, split_of) over the weather splits.json — same safety
    invariant as the NBA loader: TEST excluded unless asked explicitly."""
    splits = {}
    if SPLITS_PATH.exists():
        splits = json.loads(SPLITS_PATH.read_text())
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
    raise ValueError(f"unknown split {split!r}; expected "
                     "'train'/'val'/'test'/'nontest'/None")


# --------------------------------------------------------------------------- #
# Panel construction from one cached event
# --------------------------------------------------------------------------- #
def build_panel(rec: dict, split_of=None) -> Optional[Panel]:
    """One Panel from a cached event record (pure transform; no I/O).

    ``rec``: {"event_ticker", "city", "date", "markets": [{"ticker", "floor",
    "cap", "result", "candles": [{"ts","yes_bid","yes_ask","mid","last"}]}]}.
    Returns ``None`` when the book is unusable (no quotes, ambiguous
    settlement, single-minute window).
    """
    markets = []
    for m in rec.get("markets", []):
        b = _bucket_bounds(m)
        if b is None or not m.get("candles"):
            continue
        markets.append((b, m))
    if len(markets) < 2:
        return None
    markets.sort(key=lambda bm: (bm[0][0], bm[0][1]))

    # Minute index: union of candle minutes across buckets.
    ts_all = sorted({int(c["ts"]) // 60 * 60
                     for _, m in markets for c in m["candles"]
                     if c.get("ts") is not None})
    if len(ts_all) < 5:
        return None
    minute_ts = np.asarray(ts_all, dtype=float)
    pos = {t: i for i, t in enumerate(ts_all)}
    n = len(ts_all)

    def _series(m, key):
        out = np.full(n, np.nan)
        for c in m["candles"]:
            t = c.get("ts")
            v = c.get(key)
            if t is None or v is None:
                continue
            out[pos[int(t) // 60 * 60]] = float(v)
        # forward-fill within the event window
        last = np.nan
        for i in range(n):
            if np.isfinite(out[i]):
                last = out[i]
            else:
                out[i] = last
        return out

    probs = np.vstack([_series(m, "mid") for _, m in markets])      # (k, n)
    bids = np.vstack([_series(m, "yes_bid") for _, m in markets])
    asks = np.vstack([_series(m, "yes_ask") for _, m in markets])

    # Cumulative ladder at the interior boundaries, fewest-legs side.
    k = len(markets)
    boundaries = [markets[j + 1][0][0] for j in range(k - 1)]
    ladder: dict = {}
    with np.errstate(invalid="ignore"):
        for j, b in enumerate(boundaries):
            above, below = probs[j + 1:], probs[: j + 1]
            if above.shape[0] <= below.shape[0]:
                arr = np.nansum(above, axis=0)
                arr[~np.isfinite(above).any(axis=0)] = np.nan
            else:
                arr = 1.0 - np.nansum(below, axis=0)
                arr[~np.isfinite(below).any(axis=0)] = np.nan
            if np.isfinite(arr).sum() == 0:
                continue
            ladder[float(b)] = np.clip(arr, 0.0, 1.0)
    if len(ladder) < 2:
        return None

    # Implied temperature: invert the cumulative curve at P=0.5 per minute.
    bnds = np.array(sorted(ladder.keys()))
    mat = np.vstack([ladder[b] for b in bnds])                       # (kb, n)
    mid = np.full(n, np.nan)
    for i in range(n):
        y = mat[:, i]
        ok = np.isfinite(y)
        if ok.sum() < 2:
            continue
        xs, ys = bnds[ok], y[ok]
        order = np.argsort(ys)  # interp needs increasing x (=prob here)
        mid[i] = float(np.interp(0.5, ys[order], xs[order]))
    if np.isfinite(mid).sum() < 5:
        return None

    # Settlement: exactly one YES bucket -> its representative temperature.
    yes = [bm for bm in markets if str(bm[1].get("result", "")).lower() == "yes"]
    final_temp = float(yes[0][0][2]) if len(yes) == 1 else None

    # Measured per-minute half-spread (mean across quoted buckets), the honest
    # execution input for this thin book.
    with np.errstate(invalid="ignore"):
        half = (asks - bids) / 2.0
        half_spread = np.nanmean(np.where(np.isfinite(half), half, np.nan), axis=0)

    chg = np.r_[True, np.abs(np.diff(mid)) > 1e-9]
    last_chg = np.maximum.accumulate(np.where(chg, np.arange(n), -1))
    stale_min = (np.arange(n) - last_chg).astype(float)

    gid = rec["event_ticker"]
    if split_of is None:
        _, split_of = _split_filter(None)
    nanarr = np.full(n, np.nan)
    return Panel(
        game_id=gid, date=rec.get("date") or event_date(gid) or "",
        market=TEMP,
        home_team=rec.get("city") or "", away_team=rec.get("city") or "",
        minute_ts=minute_ts,
        elapsed_sec=minute_ts - minute_ts[0],
        margin=nanarr.copy(), total=nanarr.copy(),
        mid=mid, ladder=ladder,
        features={"stale_min": stale_min, "half_spread": half_spread},
        home_won=None, final_total=final_temp, final_margin=final_temp,
        split=split_of(gid),
        duration_sec=float(minute_ts[-1] - minute_ts[0]),
    )


# --------------------------------------------------------------------------- #
# the provider
# --------------------------------------------------------------------------- #
class WeatherProvider:
    """MarketDataProvider for Kalshi daily-high-temperature events."""

    family = "weather"
    markets = (TEMP,)

    def _cached(self) -> list:
        if not CACHE_DIR.is_dir():
            return []
        return sorted(fn[:-4] for fn in os.listdir(CACHE_DIR)
                      if fn.endswith(".pkl"))

    def enumerate_events(self, market: str, *, start: Optional[str] = None,
                         end: Optional[str] = None,
                         split: Optional[str] = None) -> list:
        self._check_market(market)
        predicate, _ = _split_filter(split)
        out = []
        for tick in self._cached():
            d = event_date(tick)
            if d is None:
                continue
            if start is not None and d < start:
                continue
            if end is not None and d > end:
                continue
            if predicate(tick):
                out.append(tick)
        return out

    def load_panel(self, event, market: str) -> Optional[Panel]:
        self._check_market(market)
        tick = event if isinstance(event, str) else event.get("event_ticker")
        path = CACHE_DIR / f"{tick}.pkl"
        if not path.exists():
            return None
        try:
            with open(path, "rb") as fh:
                rec = pickle.load(fh)
            return build_panel(rec)
        except Exception:  # noqa: BLE001 — one bad event must not kill a run
            return None

    def load_panels(self, market: str, split: Optional[str] = None, *,
                    start: Optional[str] = None, end: Optional[str] = None,
                    limit: Optional[int] = None) -> list:
        events = self.enumerate_events(market, start=start, end=end, split=split)
        if limit is not None:
            events = events[:limit]
        panels = []
        for ev in events:
            p = self.load_panel(ev, market)
            if p is not None:
                panels.append(p)
        return panels

    def available(self, market: str) -> int:
        self._check_market(market)
        return len(self._cached())

    def event_duration_sec(self, event) -> Optional[float]:
        p = self.load_panel(event, TEMP)
        return None if p is None else p.duration_sec

    @staticmethod
    def _check_market(market: str) -> None:
        if market != TEMP:
            raise ValueError(f"unknown market {market!r}; weather owns {TEMP!r}")


# --------------------------------------------------------------------------- #
# cache build + splits lock (network CLI — operator-run)
# --------------------------------------------------------------------------- #
def _iso_ts(s) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def build_cache(cities: list, max_events: int = 250, sleep_s: float = 0.15,
                log=print) -> dict:
    """Fetch settled events + per-bucket candlesticks into the pkl cache.

    Skips events already cached (idempotent / resumable). Returns counts.
    """
    from research.lab.providers import _kalshi_fetch as K

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"events": 0, "skipped": 0, "empty": 0}
    for city in cities:
        series = CITY_SERIES[city]
        evs = K.settled_events(series)
        evs = evs[:max_events]
        log(f"[{city}] {len(evs)} settled events to consider")
        for e in evs:
            tick = e.get("event_ticker")
            if not tick:
                continue
            out = CACHE_DIR / f"{tick}.pkl"
            if out.exists():
                counts["skipped"] += 1
                continue
            markets = K.event_markets(tick)
            rows = []
            for m in markets:
                mt = m.get("ticker")
                lo = _iso_ts(m.get("open_time"))
                hi = _iso_ts(m.get("close_time") or m.get("expiration_time"))
                if mt is None or lo is None or hi is None:
                    continue
                candles = K.fetch_candles(series, mt, lo, hi)
                rows.append({"ticker": mt,
                             "floor": m.get("floor_strike"),
                             "cap": m.get("cap_strike"),
                             "result": m.get("result"),
                             "sub": m.get("yes_sub_title"),
                             "candles": candles})
                time.sleep(sleep_s)
            rec = {"event_ticker": tick, "series": series, "city": city,
                   "date": event_date(tick), "markets": rows}
            if not any(r["candles"] for r in rows):
                counts["empty"] += 1
                continue
            with open(out, "wb") as fh:
                pickle.dump(rec, fh)
            counts["events"] += 1
        log(f"[{city}] done: {counts}")
    return counts


def make_splits(train_frac: float = 0.70, val_frac: float = 0.15,
                force: bool = False) -> dict:
    """Chronological train/val/test split over the cached events, locked.

    Mirrors the NBA splits.json schema. Refuses to overwrite an existing lock
    unless ``force`` (re-splitting after results have been seen is leakage).
    """
    if SPLITS_PATH.exists() and not force:
        raise RuntimeError(f"{SPLITS_PATH} already locked; pass force=True only "
                           "if you understand the leakage implications")
    prov = WeatherProvider()
    dated = sorted((event_date(t), t) for t in prov._cached()
                   if event_date(t) is not None)
    ids = [t for _, t in dated]
    n = len(ids)
    if n < 20:
        raise RuntimeError(f"only {n} cached events; build the cache first")
    n_train = int(n * train_frac)
    n_val = int(n * val_frac)
    splits = {
        "train_game_ids": ids[:n_train],
        "val_game_ids": ids[n_train:n_train + n_val],
        "test_game_ids": ids[n_train + n_val:],
        "train_end_date": dated[n_train - 1][0],
        "val_end_date": dated[n_train + n_val - 1][0],
        "test_end_date": dated[-1][0],
        "locked_at_ts": int(datetime.now(timezone.utc).timestamp()),
        "n_train": n_train, "n_val": n_val,
        "n_test": n - n_train - n_val,
    }
    SPLITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPLITS_PATH.write_text(json.dumps(splits, indent=2))
    return {k: splits[k] for k in
            ("n_train", "n_val", "n_test", "train_end_date", "val_end_date")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true", help="fetch the event cache")
    ap.add_argument("--cities", default="NYC,CHI,MIA,AUS,DEN")
    ap.add_argument("--max-events", type=int, default=250,
                    help="newest settled events per city")
    ap.add_argument("--make-splits", action="store_true",
                    help="lock chronological train/val/test splits")
    a = ap.parse_args(argv)
    if a.build:
        cities = [c.strip().upper() for c in a.cities.split(",") if c.strip()]
        unknown = [c for c in cities if c not in CITY_SERIES]
        if unknown:
            raise SystemExit(f"unknown cities {unknown}; known: {sorted(CITY_SERIES)}")
        print(json.dumps(build_cache(cities, max_events=a.max_events), indent=2))
    if a.make_splits:
        print(json.dumps(make_splits(), indent=2))
    if not (a.build or a.make_splits):
        prov = WeatherProvider()
        print(json.dumps({"family": prov.family, "markets": list(prov.markets),
                          "cached_events": prov.available(TEMP)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
