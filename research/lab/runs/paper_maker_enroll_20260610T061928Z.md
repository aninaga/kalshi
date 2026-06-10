# Paper-maker enrollment — `forecast_gap_maker` (weather/temp), the program's first live-promotion candidate

**Lane:** build (maker paper-trading tenant). **Date:** 2026-06-10T06:19Z.
**Maker hyp:** `b8b071fa26ea4933` (`research/scripts/weather_maker_study.py`).
**Parent (signal) hyp:** `3cb384c482d1c2f7` (`research/scripts/weather_forecast_gap_e2e.py`).

## What was built

1. **Maker order lifecycle in `research/lab/paper.py`** (extends the forward
   paper harness; the taker control tenant and its book format are untouched):
   - `scan_and_rest` (`--open` for tenants with `execution="maker"`): when the
     FROZEN signal fires NOW (same `recent_min` current-signal gate as the
     taker path), append a `rest` row (status `resting`) — side, ATM-snapped
     strike on the listed boundary set, `rest_price` computed EXACTLY like the
     study from the live ladder (boundary OVER bid = sum of contributing legs'
     `yes_bid`; UNDER bid = 1 − boundary ask; join the queue, never improve),
     `entry_ts` = signal + 1 min (the study's i+1), `horizon_end_ts` =
     entry + 30 min.
   - `mark_book` (`--mark`): refetches live candles (which now extend past the
     rest time, so prints between passes are observed retroactively) and
     applies the study's fill detector verbatim (`weather_maker_study
     ._detect_fill`, `QUEUE_THROUGH_C = 0`): FILLED at the first in-horizon
     minute with volume > 0 on a contributing leg AND boundary traded-low <=
     rest (fill price = rest, maker fee 0c); CANCELLED once `now` passes the
     horizon end unfilled. Cancelled rows are KEPT — unfilled signals are the
     denominator of per-signal EV. Passes are idempotent (terminal states are
     skipped on replay).
   - `settle_book` now settles `filled` rows exactly like `open` ones
     (payoff vs strike against the realized bucket; pnl = payoff − all_in).
     `resting` rows never settle (no position).
   - `status` gained a `maker` block: resting / filled_unsettled / cancelled /
     settled counts, `fill_rate` over decided signals, and `ev_per_signal_c`
     (settled pnl over resolved signals; cancels contribute exactly 0).
   - Live-panel glue fixes needed by ANY windowed live signal:
     `_live_weather_rec` now captures the listed close time (`close_ts`) and
     `_build_live_panel` overrides `duration_sec` with it — a live panel is
     truncated at "now", so the candle-span duration would make
     `elapsed_frac` ≈ 1 at every bar and the [0.05, 0.30] entry window could
     never fire. `_build_live_panel` also runs `feature_store.join_panel`, so
     live panels carry `forecast_high_f` exactly as backtest panels do.

2. **Tenant `forecast_gap_maker`** in `paper.STRATEGIES`, book
   `weather_maker_v1`. The factory returns the frozen parent strategy via
   `weather_forecast_gap_e2e.build_strategy()`; signal-bar selection reuses
   `weather_maker_study._frozen_signal_bar` (frozen entry + window +
   freshness, one trade/event). Nothing is retuned in the harness.

3. **Live forecast vintage updater**
   `research/scripts/update_forecast_vintages.py`: appends the lead-1/lead-2
   Open-Meteo Previous-Runs vintages for currently OPEN events to
   `market_data/weather/features/forecast_high_f.parquet`, with the original
   builder's exact PIT rule (`known_from_ts = (local target-day 23:00) −
   24·lead h`; see `research/lab/runs/data_forecast_high_f_20260609T214420Z.md`).
   Live-boundary honesty added on top: a (event, lead) row is written only
   when the full local day's hourly series is present and finite AND its
   `known_from_ts` is already in the past — a partial-day max is not the
   vintage the backtest used. Appends dedupe on (event_id, known_from_ts);
   re-runs are no-ops. Original provenance preserved; each run appends a
   `live_updates` entry. First live run: 12 rows / 6 open events appended
   (store 2500 → 2512 rows). NOTE: the assignment said "lead-0/lead-1"; the
   original field is built from previous_day1/previous_day2 (lead-1/lead-2),
   and the live updater mirrors THAT — same field, same vintages, no nowcast.

4. **Tests** `research/lab/tests/test_paper_maker.py` (synthetic panels with
   the Phase-1 OHLC candle keys; all live seams injected; no network):
   rest→fill, rest→cancel at horizon, volume-0 "quote flicker" does not fill,
   a print after the horizon does not fill, no premature cancel inside the
   horizon, idempotent open/mark passes, settle of filled rows, per-signal EV
   accounting, and frozen-param registry checks. 18/18 pass; full lab suite
   310 passed + 2 pre-existing `test_eda.py` failures that also fail on the
   clean tree (not this lane).

## Exact frozen parameters (cited, never retuned here)

Signal (`3cb384c482d1c2f7`): per-city TRAIN debias
{AUS −2.69, CHI −1.71, DEN −0.90, MIA −2.08, NYC −2.00}; |debiased forecast −
implied mid| ≥ 2.0 °F; elapsed_frac ∈ [0.05, 0.30]; staleness ≤ 2 min; i+1
entry; one trade/event; buy toward the forecast at the ATM-snapped strike.

Maker execution (`b8b071fa26ea4933`): rest at prevailing best bid for the
signal side at i+1; horizon H = 30 min (pre-registered headline); maker fee
0c (KXHIGH = standard general series); fill iff an in-horizon minute printed
volume > 0 AND boundary traded-low ≤ rest (joined-queue conservatism,
queue_through 0).

Backtest reference re-confirmed this session (nontest, H=30, fee 0c, study
re-run read-only): n_signal 616, fill_rate **26.5%**, edge per FILLED trade
+4.31c (filled-trade CI −3.2 … +11.6), per-signal EV **+1.14c**
(CI −0.85 … +3.06), win(filled) 52.1% vs win(unfilled) 54.1%.

## Pre-registered promotion criterion (frozen before the first live signal)

After **100+ live signals** on book `weather_maker_v1`:
1. live fill rate within **±8 points of 26%**, and
2. **per-signal EV positive** (settled pnl / resolved signals, cancels = 0).

Divergence on (1) ⇒ the candle-derived fill model is wrong; failure of (2) ⇒
live adverse selection exceeds the study's estimate. Either blocks promotion.
The criterion lives verbatim in the tenant factory docstring
(`paper._forecast_gap_maker`).

## How the controller should drive it

The signal window is the FIRST 5–30% of each ~34 h quote window, so `--open`
must run around the clock; `recent_min` 30 matches a 15-min cadence with
slack. Cadence (all idempotent; book JSONL is append-only and tracked):

```bash
# 1. vintages first (hourly) — open passes read the feature store
python3 -m research.scripts.update_forecast_vintages
# 2. rest new orders on current signals (every 15 min)
python3 -m research.lab.paper --open   --book weather_maker_v1 --strategy forecast_gap_maker
# 3. advance resting orders: fill/cancel (every 10–15 min; horizon is 30 min)
python3 -m research.lab.paper --mark   --book weather_maker_v1
# 4. settle filled rows after events resolve (hourly is plenty)
python3 -m research.lab.paper --settle --book weather_maker_v1
# 5. read the forward record
python3 -m research.lab.paper --status --book weather_maker_v1
```

## Caveats / honest notes

- Fill detection is the study's candle proxy, not an order-book replay: on
  multi-leg boundaries it is, if anything, GENEROUS to the maker (the study
  flags `multileg_over_fills` for exactly this reason). The ±8-point fill-rate
  band is the live check on that proxy.
- A `--mark` pass during a network/book outage never cancels early: cancels
  require `now > horizon_end_ts`, and a missing book at expiry is recorded
  with `reason="book_unavailable"` so those rows can be audited separately.
- Live smoke (2026-06-10 ~06:20Z): open/mark/status CLI passes ran clean
  against live Kalshi; no current signal at run time (expected — entries fire
  only in each event's early window), book starts empty.
