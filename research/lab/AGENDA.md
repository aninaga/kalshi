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
4. **(self-opt 2026-06-11T19:41Z) Surface collapsed to ONE survivor; new wave.**
   The robustness pass (graded this beat) leaves the entire program with exactly
   ONE surviving backtested edge: **BTC 95-99c, hour=20 ET, spread=1c, 31-60m,
   vol=1-99 taker** — +1.63c/contract, K=386 declared, clears alpha/K AND stable
   under all 3 boundary shifts (btc_9599_selection). Everything else died:
   wx_sched_walkforward proved the 66-cell weather schedule is EX-POST-ONLY
   (negative REPORT EV, CI crosses 0) -> **the live-book re-point proposal is
   DEAD**; wx_gap_taker -2.32c (taker can't rescue the maker calibration);
   nba_revrev_oos DEAD (but the home moneyline leg is clean & reusable).
   Codex wave this beat (all 4 spooled): (a) **btc_9599_capacity** — is the lone
   survivor tradeable at size or a thin-book (vol1-99) artifact?; (b)
   **btc_session_regime** — is hour=20 a coherent ET-session effect or an
   isolated-spike artifact?; (c) **nba_clv_homeleg** — favorite-longshot CLV on
   the clean home leg (tests the desk's cross-family theme in NBA); (d)
   **wx_xcity_spillover** — NEW weather family, cross-city realized-temp signal,
   non-microstructure. **Next interactive-apex build thread (gated on the
   capacity verdict): a crypto paper pilot of the BTC hour=20 survivor** — the
   first edge that has earned a forward test. Do NOT pilot until btc_9599_capacity
   confirms it is one-sided/real and sized, not an illiquidity artifact.

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
  As of 2026-06-11 19:41Z: 0 fills, **5 cancelled**, fill_rate 0.0, ev 0.0.
  **RE-POINT PROPOSAL NOW DEAD:** wx_sched_walkforward (graded this beat) proved
  the 66-cell schedule is EX-POST-ONLY (negative REPORT EV, Bonferroni CI crosses
  0), so there is NO validated spec to re-point the book to. The book is now a
  zero-evidence tenant running a spec (all-city 30c) whose own backtest rationale
  is a mark-to-unfilled artifact, AND it produces 0 fills, so it cannot even serve
  as a passive fill-rate measurement. **STRUCTURAL PROPOSAL for the owner/
  interactive apex (do NOT self-modify the tenant): either (a) PAUSE the book —
  it is burning a tenant slot for zero information — or (b) re-point it to a
  TIGHTER, more-aggressive quote (touch/cross, fewer cities) purely to manufacture
  fills, accepting it as a fill-mechanics probe not an alpha bet.** Maker viability
  is otherwise resolvable only by real fills, which this book is not generating.
- **BTC stride-1 backfill** — **DONE** (pid 48627 exited; crypto cache 2011,
  full ~66d BTC density; log summary skipped=1574 empty=9). Favorite-leg
  standalone re-test is NOT being re-run: the favorite leg + its maker successor
  are now DEAD (see DONE), so the n>=200 stability re-test is moot. The only
  surviving crypto thread (BTC 95-99c sleeve) is queued as btc_9599_subspec.

## DONE (recent)
- **Robustness + new-family wave graded (2026-06-11T19:41Z)** — 4 memos; the
  surviving surface collapses to ONE candidate and the live-book re-point dies.
  - `wx_sched_walkforward`: EX-POST-ONLY — the 66-cell weather schedule does NOT
    generalize. On the untouched REPORT window (05-15..06-08) the SELECT-survivor
    list has NEGATIVE EV/signal (-0.606c, barely better than -0.615c quote-
    everywhere) and its Bonferroni CI [-3.95c,+2.49c] crosses 0. **Kills the
    prior beat's structural re-point proposal.**
  - `btc_9599_selection`: PARTIAL SURVIVE — K=386; hour=20 ET cell clears alpha/K
    on the declared split AND stays significant under all 3 boundary shifts
    (+1.63c/contract, CI [+0.56c,+1.97c]); hour=09 boundary-fragile. **The desk's
    only surviving backtested edge.** -> btc_9599_capacity (size?) +
    btc_session_regime (real session effect?) lanes.
  - `wx_gap_taker`: TAKER DEAD — best cell -2.32c; every threshold x side
    negative; no city slice survives event-cluster CI. Crossing the spread cannot
    monetize the unfilled-maker calibration.
  - `nba_revrev_oos`: DEAD (1st new family) — holdout reversion mean positive but
    adjusted CI crosses 0, train config negative after 2c. **Reusable data fact:**
    away leg unrecoverable from token IDs, but the home moneyline leg is clean
    (terminal mid matched result 1205/1221) -> nba_clv_homeleg builds on it.
  META: surviving surface = 1 candidate (BTC hour=20). Replaced by 4 lanes:
  btc_9599_capacity, btc_session_regime, nba_clv_homeleg, wx_xcity_spillover.
  Row in experiments.jsonl.
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
