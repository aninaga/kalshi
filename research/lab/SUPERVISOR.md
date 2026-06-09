# Supervisor: ≥5 Opus 4.8 agents, always up

Standing policy for running the falsification factory at full tilt. This file is
the durable contract so that **any fresh session can resume the fleet in one
step** — which is the real answer to "keep them up if the container closes" (a
process inside an ephemeral container cannot outlive the container; only git
state + an external trigger persist).

## Invariant
Keep **at least 5** Claude **Opus 4.8 (1M context, max effort)** agents running
at all times. The moment one concludes, spawn a replacement on the next open
lane. The coordinator (the operator chat session) is the supervisor: it launches
agents in the background and respawns on each completion notification.

## Lanes (distinct, to avoid double-work and write races)
1. **spread / deepen** — deepen the one live lead (uncertified-spreads anchoring)
   toward the gate. Mechanism-first; realistic fills.
2. **spread / adversary** — try to KILL lane 1's lead: OOS, cost sweep, cluster
   knockout, staleness. Honest negatives are the deliverable.
3. **total / originate** — originate a fresh, mechanism-grounded hypothesis from
   `lab.eda`, falsify it against the gate.
4. **winner / originate** — same, winner market.
5. **meta / diagnostic + data-quality** — read the accumulating ledger; run
   `lab.audit` (leakage) on any new signals; if the last ≥3 hunts are all DEAD,
   write `research/DIAGNOSTIC_<n>.md` (continue / change / pause). Keep the
   registry pool healthy (director role).

## Rules every agent obeys (non-negotiable)
- **NBA research ONLY.** Never touch `kalshi_arbitrage/` or root `*.py`.
- Read first: `research/lab/OPERATING_MODEL.md`, `README.md`, `CONTRACT.md`,
  `research/ALPHA_FINDINGS.md`, `TOTALS_REFINE_FINDINGS.md`, `SPREADS_FINDINGS.md`.
- Use the substrate: `lab.session`, `lab.signals`, `lab.strategy`,
  `lab.execution` (REALISTIC — never a 0.50 fill), `lab.evaluate` (the gate is
  the judge; never weaken it; pass `ledger_path` for N-aware DSR),
  `lab.hypothesis` (register / **claim** / update), `lab.audit`.
- Honest i+1 entry latency. Hunt the bug on any too-good result.
- **NEVER read the test split** (`train`/`val`/`nontest` only).
- **Single git writer = the coordinator.** Agents WRITE files only; they do NOT
  `git commit`/`git push`. Each writes its verdict to a **tracked** path
  `research/lab/runs/<lane>_<utc-timestamp>.md` (durable — survives a container
  reclaim) and appends trials to the shared ledger
  (`research/reports/alpha/ledger.jsonl`) + registers verdicts. NB:
  `research/reports/` is gitignored (local working state only), so the durable
  record is the markdown under `research/lab/runs/`.

## Deliverable per agent
A verdict — `PROMOTE | PROMISING | NEEDS_DATA | DEAD` — with the gate numbers
(cents/contract, CI, cost sweep, OOS) and the bug-hunt performed, recorded to the
registry/ledger and a durable markdown summary under `research/lab/runs/`.
A `PROMOTE` survivor is surfaced for HUMAN review; capital + the test-split burn
stay human-gated. The factory places no orders.

## Resume (fresh session, after a container reclaim)
1. `git pull` the branch `claude/hedge-fund-strategy-analysis-jyXWB`.
2. Read this file + the latest `research/lab/runs/*` (durable verdicts).
3. Re-launch 5 background Opus agents on the lanes above (skip lanes whose open
   hypotheses are already `running`/claimed).
4. Resume the respawn-on-completion loop.

## Cross-container persistence
Nothing in-container survives a reclaim. For true "always up" across restarts,
an EXTERNAL scheduled trigger must start a fresh session that runs the Resume
steps — e.g. a Claude Code on the web scheduled/triggered session
(https://code.claude.com/docs/en/claude-code-on-the-web). The coordinator keeps
all state in git precisely so that resume is a one-step, idempotent operation.
