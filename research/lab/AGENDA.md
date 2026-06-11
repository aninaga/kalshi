# Standing research agenda — the queue must never be empty

Apex-maintained backlog. The 5h apex loop consumes from the top; anything
launched moves to IN-FLIGHT; verdicts move to DONE with a one-line result.
Priority = expected information value about making (at least paper) money.

## NOW (top = next lane to launch)
1. **Favorite-leg standalone gated lane** (crypto hyp #3) — T-15 buy YES
   ask in [0.70,0.75], volume>0, one per event. BLOCKED two ways this beat:
   (a) needs the stride-1 backfill (IN-FLIGHT) to finish so n>=200; (b)
   Claude-side GATED and claude_policy.desk_may_spend was not set. Leave for
   the interactive apex / when surplus opens. Watch the walkforward decay
   (Apr +3.3 -> May +0.9 combined) — if the refreshed sample decays the same
   way it's regime, kill without regret.
2. **Crypto paper-trading support** (build lane) — paper.py is
   weather-specific (_live_weather_events). Generalize the live-panel seam so
   crypto tenants can enroll: late-ATM maker variant + the wave-3 spread
   maker pilot (measure-only: record conditional-on-fill win rates). NOTE:
   load-bearing paper-engine refactor -> interactive apex/Claude, not a
   fire-and-forget codex lane.
3. **Crypto late-ATM maker variant** (gated lane) — the DEAD taker lane's
   survivor insight + maker-study methodology applied to KXBTC/KXETH books.

## QUEUED
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
  As of 2026-06-11 04:41Z: 0 fills, 3 cancelled, fill_rate 0.0 (no touch
  fill yet).
- **BTC stride-1 backfill** — launched 2026-06-11 04:41Z, pid 48627, detached,
  `--assets BTC --max-events 1600 --stride 1` (covers same ~66d window at full
  density). Idempotent/resumable; log ~/.kalshi_fund/lanes/crypto_btc_stride1.log
  (python block-buffers stdout — judge progress by cache count, not the log).
  When done, re-run favorite-leg standalone to test n>=200 stability.

## DONE (recent)
- **Wave-3 vendor A/B graded (2026-06-11)** — 5/6 scouts landed; w3_cdx_2
  (spread lens, codex) died with NO memo. On the two lenses where both vendors
  delivered, conclusions CONVERGE (taker is dead; the apparent edge IS the
  spread). Per-lens: volume-bursts -> codex taker DEAD (-3.2c holdout) /
  claude maker liq-provision PILOT +0.5c/contract holdout (t=3.4);
  ladder-consistency -> codex crossed-credit BTC vertical (sparse arb monitor,
  n=2 holdout) / claude intra_ladder_box riskless but economically negligible;
  spread -> codex MISSING / claude taker DEAD + stale-bid maker pilot
  (measure-only, ~0-3c). Outcome: claude arm wins confirmed-findings-per-$
  (3/3 landed, 1 pre-registrable candidate vs codex 2/3, 0). META-FINDING: all
  three lenses say taker is structurally dead at this venue/frequency; the only
  live direction is MAKER-side liquidity provision -> supports crypto paper
  maker pilots (item 2/3) and weather_maker_v1. Row appended to experiments.jsonl.
- Crypto hyp #2 fav_tail composite: **DEAD in-gate** (combined net +1.81c
  nontest but walkforward decay Apr->May->Jun + longshot CI<=0; commit
  1771e68). Survivor: favorite leg standalone (+6.40c, n-starved).
- Round-2 verification: fav_tail WEAKEN (then died in-gate, above); xasset
  KILL; wx_favorite KILL (zero-volume); wx_revision KILL (market prices the
  vintage within ~1 min).
- Maker study (weather forecast-gap): OOS +1.14c/signal, promoted to paper.
- Crypto hyp #1 late-ATM sell: honest DEAD (taker costs).
