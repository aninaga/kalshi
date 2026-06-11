# Standing research agenda — the queue must never be empty

Apex-maintained backlog. The 5h apex loop consumes from the top; anything
launched moves to IN-FLIGHT; verdicts move to DONE with a one-line result.
Priority = expected information value about making (at least paper) money.

## HARD RULES (2026-06-10 first-principles audit)
- **Selection declaration**: every registered hypothesis MUST declare K (how
  many candidates scout/verify waves examined in its family first) in its run
  note, and clear its bootstrap CI at alpha/K until governance counts
  scout-examined candidates natively. A PASS without declared K is invalid.
- **Splits re-lock**: stride-1 added events outside the locked crypto id
  lists. Re-lock deterministically (same date boundaries) before any new
  crypto hypothesis registers.
- **Agenda lock**: two apexes edit this file — take /tmp/agenda.lock (mkdir)
  before editing, release after committing. A lost update already happened.
- **No-dead-microstructure STOP rule (2026-06-11 self-opt)**: do NOT spool any
  more top-of-book-only maker/taker microstructure backtests on crypto/weather.
  Eight lenses (taker volume-bursts/spread/ladder; maker late-ATM/80-99c-band/
  wide-spread-NO/under-side) have ALL died on the same wall — the apparent edge
  is unfilled-quote calibration that goes adversely-selected-negative once
  conditioned on fills, and the parquet has no queue/depth to model it. Maker
  viability is now resolvable ONLY by (a) real paper fills or (b) an explicit
  queue/fill-position model. Codex effort redirects to: hardening the LIVE
  weather maker (fill-schedule, queue-haircut, real-fill EV), the one
  weakly-alive BTC 95-99c thread, and NEW families whose edge is not a
  microstructure-fill edge.

## NOW (top = next lane to launch)
1. **Crypto paper-trading support** (build lane) — fills are the program's
   binding uncertainty (paper 0/3 vs ~26-42% expected; crypto books give ~48
   events/day vs weather's ~5, so fill evidence accumulates 10x faster).
   Load-bearing paper-engine refactor -> interactive apex, not a codex lane.
   Then enroll measure-only: late-ATM maker + spread maker pilot.
2. ~~Favorite-leg taker lane (hyp #3) + 80-99c maker successor~~ **BOTH DEAD**
   (taker cancelled pre-gate; 80-99c maker successor KILLED by band8099_maker
   memo 2026-06-11: no maker spec survives holdout, -1.49c bid/-2.91c mid on
   fills, only 40% restable). See DONE.
3. ~~Crypto late-ATM maker variant~~ **DEAD pre-launch** (late_atm_maker memo
   2026-06-11: UNDER-side late-window maker neg dev+holdout, -11.64c holdout,
   adverse selection; gated lane saved). See DONE. The hardening successors
   (wx_maker_schedule/queue/gap_realfill, btc_9599_subspec) ALL graded
   2026-06-11T14:41Z — none promotable (see DONE).
4. **(self-opt 2026-06-11T14:41Z) Codex direction pivots to NEW families.**
   Weather/crypto top-of-book microstructure is now exhausted from a 4th angle:
   8 lenses + 4 hardening lanes all died or shrank to ex-post-selected thin
   slices once fills/haircuts were conditioned. Backtests can no longer resolve
   maker viability there — only the LIVE paper books can. So codex effort now
   splits: (a) selection-robustness re-tests of the few standing candidates
   (wx_sched_walkforward, btc_9599_selection, wx_gap_taker — already spooled),
   and (b) NEW families whose edge is NOT a microstructure-fill edge. First (b)
   lane spooled this beat: nba_revrev_oos (NBA moneyline revision-reversion OOS
   on the polymarket_ticks lake). Promotes QUEUED#7 into the active wave.

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
  As of 2026-06-11 14:41Z: still 0 fills, 3 cancelled, fill_rate 0.0. **NOT a
  broken tenant** — Pr(0/3)=19-34% under a strict proxy (wx_fill_geography).
  KEEP RUNNING. **NEW CAVEAT (wx_gap_realfill):** the book's quote logic is
  all-city 30c, whose backtest edge is haircut-fragile (mark-to-unfilled
  artifact; -0.49c strict passive). At 0/3 fills it is also generating ZERO
  evidence. STRUCTURAL PROPOSAL for the interactive apex (do NOT self-modify the
  tenant): re-point the book to the wx_maker_schedule 66-cell survivor list with
  60m rest (~39.5% modeled fill) so it actually produces real fills — the only
  instrument left that can settle maker viability. Levers: 60m>>30m rest, city
  NYC/CHI>DEN, AM-mid-afternoon.
- **BTC stride-1 backfill** — **DONE** (pid 48627 exited; crypto cache 2011,
  full ~66d BTC density; log summary skipped=1574 empty=9). Favorite-leg
  standalone re-test is NOT being re-run: the favorite leg + its maker successor
  are now DEAD (see DONE), so the n>=200 stability re-test is moot. The only
  surviving crypto thread (BTC 95-99c sleeve) is queued as btc_9599_subspec.

## DONE (recent)
- **Maker-hardening spool wave graded (2026-06-11T14:41Z)** — 4 memos, all
  reinforce the STOP rule from a 4th angle. None promotable.
  - `wx_maker_schedule`: CONDITIONAL/WEAK — quote-everywhere all-cell book
    selects NEGATIVE on the strict fill proxy (-0.76c EV/signal, 38% 60m fill);
    a train-positive screen still LOST on holdout; only an ex-post HOLDOUT-
    SELECTED 66-cell (city,hour,offset) list shows +5.49c. Selection-inflated,
    not forward alpha. -> robustness lane wx_sched_walkforward spooled.
  - `wx_maker_queue`: DEAD — forecast-gap maker holdout EV already negative at
    queue q=0 under BOTH brackets (-4.71c cross / -2.69c touch); fails before
    any queue penalty. No breakeven queue fraction exists.
  - `wx_gap_realfill`: HEADLINE/FRAGILE — the LIVE book's +1.14c edge does NOT
    survive realistic fills: +1.24c optimistic touch -> -0.49c strict passive
    -> -1.32c with a 1-tick queue proxy. The positive touch number is a
    mark-to-UNFILLED-quotes artifact; signals that actually cross are negative.
    Only thin ex-post MIA/DEN slices survive. Do NOT treat all-city 30c as
    validated live alpha. -> wx_gap_taker lane tests if a TAKER captures the
    (real) calibration the maker can't get filled on.
  - `btc_9599_subspec`: WEAK — broad 95-99c sleeve DEAD on holdout cross; 2
    ex-post cells (hr09/hr20 ET, spread1c, 31-60m, vol1-99) clear date split +
    n>=200 + mid-queue haircut but K undeclared from a large grid. ->
    btc_9599_selection lane applies the alpha/K rule + boundary-shift stability.
  META: even the LIVE book's backtest rationale is haircut-fragile -> maker
  viability for weather/crypto is resolvable ONLY by real paper fills. Row in
  experiments.jsonl. Replaced by 3 robustness lanes + 1 NEW family (nba_revrev_oos).
- **Maker-expression spool wave graded (2026-06-11T09:41Z)** — 5 memos, all
  converge: every top-of-book maker expression of a surviving calibration edge
  DIES on adverse selection once conditioned on fills.
  - `late_atm_maker`: DEAD — UNDER-side late-window maker neg dev+holdout
    (-11.64c holdout); passive fills selected exactly when UNDER is wrong side.
  - `band8099_maker`: DEAD — no maker spec survives holdout; YES 80-99c favorite
    -1.49c bid/-2.91c mid on fills; only 40% of band-events restable.
  - `wide_spread_no`: DEAD on fills — unfilled edge +8.63c in low-mid wide but
    -10c markout once filled (fills arrive as the market moves against).
  - `eth_btc_structure`: calibration confirmed; BTC 95-99c favorite +1.20c/
    contract holdout post-fee WITH real capacity (vol ~2,610) vs ETH no
    capacity (vol 25); but SIDE edge flips train->holdout => not directional
    alpha. The desk's only weakly-alive crypto thread -> btc_9599_subspec lane.
  - `wx_fill_geography`: weather 0/3 fills is NORMAL (Pr(0/3)=19-34% under strict
    proxy), NOT a broken tenant. Levers: 60m rest, city select (NYC/CHI best,
    DEN worst), morning-mid-afternoon. -> wx_maker_schedule/queue/realfill lanes.
  META: maker viability is resolvable ONLY by real paper fills or an explicit
  queue/fill model -> see No-dead-microstructure STOP rule. Row in experiments.jsonl.
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
