"""Tests for the crypto provider's pure transforms (no network, no cache).

``build_panel`` turns a cached threshold-book record into a lab Panel: NATIVE
cumulative ladder from real single-leg threshold quotes, implied-price mid,
measured half-spread feature, exact settlement from ``expiration_value`` (with
a YES/NO-transition fallback), timed duration, asset-x-hour cluster key.
"""
from __future__ import annotations

import unittest

import numpy as np

from research.lab.providers import crypto as C


def _candles(probs, *, t0=1_780_000_000, spread=0.02):
    """One candle per minute with a two-sided quote around each prob."""
    out = []
    for i, p in enumerate(probs):
        out.append({"ts": t0 + 60 * i, "yes_bid": max(p - spread / 2, 0.01),
                    "yes_ask": min(p + spread / 2, 0.99), "mid": p, "last": p})
    return out


def _event(n=30, expiration_value="61666.23"):
    """A 4-threshold book around $61.6k settling at 61,666.23.

    Three floor-only "$X or above" thresholds plus one cap-only "Y or below"
    (whose quote is the COMPLEMENT of P(px > cap)). Mids drift so the implied
    price rises over the window.
    """
    ramp = np.linspace(0.0, 0.10, n)
    return {
        "event_ticker": "KXBTCD-26JUN0919",
        "series": "KXBTCD",
        "asset": "BTC",
        "date": "2026-06-09",
        "hour": 19,
        "expiration_value": expiration_value,
        "markets": [
            # P(px > 61399.99) ~ 0.85 rising
            {"ticker": "T61399.99", "floor": 61399.99, "cap": None,
             "result": "yes", "candles": _candles(0.85 + ramp)},
            # P(px > 61599.99) ~ 0.55 rising
            {"ticker": "T61599.99", "floor": 61599.99, "cap": None,
             "result": "yes", "candles": _candles(0.55 + ramp)},
            # P(px > 61799.99) ~ 0.25 rising
            {"ticker": "T61799.99", "floor": 61799.99, "cap": None,
             "result": "no", "candles": _candles(0.25 + ramp)},
            # cap-only "61,999.99 or below": quotes P(px <= 61999.99) ~ 0.92
            # falling, so the ladder gets P(px > 61999.99) = 1 - quote.
            {"ticker": "B61999.99", "floor": None, "cap": 61999.99,
             "result": "yes", "candles": _candles(0.92 - ramp / 2)},
        ],
    }


class TestThresholdGeometry(unittest.TestCase):
    def test_boundary_orientation(self) -> None:
        b, o = C._boundary({"floor": 61599.99, "cap": None})
        self.assertEqual((b, o), (61599.99, +1))
        b, o = C._boundary({"floor": None, "cap": 61999.99})
        self.assertEqual((b, o), (61999.99, -1))
        # Range buckets are not single-leg cumulative quotes -> skipped.
        self.assertIsNone(C._boundary({"floor": 61500, "cap": 61599.99}))
        self.assertIsNone(C._boundary({"floor": None, "cap": None}))

    def test_event_date_hour_parses_both_ticker_eras(self) -> None:
        self.assertEqual(C.event_date_hour("KXBTCD-26JUN0919"), ("2026-06-09", 19))
        self.assertEqual(C.event_date_hour("BTCD-24MAR18-16"), ("2024-03-18", 16))
        self.assertIsNone(C.event_date_hour("NODATE"))
        self.assertEqual(C.event_date("KXETHD-26MAY0214"), "2026-05-02")

    def test_sort_key_is_chronological_within_a_day(self) -> None:
        ticks = ["KXBTCD-26JUN0919", "KXBTCD-26JUN0905", "KXBTCD-26JUN0817"]
        self.assertEqual(sorted(ticks, key=C._sort_key),
                         ["KXBTCD-26JUN0817", "KXBTCD-26JUN0905",
                          "KXBTCD-26JUN0919"])


class TestBuildPanel(unittest.TestCase):
    def test_panel_contract(self) -> None:
        p = C.build_panel(_event(), split_of=lambda gid: "train")
        self.assertIsNotNone(p)
        self.assertEqual(p.market, C.COIN_PX)
        self.assertEqual(p.game_id, "KXBTCD-26JUN0919")
        # Cluster key: asset x hour-of-day (concentration/knockout dimension).
        self.assertEqual(p.home_team, "BTCH19")
        self.assertEqual(p.away_team, "BTCH19")
        n = p.n
        for arr in (p.minute_ts, p.elapsed_sec, p.mid):
            self.assertEqual(len(arr), n)
        self.assertEqual(sorted(p.ladder),
                         [61399.99, 61599.99, 61799.99, 61999.99])
        for arr in p.ladder.values():
            self.assertEqual(len(arr), n)
            self.assertTrue((arr[np.isfinite(arr)] >= 0).all())
            self.assertTrue((arr[np.isfinite(arr)] <= 1).all())
        # Native cumulative monotonicity across boundaries, per minute.
        a, b, c, d = (p.ladder[61399.99], p.ladder[61599.99],
                      p.ladder[61799.99], p.ladder[61999.99])
        ok = np.isfinite(a) & np.isfinite(b) & np.isfinite(c) & np.isfinite(d)
        self.assertTrue((a[ok] >= b[ok] - 1e-9).all())
        self.assertTrue((b[ok] >= c[ok] - 1e-9).all())
        self.assertTrue((c[ok] >= d[ok] - 1e-9).all())

    def test_cap_only_market_contributes_complement(self) -> None:
        p = C.build_panel(_event(), split_of=lambda gid: "train")
        # quote at bar 0 is 0.92 -> P(px > 61999.99) = 0.08
        self.assertAlmostEqual(p.ladder[61999.99][0], 0.08, places=6)

    def test_mid_is_half_crossing_inversion(self) -> None:
        p = C.build_panel(_event(), split_of=lambda gid: "train")
        # At bar 0, P(>61599.99)=0.55 and P(>61799.99)=0.25: the 0.5 crossing
        # interpolates inside (61599.99, 61799.99).
        self.assertTrue(61599.99 < p.mid[0] < 61799.99)
        # Mids rise over the window -> implied price rises.
        self.assertGreater(p.mid[-1], p.mid[0])

    def test_settlement_is_exact_expiration_value(self) -> None:
        p = C.build_panel(_event(), split_of=lambda gid: "train")
        self.assertEqual(p.final_total, 61666.23)
        self.assertEqual(p.final_margin, 61666.23)  # both outcome fields
        # Exact boundary comparisons agree with the venue's results.
        self.assertTrue(p.final_total > 61599.99)
        self.assertFalse(p.final_total > 61799.99)

    def test_settlement_fallback_from_result_transition(self) -> None:
        p = C.build_panel(_event(expiration_value=None),
                          split_of=lambda gid: "train")
        # Highest YES floor 61599.99, lowest NO floor 61799.99 -> midpoint.
        self.assertAlmostEqual(p.final_total, 61699.99, places=6)
        self.assertTrue(p.final_total > 61599.99)
        self.assertFalse(p.final_total > 61799.99)

    def test_ambiguous_fallback_returns_none_outcome(self) -> None:
        rec = _event(expiration_value=None)
        rec["markets"][2]["result"] = "yes"   # YES above a NO? inconsistent...
        rec["markets"][1]["result"] = "no"    # max YES floor > min NO floor
        p = C.build_panel(rec, split_of=lambda gid: "train")
        self.assertIsNone(p.final_total)

    def test_timed_event_duration(self) -> None:
        p = C.build_panel(_event(n=30), split_of=lambda gid: "train")
        self.assertEqual(p.duration_sec, 29 * 60.0)
        self.assertEqual(p.elapsed_sec[0], 0.0)
        # Pace signals never fire on crypto (no accumulating total).
        from research.lab import signals
        self.assertTrue(np.isnan(signals.pace_projection(p)).all())

    def test_measured_half_spread_exported(self) -> None:
        p = C.build_panel(_event(), split_of=lambda gid: "train")
        hs = p.features["half_spread"]
        ok = np.isfinite(hs)
        self.assertTrue(ok.any())
        np.testing.assert_allclose(hs[ok], 0.01, atol=1e-6)  # spread 0.02 / 2

    def test_unusable_book_returns_none(self) -> None:
        rec = _event()
        for m in rec["markets"]:
            m["candles"] = []
        self.assertIsNone(C.build_panel(rec))
        rec = _event()
        rec["markets"] = rec["markets"][:1]   # single threshold: no ladder
        self.assertIsNone(C.build_panel(rec))


class TestProviderSurface(unittest.TestCase):
    def test_market_guard(self) -> None:
        prov = C.CryptoProvider()
        with self.assertRaises(ValueError):
            prov.enumerate_events("total")
        with self.assertRaises(ValueError):
            prov.available("temp")

    def test_family_resolution(self) -> None:
        import importlib

        providers = importlib.import_module("research.lab.providers")
        self.assertEqual(providers.family_of(C.COIN_PX), "crypto")
        self.assertIn("crypto", providers.families())


if __name__ == "__main__":
    unittest.main()
