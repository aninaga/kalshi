"""research.lab.paper — forward paper-trading harness (the bridge to live).

The backtest gate judges the PAST; a fund needs forward evidence. This module
runs registered strategies against LIVE Kalshi quotes, records the paper fills
it would have taken (real bid/ask, real fee schedule, one contract each), and
settles them against realized outcomes — placing NO orders, ever. It is the
last honest step before capital: if a strategy's forward paper record diverges
from its backtest, the backtest's assumptions (not reality) are wrong.

Design
------
* **Append-only book.** ``market_data/paper/<book>.jsonl`` — one ``open`` row
  per position, one ``settle`` row when its event resolves; state is replayed
  last-write-wins per position id (the registry/ledger pattern). The book is
  TRACKED in git: the forward record is evidence and must survive containers.
* **Signal must be CURRENT.** A position opens only when the strategy's entry
  fires within the trailing ``recent_min`` minutes of live data. Without this
  gate a panel-so-far replay would "enter" on stale signals from hours ago at
  today's price — look-ahead's forward-mode twin.
* **Real quotes, honest costs.** Fills go through ``lab.execution.FillModel``
  (venue fees + half-spread) against the live panel's REAL ladder quotes,
  with the same strike pinning available to backtests.
* **Frozen tenants.** Strategies enroll in :data:`STRATEGIES` with their
  pre-registered parameters; the harness never tunes anything. The first
  tenant is a deliberate CONTROL — the (DEAD) drift-fade with expectation
  ~0c gross minus ~3c costs — so forward reality can validate the cost model
  itself before any live candidate exists.
* **Maker tenants.** A tenant whose spec carries ``execution="maker"`` does
  not fill immediately. ``--open`` RESTS a passive order (kind ``rest``,
  status ``resting``) at the prevailing best bid for the signal side at the
  i+1 bar — the exact construction of the maker study
  ``research/scripts/weather_maker_study.py`` (hyp ``b8b071fa26ea4933``):
  boundary OVER bid = sum of contributing legs' ``yes_bid``; UNDER bid =
  1 − boundary ask. A later ``--mark`` pass refetches live candles and either
  FILLS the order (a minute within the horizon printed volume>0 on a
  contributing leg AND the boundary traded-low reached the rest price —
  joined-queue conservatism, fill price = rest, maker fee 0 for KXHIGH) or
  CANCELS it once the horizon expires unfilled (EV 0; the row is KEPT —
  unfilled signals are the denominator of per-signal EV). Settlement of
  filled rows mirrors the taker path.
* **Per-family live seam.** All live I/O goes through :data:`LIVE_FAMILIES`
  hooks (``events`` / ``rec`` / ``panel`` / ``maker_book``), resolved from the
  tenant spec's ``family`` on open passes and from each position row's
  recorded ``family`` on mark/settle passes — one book can therefore hold any
  family, and weather and crypto tenants share one lifecycle. Network happens
  only inside the hook implementations (CLI paths); tests inject fakes.
* **Scanner tenants (measure-only).** ``execution="taker_scan"`` tenants do
  not run a lab ``Strategy`` over the panel mid: their factory returns a
  REC-level scanner (the per-threshold top-of-book is the signal surface,
  which the Panel idiom intentionally collapses). The scanner returns
  would-be TAKER entries at the displayed ask; the harness applies the same
  current-signal gate, idempotency (one position per event-SIDE), and
  settlement as any taker tenant. Nothing rests, nothing is ever ordered.

CLI (operator/cron; network for --open/--mark/--settle)::

    python -m research.lab.paper --open   --book weather_v1 --strategy drift_fade_control
    python -m research.lab.paper --open   --book weather_maker_v1 --strategy forecast_gap_maker
    python -m research.lab.paper --open   --book weather_fillprobe_v2 --strategy wx_fillprobe
    python -m research.lab.paper --open   --book crypto_btc9599_v1 --strategy btc_9599_pilot
    python -m research.lab.paper --mark   --book weather_maker_v1
    python -m research.lab.paper --settle --book weather_maker_v1
    python -m research.lab.paper --status --book weather_maker_v1
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from research.lab.types import OVER_SIDES, Panel, is_over_side

_ROOT = Path(__file__).resolve().parents[2]
PAPER_DIR = _ROOT / "market_data" / "paper"

# Sides that pay 1.0 when the outcome ends ABOVE the strike. De-duplicated
# 2026-06-12: ALIAS of the single source of truth ``research.lab.types``;
# this independent copy previously carried the SAME ``long_away`` gap as
# execution/strategy (audit defect C3). Settlement goes through
# ``is_over_side`` so an unknown side raises rather than silently settling as
# the away/below bet.
_OVER_SIDES = OVER_SIDES


# --------------------------------------------------------------------------- #
# the frozen strategy registry (pre-registered params only; never tuned here)
# --------------------------------------------------------------------------- #
def _drift_fade_control():
    """COST-MODEL CONTROL: fade the 5-bar implied-temp drift (both directions
    of this mechanism are DEAD in backtest: fade ≈ +3.0c gross − 3.24c costs).
    Forward expectation ≈ −0.3c/trade net. If the forward record differs
    materially, the COST MODEL (spread/fee assumptions) is miscalibrated —
    that is exactly what a control tenant is for. Params frozen from the
    pre-registered fade runs (`af866e8a`/`ac6c9c02`)."""
    from research.lab.strategy import Strategy

    K, TAU = 5, 0.75

    def _drift(p):
        mid = np.asarray(p.mid, float)
        out = np.full_like(mid, np.nan)
        out[K:] = mid[K:] - mid[:-K]
        return out

    def entry(p):
        d = _drift(p)
        return np.isfinite(d) & (np.abs(d) >= TAU)

    def side(p, i):
        return "under" if _drift(p)[i] > 0 else "over"   # fade the move

    return Strategy(name="paper.drift_fade_control", entry=entry, side=side,
                    exit="settlement", min_elapsed=3600.0, max_elapsed=1e9,
                    max_stale_min=2.0)


def _forecast_gap_maker():
    """MAKER candidate: the FROZEN forecast-gap signal under maker execution.

    Signal — EXACT parent hypothesis ``3cb384c482d1c2f7``
    (``research/scripts/weather_forecast_gap_e2e.py``, params frozen
    2026-06-09T22:54:16Z): per-city TRAIN debias constants
    {AUS:-2.69, CHI:-1.71, DEN:-0.90, MIA:-2.08, NYC:-2.00}, |debiased
    forecast − implied mid| >= 2.0F, elapsed_frac in [0.05, 0.30],
    staleness <= 2 min, i+1 entry latency, one trade per event, buy TOWARD
    the forecast at the ATM-snapped strike. Nothing retuned here.

    Execution — maker study hypothesis ``b8b071fa26ea4933``
    (``research/scripts/weather_maker_study.py``): rest at the prevailing
    best bid for the signal side at i+1 (join the queue, never improve),
    horizon 30 min (the study's pre-registered headline H), maker fee 0c
    (KXHIGH is a standard general series), fill only on a REAL print at or
    through the rest price, unfilled = EV 0 and kept in the denominator.

    PRE-REGISTERED PROMOTION CRITERION (frozen before the first live signal):
    after 100+ live signals, (a) the live fill rate is within +/-8 points of
    the study's backtest fill rate of 26%, and (b) per-signal EV
    (sum of settled pnl / number of decided signals) is positive. Divergence
    on (a) means the candle-derived fill model is wrong; failure of (b)
    means adverse selection in the wild exceeds the study's estimate —
    either one blocks promotion to capital.
    """
    from research.scripts.weather_forecast_gap_e2e import build_strategy
    return build_strategy()


# --------------------------------------------------------------------------- #
# wx_fillprobe — measure-only maker FILL PROBE (book weather_fillprobe_v2)
# --------------------------------------------------------------------------- #
_FILLPROBE_TZ = {"NYC": "America/New_York", "CHI": "America/Chicago"}
_FILLPROBE_HOURS = (6, 15)        # local clock hour band, inclusive (see gate)


def _wx_fillprobe_gate(ev: dict, signal_ts: float) -> bool:
    """Fill-probe entry levers (cities + local clock hours; see the tenant
    docstring for the derivation): NYC/CHI only, and the SIGNAL's local clock
    hour must fall in 06..15 (06:00–15:59 local, any calendar day — the frozen
    signal mostly fires on the day BEFORE the event date, and the prior-day
    morning/afternoon cells were real fill generators in the schedule study)."""
    tz = _FILLPROBE_TZ.get((ev.get("city") or "").upper())
    if tz is None:
        return False
    from zoneinfo import ZoneInfo

    hour = datetime.fromtimestamp(float(signal_ts), tz=ZoneInfo(tz)).hour
    return _FILLPROBE_HOURS[0] <= hour <= _FILLPROBE_HOURS[1]


def _wx_fillprobe():
    """MEASURE-ONLY maker FILL PROBE — exists to GENERATE FILLS, explicitly
    NOT to make money. Its product is fill-rate and adverse-selection
    measurements (fill latency, filled-vs-cancelled mix, settled pnl OF
    fills), which the AGENDA's no-dead-microstructure STOP rule says are the
    ONLY remaining way to resolve maker viability: every top-of-book maker
    backtest on this venue died on unfilled-quote calibration, and the live
    ``weather_maker_v1`` book produced 0 fills in its first week.

    Signal — IDENTICAL frozen forecast-gap signal as ``forecast_gap_maker``
    (parent hyp ``3cb384c482d1c2f7``; per-city TRAIN debias {AUS:-2.69,
    CHI:-1.71, DEN:-0.90, MIA:-2.08, NYC:-2.00}, |debiased forecast − implied
    mid| >= 2.0F, elapsed_frac in [0.05, 0.30], staleness <= 2 min, i+1
    entry, one order per event). Nothing retuned.

    Execution — the maker-study construction (hyp ``b8b071fa26ea4933``) with
    rest horizon **60 minutes**: one of the study's PRE-REGISTERED horizons
    (HORIZONS_MIN = 10/30/60), chosen because the fill-geography memo
    measured ~40% lower-bound 60m fill vs ~27% at 30m — more fills per
    signal is the whole point of a probe.

    Entry restriction — the wx_maker_schedule memo's 66-cell
    (city, hour, offset) list is EX-POST (holdout-selected; the
    wx_sched_walkforward lane confirmed it does NOT generalize: negative
    REPORT-window EV/signal, Bonferroni CI crosses 0), so this tenant does
    NOT use the cell list. It restricts ONLY on the memo's city/hour/horizon
    LEVERS, which are fill-MECHANICS facts rather than alpha selections
    (wx_fill_geography memo): cities NYC+CHI (highest lower-bound 60m touch
    rates, ~77%; DEN worst), local clock hours 06:00–15:59 (the strongest
    touch-liquidity hours cluster at local 06–14, "morning to mid-afternoon"),
    60-minute rests. See :func:`_wx_fillprobe_gate`.

    PROMOTION IS FORBIDDEN from this book alone: it is a measurement
    instrument. Any alpha claim built on its record must independently
    satisfy the AGENDA hard rules — a holdout firing on >= 12 DISTINCT
    settlement events AND a declared selection count K with the bootstrap CI
    cleared at alpha/K. This docstring is that contract.
    """
    from research.scripts.weather_forecast_gap_e2e import build_strategy
    return build_strategy()


# --------------------------------------------------------------------------- #
# btc_9599_pilot — measure-only taker scanner (book crypto_btc9599_v1)
# --------------------------------------------------------------------------- #
_BTC9599 = {
    "asset": "BTC",            # BTC ONLY — see BTC-ONLY-ARTIFACT caveat below
    "hour_et": 20,             # settlement hour, America/New_York (ET)
    "spread_c": 1,             # displayed spread, EXACTLY 1 cent
    "mts_lo": 31.0,            # minutes_to_settle window, inclusive
    "mts_hi": 60.0,
    "band_lo_c": 95,           # favorite-ask band, cents, inclusive
    "band_hi_c": 99,
    "min_volume": 100.0,       # cumulative contracts at the entry minute
    "declared_K": 386,         # selection grid size (btc_9599_selection memo)
}


def _et_settle_ts(event_ticker: str) -> Optional[float]:
    """Settlement epoch from the hourly ticker's (date, hour) in ET — the
    xasset memo's exact minutes_to_settle convention (Kalshi hourly coin
    tickers encode the ET settlement hour)."""
    from zoneinfo import ZoneInfo

    from research.lab.providers.crypto import event_date_hour

    dh = event_date_hour(event_ticker or "")
    if dh is None:
        return None
    y, m, d = (int(x) for x in dh[0].split("-"))
    return datetime(y, m, d, dh[1],
                    tzinfo=ZoneInfo("America/New_York")).timestamp()


def _btc9599_event_gate(ev: dict, now_ts: float) -> bool:
    """Cheap PRE-FETCH gate on the event dict alone (rate-limit hygiene; the
    scanner re-applies the full frozen predicate on real candles): a BTC
    hourly event settling at 20:00 ET whose [31,60]-minutes-to-settle entry
    band is near NOW (now within 75 min of settlement, recent_min slack
    included)."""
    from research.lab.providers.crypto import _asset_of, event_date_hour

    tick = ev.get("event_ticker", "")
    if (ev.get("asset") or _asset_of(tick)) != _BTC9599["asset"]:
        return False
    dh = event_date_hour(tick)
    if dh is None or dh[1] != _BTC9599["hour_et"]:
        return False
    settle = _et_settle_ts(tick)
    return settle is not None and (settle - 75 * 60.0) <= now_ts <= settle


def _btc_9599_pilot():
    """MEASURE-ONLY pilot of the BTC 95–99c hour-20 favorite sleeve — records
    signals and the WOULD-BE taker fill at the displayed ask. NO maker
    resting, NO orders, ever. The factory returns a REC-level scanner
    (``scan(rec, now_ts=...) -> [candidate]``); the harness books at most one
    position per event-SIDE and settles against the realized index price.

    FROZEN SUB-SPEC (verbatim from the btc_9599_subspec/xasset memos and
    AGENDA NOW#1; nothing tuned here):
      * asset == BTC; event settlement hour == 20 ET (from the ticker);
      * displayed spread EXACTLY 1c (``yes_ask − yes_bid == 0.01``);
      * minutes_to_settle in [31, 60] at the entry minute (entry-minute ts to
        the ET settlement hour — the memos' bucket convention);
      * favorite sleeve: ``yes_ask in [0.95, 0.99]`` (buy YES at the ask) OR
        ``1 − yes_bid in [0.95, 0.99]`` (buy NO at the NO ask = 1 − yes_bid);
      * cumulative volume >= 100 contracts at the entry minute (AGENDA's
        "vol>=100 preferred": the capacity memo shows the cell positive in
        the 100-999 and 1000+ buckets, so the pilot measures where size is);
      * max 1 entry per event-side (normalized over/under; among simultaneous
        qualifiers on a side: highest entry-minute volume, then cheaper ask,
        then lower strike — deterministic);
      * cost line: ``all_in < payoff − fee + 1c`` with payoff = 1.0 and the
        REAL Kalshi taker fee (ceil(7·p·(1−p)) cents) billed in ``all_in``
        — i.e. ``ask + 2·fee < 1.01``. With the 1c-ceil fee this rejects
        exactly the 99c ask (profit-if-win 0 after fee), the AGENDA's
        "holds only under ~fee+1c adverse cost; fee+2c turns it negative"
        line. Entry at the ask IS the memos' cross-through case (touch+1c),
        which still cleared the holdout at +1.15..+1.38c.

    CAVEATS (why this is a pilot and not a position):
      * **BTC-ONLY-ARTIFACT** — the btc_9599_xasset memo applied the frozen
        sleeve verbatim to ETH (the only other asset) and it FAILED out of
        sample: −3.49c/contract, event-clustered 95% CI [−19.28, +2.06].
        Generalization is refuted; nothing here may be extended to ETH.
      * **Mechanism-less single-hour flag** — btc_session_regime graded
        hour 20 as ISOLATED-SPIKE-NOISE: neighbors hr19 −1.87c / hr21 −0.66c,
        lag-1 autocorr −0.24, and NO a-priori ET session block survives
        Bonferroni. A single positive hour with no mechanism. Do NOT widen
        to neighbor hours; expect this to be able to die at any time.
      * **Declared K = 386** (AGENDA hard rule) — the sleeve emerged from the
        9599 conditioning grid; btc_9599_selection declared K=386 and the
        hour-20 cell cleared alpha/K on the locked split (+1.63c/contract,
        CI [+0.56, +1.97]) and survived all 3 boundary shifts. That declared
        K travels with this tenant.
      * **Pilot-tiny rationale** — one hour-20 event per day × ≤2 sides means
        a handful of rows/week; the original thin cell's median displayed
        size was 24–26 contracts. The binding uncertainty is FILLS, not
        backtest EV (paper-maker fills run 0 vs ~26–42% expected), so the
        pilot exists to measure realized entry quality at 1-contract scale,
        not to harvest the ~$683/day full-size estimate.

    PROMOTION IS FORBIDDEN from this book alone: measure-only means the book
    records and settles, and any promotion claim must independently satisfy
    the AGENDA hard rules — >= 12 DISTINCT holdout settlement events AND the
    declared-K (alpha/K) correction. This docstring is that contract.
    """
    from research.lab.execution import FillModel
    from research.lab.providers.crypto import _boundary, event_date_hour

    P = _BTC9599
    fee_of = FillModel(venue="kalshi").fee

    def scan(rec: dict, *, now_ts: float) -> list:
        if (rec.get("asset") or "") != P["asset"]:
            return []
        dh = event_date_hour(rec.get("event_ticker", ""))
        if dh is None or dh[1] != P["hour_et"]:
            return []
        settle_ts = _et_settle_ts(rec["event_ticker"])
        if settle_ts is None:
            return []
        best: dict = {}                     # normalized side -> best candidate
        for m in rec.get("markets", []):
            b = _boundary(m)
            if b is None:
                continue
            boundary, orient = b
            candles = m.get("candles") or []
            row = None                      # entry minute = last two-sided bar
            for c in reversed(candles):
                if (c.get("ts") is not None and c.get("yes_bid") is not None
                        and c.get("yes_ask") is not None):
                    row = c
                    break
            if row is None:
                continue
            ts = float(int(row["ts"]) // 60 * 60)
            bid, ask = float(row["yes_bid"]), float(row["yes_ask"])
            bid_c, ask_c = int(round(bid * 100)), int(round(ask * 100))
            if ask_c - bid_c != P["spread_c"]:
                continue
            mts = (settle_ts - ts) / 60.0
            if not (P["mts_lo"] - 1e-9 <= mts <= P["mts_hi"] + 1e-9):
                continue
            # Cumulative volume AT the entry minute: the market's cumulative
            # volume as fetched, minus per-minute prints AFTER that minute.
            vol = float(m.get("volume") or 0.0) - sum(
                float(c.get("volume") or 0.0) for c in candles
                if c.get("ts") is not None and int(c["ts"]) // 60 * 60 > ts)
            if vol < P["min_volume"]:
                continue
            legs = []
            if P["band_lo_c"] <= ask_c <= P["band_hi_c"]:
                legs.append(("yes", ask))               # buy YES at the ask
            if P["band_lo_c"] <= 100 - bid_c <= P["band_hi_c"]:
                legs.append(("no", round(1.0 - bid, 10)))   # buy NO at its ask
            for leg, price in legs:
                fee = fee_of(price)
                all_in = price + fee
                if not (all_in < 1.0 - fee + 0.01 - 1e-12):   # frozen cost line
                    continue
                pays_above = (leg == "yes") == (orient > 0)
                cand = {
                    "side": "over" if pays_above else "under", "leg": leg,
                    "ticker": m.get("ticker"), "strike": float(boundary),
                    "orient": int(orient), "signal_ts": ts,
                    "yes_bid": bid, "yes_ask": ask,
                    "fill_price": price, "fee": fee, "all_in": all_in,
                    "fill_mid": round((bid + ask) / 2.0, 10),
                    "half_spread": round(price - (bid + ask) / 2.0, 10),
                    "volume_at_entry": vol, "minutes_to_settle": mts,
                }
                cur = best.get(cand["side"])
                key = (cand["volume_at_entry"], -cand["fill_price"],
                       -cand["strike"])
                if cur is None or key > (cur["volume_at_entry"],
                                         -cur["fill_price"], -cur["strike"]):
                    best[cand["side"]] = cand
        return [best[k] for k in sorted(best)]

    return scan


STRATEGIES = {
    "drift_fade_control": {
        "factory": _drift_fade_control,
        "family": "weather", "market": "temp",
        "expectation": "control: ~-0.3c/trade net (validates the cost model)",
    },
    "forecast_gap_maker": {
        "factory": _forecast_gap_maker,
        "family": "weather", "market": "temp",
        "execution": "maker",            # rest/mark lifecycle, not immediate fill
        "book": "weather_maker_v1",
        "horizon_min": 30.0,             # study's pre-registered headline H
        "maker_fee_c": 0.0,              # KXHIGH: standard series, 0c maker fee
        "hyp_id": "b8b071fa26ea4933",
        "parent_hyp_id": "3cb384c482d1c2f7",
        "expectation": ("candidate: fill_rate ~26%, positive per-signal EV; "
                        "promotion criterion in the factory docstring"),
    },
    "wx_fillprobe": {
        "factory": _wx_fillprobe,
        "family": "weather", "market": "temp",
        "execution": "maker",            # rest/mark lifecycle, not immediate fill
        "book": "weather_fillprobe_v2",
        "horizon_min": 60.0,             # pre-registered horizon (study sweep)
        "maker_fee_c": 0.0,              # KXHIGH: standard series, 0c maker fee
        "hyp_id": "b8b071fa26ea4933",    # same maker construction, H=60 arm
        "parent_hyp_id": "3cb384c482d1c2f7",
        "entry_gate": _wx_fillprobe_gate,   # NYC+CHI, local 06:00-15:59
        "expectation": ("FILL PROBE, measure-only: ~40% lower-bound 60m fill; "
                        "product is fill mechanics, NOT alpha (see docstring)"),
    },
    "btc_9599_pilot": {
        "factory": _btc_9599_pilot,
        "family": "crypto", "market": "coin_price",
        "execution": "taker_scan",       # measure-only rec scanner, no resting
        "book": "crypto_btc9599_v1",
        "event_gate": _btc9599_event_gate,
        "declared_K": _BTC9599["declared_K"],   # AGENDA hard rule: K=386
        "expectation": ("MEASURE-ONLY pilot: BTC-only hour-20 artifact, "
                        "~+1.2c/contract at the cross if backtests hold; "
                        "promotion forbidden per the factory docstring"),
    },
}


# --------------------------------------------------------------------------- #
# book persistence (append-only JSONL, replayed)
# --------------------------------------------------------------------------- #
def _book_path(book: str) -> Path:
    return PAPER_DIR / f"{book}.jsonl"


def _append(book: str, row: dict) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    with open(_book_path(book), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def positions(book: str) -> dict:
    """Replay the book: position id -> merged row (open fields + settlement)."""
    path = _book_path(book)
    state: dict = {}
    if not path.exists():
        return state
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = row.get("id")
            if not pid:
                continue
            state.setdefault(pid, {}).update(row)
    return state


def _pid(book: str, strategy: str, event_id: str) -> str:
    return f"{strategy}|{event_id}"   # one position per strategy per event


# --------------------------------------------------------------------------- #
# live data glue (per-family thin seams, patched in tests)
# --------------------------------------------------------------------------- #
def _live_weather_events() -> list:
    """Open (unsettled) daily-high events across the configured cities."""
    from research.lab.providers import _kalshi_fetch as K
    from research.lab.providers.weather import CITY_SERIES

    out = []
    for city, series in CITY_SERIES.items():
        d = K._get("/events", {"series_ticker": series, "status": "open",
                               "limit": 50})
        for e in (d or {}).get("events", []):
            tick = e.get("event_ticker")
            if tick:
                out.append({"event_ticker": tick, "city": city,
                            "series": series})
        time.sleep(0.1)
    return out


def _live_weather_rec(ev: dict, *, settled: bool = False) -> Optional[dict]:
    """A cache-shaped record for one live (or just-settled) event."""
    from research.lab.providers import _kalshi_fetch as K
    from research.lab.providers.weather import event_date

    tick = ev["event_ticker"]
    markets = K.event_markets(tick)
    if not markets:
        return None
    rows = []
    now = int(time.time())
    close_ts = None
    for m in markets:
        mt = m.get("ticker")
        if mt is None:
            continue
        try:
            from research.lab.providers.weather import _iso_ts
            start = _iso_ts(m.get("open_time")) or (now - 3 * 86400)
            mclose = _iso_ts(m.get("close_time") or m.get("expiration_time"))
            if mclose is not None:
                close_ts = mclose if close_ts is None else max(close_ts, mclose)
        except Exception:  # noqa: BLE001
            start = now - 3 * 86400
        candles = K.fetch_candles(ev.get("series") or tick.split("-")[0], mt,
                                  start, now)
        rows.append({"ticker": mt, "floor": m.get("floor_strike"),
                     "cap": m.get("cap_strike"), "result": m.get("result"),
                     "sub": m.get("yes_sub_title"), "candles": candles})
        time.sleep(0.1)
    return {"event_ticker": tick, "series": ev.get("series", ""),
            "city": ev.get("city", ""), "date": event_date(tick),
            "close_ts": close_ts, "markets": rows}


def _build_live_panel(rec: dict) -> Optional[Panel]:
    from research.lab.providers import feature_store
    from research.lab.providers.weather import build_panel

    panel = build_panel(rec, split_of=lambda gid: "live")
    if panel is None:
        return None
    # A LIVE panel is truncated at "now", so build_panel's candle-span duration
    # is NOT the quote window — elapsed_frac signals would think the window is
    # already over. Use the listed close time captured by _live_weather_rec
    # (settled-event replays in tests omit close_ts and keep the old behavior).
    close_ts = rec.get("close_ts")
    if close_ts:
        panel.duration_sec = float(close_ts) - float(panel.minute_ts[0])
    # Attach durable feature-store fields (e.g. forecast_high_f) exactly as the
    # backtest provider does — as-of known_from_ts, leakage-safe by construction.
    feature_store.join_panel(panel, "weather")
    return panel


def _weather_maker_book(rec: dict):
    """Boundary-cumulative maker inputs for one weather rec (study builder)."""
    from research.scripts import weather_maker_study as MK

    return MK.build_maker_book(rec)


def _live_crypto_events() -> list:
    """Open (unsettled) hourly coin-threshold events across configured assets."""
    from research.lab.providers import _kalshi_fetch as K
    from research.lab.providers.crypto import ASSET_SERIES

    out = []
    for asset, series in ASSET_SERIES.items():
        d = K._get("/events", {"series_ticker": series, "status": "open",
                               "limit": 50})
        for e in (d or {}).get("events", []):
            tick = e.get("event_ticker")
            if tick:
                out.append({"event_ticker": tick, "asset": asset,
                            "series": series})
        time.sleep(0.1)
    return out


def _live_crypto_rec(ev: dict, *, settled: bool = False) -> Optional[dict]:
    """A cache-shaped record for one live (or just-settled) hourly coin event.

    Markets: the top 24 by cumulative volume among volume>0 thresholds (the
    cache build's activity filter; the 95-99c sleeve requires volume>=100, so
    qualifying thresholds rank high — and rate-limit hygiene matters at a
    15-min cadence). Candle span: trailing 90 min on open passes (entries
    must be CURRENT anyway; cumulative volume rides on the market dict), the
    full quote window when ``settled`` (settlement rebuilds the panel).
    """
    from research.lab.providers import _kalshi_fetch as K
    from research.lab.providers.crypto import (ASSET_SERIES, _asset_of,
                                               _iso_ts, event_date_hour)

    tick = ev["event_ticker"]
    asset = ev.get("asset") or _asset_of(tick) or ""
    series = ev.get("series") or ASSET_SERIES.get(asset) or tick.split("-")[0]
    markets = K.event_markets(tick)
    if not markets:
        return None
    exp_val = None
    for m in markets:
        v = m.get("expiration_value")
        if v not in (None, ""):
            exp_val = v
            break
    active = [m for m in markets
              if float(m.get("volume_fp") or m.get("volume") or 0) > 0]
    active.sort(key=lambda m: -float(m.get("volume_fp") or m.get("volume") or 0))
    active = active[:24]
    now = int(time.time())
    close_ts = None
    rows = []
    for m in active:
        mt = m.get("ticker")
        if mt is None:
            continue
        start = _iso_ts(m.get("open_time")) or (now - 2 * 3600)
        mclose = _iso_ts(m.get("close_time") or m.get("expiration_time"))
        if mclose is not None:
            close_ts = mclose if close_ts is None else max(close_ts, mclose)
        if not settled:
            start = max(start, now - 90 * 60)
        candles = K.fetch_candles(series, mt, start, now)
        rows.append({"ticker": mt, "floor": m.get("floor_strike"),
                     "cap": m.get("cap_strike"), "result": m.get("result"),
                     "volume": float(m.get("volume_fp") or m.get("volume")
                                     or 0.0),
                     "sub": m.get("yes_sub_title"), "candles": candles})
        time.sleep(0.1)
    dh = event_date_hour(tick)
    return {"event_ticker": tick, "series": series, "asset": asset,
            "date": dh[0] if dh else None, "hour": dh[1] if dh else None,
            "expiration_value": exp_val, "close_ts": close_ts,
            "markets": rows}


def _build_live_crypto_panel(rec: dict) -> Optional[Panel]:
    from research.lab.providers import feature_store
    from research.lab.providers.crypto import build_panel

    panel = build_panel(rec, split_of=lambda gid: "live")
    if panel is None:
        return None
    # Same live-truncation fix as weather: a live panel ends at "now", so the
    # candle-span duration is not the quote window — use the listed close.
    close_ts = rec.get("close_ts")
    if close_ts:
        panel.duration_sec = float(close_ts) - float(panel.minute_ts[0])
    feature_store.join_panel(panel, "crypto")
    return panel


# Per-family live hooks — THE seam between the lifecycle and the venues.
# ``events``: enumerate open events; ``rec``: one cache-shaped event record
# (kw ``settled`` for resolution passes); ``panel``: rec -> Panel (pure);
# ``maker_book``: rec -> maker-fill inputs, or None for families with no
# maker tenants. Open passes resolve via the tenant spec's family; mark and
# settle passes resolve via each position row's recorded ``family``.
LIVE_FAMILIES = {
    "weather": {"events": _live_weather_events, "rec": _live_weather_rec,
                "panel": _build_live_panel, "maker_book": _weather_maker_book},
    "crypto": {"events": _live_crypto_events, "rec": _live_crypto_rec,
               "panel": _build_live_crypto_panel, "maker_book": None},
}


def _family_hooks(family: str) -> dict:
    try:
        return LIVE_FAMILIES[family]
    except KeyError:
        raise ValueError(f"no live hooks for family {family!r}; "
                         f"known: {sorted(LIVE_FAMILIES)}") from None


# --------------------------------------------------------------------------- #
# open / settle / status
# --------------------------------------------------------------------------- #
def scan_and_open(strategy_name: str, *, book: str, recent_min: float = 30.0,
                  fill_model=None, now_ts: Optional[float] = None,
                  live_events=None, live_rec=None, live_panel=None,
                  log=print) -> list:
    """One forward pass: open a paper position per live event whose signal
    fires NOW (within the trailing ``recent_min`` of data). Idempotent: an
    event already holding a position for this strategy is skipped. Live seams
    default to the tenant family's :data:`LIVE_FAMILIES` hooks.

    ``execution="taker_scan"`` tenants route to :func:`_scan_and_take`: their
    factory yields a rec-level scanner (per-threshold top-of-book predicate)
    instead of a panel ``Strategy``."""
    spec = STRATEGIES[strategy_name]
    hooks = _family_hooks(spec["family"])
    live_events = live_events or hooks["events"]
    live_rec = live_rec or hooks["rec"]
    live_panel = live_panel or hooks["panel"]
    if spec.get("execution") == "taker_scan":
        return _scan_and_take(strategy_name, spec, book=book,
                              recent_min=recent_min, now_ts=now_ts,
                              live_events=live_events, live_rec=live_rec,
                              log=log)
    strat = spec["factory"]()
    if fill_model is None:
        from research.lab.execution import FillModel
        fill_model = FillModel(venue="kalshi", half_spread=0.015)

    held = positions(book)
    opened = []
    for ev in live_events():
        pid = _pid(book, strategy_name, ev["event_ticker"])
        if pid in held:
            continue
        rec = live_rec(ev)
        panel = live_panel(rec) if rec else None
        if panel is None or panel.n < 10:
            continue
        trade = strat._run_one(panel, fill_model)
        if trade is None:
            continue
        now = float(now_ts if now_ts is not None else time.time())
        # The signal must be CURRENT: its bar inside the trailing window.
        signal_ts = float(panel.minute_ts[int(trade.meta["signal_bar"])])
        if now - signal_ts > recent_min * 60.0:
            continue
        row = {
            "kind": "open", "id": pid, "book": book,
            "strategy": strategy_name, "family": spec["family"],
            "market": spec["market"], "event_id": ev["event_ticker"],
            "city": ev.get("city", ""),
            "opened_ts": now, "signal_ts": signal_ts,
            "side": trade.side, "strike": trade.entry_strike,
            "fill_mid": trade.entry_price,
            "half_spread": trade.meta["half_spread"],
            "fee": trade.meta["fee"], "all_in": trade.meta["all_in_price"],
            "status": "open",
        }
        _append(book, row)
        opened.append(row)
        log(f"[paper:{book}] OPEN {pid} {row['side']} strike={row['strike']} "
            f"all_in={row['all_in']:.3f}")
    return opened


def _scan_and_take(strategy_name: str, spec: dict, *, book: str,
                   recent_min: float, now_ts: Optional[float],
                   live_events, live_rec, log) -> list:
    """Measure-only taker pass for ``execution="taker_scan"`` tenants.

    The scanner consumes the RAW event rec (per-threshold quotes — the signal
    surface the Panel idiom collapses) and emits would-be taker entries at the
    displayed ask. Discipline mirrors the Strategy path: a spec-level
    ``event_gate`` may veto the rec FETCH on the event dict alone (rate-limit
    hygiene), the current-signal gate rejects stale entry minutes, and the
    position id is ``strategy|event|side`` — at most ONE entry per event-SIDE,
    forever (the frozen sub-spec's cap)."""
    scanner = spec["factory"]()
    event_gate = spec.get("event_gate")
    held = positions(book)
    opened = []
    for ev in live_events():
        now = float(now_ts if now_ts is not None else time.time())
        tick = ev["event_ticker"]
        if event_gate is not None and not event_gate(ev, now):
            continue
        if all(f"{strategy_name}|{tick}|{s}" in held
               for s in ("over", "under")):
            continue                      # both sides held: skip the fetch
        rec = live_rec(ev)
        if rec is None:
            continue
        for cand in scanner(rec, now_ts=now):
            pid = f"{strategy_name}|{tick}|{cand['side']}"
            if pid in held:
                continue
            # The signal must be CURRENT (same gate as every other path).
            if now - float(cand["signal_ts"]) > recent_min * 60.0:
                continue
            row = {
                "kind": "open", "id": pid, "book": book,
                "strategy": strategy_name, "family": spec["family"],
                "market": spec["market"], "event_id": tick,
                "asset": rec.get("asset", ""), "opened_ts": now,
                "status": "open", **cand,
            }
            _append(book, row)
            held[pid] = row
            opened.append(row)
            log(f"[paper:{book}] OPEN {pid} {row['leg']}@{row['fill_price']:.2f} "
                f"strike={row['strike']} all_in={row['all_in']:.3f} "
                f"mts={row['minutes_to_settle']:.0f}")
    return opened


# --------------------------------------------------------------------------- #
# maker lifecycle: rest -> (fill | cancel) -> settle
# --------------------------------------------------------------------------- #
def scan_and_rest(strategy_name: str, *, book: str, recent_min: float = 30.0,
                  now_ts: Optional[float] = None,
                  live_events=None, live_rec=None, live_panel=None,
                  log=print) -> list:
    """One maker forward pass: REST a passive order per live event whose frozen
    signal fires NOW. Mirrors ``weather_maker_study.run_arm``'s construction
    exactly: signal bar from the frozen entry/window/freshness, i+1 latency,
    ATM strike snapped on the boundary set, rest at the prevailing best bid for
    the signal side (joined queue, never improved). Idempotent per event.

    A spec-level ``entry_gate(ev, signal_ts)`` (e.g. the fill probe's
    city/local-hour levers) may veto the rest AFTER the frozen signal fires;
    tenants without one (``forecast_gap_maker``) are untouched."""
    from research.scripts import weather_maker_study as MK

    spec = STRATEGIES[strategy_name]
    if spec.get("execution") != "maker":
        raise ValueError(f"{strategy_name!r} is not a maker tenant")
    horizon_min = float(spec["horizon_min"])
    entry_gate = spec.get("entry_gate")
    hooks = _family_hooks(spec["family"])
    live_events = live_events or hooks["events"]
    live_rec = live_rec or hooks["rec"]
    build_panel_fn = live_panel or hooks["panel"]

    held = positions(book)
    rested = []
    for ev in live_events():
        pid = _pid(book, strategy_name, ev["event_ticker"])
        if pid in held:
            continue
        rec = live_rec(ev)
        panel = build_panel_fn(rec) if rec else None
        if panel is None or panel.n < 10:
            continue
        bar = MK._frozen_signal_bar(panel)
        if bar is None:
            continue
        now = float(now_ts if now_ts is not None else time.time())
        xp = np.asarray(panel.minute_ts, float)
        signal_ts = float(xp[bar])
        # The signal must be CURRENT (forward look-ahead's twin, same gate as
        # the taker path).
        if now - signal_ts > recent_min * 60.0:
            continue
        if entry_gate is not None and not entry_gate(ev, signal_ts):
            continue
        maker_book_fn = hooks.get("maker_book")
        if maker_book_fn is None:
            raise ValueError(f"family {spec['family']!r} has no maker-book "
                             "builder; maker tenants are weather-only")
        side = MK.FROZEN.side(panel, bar)
        mbook = maker_book_fn(rec)
        if mbook is None:
            log(f"[paper:{book}] SKIP {pid}: maker book unusable")
            continue
        entry_ts = signal_ts + MK.ENTRY_LATENCY_MIN * 60.0

        # ATM strike snapped on the LISTED boundary set (study construction).
        strikes = np.asarray(mbook.boundaries, float)
        implied = float(np.interp(entry_ts, xp, MK._ffill(panel.mid)))
        strike = float(strikes[int(np.argmin(np.abs(strikes - implied)))])

        j = int(np.searchsorted(mbook.minute_ts, entry_ts))
        if j >= len(mbook.minute_ts):
            j = len(mbook.minute_ts) - 1
        ob, oa = mbook.over_bid.get(strike), mbook.over_ask.get(strike)
        if ob is None or oa is None:
            log(f"[paper:{book}] SKIP {pid}: strike {strike} not in maker book")
            continue
        rest = ob[j] if side == "over" else 1.0 - oa[j]
        if not np.isfinite(rest):
            log(f"[paper:{book}] SKIP {pid}: no quote to join at strike {strike}")
            continue
        rest = float(min(max(rest, 0.0), 1.0))

        row = {
            "kind": "rest", "id": pid, "book": book,
            "strategy": strategy_name, "family": spec["family"],
            "market": spec["market"], "event_id": ev["event_ticker"],
            "city": ev.get("city", ""),
            "rested_ts": now, "signal_ts": signal_ts, "entry_ts": entry_ts,
            "side": side, "strike": strike, "rest_price": rest,
            "horizon_min": horizon_min,
            "horizon_end_ts": entry_ts + horizon_min * 60.0,
            "maker_fee_c": float(spec.get("maker_fee_c", 0.0)),
            "hyp_id": spec.get("hyp_id"),
            "parent_hyp_id": spec.get("parent_hyp_id"),
            "status": "resting",
        }
        _append(book, row)
        rested.append(row)
        log(f"[paper:{book}] REST {pid} {side} strike={strike} "
            f"rest={rest:.3f} horizon={horizon_min:.0f}m")
    return rested


def mark_book(book: str, *, now_ts: Optional[float] = None,
              live_rec=None, log=print) -> list:
    """Advance every RESTING order against fresh live candles.

    For each resting order: rebuild the maker book from a fresh fetch (its
    candles now extend past the rest time, so prints that occurred between
    passes are observed retroactively) and apply the study's fill detector —
    FILLED at the first in-horizon minute where a contributing leg printed
    volume>0 AND the boundary traded-low reached the rest price (fill price =
    rest, maker fee as registered); CANCELLED once ``now`` is past the horizon
    end with no such print (EV 0, row kept: unfilled signals are denominators).
    Idempotent: filled/cancelled orders are terminal for this pass. Hooks
    resolve per ROW family (an injected ``live_rec`` overrides them all)."""
    from research.scripts import weather_maker_study as MK

    advanced = []
    for pid, pos in positions(book).items():
        if pos.get("status") != "resting":
            continue
        hooks = _family_hooks(pos.get("family", "weather"))
        maker_book_fn = hooks.get("maker_book")
        if maker_book_fn is None:        # defensive: no such rows are written
            log(f"[paper:{book}] SKIP {pid}: family "
                f"{pos.get('family')!r} has no maker-book builder")
            continue
        rec_fn = live_rec or hooks["rec"]
        now = float(now_ts if now_ts is not None else time.time())
        horizon_end = float(pos["horizon_end_ts"])
        rec = rec_fn({"event_ticker": pos["event_id"],
                      "city": pos.get("city", ""),
                      "asset": pos.get("asset", "")})
        mbook = maker_book_fn(rec) if rec else None
        strike = float(pos["strike"])
        entry_ts = float(pos["entry_ts"])
        rest = float(pos["rest_price"])
        fill_bar = None
        if mbook is not None and strike in mbook.over_bid:
            j = int(np.searchsorted(mbook.minute_ts, entry_ts))
            if j >= len(mbook.minute_ts):
                j = len(mbook.minute_ts) - 1
            fill_bar = MK._detect_fill(
                mbook, strike, pos["side"], j, entry_ts,
                float(pos["horizon_min"]), rest, MK.QUEUE_THROUGH_C)
        if fill_bar is not None:
            fee = float(pos.get("maker_fee_c", 0.0)) / 100.0
            filled_ts = float(mbook.minute_ts[fill_bar])
            row = {"kind": "fill", "id": pid, "filled_ts": filled_ts,
                   "fill_price": rest, "fee": fee, "all_in": rest + fee,
                   "fill_lat_min": (filled_ts - entry_ts) / 60.0,
                   "status": "filled"}
            _append(book, row)
            advanced.append(row)
            log(f"[paper:{book}] FILL {pid} @ {rest:.3f} "
                f"(+{row['fill_lat_min']:.0f}m)")
        elif now > horizon_end:
            row = {"kind": "cancel", "id": pid, "cancelled_ts": now,
                   "reason": ("book_unavailable" if mbook is None
                              else "horizon_expired_unfilled"),
                   "status": "cancelled"}
            _append(book, row)
            advanced.append(row)
            log(f"[paper:{book}] CANCEL {pid} ({row['reason']})")
        # else: still inside the horizon with no print yet — keep resting.
    return advanced


def settle_book(book: str, *, live_rec=None, log=print) -> list:
    """Settle every open/filled position whose event has resolved. Hooks
    resolve per ROW family (an injected ``live_rec`` overrides them all;
    the panel builder always follows the row's family)."""
    settled = []
    for pid, pos in positions(book).items():
        # "open" = taker position; "filled" = maker order that got a real
        # print. RESTING orders never settle — they have no position.
        if pos.get("status") not in ("open", "filled"):
            continue
        hooks = _family_hooks(pos.get("family", "weather"))
        rec_fn = live_rec or hooks["rec"]
        rec = rec_fn({"event_ticker": pos["event_id"],
                      "city": pos.get("city", ""),
                      "asset": pos.get("asset", "")}, settled=True)
        panel = hooks["panel"](rec) if rec else None
        if panel is None or panel.final_total is None:
            continue   # not resolved yet (or ambiguous) — stays open
        outcome = float(panel.final_total)
        # Fail loud on an unknown side rather than silently settling as the
        # away/below bet (audit defect C3).
        bet_above = is_over_side(pos["side"])
        strike = pos.get("strike")
        if strike is None:
            continue
        payoff = 1.0 if ((outcome > float(strike)) == bet_above) else 0.0
        pnl = payoff - float(pos["all_in"])
        row = {"kind": "settle", "id": pid, "settled_ts": time.time(),
               "outcome": outcome, "payoff": payoff, "pnl": pnl,
               "status": "settled"}
        _append(book, row)
        settled.append(row)
        log(f"[paper:{book}] SETTLE {pid} outcome={outcome} payoff={payoff} "
            f"pnl={pnl:+.3f}")
    return settled


def status(book: str) -> dict:
    """Forward record summary: open/settled counts, total + mean pnl (cents),
    plus a maker block (fill rate + per-SIGNAL EV with unfilled signals as
    EV-0 denominators — the study's headline accounting)."""
    state = positions(book)
    open_rows = [p for p in state.values() if p.get("status") == "open"]
    done = [p for p in state.values() if p.get("status") == "settled"]
    pnls = [float(p["pnl"]) for p in done if "pnl" in p]
    by_strat: dict = {}
    for p in done:
        by_strat.setdefault(p.get("strategy", "?"), []).append(float(p["pnl"]))

    # Maker lifecycle accounting (rows that went through a "rest").
    maker: dict = {}
    for p in state.values():
        if "rest_price" not in p:
            continue
        m = maker.setdefault(p.get("strategy", "?"), {
            "resting": 0, "filled_unsettled": 0, "cancelled": 0, "settled": 0,
            "_settled_pnl": []})
        st = p.get("status")
        if st == "resting":
            m["resting"] += 1
        elif st == "filled":
            m["filled_unsettled"] += 1
        elif st == "cancelled":
            m["cancelled"] += 1
        elif st == "settled":
            m["settled"] += 1
            if "pnl" in p:
                m["_settled_pnl"].append(float(p["pnl"]))
    for m in maker.values():
        settled_pnl = m.pop("_settled_pnl")
        n_filled = m["filled_unsettled"] + m["settled"]
        n_decided = n_filled + m["cancelled"]      # signals past their horizon
        m["fill_rate"] = round(n_filled / n_decided, 3) if n_decided else None
        # per-signal EV: cancelled signals contribute EXACTLY 0; only settled
        # fills carry realized pnl (unsettled fills are excluded from the
        # numerator AND denominator until they resolve).
        n_resolved = m["settled"] + m["cancelled"]
        m["ev_per_signal_c"] = (round(100 * sum(settled_pnl) / n_resolved, 2)
                                if n_resolved else None)

    out = {
        "book": book, "open": len(open_rows), "settled": len(done),
        "total_pnl_c": round(100 * sum(pnls), 2) if pnls else 0.0,
        "mean_cents_per_trade": (round(100 * float(np.mean(pnls)), 2)
                                 if pnls else None),
        "by_strategy": {k: {"n": len(v),
                            "mean_c": round(100 * float(np.mean(v)), 2)}
                        for k, v in by_strat.items()},
    }
    if maker:
        out["maker"] = maker
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book", default="weather_v1")
    ap.add_argument("--strategy", default="drift_fade_control",
                    choices=sorted(STRATEGIES))
    ap.add_argument("--open", action="store_true",
                    help="scan live events and open/rest current-signal positions")
    ap.add_argument("--mark", action="store_true",
                    help="advance resting maker orders (fill/cancel) on fresh candles")
    ap.add_argument("--settle", action="store_true",
                    help="settle resolved positions")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--recent-min", type=float, default=30.0)
    a = ap.parse_args(argv)
    if a.open:
        if STRATEGIES[a.strategy].get("execution") == "maker":
            scan_and_rest(a.strategy, book=a.book, recent_min=a.recent_min)
        else:
            scan_and_open(a.strategy, book=a.book, recent_min=a.recent_min)
    if a.mark:
        mark_book(a.book)
    if a.settle:
        settle_book(a.book)
    if a.status or not (a.open or a.mark or a.settle):
        print(json.dumps(status(a.book), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
