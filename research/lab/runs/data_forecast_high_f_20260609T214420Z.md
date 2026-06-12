# Data request `7e0d214cbea6162f` — `forecast_high_f` (weather/temp)

**Outcome: FULFILLED via EXTERNAL fetch.** 1,250 / 1,250 cached events (100%) got a
point-in-time daily-high-temperature FORECAST, two lead times each (2,500 rows),
written to the durable feature store at
`market_data/weather/features/forecast_high_f.parquet` and joined as-of onto
every `temp` panel.

- Filed: 2026-06-09T21:38:07Z. Fulfilled: 2026-06-09T21:48Z.
- Fetcher (re-runnable): `research/lab/runs/_fetch_forecast_high_f.py`
  (`PYTHONPATH=/home/user/kalshi python3 research/lab/runs/_fetch_forecast_high_f.py`).

## Source

**Open-Meteo Previous Runs API** — `https://previous-runs-api.open-meteo.com/v1/forecast`.
Free, no-auth, archives past model runs at fixed lead-time offsets. We request
hourly `temperature_2m_previous_day1` and `temperature_2m_previous_day2` and
compute the **daily max over the local calendar day** = the daily-high forecast
at 1-day and 2-day lead.

Endpoint pattern (one call per city covers its whole date range + both leads):
```
https://previous-runs-api.open-meteo.com/v1/forecast
  ?latitude={lat}&longitude={lon}
  &hourly=temperature_2m_previous_day1,temperature_2m_previous_day2
  &temperature_unit=fahrenheit&timezone={tz}
  &start_date={lo}&end_date={hi}
```
5 API calls total (one per city), throttled 0.25s apart.

### Why Previous Runs, not the Historical Forecast API
The Historical Forecast API stitches the *first hours* of successive runs into a
near-analysis "best estimate" series — its day-D value comes from a run
initialised close to D, so it is effectively a nowcast/analysis and its issue
time cannot be placed *before* the quote window. The request wants a forecast
that was knowable BEFORE the target day (to test informational edge). The
Previous Runs API delivers exactly that: per its docs,
`temperature_2m_previous_dayN` = "the value predicted 24*N hours before valid
time" (`_previous_day0` == the live run). I used the Historical Forecast API only
as a near-analysis proxy to *validate station coordinates* (below), not as the
forecast field.

## Point-in-time verification (the whole game)

`known_from_ts` is the forecast **ISSUE** time, never the valid/target time.

**Rule:** for target local day D and lead N,
`known_from_ts = epoch( (local day-D 23:00) - 24*N hours )` in UTC.

**Why it is leakage-safe (and why this exact anchor):** `previous_dayN` carries,
for each hour T, the value predicted ~24*N h before T. The day-D max can fall on
any hour up to 23:00 local, whose forecast was issued ~24*N h earlier. Anchoring
at the LAST hour of the day guarantees **no contributing hour was issued after
`known_from_ts`** — it is the leakage-safe *maximum* (latest) issue time. The max
almost always occurs mid-afternoon (verified: NYC median 15:00, p90 16:00 local),
whose true issue time is earlier, so the anchor is deliberately *conservative*: it
never claims an earlier issue than can be guaranteed.

**Empirically verified:**
- Semantics confirmed from Open-Meteo docs and a probe: the `_previous_dayN`
  suffix is **hourly-only** (the daily `temperature_2m_max_previous_dayN` form is
  rejected with HTTP 400), so the daily max is computed by us over local hours.
- `previous_dayN` values differ across leads (distinct model vintages, not a
  relabelled analysis); hourly values track the realized daily high to ~1 F at
  lead 1.
- **As-of join leakage assertion passes**: for 5 hand-checked events the
  forecast series attached to the panel exactly equals an independent
  recomputation `value of latest row with known_from_ts <= minute_ts[i]`
  (`leak_ok=True` for all). A future-issued value structurally cannot reach an
  earlier bar.
- **Multi-lead revision verified on KXHIGHNY-26MAY20**: window opens 2026-05-19
  14:01 UTC; lead-2 row (issued 2026-05-19 03:00 UTC, 95.7 F) covers from bar 0;
  the join revises to the lead-1 row (issued 2026-05-20 03:00 UTC, 94.2 F) at
  exactly bar 703 = 2026-05-20 03:00 UTC. Both track implied-mid ~92 F.
- **No NaN prefix** in practice: the lead-2 issue time (~36 h before local
  end-of-day) precedes every quote-window open (~14 h before local midnight), so
  the forecast is present from the first bar via the 2-day vintage and sharpens to
  the 1-day vintage mid-window. (The lead-1 issue time alone lands ~13 h *after*
  the NYC open, which is why a single-lead field WOULD have a NaN prefix — the
  multi-lead design fixes that honestly rather than by back-dating.)

## Station coordinates (validated, with one correction)

Kalshi settles each city on a specific NWS station. I chose coords to match and
**validated them against realized settlement buckets** (near-analysis daily-max
vs the settled YES-bucket representative; |error|):

| City | Station | Requested coord | Open-Meteo grid | MAE F | bias F |
|------|---------|-----------------|-----------------|-------|--------|
| NYC | Central Park (KNYC) | 40.7790, -73.9693 | 40.78858, -73.9661 | 1.06 | -0.11 |
| CHI | Midway (KMDW) | 41.7860, -87.7524 | 41.77811, -87.737 | 1.08 | -0.79 |
| MIA | Miami Intl (KMIA) | 25.7959, -80.2870 | 25.78765, -80.28457 | 1.41 | -1.07 |
| AUS | Austin-Bergstrom (KAUS) | 30.1944, -97.6699 | 30.18921, -97.66044 | 1.59 | -1.26 |
| DEN | Denver Intl (KDEN) | 39.8466, -104.6562 | 39.84898, -104.64549 | 1.94 | -1.23 |

**Correction to the request's station note:** it said "AUS≈Camp Mabry". The data
says otherwise — against realized buckets, **Austin-Bergstrom (KAUS) MAE 1.41 F vs
Camp Mabry 2.05 F**, so KXHIGHAUS settles on Bergstrom. I used Bergstrom and
recorded the discrepancy in provenance. (Open-Meteo grid-snaps to a ~2 km cell
near each station; both requested and returned coords are in
`forecast_high_f.provenance.json`.)

## Coverage & skill

- **Coverage: 1,250 / 1,250 events (100%)**, 250 per city × 2 leads = 2,500 rows.
  Every cached date 2025-10-02 .. 2026-06-08 had full 24-hour hourly data for both
  leads, including the recent boundary (verified through 2026-06-08).
- **Skill (last as-of = lead-1 forecast vs realized, all 1,061 settled panels):**
  forecast MAE 2.69 F (median 2.30); implied-mid(end) MAE 1.53 F (median 1.00).
  The market end-mid is sharper on average (it absorbs same-day info the day-ahead
  model lacks), but the forecast carries independent, point-in-time-honest signal
  available *earlier* in the window — exactly the informational-edge test the
  analyst wanted to originate.

## Join verification

- `feature_store.list_fields("weather")` → `['forecast_high_f']`.
- `data.load_panels("temp", ...)` attaches `features["forecast_high_f"]` on every
  panel; values are sane vs implied-mid and realized across all 5 cities.
- As-of leakage assertion passes (above); revision timestamp exact (above).

## Known limitations (also in provenance.json)

1. `known_from_ts` is the conservative LATEST issue time for the daily max; the
   afternoon-max forecast was knowable somewhat earlier, so the as-of join is
   pessimistic (never optimistic) about signal arrival.
2. Open-Meteo grid-snaps to a ~2 km cell near each station (returned coords
   recorded), not the exact instrument; residual station mismatch <= ~2 F.
3. Lead is in 24 h steps relative to valid time, an approximation of the true
   model init cadence (00/06/12/18 UTC runs); the 24 h-step anchor stays
   conservative.
4. `best_match` model selection (Open-Meteo may switch models across the
   archive); the field is a model forecast, not a single fixed model.
5. Daily max is computed from hourly `temperature_2m`, which can differ slightly
   from the official NWS daily-high instrument (sub-hourly peaks) — the ~1 F
   validation residual reflects this plus the ±0.5 F bucket-rep granularity.
