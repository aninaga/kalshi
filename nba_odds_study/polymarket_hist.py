"""Polymarket historical odds for a single NBA game.

A game is one Gamma event (slug ``nba-{away}-{home}-{date}``) holding the
moneyline, spread and total markets. Per-minute price history comes from the
CLOB ``prices-history`` endpoint (one price series per outcome token).
"""
from __future__ import annotations

import json
import re
import time

import requests

from . import teams

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_PARTIAL = ("1H", "2H", "1Q", "2Q", "3Q", "4Q", "Quarter", "Half")


def _get(base: str, path: str, params: dict | None = None, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            r = _SESSION.get(f"{base}{path}", params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Polymarket GET failed: {path} ({last})")


def _loads(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _find_event(date: str, away_tri: str, home_tri: str) -> dict | None:
    a, h = away_tri.lower(), home_tri.lower()
    for slug in (f"nba-{a}-{h}-{date}", f"nba-{h}-{a}-{date}"):
        d = _get(GAMMA, "/events", {"slug": slug})
        rows = d if isinstance(d, list) else d.get("data", [])
        if rows:
            return rows[0]
    return None


def enumerate_markets(date: str, away_tri: str, home_tri: str) -> list[dict]:
    """Return descriptors for the full-game winner / spread / total markets."""
    event = _find_event(date, away_tri, home_tri)
    if not event:
        return []
    home_nick, away_nick = teams.nickname(home_tri), teams.nickname(away_tri)
    out = []
    for m in event.get("markets", []) or []:
        q = m.get("question") or ""
        if any(t in q for t in _PARTIAL):
            continue
        outcomes = _loads(m.get("outcomes"))
        tokens = _loads(m.get("clobTokenIds"))
        if len(outcomes) != len(tokens) or not tokens:
            continue
        by_outcome = dict(zip(outcomes, tokens))

        kind = primary_outcome = None
        strike = None
        if " vs. " in q and ":" not in q:
            kind, primary_outcome = "winner", home_nick
        elif q.startswith("Spread:"):
            kind, primary_outcome = "spread", home_nick
            nums = _NUM_RE.findall(q)
            strike = float(nums[0]) if nums else None
        elif "O/U" in q:
            kind, primary_outcome = "total", "Over"
            nums = _NUM_RE.findall(q)
            strike = float(nums[-1]) if nums else None
        else:
            continue
        if primary_outcome not in by_outcome:
            continue
        out.append({
            "platform": "polymarket",
            "kind": kind,
            "question": q,
            "token_id": by_outcome[primary_outcome],
            "outcome": primary_outcome,
            "tokens": by_outcome,
            "strike": strike,
            "label": q,
        })
    return out


def fetch_history(token_id: str, start_ts: int, end_ts: int, fidelity: int = 1) -> list[dict]:
    """1-minute price history: list of {ts, p} (p = probability of the token's outcome)."""
    d = _get(CLOB, "/prices-history",
             {"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity})
    return [{"ts": pt["t"], "p": pt["p"]} for pt in (d or {}).get("history", [])]
