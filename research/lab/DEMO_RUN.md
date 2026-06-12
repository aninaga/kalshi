# Live-agent test of the decision layer (2026-06-09)

A real end-to-end run of the agentic layer on the actual cached NBA data, driven
by live Opus agents — origination → prioritization → execution → feedback, with
**zero hardcoded strategy content**. (Demo registry/ledger live under the
gitignored `research/reports/alpha/demo/`.)

## The loop, as it ran

1. **EDA** (`lab.eda`, real cached train data) — idea-agnostic diagnostics:
   totals anchoring-gap, spread margin-gap, winner calibration-by-price-bucket.
2. **Scout agent** (`scout_prompt.md`) — originated **4 distinct, mechanism-grounded
   hypotheses** and registered them. Crucially it showed analyst judgment:
   - rejected the headline **+13.5 totals anchoring gap as an early-game projection
     artifact** (the naive `total*2880/elapsed` overstates when little time has
     elapsed; late-window mean ≈ 0), keeping only a de-noised late-gated variant;
   - refused to turn high quote **staleness into a timing edge** (banned), using it
     as a freshness *guard* instead.
3. **Director agent** (`director_prompt.md`) — ranked the 4 against the research
   ledger and returned **only `ed9a031d` (spread late-game anchoring)**:
   - **dropped both winner calibration / favorite-longshot ideas** (DEAD family,
     OOS-inverted per `calib_alpha`; also near-duplicates of each other);
   - **down-ranked the totals over** (execution-killed; its late-window / larger-gap
     refinements were already pre-tested in `TOTALS_REFINE` and failed).
4. **Executor agent** — implemented the pick as a `lab.strategy.Strategy`, backtested
   on real cached spread data through `lab.evaluate` (realistic fills + gate +
   walk-forward): **600 trades, +4.24¢@0¢ / +2.24¢@2¢ (CI lo −1.71), gate FAIL on
   season/parity stability, 6/7 WF months positive, edge decays OOS
   (H1 +8.4¢ → H2 +0.06¢)** → verdict **PROMISING** (real-but-uncertified taker
   edge, consistent with the documented spread finding). Recorded to registry + ledger.
5. **Feedback** (`director.incorporate_results`) — folded the verdict into the
   registry; `ed9a031d` → `verdict=PROMISING` (kept open for deepening).

## Bugs the live test caught (both fixed + regression-tested)

- **EDA used the TOTAL-only `anchoring_gap` for all markets** → nonsense
  diagnostics (a "+258 gap" for winner = total points minus a win-prob). Fixed:
  `lab.eda` is now market-aware (totals = pace-of-points, spread = margin
  projection, winner = calibration only).
- **`cover_home`/`cover_away` side labels were in neither `strategy._OVER_SIDES`
  nor `execution._SHORT_SIDES`** → the FillModel filled `cover_home` long while
  settlement settled it short, silently inverting spread P&L. Fixed: `cover_home`
  → `_OVER_SIDES`, `cover_away` → `_SHORT_SIDES`, with a cross-module
  taxonomy-consistency test.

## Takeaway
The layer works as intended with real agents: ideas are *originated* from data
(and artifacts rejected), *prioritized* against prior results (DEAD families
skipped), *tested* on real data through the honest gate, and *fed back*. Nothing
about which strategies got pursued was hardcoded — only the scout→director→feedback
machinery. And running it surfaced two real integration bugs the unit tests
(synthetic + stubbed) had missed.
