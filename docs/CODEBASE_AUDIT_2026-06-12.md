<!-- Provenance: produced 2026-06-12 by a 45-agent multi-model audit (16 subsystem/lens
     surveys -> 27 adversarial verifications, ALL confirmed -> synthesis), run via Claude
     Code workflow wf_4fcfe316-c92. Owner-verified additions: kalshi_private_key.pem and
     .env were confirmed NEVER committed (full-history check) and .gitignore guards hold;
     repo visibility confirmed PUBLIC via gh. Defect IDs C1-C6 + the W1.x workstreams are
     the actionable contract; PROGRAM_STATE.md and the fee audit run-note are companions. -->
# CODEBASE TRUST AUDIT — DRAFT REPORT
Repo: `/Users/anirudh/Desktop/Projects/kalshi` @ main (post-merge, 2026-06-12). 16 subsystem surveys; 27 adversarial verifications (all 27 returned **confirmed**, several with material refinements — see §4).

---

## 1. TRUST SCORECARD

| Subsystem | Verdict | One-line justification |
|---|---|---|
| detection-core (`kalshi_arbitrage/market_analyzer` + clients) | **YELLOW** | Economics path is genuinely correct and fee-canonical (54 targeted tests green), but "lossless completeness" and Kalshi real-time are verified theater, and the richer arb structures live in a duplicate stack (`live_probe.py`). |
| ws-stack (`ws_books`, `websocket_client`, `enhanced_*`) | **RED** | The only deployed WS layer is empirically inert at deployed scale (804-token single-frame subscribe → 2/804 snapshots; 0/207 ledger episodes ever beat the 20s REST sweep); the other two stacks are dead or never-resubscribe-broken. |
| execution-stack (`kalshi_arbitrage/execution/*` + executors) | **RED** | Lock/paper discipline is real and tested, but two verified criticals (PM FOK units, polluted capture ledger) plus ~10 verified majors mean a $5 pilot today would mis-size its PM leg, trip the kill switch on its first hedge, and report wrong P&L from a never-settling ledger. |
| matching-stack (`kalshi_arbitrage/matching/*`) | **YELLOW** | Wired end-to-end, reproducible (0.9925/691), and fail-closed for capital — but the headline is in-sample rubric agreement, and a verified critical lets an up?/down? pair pass as an ALIGNED clean hedge. |
| shadow-ops (`tools/monitor_arb`, ledger, deploy, keepalive) | **RED** | Pricing code is good, but the $648 headline is verified ~80% double-count + basis-risk (only $121 of genuinely closed clean episodes), and the 6h keepalive cron has never executed. |
| lab-core (`research/lab` substrate + `research/scorer`) | **YELLOW** | The statistical gate (block bootstrap, sha256 parity, knockouts, DSR) is correct and tested, but the execution layer carries a verified-critical side-label trap (`long_away`) and cost-free callable exits. |
| lab-agentic (paper harness + apex beat + agent seams) | **YELLOW** | `paper.py` is honest, idempotent, and live (46/46 tests), but the operative research loop bypasses DSR governance (N≈8 vs 30+ real trials) and paper beats are crash-looping on self-inflicted Kalshi 429s. |
| providers-families (weather/crypto/NBA providers, lake) | **YELLOW** | Provider math and the as-of feature store are solid (89 tests), but split governance is broken at every seam: 143 crypto test-window events load by default, the lake's split column is all-NULL, and weather/crypto splits are unprotected. |
| legacy-research (harness/registry/promotion/scripts) | **YELLOW** | Scorer/registry core is solid, but the first real promotion would burn 3 of 5 lifetime unlocks for one test run and execute under the fictional PESSIMISTIC fee profile. |
| nba-data (`research/nba_odds_study` + shim) | **YELLOW** | The data recovery is real and verified (1294/1310 Kalshi shards, depth-28 calibration chain intact), but the study layer still forward-fills hours-stale quotes into every analysis verdict. |
| ops-runtime (`ops/controller`, governor, meters) | **YELLOW** | Genuinely live and conducting (5+ days uptime, vendor-truth meters, working budget gates), but the human-yield logic is structurally defeated, the apex beat can hang forever unmonitored, and there are zero ops tests. |
| glue-config (packaging, docs, hooks, `venue_fees`) | **RED** | Install and `venue_fees` are verified good, but the agent-steering CLAUDE.md is a false map of a deleted repo, importing two orphan tools fires a live API storm, the test-set deny-hook doesn't travel with the repo, and all safety state is cwd-relative. |
| trust-lens (cross-cutting: can headlines lie?) | **RED** | Yes: the arb "capturable net" is restart-inflated hindsight-peak math, and the weather taker cost floor uses a 1.5c constant where the measured crossing cost is 2.0c median / 3.7c mean. |
| dead-map (cross-cutting: dead code) | **YELLOW** | The big deletion already landed (c509940); what remains is ~3,000 lines of receipted-dead modules, fossils, and 36MB of scratch — mechanical to sweep. |
| synergy-map (cross-cutting: convergence) | **YELLOW** | The seams are real, named in code on both sides, and mostly S/M effort — but the flagship pipe (tick capture → research lake) is built, tested, and wired to nothing. |
| scale-lens (cross-cutting: infra) | **RED** | One logged-in MacBook + a never-run GitHub Actions cron, plaintext wallet keys in an iCloud-synced public-repo working copy, zero file locks, and CI covering 311 of 815 tests. |

---

## 2. WHAT PROVIDES VALUE TODAY

Demonstrably working, with evidence:

1. **`venue_fees.py` as canonical fee truth** — both product lines delegate (arb `mock_execution.py:108-141`, research `realistic_fills.py:132-151`, lab `execution.py:101-110`); 33 golden tests pass; no flat-2% remnant on any active math path. The proven convergence template.
2. **`ops/controller.py` is a real, live conductor** — launchd daemon (5+ days uptime), three paper books on cadence, 5-hourly apex beats that grade and commit, budget governor actively gating on vendor-truth meters (currently correctly locking the desk out of Claude at −$2,427 surplus).
3. **`research/lab/paper.py` forward measurement** — frozen pre-registered tenants, idempotent replay, conservative settlement, EV-0 cancel accounting, honest zeros (weather_maker: 0 fills / 5 cancels is itself the most truthful maker evidence in the repo). 46/46 tests.
4. **`live_probe.py` economics** — depth-aware, fee-aware ladder walks for the full structure set (complementary, pm_intra, dutch, ladder, PM-US), unit-tested pure functions, polarity fail-safe (`_MIN_COMPLEMENTARY_SUM=0.80`).
5. **Promotion gate statistics** (`research/scorer/`) — correct cluster bootstrap, deterministic sha256 parity (salted-hash bug verifiably fixed), |sum| knockout fix present, DSR implemented; all gates compose without short-circuiting.
6. **Execution lock discipline** — live lock checked inside both real gateways, paper hardcoded default, arm-phrase file required; verified across multiple verdicts that **no real order can fire today** (paper default + no arm file + no PM SDK + no allowlist).
7. **Matching machinery as plumbing** — verifier wired into both scan paths, headline metrics reproduce exactly, review console → allowlist → live gate flow works mechanically, 415-pair live watchlist with honest clean/uncertain segregation.
8. **NBA data recovery** — Kalshi historical-tier client recovered 30→1294 game shards (440,593 rows, verified live); PM trades→depth-28.26 calibration artifact exists and is consumed; compat shim works including pickle identity.
9. **The as-of feature store** (`providers/feature_store.py`) — structurally leakage-safe joins, provenance-required writes, live hourly forecast-vintage updates.
10. **Packaging** — clean-venv `pip install -e .` yields a fully working 14-subcommand CLI; arb-line CI exists and runs.

---

## 3. CONFIRMED DEFECTS

### CRITICAL (adversarially verified)

**C1. PM FOK/FAK BUY passes shares where the SDK expects dollars — live PM buy legs overspend by 1/price, blowing the $5 clamp up to 20x.**
Where: `polymarket_executor.py:224,250-258` → `venue_gateway.py:179-182`; SDK semantics confirmed in installed py_clob_client (`amount` = $$$ for BUY). Paper (`simulated_gateway.py`) fills shares, so paper validates different semantics than live executes, and `test_polymarket_executor_v2.py:165` pins the bug. Mitigant: PM live leg can't run today (SDK absent, lock disarmed).
Fix: convert shares→pUSD (`size * limit_price`) for marketable BUYs in the gateway, fix `_parse_fill`'s FOK fallback, flip the test assertion.

**C2. Live-capital secrets in plaintext inside an iCloud-synced working copy of a PUBLIC repo.**
Where: `kalshi_private_key.pem` at repo root (wired default at `kalshi_executor.py:37`), `.env` with `POLYMARKET_PRIVATE_KEY` (raw wallet key) plus unclaimed extras the verifier found: `KALSHI_PASSWORD`, `KALSHI_EMAIL`, `POLYMARKET_US_SECRET_KEY`, `POLYGON_API_KEY`. Never committed (gitignore holds), but one `git add -f` from a drainable wallet, and the key has lived in cloud-synced plaintext for weeks.
Fix: **rotate the PM wallet key and Kalshi password now**; move secrets to `~/.config/kalshi/` or Keychain; delete the repo-root pem fallback in favor of a required path env var.

**C3. `long_away` side-label trap manufactures fake edge in the lab substrate.**
Where: `research/lab/execution.py:54` and `strategy.py:50` — two manually-synced frozensets, neither contains `long_away`, which strategy.py's own docstring documents and `strategies/calibration.py:136,216` emit. Verified E2E: fills at p_home, settles as the away bet (up to ~+84c/contract fake edge on longshot fades). Verifier found it worse: `paper.py:83,978` carries an independent copy with the same gap, and agent workers have already emitted the label (`experiments/codex_2d7718ec/spec.json:25`).
Fix: add `long_away` to `_SHORT_SIDES` (and paper.py's copy); make fill/settlement **raise** on unmapped labels; coverage test through the real FillModel.

**C4. Polarity verifier passes "end the year down?" vs "end the year up?" as an ALIGNED clean hedge.**
Where: `matching/verification.py:106-124` (raw-title `.split()`, punctuation makes "down?" invisible) + verifier-found second bug: "down" is in BOTH `_NEGATION_TOKENS` and `_UNDER_TOKENS`, so even on clean titles `double_flip_cancels` returns ALIGNED. The ALIGNED mislabel skips the YES/NO book swap (`market_analyzer.py:1409-1411`) — the exact un-hedged two-leg failure this layer exists to prevent — and the corpus has zero inverted pairs to catch it.
Fix: tokenize clean titles with punctuation stripping AND fix the token double-count; add labeled inverted pairs to the corpus.

**C5. The $648 / 53-episode / 1.63% shadow headline is structurally inflated, and inflation grows with restart count.** (Collapses three confirmed claims: still_open flush double-count, restart re-open, no-dedup summation.)
Where: `tools/monitor_arb.py:309-338,425-432` (episodes drop the `uncertain` flag; every shutdown flushes all open episodes; in-memory `open_eps` re-opens everything on restart) + `tools/analyze_ledger.py:35,67-68,107-111` (sums peak_net over all rows, no dedup, projects $/day from it). Verified decomposition: 42/53 rows are flushes of the SAME 21 markets twice ($261.86 literal double-count); **52 of 53 episodes ($647.17) are in markets the system's own snapshots only ever label basis-risk**; genuinely closed clean episodes: 11 / exactly $121. `peak_net` itself is hindsight best-entry at full depth, zero latency. Episode rows carry no date, no run id, no fee metadata.
Fix: propagate `uncertain` + epoch + run_id into episode rows; analyze_ledger dedups still_open continuations per key and reports clean-closed / standing / basis-risk separately; never project from peak_net.

**C6. Running pytest pollutes the authoritative execution capture ledger with fake exchange-confirmed receipts.**
Where: `tests/test_arbitrage_hedge.py:48-59` (patches mode to live, never patches capture/DATA_DIR) → `market_data/executions/executions.jsonl` now holds 28 fake `confirmation_source="exchange"` rows; `reconcile.py:51` filters on exactly that field — verified replay produces a fabricated "$162.75 locked-in across 28 pairs."
Fix: autouse conftest fixture redirecting capture to tmp_path; quarantine the fake rows; partition capture files by run mode.

### MAJOR — adversarially verified

**Execution stack (the pre-pilot checklist):**
- **PM idempotency is cosmetic** — client_order_id derived but never transmitted; verifier confirmed the SDK salts every order randomly, so no server-side dedup is even possible. Reconcile-before-retry on ambiguous failures; scope the no-duplicate docstring to Kalshi. (`venue_gateway.py:169-182`)
- **Circuit breaker blind to the dominant failure mode** — gateways return error dicts, breaker counts only raised exceptions; verifier found returned failures additionally RESET `consecutive_failures`, masking real ones. (`single_leg.py:72-96`, `circuit_breaker.py:89-108`)
- **Settlement reconciliation can never settle** — `_venue_resolved` needs `client.is_resolved`; neither wired client defines it (only test fakes); failure is silent ("0 newly settled"). (`reconcile.py:131-134`)
- **ConfirmedPnLTracker books complementary trades with sell−buy math** — verified example: true +$8.95 reported as −$76.15 for the only strategy the pilot trades. Branch on `strategy_type`. (`confirmed_pnl.py:84-99`)
- **Readiness CLI and conftest silently clear a real operator halt** — `was_active` captured, never restored; sentinel deleted. Probe in a temp DATA_DIR. (`live_readiness_checklist.py:108-117`, `tests/conftest.py:17-26`)
- **Fractional sizing guarantees a live leg imbalance** — Kalshi truncates to int, PM gets the float (then the C1 units bug compounds it); first pilot trade likely trips the kill switch on a sub-share unwind below PM's $1 minimum. Floor to whole contracts once. (`arbitrage_executor.py:397-404`, `venue_gateway.py:81`)
- **Two unrecorded-fill black holes** — 25s outer `wait_for` vs verified ~106s worst-case leg time cancels mid-flight, discards even completed legs, records nothing; Kalshi poll-timeout cancels then reports filled=0 without re-fetching. Use `asyncio.wait`/shield, record order ids on every timeout, re-fetch after cancel. (`arbitrage_executor.py:126-137`, `venue_gateway.py:115-121`)
- **Hedge unwind is blocked by the very kill switch a failed leg trips**; the bypass (`OperatorControls.flatten_all`) is wired to nothing. Risk-reducing bypass flag + CLI wiring. (`single_leg.py:55-58`, `operator.py`)
- **Risk gate structurally cannot reject** — verifier strengthened the claim: max possible score is 38 vs threshold 60 even WITH orderbooks; confidence floored at exactly the rejection bound; exceptions proceed. (`risk_engine.py:509-615`, `config.py:310`)
- **Ungated raw KalshiOrderClient + working creds on disk** — verifier produced fully signed live order headers locally; any REPL/agent script can POST a real order bypassing the lock. Push the lock check into `_request` for mutating calls. (`kalshi_executor.py:131-187`)

**Detection / WS:**
- **Completeness system is half-fictional** — per-level cache TTLs govern never-written dicts; "BALANCED 99%" is a hardcoded prior minus magic penalties. Delete or report raw counters. (`market_analyzer.py:67-68,2310-2351`)
- **Kalshi real-time in the analyzer is inert** — verified live: WS subscribes the first 100 catalog keys = 100/100 multi-leg parlays the matcher discards; deltas deliberately dropped; every book that matters arrives via REST at ~2/sec, capping scans far below the documented 8-15s. A working snapshot+delta state machine exists in `ws_books.py` — port it. (`api_clients.py:124-127,453-472,571-576`)
- **ws_books PM mirror silently inert at deployed scale** — single-frame 804-token subscribe → snapshots for ~2-6 assets; live-verified that extra frames on one connection are ignored, so the fix is **sharding across connections (≤500 tokens each)**, not chunking. Add a ticks/min DQ gate so an inert mirror can't run silent for months again. (`ws_books.py:163-164`)
- **Old WS stack never resubscribes after any disconnect and permanently stops after 10 attempts** — verifier strengthened: nothing ever re-invokes subscribe anyway (`realtime_manager` is permanently None). Retire it (see §5). (`websocket_client.py:227-245`)

**Matching:**
- **`run_machine.py` wipes operator review approvals on every run** (replaces the whole `approved` array, keeps only `denied`); approved-uncertain pairs re-enter the review queue forever. Merge-by-source. Failure direction is conservative (no capital path). (`run_machine.py:86-90`)
- **The 0.9925 headline is in-sample** — git history is decisive: the corpus commit and verifier rule changes land together (6d25edc, 71dc149 "precision 0.985→0.9925" eight minutes apart); the "HAND-LABELED independent" rubric is an LLM batch prompt restating the verifier's veto list; live audits on 6/09 and 6/10 each found NEW FP classes. Freeze a hook-protected holdout batch; relabel independently.
- **The corpus structurally cannot exercise the rules-text layers or inverted polarity** — kalshi_rules/subtitle empty on all 691 pairs; all 263 true matches aligned, so `min_polarity_accuracy=1.0` is satisfied by a constant-ALIGNED verifier. (Slight overstatement noted in §4.) Recapture with rules text + add inverted pairs.
- **One-sided out-of-gazetteer scope passes CLEAN** — verified probe: "2026 election" vs "2026 Jakarta election" → passed, overlap 1.00, not even uncertain, and `find_live_arb` would auto-allowlist it. Treat one-sided leftover tokens as uncertain minimum. (`verification.py:939-948`, `gazetteer.py:21-58`)

**Docs (verified):** **CLAUDE.md/README describe a deleted 2025 repo** — phantom `debug/`/`demos/` trees and five phantom test/tool files, "no trading functionality" twice vs a full gated executor stack, similarity "55%" vs 0.78, four documented tunables with zero code references, and zero acknowledgment of `research/`/`ops/`. In an agent-driven program this is the steering input. Rewrite to the merged reality.

### MAJOR — survey-receipted (strong receipts, not independently re-verified)

- **Lab execution honesty holes:** callable exits pay zero exit-side spread/fee, get no latency, and accept exit-before-entry (`strategy.py:147-196,228-247`); `_interp_at` bridges quote gaps with FUTURE quotes, locked in by a passing test (`execution.py:57-69`).
- **Promotion endgame traps:** one sanctioned test burn writes **3** "burn" rows (run_backtest + run_batch + review_cli) so the 5-unlock lifetime budget is effectively ~2; the burn executes under the **fictional PESSIMISTIC profile** (review_cli passes no profile; run_backtest's CLI can't even select `calibrated_pm`/`official_2026`); `BatchResult.avg_holding_sec`/`notional_traded` always 0.0 (reads attributes real Trades lack); spread_bps=100 charges 1¢ round-trip in code while every docstring says 2¢.
- **Split governance at the seams:** 1,177 crypto events cached AFTER the split lock load by default, 143 dated inside the locked test window; lake `games.split` is NULL for all 1,310 games so `TestSetAccessError` is permanently inert; weather/crypto `splits.json` are neither git-tracked nor deny-hook-protected; NBA winner/total panels = 0 (merge lost the cache; the running rebuild never queues `--kinds winner`).
- **NBA study layer:** `dataset.py:63` forward-fills odds indefinitely (the 300s staleness bound exists only on the harness path) — hours-stale Kalshi quotes are blended into every sub/player/totals verdict; `schedule.py` silently returns `{}` on sustained ESPN failure.
- **Ops/agentic:** paper open passes crash-looping on Kalshi 429s from 4+ uncoordinated processes spawned the same second, errors mis-reported as "(None)"; the apex/codex memo pipeline bypasses the DSR ledger (governance deflates against N≈8 while 30+ trials live in markdown); apex beat has no timeout and is exempt from the zombie sweep (re-creates the exact day-long-stall failure it was built to prevent); meters fail OPEN (outage reads as 0% used); dashboard codex budget permanently $0 (glob `.log` vs `.jsonl`).
- **Measurement biases (trust-lens):** weather taker half-spread constant 1.5c vs measured 2.0c median / 3.7c mean (73% of samples exceed it — flatters every weather taker verdict); maker fills optimistic at three layers (quote-touch, any-leg-print + summed lows, queue=0 live); per-signal EV CI freezes the fill rate (narrows the decisive interval pro-PASS); staleness guards count sparse bars not wall minutes (gaps to 107 min slip a 2-min guard); bootstrap clusters only by event — same-date cities treated as independent.
- **Glue/infra:** importing `tools/simulate_24h*.py` fires a live 24h scan (module-level `asyncio.run`; actually triggered a 429 storm during the audit; v2 also has an outright-wrong fee formula); `kalshi-arb readiness` is structurally unpassable (0.99 CI bar vs 0.9805 achievable) and burns 4-7 silent CPU-minutes on a 2000x-redundant bootstrap; the test-set deny-hook lives only in untracked absolute-path local settings (`.gitignore` blocks `.claude/`) — fresh clones/worktrees/CI run unguarded; all safety state (arm file, kill switch, allowlist) is cwd-relative (`Config.DATA_DIR="market_data"`) — a halt issued from the wrong cwd does nothing; CI is red on main (pyarrow/matplotlib extras) and runs 311 of 815 tests (zero research coverage); shadow-keepalive has **zero runs ever** and its push-failure path silently discards 5.4h of capture; EC2 runbook crash-loops as shipped (bare `kalshi-arb` off systemd PATH); `/api/enqueue` is unauthenticated shell-exec on the box holding the wallet; the public repo publishes the watchlist and ledger (strategy disclosure) every 6h by design.
- **Synergy defects:** `SnapshotStore` — the designed arb→lake bridge — has zero production callers AND an O(N²) rewrite-whole-parquet append; `tools/polymarket_us_cli.py` is a second, unreferenced live-order door that bypasses LiveTradingLock/KillSwitch entirely.

### MINOR (rollup)

`data_normalizer` zero-value truthiness; duplicated `KALSHI_ORDER_TTL_SECONDS`; PM catalog truncation never surfaced; `dsr_pvalue`/`dsr_p_value` attr mismatch papered by adapter (test fixture uses the wrong name); depth calibration is p50 mislabeled as p25, sampled from capped end-of-game flow; Kalshi real-quote branch still pads fabricated 1000/level depth; `load_panel` bypasses test-split filtering; `hypothesis.claim` race; `_DEFAULT_END='2026-06-08'` hardcoded; governor `_save_est` TOCTOU race (already in launchd.err); review_cli falsy-0.0 / siblings-by-hash / ungated burn paper cuts; five findings docs still publish fictional-2%-fee numbers with no correction banner; PM-US fees live only in a `pmus.py` comment, not `venue_fees`; SUPERVISOR.md resumes onto a dead branch; AUTORESEARCH_V2.md claims deleted infra is "tested"; `capture.py` docstring points at a nonexistent path; monthly walk-forward gate structurally always False (200-trade floor per month).

---

## 4. REFUTED / OVERBLOWN

All 27 verified claims were confirmed at the headline level — **nothing went to zero** — but these specifics did not survive, and these fears can be retired:

**Refuted or corrected in verification:**
- **"Test-generated paper rows can satisfy the readiness paper-run check"** — refuted: the verifier ran it; the 14 test rows FAIL the drift gate (4.18 > 0.25). The real (lesser) issue is the default `since=None` mixing test rows into genuine session stats.
- **"The polarity bug is the raw-title-first choice; clean titles fix it"** — wrong mechanism: clean titles still mislabel via `double_flip_cancels` ("down" sits in both token sets). Two fixes required, not one.
- **"iCloud data plane is critical"** — downgraded to major: alpha ledger and paper book are git-tracked with zero drift, the shadow ledger has off-machine backups, no WAL sidecars present, single-machine use removes the multi-device corruption mode. `trials.db` is the only irreplaceable unbacked artifact (and it's dormant).
- **"Corpus exercises only one verifier layer"** — slightly overstated: comparator-asymmetry and deadline gates DO fire via titles (4x and 45x in the verifier's run); it's the both-sides-rules gates and inverted polarity that are structurally dead.
- **"Nearly every live trade produces a fractional imbalance"** — except when price evenly divides $5 (0.50/0.25/0.20/0.10) or book depth binds. Still a guaranteed eventual trip, just not literally every trade.
- **"Secrets are exposed"** — exposure is *one slip away*, not realized: never committed, gitignore verified holding across full history. (Rotation still warranted — weeks of cloud-synced plaintext.)
- **PM funder env-name mismatch** — real but not the binding blocker (SDK absence is; funder optional for sig_type 0).
- **Kalshi poll-timeout black hole** — narrowed: with TTL=5s pseudo-IOC, the zero-fill path requires ~15s of sustained venue API errors, i.e. an incident window (which is exactly when fill races happen, so it stays major).

**Do-not-worry list (cleared by the surveys with receipts):**
- **Fee canonicalization held.** No flat-2% on any active math path in either line; golden tests pin parity; the stale constants survive only in dead code, unused config, and doc text. The correction did its job.
- **Root executors are NOT duplicates of `execution/`** — verified layering (raw auth clients → gateways → orchestrator); all live.
- **The Kalshi WS delta-drop (B2) is a deliberate, documented correctness fix**, not a bug — it prevents corrupted-book false arbs.
- **The nba_odds_study compat shim works**, including pickle class identity for the 2,070 legacy caches.
- **No live order can fire today through any orchestrated path** — verified at four independent layers (paper default, source-edit + arm-phrase lock checked inside the gateways, PM SDK absent, allowlist absent/fail-closed).
- **The allowlist-wipe defect fails conservative** — wiped approvals make the live gate MORE restrictive; denied entries survive.
- **The scorer core is clean** — salted-hash parity fix verified in code, knockout |sum| fix present, append-only registry assertions real.
- **Paper books are honest** — the 0-fills record is correct measurement, not a bug.
- The "$648" figure reproduces exactly from the ledger but **no document was found quoting it** — it misleads only if quoted as-is going forward.

---

## 5. KILL / CONSOLIDATE LIST

### Safe to DELETE (zero production references, receipted)
| Target | Receipt / note |
|---|---|
| `kalshi_arbitrage/enhanced_websocket_client.py` (523 ln) | 0 imports; guaranteed TypeError in its own reconnect (`:340`); abstract parser never subclassed |
| `kalshi_arbitrage/cache_manager.py` (524 ln) + redis extra + `requirements.txt:13` | 0 imports; revive-blocking redis API bugs (`wait_closed`, positional `zadd`); also the sole base-install import failure |
| `kalshi_arbitrage/monitoring.py` (612 ln) + its test sections | imported only by 2 test files |
| `risk_engine.FeeCalculator` (`risk_engine.py:324-444`) + `tests/test_enhanced_infrastructure.py:247-264` | fictional fee schedule kept green only by its own test; never called in production — the last fee-fiction remnant |
| `market_analyzer` dead methods: `_process_kalshi_batch`, `_get_kalshi_orderbook`, `_get_polymarket_orderbook`, `_get_cached_similarity`, `_calculate_slippage_impact`, `_calculate_profit_margin`, `_extract_kalshi_price` | definition-only on grep; several are dangerous if revived (bypass verifier / freshness gate / stale fees) |
| Analyzer dead caches + `_cleanup_expired_caches` + COMPLETENESS cache-TTL keys | never written (the completeness-theater substrate) |
| Config zero-reference constants: `MAX_CONCURRENT_API_CALLS`, `BATCH_PROCESSING_SIZE`, `STREAM_BUFFER_SIZE`, `STREAM_FRESHNESS_THRESHOLD`, `POLYMARKET_GAS_FEE`, `POLYMARKET_PROTOCOL_FEE`, `POLYMARKET_SLIPPAGE_TOLERANCE` + dup `KALSHI_ORDER_TTL_SECONDS` | fee-fiction trio invites reintroduction |
| `data_normalizer.MarketMatcher` | third, weaker matcher; zero callers |
| `tools/simulate_24h.py` + `simulate_24h_v2.py` | zero references; module-level `asyncio.run` fires live scans on import (observed); v2's fee formula matches neither schedule |
| `tools/backtest_strategies.py`, `tools/audit_candidates.py`, `tools/dry_run_execution.py`, `tools/oracle_label.py` (byte-identical dup — keep the `docs/matching_labeling/` copy) | zero references each |
| `research/agents/orchestrator/` (pycache-only dir) + `__pycache__/foreman*.pyc` + `market_data/orchestrator_state*.json` + root 0-byte `trials.db` | residue of c509940 deletion |
| `research/agents/AUTORESEARCH_V2.md`, `research/lab/SUPERVISOR.md` | both instruct dead infrastructure / a dead branch; archive with deprecation headers pointing at OPERATING_MODEL.md |
| Deprecated worker prompts (`codex_worker_prompt.md`, `gpt55_edge_hunter_prompt.md`, `claude_worker_prompt.md`, `diagnostic_prompt.md`, examples) + `test_prompt_contract.py` | superseded per `analyst_prompt.md:6-9`. **KEEP** `scout_prompt.md`/`director_prompt.md`/`analyst_prompt.md` — runtime-read by live lab code |
| One-shot scripts: `review_probe_174.py`, `review_174_workflow.js`, `verify_entry_latency_174.py`, `rebaseline.py`, `fee_audit_rerate.py`, `discover_structural_edges.js`, `check_parallel_cost.py` (hardcodes `/tmp/spec174.json`) | purposes served, results archived elsewhere |
| `research/experiments/` claude_*/codex_* scratch (≈720 dirs, 36MB, untracked) → tar; `research/reports/` non-ledger residue → archive. **Do not touch** `alpha/hypotheses.jsonl` + `alpha/ledger.jsonl` (load-bearing for DSR N) | |
| Root fossils → `archive/`: `ARBITRAGE_PROFIT_ANALYSIS_REPORT.md`, `ENHANCED_INFRASTRUCTURE.md`, `PRICE_DISCREPANCY_FINAL_REPORT.md`, `price_discrepancies_*.json`, `worklog.md`, `writeup.md`, `analyze_price_discrepancies.py`, `simple_price_check.py` | first files an outside engineer sees |
| `data_backups/shadow/watchdog.sh` (dead `/home/user/kalshi` path), `ledger_lumped_archive.jsonl` | |
| `realistic_fills.POLYMARKET_FLAT_TAKER_RATE`, lab `signals.calibration_gap`, `Panel.df()`, `read_literature.py` (papers/ is empty), `analyst._budget_for` vestige | zero runtime consumers |

### QUARANTINE (not delete)
- **`tools/polymarket_us_cli.py`** — live-order capable, bypasses the entire lock stack; gate through LiveTradingLock or archive until PM-US is a real workstream.
- **`kalshi_arbitrage/snapshot_store.py`** — dead *integration*, not dead code; it is the missing arb→lake bridge (Synergy #1). Fix the O(N²) append, then wire.
- **Legacy fee shims** (`venue_fees.legacy_pm_taker_fee`, lab re-exports) — deliberate memo-reproduction surfaces, golden-tested; do not delete; consolidate to one home.

### CONSOLIDATE (the duplication map)
1. **Two arb detection stacks** — `market_analyzer` four-strategy walks vs `live_probe` pure economics. Converge on live_probe's tested primitives; deletes the second economics implementation and gives the continuous bot dutch/ladder/pm_intra/PM-US for free.
2. **Three WS stacks → one** (`ws_books`, after sharding) — old stack never resubscribes, enhanced stack is dead; dedupe the duplicated Kalshi RSA auth.
3. **Two fill/cost models** — `research/harness/realistic_fills.py` vs `research/lab/execution.py` (+ `MockExecutionEngine` vs `SimulatedGateway`); one shared execution-sim library with one set of golden tests.
4. **Two trial ledgers** — sqlite registry vs lab JSONL; the DSR N split-brain gets worse with every trial (see Synergy #6).
5. **Two Kalshi REST clients** — `_kalshi_fetch.py` vs `api_clients.py`, currently competing for the same per-IP rate limit (the 429 crash-loop is partly self-inflicted).
6. **Triplications:** `_split_filter` (data.py/weather.py/crypto.py — the single most safety-critical predicate), `staleness_min` (3 copies), `_share`/`_concentration_share`, PRE/POST_GAME constants, side-label frozensets (2+ copies incl. paper.py).
7. **1,362 legacy `total-winner*` pkls** unreadable by every current loader — migrate or delete.

---

## 6. SYNERGY BACKLOG (ranked)

| # | Synergy | Seam files | Effort | Why now |
|---|---|---|---|---|
| 1 | **Wire tick capture: ws_books → SnapshotStore → `market_data/live_capture/`** (fix shard-per-day append first; PM sharded ≤500 tokens/conn) | `kalshi_arbitrage/ws_books.py`, `snapshot_store.py`, `tools/monitor_arb.py` ↔ `research/lab/providers/base.py:28` (already names the path) | **S** (+S shard fix) | The flagship merge synergy; ends the "book depth is unrecoverable" gap; every shadow hour currently evaporates |
| 2 | **Shared rate-limited Kalshi client / 429-aware fetch** (token bucket; copy `_kalshi_fetch`'s Retry-After into `live_probe.get`; treat fetch-failure as not-priced, not closed) | `research/lab/providers/_kalshi_fetch.py` ↔ `kalshi_arbitrage/api_clients.py`, `live_probe.py:42-46`, `ops/controller.py` | **S-M** | Fixes the paper-beat crash-loop AND the silent episode corruption at once; prerequisite for more tenants |
| 3 | **Export the matched-pair corpus + episode ledger as a research dataset** (415 labeled pairs incl. uncertainty, 207 episodes; later: settlement joins for divergence ground truth) | `data_backups/shadow/{watchlist,ledger}.jsonl` + `matching/verification.py` ↔ new `research/lab/providers` loader | **S** now, **M** outcomes | First cross-venue research family; measured basis-risk prior for the arb executor |
| 4 | **Real fills / queue model for the maker question** — WS trade prints + displayed depth feeding `mark_book` instead of candle touch-fills | `ws_books.py` ↔ `research/scripts/weather_maker_study.py`, `paper.py:933-935` | **M** | Resolves the program's stated binding uncertainty (0 live fills vs 26% backtest) |
| 5 | **Unify the DSR multiple-testing N** — governance counts registry trials; run_backtest counts lab ledger rows; route apex memos into the ledger | `research/lab/governance.py:222` ↔ `research/registry/api.py` | **S** | Both lines now test the same families; the hurdle is currently too low on both sides |
| 6 | **Paper→micro-live bridge** — lab tenants emit `OrderRequest` through `ExecutionEngine` (simulated gateway first, then $5 behind the lock, per-tenant capture) | `research/lab/paper.py` ↔ `execution/single_leg.py`, `capture.py` | **M** | Real-fill calibration of FillModel/half-spread; **blocked on the execution criticals (C1, C6) landing first** |
| 7 | **venue_fees grows PM-US + maker fees** (Theta=0.05 schedule out of the `pmus.py` comment; maker-side legs priced in live_probe walks) | `venue_fees.py`, `pmus.py`, `live_probe.py` | **S** | Three-venue cost truth under the existing golden-test regime |
| 8 | **Converge detection stacks on live_probe economics** (also port `_MIN_COMPLEMENTARY_SUM` + Gamma top-up into the continuous bot) | `market_analyzer.py` ↔ `live_probe.py` | **M-L** | Biggest divergence-risk reducer |
| 9 | **Arb shadow monitor as an ops/controller tenant** — retire the never-run GH keepalive; one scheduler, one PAUSE path, one dashboard | `ops/controller.py:58-77` ↔ `tools/monitor_arb.py` | **S-M** | Kills the 35-min blind windows and git-as-database |
| 10 | **One shared fill/execution-sim library** (realistic_fills + lab FillModel + per-venue depth calibration from prints, replacing the 28.26 and 1.5c constants) | `research/harness/realistic_fills.py` ↔ `research/lab/execution.py` ↔ `mock_execution.py` | **M** | Makes registry trials, lab books, and arb estimates cost-comparable |
| 11 | **Exact NBA identity matching** — `teams.py` tricodes → deterministic Kalshi ticker + PM slug, bypassing fuzzy matching for NBA | `research/nba_odds_study/teams.py` ↔ `kalshi_arbitrage/matching` | **S** | Free precision in the highest-volume vertical |
| 12 | **Gate the shadow episode stream through the promotion gate** (per-episode capturable-at-open as the metric) | `research/scorer` ↔ ledger export from #3 | **M** | Forces the headline-accounting fixes; gives the arb line the research line's evidentiary standard |

Anti-synergies to respect (from synergy-map, keep as constraints): research must never import `kalshi_arbitrage` (bridge via disk artifacts; the stdlib-only `venue_fees` design is correct); live-captured data must pass an explicit embargo/split policy before lab strategies may train on it; capture stores partitioned by run mode before tenants multiply.

---

## 7. HYPERSCALE ROADMAP

### H1 — Trust + consolidation (this week)

**W1.1 Secrets + perimeter (do first).** Rotate the PM wallet key and Kalshi password; move `.env`/pem out of the repo/iCloud scope; delete the pem default path (`kalshi_executor.py:37`); push the live-lock check into `KalshiOrderClient._request` for mutating calls; quarantine `tools/polymarket_us_cli.py`; add a bearer token to `/api/enqueue`.
*Accept:* no plaintext venue secret under the repo tree; a REPL `place_order` call returns `live_trading_locked`; old keys provably dead.

**W1.2 Ledger + headline honesty.** Quarantine the 56 fake rows in `executions.jsonl`; autouse conftest capture-redirect; run-mode partitioning; monitor episode rows gain `uncertain`/epoch/run_id/pm_fee_bps; `analyze_ledger` dedups still_open continuations and reports clean-closed vs standing vs basis-risk.
*Accept:* full pytest run leaves production ledgers byte-identical; re-running analyze-ledger across a simulated restart produces an identical clean-closed total; the "$648" figure is formally retired and restated (~$121 clean-closed + standing-arb snapshot series).

**W1.3 Matching correctness.** Polarity fix (clean-title tokenization + remove "down" from `_NEGATION_TOKENS`); one-sided out-of-gazetteer → uncertain; `run_machine` merges instead of replacing approvals; add inverted + one-sided-scope pairs to the corpus; freeze one labeler batch as a hook-protected holdout.
*Accept:* down?/up? probe returns INVERTED/rejected; Jakarta probe returns uncertain; review approvals survive a run_machine pass; regression fixtures pin all three.

**W1.4 Execution pilot checklist (pre-requisite for any micro-live).** Land the ten verified majors + C1: PM units conversion, integer sizing, outcome-aware breaker, halt-preserving readiness/conftest, `is_resolved` on both clients, strategy-aware tracker PnL, hedge bypass for risk-reducing orders + OperatorControls CLI wiring, timeout harvesting (record order ids, re-fetch after cancel), risk-gate rescale or removal, absolute DATA_DIR anchoring.
*Accept:* each item has a regression test; a full simulated pilot run reconciles end-to-end (locked-in → settled) with correct complementary math.

**W1.5 Lab substrate honesty.** `long_away` → `_SHORT_SIDES` (+ paper.py copy) + fail-loud on unknown labels; callable exits charge exit-side spread/fee with latency lock and `exit_bar > entry_bar`; `_interp_at` → true ffill; de-triplicate `_split_filter`; extend the deny-hook to weather/crypto `splits.json` and git-track them; add the cache⊆splits invariant; queue the winner-kind prefetch.
*Accept:* real-FillModel `long_away` test asserts `1−p_home`; an early-exit fixture's pnl drops by exactly the exit costs; crypto `split=None` load excludes post-lock test-window events; `available('winner') > 0`.

**W1.6 Docs, hooks, CI.** Rewrite CLAUDE.md/README to the merged reality (both lines, real tree, true execution model); commit hook wiring as tracked `.claude/settings.json` with repo-relative paths (narrow `.gitignore:157`); fix red CI (extras) and add a research-suite job; fix the promotion triple-burn (one burn row per sanctioned run, dedup by spec_hash) and make `calibrated_pm`/`official_2026` selectable + default for burns; guard/delete `simulate_24h*`.
*Accept:* fresh clone has the deny-hook active; CI green and covering both lines; a dry-run burn writes exactly one row under the canonical profile; importing any module under `tools/` is side-effect-free.

**W1.7 Kill-list sweep** (§5 deletes + archive/). *Accept:* greps for deleted symbols return nothing; targeted suites stay green; repo root contains no 2025 fossils.

### H2 — The convergence build (~2-4 weeks)

**W2.1 Shared data spine.** Fix SnapshotStore sharding; shard the PM WS subscription (≤500/conn); arm Kalshi WS with creds on the always-on box; wire mirrors → `market_data/live_capture/`; add the ticks/min DQ gate (assert recorded episodes beat sweep cadence when "WS feed started" is logged); land a `live_capture` provider with an explicit embargo/split policy before any strategy may train on it.
*Accept:* episodes exceeding 3 ticks/min appear in the ledger within 48h of deploy; the lake gains cross-venue book-depth tables; split-policy test blocks un-embargoed capture from `load_panels`.

**W2.2 Micro-live calibration.** Run the $5/one-contract pilot through the fixed execution stack (real allowlist via the review console, per-tenant capture); feed realized fills back into FillModel half-spread (replace the 1.5c constant with measured per-minute `half_spread` already exported by the weather provider) and a queue-position model for maker fills; re-state the forecast_gap_maker verdict under measured costs.
*Accept:* ≥N pilot trades with estimate-vs-realized drift within a pre-registered band; weather taker evaluations re-rated under measured spreads; maker fill-rate divergence test (±8pt) evaluated against real prints, not candles.

**W2.3 One detection stack, one matcher.** market_analyzer consumes live_probe primitives (deletes the second economics implementation, gains dutch/ladder/pm_intra/PM-US in the continuous bot); allowlist gains merge semantics + audit trail; matching gate re-baselined on the frozen holdout with the bootstrap memoized (verify-once, resample outcomes — kills the 4-minute readiness stall).
*Accept:* one walk-economics implementation repo-wide; readiness completes <30s; gate reports in-sample and holdout precision separately.

**W2.4 Governance unification.** Single DSR N across registry + lab ledger (+ apex memo verdicts written to the ledger); family-partitioned hurdles incl. an `arb` family; alpha/K haircut codified (thread K → CI level in `evaluate()`); vectorize the bootstrap (per-group sums; ~100x) before fleet-scale evaluation.
*Accept:* `governance.trial_count` ≥ true graded-trial count; evaluate() throughput supports thousands of gate runs/day.

**W2.5 Operations consolidation.** Shadow monitor + paper beats as controller tenants on the always-on box (retire shadow-keepalive); shared rate-limited Kalshi client across all consumers; apex beat gets a timeout + zombie-sweep coverage; meters fail closed; basic ops tests (governor math, feeder race, backlog locking via flock).
*Accept:* zero 429 crash-loops over a week; controller restart loses no backlog items under concurrent apex appends; a hung beat is killed and the next beat fires.

### H3 — The scaled operation

**W3.1 Always-on box, real secrets, state out of iCloud/git.** Deploy the already-written `deploy/` stack (fixed: venv on unit PATH, `--snapshot-every`) to a small instance; venue creds via instance secret store; mutable state to `/var/lib/kalshi` (laptop: `~/.kalshi_fund/data`); nightly snapshots to S3 replace git data commits; decide repo visibility deliberately (private, or stop publishing watchlist/ledger).
*Accept:* laptop can be closed for 72h with zero capture gaps; `git log` on main contains no data commits; no strategy artifacts world-readable.

**W3.2 Real database + ledger discipline.** Registry gains migration tooling (replace schema-equality hard-fail); episodes/trials/books move to one store with run ids and fee-model stamps (SQLite-WAL single-writer is fine until a second writer box exists — then Postgres); compaction/indexing for the JSONL replay paths.
*Accept:* any historical row is re-ratable after a fee change by join, not archaeology; analyze/status endpoints are O(window), not O(history).

**W3.3 Multi-venue scale-out.** PM-US as a first-class family (venue_fees entry → capture → provider → paper tenant), using the catalog-row-mapping template `pmus.py` proved; per-venue depth-calibration artifacts from executed prints (replacing the global 28.26); venue-generic tick tables beside the NBA lake (do not extend the per-game-parquet pattern to hourly families).
*Accept:* adding venue N+1 touches one fee entry, one catalog mapper, one provider — no new matcher, no new economics.

**W3.4 Fleet-grade evaluation + control.** Locked hypothesis registry (flock), authenticated control plane, dynamic controller tenancy (no source-edit + restart per tenant), CI as the merge gate for both lines including the matching holdout gate and the ticks/min DQ gate.
*Accept:* a second operator (or agent fleet) can stand up the desk from a fresh clone with hooks, gates, and budgets intact — the current bus-factor-1 receipts (hardcoded `/Users/anirudh` paths, Keychain-only metering) eliminated.

---

*Cross-cutting note for the owner: the merge is in better shape than its documentation in both directions — the math cores (fees, bootstrap, walks, as-of joins) verify clean, while the marquee narratives (lossless completeness, Phase-2 real-time, $648 capturable, 0.9925 precision, "N-aware governance", "24/7 shadow") each overstate what the code does. H1 is mostly about making the narratives true or retiring them; the verified defect list above is the complete receipt set for that work.*