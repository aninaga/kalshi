"""Generic Kalshi historical fetcher for the provider seam.

Series-agnostic: enumerates SETTLED events for any Kalshi series ticker, the
markets nested in each event, and 1-minute candlesticks per market — the same
live-tier + historical-tier fallback pattern proven by the NBA study's
fetcher, re-implemented standalone so non-NBA providers do not import
``nba_odds_study``. Read-only; no auth needed for historical/public data.

Candles carry REAL two-sided quotes (``yes_bid``/``yes_ask``), so providers
can build panels with measured spreads instead of assumed ones.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "curl/8"})
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _get(path: str, params: Optional[dict] = None, tries: int = 6):
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


def _money(d: dict, key: str):
    """A price field's close from a candlestick, in dollars.

    Live candles expose ``close_dollars``; historical-tier candles expose
    ``close`` as string-dollars. Handle both (the NBA recovery's lesson).
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


def settled_events(series_ticker: str, *, max_pages: int = 60,
                   limit: int = 200) -> list[dict]:
    """Every SETTLED event for ``series_ticker`` (newest first), paged fully.

    Returns the raw event dicts (``event_ticker``, ``title``, ...). Includes
    pre-rename tickers (e.g. ``HIGHNY-...`` under series ``KXHIGHNY``).
    """
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params = {"series_ticker": series_ticker, "status": "settled",
                  "limit": limit}
        if cursor:
            params["cursor"] = cursor
        d = _get("/events", params)
        evs = (d or {}).get("events", [])
        out.extend(evs)
        cursor = (d or {}).get("cursor")
        if not cursor or not evs:
            break
    return out


def event_markets(event_ticker: str) -> list[dict]:
    """All markets nested in one event (live tier, then historical tier)."""
    d = _get(f"/events/{event_ticker}", {"with_nested_markets": "true"})
    ms = (d or {}).get("markets") or (d or {}).get("event", {}).get("markets") or []
    if ms:
        return ms
    d = _get("/historical/markets", {"event_ticker": event_ticker})
    return (d or {}).get("markets") or []


def fetch_candles(series_ticker: str, market_ticker: str, start_ts: int,
                  end_ts: int, period: int = 1) -> list[dict]:
    """1-minute price history: ``[{ts, yes_bid, yes_ask, mid, last}]``.

    Live series endpoint first; settled markets that 404 there return full
    history under the historical tier. ``mid`` is the two-sided bid/ask
    midpoint when both sides are quoted, else the last trade.
    """
    params = {"start_ts": int(start_ts), "end_ts": int(end_ts),
              "period_interval": period}
    d = _get(f"/series/{series_ticker}/markets/{market_ticker}/candlesticks", params)
    cs = (d or {}).get("candlesticks", [])
    if not cs:
        d = _get(f"/historical/markets/{market_ticker}/candlesticks", params)
        cs = (d or {}).get("candlesticks", [])
    rows = []
    for c in cs:
        bid = _money(c, "yes_bid")
        ask = _money(c, "yes_ask")
        last = _money(c, "price")
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2
        else:
            mid = last
        rows.append({"ts": c.get("end_period_ts"), "yes_bid": bid,
                     "yes_ask": ask, "mid": mid, "last": last})
    return rows


def first_number(*texts) -> Optional[float]:
    """First numeric token across the given strings (strike extraction)."""
    for t in texts:
        if not t:
            continue
        m = _NUM_RE.findall(str(t))
        if m:
            return float(m[0])
    return None
