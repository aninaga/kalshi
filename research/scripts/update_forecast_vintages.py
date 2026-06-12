"""Live updater for the ``forecast_high_f`` feature (weather/temp).

The frozen forecast-gap signal (hyp ``3cb384c482d1c2f7``) and its maker paper
tenant (``research.lab.paper`` strategy ``forecast_gap_maker``, maker hyp
``b8b071fa26ea4933``) read ``panel.features["forecast_high_f"]`` — the
point-in-time daily-high forecast built by the one-shot fetcher
``research/lab/runs/_fetch_forecast_high_f.py`` (data request
``7e0d214cbea6162f``). That fetcher only covers CACHED (settled) events; live
paper trading needs TODAY'S vintages for the OPEN events. This script appends
them, with the ORIGINAL builder's exact point-in-time rule.

Source & PIT rule (mirrored verbatim from the original — see
``research/lab/runs/data_forecast_high_f_20260609T214420Z.md``):
  * Open-Meteo **Previous Runs API**: hourly ``temperature_2m_previous_dayN``
    = the value predicted ~24*N hours before its valid time. Daily-high
    forecast at lead N = max over the local calendar day's hours.
  * ``known_from_ts = epoch( (local target-day 23:00) - 24*N hours )`` — the
    leakage-safe LATEST issue time of any contributing hour.
  * Leads N in {1, 2}, the SAME vintages the frozen backtest feature carries
    (lead-2 is knowable before the quote window opens; lead-1 arrives
    mid-window — the multi-lead as-of join sharpens naturally).

Live-boundary honesty (the one thing the original never faced): for an open
event whose target day is not over, some leads' hourly values come from model
runs that have not happened yet (Open-Meteo serves nulls there). A daily max
over a PARTIAL day is not the vintage the backtest used, and its known_from
anchor would be wrong. We therefore only write a (event, lead) row when the
full local day's hourly series is present and finite AND its known_from_ts is
already in the past. Re-running is safe: the feature store appends with
dedupe on (event_id, known_from_ts), and a complete vintage re-fetch yields
the same value.

Run (operator/cron, network; ~1 API call per city + Kalshi event listing)::

    python -m research.scripts.update_forecast_vintages
    python -m research.scripts.update_forecast_vintages --dry-run
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from research.lab.providers import feature_store
from research.lab.providers.weather import CITY_SERIES, event_date

PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
LEADS = (1, 2)   # same vintages as the original builder (previous_day1/2)

# city -> (lat, lon, tz) — identical to the original fetcher's validated
# station coords (research/lab/runs/_fetch_forecast_high_f.py).
CITY_COORD = {
    "NYC": (40.7790,  -73.9693, "America/New_York"),
    "CHI": (41.7860,  -87.7524, "America/Chicago"),
    "MIA": (25.7959,  -80.2870, "America/New_York"),
    "AUS": (30.1944,  -97.6699, "America/Chicago"),
    "DEN": (39.8466, -104.6562, "America/Denver"),
}


def _issue_ts(date_str: str, lead: int, tz: ZoneInfo) -> int:
    """Leakage-safe ISSUE epoch — the original builder's rule, verbatim."""
    end_local = datetime.fromisoformat(f"{date_str}T23:00:00").replace(tzinfo=tz)
    issue = end_local - timedelta(hours=24 * lead)
    return int(issue.astimezone(timezone.utc).timestamp())


def daily_max_complete(times: list, values: np.ndarray, date_str: str):
    """Max of ``values`` over the hours of local day ``date_str`` — but ONLY
    if the day's hourly series is complete and fully finite (>= 23 hours
    covers DST-short days). Returns None otherwise. Pure; unit-tested."""
    idx = [i for i, t in enumerate(times) if t[:10] == date_str]
    if len(idx) < 23:
        return None
    vals = values[idx]
    if not np.all(np.isfinite(vals)):
        return None
    return float(np.max(vals))


def _open_event_dates(log=print) -> dict:
    """city -> {date_str -> event_ticker} for currently OPEN Kalshi events."""
    from research.lab.providers import _kalshi_fetch as K

    out: dict = {}
    for city, series in CITY_SERIES.items():
        d = K._get("/events", {"series_ticker": series, "status": "open",
                               "limit": 50})
        by_date = {}
        for e in (d or {}).get("events", []):
            tick = e.get("event_ticker")
            dt = event_date(tick) if tick else None
            if dt:
                by_date[dt] = tick
        out[city] = by_date
        log(f"[{city}] open events: {sorted(by_date)}")
        time.sleep(0.1)
    return out


def fetch_city_rows(city: str, by_date: dict, *, now_ts: float,
                    log=print) -> list:
    """Feature-store rows for one city's open events (complete vintages only)."""
    lat, lon, tzname = CITY_COORD[city]
    tz = ZoneInfo(tzname)
    dates = sorted(by_date)
    if not dates:
        return []
    hourly_vars = ",".join(f"temperature_2m_previous_day{n}" for n in LEADS)
    r = requests.get(PREV_RUNS_URL, params={
        "latitude": lat, "longitude": lon, "hourly": hourly_vars,
        "temperature_unit": "fahrenheit", "timezone": tzname,
        "start_date": dates[0], "end_date": dates[-1],
    }, timeout=120)
    r.raise_for_status()
    h = r.json().get("hourly", {})
    times = h.get("time", [])
    rows = []
    for d in dates:
        for n in LEADS:
            series = np.asarray(
                h.get(f"temperature_2m_previous_day{n}", []), float)
            v = daily_max_complete(times, series, d) if series.size else None
            if v is None:
                log(f"[{city}] {d} lead{n}: vintage incomplete — skipped")
                continue
            kft = _issue_ts(d, n, tz)
            if kft > now_ts:
                log(f"[{city}] {d} lead{n}: known_from in the future — skipped")
                continue
            rows.append({"event_id": by_date[d], "known_from_ts": kft,
                         "value": round(v, 1)})
    log(f"[{city}] {len(rows)} vintage rows ready")
    time.sleep(0.25)
    return rows


def run(*, dry_run: bool = False, log=print) -> dict:
    now_ts = time.time()
    open_events = _open_event_dates(log=log)
    all_rows: list = []
    for city, by_date in open_events.items():
        if by_date:
            all_rows.extend(fetch_city_rows(city, by_date, now_ts=now_ts,
                                            log=log))
    summary = {"rows": len(all_rows),
               "events": len({r["event_id"] for r in all_rows}),
               "dry_run": dry_run}
    if not all_rows or dry_run:
        log(json.dumps({**summary, "sample": all_rows[:4]}, indent=2))
        return summary

    df = pd.DataFrame(all_rows)
    # Preserve the original provenance; append a live-update log entry so the
    # provenance file keeps telling the WHOLE story of the field.
    prov_path = (feature_store.features_dir("weather")
                 / "forecast_high_f.provenance.json")
    prov = json.loads(prov_path.read_text()) if prov_path.exists() else {
        "source": "Open-Meteo Previous Runs API"}
    prov.setdefault("live_updates", []).append({
        "script": "research/scripts/update_forecast_vintages.py",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows_appended": len(df),
        "events": sorted(df["event_id"].unique().tolist()),
        "rule": "identical PIT rule; complete-local-day vintages only; "
                "known_from_ts always in the past at write time",
    })
    path = feature_store.write_field("weather", "forecast_high_f", df,
                                     provenance=prov, mode="append")
    log(f"APPENDED {len(df)} rows -> {path}")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + print rows without writing the store")
    a = ap.parse_args(argv)
    run(dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
