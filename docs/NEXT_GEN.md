# Next-Gen Improvements — Scoping (2026-06-10)

Where the system stands: structure-complete detection on Kalshi↔Polymarket
(complementary, PM-intra, event dutch YES/NO with guarantee-tiered
exhaustiveness), audited matcher (precision 1.0 corpus + live audit), official
fee models, self-healing shadow run with repo-persisted ledger, CLOB V2
execution adapter + reconciler built, three-gate live lock. ~$10.64 clean
captured in ~9h effective; ~$260 standing held-for-review.

What follows is scoped by expected $/effort, highest first.

---

## 1. WebSocket capture layer (sports transients) — HIGHEST VALUE
The historical analysis showed the bulk of a high week's edge is TRANSIENT
gaps on liquid sports markets (~minutes long, dozens/week). A 20s REST poll
samples these poorly and misses sub-20s flickers entirely.

Build:
- Kalshi WS (`wss://api.elections.kalshi.com/trade-api/ws/v2`,
  `orderbook_delta` channel) + Polymarket CLOB V2 WS (market channel) clients
  with auto-reconnect + sequence-gap resync.
- Local order-book mirrors for the watch-list; on every delta, re-run the
  SAME economics (walk_complementary / walk_basket) — no new math.
- Episode engine reuse; ledger records gain millisecond timestamps.
- Backpressure: only re-price the pair/event whose book changed.

Effort: 2-4 days. Risk: low (additive; REST poll stays as fallback).
Unlocks: the largest known edge class; precise episode durations for the
executability study (can a taker actually hit these?).

## 2. Review console (unlock the standing $260) — CHEAPEST $
The held-for-review pile is the biggest standing number and only an operator
can clear it. Today that means reading raw JSON.

Build: `kalshi-arb review` CLI — walks held-for-review pairs sorted by
standing net, shows both venues' titles + rules side-by-side + the verifier's
reasons, takes y/n/skip, writes the allowlist/denylist. ~0.5 day.
Also: surface the `conflict` tag and `rules_divergent_review` reasons added
by the audit (they tell the operator exactly what to check).

## 3. Polymarket-US as a third venue (the legality unlock)
PM-US is CFTC-regulated and legal for US persons TODAY (unlike PM-global).
Its book is walled off from PM-global, so Kalshi↔PM-US is a genuinely
tradable-today cross-venue pair — and PM-US↔PM-global divergence is itself
informative.

Build: catalog + book fetchers (PM-US API), reuse the matcher verbatim
(same question shapes), venue flag in the economics (fee schedule differs —
needs research), executor adapter later. Effort: 3-5 days research-heavy.

## 4. Implication / containment arbs (new structure class, review-gated)
Cross-EVENT logical relations: "X wins the presidency" ⟹ "X wins the
nomination" (price(presidency) > price(nomination) + costs = arb);
spread/total ladders within the same game; "before March" ⊆ "before June".

Build: deterministic detectors for KNOWN relation templates only
(entity+office containment, date-window containment, numeric-threshold
monotonicity — the threshold machinery exists in verification.py). Every hit
held-for-review initially; promotion via the review console. The economics
need a short-payout variant (sell the implied, buy the implier ≈ buy NO +
buy YES cross-event). Effort: 2-3 days detector + 1 day economics.
Risk: basis-risk-heavy — the gates philosophy (uncertain by default) applies.

## 5. Dutch execution support
The executor places 2-leg comp only. Dutch needs N-leg placement with a
leg-failure unwind policy (fill-or-revert: if leg k fails, immediately sell
back 1..k-1 at bid; cap worst-case unwind cost and require net > that cap
before firing). Effort: 2-3 days + tests. Prereq for ever trading the
dutch/intra structures live.

## 6. Cross-venue event ALIGNMENT for dutch legs
Today a dutch leg only merges the PM ladder when that outcome happens to be
a verified watch-list pair. Matching whole EVENTS (Kalshi event ↔ PM
neg-risk event) would merge every leg. Reuses the pair matcher per-outcome +
a Hungarian-style assignment. Effort: 1-2 days. Value: moderate (deeper
dutch fills).

## 7. Ops hardening (operator items, mostly done waiting on approval)
- EC2 deploy: runbook in deploy/README.md — ~30 operator-minutes, removes
  the container-reclaim single point of failure. SHOULD COME FIRST of all.
- shadow-keepalive workflow: cron only fires from the default branch — needs
  a merge of `.github/workflows/shadow-keepalive.yml` to main (approval
  pending).
- LLM tiebreaker: built, dormant. An ANTHROPIC_API_KEY would let it
  auto-adjudicate the review pile and the implication-arb candidates.
- Live pilot: creds + pUSD onramp + $5 caps; readiness checklist, live lock,
  and reconciler are already built.

---

## Recommended order
1. **EC2 deploy** (operator, 30 min) — everything else compounds on uptime.
2. **WebSocket layer** (2-4 days) — the biggest known edge class.
3. **Review console** (0.5 day) — unlocks the standing $260 immediately.
4. **PM-US venue** (3-5 days) — the tradable-today pair.
5. **Dutch execution + implication detectors** (4-6 days) — breadth.

Expected trajectory if 1-3 land and review clears half the pile: clean
capture moves from ~$1/day standing + episodic spikes to continuous
multi-pair capture with sports transients sampled at tick resolution —
that's the regime where the $15-40k/yr question gets a real empirical
answer instead of an extrapolation.
