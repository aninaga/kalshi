# Apex protocol — glossary + self-optimization

## Glossary (one canonical term each)
- **Owner** — the human. Capital decisions, kill criteria, START/PAUSE.
- **Apex** (= "operator", deprecated term) — the resident orchestrator chat
  session (Opus 4.8). Allocates work, grades results, escalates.
- **Judgment** — not a seat: a *class of decision* (promotion to paper,
  test-set unlocks, capital). The apex escalates these to Fable 5 for
  analysis and to the Owner for authorization.
- **Seats** — scout, verifier, analyst, data desk, director (apex-inline for
  now), ops. Staffing floors + dynamic upgrades come from `governor.policy()`.

## Self-optimization loop (the apex experiments on its own configuration)
Every fleet/lane launch is logged to `~/.kalshi_fund/experiments.jsonl`:

    {ts, wave, factor, arms: [{arm, model, effort, lens/brief-id, agents}],
     hypothesis, grading: how outcomes will be scored}

Rules:
1. **Vary ONE factor per wave** (model, brief style, effort, fleet width,
   lens design). Everything else held fixed.
2. **Grade outcomes mechanically where possible**: findings that survive
   adversarial verification per $; kill-rate of verification; memo error
   rate caught by graders; cost per confirmed lead.
3. **Append the outcome row** (`{wave, arm, graded: {...}}`) when verdicts
   land. The pair (config, outcome) is the apex's own trial ledger.
4. **Shift staffing only on accumulated evidence** (≥2 consistent waves),
   never on one wave's noise — same epistemics as the trading gate.
5. Hour-by-hour dynamics come from `governor.policy()` (surplus, demand
   profile, shadow prices); experiments change the *defaults* those
   dynamics modulate.

First experiment: **wave 3 (2026-06-10)** — factor: scout model vendor.
Same 3 crypto lenses run by GPT-5.5 xhigh (codex) and Opus 4.8 (Claude
subagents) in parallel quarantines. Hypothesis: vendor scouts differ in
finding quality per dollar. Grading: blind cross-verification of each pair's
findings + cost from meters.

## First-principles audit rules (2026-06-10)
- **Selection inflation**: scout waves examine K candidates per family;
  registration of a survivor must declare K and apply an alpha/K haircut to
  its gate CI until lab.governance counts scout-examined candidates natively
  (governance patch = owner-approved structural change, queued).
- **Beat scope**: the headless beat runs --dangerously-skip-permissions on a
  trusted personal machine. Boundary unchanged: no structural code self-mods,
  no order APIs, no test splits, no deletes. Revisit the permission model
  BEFORE any real-capital phase.
