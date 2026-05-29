export const meta = {
  name: 'discover-structural-edges',
  description: 'Claude designs + self-tests cost-robust STRUCTURAL NBA edges under the now-honest engine (zero Codex, no registry writes)',
  phases: [
    { title: 'Design+Test' },
    { title: 'Review' },
    { title: 'Synthesize' },
  ],
}

const ROOT = '/Users/anirudh/Desktop/Projects/kalshi'
const PY = '/Users/anirudh/code/kvenv/bin/python'
const EVAL = `${PY} ${ROOT}/research/scripts/eval_spec.py --spec-file`

const CONTEXT = `
You are a quantitative researcher hunting for a REAL, tradeable NBA in-game
moneyline edge for a Kalshi/Polymarket book. Work from repo root ${ROOT}.

HARD CONTEXT (learned the hard way this session):
- The replay engine was just FIXED: entries now fill at bar i+1, symmetric with
  exits (1 game-minute execution latency). Previously entries got a free 1-bar
  head-start, which manufactured a fake "momentum" edge.
- CONSEQUENCE: the ENTIRE score-run momentum-continuation theme is DEAD under
  honest execution (every variant flips negative). Do NOT propose momentum /
  run-continuation / "ride the hot team" ideas. Do NOT propose anything whose
  edge depends on sub-minute timing or reacting faster than the market — with
  1-bar latency you cannot.
- The bar granularity is 1 game-minute. You decide on bar i, you FILL on bar i+1.
- Realistic Polymarket cost is ~2¢ round-trip. An edge is only real if it stays
  net-positive at 2¢ with >=200 trades. Thin 1¢ edges are worthless.

THEREFORE you must hunt for STRUCTURAL mispricings with a FAT per-trade cushion
(several ¢/contract gross), where the cushion comes from the PRICE LEVEL or a
slow/persistent mispricing — NOT from speed. Canonical examples:
- Deep-deficit mean-reversion: buy a deeply-trailing team while its price is
  depressed (e.g. <=0.15); even a partial comeback moves price a lot off a cheap
  base (buy 0.12 -> 0.18 = +6¢/contract). Cushion comes from the cheap entry.
- Rich-favorite fade: a team priced very high (>=0.88) that the score/clock
  doesn't justify; fade it / buy the cheap underdog. Cushion from selling rich.
- Favorite "calcification": late game, a lead is near-certain by margin vs time
  remaining, but price still lags terminal (e.g. <0.93); buy it to converge to 1.
- Score-vs-price disagreement that PERSISTS over minutes (not a 1-bar reaction).

STRATEGYSPEC SCHEMA (write valid JSON; spec.validate() must pass):
{
  "name": "snake_case_name",
  "description": "one line",
  "features": [subset of: margin, time_remaining, pace_ppm, recent_run_signed,
                lineup_hash, home_stars_on, away_stars_on, kalshi_implied_wp,
                pm_implied_wp, lead_changes_cum],
  "entry_condition": {"expr": "<DSL>", "description": "..."},
  "exit_conditions": [{"expr": "<DSL>", "description": "..."}],  // non-empty
  "sizing": {"mode": "fixed_contracts", "value": 5.0, "max_position_contracts": 100},
  "time_stop_sec": <int <= 7200>,
  "side": "long_home"|"long_away"|"long_trailing"|"long_hot"|"long_cold",
  "venue": "polymarket",
  "cost_assumption_bps": null
}
DSL: only arithmetic / comparison / boolean and functions abs,min,max,sign.
Context vars usable in exprs: home_score, away_score, elapsed_game_sec, margin
(=home-away score), total, pos_side, pos_entry_price, pos_entry_elapsed, pos_size,
minutes_since_entry, unrealized_pnl_bps, PLUS any feature you declare.
ENTRY expr MUST include "pos_side is None". EXIT exprs MUST include
"pos_side is not None". Use unrealized_pnl_bps for TP/SL, e.g.
"pos_side is not None and unrealized_pnl_bps >= 600" (TP) /
"... and unrealized_pnl_bps <= -800" (SL). unrealized_pnl_bps = (mark-entry)/entry*1e4,
so cheap entries produce large bps swings — size TP/SL accordingly.
SIDE semantics: long_trailing longs the team that is behind (by margin);
long_home/long_away are explicit. pm_implied_wp is the HOME win prob (so a
deeply-trailing HOME team has LOW pm_implied_wp; a trailing AWAY team has HIGH
pm_implied_wp — handle both, e.g. "(margin<=-8 and pm_implied_wp<=0.18) or (margin>=8 and pm_implied_wp>=0.82)").
NOTE: kalshi_implied_wp is NaN in ~all cache games (avoid); lead_changes_cum is
per-minute not cumulative (don't threshold it as a running tally).

THE EVALUATOR (your feedback loop — run it yourself; NO registry writes, NO Codex):
  ${EVAL} <path-to-your-spec.json>
It prints JSON with val results at zero/1¢/2¢ cost, the gate at 1¢ and 2¢, the
train/val overfit gap, and a top-level "COST_ROBUST" boolean + "verdict".
A spec is worth surfacing only if it has >=200 trades and is net-positive at 2¢.
`

const SPEC_SUMMARY = {
  type: 'object', additionalProperties: false,
  required: ['family', 'specs', 'best_cost_robust', 'notes'],
  properties: {
    family: { type: 'string' },
    specs: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['spec_path', 'name', 'mechanism', 'n_trades_2c', 'cents_per_contract_2c', 'gate_2c_passed', 'cost_robust'],
        properties: {
          spec_path: { type: 'string' },
          name: { type: 'string' },
          mechanism: { type: 'string', description: 'why this should be a real, slow, level-based mispricing (not speed)' },
          n_trades_2c: { type: 'integer' },
          cents_per_contract_2c: { type: 'number' },
          gate_2c_passed: { type: 'boolean' },
          cost_robust: { type: 'boolean' },
        },
      },
    },
    best_cost_robust: { type: 'boolean', description: 'true if ANY spec in this family is cost-robust' },
    notes: { type: 'string' },
  },
}

const FAMILIES = [
  { slug: 'deep_deficit_reversion', brief: 'Deep-deficit mean-reversion: long the deeply-trailing team when its price is depressed; profit on partial convergence. Cushion from the cheap entry price. Tune the deficit/price thresholds and TP/SL so n>=200 AND it survives 2¢.' },
  { slug: 'rich_favorite_fade', brief: 'Rich-favorite fade: when a team is priced very high (>=0.88) but margin/clock does not justify near-certainty, buy the cheap underdog (long_trailing or explicit). Cushion from the cheap underdog leg.' },
  { slug: 'favorite_calcification', brief: 'Favorite calcification: late game, a lead is near-certain given margin vs time_remaining, but price still lags terminal (<0.93); long the favorite to converge to ~1. Cushion from the price-to-certainty gap.' },
  { slug: 'persistent_score_price_gap', brief: 'Persistent score-vs-price disagreement: the score state strongly favors one side but pm_implied_wp lags for MULTIPLE minutes (a slow repricing, NOT a 1-bar reaction). Long the score-favored side. Be careful this is not just disguised momentum — justify persistence.' },
  { slug: 'open_lane', brief: 'Your single best ORIGINAL structural edge not covered by the other lanes. Must have a fat per-trade cushion, a slow/level-based (non-speed) mechanism, and survive 2¢ with n>=200.' },
]

phase('Design+Test')
const designed = (await parallel(
  FAMILIES.map((f) => () =>
    agent(
      `${CONTEXT}\n\n## YOUR LANE: ${f.slug}\n${f.brief}\n\nDesign 1-2 concrete specs.
Write each to ${ROOT}/research/experiments/claude_design/${f.slug}/<name>.json, run the
evaluator on it, and ITERATE (up to ~4 tries) to get >=200 trades and, ideally,
cost-robustness at 2¢. It is FINE to report that nothing in your lane survives 2¢
— an honest negative is valuable. Return the summary object with the eval numbers
you actually observed. Do not fabricate numbers; only report what the evaluator printed.`,
      { label: `design:${f.slug}`, phase: 'Design+Test', schema: SPEC_SUMMARY, agentType: 'general-purpose' }
    )
  )
)).filter(Boolean)

// Collect cost-robust survivors across all lanes.
const survivors = designed.flatMap((d) =>
  (d.specs || []).filter((s) => s.cost_robust).map((s) => ({ ...s, family: d.family }))
)

phase('Review')
let reviews = []
if (survivors.length > 0) {
  const REVIEW_SCHEMA = {
    type: 'object', additionalProperties: false,
    required: ['spec_path', 'real_edge', 'risk_score', 'rationale'],
    properties: {
      spec_path: { type: 'string' },
      real_edge: { type: 'boolean', description: 'true if this is a plausibly real, non-priced-in, non-artifact edge' },
      risk_score: { type: 'integer', minimum: 1, maximum: 5 },
      rationale: { type: 'string' },
    },
  }
  reviews = (await parallel(
    survivors.map((s) => () =>
      agent(
        `${CONTEXT}\n\n## ADVERSARIAL REVIEW of a cost-robust survivor\nSpec: ${s.spec_path} (${s.name}), family ${s.family}.\nReported: n=${s.n_trades_2c} at 2¢, ${s.cents_per_contract_2c}¢/contract, gate_2c=${s.gate_2c_passed}.\n\nRe-run the evaluator yourself (${EVAL} ${s.spec_path}) to confirm the numbers. Then attack it: is the edge a real structural mispricing or just (a) priced-in / no real alpha, (b) concentrated in a few games, (c) regime/sample-specific, (d) a residual engine artifact? Read research/reports concentration logic if useful. Be skeptical; default to real_edge=false unless the evidence is strong.`,
        { label: `review:${s.name}`, phase: 'Review', schema: REVIEW_SCHEMA, agentType: 'general-purpose' }
      )
    )
  )).filter(Boolean)
}

phase('Synthesize')
const SYN = {
  type: 'object', additionalProperties: false,
  required: ['edge_found', 'survivors', 'verdict', 'next_action', 'codex_scaleup'],
  properties: {
    edge_found: { type: 'boolean' },
    survivors: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string', description: 'honest 3-5 sentence assessment of whether a real structural edge exists under the honest engine' },
    next_action: { type: 'string', description: 'the single highest-value next step' },
    codex_scaleup: { type: 'string', description: 'if a template is worth scaling, which family + what param sweep, and whether Spark (dumb/cheap) or gpt-5.5 (smart/scarce) fits that sweep; else say no scale-up warranted' },
  },
}
const synthesis = await agent(
  `${CONTEXT}\n\nSynthesize the hunt. Design+test results (JSON):\n${JSON.stringify(designed, null, 1)}\n\nAdversarial reviews of cost-robust survivors (JSON):\n${JSON.stringify(reviews, null, 1)}\n\nGive an HONEST verdict: does a real, cost-robust structural edge exist under the now-honest engine? If yes, name the survivor(s) and the next validation step (toward a test-set unlock — which is precious, 5 lifetime). If no, say so plainly and recommend the pivot. For codex_scaleup: a smart model (Claude/gpt-5.5) invents mechanisms; the cheap dumb Spark model is only worth using for MECHANICAL parameter sweeps around an ALREADY-validated template — recommend accordingly.`,
  { label: 'synthesize', phase: 'Synthesize', schema: SYN }
)

return { synthesis, designed, reviews, survivor_count: survivors.length }
