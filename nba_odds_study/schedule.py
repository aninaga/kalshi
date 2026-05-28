"""Enumerate completed NBA games over a date range (via ESPN scoreboard)."""
from __future__ import annotations

import datetime as dt
import time

import requests

from . import teams

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


_last_call = [0.0]


def _get(ymd: str) -> dict:
    for i in range(5):
        try:
            dt = time.time() - _last_call[0]
            if dt < 0.3:
                time.sleep(0.3 - dt)
            _last_call[0] = time.time()
            r = _SESSION.get(SCOREBOARD, params={"dates": ymd}, timeout=25)
            if r.status_code in (429, 403):
                time.sleep(min(2.0 * 2 ** i, 30.0))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (i + 1))
    return {}


def completed_games(start: str, end: str) -> list[dict]:
    """Return [{date, away, home, espn_id}] for finished games in [start, end] (YYYY-MM-DD)."""
    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    out = []
    d = d0
    while d <= d1:
        sb = _get(d.strftime("%Y%m%d"))
        for ev in sb.get("events", []):
            if ev.get("status", {}).get("type", {}).get("name") != "STATUS_FINAL":
                continue
            comp = (ev.get("competitions") or [{}])[0]
            home = away = None
            for c in comp.get("competitors", []):
                try:
                    tri = teams.from_espn(c.get("team", {}))
                except ValueError:
                    continue
                if c.get("homeAway") == "home":
                    home = tri
                else:
                    away = tri
            if home and away:
                out.append({"date": d.isoformat(), "away": away, "home": home, "espn_id": ev.get("id")})
        d += dt.timedelta(days=1)
    return out
