"""Tests for the family-generic live seam + the two MEASURE-ONLY pilots.

Contracts under test (no network; every live seam injected with synthetic
event records):
  * the :data:`paper.LIVE_FAMILIES` hook registry (weather + crypto) and the
    per-row family dispatch in settle (a crypto row settles through the
    crypto panel builder without any injectable panel seam);
  * ``btc_9599_pilot``'s FROZEN entry predicate, clause by clause — asset,
    settlement hour, exact 1c spread, minutes-to-settle band, 95-99c favorite
    band on BOTH legs, entry-minute volume floor, the all-in cost line, the
    one-entry-per-event-SIDE cap, and over/under normalization for floor- and
    cap-oriented thresholds — plus the harness gates (current-signal,
    event_gate fetch hygiene, idempotency) and crypto settlement math;
  * ``wx_fillprobe``'s city/local-hour entry gate and 60-minute horizon, and
    that ``weather_maker_v1``'s tenant (``forecast_gap_maker``) is untouched.
"""
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from research.lab import paper

_ET = ZoneInfo("America/New_York")

# KXBTCD-26JUN1120 settles 2026-06-11 20:00 ET (EDT) = 1781222400.
_BTC_TICK = "KXBTCD-26JUN1120"
_SETTLE = datetime(2026, 6, 11, 20, tzinfo=_ET).timestamp()
_W_OPEN = _SETTLE - 3600.0            # hourly quote window opens 19:00 ET


def _cmkt(ticker, *, floor=None, cap=None, bid, ask, vol, t0, n=21,
          per_min_vol=0.0):
    """One synthetic threshold market: constant top-of-book, minute candles."""
    candles = [{"ts": int(t0 + 60 * i), "yes_bid": bid, "yes_ask": ask,
                "mid": (bid + ask) / 2.0, "last": None,
                "volume": per_min_vol} for i in range(n)]
    return {"ticker": ticker, "floor": floor, "cap": cap, "result": "",
            "volume": vol, "candles": candles}


def _crec(markets, *, tick=_BTC_TICK, asset="BTC", expiration_value=None):
    return {"event_ticker": tick, "series": "KXBTCD", "asset": asset,
            "date": "2026-06-11", "hour": 20,
            "expiration_value": expiration_value,
            "close_ts": _SETTLE, "markets": markets}


# Default book: last candle at _W_OPEN + 20 min = 19:20 ET -> mts = 40.
def _markets(t0=_W_OPEN, n=21):
    return [
        # YES favorite (floor): ask 97c, spread 1c, cum vol 500.
        _cmkt("T107000", floor=106999.99, bid=0.96, ask=0.97, vol=500,
              t0=t0, n=n),
        # NO favorite (floor): yes bid 3c -> NO ask 97c, cum vol 300.
        _cmkt("T109000", floor=108999.99, bid=0.03, ask=0.04, vol=300,
              t0=t0, n=n),
    ]


_NOW = _W_OPEN + 20 * 60 + 60         # one minute after the last candle


def _scan(rec, now_ts=_NOW):
    return paper.STRATEGIES["btc_9599_pilot"]["factory"]()(rec, now_ts=now_ts)


class TestBtcScannerPredicate(unittest.TestCase):
    def test_both_favorite_legs_qualify(self) -> None:
        cands = _scan(_crec(_markets()))
        self.assertEqual({c["side"] for c in cands}, {"over", "under"})
        over = next(c for c in cands if c["side"] == "over")
        self.assertEqual(over["leg"], "yes")
        self.assertEqual(over["ticker"], "T107000")
        self.assertEqual(over["strike"], 106999.99)
        self.assertEqual(over["fill_price"], 0.97)       # taker at the ask
        self.assertEqual(over["fee"], 0.01)              # ceil'd Kalshi taker
        self.assertAlmostEqual(over["all_in"], 0.98, places=9)
        self.assertAlmostEqual(over["minutes_to_settle"], 40.0)
        self.assertEqual(over["volume_at_entry"], 500.0)
        under = next(c for c in cands if c["side"] == "under")
        self.assertEqual(under["leg"], "no")
        self.assertEqual(under["strike"], 108999.99)
        self.assertAlmostEqual(under["fill_price"], 0.97)  # 1 - yes_bid
        self.assertAlmostEqual(under["all_in"], 0.98, places=9)

    def test_wrong_hour_rejected(self) -> None:
        # Same book, hour-19 event: the sleeve is the hour-20 cell ONLY.
        settle19 = datetime(2026, 6, 11, 19, tzinfo=_ET).timestamp()
        rec = _crec(_markets(t0=settle19 - 2400),
                    tick="KXBTCD-26JUN1119")
        self.assertEqual(_scan(rec, now_ts=settle19 - 2340), [])

    def test_eth_rejected(self) -> None:
        rec = _crec(_markets(), tick="KXETHD-26JUN1120", asset="ETH")
        self.assertEqual(_scan(rec), [])

    def test_spread_must_be_exactly_1c(self) -> None:
        m = _cmkt("T108000", floor=107999.99, bid=0.95, ask=0.97, vol=900,
                  t0=_W_OPEN)
        self.assertEqual(_scan(_crec([m])), [])

    def test_volume_floor_100(self) -> None:
        m = _cmkt("T106000", floor=105999.99, bid=0.96, ask=0.97, vol=99,
                  t0=_W_OPEN)
        self.assertEqual(_scan(_crec([m])), [])

    def test_volume_is_measured_at_the_entry_minute(self) -> None:
        # Cumulative 110 now, but 1 contract/min printed in the 20 minutes
        # AFTER... per-minute prints after the entry bar are subtracted; here
        # the entry bar IS the last bar, so nothing is subtracted and 110
        # passes; with the entry quote only on the FIRST bar, the 20 later
        # prints are subtracted: 110 - 20 = 90 < 100 -> rejected.
        m = _cmkt("T107000", floor=106999.99, bid=0.96, ask=0.97, vol=110,
                  t0=_W_OPEN, per_min_vol=1.0)
        self.assertEqual(len(_scan(_crec([m]))), 1)
        m2 = _cmkt("T107000", floor=106999.99, bid=0.96, ask=0.97, vol=110,
                   t0=_W_OPEN, per_min_vol=1.0)
        for c in m2["candles"][1:]:
            c["yes_bid"] = c["yes_ask"] = None       # quote only at bar 0
        self.assertEqual(_scan(_crec([m2])), [])

    def test_minutes_to_settle_band_edges(self) -> None:
        def at_mts(mts):
            t_last = _SETTLE - mts * 60.0
            m = _cmkt("T107000", floor=106999.99, bid=0.96, ask=0.97,
                      vol=500, t0=t_last, n=1)
            return _scan(_crec([m]), now_ts=t_last + 60.0)
        self.assertEqual(len(at_mts(31.0)), 1)
        self.assertEqual(len(at_mts(60.0)), 1)
        self.assertEqual(at_mts(30.0), [])
        self.assertEqual(at_mts(61.0), [])

    def test_band_and_cost_line(self) -> None:
        def at_ask(bid, ask):
            m = _cmkt("T107000", floor=106999.99, bid=bid, ask=ask, vol=500,
                      t0=_W_OPEN)
            return _scan(_crec([m]))
        self.assertEqual(at_ask(0.93, 0.94), [])          # below the band
        self.assertEqual(len(at_ask(0.94, 0.95)), 1)      # band lower edge
        self.assertEqual(len(at_ask(0.97, 0.98)), 1)      # passes cost line
        # 99c ask is IN the frozen band but fails the frozen cost line:
        # all_in 1.00 is not < payoff(1.0) - fee(0.01) + 1c = 1.00.
        self.assertEqual(at_ask(0.98, 0.99), [])

    def test_one_entry_per_side_picks_highest_volume(self) -> None:
        ms = _markets() + [
            _cmkt("T106900", floor=106899.99, bid=0.95, ask=0.96, vol=800,
                  t0=_W_OPEN)]
        cands = _scan(_crec(ms))
        over = [c for c in cands if c["side"] == "over"]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["ticker"], "T106900")    # vol 800 > 500

    def test_cap_only_threshold_normalizes_to_under(self) -> None:
        # "<= cap" market: buying its YES pays when the price ends BELOW.
        m = _cmkt("B107900", cap=107899.99, bid=0.96, ask=0.97, vol=200,
                  t0=_W_OPEN)
        cands = _scan(_crec([m]))
        self.assertEqual(len(cands), 1)
        self.assertEqual((cands[0]["side"], cands[0]["leg"]), ("under", "yes"))
        self.assertEqual(cands[0]["strike"], 107899.99)


class _PilotBookCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = paper.PAPER_DIR
        paper.PAPER_DIR = Path(self._tmp.name)
        self.addCleanup(setattr, paper, "PAPER_DIR", self._old)

    def _open(self, *, rec=None, now_ts=_NOW, counter=None):
        rec = rec or _crec(_markets())
        events = [{"event_ticker": rec["event_ticker"],
                   "asset": rec.get("asset", "BTC"), "series": "KXBTCD"}]

        def live_rec(ev, **kw):
            if counter is not None:
                counter.append(ev)
            return rec

        return paper.scan_and_open(
            "btc_9599_pilot", book="t", recent_min=30.0, now_ts=now_ts,
            live_events=lambda: events, live_rec=live_rec, log=lambda *a: None)


class TestBtcHarness(_PilotBookCase):
    def test_open_records_one_row_per_event_side(self) -> None:
        opened = self._open()
        self.assertEqual(len(opened), 2)
        ids = {r["id"] for r in opened}
        self.assertEqual(ids, {f"btc_9599_pilot|{_BTC_TICK}|over",
                               f"btc_9599_pilot|{_BTC_TICK}|under"})
        for r in opened:
            self.assertEqual(r["status"], "open")
            self.assertEqual(r["family"], "crypto")
            self.assertEqual(r["market"], "coin_price")
            self.assertEqual(r["asset"], "BTC")

    def test_idempotent_across_passes(self) -> None:
        self.assertEqual(len(self._open()), 2)
        counter: list = []
        self.assertEqual(self._open(counter=counter), [])
        self.assertEqual(counter, [])        # both sides held: fetch skipped

    def test_stale_signal_rejected(self) -> None:
        # 35 min after the last quoted minute: inside the event_gate window
        # but past the current-signal gate.
        self.assertEqual(self._open(now_ts=_NOW + 34 * 60), [])

    def test_event_gate_blocks_the_fetch(self) -> None:
        counter: list = []
        opened = self._open(now_ts=_SETTLE - 2 * 3600, counter=counter)
        self.assertEqual((opened, counter), ([], []))

    def test_settles_through_the_crypto_family_hooks(self) -> None:
        opened = self._open()
        self.assertEqual(len(opened), 2)
        settled_rec = _crec(_markets(n=21), expiration_value=107450.19)
        out = paper.settle_book(
            "t", live_rec=lambda ev, **kw: settled_rec, log=lambda *a: None)
        self.assertEqual(len(out), 2)
        by_id = {r["id"]: r for r in out}
        over = by_id[f"btc_9599_pilot|{_BTC_TICK}|over"]
        under = by_id[f"btc_9599_pilot|{_BTC_TICK}|under"]
        # outcome is the EXACT index price; both favorites won here.
        self.assertEqual(over["outcome"], 107450.19)
        self.assertEqual((over["payoff"], under["payoff"]), (1.0, 1.0))
        self.assertAlmostEqual(over["pnl"], 1.0 - 0.98, places=9)
        self.assertAlmostEqual(under["pnl"], 1.0 - 0.98, places=9)
        st = paper.status("t")
        self.assertEqual((st["open"], st["settled"]), (0, 2))

    def test_unresolved_event_stays_open(self) -> None:
        self._open()
        out = paper.settle_book(
            "t", live_rec=lambda ev, **kw: _crec(_markets()),
            log=lambda *a: None)
        self.assertEqual(out, [])
        self.assertEqual(paper.status("t")["open"], 2)


# --------------------------------------------------------------------------- #
# wx_fillprobe (weather maker FILL PROBE, 60m rests, NYC/CHI, AM-to-mid-PM)
# --------------------------------------------------------------------------- #
def _wcandles(probs, *, t0, spread=0.04, prints=None):
    prints = prints or {}
    rows = []
    for i, p in enumerate(probs):
        bid, ask = max(p - spread / 2, 0.01), min(p + spread / 2, 0.99)
        low, vol = prints.get(i, (None, 0.0))
        rows.append({"ts": t0 + 60 * i, "yes_bid": bid, "yes_ask": ask,
                     "mid": p, "last": p,
                     "price_open": low, "price_high": low, "price_low": low,
                     "price_close": low, "volume": vol,
                     "yes_bid_low": bid, "yes_bid_high": bid,
                     "yes_ask_low": ask, "yes_ask_high": ask,
                     "open_interest": 0.0})
    return rows


def _wrec(*, city="NYC", t0, n=75, results=("", "", "", ""),
          prints_b895=None):
    series = {"NYC": "KXHIGHNY", "CHI": "KXHIGHCHI", "MIA": "KXHIGHMIA"}[city]
    i = np.arange(n)
    return {
        "event_ticker": f"{series}-99JAN01", "series": series, "city": city,
        "date": "2099-01-01", "close_ts": t0 + 86400,
        "markets": [
            {"ticker": "T87", "floor": None, "cap": 87, "result": results[0],
             "candles": _wcandles(np.full(n, 0.10), t0=t0)},
            {"ticker": "B87.5", "floor": 87, "cap": 88, "result": results[1],
             "candles": _wcandles(np.full(n, 0.35), t0=t0)},
            {"ticker": "B89.5", "floor": 89, "cap": 90, "result": results[2],
             "candles": _wcandles(0.40 + 0.0005 * i, t0=t0,
                                  prints=prints_b895)},
            {"ticker": "T90", "floor": 90, "cap": None, "result": results[3],
             "candles": _wcandles(0.15 + 0.0002 * i, t0=t0)},
        ],
    }


def _wpanel(rec):
    panel = paper._build_live_panel(rec)
    if panel is not None:
        panel.features["forecast_high_f"] = np.full(panel.n, 93.0)
    return panel


_SIGNAL_BAR = 72          # first bar with elapsed_frac >= 0.05 of the 24h window


def _local_ts(city: str, hour: int, minute: int = 30) -> float:
    tz = {"NYC": "America/New_York", "CHI": "America/Chicago",
          "MIA": "America/New_York"}[city]
    return datetime(2026, 6, 8, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


def _t0_for(city: str, hour: int) -> int:
    """Anchor t0 so the frozen signal bar lands at ``hour``:30 local time."""
    return int(_local_ts(city, hour)) - _SIGNAL_BAR * 60


class TestFillprobeGate(unittest.TestCase):
    def test_local_hour_band(self) -> None:
        g = paper._wx_fillprobe_gate
        self.assertFalse(g({"city": "NYC"}, _local_ts("NYC", 5)))
        self.assertTrue(g({"city": "NYC"}, _local_ts("NYC", 6)))
        self.assertTrue(g({"city": "NYC"}, _local_ts("NYC", 10)))
        self.assertTrue(g({"city": "NYC"}, _local_ts("NYC", 15)))
        self.assertFalse(g({"city": "NYC"}, _local_ts("NYC", 16)))
        self.assertFalse(g({"city": "NYC"}, _local_ts("NYC", 20)))

    def test_cities(self) -> None:
        g = paper._wx_fillprobe_gate
        self.assertTrue(g({"city": "CHI"}, _local_ts("CHI", 15)))
        self.assertFalse(g({"city": "MIA"}, _local_ts("MIA", 10)))
        self.assertFalse(g({"city": "DEN"}, _local_ts("NYC", 10)))
        self.assertFalse(g({"city": ""}, _local_ts("NYC", 10)))


class TestFillprobeLifecycle(_PilotBookCase):
    def _rest(self, *, city="NYC", hour=10, n=75):
        t0 = _t0_for(city, hour)
        rec = _wrec(city=city, t0=t0, n=n)
        events = [{"event_ticker": rec["event_ticker"], "city": city,
                   "series": rec["series"]}]
        signal_ts = t0 + _SIGNAL_BAR * 60
        rested = paper.scan_and_rest(
            "wx_fillprobe", book="t", recent_min=30.0,
            now_ts=signal_ts + 5 * 60,
            live_events=lambda: events, live_rec=lambda ev, **kw: rec,
            live_panel=_wpanel, log=lambda *a: None)
        return rested, t0

    def test_rests_inside_the_window_with_60m_horizon(self) -> None:
        rested, t0 = self._rest(city="NYC", hour=10)
        self.assertEqual(len(rested), 1)
        row = rested[0]
        self.assertEqual(row["status"], "resting")
        self.assertEqual(row["horizon_min"], 60.0)
        entry_ts = t0 + (_SIGNAL_BAR + 1) * 60       # i+1, the frozen latency
        self.assertEqual(row["entry_ts"], entry_ts)
        self.assertEqual(row["horizon_end_ts"], entry_ts + 3600.0)
        self.assertEqual(row["maker_fee_c"], 0.0)
        self.assertEqual(row["strike"], 88.5)        # same frozen ATM snap

    def test_chicago_passes_the_gate(self) -> None:
        rested, _ = self._rest(city="CHI", hour=14)
        self.assertEqual(len(rested), 1)

    def test_evening_signal_blocked(self) -> None:
        rested, _ = self._rest(city="NYC", hour=20)
        self.assertEqual(rested, [])

    def test_excluded_city_blocked(self) -> None:
        rested, _ = self._rest(city="MIA", hour=10)
        self.assertEqual(rested, [])

    def test_print_at_45_minutes_fills_under_the_60m_horizon(self) -> None:
        rested, t0 = self._rest(city="NYC", hour=10)
        rest = rested[0]["rest_price"]
        # A real print 45 min after entry: bar 73 + 45 = 118. The 30m book
        # (weather_maker_v1) would have cancelled; the probe's 60m fills.
        rec = _wrec(city="NYC", t0=t0, n=140,
                    prints_b895={118: (0.30, 2.0)})
        out = paper.mark_book("t", now_ts=t0 + 140 * 60,
                              live_rec=lambda ev, **kw: rec,
                              log=lambda *a: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "fill")
        self.assertEqual(out[0]["fill_price"], rest)
        self.assertAlmostEqual(out[0]["fill_lat_min"], 45.0)
        # Terminal: a second pass advances nothing.
        self.assertEqual(paper.mark_book("t", now_ts=t0 + 141 * 60,
                                         live_rec=lambda ev, **kw: rec,
                                         log=lambda *a: None), [])

    def test_print_past_the_horizon_cancels(self) -> None:
        rested, t0 = self._rest(city="NYC", hour=10)
        self.assertEqual(len(rested), 1)
        rec = _wrec(city="NYC", t0=t0, n=160,
                    prints_b895={138: (0.30, 2.0)})   # entry + 65 min
        out = paper.mark_book("t", now_ts=t0 + 160 * 60,
                              live_rec=lambda ev, **kw: rec,
                              log=lambda *a: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "cancel")
        self.assertEqual(out[0]["reason"], "horizon_expired_unfilled")


class TestRegistryAndSeam(unittest.TestCase):
    def test_live_family_hooks(self) -> None:
        self.assertEqual(set(paper.LIVE_FAMILIES), {"weather", "crypto"})
        w = paper._family_hooks("weather")
        self.assertIs(w["events"], paper._live_weather_events)
        self.assertIs(w["rec"], paper._live_weather_rec)
        self.assertIs(w["panel"], paper._build_live_panel)
        self.assertIs(w["maker_book"], paper._weather_maker_book)
        c = paper._family_hooks("crypto")
        self.assertIs(c["events"], paper._live_crypto_events)
        self.assertIs(c["rec"], paper._live_crypto_rec)
        self.assertIs(c["panel"], paper._build_live_crypto_panel)
        self.assertIsNone(c["maker_book"])
        with self.assertRaises(ValueError):
            paper._family_hooks("nba")

    def test_btc_pilot_registry_is_frozen_and_measure_only(self) -> None:
        spec = paper.STRATEGIES["btc_9599_pilot"]
        self.assertEqual(spec["family"], "crypto")
        self.assertEqual(spec["market"], "coin_price")
        self.assertEqual(spec["execution"], "taker_scan")
        self.assertEqual(spec["book"], "crypto_btc9599_v1")
        self.assertEqual(spec["declared_K"], 386)       # AGENDA hard rule
        self.assertTrue(callable(spec["event_gate"]))
        doc = paper._btc_9599_pilot.__doc__
        self.assertIn("MEASURE-ONLY", doc)
        self.assertIn("BTC-ONLY-ARTIFACT", doc)         # ETH refuted (xasset)
        self.assertIn("ISOLATED-SPIKE-NOISE", doc)      # mechanism-less hour
        self.assertIn("Declared K = 386", doc)
        self.assertIn("PROMOTION IS FORBIDDEN", doc)
        self.assertEqual(paper._BTC9599["hour_et"], 20)
        self.assertEqual(paper._BTC9599["spread_c"], 1)
        self.assertEqual((paper._BTC9599["mts_lo"], paper._BTC9599["mts_hi"]),
                         (31.0, 60.0))
        self.assertEqual((paper._BTC9599["band_lo_c"],
                          paper._BTC9599["band_hi_c"]), (95, 99))
        self.assertEqual(paper._BTC9599["min_volume"], 100.0)

    def test_fillprobe_registry(self) -> None:
        spec = paper.STRATEGIES["wx_fillprobe"]
        self.assertEqual(spec["execution"], "maker")
        self.assertEqual(spec["book"], "weather_fillprobe_v2")
        self.assertEqual(spec["horizon_min"], 60.0)     # pre-registered arm
        self.assertEqual(spec["maker_fee_c"], 0.0)
        self.assertEqual(spec["hyp_id"], "b8b071fa26ea4933")
        self.assertEqual(spec["parent_hyp_id"], "3cb384c482d1c2f7")
        self.assertIs(spec["entry_gate"], paper._wx_fillprobe_gate)
        doc = paper._wx_fillprobe.__doc__
        self.assertIn("NOT to make money", doc)
        self.assertIn("EX-POST", doc)                   # 66-cell list unused
        self.assertIn("PROMOTION IS FORBIDDEN", doc)
        # The probe's signal is the SAME frozen strategy as the v1 book.
        strat = spec["factory"]()
        self.assertEqual(strat.name, "weather.forecast_gap_toward")
        self.assertEqual(strat.entry_latency_min, 1.0)
        self.assertEqual(strat.max_stale_min, 2.0)

    def test_weather_maker_v1_tenant_untouched(self) -> None:
        spec = paper.STRATEGIES["forecast_gap_maker"]
        self.assertEqual(spec["book"], "weather_maker_v1")
        self.assertEqual(spec["horizon_min"], 30.0)
        self.assertNotIn("entry_gate", spec)            # no new gating on v1


if __name__ == "__main__":
    unittest.main()
