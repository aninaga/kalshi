"""Polymarket historical TRADES for NBA winner markets.

The CLOB exposes only mid history (``prices-history``) and a LIVE-only book, so
there is no historical order-book depth anywhere official (verified
2026-05-29 — see research/lake/RECOVERY_NOTES.md). But the data-api exposes
executed TRADES (size, price, side, timestamp) per market, which lets us
calibrate a REALISTIC fill model from real execution data instead of a
fabricated constant-depth book.

The trade filter is the market CONDITION ID (the ``0x…`` hash), NOT the CLOB
token id — the latter returns unrelated/global trades.
"""
from __future__ import annotations

import time

import requests

from . import polymarket_hist as pmh

DATA_API = "https://data-api.polymarket.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


def winner_condition_id(date: str, away_tri: str, home_tri: str):
    """Return the conditionId of the full-game winner market, or None."""
    event = pmh._find_event(date, away_tri, home_tri)
    if not event:
        return None
    for m in event.get("markets", []) or []:
        q = m.get("question") or ""
        if any(t in q for t in pmh._PARTIAL):
            continue
        if " vs. " in q and ":" not in q:
            return m.get("conditionId")
    return None


def fetch_trades(condition_id: str, limit: int = 500, max_pages: int = 4) -> list[dict]:
    """Executed trades for a market via the data-api, as a list of
    ``{ts, side, size, price}``.

    Paginated by ``offset``; stops when a short page is returned or pages are
    exhausted. A few hundred trades per market is ample for cost calibration.
    """
    out: list[dict] = []
    seen: set = set()
    offset = 0
    for _ in range(max_pages):
        try:
            r = _SESSION.get(
                f"{DATA_API}/trades",
                params={"market": condition_id, "limit": limit, "offset": offset},
                timeout=25,
            )
            r.raise_for_status()
            rows = r.json()
        except Exception:
            break
        if not isinstance(rows, list) or not rows:
            break
        new = 0
        for t in rows:
            key = (t.get("transactionHash"), t.get("timestamp"), t.get("asset"), t.get("size"))
            if key in seen:
                continue
            seen.add(key)
            new += 1
            out.append({
                "ts": t.get("timestamp"),
                "side": t.get("side"),
                "size": t.get("size"),
                "price": t.get("price"),
                "asset": t.get("asset"),
            })
        if len(rows) < limit or new == 0:
            break
        offset += limit
        time.sleep(0.2)
    return out
