# crypto hypothesis #2 — favorite-longshot composite (T-15 buy 70-75c / T-5 sell 2-8c)

_Date: 2026-06-10 (UTC). Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Agent: `fable_crypto2`. Family: **crypto**, market: **coin_price**
(provider `research/lab/providers/crypto.py`). Test split never read._

## Provenance (scout wave → adversarial verification → this lane)

1. **Scout wave**: favorite-longshot bias candidate on Kalshi hourly crypto
   threshold ladders (KXBTCD/KXETHD), surfaced from the family's dev panel.
2. **Adversarial verification** (`codex_r2_fav_tail`): memo at
   `/tmp/codex_r2_fav_tail/analyst_memo.md`, preserved verbatim in-repo at
   `runs/fable_crypto2_favtail_verification_memo_codex_r2.md`. Verdict
   **WEAKEN**: the original broad rule (uncapped, no volume filter, ask
   0.65–0.75 / bid 0.02–0.05) is +2.94c net (n=611, 478 events, cluster SE
   1.12c, t=2.62) but timing-fragile and month-unstable at the edges; the
   candidate "survives only as a narrower, more explicit, pre-registered
   rule". The memo's **§Frozen pre-registration spec (taker variant)** is the
   single combined rule the verifier carries forward: expected in-sample
   **+3.24c/contract** (n=587, 492 events, cluster SE 0.93c, t=3.48) on its
   879-event dev panel; survives 2× fee stress (+2.72c).
3. **This lane** (`fable_crypto2`): register EXACTLY the memo's frozen taker
   spec and drive it through the real lane stack (Strategy seam → realistic
   FillModel → promotion gate) on train, then nontest. No tuning. The memo's
   dev panel was the nontest dev sample (train∪val = 879 locked ids), so this
   run is a **confirmatory re-implementation through honest execution + the
   decisive gate**, not fresh out-of-sample; the real test split stays locked.

## Spec resolutions (recorded BEFORE scoring)

The task hand-off described the candidate as "ask 0.65–0.75 / bid 0.02–0.05,
max one per event, closest to 0.70 mid, tie-break tighter spread" while citing
the stat line (+2.94c, n=611, 478 events). Those two halves are inconsistent
in the memo: **+2.94c/n=611 belongs to the UNCAPPED, no-volume-filter original
rule**, while the per-event cap + volume filter belong to the memo's frozen
spec, which uses different bands and selection rules. The lane contract says
spec ambiguity is resolved by the memo; the memo says "Use this exact spec for
the next out-of-sample test" of its §Frozen pre-registration spec. **Resolution:
register the memo's frozen TAKER spec exactly** (the hybrid in the hand-off
text was never tested by the verifier and registering it would be tuning):

* **Universe**: Kalshi BTC/ETH hourly threshold ladders (cache
  `market_data/crypto/_cache`, 1,198 events; all 25,403 cached threshold
  markets verified floor-only "$X or above" — no cap-only legs). Eligible
  contracts have the exact entry-minute candle present, the required side
  quote non-missing, non-crossed (`yes_ask >= yes_bid` when both exist), and
  `volume > 0` in that minute's candle.
* **Clock**: `close_ts` = event date at `hour`:00 ET (America/New_York), ≡
  the memo's `date + (hour+4)h` UTC rule over this Apr–Jun 2026 (all-EDT)
  sample; verified `close_ts == max candle ts` on 1,195/1,198 cached events
  (3 books end 1 min early). Entry snapshots are exact minute candles
  (`ts == close_ts - 900` / `- 300`; candle quotes are minute-close). Missing
  exact-minute row ⇒ that threshold ineligible; no eligible threshold ⇒ leg
  skipped for that event. Conditioning is TIME-TO-CLOSE, never elapsed
  fraction.
* **Favorite leg (T-15)**: `yes_ask` ∈ [0.70, 0.75] ⇒ BUY 1 YES. Selection if
  several qualify: `yes_ask` closest to 0.72, tie-break lower ask (residual
  tie: lower boundary — memo silent; frozen here for determinism). Hold to
  settlement.
* **Longshot leg (T-5)**: `yes_bid` ∈ [0.02, 0.08] ⇒ SELL 1 YES (buy NO).
  Selection: highest `yes_bid`, tie-break lexically by ticker (memo rule).
  Hold to settlement.
* **Cap**: at most one favorite and one longshot trade per event.

Implementation-level resolutions (all frozen pre-scoring):

1. **Two legs under ONE hypothesis id.** The lane `Strategy` fires one trade
   per event, so the legs run as two `Strategy` instances under hypothesis
   `94879b60a8a90098`. The **decisive gate object is the COMBINED trade set**
   (the memo carries forward "the single combined rule"); per-leg gates are
   reported as diagnostics. Declared up front: the favorite leg alone is
   expected to fail the gate's n≥200 floor mechanically (train eligibility
   ≈125 events), so per-leg `passed` flags are not the verdict.
2. **Execution**: lane-standard `FillModel(venue="kalshi",
   half_spread=0.015)`, fill at the threshold's candle-mid + 1.5c + Kalshi
   taker fee, strike PINNED to the signal's selection (`pick_strike`; the
   banded, quote-level-conditioned signal is exactly the snap-flip hazard
   case). The memo's verified rule fills AT the touch (ask/bid + fee = mid +
   true half-spread + fee); measured TRAIN in-band median half-spreads at the
   entry minutes are 1.0c (favorite) and 0.5c (longshot), so the frozen 1.5c
   charge is strictly conservative vs the verified economics (≈ +0.5c/trade
   drag on favorites, ≈ +1.0c on longshots). Gate cost sweep stresses a
   further +1..4c.
3. **Honest i+1 latency** (lane-mandatory, crypto1/xcity precedent): signal
   evaluates the exact T-15/T-5 minute-close candles (the memo's snapshot);
   the fill prices 1 minute later (T-14/T-4) on the pinned strike. The memo
   fills at the snapshot itself; its timing table (T-10 favorite +2.78c vs
   T-15 +5.24c) implies mild attenuation — accepted, not compensated.
4. **Freshness**: the spec's own hygiene (exact-minute candle + required
   quote + non-crossed + volume>0) replaces the lane's panel-mid staleness
   guard (`max_stale_min` set inert at 1e9). The panel-mid staleness is an
   implied-price-movement object that is NOT part of the verified spec and
   would arbitrarily drop eligible snapshots.
5. **Splits**: provider semantics — `train` = 724 locked ids; `nontest` =
   everything not in test = 879 locked train∪val ids **+ 163 ETH events
   cached after the splits lock** (all dated ≤ val_end 2026-06-01, disjoint
   from test; crypto1 precedent: nontest=1041 usable panels). The memo's dev
   panel = the 879 locked ids only; the extra ETH events are genuinely unseen
   by the verifier. Reported as-is; a locked-879-subset slice is reported as
   a reproduction cross-check (diagnostic only, not a second gate attempt).
6. **Settlement**: provider's exact `expiration_value` vs the pinned floor
   strike (payoff 1 iff settle > floor), ≡ the memo's `result == "yes"`
   payout for these floor-only contracts.

## Pre-registration measurements (quotes-only; no outcome joins, no PnL)

* Cache: 1,198 events; splits locked train 724 / val 155 / test 156;
  163 post-lock ETH ids (nontest-eligible, none in the test date window).
* Orientation scan: floor-only 25,403 / cap-only 0 / range 0.
* `close_ts` check: equals last candle minute on 1,195/1,198 events;
  zoneinfo(America/New_York) ≡ +4h rule on every event (all-EDT sample).
* TRAIN eligibility under the frozen rule: favorite 125 events, longshot 371
  events, both-legs 82, expected combined trades 496 (scales with the memo's
  879-panel frozen-spec counts 148/439/587 at the 724/879 ratio ✓).
* TRAIN selected-contract in-band spreads at the entry minute: favorite
  median 2c (p90 ~4.7c), longshot median 1c (p90 ~3c) — matches the memo's
  fill-proxy table (2c / 1c).

## Registration

* Hypothesis id: **`94879b60a8a90098`**, registered 2026-06-10T08:22:55Z and
  claimed by `fable_crypto2` BEFORE any strategy PnL was computed; provenance
  (scout wave → verification memo → this lane) recorded on the registry row.
* Script: `research/scripts/crypto_fav_tail_e2e.py` (frozen params at top;
  pattern: `crypto_late_atm_sell_e2e.py` / `weather_xcity_e2e.py`).

## Gate numbers (exactly as emitted; no post-registration tuning)

Both runs with ledger on (`research/reports/alpha/ledger.jsonl`,
family=crypto, governance read-back n_trials=1 cold-start at run time).
Leg-table counts matched the pre-registration scan exactly
(train 125/371/82-both; ladder_miss=0 on both splits).

### train (`--split train`; 723 usable panels of 724 ids)

| object | n | events | avg fill | avg fee | win | gross | net | bootstrap CI | gate |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| **COMBINED (decisive)** | 496 | 414 | 0.893 | 1.24c | 0.944 | +5.01c | **+2.27c** | [+0.43, +3.95] | **FAIL** |
| favorite T-15 70–75c | 125 | 125 | 0.723 | 1.86c | 0.832 | +10.93c | **+7.56c** | [+0.78, +13.66] | FAIL |
| longshot T-5 2–8c | 371 | 371 | 0.951 | 1.03c | 0.981 | +3.02c | **+0.49c** | [−0.64, +1.45] | FAIL |

* COMBINED fail reasons: `season_split: early_ci_lo=+0.0115,
  late_ci_lo=-0.0182, overlap=False`; `parity_split: even_ci_lo=-0.0085,
  odd_ci_lo=+0.0021, overlap=False`. (Bootstrap CI lo POSITIVE; n≥200 ✓;
  all three knockouts ✓; concentration ✓ top team BTCH10 0.122/0.25.)
* favorite fail reasons: `n_trades 125 < 200` (pre-declared mechanical),
  season_split late −0.0557, parity_split even −0.0451.
* longshot fail reasons: `block_bootstrap_ci_lo −0.0064 <= 0`, season_split,
  parity_split.
* COMBINED walkforward: 2026-04 n=284 +3.23c (fail), 2026-05 n=212 +0.99c
  (fail).
* COMBINED cost sweep (extra cents): 0→+2.27, 1→+1.27, 2→+0.27, 3→−0.73,
  4→−1.73.
* adversarial: staleness ✓ (enforced upstream by the spec's exact-minute +
  volume>0 hygiene), estimator-bias ✗ on the favorite leg (win 0.832 vs paid
  0.723 — for a banded favorite-underpricing hypothesis this gap IS the
  claimed effect, not a snap artifact; combined 0.944 vs 0.893 borderline
  0.050), concentration ✓, oos ✓ combined (H1 +3.56c / H2 +0.99c) but ✗
  longshot (H2 +0.03c).

### nontest (`--split nontest`; 1041 usable panels = 878/879 locked
(`KXBTCD-26APR0222` unbuildable) + all 163 post-lock ETH)

| object | n | events | avg fill | avg fee | win | gross | net | bootstrap CI | gate |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| **COMBINED (decisive)** | 612 | 517 | 0.896 | 1.23c | 0.941 | +4.54c | **+1.81c** | [+0.03, +3.46] | **FAIL** |
| favorite T-15 70–75c | 151 | 151 | 0.724 | 1.85c | 0.821 | +9.75c | **+6.40c** | [+0.32, +12.28] | FAIL |
| longshot T-5 2–8c | 461 | 461 | 0.952 | 1.03c | 0.980 | +2.83c | **+0.30c** | [−0.80, +1.29] | FAIL |

* COMBINED fail reasons: `season_split: early_ci_lo=+0.0059,
  late_ci_lo=-0.0202, overlap=False`; `parity_split: even_ci_lo=-0.0134,
  odd_ci_lo=+0.0001, overlap=False`. (Bootstrap CI lo +0.034 — barely
  positive; knockouts ✓; concentration ✓ top team BTCH20 0.114/0.25.)
* favorite fail reasons: `n_trades 151 < 200`, season_split late −0.0699,
  parity_split even −0.0605.
* longshot fail reasons: `block_bootstrap_ci_lo −0.0080 <= 0`, season_split,
  parity_split; walkforward May NEGATIVE (n=231, −0.51c).
* COMBINED walkforward: 2026-04 n=300 +3.27c; 2026-05 n=309 +0.87c;
  2026-06 n=3 **−47.5c** (the memo's June warning, still n=3 noise — but the
  favorite-leg June slice is n=2 at −71.8c).
* COMBINED cost sweep: 0→+1.81, 1→+0.81, 2→−0.19, 3→−1.19, 4→−2.19.
* adversarial: staleness ✓, estimator-bias ✓ combined (0.941 vs 0.896,
  gap 0.045) / ✗ favorite (0.821 vs 0.724 — the hypothesis's own effect),
  concentration ✓, oos ✗ (H1 +3.10c CIlo +0.99 vs H2 +0.51c CIlo −2.25).

### Reproduction cross-check (diagnostic slice, declared pre-scoring)

Restricting the SAME frozen-rule nontest trades to the memo's dev panel (the
879 locked train∪val ids): **587 trades / 492 events — the memo's
frozen-spec counts EXACTLY** (memo: n=587, 492 events). Net there +1.67c,
CI [−0.13, +3.44], gate fail. The −1.57c gap to the memo's +3.24c is fully
accounted for by the pre-declared harsher lane execution: vs at-the-touch
fills the flat 1.5c half-spread overcharges ≈+0.5c on favorites and ≈+1.0c
on longshots, and the per-order fee ceiling adds ≈+0.5c on favorites
(2c vs memo's 1.39c avg) and ≈+0.74c on longshots (1c vs 0.26c) —
trade-weighted ≈ +1.6c. The implementation IS the verified spec; the lane's
execution discipline is what the memo's numbers could not survive.

## Verdict — **DEAD** (settled in registry + ledger, 2026-06-10T08:30:43Z)

The pre-verified favorite-longshot composite, tested exactly as frozen,
FAILS the decisive gate on BOTH splits. What the gate says, precisely:

1. **The premium is real but execution-sized.** Gross +5.0c/+4.5c
   (train/nontest combined) collapses to +2.3c/+1.8c under lane-honest taker
   execution; the nontest bootstrap CI lo is +0.03c — one extra cent of
   slippage (cost sweep) sends it negative.
2. **The decisive failures are stability, not sign**: season-split late-half
   decay on both splits (Apr +3.3c → May +0.9c → Jun n=3 −47.5c — the memo's
   own pre-flagged failure modes, "T-15 favorite timing decay" and "May-like
   longshot compression", materialized in-gate) and parity-split
   even/odd-cluster disagreement.
3. **The longshot leg (75% of trades) is dead as a taker** at lane costs:
   +0.3–0.5c net, CI spanning zero, May standalone negative. The memo's own
   maker table said this leg's edge lives at the ask (+2.93c conditional).
4. **The favorite leg is the real survivor** — +7.6c/+6.4c net with
   bootstrap CI lo > 0 under harsh execution on both splits — but it is
   structurally n-starved (125/151 trades < 200 floor at ~1 trade per 6
   events) and its late-half decay (May halves April; June n=2
   catastrophic) fails stability on its own.

No post-registration tuning was performed; per-leg numbers above are the
pre-declared diagnostics, not alternate gate attempts.

What survives the death (successor material, NOT scored here):

* The **maker variant** of this exact spec (memo §Maker variant: +3.59c
  conditional on fill, longshot leg +2.93c at the ask) — same signal,
  different execution primitive; needs the family's maker-fill study
  (fill-rate + adverse selection) before it can be a registered hypothesis.
  This is now the SECOND crypto taker death that points at maker execution
  (cf. crypto #1's epitaph).
* The favorite-leg premium (70–75c at T-15) as a standalone candidate once
  the cache holds enough months for n≥200 — provided the Apr→May decay
  reverses with more data rather than confirming a regime artifact.
* A regime question for the scout: combined edge Apr +3.3c vs May +0.9c —
  if the bias is a feature of April's vol regime, longer history should
  show it cycling rather than dying.

## Files

* `research/scripts/crypto_fav_tail_e2e.py` — registered lane script
  (frozen params at top; two Strategy legs under one hypothesis id)
* `research/lab/runs/fable_crypto2_favtail_verification_memo_codex_r2.md` —
  verbatim copy of the verification memo (provenance)
* `research/reports/alpha/hypotheses.jsonl` — register/claim/settle rows for
  `94879b60a8a90098`
* `research/reports/alpha/ledger.jsonl` — settled-trial row #2 of the crypto
  DSR partition
