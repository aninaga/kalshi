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
- **K-inheritance on re-grades (2026-06-12T10:43Z self-opt)**: a cell drawn from
  an already-swept family MUST inherit the family's prior sweep K — you CANNOT
  reset to K=1 by re-declaring one cell as "the single pre-registered hypothesis"
  AFTER the sweep already happened. Motivating failure: nba_extreme_fav_hold
  re-graded p>=0.85 NBA home-fav hold as K=1 and cleared (CI [+0.26,+8.37]), but
  the SAME family was swept at 4 thresholds the prior wave (nba_fav_hold) where
  all four CIs crossed 0 AND train was negative — so the honest multiplicity is
  K=4, not K=1, and the "clean" CI is K-laundering. Corollary: any re-grade of a
  surviving cell MUST also report TRAIN performance (a holdout-positive /
  train-negative family is a sign flip, not an edge). (Codify into governance
  when convenient.)

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
4. **(2026-06-12T05:42Z) Pilot survivor confirmed BTC-only & narrow; all 4 new
   families DEAD; backtest surface near-exhausted; new wave + cost-model flag.**
   Graded this beat (see DONE): **btc_9599_xasset = BTC-ONLY-ARTIFACT** (BTC hr20
   holdout +1.41c CI [0.88,1.81] stays positive but ETH -3.49c, hr20 not even
   ETH's best hour -> do NOT size the pilot up; keep it measure-only narrow);
   **crypto_favlongshot_hold = NO-EDGE** (broad 95-99c hold flat -0.18c; survivor
   is a narrow special cell, not a broad-bias tip); **wx_forecast_revision =
   DEAD** (market mid prices the revision; locked taker 14 events, CI entirely
   <0); **nba_fav_hold = DEAD-as-tradeable** (p=0.85 +4.31c but all CIs cross 0,
   train negative). The lone survivor is now boxed in (BTC-only, no mechanism,
   not a broad tip) and is correctly running ONLY as a measure-only narrow paper
   pilot. **COST-MODEL FLAG (self-opt proposal, not self-applied):** every
   hold-to-settlement memo charged a ROUND-TRIP fee 2*0.07*p*(1-p) on positions
   held to FREE Kalshi settlement; canonical desk fee (execution.py) is ONE-WAY
   0.07*p*(1-p). Arithmetic says it does NOT revive a dead family this round, but
   it systematically over-kills hold edges -> see the fee_model_audit lane +
   report proposal. New codex wave this beat (4 spooled, backlog floor restored):
   (a) **fee_model_audit** — ✅ DONE 2026-06-12, scope expanded far beyond the
   one-way-fee question: the PM flat-2%-of-notional taker fee NEVER EXISTED
   (official = per-category parabolic, sports 300bps; makers $0+rebate), and
   Kalshi NBA maker = quarter-of-taker (charged as FULL taker before). All fee
   logic unified into root `venue_fees.py` (golden tests
   `tests/test_venue_fees_golden.py`); re-rates in
   `runs/fee_model_audit_20260612T075325Z.md` + PROGRAM_STATE correction
   banner (spread taker +0.77→+1.92c; required-n 15.2k→~2.5k; maker fee floor
   ≈0). REMAINING from original scope: one-way-vs-round-trip re-grade of hold
   cells still needs the cache rebuild (queued in the run note).
   (b) **btc_9599_mechanism** — ✅ GRADED 2026-06-12T10:43Z (NOW#5);
   (c) **crypto_noside_hold** — ✅ GRADED (NOW#5); (d) **nba_extreme_fav_hold**
   — ✅ GRADED (NOW#5).
5. **(2026-06-12T10:43Z) Mechanism/broad-hold/extreme-fav wave graded; lone live
   thread (NBA) is K-contested; new K-inheritance hard rule + 4-lane robustness
   wave spooled.** Graded this beat (see DONE): **btc_9599_mechanism =
   NOISE-TO-RETIRE** (jackknife-robust +1.69c but NO mechanism — hr20 is the UTC
   opener not ET terminal hour, neighbors hr18/21 negative, train doesn't favor
   hr20; pre-commit pilot retirement if real fills don't confirm);
   **crypto_noside_hold = NARROW/NO-BROAD-EDGE** (pooled all-hours -0.024c CI
   crosses 0; only 97-98c band survives with a degenerate 0-loss flat +2.00c CI;
   dominant 98-99c negative); **nba_extreme_fav_hold = STRONGEST-CANDIDATE-BUT-
   K-CONTESTED** (memo says TRADEABLE p>=0.85 +5.31c CI [+0.26,+8.37], but K=1 is
   contestable vs the prior 4-threshold sweep + no train reported → treat as
   forward-watchlist, NOT a declared edge; NBA out of season til ~Oct anyway).
   **SELF-OPT (applied):** added the K-inheritance hard rule above. New codex wave
   (4 spooled, backlog floor restored): (a) **nba_fav_hold_kaudit** — honest K=4
   + train re-grade of the NBA candidate (does it survive or is it K-laundering?);
   (b) **crypto_9798_lossexposure** — is the 97-98c +2.00c a zero-realized-loss
   survivorship artifact / payoff tautology?; (c) **btc_pilot_power** — a
   pre-registered confirm/retire decision rule for the BTC pilot from sleeve
   variance (answers btc_9599_mechanism's follow-up); (d) **nba_homefav_curve** —
   NEW continuous (non-threshold) home-fav underpricing expression of the clean
   home-leg substrate. Next interactive-apex action: keep the 3 measure-only
   books running. **If nba_fav_hold_kaudit also kills the NBA thread, the honest
   move per the standing guidance is to SLOW the codex cadence and let the paper
   books run rather than keep mining a near-exhausted backtest surface.**

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
- **Mechanism + broad-hold + extreme-fav wave graded (2026-06-12T10:43Z)** — 3
  memos (the 4th, fee_model_audit, was graded+built into `venue_fees.py` by the
  interactive apex). The lone live thread is K-contested; new K-inheritance rule.
  - `btc_9599_mechanism`: **NOISE-TO-RETIRE** — holdout +1.69c is jackknife-robust
    (drop-one-event LOO +1.65..+1.93c over 16 events) but has NO mechanism: hr20
    is the UTC-day OPENER not the ET terminal hour (max ET hour = 23), no
    weekday/calendar concentration, hr19 carryover explains nothing (~52% side
    match), neighbors incoherent (hr18 -3.79c, hr21 -2.32c), and TRAIN does not
    favor hr20 (+0.74c train event-equal). Keep btc_9599_pilot BTC-only/measure-
    only AND pre-commit retirement if real fills don't confirm. -> btc_pilot_power.
  - `crypto_noside_hold`: **NARROW / NO-BROAD-EDGE** — under corrected one-way fee,
    pooled BTC+ETH all-hours NO-favorite hold = -0.024c, clustered CI
    [-0.569,+0.383] crosses 0 (2212 sig/463 events); BTC -0.060c, ETH +0.872c on
    tiny n. 18/24 hours Bonferroni-positive but 6 losing hours sink the pool. Band
    ladder non-monotone: only 97-98c survives (+2.000c, degenerate flat CI, 0
    losses/97 events — a survivorship flag) while the dominant 98-99c band (1939
    sig) is negative. Prior +0.77c NO-side ref does NOT upgrade to a broad edge.
    -> crypto_9798_lossexposure.
  - `nba_extreme_fav_hold`: **STRONGEST-CANDIDATE-BUT-K-CONTESTED** — memo claims
    TRADEABLE p>=0.85 home-fav hold (43 games, +5.31c one-way, game-clustered CI
    [+0.26,+8.37], jackknife +4.91c dropping 2 winners). BUT K=1 is contestable —
    the same family was swept at 4 thresholds last wave (all four CIs crossed 0,
    train negative) so honest K≈4 — and the re-grade never reports TRAIN. Graded
    as forward-watchlist/pilot-candidate, NOT a declared edge; NBA out of season
    til ~Oct. -> nba_fav_hold_kaudit (honest K=4 + train) + nba_homefav_curve.
  META: 3/4 of the wave resolve negative-or-contested; the one live thread clears
  ONLY under a contestable K=1 framing (the same point-positive/CI-crosses-zero
  failure mode in a K-laundering disguise). Backtest surface near-exhausted;
  forward progress still hinges on the 3 measure-only paper books (all 0 fills).
  SELF-OPT: added the **K-inheritance on re-grades** HARD RULE. Replaced by 4
  robustness lanes: nba_fav_hold_kaudit, crypto_9798_lossexposure, btc_pilot_power,
  nba_homefav_curve. Row in experiments.jsonl.
- **Xasset + broad-hold + new-family wave graded (2026-06-12T05:42Z)** — 4 memos;
  the pilot survivor is boxed in (BTC-only & narrow) and all 4 families resolve
  NEGATIVE. Backtest surface is now near-exhausted.
  - `btc_9599_xasset`: **BTC-ONLY-ARTIFACT** — frozen hr20 sleeve verbatim: BTC
    holdout +1.41c CI [0.88,1.81] (16 events) positive, ETH -3.49c CI [-19.28,
    2.06] (9 events), and hr20 is not even ETH's best hour (ETH best = hr08
    +2.68c). Does NOT generalize cross-asset -> keep the pilot BTC-only, narrow,
    measure-only; do NOT size up.
  - `crypto_favlongshot_hold`: **NO-EDGE** — broad 95-99c favorite hold flat
    -0.18c CI [-1.14,0.63]; ladder non-monotone (only 97-98c CI>0). BTC hr20 in
    fixed-entry test only +0.43c CI [-2.66,2.14]. Survivor is a narrow special
    cell, not a broad-bias tip. Side split NO +0.77c [-0.30,1.60] > YES -1.08c
    -> motivated crypto_noside_hold lane.
  - `wx_forecast_revision`: **DEAD** — revision improves holdout log-loss 0.024
    over forecast-only but market mid still better by 0.165; locked NYC-YES-0.15
    taker fired 14 events (above floor), -0.130 EV, Bonferroni CI [-0.2924,
    -0.0340] entirely below zero (0 winners/14 losers). Market prices the
    revision; another dead weather fair-value family.
  - `nba_fav_hold`: **DEAD-as-tradeable** — early fixed-entry hold point-positive
    at 3/4 thresholds (p=0.85 +4.31c on 43 games) but ALL game-cluster Bonferroni
    CIs cross 0 and train negative at all four; all cleared the 12-game floor
    (power fine, stability the issue). p=0.85 = forward-watchlist only ->
    re-graded properly-powered in nba_extreme_fav_hold lane.
  META: lone survivor confirmed BTC-only + not-a-broad-tip -> correctly held as a
  measure-only narrow pilot. Crypto + weather market mids both well-calibrated
  (taker/fair-value dead); NBA calibration clean-but-untradeable. COST-MODEL
  FLAG: hold-to-settlement memos charged round-trip fee on free-settlement
  positions (canonical desk fee is one-way) -> fee_model_audit lane + owner
  proposal. Replaced by 4 lanes: fee_model_audit, btc_9599_mechanism,
  crypto_noside_hold, nba_extreme_fav_hold. Row in experiments.jsonl.
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
