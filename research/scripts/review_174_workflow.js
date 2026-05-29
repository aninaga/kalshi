export const meta = {
  name: 'review-174-adversarial',
  description: 'Multi-agent adversarial review of promotion candidate trial id=174 before a test-set unlock',
  phases: [
    { title: 'Panel' },
    { title: 'Verify' },
    { title: 'Synthesize' },
  ],
}

// --------------------------------------------------------------------------
// Shared, stable context embedded in every agent prompt. The volatile,
// quantitative evidence lives in JSON files the agents read themselves.
// --------------------------------------------------------------------------
const EV = 'research/reports/2026-05-28_review_174'
const CONTEXT = `
You are reviewing PROMOTION CANDIDATE trial id=174 in an NBA in-game moneyline
trading research project. This is the LAST automated check before a human decides
whether to burn one of only 5 lifetime test-set unlocks. Treat it as if the
project depends on it. The block-bootstrap-CI-lo>0 gate ALREADY PASSED — do not
re-litigate the gate; find failure modes it could have missed.

CANDIDATE (spec_hash 5208c20ab8…, name halftime_v2_window_wide_v1, MANUAL spec
agent_id=manual:halftime_v2_window_wide_live_pm):
- venue: polymarket ; side: long_hot (BUY the team currently on the scoring run)
- entry: elapsed_game_sec in [1320,1740]  AND  abs(recent_run_signed) >= 6  AND pos flat
  (1320s=22:00 game-elapsed = last ~2 min of Q2; 1740s=29:00 = first ~5 min of Q3)
- exits: TP +200bps, SL -300bps, time_stop 600s
- sizing: fixed 5 contracts ; features: margin, pm_implied_wp, recent_run_signed
- cost: cost_assumption_bps=null → uses active CostProfile (scored under live_pm)

REPRODUCED VAL RESULT (live_pm, matches registry to the cent):
- n=400 trades over 144 games ; pnl_net +$24.26 ; mean/trade +$0.0607
- sharpe 0.237 ; win 58.25% ; gate PASSED ; block-bootstrap CI [+0.0389,+0.0827]
- cluster knockouts all positive ; season & parity CIs overlap ; no game>20%/team>25%
- exit reasons: 372 TP/SL + 28 time-stop
TRAIN RESULT (live_pm): n=2316 over 704 games ; mean/trade +$0.0668 ; sharpe 0.264 ;
  win 58.38%  → essentially NO train/val gap (not an overfit signature).

REGISTRY CONTEXT: ~233 total trials (GROWING — an autoresearch orchestrator is
running right now, adding non-passing trials across many themes; treat N≈233 as a
snapshot); id=174 is STILL the ONLY gate-passing trial ever; 0 promoted; 0/5 test
unlocks burned. The DB is market_data/trials.db (read-only SQLite; you may query
it). 36 trials have reached the n>=200 floor; only id=174 cleared the full gate.
Halftime-THEME cousins (different spec_name, same idea):
id=86 halftime_run_continuation (gate=0, sharpe .114, n=106), id=87 halftime_resume_run
(0,.171,106), id=88 posthalf_run_hot (0,.114,106), id=89 halftime_fade_hot (0,-.498,163),
id=209-214 halftime hot/value/cheap variants (0 trades each). The EXACT name
halftime_v2_window_wide_v1 has 0 failed siblings, so the harness's automated
sibling-counter reports 0 — a blind spot. A human widened the window to [1320,1740]
which quadrupled trades 106→400 vs the ~106-trade cousins, crossing the n>=200 floor.

FEATURE TIMING (from the live-safety registry):
- recent_run_signed: signed 4-game-minute scoring run, source espn_pbp,
  latency_ms=5000, available_at_offset_sec=0, is_live_safe=true, revisable=false.
- margin: espn_pbp, latency_ms=5000, offset 0.
- pm_implied_wp: polymarket_clob, latency_ms=2000, offset 0, revisable=true.

DATA NOTE: val split lists 199 games but 55 produced no trades and MANY 2026-03
games raise FileNotFoundError (no lake data). Effective tradeable val universe = 144
games. The replay engine + fill model live in research/harness/replay.py and
research/harness/realistic_fills.py. Cost profiles in research/harness/cost_profile.py
(live_pm = 0.5¢ half-spread + 0.5% flat + 200bps curve piece).

KEY EVIDENCE HIGHLIGHTS (orienting only — verify against the JSON yourself; do
NOT just accept these, re-derive from the files):
- COST: gate passes at live_pm (1¢ r/t, CI lo +0.040) and +0.5¢ (+0.028), but
  FAILS at +1.0¢ (CI lo +0.014, stability fails) and is -$32 at pessimistic.
  Per-trade net at live_pm ≈ +1.2¢/contract; a +1¢/contract cost error ~halves it.
- THRESHOLDS: broad plateau — almost all 22 perturbations stay positive; run
  threshold shows monotonic dose-response (run>=7 → mean/tr .069, run>=8 → .077).
  NOTE: every threshold variant was costed at live_pm, so the plateau exists only
  at the optimistic cost.
- CONCENTRATION: 144 games, 64.6% profitable, median game +$0.086; top-10 games =
  47.6% of TOTAL net pnl; entry-minute histogram SPIKES at minute 24 (=1440s = Q3
  tip-off after halftime): 99 of 400 entries (25%).
- GATE STABILITY: the parity-split stability check flips pass↔fail depending on the
  bootstrap rng_seed (seed 0 → passes/overlap=true; seed 42 → fails/overlap=false),
  i.e. the gate-pass is marginal on that split.

EVIDENCE FILES (read the one(s) relevant to your section):
- ${EV}/baseline.json      (val/train reproduction, overfit gap, exit-reason histogram)
- ${EV}/cost.json          (cost ladder: zero→live_pm→+0.5/1/1.5/2¢→pessimistic; gate CI lo at each)
- ${EV}/concentration.json (per-game/team PnL dist, top-10 share, entry-minute histogram)
- ${EV}/thresholds.json    (22 perturbations of window/run-threshold/TP/SL/time-stop)
Run from the repo root /Users/anirudh/Desktop/Projects/kalshi. Use cat/python to read JSON.
`

const FINDING_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['section', 'risk_score', 'paragraph', 'key_evidence', 'uncertainty'],
  properties: {
    section: { type: 'string', description: 'Exact section title' },
    risk_score: { type: 'integer', minimum: 1, maximum: 5, description: '1=no concern, 5=disqualifying' },
    paragraph: { type: 'string', description: '3-5 terse, evidence-cited sentences' },
    key_evidence: { type: 'array', items: { type: 'string' }, description: 'Specific numbers/files cited' },
    uncertainty: { type: 'string', description: 'What is missing and what would change the score' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['section', 'original_risk', 'adjusted_risk', 'agreed', 'rationale'],
  properties: {
    section: { type: 'string' },
    original_risk: { type: 'integer', minimum: 1, maximum: 5 },
    adjusted_risk: { type: 'integer', minimum: 1, maximum: 5 },
    agreed: { type: 'boolean' },
    rationale: { type: 'string', description: '2-4 sentences on why the score holds or moves' },
  },
}

// Eight canonical sections (parsed by the promotion CLI) + 2 diligence probes.
const SECTIONS = [
  {
    key: 'Mechanism plausibility',
    attack: `Could you defend to a market-microstructure professor WHY long_hot
(riding, not fading, a post-halftime scoring run) should generalize next season?
The mechanistic writeup claims (a) unpriced locker-room lineup/positional
adjustments and (b) slow PM repricing during the halftime microstructure window.
Is "ride the run" the natural sign, or is momentum-continuation a known-weak/
contrarian claim? Note the 58% win on a +2¢/-3¢ bracket means TP fires more than SL.`,
  },
  {
    key: 'Multiple-testing risk',
    attack: `With 214 trials in the registry and id=174 the ONLY one passing, what
is the chance it passed by luck? It is NOT at the edge of the gate (CI lo +0.039,
not ~0). But fold in the halftime-theme cousins (id=86-89, 209-214) as additional
hidden tests of the same idea. Query trials.db if useful.`,
  },
  {
    key: 'Regime-dependence risk',
    attack: `The result is one season (2025-26). Does the edge plausibly depend on
this season's pace/rules/refereeing, or on structural clock features (halftime
break, Q3 tip)? There is NO 2024-25 holdout available. Weigh how much that
inherent un-testability should cost the score.`,
  },
  {
    key: 'Threshold-selection risk',
    attack: `READ ${EV}/thresholds.json. This is a MANUAL spec — a human chose
window [1320,1740], run>=6, TP200/SL-300, ts600. Is +24/0.237 a broad PLATEAU
(robust) or a SPIKE (curve-fit)? Specifically: do neighboring window bounds,
run-thresholds 5/7, and TP/SL neighbors still pass the gate and keep positive
mean/trade? Flag any sign the baseline sits on a knife-edge or that only the
window that crossed n>=200 happens to pass.`,
  },
  {
    key: 'Cost-model fragility',
    attack: `READ ${EV}/cost.json. Phase -1 showed this family flips from pass to
fail between 2.5¢ and 4¢ cost. Does id=174's gate (CI lo>0) survive a +1¢
round-trip shift (live_pm+1.0c)? At +1.5¢? Where does CI lo cross zero? Compare
the per-trade cushion (mean/trade +$0.0607 on 5 contracts ≈ +1.2¢/contract) to
the cost increments. live_pm is an unvalidated best-guess; how much headroom is there?`,
  },
  {
    key: 'Subtle Leakage paths',
    attack: `recent_run_signed comes from espn_pbp with latency_ms=5000 but
available_at_offset_sec=0. By the time a 6-pt run is visible in PBP (5s late) and
you place a PM order (CLOB latency 2s), would PM have ALREADY repriced the run?
The whole edge rests on PM being slower than ESPN — verify the replay/fill model
in research/harness/replay.py + realistic_fills.py: does it enter at a price that
already reflects the run (no edge) or a stale pre-run price (optimistic/leaky)?
Also check entry-minute histogram in ${EV}/concentration.json for clustering at
clock-stop boundaries (end-Q2 / start-Q3) where the printed price is thin/stale.`,
  },
  {
    key: 'Concentration risk',
    attack: `READ ${EV}/concentration.json. The gate's concentration check passed
(no game>20%, no team>25% of |pnl|), but examine the full distribution: top-10
game share of total/positive PnL, median game PnL, fraction of games profitable,
top teams. Is the +$24 driven by a fat tail of a few games, or broad-based across
144 games?`,
  },
  {
    key: 'Survivorship / sibling-failure risk',
    attack: `The automated sibling-counter keys on EXACT spec_name and reports 0
failed siblings — but the halftime CONTINUATION theme was tried many ways (id=86,
87,88 ~106 trades sharpe .11-.17 failed n>=200; id=89 fade failed; id=209-214 0
trades). Query trials.db. Is id=174 the lucky survivor of a heavily-mined theme,
or a genuinely distinct, better-specified variant (wider window = more signal, not
just more shots)? This compounds the multiple-testing concern.`,
  },
  // ---- diligence probes (folded into canonical sections at synthesis) ----
  {
    key: 'DILIGENCE: Data integrity',
    attack: `val lists 199 games but ~55 have no lake data (FileNotFoundError) and
effective universe is 144 games. Inspect market_data/splits.json and the lake
(market_data/lake or research/lake). Is the missing data RANDOM (date-clustered
2026-03 = a gap in collection) or could missingness correlate with the signal
(biasing the +$24)? Does a 144/199 effective sample undermine the n=400 claim or
the season-half split (if late-season games are the missing ones)?`,
  },
  {
    key: 'DILIGENCE: Bug-fix / engine taint',
    attack: `id=174's result depends entirely on three recently-fixed bugs being
correct: (1) unrealized_pnl_bps now marks-to-market via _current_mark in replay.py
(previously always 0 → TP/SL were dead code); (2) DSR made informational; (3)
cost-model de-pessimized via CostProfile. Also a newly-found footgun: run_batch
under multiprocessing (parallel>1) silently ignores the active CostProfile on
macOS spawn (runs PESSIMISTIC). Verify in replay.py that _current_mark and the
TP/SL exit logic are SIGN-correct for long_hot (a price RISE on the bought side =
positive unrealized_pnl_bps = TP). A sign error here would invert the result.
Confirm id=174 was scored at parallel=1 (the sanctioned run_backtest path) so the
footgun did NOT taint it. Could the 58% win be an artifact of the fill/mark logic
rather than real edge?`,
  },
]

// -------------------------------------------------------------------------- //
// Phase Panel → Verify, pipelined per section (each verifies as soon as its
// panel finding is ready). Synthesis needs ALL sections → barrier via flat().
// -------------------------------------------------------------------------- //
phase('Panel')
const reviewed = await pipeline(
  SECTIONS,
  (s) =>
    agent(
      `${CONTEXT}\n\n## YOUR SECTION: ${s.key}\n${s.attack}\n\nBe adversarial and
specific. Cite exact numbers from the evidence files / trials.db. Output a single
finding object. risk_score: 1=no concern … 5=disqualifying.`,
      { label: `panel:${s.key}`, phase: 'Panel', schema: FINDING_SCHEMA }
    ),
  (finding, s) => {
    if (!finding) return null
    const lean = finding.risk_score <= 2
      ? `The panelist rated this LOW risk (${finding.risk_score}). Play devil's
advocate: find the strongest reason this is MORE dangerous than rated. Re-read the
evidence; if you cannot substantiate a higher score, confirm it.`
      : `The panelist rated this HIGH/MEDIUM risk (${finding.risk_score}). Check
whether it is overblown or addressable with more data; if the concern is solid,
confirm it. Do not soften a real disqualifier.`
    return agent(
      `${CONTEXT}\n\n## SECTION UNDER VERIFICATION: ${s.key}\n` +
        `Panelist paragraph: ${finding.paragraph}\n` +
        `Panelist evidence: ${JSON.stringify(finding.key_evidence)}\n` +
        `Panelist risk_score: ${finding.risk_score}\n\n${lean}\n\n` +
        `Re-read the relevant evidence file(s) yourself before deciding. Output a verdict.`,
      { label: `verify:${s.key}`, phase: 'Verify', schema: VERIFY_SCHEMA }
    ).then((v) => ({ section: s.key, finding, verify: v }))
  }
)

const all = reviewed.filter(Boolean)

// -------------------------------------------------------------------------- //
phase('Synthesize')
const SYNTH_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['sections', 'recommendation', 'justification', 'deferral_evidence_needed', 'bottom_line'],
  properties: {
    sections: {
      type: 'array',
      description: 'EXACTLY the 8 canonical sections in order',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'paragraph', 'risk_score'],
        properties: {
          title: { type: 'string' },
          paragraph: { type: 'string' },
          risk_score: { type: 'integer', minimum: 1, maximum: 5 },
        },
      },
    },
    recommendation: { type: 'string', enum: ['PROMOTE', 'DEFER', 'REJECT'] },
    justification: { type: 'string' },
    deferral_evidence_needed: { type: 'string' },
    bottom_line: { type: 'string', description: '2-3 sentence plain-English verdict for the human' },
  },
}

const synthesis = await agent(
  `${CONTEXT}\n\nYou are the lead reviewer. Below are 10 verified findings (8
canonical sections + 2 diligence probes). FOLD the two DILIGENCE findings into the
most relevant canonical sections (data-integrity → Concentration/Leakage as
appropriate; bug-taint → Mechanism/Leakage). Use each section's VERIFIED
(adjusted) risk_score.

Produce EXACTLY these 8 canonical sections in order:
1. Mechanism plausibility
2. Multiple-testing risk
3. Regime-dependence risk
4. Threshold-selection risk
5. Cost-model fragility
6. Subtle Leakage paths
7. Concentration risk
8. Survivorship / sibling-failure risk

Apply the rubric STRICTLY:
- PROMOTE: no section >= 4, and at most two sections >= 3.
- DEFER: one section 4-5 but addressable with more data; OR three+ sections at 3.
- REJECT: any section = 5; OR two+ sections at 4.
If DEFER, name the specific additional evidence that would flip the verdict.

Findings (JSON):
${JSON.stringify(all.map((r) => ({ section: r.section, risk: r.verify?.adjusted_risk ?? r.finding.risk_score, paragraph: r.finding.paragraph, verify: r.verify?.rationale })), null, 1)}`,
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA }
)

return { synthesis, findings: all }
