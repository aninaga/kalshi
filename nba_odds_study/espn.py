"""ESPN NBA play-by-play source.

ESPN's public summary endpoint is the one NBA feed reachable without the
cdn.nba.com edge block, and every play carries a real ``wallclock`` (UTC)
timestamp plus the running score, which is what lets us align game events to
market-odds timestamps. Substitutions arrive as text ("X enters the game for
Y"); we replay them from the starting lineups to know who is on court at any
moment.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from . import teams

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

_SUB_RE = re.compile(r"^(.*?) enters the game for (.*)$")

# Global throttle: ESPN blocks bursts, so space out every request and back off
# hard on 429/403. Tunable via NBA_ESPN_MIN_INTERVAL.
_MIN_INTERVAL = float(os.environ.get("NBA_ESPN_MIN_INTERVAL", "0.4"))
_last_call = [0.0]


def _throttle() -> None:
    dt = time.time() - _last_call[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_call[0] = time.time()


def _get(url: str, params: dict | None = None, tries: int = 6) -> dict:
    last = None
    for i in range(tries):
        try:
            _throttle()
            r = _SESSION.get(url, params=params, timeout=25)
            if r.status_code in (429, 403):
                time.sleep(min(2.0 * 2 ** i, 30.0))  # exponential backoff on rate limit
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - network resilience
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"ESPN GET failed: {url} ({last})")


def _iso_to_unix(iso: str) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


@dataclass
class GameData:
    event_id: str
    date: str
    home_tri: str
    away_tri: str
    home_id: str
    away_id: str
    home_name: str
    away_name: str
    plays: list[dict]
    first_ts: int
    last_ts: int
    starters: dict[str, list[str]]
    minutes: dict[str, int]
    stars: dict[str, list[str]]
    player_team: dict[str, str]
    on_intervals: dict[str, list[tuple[int, int]]]
    espn_winprob: list[tuple[int, float]] = field(default_factory=list)

    @property
    def subs(self) -> list[dict]:
        """Substitution events: {ts, team, player_in, player_out}."""
        return [p for p in self.plays if p["type"] == "Substitution" and p.get("player_in")]


def resolve_event(date: str, away_tri: str, home_tri: str) -> str:
    """Find the ESPN event id for ``away_tri`` @ ``home_tri`` on ``date`` (YYYY-MM-DD)."""
    ymd = date.replace("-", "")
    sb = _get(SCOREBOARD, {"dates": ymd})
    want = {away_tri, home_tri}
    for ev in sb.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        tris = set()
        for c in comp.get("competitors", []):
            try:
                tris.add(teams.from_espn(c.get("team", {})))
            except ValueError:
                pass
        if tris == want:
            return ev["id"]
    raise LookupError(f"No ESPN game found for {away_tri}@{home_tri} on {date}")


def fetch_game(event_id: str, date: str = "", n_stars: int = 2) -> GameData:
    s = _get(SUMMARY, {"event": event_id})

    comp = (s.get("header", {}).get("competitions") or [{}])[0]
    home_id = away_id = home_tri = away_tri = home_name = away_name = ""
    for c in comp.get("competitors", []):
        t = c.get("team", {})
        tri = teams.from_espn(t)
        if c.get("homeAway") == "home":
            home_id, home_tri, home_name = t.get("id"), tri, t.get("displayName")
        else:
            away_id, away_tri, away_name = t.get("id"), tri, t.get("displayName")
    id_to_tri = {home_id: home_tri, away_id: away_tri}

    # --- plays ---
    plays = []
    for p in s.get("plays", []):
        ts = _iso_to_unix(p.get("wallclock"))
        if ts is None:
            continue
        rec = {
            "id": p.get("id"),
            "seq": int(p.get("sequenceNumber") or 0),
            "ts": ts,
            "iso": p.get("wallclock"),
            "period": (p.get("period") or {}).get("number"),
            "clock": (p.get("clock") or {}).get("displayValue"),
            "home_score": p.get("homeScore"),
            "away_score": p.get("awayScore"),
            "scoring": bool(p.get("scoringPlay")),
            "score_value": p.get("scoreValue") or 0,
            "team_id": (p.get("team") or {}).get("id"),
            "team": id_to_tri.get((p.get("team") or {}).get("id")),
            "type": (p.get("type") or {}).get("text") or "",
            "text": p.get("text") or "",
            "player_in": None,
            "player_out": None,
        }
        if rec["type"] == "Substitution":
            m = _SUB_RE.match(rec["text"])
            if m:
                rec["player_in"], rec["player_out"] = m.group(1).strip(), m.group(2).strip()
        plays.append(rec)
    plays.sort(key=lambda r: (r["seq"], r["ts"]))
    first_ts = plays[0]["ts"]
    last_ts = plays[-1]["ts"]

    # --- boxscore: starters, minutes, stars, player->team ---
    starters: dict[str, list[str]] = {home_tri: [], away_tri: []}
    minutes: dict[str, int] = {}
    player_team: dict[str, str] = {}
    for blk in s.get("boxscore", {}).get("players", []):
        tri = teams.from_espn(blk.get("team", {}))
        for grp in blk.get("statistics", []):
            labels = grp.get("labels", [])
            mi = labels.index("MIN") if "MIN" in labels else 0
            for a in grp.get("athletes", []):
                name = a.get("athlete", {}).get("displayName")
                if not name:
                    continue
                player_team[name] = tri
                try:
                    minutes[name] = int(a.get("stats", [])[mi])
                except (ValueError, IndexError):
                    minutes[name] = 0
                if a.get("starter"):
                    starters.setdefault(tri, []).append(name)
    stars = {}
    for tri in (home_tri, away_tri):
        roster = sorted(
            (n for n, t in player_team.items() if t == tri and minutes.get(n, 0) > 0),
            key=lambda n: minutes.get(n, 0),
            reverse=True,
        )
        stars[tri] = roster[:n_stars]

    on_intervals = _reconstruct_intervals(plays, starters, player_team, first_ts, last_ts)
    winprob = _parse_winprob(s, {p["id"]: p["ts"] for p in plays})

    return GameData(
        event_id=event_id, date=date, home_tri=home_tri, away_tri=away_tri,
        home_id=home_id, away_id=away_id, home_name=home_name, away_name=away_name,
        plays=plays, first_ts=first_ts, last_ts=last_ts, starters=starters,
        minutes=minutes, stars=stars, player_team=player_team,
        on_intervals=on_intervals, espn_winprob=winprob,
    )


def _reconstruct_intervals(plays, starters, player_team, first_ts, last_ts):
    """Per-player on-court intervals, by replaying subs from the starters."""
    started = {n for names in starters.values() for n in names}
    status = {n: ("on" if n in started else "off") for n in player_team}
    open_at = {n: (first_ts if status[n] == "on" else None) for n in player_team}
    intervals: dict[str, list[tuple[int, int]]] = {n: [] for n in player_team}

    for p in plays:
        if p["type"] != "Substitution" or not p.get("player_in"):
            continue
        ts, pin, pout = p["ts"], p["player_in"], p["player_out"]
        for n in (pin, pout):
            status.setdefault(n, "off")
            open_at.setdefault(n, None)
            intervals.setdefault(n, [])
        if status.get(pout) == "on" and open_at.get(pout) is not None:
            intervals[pout].append((open_at[pout], ts))
        status[pout] = "off"
        open_at[pout] = None
        if status.get(pin) != "on":
            status[pin] = "on"
            open_at[pin] = ts
    for n, st in status.items():
        if st == "on" and open_at.get(n) is not None:
            intervals[n].append((open_at[n], last_ts))
    return intervals


def _parse_winprob(summary: dict, id_to_ts: dict) -> list[tuple[int, float]]:
    out = []
    for w in summary.get("winprobability", []) or []:
        pct = w.get("homeWinPercentage")
        ts = id_to_ts.get(w.get("playId"))
        if pct is not None and ts is not None:
            out.append((ts, float(pct)))
    out.sort()
    return out


def is_on(intervals: list[tuple[int, int]], ts: int) -> bool:
    return any(a <= ts <= b for a, b in intervals)
