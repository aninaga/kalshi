"""research.lab.providers.crypto — Kalshi hourly coin-price threshold ladders.

The SECOND non-NBA family through the generic provider seam: hourly "BTC/ETH
price at <hour>" events (series ``KXBTCD`` / ``KXETHD``), chosen by the
2026-06-09 coverage probe (see ``runs/fable_crypto_20260609T233132Z.md``) for

* **n**: 9,834 settled events per series (hourly cadence back to 2024-03) —
  two orders of magnitude above the econ-print candidates;
* **a NATIVE cumulative ladder**: every market is a single-leg "$X or above"
  threshold binary with its own two-sided book, so ``ladder[boundary] =
  P(price > boundary)`` is one quoted instrument — no multi-leg synthetic
  (unlike weather's interior boundaries or the KXBTC range buckets) and the
  measured per-minute half-spread is the honest single-leg execution input;
* **exact objective settlement**: ``expiration_value`` on every market dict
  is the realized CF Benchmarks 60s-average index price, so every boundary
  comparison settles exactly (no bucket-midpoint representative needed);
* **full candle coverage**: settled markets return the whole 1-minute history
  of the ~1-hour quote window, nearly all bars two-sided.

Contract model
--------------
Each event lists ~75–188 threshold binaries; only the ~10–40 near the money
trade (``volume_fp > 0`` — the outcome-blind activity filter the cache build
uses). The provider converts the threshold book into the lab's Panel idiom:

* ``ladder``: boundary -> per-minute P(price > boundary), read DIRECTLY from
  each floor-only threshold's quoted mid (cap-only markets contribute the
  complement at their cap — still single-leg, bid/ask swapped).
* ``mid``: the implied settle price (0.5 crossing of the cumulative curve,
  interpolated) — the analogue of the NBA total's implied level.
* ``final_total`` AND ``final_margin`` both carry the EXACT realized price
  (``expiration_value``), so settlement works for any strategy regardless of
  which outcome field the core consults for a non-NBA market string.
* ``home_team``/``away_team`` are both ``"<ASSET>H<hh>"`` (asset x hour of
  day, e.g. ``BTCH19``): the decisive team-concentration and cluster-knockout
  gates need a meaningful cluster key, and for a single-underlying hourly
  family that key is hour-of-day — mirroring weather's city.
* Timed event: ``elapsed_sec`` is seconds since the first quote and
  ``duration_sec`` the quote-window length (~3,540s). There is no
  accumulating score, so ``margin``/``total`` are NaN and pace-style signals
  correctly never fire.

Splits live in ``market_data/crypto/splits.json`` (same schema as the NBA /
weather splits); the TEST split is excluded unless requested explicitly.

CLI (cache build + splits lock; network — operator-run, not test-run)::

    python -m research.lab.providers.crypto --build --assets BTC --max-events 800 --stride 2
    python -m research.lab.providers.crypto --build --assets ETH --max-events 200 --stride 8
    python -m research.lab.providers.crypto --make-splits
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

COIN_PX = "coin_price"  # market string this family owns: P(settle px > boundary)

# Asset code -> Kalshi series ticker (hourly price-threshold events).
ASSET_SERIES = {
    "BTC": "KXBTCD",
    "ETH": "KXETHD",
}

_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _ROOT / "market_data" / "crypto" / "_cache"
SPLITS_PATH = _ROOT / "market_data" / "crypto" / "splits.json"

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
# KX-era hourly ticker: KXBTCD-26JUN0919 (date then 2-digit hour, no dash).
_TICKER_DH_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")
# Pre-rename era: BTCD-24MAR18-16 (hour after a dash).
_TICKER_DH_OLD_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-(\d{1,2})$")


def event_date_hour(event_ticker: str) -> Optional[tuple]:
    """``("YYYY-MM-DD", hour)`` parsed from an hourly event ticker."""
    for rx in (_TICKER_DH_RE, _TICKER_DH_OLD_RE):
        m = rx.search(event_ticker or "")
        if not m:
            continue
        yy, mon, dd, hh = m.groups()
        mm = _MONTHS.get(mon)
        if not mm:
            continue
        return f"20{yy}-{mm:02d}-{int(dd):02d}", int(hh)
    return None


def event_date(event_ticker: str) -> Optional[str]:
    dh = event_date_hour(event_ticker)
    return None if dh is None else dh[0]


def _sort_key(event_ticker: str) -> tuple:
    """Chronological key (date, hour, ticker) for splits and enumeration."""
    dh = event_date_hour(event_ticker)
    if dh is None:
        return ("", -1, event_ticker)
    return (dh[0], dh[1], event_ticker)


def _asset_of(event_ticker: str) -> Optional[str]:
    base = (event_ticker or "").split("-", 1)[0]
    for asset, series in ASSET_SERIES.items():
        if base in (series, series.removeprefix("KX")):
            return asset
    return None


# --------------------------------------------------------------------------- #
# threshold geometry
# --------------------------------------------------------------------------- #
def _boundary(m: dict) -> Optional[tuple]:
    """``(boundary, orient)`` for one threshold market dict.

    Kalshi ``KX*D`` convention (verified live): ``floor``-only markets are
    "$X or above" with ``floor_strike`` just under the round level (e.g.
    61599.99 -> "$61,600 or above"), settling YES iff value > floor. Their
    quoted mid IS P(price > boundary): ``orient=+1``. A ``cap``-only market
    ("Y or below") settles YES iff value <= cap, so P(price > cap) is the
    COMPLEMENT of its quote: ``orient=-1`` (still one leg — bid/ask swap).
    Range buckets (floor+cap) are not single-leg cumulative quotes and are
    skipped (this family fetches the threshold series, not KXBTC buckets).
    """
    f, c = m.get("floor"), m.get("cap")
    if f is not None and c is None:
        return float(f), +1
    if f is None and c is not None:
        return float(c), -1
    return None


def _split_filter(split: Optional[str]):
    """(predicate, split_of) over the crypto splits.json — same safety
    invariant as the NBA/weather loaders: TEST excluded unless asked."""
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

    ``rec``: {"event_ticker", "asset", "date", "hour", "expiration_value",
    "markets": [{"ticker", "floor", "cap", "result",
    "candles": [{"ts","yes_bid","yes_ask","mid","last"}]}]}.
    Returns ``None`` when the book is unusable (fewer than 2 quoted
    boundaries, <5 minutes of quotes).
    """
    markets = []
    for m in rec.get("markets", []):
        b = _boundary(m)
        if b is None or not m.get("candles"):
            continue
        markets.append((b, m))
    if len(markets) < 2:
        return None
    markets.sort(key=lambda bm: bm[0][0])

    # Minute index: union of candle minutes across thresholds.
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

    # Native cumulative ladder: each threshold quotes P(px > boundary)
    # directly (orient=+1) or its complement (orient=-1). Single leg either
    # way — the measured spread is the whole execution friction.
    ladder: dict = {}
    halves = []
    for (b, orient), m in markets:
        mid = _series(m, "mid")
        if np.isfinite(mid).sum() == 0:
            continue
        prob = mid if orient > 0 else 1.0 - mid
        ladder[float(b)] = np.clip(prob, 0.0, 1.0)
        bidc, askc = _series(m, "yes_bid"), _series(m, "yes_ask")
        with np.errstate(invalid="ignore"):
            halves.append((askc - bidc) / 2.0)
    if len(ladder) < 2:
        return None

    # Implied settle price: invert the cumulative curve at P=0.5 per minute.
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

    # Settlement: the EXACT realized index value, with a transition fallback
    # (midpoint between highest-YES and lowest-NO floor boundary) for old
    # records lacking expiration_value. Either settles every boundary the
    # provider listed consistently with the venue's per-market results.
    final_px = None
    ev = rec.get("expiration_value")
    try:
        if ev is not None and np.isfinite(float(ev)):
            final_px = float(ev)
    except (TypeError, ValueError):
        final_px = None
    if final_px is None:
        yes_b = [b for (b, o), m in markets
                 if o > 0 and str(m.get("result", "")).lower() == "yes"]
        no_b = [b for (b, o), m in markets
                if o > 0 and str(m.get("result", "")).lower() == "no"]
        if yes_b and no_b and max(yes_b) < min(no_b):
            final_px = (max(yes_b) + min(no_b)) / 2.0

    # Measured per-minute half-spread (mean across quoted thresholds) — the
    # honest, SINGLE-LEG execution input for this book.
    with np.errstate(invalid="ignore"):
        hs = np.vstack(halves)
        half_spread = np.nanmean(np.where(np.isfinite(hs), hs, np.nan), axis=0)

    chg = np.r_[True, np.abs(np.diff(mid)) > 1e-9]
    last_chg = np.maximum.accumulate(np.where(chg, np.arange(n), -1))
    stale_min = (np.arange(n) - last_chg).astype(float)

    gid = rec["event_ticker"]
    if split_of is None:
        _, split_of = _split_filter(None)
    dh = event_date_hour(gid)
    date = rec.get("date") or (dh[0] if dh else "")
    hour = rec.get("hour")
    if hour is None and dh is not None:
        hour = dh[1]
    asset = rec.get("asset") or _asset_of(gid) or ""
    team = f"{asset}H{int(hour):02d}" if hour is not None else asset
    nanarr = np.full(n, np.nan)
    return Panel(
        game_id=gid, date=date, market=COIN_PX,
        home_team=team, away_team=team,
        minute_ts=minute_ts,
        elapsed_sec=minute_ts - minute_ts[0],
        margin=nanarr.copy(), total=nanarr.copy(),
        mid=mid, ladder=ladder,
        features={"stale_min": stale_min, "half_spread": half_spread},
        home_won=None, final_total=final_px, final_margin=final_px,
        split=split_of(gid),
        duration_sec=float(minute_ts[-1] - minute_ts[0]),
    )


# --------------------------------------------------------------------------- #
# the provider
# --------------------------------------------------------------------------- #
class CryptoProvider:
    """MarketDataProvider for Kalshi hourly coin-price threshold events."""

    family = "crypto"
    markets = (COIN_PX,)

    def _cached(self) -> list:
        if not CACHE_DIR.is_dir():
            return []
        ticks = (fn[:-4] for fn in os.listdir(CACHE_DIR) if fn.endswith(".pkl"))
        return sorted(ticks, key=_sort_key)

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
            panel = build_panel(rec)
            if panel is not None:
                # Custom datasets from fulfilled data requests attach here,
                # as-of known_from_ts (leakage-safe by construction).
                from research.lab.providers import feature_store
                feature_store.join_panel(panel, self.family)
            return panel
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
        p = self.load_panel(event, COIN_PX)
        return None if p is None else p.duration_sec

    @staticmethod
    def _check_market(market: str) -> None:
        if market != COIN_PX:
            raise ValueError(f"unknown market {market!r}; crypto owns {COIN_PX!r}")


# --------------------------------------------------------------------------- #
# cache build + splits lock (network CLI — operator-run)
# --------------------------------------------------------------------------- #
def _iso_ts(s) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def _atomic_pickle(path: Path, obj) -> None:
    """Write ``obj`` to ``path`` atomically (tmp + os.replace)."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "wb") as fh:
        pickle.dump(obj, fh)
    os.replace(tmp, path)


def build_cache(assets: list, max_events: int = 800, stride: int = 1,
                shard: int = 0, nshards: int = 1, sleep_s: float = 0.05,
                max_markets: int = 48, log=print) -> dict:
    """Fetch settled hourly events + per-threshold candlesticks into the cache.

    Newest events first; ``stride`` keeps every k-th event (outcome-blind
    calendar widening: more walk-forward months per API call). Candles are
    fetched only for markets with ``volume_fp > 0`` (activity filter, capped
    at the top ``max_markets`` by volume). Skips events already cached
    (idempotent / resumable); writes are atomic. ``shard``/``nshards``
    partition the event list for parallel workers. Returns counts.
    """
    from research.lab.providers import _kalshi_fetch as K

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"events": 0, "skipped": 0, "empty": 0}
    for asset in assets:
        series = ASSET_SERIES[asset]
        # Page only as deep as the stride x cap needs (rate-limit hygiene).
        pages = max(1, (max(1, stride) * max_events + 199) // 200 + 1)
        evs = [e for e in K.settled_events(series, max_pages=pages)
               if event_date_hour(e.get("event_ticker", "")) is not None]
        evs = evs[::max(1, stride)][:max_events]
        evs = [e for i, e in enumerate(evs) if i % nshards == shard]
        log(f"[{asset} shard {shard}/{nshards}] {len(evs)} settled events to consider")
        for e in evs:
            tick = e.get("event_ticker")
            if not tick:
                continue
            out = CACHE_DIR / f"{tick}.pkl"
            if out.exists():
                counts["skipped"] += 1
                continue
            try:
                markets = K.event_markets(tick)
            except Exception as exc:  # noqa: BLE001
                log(f"[{asset} shard {shard}/{nshards}] {tick}: markets fetch "
                    f"failed ({exc}); event left uncached")
                counts["empty"] += 1
                time.sleep(5.0)
                continue
            exp_val = None
            for m in markets:
                v = m.get("expiration_value")
                if v not in (None, ""):
                    exp_val = v
                    break
            active = [m for m in markets
                      if float(m.get("volume_fp") or m.get("volume") or 0) > 0]
            active.sort(key=lambda m: -float(m.get("volume_fp") or m.get("volume") or 0))
            active = active[:max_markets]
            rows = []
            failed = False
            for m in active:
                mt = m.get("ticker")
                lo = _iso_ts(m.get("open_time"))
                hi = _iso_ts(m.get("close_time") or m.get("expiration_time"))
                if mt is None or lo is None or hi is None:
                    continue
                try:
                    candles = K.fetch_candles(series, mt, lo, hi)
                except Exception as exc:  # noqa: BLE001 — rate-limit storms etc.
                    # Idempotent build: leave this event uncached and move on;
                    # a re-run (or the next shard pass) picks it up cleanly.
                    log(f"[{asset} shard {shard}/{nshards}] {tick}: candle "
                        f"fetch failed ({exc}); event left uncached")
                    failed = True
                    time.sleep(5.0)
                    break
                rows.append({"ticker": mt,
                             "floor": m.get("floor_strike"),
                             "cap": m.get("cap_strike"),
                             "result": m.get("result"),
                             "volume": m.get("volume_fp"),
                             "sub": m.get("yes_sub_title"),
                             "candles": candles})
                time.sleep(sleep_s)
            if failed:
                counts["empty"] += 1
                continue
            dh = event_date_hour(tick)
            rec = {"event_ticker": tick, "series": series, "asset": asset,
                   "date": dh[0] if dh else None,
                   "hour": dh[1] if dh else None,
                   "expiration_value": exp_val, "markets": rows}
            if not any(r["candles"] for r in rows):
                counts["empty"] += 1
                continue
            _atomic_pickle(out, rec)
            counts["events"] += 1
            if counts["events"] % 50 == 0:
                log(f"[{asset} shard {shard}/{nshards}] progress: {counts}")
        log(f"[{asset} shard {shard}/{nshards}] done: {counts}")
    return counts


def make_splits(train_frac: float = 0.70, val_frac: float = 0.15,
                force: bool = False) -> dict:
    """Chronological train/val/test split over the cached events, locked.

    Mirrors the NBA/weather splits.json schema (ordered by (date, hour)).
    Refuses to overwrite an existing lock unless ``force`` (re-splitting
    after results have been seen is leakage).
    """
    if SPLITS_PATH.exists() and not force:
        raise RuntimeError(f"{SPLITS_PATH} already locked; pass force=True only "
                           "if you understand the leakage implications")
    prov = CryptoProvider()
    ids = [t for t in prov._cached() if event_date(t) is not None]
    n = len(ids)
    if n < 20:
        raise RuntimeError(f"only {n} cached events; build the cache first")
    n_train = int(n * train_frac)
    n_val = int(n * val_frac)
    splits = {
        "train_game_ids": ids[:n_train],
        "val_game_ids": ids[n_train:n_train + n_val],
        "test_game_ids": ids[n_train + n_val:],
        "train_end_date": event_date(ids[n_train - 1]),
        "val_end_date": event_date(ids[n_train + n_val - 1]),
        "test_end_date": event_date(ids[-1]),
        "locked_at_ts": int(datetime.now(timezone.utc).timestamp()),
        "n_train": n_train, "n_val": n_val,
        "n_test": n - n_train - n_val,
    }
    SPLITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SPLITS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(splits, indent=2))
    os.replace(tmp, SPLITS_PATH)
    return {k: splits[k] for k in
            ("n_train", "n_val", "n_test", "train_end_date", "val_end_date")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true", help="fetch the event cache")
    ap.add_argument("--assets", default="BTC,ETH")
    ap.add_argument("--max-events", type=int, default=800,
                    help="settled events per asset AFTER striding (newest first)")
    ap.add_argument("--stride", type=int, default=1,
                    help="keep every k-th settled event (calendar widening)")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--make-splits", action="store_true",
                    help="lock chronological train/val/test splits")
    a = ap.parse_args(argv)
    if a.build:
        assets = [c.strip().upper() for c in a.assets.split(",") if c.strip()]
        unknown = [c for c in assets if c not in ASSET_SERIES]
        if unknown:
            raise SystemExit(f"unknown assets {unknown}; known: {sorted(ASSET_SERIES)}")
        print(json.dumps(build_cache(assets, max_events=a.max_events,
                                     stride=a.stride, shard=a.shard,
                                     nshards=a.nshards), indent=2))
    if a.make_splits:
        print(json.dumps(make_splits(), indent=2))
    if not (a.build or a.make_splits):
        prov = CryptoProvider()
        print(json.dumps({"family": prov.family, "markets": list(prov.markets),
                          "cached_events": prov.available(COIN_PX)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
