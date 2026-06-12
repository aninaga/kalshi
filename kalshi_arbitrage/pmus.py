"""Polymarket US (polymarketexchange.com) — the legal-today third venue.

QCX LLC d/b/a Polymarket US: CFTC-regulated DCM, live to US users since
2025-12-03, public API since 2026-02-16 (verified 2026-06-10 against
docs.polymarket.us). Completely separate stack and liquidity from
polymarket.com's global book: fiat USD, slug-keyed markets, unified
per-market order book (like Kalshi — buying the short side hits the bid),
NO condition_ids/tokens, and an UNAUTHENTICATED public REST tier:

    GET https://gateway.polymarket.us/v1/markets          (catalog)
    GET https://gateway.polymarket.us/v1/markets/{slug}/book

Fee model: Theta * C * p * (1-p), taker Theta = 0.05 (2026-04-03 schedule)
— mathematically identical to the official PM-global parabolic model at
500 bps, so US legs are priced with the existing
``FeeModel.polymarket_taker_fee(price, count, 500)``. Rate limit 60 req/min
on the public tier: books are fetched only for verified pairs, and the
monitor rotates a bounded subset per sweep.

Integration trick: US markets are mapped into the SAME catalog-row shape
``live_probe.match_pairs`` consumes (synthetic "token" ids carry the slug,
prefixed ``us:``), so the matcher, verifier gates, watch-list cache,
reverification, and conflict arbitration all apply verbatim. Pricing routes
``us:``-prefixed pairs to :func:`pmus_book` and (lacking separate per-token
books) skips the pm_intra structure — a unified book cannot self-arb.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .live_probe import get

GATEWAY = "https://gateway.polymarket.us"
US_PREFIX = "us:"


def us_slug(token_or_pid: str) -> Optional[str]:
    """The PM-US market slug from a synthetic id, or None if not a US id."""
    s = str(token_or_pid or "")
    if not s.startswith(US_PREFIX):
        return None
    return s[len(US_PREFIX):].split("#", 1)[0]


def fetch_pmus(max_pages: int = 12) -> List[Dict]:
    """Active PM-US markets in the catalog-row shape match_pairs consumes."""
    rows, offset = [], 0
    for _ in range(max_pages):
        d = get(f"{GATEWAY}/v1/markets?limit=250&offset={offset}&active=true&closed=false")
        ms = (d or {}).get("markets") or []
        if not ms:
            break
        for m in ms:
            if not m.get("active") or m.get("closed") or m.get("archived"):
                continue
            slug = m.get("slug") or ""
            if not slug:
                continue
            sides = m.get("marketSides") or []
            # Outcome labels for polarity checks: the long side is the market's
            # nominal YES. Binary markets label sides Yes/No; sports moneylines
            # label them with team names.
            names = [str(s.get("description") or "") for s in sides[:2]] + ["Yes", "No"]
            rows.append({
                "condition_id": US_PREFIX + slug,
                "question": m.get("question") or "",
                "description": m.get("description") or "",
                "end_date_iso": m.get("endDate"),
                "active": True, "closed": False,
                "tokens": [
                    {"token_id": US_PREFIX + slug, "outcome": names[0] or "Yes"},
                    {"token_id": US_PREFIX + slug + "#no", "outcome": names[1] or "No"},
                ],
            })
        offset += len(ms)
        if len(ms) < 250:
            break
    return rows


def _px(level: Dict) -> Tuple[float, float]:
    px = level.get("px") or {}
    return float(px.get("value") or 0), float(level.get("qty") or 0)


def pmus_book(slug: str) -> Tuple[list, list]:
    """(yes_ask_ladder, no_ask_ladder), ascending — unified-book semantics:
    buying the short side at p means hitting the long bid at 1-p."""
    d = get(f"{GATEWAY}/v1/markets/{slug}/book")
    md = (d or {}).get("marketData") or {}
    offers = sorted(_px(l) for l in md.get("offers") or [])
    bids = [_px(l) for l in md.get("bids") or []]
    no_asks = sorted((round(1.0 - p, 6), q) for p, q in bids if p > 0)
    return [(p, q) for p, q in offers if q > 0], [(p, q) for p, q in no_asks if q > 0]


def pmus_description(pid: str) -> Optional[str]:
    """Rules text by synthetic id (reverification path). None on failure."""
    slug = us_slug(pid)
    if not slug:
        return None
    d = get(f"{GATEWAY}/v1/market/slug/{slug}")
    m = (d or {}).get("market") or d or {}
    desc = m.get("description")
    return str(desc) if desc is not None else None
