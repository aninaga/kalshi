"""fable_xcity — cross-city realized-surprise feature + PRE-REGISTERED hypothesis (weather/temp).

LANE: cross-city information. Single-agent mode of the documented data-request
loop: the ANALYST and the DATA AGENT are the same agent here (role merge noted
in research/lab/runs/fable_xcity_*.md). DataRequest id 5fed1350dc0784a2.

Feature: ``xcity_realized_signal``
----------------------------------
For city B's daily-high event on local day E, the value is the MEAN over the
OTHER four cities A of the TRAIN-debiased realized-vs-forecast surprise of A's
day-(E-1) event::

    surprise_A(E-1) = realized_A(E-1) - forecast_asof_A(E-1) - mu_A

* ``realized_A`` = settled YES bucket representative from the cache pkl
  (rows with no unique YES bucket are DROPPED, never guessed).
* ``forecast_asof_A`` = last ``forecast_high_f`` parquet row with
  ``known_from_ts <= anchor_A`` (the lead-1 vintage; conservative ISSUE-time
  anchored, see data_forecast_high_f run note).
* ``mu_A`` = TRAIN-only mean of (realized - forecast_asof) per city, frozen at
  build time and recorded in provenance (matches the negated CITY_BIAS of
  fable_forecast: AUS 2.69, CHI 1.71, DEN 0.90, MIA 2.08, NYC 2.00).

Point-in-time rule (the whole game)::

    anchor_A(D)      = max(last cached candle ts of A's day-D event + 60 s,
                           epoch(end of A's LOCAL day D) + 3600 s)
    known_from_ts(B-row) = max over the 4 contributing cities A of anchor_A(E-1)

Defense: at ``anchor_A`` the market for A's day-D event has closed (the candle
window is over) AND A's local calendar day D ended >= 1 hour earlier, so the
daily high is physically fixed and publicly observable (real-time NWS obs; the
closed Kalshi book itself). Empirically the anchor resolves to 01:00 local of
day D+1 for every cached event (close is local midnight +/- 1 h; the +1 h floor
binds). We never claim "when the high occurred". A row missing ANY of its four
contributors (missing event, no unique YES bucket, no forecast) is dropped.

Pre-registered hypothesis (frozen BEFORE any gate scoring; registry id printed
on --register): when the other cities came in WARM vs their (debiased) model
forecasts on day E-1, city B's still-open day-E market underadjusts -> buy
"over" at the ATM strike; symmetric cold/under. Signal: first bar i with
elapsed_frac in [0.35, 0.70], stale_min <= 2, finite mid and feature, and
|xcity_realized_signal[i]| >= 1.0 F. Side = over if value > 0 else under.
Entry i+1 (1 min), hold to settlement, FillModel(venue="kalshi",
half_spread=max(2 x 0.0067 measured TRAIN median, 0.015) = 0.015),
min_elapsed=0, max_elapsed=200000 (weather override). Side conditions on the
EXOGENOUS feature sign, never on a quote level -> the i+1 ATM re-snap cannot
invert exposure (no pick_strike pinning needed; the snap-flip hazard of
runs/model_bakeoff_20260609.md applies to quote-level-conditioned signals).

Run::

    PYTHONPATH=/home/user/kalshi python3 -m research.scripts.weather_xcity_e2e --build-feature
    PYTHONPATH=/home/user/kalshi python3 -m research.scripts.weather_xcity_e2e --verify-pit
    PYTHONPATH=/home/user/kalshi python3 -m research.scripts.weather_xcity_e2e --split train --no-ledger
    PYTHONPATH=/home/user/kalshi python3 -m research.scripts.weather_xcity_e2e --split nontest --no-ledger
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pickle
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research.lab import data as lab_data
from research.lab import evaluate as lab_eval
from research.lab.execution import FillModel
from research.lab.providers import feature_store
from research.lab.providers.weather import (CACHE_DIR, SPLITS_PATH, TEMP,
                                            _bucket_bounds)
from research.lab.strategy import Strategy

HYP_ID = "cc772c239008304b"          # registered 2026-06-09T23:33:58Z, agent fable_xcity
REQ_ID = "5fed1350dc0784a2"          # DataRequest (analyst/data-agent merged)
LEDGER = "research/reports/alpha/ledger.jsonl"

FIELD = "xcity_realized_signal"
FAMILY = "weather"
CITIES = ("NYC", "CHI", "MIA", "AUS", "DEN")
TZ = {"NYC": "America/New_York", "MIA": "America/New_York",
      "CHI": "America/Chicago", "AUS": "America/Chicago",
      "DEN": "America/Denver"}

# ---- pre-registered strategy parameters (frozen before scoring) -------------
FRAC_LO, FRAC_HI = 0.35, 0.70   # entry window, fraction of quote-window duration
THRESH_F = 1.0                  # |mean-of-4 debiased surprise| threshold, degF
HALF_SPREAD = 0.015             # max(2 x 0.0067 measured TRAIN bucket median, 0.015)

# TRAIN-only debias constants, asserted against the build (negated fable_forecast
# CITY_BIAS — an independent cross-check of the same train population).
EXPECTED_MU = {"AUS": 2.69, "CHI": 1.71, "DEN": 0.90, "MIA": 2.08, "NYC": 2.00}


# --------------------------------------------------------------------------- #
# Phase 1 — feature construction (data-agent role)
# --------------------------------------------------------------------------- #
def _load_rec(path):
    """Load one cache pkl; retry once (another lane may rewrite atomically)."""
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh)
    except Exception:  # noqa: BLE001
        time.sleep(0.5)
        with open(path, "rb") as fh:
            return pickle.load(fh)


def _anchor(city: str, date: str, last_candle_ts: int | None) -> int:
    """Conservative publication time of the day-``date`` realized high."""
    d = dt.date.fromisoformat(date)
    midnight_end = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0, 0),
                                       ZoneInfo(TZ[city]))
    floor = int(midnight_end.timestamp()) + 3600
    return max((int(last_candle_ts) + 60) if last_candle_ts else 0, floor)


def build_event_table() -> pd.DataFrame:
    """Per cached event: realized settled rep, close anchor, as-of forecast."""
    rows = []
    for p in sorted(CACHE_DIR.glob("*.pkl")):
        rec = _load_rec(p)
        city, date, tick = rec["city"], rec["date"], rec["event_ticker"]
        yes = [m for m in rec["markets"]
               if str(m.get("result", "")).lower() == "yes"]
        realized = np.nan
        if len(yes) == 1:
            b = _bucket_bounds(yes[0])
            if b is not None:
                realized = float(b[2])
        ts = [c["ts"] for m in rec["markets"] for c in m["candles"]]
        last_ts = max(ts) if ts else None
        rows.append(dict(event_id=tick, city=city, date=date,
                         realized=realized, last_candle_ts=last_ts,
                         anchor=_anchor(city, date, last_ts)))
    ev = pd.DataFrame(rows)

    fc = (pd.read_parquet(feature_store.features_dir(FAMILY)
                          / "forecast_high_f.parquet")
            .sort_values(["event_id", "known_from_ts"]))
    grouped = {eid: g for eid, g in fc.groupby("event_id")}

    def asof(eid, anchor):
        g = grouped.get(eid)
        if g is None:
            return np.nan
        g = g[g.known_from_ts <= anchor]
        return float(g.value.iloc[-1]) if len(g) else np.nan

    ev["fc_asof"] = [asof(e, a) for e, a in zip(ev.event_id, ev.anchor)]
    ev["surprise_raw"] = ev.realized - ev.fc_asof
    return ev


def train_mu(ev: pd.DataFrame) -> dict:
    """TRAIN-only per-city mean surprise (the debias constants)."""
    sp = json.loads(SPLITS_PATH.read_text())
    train = set(sp["train_game_ids"])
    mu = (ev[ev.event_id.isin(train)]
          .groupby("city").surprise_raw.mean().to_dict())
    for c, exp in EXPECTED_MU.items():
        if abs(mu[c] - exp) > 0.02:
            raise AssertionError(f"train mu for {c} = {mu[c]:.3f}, expected "
                                 f"~{exp} (negated fable_forecast CITY_BIAS)")
    return mu


def build_feature_rows(ev: pd.DataFrame, mu: dict) -> pd.DataFrame:
    """One row per B event: mean-of-4 debiased day-(E-1) surprise, max anchor."""
    ev = ev.copy()
    ev["surprise_adj"] = ev.surprise_raw - ev.city.map(mu)
    by_key = ev.set_index(["city", "date"])[["surprise_adj", "anchor"]]
    out = []
    for _, r in ev.iterrows():
        prev = (dt.date.fromisoformat(r.date)
                - dt.timedelta(days=1)).isoformat()
        vals, anchors = [], []
        for c in CITIES:
            if c == r.city:
                continue
            try:
                row = by_key.loc[(c, prev)]
            except KeyError:
                break
            if not np.isfinite(row.surprise_adj):
                break
            vals.append(float(row.surprise_adj))
            anchors.append(int(row.anchor))
        if len(vals) != 4:        # require ALL four contributors, else drop
            continue
        out.append(dict(event_id=r.event_id,
                        known_from_ts=int(max(anchors)),
                        value=float(np.mean(vals))))
    return pd.DataFrame(out)


def build_feature() -> dict:
    ev = build_event_table()
    mu = train_mu(ev)
    df = build_feature_rows(ev, mu)
    prov = {
        "source": ("DERIVED offline: market_data/weather/_cache pkls (settled "
                   "YES bucket reps + candle windows) + "
                   "market_data/weather/features/forecast_high_f.parquet "
                   "(as-of lead-1 forecast at each contributor's close anchor)"),
        "construction": ("value(B event, local day E) = mean over the other 4 "
                         "cities A of [realized_A(E-1) - forecast_asof_A(E-1) "
                         "- mu_A]; mu_A = TRAIN-only mean surprise; all 4 "
                         "contributors required, else the row is dropped"),
        "point_in_time_rule": ("known_from_ts = max over contributors A of "
                               "anchor_A(E-1); anchor_A = max(last candle ts + "
                               "60s, epoch(end of A's LOCAL day, IANA tz) + "
                               "3600s) — market closed AND local day over >=1h; "
                               "empirically 01:00 local of day E for all rows. "
                               "Never 'when the high occurred'."),
        "train_mu_debias": {k: round(v, 4) for k, v in mu.items()},
        "timezones": TZ,
        "limitations": [
            "realized is the settled bucket REPRESENTATIVE (+-0.5-1F "
            "granularity; open-ended extreme buckets truncate to cap-1/floor+1)",
            "Kalshi's official settlement confirmation can postdate the anchor; "
            "the underlying high is fixed and publicly observable (NWS real-time "
            "obs) by the anchor, and the bucketization is deterministic",
            "mu debias constants are TRAIN-derived (frozen; documented here); "
            "train rows are therefore mildly in-sample w.r.t. the debias mean",
            "first cached date per city and 2026-04-12 (KXHIGHMIA-26APR11 has "
            "no unique YES bucket) have no/partial contributors -> rows dropped",
        ],
        "writer": "fable_xcity (analyst/data-agent role merge, single-agent "
                  "mode of the data-request loop)",
        "data_request_id": REQ_ID,
    }
    path = feature_store.write_field(FAMILY, FIELD, df, provenance=prov,
                                     mode="replace")
    return {"path": str(path), "rows": len(df),
            "events_total": int(ev.event_id.nunique()),
            "coverage": f"{100.0 * len(df) / ev.event_id.nunique():.1f}%",
            "train_mu": {k: round(v, 4) for k, v in mu.items()}}


# --------------------------------------------------------------------------- #
# PIT verification (independent as-of recompute + timezone-leakage proof)
# --------------------------------------------------------------------------- #
def verify_pit(hand_events=("KXHIGHNY-26JAN15", "KXHIGHDEN-25NOV20",
                            "KXHIGHAUS-26APR20")) -> dict:
    """(1) every row's known_from_ts >= each contributor's local end-of-day+1h
    AND >= each contributor's last candle + 60s (re-derived from the cache);
    (2) per-row known_from_ts within B's panel window (coverage, not leakage);
    (3) hand as-of recompute on 3 events at in-window bars vs the joined series.
    """
    ev = build_event_table()
    mu = train_mu(ev)
    ev["surprise_adj"] = ev.surprise_raw - ev.city.map(mu)
    by_key = ev.set_index(["city", "date"])
    stored = pd.read_parquet(feature_store.features_dir(FAMILY)
                             / f"{FIELD}.parquet")
    meta = ev.set_index("event_id")

    # (1) global timezone-leakage assertion over EVERY stored row.
    n_checked = 0
    for _, r in stored.iterrows():
        b = meta.loc[r.event_id]
        prev = (dt.date.fromisoformat(b.date) - dt.timedelta(days=1)).isoformat()
        for c in CITIES:
            if c == b.city:
                continue
            con = by_key.loc[(c, prev)]
            local_day_end = dt.datetime.combine(
                dt.date.fromisoformat(prev) + dt.timedelta(days=1),
                dt.time(0, 0), ZoneInfo(TZ[c]))
            assert r.known_from_ts >= int(local_day_end.timestamp()) + 3600, \
                (r.event_id, c, "known_from_ts before contributor local day end +1h")
            assert r.known_from_ts >= int(con.last_candle_ts) + 60, \
                (r.event_id, c, "known_from_ts before contributor market close")
            n_checked += 1

    # (2)+(3) hand checks on 3 events: row knowable INSIDE B's quote window
    # (usability), NaN strictly before known_from_ts, joined value == stored
    # value == independent recompute from raw inputs.
    samples = {}
    for tick in hand_events:
        p = lab_data.load_panel(tick, TEMP)
        assert p is not None, tick
        row = stored[stored.event_id == tick]
        assert len(row) == 1, tick
        kts, val = int(row.known_from_ts.iloc[0]), float(row.value.iloc[0])
        assert kts <= p.minute_ts[-1], (tick, "row knowable only after window end")
        joined = np.asarray(p.features[FIELD], float)
        pre = p.minute_ts < kts
        post = p.minute_ts >= kts
        assert np.all(~np.isfinite(joined[pre])), (tick, "value visible before known_from_ts")
        assert np.allclose(joined[post], val), (tick, "joined value != stored value")
        # independent recompute of the value itself from raw inputs
        b = meta.loc[tick]
        prev = (dt.date.fromisoformat(b.date) - dt.timedelta(days=1)).isoformat()
        manual = np.mean([by_key.loc[(c, prev)].surprise_adj
                          for c in CITIES if c != b.city])
        assert abs(manual - val) < 1e-9, (tick, "stored value != independent recompute")
        first_bar = int(np.flatnonzero(post)[0]) if post.any() else None
        samples[tick] = {
            "known_from_ts_utc": dt.datetime.fromtimestamp(
                kts, dt.timezone.utc).isoformat(),
            "value_F": round(val, 3),
            "first_visible_bar": first_bar,
            "first_visible_frac": (round(float(p.elapsed_sec[first_bar]
                                                / p.duration_sec), 3)
                                   if first_bar is not None else None),
            "nan_before": int(pre.sum()),
        }
    return {"rows": len(stored), "contributor_checks": n_checked,
            "hand_samples": samples}


# --------------------------------------------------------------------------- #
# Phase 2 — pre-registered strategy
# --------------------------------------------------------------------------- #
def entry(panel) -> np.ndarray:
    n = panel.n
    dur = panel.duration_sec or float("nan")
    if not np.isfinite(dur) or dur <= 0:
        return np.zeros(n, dtype=bool)
    frac = np.asarray(panel.elapsed_sec, float) / float(dur)
    x = np.asarray(panel.features.get(FIELD, np.full(n, np.nan)), float)
    mid = np.asarray(panel.mid, float)
    return (np.isfinite(x) & np.isfinite(mid) & (np.abs(x) >= THRESH_F)
            & (frac >= FRAC_LO) & (frac <= FRAC_HI))
    # staleness <= 2 min enforced by Strategy(max_stale_min=2.0)


def side(panel, i: int) -> str:
    x = float(np.asarray(panel.features[FIELD], float)[i])
    return "over" if x > 0 else "under"


def build_strategy() -> Strategy:
    return Strategy(
        name="weather.xcity_realized_surprise",
        entry=entry, side=side, exit="settlement",
        entry_latency_min=1.0, max_stale_min=2.0,
        min_elapsed=0.0, max_elapsed=200000.0,
    )


def run(split: str, ledger: bool = True) -> dict:
    panels = lab_data.load_panels(TEMP, split=split)
    strat = build_strategy()
    fills = FillModel(venue="kalshi", half_spread=HALF_SPREAD)
    trades = strat.run(panels, fill_model=fills)
    gate = lab_eval.evaluate(trades, ledger_path=(LEDGER if ledger else None),
                             family=FAMILY)
    df = trades.df()
    return {
        "hyp_id": HYP_ID, "split": split,
        "params": {"frac_lo": FRAC_LO, "frac_hi": FRAC_HI,
                   "thresh_f": THRESH_F, "half_spread": HALF_SPREAD,
                   "venue": "kalshi", "field": FIELD},
        "n_panels": len(panels), "n_trades": len(trades),
        "by_city": (df.groupby("home_team").size().to_dict() if len(df) else {}),
        "by_side": (df.groupby("side").size().to_dict() if len(df) else {}),
        "avg_fill": (float(df["entry_price"].mean()) if len(df) else None),
        "win_rate": (float((df["payoff"] > 0.5).mean()) if len(df) else None),
        "gate": {
            "passed": gate.passed,
            "cents_per_contract": gate.cents_per_contract,
            "ci": [gate.ci_lo, gate.ci_hi],
            "n": gate.n, "n_games": gate.n_games,
            "reasons": gate.reasons,
            "cost_sweep": gate.cost_sweep,
            "walkforward": gate.walkforward,
            "adversarial": {k: v for k, v in gate.adversarial.items()},
            "governance": gate.governance,
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-feature", action="store_true",
                    help="rebuild + write the feature-store field (data-agent role)")
    ap.add_argument("--verify-pit", action="store_true",
                    help="independent point-in-time verification")
    ap.add_argument("--split", default=None,
                    choices=["train", "val", "nontest"])
    ap.add_argument("--no-ledger", action="store_true",
                    help="diagnostic rerun: do not touch the trial ledger")
    a = ap.parse_args(argv)
    if a.build_feature:
        print(json.dumps(build_feature(), indent=2))
    if a.verify_pit:
        print(json.dumps(verify_pit(), indent=2))
    if a.split:
        print(json.dumps(run(a.split, ledger=not a.no_ledger),
                         indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
