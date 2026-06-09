"""Tests for the weather provider's pure transforms (no network, no cache).

``build_panel`` turns a cached bucket-book record into a lab Panel: cumulative
boundary ladder from real bucket quotes, implied-temperature mid, measured
half-spread feature, exact settlement vs every boundary, timed duration.
"""
from __future__ import annotations

import unittest

import numpy as np

from research.lab.providers import weather as W


def _candles(probs, *, t0=1_780_000_000, spread=0.04):
    """One candle per minute with a two-sided quote around each prob."""
    out = []
    for i, p in enumerate(probs):
        out.append({"ts": t0 + 60 * i, "yes_bid": max(p - spread / 2, 0.01),
                    "yes_ask": min(p + spread / 2, 0.99), "mid": p, "last": p})
    return out


def _event(n=30):
    """A 4-bucket book: <87 | 87-88 | 89-90 | >90, settling in 89-90.

    Mids drift so the implied temperature rises over the window.
    """
    ramp = np.linspace(0.0, 0.15, n)
    return {
        "event_ticker": "KXHIGHNY-26JUN07",
        "series": "KXHIGHNY",
        "city": "NYC",
        "date": "2026-06-07",
        "markets": [
            {"ticker": "T87", "floor": None, "cap": 87, "result": "no",
             "candles": _candles(0.30 - ramp)},
            {"ticker": "B87.5", "floor": 87, "cap": 88, "result": "no",
             "candles": _candles(0.30 - ramp / 2)},
            {"ticker": "B89.5", "floor": 89, "cap": 90, "result": "yes",
             "candles": _candles(0.25 + ramp)},
            {"ticker": "T90", "floor": 90, "cap": None, "result": "no",
             "candles": _candles(0.15 + ramp / 2)},
        ],
    }


class TestBucketGeometry(unittest.TestCase):
    def test_bounds_and_representatives(self) -> None:
        lo, hi, rep = W._bucket_bounds({"floor": None, "cap": 87})
        self.assertEqual((hi, rep), (86.5, 86.0))
        lo, hi, rep = W._bucket_bounds({"floor": 87, "cap": 88})
        self.assertEqual((lo, hi, rep), (86.5, 88.5, 87.5))
        lo, hi, rep = W._bucket_bounds({"floor": 90, "cap": None})
        self.assertEqual((lo, rep), (90.5, 91.0))

    def test_event_date_parses_ticker(self) -> None:
        self.assertEqual(W.event_date("KXHIGHNY-26JUN07"), "2026-06-07")
        self.assertEqual(W.event_date("HIGHNY-24MAR31"), "2024-03-31")
        self.assertIsNone(W.event_date("NODATE"))


class TestBuildPanel(unittest.TestCase):
    def test_panel_contract(self) -> None:
        p = W.build_panel(_event(), split_of=lambda gid: "train")
        self.assertIsNotNone(p)
        self.assertEqual(p.market, W.TEMP)
        self.assertEqual(p.game_id, "KXHIGHNY-26JUN07")
        self.assertEqual(p.home_team, "NYC")
        self.assertEqual(p.away_team, "NYC")     # concentration keys by city
        n = p.n
        for arr in (p.minute_ts, p.elapsed_sec, p.mid):
            self.assertEqual(len(arr), n)
        # Boundaries between the 4 buckets: 86.5, 88.5, 90.5.
        self.assertEqual(sorted(p.ladder), [86.5, 88.5, 90.5])
        for arr in p.ladder.values():
            self.assertEqual(len(arr), n)
            self.assertTrue((arr[np.isfinite(arr)] >= 0).all())
            self.assertTrue((arr[np.isfinite(arr)] <= 1).all())
        # Cumulative monotonicity: P(>86.5) >= P(>88.5) >= P(>90.5) per minute.
        a, b, c = (p.ladder[86.5], p.ladder[88.5], p.ladder[90.5])
        ok = np.isfinite(a) & np.isfinite(b) & np.isfinite(c)
        self.assertTrue((a[ok] >= b[ok] - 1e-9).all())
        self.assertTrue((b[ok] >= c[ok] - 1e-9).all())

    def test_settlement_is_yes_bucket_representative(self) -> None:
        p = W.build_panel(_event(), split_of=lambda gid: "train")
        self.assertEqual(p.final_total, 89.5)    # 89-90 bucket midpoint
        self.assertEqual(p.final_margin, 89.5)   # both outcome fields carry it
        # Any in-bucket representative settles every boundary exactly:
        self.assertTrue(p.final_total > 88.5)
        self.assertFalse(p.final_total > 90.5)

    def test_timed_event_duration(self) -> None:
        p = W.build_panel(_event(n=30), split_of=lambda gid: "train")
        self.assertEqual(p.duration_sec, 29 * 60.0)
        self.assertEqual(p.elapsed_sec[0], 0.0)
        # Pace signals never fire on weather (no accumulating total).
        from research.lab import signals
        self.assertTrue(np.isnan(signals.pace_projection(p)).all())

    def test_measured_half_spread_exported(self) -> None:
        p = W.build_panel(_event(), split_of=lambda gid: "train")
        hs = p.features["half_spread"]
        ok = np.isfinite(hs)
        self.assertTrue(ok.any())
        np.testing.assert_allclose(hs[ok], 0.02, atol=1e-6)  # spread 0.04 / 2

    def test_ambiguous_settlement_returns_none_outcome(self) -> None:
        rec = _event()
        rec["markets"][0]["result"] = "yes"      # two YES buckets -> ambiguous
        p = W.build_panel(rec, split_of=lambda gid: "train")
        self.assertIsNone(p.final_total)

    def test_unusable_book_returns_none(self) -> None:
        rec = _event()
        for m in rec["markets"]:
            m["candles"] = []
        self.assertIsNone(W.build_panel(rec))


class TestProviderSurface(unittest.TestCase):
    def test_market_guard(self) -> None:
        prov = W.WeatherProvider()
        with self.assertRaises(ValueError):
            prov.enumerate_events("total")
        with self.assertRaises(ValueError):
            prov.available("widget")

    def test_family_resolution(self) -> None:
        import importlib

        providers = importlib.import_module("research.lab.providers")
        self.assertEqual(providers.family_of(W.TEMP), "weather")


if __name__ == "__main__":
    unittest.main()
