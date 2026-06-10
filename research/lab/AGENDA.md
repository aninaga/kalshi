# Standing research agenda — the queue must never be empty

Apex-maintained backlog. The 5h apex loop consumes from the top; anything
launched moves to IN-FLIGHT; verdicts move to DONE with a one-line result.
Priority = expected information value about making (at least paper) money.

## NOW (top = next lane to launch)
1. **BTC stride-1 backfill** (data desk) — favorite leg standalone died ONLY
   on n<200 stability (net +6.40c nontest, CI [+0.32,+12.28], n=151). Stride-1
   doubles BTC event density over the same 66 days; n>=200 becomes reachable.
   Rate-limit-paced fetch, idempotent. Then:
2. **Favorite-leg standalone gated lane** (crypto hyp #3) — T-15 buy YES
   ask in [0.70,0.75], volume>0, one per event. Watch the walkforward decay
   (Apr +3.3 -> May +0.9 combined) — if the refreshed sample decays the same
   way it's regime, kill without regret.
2. **Crypto paper-trading support** (build lane) — paper.py is
   weather-specific (_live_weather_events). Generalize the live-panel seam so
   crypto tenants can enroll: late-ATM maker variant + the wave-3 spread
   maker pilot (measure-only: record conditional-on-fill win rates).
3. **Crypto late-ATM maker variant** (gated lane) — the DEAD taker lane's
   survivor insight + maker-study methodology applied to KXBTC/KXETH books.

## QUEUED
4. Wave-3 grading + experiments.jsonl outcome row when all 6 scouts land
   (vendor A/B: confirmed-findings-per-$).
5. Crypto top-up cadence: daily -> 6h (controller TOPUP change) once any lane
   needs walk-forward freshness past the locked splits.
6. Weather favorite maker expression — round-2 killed the taker version on
   zero-volume entries; maker variant only if weather_maker_v1 paper shows
   real fills at touch.
7. New-family scouting sweep (econ prints / climate / sports when in season)
   — governor-paced codex fleet, same lens methodology.
8. ccusage-independent Claude $-attribution (operator vs apex vs lanes) to
   sharpen the surplus forecast.

## IN-FLIGHT
- weather_maker_v1 paper book (forecast_gap_maker) — live, beats every 15m.
- Wave-3 A/B: w3_cdx_{1,2,3} (codex) running; w3_cl_1 (volume-bursts opus)
  running; cl_2 spread (done: reject + maker pilot), cl_3 ladder (done:
  honest negative).

## DONE (recent)
- Crypto hyp #2 fav_tail composite: **DEAD in-gate** (combined net +1.81c
  nontest but walkforward decay Apr->May->Jun + longshot CI<=0; commit
  1771e68). Survivor: favorite leg standalone (+6.40c, n-starved).
- Round-2 verification: fav_tail WEAKEN (then died in-gate, above); xasset
  KILL; wx_favorite KILL (zero-volume); wx_revision KILL (market prices the
  vintage within ~1 min).
- Maker study (weather forecast-gap): OOS +1.14c/signal, promoted to paper.
- Crypto hyp #1 late-ATM sell: honest DEAD (taker costs).
