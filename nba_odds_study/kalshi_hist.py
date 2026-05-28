"""Kalshi historical odds for a single NBA game.

A game maps to several Kalshi events that share a ``{YYMONDD}{AWAY}{HOME}``
suffix (KXNBAGAME = winner, KXNBASPREAD, KXNBATOTAL). Each event's markets are
fetched with ``with_nested_markets``; per-minute price history comes from the
candlesticks endpoint (1-minute OHLC of yes_bid / yes_ask / last trade).
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests

from . import teams

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"KXNBAGAME": "winner", "KXNBASPREAD": "spread", "KXNBATOTAL": "total"}

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "curl/8"})
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _get(path: str, params: dict | None = None, tries: int = 6):
    last = None
    for i in range(tries):
        try:
            r = _SESSION.get(f"{BASE}{path}", params=params, timeout=25)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1.0)) + 0.4 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"Kalshi GET failed: {path} ({last})")


def datecode(date: str) -> str:
    return datetime.strptime(date, "%Y-%m-%d").strftime("%y%b%d").upper()


def _money(d: dict, key: str):
    try:
        v = float(d.get(key, {}).get("close_dollars"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def enumerate_markets(date: str, away_tri: str, home_tri: str) -> list[dict]:
    """Return market descriptors for every Kalshi market covering this game."""
    dc = datecode(date)
    out = []
    for series, kind in SERIES.items():
        event = None
        for a, b in ((away_tri, home_tri), (home_tri, away_tri)):
            d = _get(f"/events/{series}-{dc}{a}{b}", {"with_nested_markets": "true"})
            if d:
                event = d
                break
        if not event:
            continue
        markets = event.get("markets") or event.get("event", {}).get("markets") or []
        for m in markets:
            sub = m.get("yes_sub_title") or ""
            yes_team = None
            strike = None
            if kind == "winner":
                for tri in (home_tri, away_tri):
                    if teams.location(tri).lower() in sub.lower():
                        yes_team = tri
            else:
                nums = _NUM_RE.findall(sub) or _NUM_RE.findall(m.get("title") or "")
                strike = float(nums[0]) if nums else None
            out.append({
                "platform": "kalshi",
                "kind": kind,
                "series": series,
                "ticker": m.get("ticker"),
                "label": sub or m.get("title"),
                "yes_team": yes_team,
                "strike": strike,
            })
    return out


def fetch_candles(series: str, ticker: str, start_ts: int, end_ts: int, period: int = 1) -> list[dict]:
    """1-minute price history: list of {ts, yes_bid, yes_ask, mid, last}."""
    d = _get(
        f"/series/{series}/markets/{ticker}/candlesticks",
        {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period},
    )
    rows = []
    for c in (d or {}).get("candlesticks", []):
        bid = _money(c, "yes_bid")
        ask = _money(c, "yes_ask")
        last = _money(c, "price")
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2
        else:
            mid = last  # one-sided quotes (esp. at settlement) are unreliable
        rows.append({"ts": c.get("end_period_ts"), "yes_bid": bid,
                     "yes_ask": ask, "mid": mid, "last": last})
    return rows
