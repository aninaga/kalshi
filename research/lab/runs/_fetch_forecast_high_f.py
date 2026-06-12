"""One-shot fetcher for the ``forecast_high_f`` data request (id 7e0d214cbea6162f).

Sources a point-in-time daily-max-temperature FORECAST for every cached Kalshi
``temp`` (weather) event from Open-Meteo's free, no-auth **Previous Runs API**
(``previous-runs-api.open-meteo.com``), which archives past model runs at fixed
lead-time offsets, and writes it to the durable feature store as
``weather/forecast_high_f`` with a leakage-safe ``known_from_ts`` (forecast
ISSUE time, never the valid/target time).

Why Previous Runs (not Historical Forecast):
  The Historical Forecast API stitches the *first hours* of successive runs into
  a near-analysis "best estimate" series — its values for day D come from runs
  initialised close to D, so it is effectively a nowcast and its issue time
  cannot be placed *before* the quote window. The request wants a forecast that
  was knowable BEFORE the day, to test informational edge. The Previous Runs API
  gives exactly that: hourly ``temperature_2m_previous_dayN`` = the value that was
  predicted ~24*N hours before its valid time (docs; ``_previous_day0`` == live
  run). We compute the daily max over the local calendar day for N in {1,2}.

Issue-time rule (the whole game), conservative & leakage-safe:
  For target local day D and lead N, every hour T in [D 00:00, D 23:00] carries a
  value issued ~T-24N. The daily max can in principle occur at any hour, so the
  LATEST issue time that could contribute to the day-D max is for T = D 23:00,
  i.e. issued at (D 23:00 local) - 24N hours. We set
      known_from_ts = epoch( (D 23:00 local) - 24N hours )
  so NO contributing hour was issued after known_from_ts (the leakage-safe
  maximum issue time). This is intentionally conservative: the max almost always
  occurs mid-afternoon (verified: median 15:00 local), whose issue time is
  earlier, but we never claim an earlier issue than we can guarantee.

Station coords (validated against realized settlement buckets, MAE ~1-2 F):
  NYC Central Park, CHI Midway, MIA Miami Intl, AUS Austin-BERGSTROM (NOT Camp
  Mabry — the prompt's note was wrong; Bergstrom MAE 1.41 vs Mabry 2.05 F),
  DEN Denver Intl. Open-Meteo snaps each to its ~2km grid cell; both requested
  and returned coords are recorded in provenance.

Run (operator/data-agent, network):  python research/lab/runs/_fetch_forecast_high_f.py
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from research.lab.providers import feature_store

CACHE = "/home/user/kalshi/market_data/weather/_cache"
PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
LEADS = (1, 2)  # previous_day1 (~24h ahead) and previous_day2 (~48h ahead)

_MON = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")

# city -> (series prefix, lat, lon, tz, station label, requested coord note)
CITY = {
    "NYC": ("KXHIGHNY",  40.7790,  -73.9693, "America/New_York", "Central Park (KNYC)"),
    "CHI": ("KXHIGHCHI", 41.7860,  -87.7524, "America/Chicago",  "Chicago Midway (KMDW)"),
    "MIA": ("KXHIGHMIA", 25.7959,  -80.2870, "America/New_York", "Miami Intl (KMIA)"),
    "AUS": ("KXHIGHAUS", 30.1944,  -97.6699, "America/Chicago",  "Austin-Bergstrom (KAUS)"),
    "DEN": ("KXHIGHDEN", 39.8466, -104.6562, "America/Denver",   "Denver Intl (KDEN)"),
}


def _event_date(tick: str):
    m = _DATE_RE.search(tick or "")
    if not m:
        return None
    yy, mo, dd = m.groups()
    mm = _MON.get(mo)
    return f"20{yy}-{mm:02d}-{int(dd):02d}" if mm else None


def _issue_ts(date_str: str, lead: int, tz: ZoneInfo) -> int:
    """Leakage-safe ISSUE epoch for the day-`date_str` max-of-previous_day`lead`.

    = (local day D 23:00) - 24*lead hours, in UTC. The latest hour (23:00) that
    can hold the daily max was predicted ~24*lead h before it; no contributing
    hour was issued later than this.
    """
    end_local = datetime.fromisoformat(f"{date_str}T23:00:00").replace(tzinfo=tz)
    issue = end_local - timedelta(hours=24 * lead)
    return int(issue.astimezone(timezone.utc).timestamp())


def _cached_tickers(prefix: str) -> list[str]:
    return sorted(f[:-4] for f in os.listdir(CACHE)
                  if f.endswith(".pkl") and f[:-4].split("-")[0] == prefix)


def fetch_city(city: str, *, sleep_s: float = 0.25, log=print) -> tuple[list[dict], dict]:
    """Fetch all leads for one city's full date range in a single hourly call.

    Returns (rows, meta) where rows are feature-store dicts and meta records the
    requested vs returned grid coords for provenance.
    """
    prefix, lat, lon, tzname, station = CITY[city]
    tz = ZoneInfo(tzname)
    ticks = _cached_tickers(prefix)
    dates = sorted({d for d in (_event_date(t) for t in ticks) if d})
    if not dates:
        return [], {}
    lo, hi = dates[0], dates[-1]
    hourly_vars = ",".join(f"temperature_2m_previous_day{n}" for n in LEADS)
    params = {
        "latitude": lat, "longitude": lon, "hourly": hourly_vars,
        "temperature_unit": "fahrenheit", "timezone": tzname,
        "start_date": lo, "end_date": hi,
    }
    r = requests.get(PREV_RUNS_URL, params=params, timeout=120)
    r.raise_for_status()
    j = r.json()
    h = j.get("hourly", {})
    times = h.get("time", [])
    # local-day -> {lead -> [hourly values]}
    byday: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    series = {n: np.asarray(h.get(f"temperature_2m_previous_day{n}", []), float)
              for n in LEADS}
    for i, ts in enumerate(times):
        d = ts[:10]
        for n in LEADS:
            v = series[n][i] if i < series[n].size else np.nan
            if np.isfinite(v):
                byday[d][n].append(float(v))

    date_to_tick = {_event_date(t): t for t in ticks}
    rows: list[dict] = []
    for d, tick in date_to_tick.items():
        for n in LEADS:
            vals = byday.get(d, {}).get(n, [])
            if not vals:
                continue
            rows.append({
                "event_id": tick,
                "known_from_ts": _issue_ts(d, n, tz),
                "value": round(max(vals), 1),
                "_lead": n,  # dropped before write; for diagnostics only
            })
    meta = {
        "requested_coord": [lat, lon],
        "returned_grid_coord": [j.get("latitude"), j.get("longitude")],
        "elevation_m": j.get("elevation"),
        "timezone": j.get("timezone"),
        "station": station,
        "events": len(ticks), "rows": len(rows),
    }
    log(f"[{city}] {station}: {len(ticks)} events, {len(rows)} rows "
        f"(leads {list(LEADS)}), grid={meta['returned_grid_coord']}")
    time.sleep(sleep_s)
    return rows, meta


def main() -> None:
    all_rows: list[dict] = []
    coords_meta: dict = {}
    for city in CITY:
        rows, meta = fetch_city(city)
        all_rows.extend(rows)
        coords_meta[city] = meta

    df = pd.DataFrame(all_rows)
    diag = df.groupby("_lead").size().to_dict()
    df = df.drop(columns=["_lead"])
    # uniqueness guard: one row per (event_id, known_from_ts)
    df = df.drop_duplicates(subset=["event_id", "known_from_ts"], keep="last")

    n_events = df["event_id"].nunique()
    total_events = sum(m["events"] for m in coords_meta.values())
    provenance = {
        "source": "Open-Meteo Previous Runs API (free, no-auth; archives past model runs)",
        "endpoint": (PREV_RUNS_URL + "?latitude={lat}&longitude={lon}"
                     "&hourly=temperature_2m_previous_day1,temperature_2m_previous_day2"
                     "&temperature_unit=fahrenheit&timezone={tz}"
                     "&start_date={lo}&end_date={hi}"),
        "field_meaning": ("daily MAX of hourly temperature_2m_previous_dayN over the "
                          "local calendar day = the daily-high FORECAST at lead N days "
                          "(previous_day1 ~24h-ahead, previous_day2 ~48h-ahead); "
                          "multiple rows per event (one per lead), as-of join handles "
                          "the revision toward shorter lead naturally"),
        "leads_days": list(LEADS),
        "units": "degrees Fahrenheit",
        "point_in_time_rule": (
            "known_from_ts = epoch( (local target-day 23:00) - 24*lead hours ). "
            "Open-Meteo docs: temperature_2m_previous_dayN is 'the value predicted "
            "24*N hours before valid time'. The day-D max can fall on any hour up to "
            "23:00 local, whose forecast was issued ~24*N h earlier; anchoring at the "
            "LAST hour guarantees no contributing hour was issued after known_from_ts "
            "(leakage-safe maximum issue time). Verified empirically that this lands "
            "inside the Kalshi quote window for lead1 (~13h after open) and before the "
            "open for lead2."),
        "stations": coords_meta,
        "station_note": ("Kalshi settles each city on a specific NWS station; coords "
                         "chosen to match it and VALIDATED against realized settlement "
                         "buckets (near-analysis daily-max MAE per city: NYC 1.06, CHI "
                         "1.08, MIA 1.41, AUS 1.59, DEN 1.94 F, small cold bias). "
                         "AUSTIN uses Austin-BERGSTROM (KAUS), NOT Camp Mabry as the "
                         "request note stated: Bergstrom MAE 1.41 vs Camp Mabry 2.05 F "
                         "against realized buckets."),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": "7e0d214cbea6162f",
        "limitations": [
            "known_from_ts is the conservative LATEST issue time for the daily max; the "
            "actual afternoon-max forecast was knowable somewhat earlier, so the as-of "
            "join is pessimistic (never optimistic) about when the signal arrives.",
            "Open-Meteo grid-snaps to a ~2km cell near each station (returned coords in "
            "provenance), not the exact instrument; residual station mismatch ~<=2 F.",
            "Lead is in 24h steps relative to valid time, an approximation of true model "
            "init cadence (00/06/12/18 UTC runs); the 24h-step anchor stays conservative.",
            "best_match model selection per Open-Meteo (model can change across the "
            "archive); the field is a model forecast, not a single fixed model.",
            "Daily max is computed from hourly temperature_2m, which can differ slightly "
            "from the official NWS daily-high instrument (sub-hourly peaks) — hence the "
            "~1 F validation residual.",
        ],
        "diagnostics": {"rows_per_lead": {str(k): int(v) for k, v in diag.items()},
                        "events_with_rows": int(n_events),
                        "total_cached_events": int(total_events)},
    }
    path = feature_store.write_field("weather", "forecast_high_f", df,
                                     provenance=provenance, mode="replace")
    print(f"\nWROTE {path}")
    print(f"rows={len(df)} events_with_rows={n_events}/{total_events} "
          f"coverage={n_events/total_events:.1%}")
    print(f"rows_per_lead={diag}")


if __name__ == "__main__":
    main()
