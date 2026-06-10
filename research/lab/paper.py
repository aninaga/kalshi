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

CLI (operator/cron; network for --open/--mark/--settle)::

    python -m research.lab.paper --open   --book weather_v1 --strategy drift_fade_control
    python -m research.lab.paper --open   --book weather_maker_v1 --strategy forecast_gap_maker
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

from research.lab.types import Panel

_ROOT = Path(__file__).resolve().parents[2]
PAPER_DIR = _ROOT / "market_data" / "paper"

# Sides that pay 1.0 when the outcome ends ABOVE the strike (lab.strategy).
_OVER_SIDES = frozenset({"over", "long_home", "home", "long", "buy", "yes",
                         "cover_home"})


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
# live data glue (weather family; thin seams, patched in tests)
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


# --------------------------------------------------------------------------- #
# open / settle / status
# --------------------------------------------------------------------------- #
def scan_and_open(strategy_name: str, *, book: str, recent_min: float = 30.0,
                  fill_model=None, now_ts: Optional[float] = None,
                  live_events=_live_weather_events,
                  live_rec=_live_weather_rec, log=print) -> list:
    """One forward pass: open a paper position per live event whose signal
    fires NOW (within the trailing ``recent_min`` of data). Idempotent: an
    event already holding a position for this strategy is skipped."""
    spec = STRATEGIES[strategy_name]
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
        panel = _build_live_panel(rec) if rec else None
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


# --------------------------------------------------------------------------- #
# maker lifecycle: rest -> (fill | cancel) -> settle
# --------------------------------------------------------------------------- #
def scan_and_rest(strategy_name: str, *, book: str, recent_min: float = 30.0,
                  now_ts: Optional[float] = None,
                  live_events=_live_weather_events,
                  live_rec=_live_weather_rec, live_panel=None,
                  log=print) -> list:
    """One maker forward pass: REST a passive order per live event whose frozen
    signal fires NOW. Mirrors ``weather_maker_study.run_arm``'s construction
    exactly: signal bar from the frozen entry/window/freshness, i+1 latency,
    ATM strike snapped on the boundary set, rest at the prevailing best bid for
    the signal side (joined queue, never improved). Idempotent per event."""
    from research.scripts import weather_maker_study as MK

    spec = STRATEGIES[strategy_name]
    if spec.get("execution") != "maker":
        raise ValueError(f"{strategy_name!r} is not a maker tenant")
    horizon_min = float(spec["horizon_min"])
    build_panel_fn = live_panel or _build_live_panel

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
        side = MK.FROZEN.side(panel, bar)
        mbook = MK.build_maker_book(rec)
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
              live_rec=_live_weather_rec, log=print) -> list:
    """Advance every RESTING order against fresh live candles.

    For each resting order: rebuild the maker book from a fresh fetch (its
    candles now extend past the rest time, so prints that occurred between
    passes are observed retroactively) and apply the study's fill detector —
    FILLED at the first in-horizon minute where a contributing leg printed
    volume>0 AND the boundary traded-low reached the rest price (fill price =
    rest, maker fee as registered); CANCELLED once ``now`` is past the horizon
    end with no such print (EV 0, row kept: unfilled signals are denominators).
    Idempotent: filled/cancelled orders are terminal for this pass."""
    from research.scripts import weather_maker_study as MK

    advanced = []
    for pid, pos in positions(book).items():
        if pos.get("status") != "resting":
            continue
        now = float(now_ts if now_ts is not None else time.time())
        horizon_end = float(pos["horizon_end_ts"])
        rec = live_rec({"event_ticker": pos["event_id"],
                        "city": pos.get("city", "")})
        mbook = MK.build_maker_book(rec) if rec else None
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


def settle_book(book: str, *, live_rec=_live_weather_rec, log=print) -> list:
    """Settle every open/filled position whose event has resolved."""
    settled = []
    for pid, pos in positions(book).items():
        # "open" = taker position; "filled" = maker order that got a real
        # print. RESTING orders never settle — they have no position.
        if pos.get("status") not in ("open", "filled"):
            continue
        rec = live_rec({"event_ticker": pos["event_id"]}, settled=True)
        panel = _build_live_panel(rec) if rec else None
        if panel is None or panel.final_total is None:
            continue   # not resolved yet (or ambiguous) — stays open
        outcome = float(panel.final_total)
        bet_above = pos["side"] in _OVER_SIDES
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
