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
    """Read a price field's close from a candlestick, in dollars.

    Live candles expose ``close_dollars`` (numeric cents-as-dollars); the
    HISTORICAL-tier candles expose ``close`` as a string-dollars value
    (e.g. "0.4400"). Handle both — reading only ``close_dollars`` left every
    backfilled (historical) candle price null, making the recovered Kalshi
    shards price-hollow (found + fixed 2026-05-29).
    """
    try:
        node = d.get(key) or {}
        v = node.get("close_dollars")
        if v is None:
            v = node.get("close")
        if v is None:
            return None
        v = float(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _event_markets(series: str, dc: str, away_tri: str, home_tri: str) -> list[dict]:
    """Find a game's markets for one series.

    Tries the LIVE event endpoint first and falls back to the HISTORICAL tier
    for settled games whose live nested-markets array has gone empty — that
    empty array (not data loss) is the root cause of the ~97% Kalshi coverage
    gap (verified 2026-05-29; see research/lake/RECOVERY_NOTES.md). Markets
    settled before the dynamic /historical/cutoff live only under
    /historical/markets.
    """
    orderings = ((away_tri, home_tri), (home_tri, away_tri))
    # Live tier first (cheapest; covers post-cutoff games).
    for a, b in orderings:
        d = _get(f"/events/{series}-{dc}{a}{b}", {"with_nested_markets": "true"})
        ms = (d or {}).get("markets") or (d or {}).get("event", {}).get("markets") or []
        if ms:
            return ms
    # Historical tier (settled markets moved out of the live endpoint).
    for a, b in orderings:
        d = _get("/historical/markets", {"event_ticker": f"{series}-{dc}{a}{b}"})
        ms = (d or {}).get("markets") or []
        if ms:
            return ms
    return []


def _winner_team_from_ticker(ticker, away_tri: str, home_tri: str):
    """KXNBAGAME winner tickers end in the yes-token's team tri (e.g. ...-LAL).

    Use it to orient the yes side robustly even when a historical market dict
    omits yes_sub_title.
    """
    if not ticker:
        return None
    suffix = str(ticker).rsplit("-", 1)[-1].upper()
    if suffix == home_tri.upper():
        return home_tri
    if suffix == away_tri.upper():
        return away_tri
    return None


def enumerate_markets(date: str, away_tri: str, home_tri: str) -> list[dict]:
    """Return market descriptors for every Kalshi market covering this game.

    Resolves markets via the live endpoint with a historical-tier fallback
    (see _event_markets). yes_team is taken from yes_sub_title when present,
    else derived from the winner-market ticker suffix.
    """
    dc = datecode(date)
    out = []
    for series, kind in SERIES.items():
        for m in _event_markets(series, dc, away_tri, home_tri):
            ticker = m.get("ticker")
            sub = m.get("yes_sub_title") or ""
            yes_team = None
            strike = None
            if kind == "winner":
                for tri in (home_tri, away_tri):
                    if teams.location(tri).lower() in sub.lower():
                        yes_team = tri
                if yes_team is None:
                    yes_team = _winner_team_from_ticker(ticker, away_tri, home_tri)
            else:
                nums = _NUM_RE.findall(sub) or _NUM_RE.findall(m.get("title") or "")
                strike = float(nums[0]) if nums else None
            out.append({
                "platform": "kalshi",
                "kind": kind,
                "series": series,
                "ticker": ticker,
                "label": sub or m.get("title"),
                "yes_team": yes_team,
                "strike": strike,
            })
    return out


def fetch_candles(series: str, ticker: str, start_ts: int, end_ts: int, period: int = 1) -> list[dict]:
    """1-minute price history: list of {ts, yes_bid, yes_ask, mid, last}.

    Tries the live series/candlesticks endpoint, then the historical-tier
    candlesticks endpoint — settled markets 404 on the live path but return
    full history under /historical/markets/{ticker}/candlesticks.
    """
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period}
    d = _get(f"/series/{series}/markets/{ticker}/candlesticks", params)
    cs = (d or {}).get("candlesticks", [])
    if not cs:
        d = _get(f"/historical/markets/{ticker}/candlesticks", params)
        cs = (d or {}).get("candlesticks", [])
    rows = []
    for c in cs:
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
