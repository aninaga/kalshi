# Data Recovery Notes (WS0 verification spike — 2026-05-29)

Confirmed by live API probes + lake profiling. These are the facts WS2/WS3 build on.
Anything marked `[LEAD]` is plausible but NOT yet verified end-to-end.

## Kalshi historical recovery — CONFIRMED VIABLE (root cause = wrong endpoint tier)

- **Coverage today:** 30 / 1090 games have Kalshi ticks (all Mar 22–25 2026). Cause is NOT data loss.
- **Root cause:** `nba_odds_study/kalshi_hist.py::enumerate_markets` reads
  `GET /events/{SERIES}-{datecode}{AWAY}{HOME}?with_nested_markets=true`. For settled games the
  event still resolves (HTTP 200) but its **nested `markets` array is empty**, so no tickers are
  enumerated → no candles → empty shard. Pre-cutoff settled markets have moved to the **historical tier**.
- **Cutoff:** `GET /trade-api/v2/historical/cutoff` → `{market_settled_ts: 2026-03-30, ...}`. Markets
  settled **before** this use the `/historical/*` tier; after, the live tier. (The cutoff advances over time.)
- **Confirmed recovery path (old game 2025-10-21 GSW@LAL):**
  1. Enumerate: `GET /historical/markets?event_ticker=KXNBAGAME-25OCT21GSWLAL`
     → **2 markets** (`...-LAL`, `...-GSW`), `status=finalized`. The `-{TEAM}` suffix is the yes-token's team.
  2. Candles: `GET /historical/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=1`
     → **307 candles** (the live `/series/{series}/markets/{ticker}/candlesticks` path 404s for old markets).
  - Candle fields unchanged from the live path: `end_period_ts`, `yes_bid`/`yes_ask` (`.close_dollars`),
    `price` (last). Mid = (bid+ask)/2.
- **Event-ticker format:** `KXNBAGAME-{datecode}{AWAY}{HOME}` where datecode = `strftime('%y%b%d').upper()`
  (e.g. `25OCT21`). Probe tries both AWAY+HOME and HOME+AWAY orderings (the existing code already does).
- **Pagination:** `/historical/markets?series_ticker=KXNBAGAME` returns a `cursor`; page back to enumerate
  all games if the per-event lookup is preferred to skip. Per-`event_ticker` lookup is simplest and targeted.
- **Auth:** none (public). **Rate limit:** generous; 1090-game backfill is fine with the existing retry/backoff.
- **WS2 implementation:** add a historical-tier fallback in `kalshi_hist.enumerate_markets`/`fetch_candles`
  (try live; if 0 markets/candles, hit `/historical/markets?event_ticker=...` then
  `/historical/markets/{ticker}/candlesticks`). Expected ceiling: high (data exists for old games).

## Polymarket fill data — no historical book; trades ARE recoverable

- **Live book is gone for settled markets:** `GET clob.polymarket.com/book?token_id=...` → 404
  ("No orderbook exists"). Confirms **no official historical depth/bid-ask**.
- **Trades CONFIRMED recoverable:** `GET data-api.polymarket.com/trades?market={conditionId}&limit=N`
  → real trades for the game ("Warriors vs. Lakers": `SELL sz=389.47 px=0.999`, `SELL sz=877.19 px=0.999`)
  with `side, size, price, timestamp`. **Filter is the `conditionId` (0x… hash), NOT the clob token id.**
  Get `conditionId` from the Gamma event: `GET gamma-api.polymarket.com/events?slug=nba-{away}-{home}-{date}`
  → `markets[].conditionId` + `clobTokenIds`.
- **Fill-data source decision (WS3):** trade-reconstruction is the FREE, OFFICIAL floor — from trade
  size/price/side compute per-market realized spread + a size→slippage curve by liquidity tier, and replace
  the fabricated constant-depth book. `[LEAD]` paid/3rd-party depth archives (PMXT hourly, PolymarketData.co
  1-min) remain unverified; only pursue if trade-calibration proves insufficient.

## Source rebuild feasibility
- `nba_odds_study/_cache/*.pkl` pickles EXIST (Dataset objects incl. `game.on_intervals`), so a
  from-pickle cache rebuild is feasible **without** re-fetching ESPN — useful if a source-level fix is ever
  needed. WS1's C1/C2/C3 fixes did NOT require it (they derive cleanly from correct raw lake data); C4
  (stars_on NaN-vs-0) measured at ~0% prevalence, so no rebuild needed there either.
