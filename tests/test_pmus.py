"""Polymarket US (third venue): catalog mapping, book parsing, routing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp
from kalshi_arbitrage import pmus


def test_us_slug_roundtrip():
    assert pmus.us_slug("us:abc-def") == "abc-def"
    assert pmus.us_slug("us:abc-def#no") == "abc-def"
    assert pmus.us_slug("0xdeadbeef") is None


def test_fetch_pmus_maps_to_matcher_row_shape(monkeypatch):
    page = {"markets": [
        {"slug": "tec-nba-champ-2026-07-01-ny", "question": "2026 NBA Champion",
         "description": "Resolves to the team that wins the 2026 NBA Finals.",
         "endDate": "2026-07-01T00:00:00Z", "active": True, "closed": False,
         "archived": False,
         "marketSides": [{"description": "Knicks"}, {"description": "Not Knicks"}]},
        {"slug": "closed-one", "question": "Old", "active": True, "closed": True},
    ]}
    monkeypatch.setattr(pmus, "get",
                        lambda url, timeout=25: page if "offset=0" in url else {"markets": []})
    rows = pmus.fetch_pmus()
    assert len(rows) == 1
    r = rows[0]
    assert r["condition_id"] == "us:tec-nba-champ-2026-07-01-ny"
    assert r["question"] == "2026 NBA Champion"
    assert r["tokens"][0]["token_id"] == "us:tec-nba-champ-2026-07-01-ny"
    assert r["tokens"][1]["token_id"].endswith("#no")
    assert r["tokens"][0]["outcome"] == "Knicks"


def test_pmus_book_unified_semantics(monkeypatch):
    book = {"marketData": {
        "offers": [{"px": {"value": "0.64"}, "qty": "300"},
                   {"px": {"value": "0.63"}, "qty": "100"}],
        "bids": [{"px": {"value": "0.60"}, "qty": "500"}],
    }}
    monkeypatch.setattr(pmus, "get", lambda url, timeout=25: book)
    yes, no = pmus.pmus_book("any-slug")
    assert yes == [(0.63, 100.0), (0.64, 300.0)]
    assert no == [(0.40, 500.0)]            # 1 - bid


def test_pair_structures_routes_us_pairs_and_skips_intra(monkeypatch):
    pair = {"ktk": "K1", "pid": "us:slug-a", "kt": "T", "pt": "T",
            "polarity": "aligned", "uncertain": False,
            "tokens": [{"token_id": "us:slug-a", "outcome": "Yes"},
                       {"token_id": "us:slug-a#no", "outcome": "No"}]}
    monkeypatch.setattr(lp, "kalshi_book",
                        lambda tk: ([(0.40, 100)], [(0.70, 100)]))
    calls = {"n": 0}

    def fake_us_book(slug):
        calls["n"] += 1
        assert slug == "slug-a"
        return [(0.45, 100)], [(0.55, 100)]
    monkeypatch.setattr("kalshi_arbitrage.pmus.pmus_book", fake_us_book)
    out = lp.pair_structures(pair)
    # comp found: K-yes 0.40 + US-no 0.55 < $1; NO pm_intra for a unified book.
    assert [r["strategy"] for r in out] == ["comp"]
    assert out[0]["a_src"] == "K" and out[0]["b_src"] == "P"
    assert calls["n"] == 1                   # one fetch serves both sides


def test_ws_feed_ignores_synthetic_us_tokens():
    from kalshi_arbitrage.ws_books import WsBookFeed
    pairs = [{"ktk": "K1", "tokens": [{"token_id": "us:slug", "outcome": "Yes"},
                                      {"token_id": "us:slug#no", "outcome": "No"}]},
             {"ktk": "K2", "tokens": [{"token_id": "123", "outcome": "Yes"}]}]
    feed = WsBookFeed(pairs)
    assert feed.pm.token_ids == ["123"]
