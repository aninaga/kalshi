#!/usr/bin/env bash
# check_phase1_gates.sh — Phase 1 startup gate.
#
# Called by research.agents.orchestrator.run BEFORE any codex worker is spawned.
# Four sub-gates; ALL must pass (exit 0). Any failure → exit 1 and the
# orchestrator aborts.
#
#   1. reproducibility            run c_score_run_buy_hot twice; stdout sha256 must match
#   2. overfit_canary_rejected    registry row for deliberate_overfit has scorer_promotion_gate_passed=False
#                                 AND was rejected on at least one substantive (non-DSR-only) reason
#   3. test_set_hash_unchanged    SHA256 of research/cache/*_v1.parquet matches .expected_sha256
#                                 (mirrors research/splits/lock.py:compute_lake_test_sha)
#   4. live_safety_validator      leakage_winprob SPEC.validate() raises ValueError mentioning espn_home_winprob
#
# Exit codes: 0 = all gates pass, 1 = any gate failed or any infra error.

set -uo pipefail

PROJECT_ROOT="/Users/anirudh/Desktop/Projects/kalshi"
PY="${KVENV:-${HOME}/code/kvenv}/bin/python"

cd "${PROJECT_ROOT}"

if [[ ! -x "${PY}" ]]; then
    echo "FATAL: python not found at ${PY} (set \$KVENV or symlink ~/code/kvenv)" >&2
    exit 1
fi

RC=0
gate_result() {
    # gate_result <name> <rc>
    local name="$1" rc="$2"
    if [[ "$rc" -eq 0 ]]; then
        echo "  [PASS] ${name}"
    else
        echo "  [FAIL] ${name}"
        RC=1
    fi
}

TMP_GATE_DIR="$(mktemp -d -t kalshi_gate.XXXXXX)"
trap 'rm -rf "${TMP_GATE_DIR}"' EXIT

echo "==== Phase 1 startup gate ===="
echo "project_root : ${PROJECT_ROOT}"
echo "python       : ${PY}"
echo "tmp          : ${TMP_GATE_DIR}"

# ---------------------------------------------------------------------------
# Gate 1 — reproducibility
# ---------------------------------------------------------------------------
echo ""
echo "Gate 1: reproducibility"
RUN1="${TMP_GATE_DIR}/run1.stdout"
RUN2="${TMP_GATE_DIR}/run2.stdout"
ERR1="${TMP_GATE_DIR}/run1.stderr"
ERR2="${TMP_GATE_DIR}/run2.stderr"

g1_rc=0
"${PY}" -m research.experiments.manual.c_score_run_buy_hot >"${RUN1}" 2>"${ERR1}" || g1_rc=$?
if [[ "$g1_rc" -ne 0 ]]; then
    echo "  run1 exited ${g1_rc}; stderr tail:" >&2
    tail -10 "${ERR1}" | sed 's/^/    /' >&2
else
    "${PY}" -m research.experiments.manual.c_score_run_buy_hot >"${RUN2}" 2>"${ERR2}" || g1_rc=$?
    if [[ "$g1_rc" -ne 0 ]]; then
        echo "  run2 exited ${g1_rc}; stderr tail:" >&2
        tail -10 "${ERR2}" | sed 's/^/    /' >&2
    else
        SHA1=$(shasum -a 256 "${RUN1}" | awk '{print $1}')
        SHA2=$(shasum -a 256 "${RUN2}" | awk '{print $1}')
        if [[ "${SHA1}" != "${SHA2}" ]]; then
            g1_rc=1
            echo "  HASH DRIFT" >&2
            echo "    run1 sha=${SHA1}" >&2
            echo "    run2 sha=${SHA2}" >&2
            echo "  ---- diff (run1 -> run2) ----" >&2
            diff "${RUN1}" "${RUN2}" | head -40 | sed 's/^/    /' >&2
        else
            echo "  sha256=${SHA1:0:16}…  (two runs identical)"
        fi
    fi
fi
gate_result "reproducibility" "$g1_rc"

# ---------------------------------------------------------------------------
# Gate 2 — overfit canary rejected
# ---------------------------------------------------------------------------
echo ""
echo "Gate 2: overfit canary rejected"

export TMP_GATE_DIR  # surface to the Python heredoc

set +e
"${PY}" - <<'PYEOF'
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from research.experiments.manual.deliberate_overfit import SPEC
from research.registry import api as registry_api

DB_PATH = "market_data/trials.db"
SPEC_HASH = SPEC.spec_hash()

def _query_row():
    rows = registry_api.query(spec_hash=SPEC_HASH, limit=1)
    return rows[0] if rows else None

row = _query_row()
if row is None:
    tmp = Path(os.environ.get("TMP_GATE_DIR", "/tmp"))
    tmp.mkdir(parents=True, exist_ok=True)
    spec_path = tmp / "deliberate_overfit.spec.json"
    spec_path.write_text(SPEC.to_json())
    print(f"  registry has no row for deliberate_overfit; "
          f"bootstrapping via run_backtest", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "research.agents.tools.run_backtest",
         "--spec-file", str(spec_path),
         "--split", "val",
         "--agent-id", "manual:deliberate_overfit_gate"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"  run_backtest exited {proc.returncode}", file=sys.stderr)
        print(f"  stdout: {proc.stdout[-400:]}", file=sys.stderr)
        print(f"  stderr: {proc.stderr[-400:]}", file=sys.stderr)
        sys.exit(1)
    row = _query_row()
    if row is None:
        print("  run_backtest succeeded but no row appeared in registry",
              file=sys.stderr)
        sys.exit(1)

if row.scorer_promotion_gate_passed:
    print(f"  GATE BROKEN: deliberate_overfit passed scorer gate "
          f"(trial_id={row.id}, spec_hash={SPEC_HASH[:12]}…)", file=sys.stderr)
    sys.exit(1)

# Pull the reasons JSON to confirm rejection wasn't purely on DSR.
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
res = conn.execute(
    "SELECT scorer_promotion_gate_reasons "
    "FROM trials WHERE id = ?",
    (row.id,),
).fetchone()
conn.close()

reasons_raw = res["scorer_promotion_gate_reasons"] if res else None
reasons = []
if reasons_raw:
    try:
        reasons = json.loads(reasons_raw)
    except json.JSONDecodeError:
        reasons = [reasons_raw]

SUBSTANTIVE_TOKENS = (
    "block_bootstrap",
    "season_split",
    "parity_split",
    "knockout_",
    "single_game_share",
    "single_team_share",
    "n_trades ",
)
substantive = [r for r in reasons if any(tok in r for tok in SUBSTANTIVE_TOKENS)]
if not substantive:
    print(f"  GATE WEAK: deliberate_overfit was rejected ONLY on DSR. "
          f"reasons={reasons!r}", file=sys.stderr)
    sys.exit(1)

print(f"  ok — trial_id={row.id}, rejected on {len(substantive)} "
      f"substantive reason(s)")
print(f"  first: {substantive[0][:100]}")
PYEOF
g2_rc=$?
set -e
gate_result "overfit_canary_rejected" "$g2_rc"
set +e

# ---------------------------------------------------------------------------
# Gate 3 — test-set hash unchanged
# ---------------------------------------------------------------------------
echo ""
echo "Gate 3: test-set hash unchanged"
"${PY}" - <<'PYEOF'
import hashlib
import sys
from pathlib import Path

EXPECTED = Path("market_data/lake_test/.expected_sha256")
if not EXPECTED.exists():
    print("  .expected_sha256 missing; run `python -m research.splits.lock` first",
          file=sys.stderr)
    sys.exit(1)

expected = EXPECTED.read_text().strip()

# Mirror research/splits/lock.py:compute_lake_test_sha.
# We hash the same files the split-locker hashed, in the same order.
cache_files = sorted(Path("research/cache").glob("*_v1.parquet"))
hasher = hashlib.sha256()
for fp in cache_files:
    hasher.update(fp.read_bytes())
actual = hasher.hexdigest()

if actual != expected:
    print(f"  HASH DRIFT", file=sys.stderr)
    print(f"  expected: {expected}", file=sys.stderr)
    print(f"  actual:   {actual}", file=sys.stderr)
    print(f"  files ({len(cache_files)}): {[fp.name for fp in cache_files]}",
          file=sys.stderr)
    sys.exit(1)

print(f"  ok — {len(cache_files)} file(s), sha256={actual[:16]}…")
PYEOF
g3_rc=$?
gate_result "test_set_hash_unchanged" "$g3_rc"

# ---------------------------------------------------------------------------
# Gate 4 — live-safety validator (leakage canary)
# ---------------------------------------------------------------------------
echo ""
echo "Gate 4: live-safety validator"
"${PY}" - <<'PYEOF'
import sys
from research.experiments.manual.leakage_winprob import SPEC

try:
    SPEC.validate()
except ValueError as exc:
    msg = str(exc)
    if "espn_home_winprob" in msg:
        print(f"  ok — validator rejected espn_home_winprob")
        print(f"  reason: {msg[:140]}")
        sys.exit(0)
    print(f"  validator raised but reason does NOT mention espn_home_winprob: "
          f"{msg!r}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:  # noqa: BLE001
    print(f"  validator raised wrong exception type: "
          f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
print("  GATE BROKEN: leakage_winprob SPEC.validate() did NOT raise",
      file=sys.stderr)
sys.exit(1)
PYEOF
g4_rc=$?
gate_result "live_safety_validator" "$g4_rc"

# ---------------------------------------------------------------------------
# Final
# ---------------------------------------------------------------------------
echo ""
if [[ "${RC}" -eq 0 ]]; then
    echo "==== ALL GATES PASSED ===="
else
    echo "==== ONE OR MORE GATES FAILED ===="
fi
exit "${RC}"
