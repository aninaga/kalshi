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
- **Min-holdout-events floor (2026-06-12 self-opt)**: any registered hypothesis
  or locked taker rule whose holdout fires on < 12 DISTINCT settlement events is
  UNDERPOWERED — label it so and do NOT claim an edge regardless of point
  estimate or which way the CI leans. Recurring failure mode this kills:
  nba_revrev, nba_clv_homeleg (CI [-5.29c,+6.16c]) and wx_xcity (5 events, 2
  winners, CI crosses 0) ALL produced point-positive holdouts whose multiple-
  testing CI crossed zero largely because the event count was tiny. Few-event
  holdouts cannot distinguish edge from luck; spend the lane on a higher-event-
  count expression instead. (Codify into governance when convenient.)

## NOW (top = next lane to launch)
1. ~~Crypto paper-trading support + BTC hour=20 pilot (build lane)~~ **BUILT
   2026-06-12T03:14Z** (interactive apex; run note
   `runs/paper_pilots_20260612T031450Z.md`). Paper harness generalized to a
   per-family live seam (LIVE_FAMILIES: weather + crypto); two MEASURE-ONLY
   books enrolled and on the controller cadence — see IN-FLIGHT. The pilot is
   exactly the NARROW spec proposed here (hour=20 only, spread 1c, 31-60m,
   vol>=100, both sides, cost line fee+1c via the real ceil'd taker fee);
   docstrings carry BTC-ONLY-ARTIFACT + ISOLATED-SPIKE-NOISE + declared K=386
   and forbid promotion without the min-12-holdout-events + alpha/K rules.
2. ~~Favorite-leg taker lane (hyp #3) + 80-99c maker successor~~ **BOTH DEAD**
   (taker cancelled pre-gate; 80-99c maker successor KILLED by band8099_maker
   memo 2026-06-11: no maker spec survives holdout, -1.49c bid/-2.91c mid on
   fills, only 40% restable). See DONE.
3. ~~Crypto late-ATM maker variant~~ **DEAD pre-launch** (late_atm_maker memo
   2026-06-11: UNDER-side late-window maker neg dev+holdout, -11.64c holdout,
   adverse selection; gated lane saved). See DONE. The hardening successors
   (wx_maker_schedule/queue/gap_realfill, btc_9599_subspec) ALL graded
   2026-06-11T14:41Z — none promotable (see DONE).
4. **(2026-06-12T00:41Z) Capacity gate PASSED; both new families DEAD; new wave.**
   Graded this beat: **btc_9599_capacity = SIZEABLE** — the lone survivor (BTC
   95-99c / hour=20 ET / spread1c / 31-60m) is positive across vol 1-99/100-999/
   1000+ and on BOTH sides (~$683 alpha/day at full size), cost-robust to fee+1c.
   The pilot gate (see NOW#1) is therefore OPEN. BUT **btc_session_regime =
   ISOLATED-SPIKE-NOISE** — hour=20 is positive in both halves yet its neighbors
   are negative and no ET session block survives Bonferroni; it is a single-hour
   anomaly with no mechanism, so the pilot must stay narrow. Both NEW families
   died: **nba_clv_homeleg = REAL-BUT-UNTRADEABLE** (clean favorite-longshot
   calibration, but late-taker CI [-5.29c,+6.16c] crosses 0; home leg still a
   clean reusable substrate); **wx_xcity_spillover = DEAD** (cross-city feature
   worsens holdout log-loss; 5-event taker, CI crosses 0). New codex wave this
   beat (4 spooled): (a) **btc_9599_xasset** — does the frozen sleeve generalize
   to ETH, or is it BTC-only? (decision-relevant for the pilot); (b)
   **crypto_favlongshot_hold** — is favorite-longshot a BROAD buy-and-hold edge or
   just the hour=20 tip? (non-microstructure); (c) **wx_forecast_revision** — NEW
   non-microstructure weather family (forecast-revision momentum/reversion);
   (d) **nba_fav_hold** — early buy-and-hold favorite vs the dead late taker, on
   the clean home leg. Next interactive-apex build = the NOW#1 pilot; the
   btc_9599_xasset verdict should gate how confidently to size it.

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
- **crypto_btc9599_v1 paper book (btc_9599_pilot, 2026-06-12)** — MEASURE-ONLY
  taker scan of the frozen BTC 95-99c / hour=20 ET / spread1c / 31-60m /
  vol>=100 sleeve; records would-be fills at the displayed ask (the memos'
  cross-through case), max 1 entry per event-side. Beats: open/mark 15m,
  settle hourly. Expect ~1 event/day; weeks to a verdict by design. Promotion
  forbidden by docstring without min-12-holdout-events + declared-K (K=386).
- **weather_fillprobe_v2 paper book (wx_fillprobe, 2026-06-12)** — measure-only
  maker FILL PROBE: same frozen forecast-gap signal as v1, 60m rests (a
  pre-registered study horizon), restricted to the fill-MECHANICS levers
  (NYC+CHI, local 06:00-15:59; the 66-cell schedule is EX-POST per
  wx_sched_walkforward and is NOT used). Purpose: manufacture fills to measure
  fill rate + adverse selection (~40% lower-bound 60m fill expected vs v1's
  27%-at-30m). NOT alpha; answers the prior beat's structural proposal with
  option (b) in a NEW book — weather_maker_v1 untouched.
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
- **Capacity + regime + new-family wave graded (2026-06-12T00:41Z)** — 4 memos;
  the pilot gate PASSES, the BTC survivor is confirmed sizeable, both new
  families die.
  - `btc_9599_capacity`: **SIZEABLE** — hour=20 sleeve positive in vol 1-99
    (+1.63c), 100-999 (+1.33c), 1000+ (+1.72c) and on BOTH sides (YES +1.49c,
    NO/sellYES +1.82c); ~$683 alpha/day if allowed into 100+ buckets, ~$14/day on
    the locked thin cell alone. Break-even ~fee+1.63c adverse entry; fee+2c kills
    it. NOT a thin-book artifact. **Pilot gate OPEN** (NOW#1).
  - `btc_session_regime`: **ISOLATED-SPIKE-NOISE** — hr20 positive both halves
    (0.96/0.99c) but hr19 -1.87c, hr21 -0.66c, lag-1 autocorr -0.24, no a-priori
    ET session block survives 4-way Bonferroni. A single-hour anomaly, no
    mechanism -> keep any pilot narrow, do NOT expand to neighbor hours.
  - `nba_clv_homeleg`: **REAL-BUT-UNTRADEABLE** — clean holdout favorite-longshot
    calibration (longshots <0.3 over by 1.2-5.1c, favorites >0.5 under by
    1.6-9.7c) but K_sel=891 late-taker +0.98c/trade with game-cluster CI
    [-5.29c,+6.16c]; OOS edge did not concentrate late. Home leg = clean reusable
    calibration substrate.
  - `wx_xcity_spillover`: **DEAD** — cross-city feature worsens holdout log-loss
    (-0.00219) vs forecast-only; market mid better-calibrated; locked YES-MIA-0.15
    taker fires on 5 events (2 winners), adjusted CI [-0.10,+0.96] crosses 0.
  META: pilot gate passed but with a narrowness caveat; both new families died
  the SAME way (point-positive holdout, multiple-testing CI crosses 0, few
  events) -> motivated the new Min-holdout-events HARD RULE. Replaced by 4 lanes:
  btc_9599_xasset, crypto_favlongshot_hold, wx_forecast_revision, nba_fav_hold.
  Row in experiments.jsonl.
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
