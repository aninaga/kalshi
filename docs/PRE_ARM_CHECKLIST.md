# Pre-arm checklist — execution stack (W1.4)

The execution pilot checklist (audit C1 + ~10 majors) landed 2026-06-12. The
combined tree was adversarially verified by four lenses (order-math/units,
safety-gating, accounting/settlement, failure-modes), all returning
**ready-with-edits**; the five must-fix edits were applied by hand and the
result is green (arb suite 384/384) and passes a 28-check simulated-pilot
acceptance script (`tools/sim_pilot_acceptance.py`).

**Today's safety posture (verified): no real order can fire.** Paper default +
live-lock disarmed + PM SDK absent + empty allowlist, gated at four independent
layers. Every item below FAILS CLOSED. This checklist gates *arming the live
lock*, not merging.

## Landed in W1.4 (with the by-hand edits)
- C1 PM marketable-BUY shares→pUSD conversion (gateway-local; `size` stays shares).
- Whole-contract integer sizing shared across both legs; floor-to-0 aborts.
- Outcome-aware circuit breaker (returned failures open it, don't reset it).
- Timeout black holes: completed legs recorded + hedged; Kalshi poll-timeout re-fetches after cancel.
- Hedge/flatten bypass: risk-reducing orders pass a halt **including at the raw Kalshi client** (cross-lane edit); arming still required.
- Complementary PnL math on BOTH the capture-replay AND live in-process paths (`strategy_type` forwarded).
- `is_resolved` on both real clients; **Gamma probe `&closed=true`** (by-hand blocker fix — without it PM never settles).
- Raw `KalshiOrderClient` mutation gate (lock+kill-switch in `_request`); pem repo-root default removed; DATA_DIR absolute/cwd-independent; readiness/conftest can't clear a real halt.
- Risk gate **abstains** on bookless opportunities (by-hand decision) so the paper desk isn't dark; fails closed on a scoring error.

## MUST close before `kalshi-arb live arm` (residual, fail-closed today)
1. **Risk-gate scoring is unwired.** The gate abstains because nothing attaches `opp['risk_orderbooks']`. Before live, wire normalized books at the scanner/analyzer call site so toxic opps are actually scored (the rescaled engine *can* reject — `test_toxic_opportunity_can_exceed_reject_threshold` — it just never sees books). Until then the desk trades unscored on risk.
2. **Audit/PnL trail on hedged & timeout paths.** `partial_fill_hedged` rows don't carry both leg order-ids/fees into capture, and `tracker_from_capture` skips `skipped_reason` rows — two real venue fills + entry/unwind fees never reach the confirmed-PnL ledger or the daily-loss tracker. Exposure is unwound correctly; the books under-record. (failure-modes major)
3. **Outer-timeout cancelled leg gets no venue-side reconcile**, and the 25s outer budget (EXECUTION_TIMEOUT 10 + FILL_POLL_BUDGET 15) is below one Kalshi leg's worst-case fail-closed recovery. Post-cancel venue reconcile for cancelled legs, or raise the outer timeout above per-leg worst case. (failure-modes)
4. **`_find_recent_fill` misattribution.** An ISO-8601 `match_time` fails `float()` and is KEPT, widening the 120s ambiguity window to all `get_trades` history → a prior opportunity's fill on the same token+side can be double-attributed. Capture t0 before POST, parse ISO timestamps, exclude already-captured trade ids. (failure-modes major)
5. **Raw `PolymarketOrderClient` is ungated** (no lock/kill-switch in the client itself — the exact class fixed for Kalshi). Mitigated today: `py_clob_client_v2` absent from the venv, no key. Gate it **before the PM SDK is ever installed**. (safety-gating)
6. **First-real-PM-BUY unit assertion.** If live CLOB reports `size_matched` in dollars (not shares) for amount-denominated buys, fills under-record by ~price → a spurious Kalshi unwind. Add a first-trade assertion comparing `size_matched` vs `takingAmount`. (order-math-units; unverifiable offline)

## Hygiene / accepted with eyes open
- `websocket_client.py` still has a repo-root `kalshi_private_key.pem` fallback (read-only WS path, not an order vector); `test_no_repo_root_pem_default_in_source` only scans `kalshi_executor.py`.
- `is_resolved` routes a public Gamma read through signed auth — creds-less env → `missing_credentials` → False forever (credential-shaped never-settle); loud-log or fetch unauthenticated.
- PM ambiguity detection is substring-based (`_PM_AMBIGUOUS_MARKERS`) — an exotically-phrased transport error is classified plain-failure and retried (no PM server dedup).
- Kalshi `determined` status is counted resolved (payout imminent, not complete) — drop it if you want payout-complete-only.
- `risk_reducing` is a trust-the-flag boolean — a future caller setting it on an OPENING order dodges kill-switch+breaker (never the lock). Documented NEVER warning in `order_types.py`.

## Operator runbook deltas (behavior changes shipped)
- **DATA_DIR is now repo-anchored absolute.** Docker/cwd deployments must set `KALSHI_ARB_DATA_DIR`.
- **`kalshi_auth_headers` now RAISES** with `KALSHI_API_KEY` set but the key path unset/missing (ws_books backs off forever in that config).
- **DELETE-cancel requires armed.** Operator halt sequence is **halt → flatten → THEN disarm**, never disarm first (disarming blocks the flatten).
- Secrets (user action, still pending): rotate the Polymarket wallet key + Kalshi password; move `.env`/pem out of the iCloud-synced repo tree.

## Acceptance
- `~/code/kvenv/bin/python3 -m pytest tests/ -q` → 384 passed.
- `PYTHONPATH=. ~/code/kvenv/bin/python3 tools/sim_pilot_acceptance.py` → 28/28 (drives the real ArbitrageExecutor→ExecutionEngine→SimulatedGateway: clamp→12 contracts, complementary PnL, forced one-leg failure→hedge, breaker-trip mid-trade→risk-reducing unwind still fills through the halt, live-mode real-order impossibility).
