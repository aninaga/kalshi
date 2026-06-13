#!/usr/bin/env bash
# test_hooks.sh — Bash-native test suite for block_test_set.sh and audit_log.sh
# No Python venv required; uses jq or python3 for JSON parsing.
# Exit 0 iff all cases pass.
#
# The suite is location-portable: it derives the project root from its own
# location, and it exercises the hook's portable root derivation by copying it
# into a fake clone under a temp dir (Cases 2, 11-13). Unlock-path appends are
# sandboxed into the fake clone so the REAL market_data/test_unlocks.log is
# never written by this suite.

set -uo pipefail

# Derived: this file lives at <root>/research/hooks/tests/.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)"
BLOCK_HOOK="${PROJECT_ROOT}/research/hooks/block_test_set.sh"
AUDIT_HOOK="${PROJECT_ROOT}/research/hooks/audit_log.sh"
UNLOCK_LOG="${PROJECT_ROOT}/market_data/test_unlocks.log"

# audit_log.sh still pins an absolute PROJECT_ROOT; Cases 9-10 must check the
# log that hook actually writes. Read its pinned root (literal double-quoted
# assignment only); fall back to our derived root if absent or not a directory
# (i.e. once audit_log.sh is made portable too).
AUDIT_HOOK_ROOT="$(sed -n 's/^PROJECT_ROOT="\([^"$]*\)"$/\1/p' "$AUDIT_HOOK" | head -1)"
if [[ -z "$AUDIT_HOOK_ROOT" || ! -d "$AUDIT_HOOK_ROOT" ]]; then
    AUDIT_HOOK_ROOT="$PROJECT_ROOT"
fi
AUDIT_LOG="${AUDIT_HOOK_ROOT}/market_data/audit.log"

PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Fake clone: byte-identical copy of the block hook at a different root, to
# prove the hook derives its project root from its own location.
# ---------------------------------------------------------------------------
FAKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hooktest_fakeclone.XXXXXX")"
FAKE_ROOT="$(cd "$FAKE_ROOT" && pwd -P)"
trap 'rm -rf "$FAKE_ROOT"' EXIT
mkdir -p "${FAKE_ROOT}/research/hooks" "${FAKE_ROOT}/market_data"
cp "$BLOCK_HOOK" "${FAKE_ROOT}/research/hooks/block_test_set.sh"
chmod +x "${FAKE_ROOT}/research/hooks/block_test_set.sh"
FAKE_HOOK="${FAKE_ROOT}/research/hooks/block_test_set.sh"
FAKE_UNLOCK_LOG="${FAKE_ROOT}/market_data/test_unlocks.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
get_decision() {
    local json_out="$1"
    if command -v jq &>/dev/null 2>&1; then
        printf '%s' "$json_out" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null || echo ""
    else
        python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d['hookSpecificOutput']['permissionDecision'])
except Exception:
    pass
" "$json_out" 2>/dev/null || echo ""
    fi
}

expect_deny() {
    local json_out="$1"
    local label="$2"
    local decision
    decision=$(get_decision "$json_out")
    if [[ "$decision" == "deny" ]]; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label  (expected deny, got '${decision}', output: ${json_out})"
        FAIL=$((FAIL + 1))
    fi
}

expect_allow() {
    local json_out="$1"
    local label="$2"
    local decision
    decision=$(get_decision "$json_out")
    if [[ "$decision" == "allow" ]]; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label  (expected allow, got '${decision}', output: ${json_out})"
        FAIL=$((FAIL + 1))
    fi
}

# args: <command> [cwd]   (cwd defaults to the derived project root)
make_pretool_payload() {
    local cmd="$1"
    local cwd="${2:-$PROJECT_ROOT}"
    # Safely encode as JSON via python3
    python3 -c "
import json, sys
cmd, cwd = sys.argv[1], sys.argv[2]
print(json.dumps({
    'tool_name': 'Bash',
    'tool_input': {'command': cmd},
    'cwd': cwd,
    'session_id': 'test-session-001',
    'transcript_path': '/tmp/transcript.json'
}))
" "$cmd" "$cwd"
}

# args: <command> [exit_code] [cwd]   (cwd defaults to audit_log.sh's own root)
make_posttool_payload() {
    local cmd="$1"
    local exit_code="${2:-0}"
    local cwd="${3:-$AUDIT_HOOK_ROOT}"
    python3 -c "
import json, sys
cmd = sys.argv[1]
ec = int(sys.argv[2])
cwd = sys.argv[3]
print(json.dumps({
    'tool_name': 'Bash',
    'tool_input': {'command': cmd},
    'cwd': cwd,
    'session_id': 'test-session-001',
    'transcript_path': '/tmp/transcript.json',
    'tool_response': {'exit_code': ec, 'stdout': '', 'stderr': ''}
}))
" "$cmd" "$exit_code" "$cwd"
}

echo "======================================================================"
echo "Running hook test suite (13 cases)"
echo "project root: ${PROJECT_ROOT}"
echo "fake clone:   ${FAKE_ROOT}"
echo "======================================================================"

# ---------------------------------------------------------------------------
# Case 1: cat lake_test path — no unlock → DENY
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "cat market_data/lake_test/2025-10-21_GSW_at_LAL.parquet" \
    | bash "$BLOCK_HOOK")
expect_deny "$out" "Case 1: cat lake_test — no unlock → DENY"

# ---------------------------------------------------------------------------
# Case 2: cat lake_test path — both unlock vars set → ALLOW + log appended
# Runs against the fake-clone copy so the burn entry lands in the sandbox,
# never in the real market_data/test_unlocks.log.
# ---------------------------------------------------------------------------
before_lines=0
if [[ -f "$FAKE_UNLOCK_LOG" ]]; then
    before_lines=$(wc -l < "$FAKE_UNLOCK_LOG" | tr -d ' ')
fi
real_before_lines=0
if [[ -f "$UNLOCK_LOG" ]]; then
    real_before_lines=$(wc -l < "$UNLOCK_LOG" | tr -d ' ')
fi

out=$(make_pretool_payload "cat market_data/lake_test/2025-10-21_GSW_at_LAL.parquet" "$FAKE_ROOT" \
    | RESEARCH_UNLOCK_TEST="mytoken123" RESEARCH_UNLOCK_SPEC_HASH="abc123hash" bash "$FAKE_HOOK")
expect_allow "$out" "Case 2: cat lake_test — both unlocks set → ALLOW"

# Verify the fake clone's log was appended
after_lines=0
if [[ -f "$FAKE_UNLOCK_LOG" ]]; then
    after_lines=$(wc -l < "$FAKE_UNLOCK_LOG" | tr -d ' ')
fi
if [[ "$after_lines" -gt "$before_lines" ]]; then
    echo "PASS: Case 2 (supplemental): unlock log appended at derived root"
    PASS=$((PASS + 1))
else
    echo "FAIL: Case 2 (supplemental): unlock log NOT appended (before=${before_lines}, after=${after_lines})"
    FAIL=$((FAIL + 1))
fi

# Verify the REAL unlock log was untouched
real_after_lines=0
if [[ -f "$UNLOCK_LOG" ]]; then
    real_after_lines=$(wc -l < "$UNLOCK_LOG" | tr -d ' ')
fi
if [[ "$real_after_lines" -eq "$real_before_lines" ]]; then
    echo "PASS: Case 2 (supplemental): REAL unlock log untouched"
    PASS=$((PASS + 1))
else
    echo "FAIL: Case 2 (supplemental): REAL unlock log grew (before=${real_before_lines}, after=${real_after_lines})"
    FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Case 3: python run_backtest.py referencing lake_test → ALLOW (allowlisted)
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "python research/agents/tools/run_backtest.py market_data/lake_test/spec.json" \
    | bash "$BLOCK_HOOK")
expect_allow "$out" "Case 3: python run_backtest.py with lake_test → ALLOW (allowlisted)"

# ---------------------------------------------------------------------------
# Case 4: sqlite3 read-only SELECT on trials.db → ALLOW
# ---------------------------------------------------------------------------
out=$(make_pretool_payload 'sqlite3 market_data/trials.db "SELECT * FROM trials"' \
    | bash "$BLOCK_HOOK")
expect_allow "$out" "Case 4: sqlite3 SELECT trials.db → ALLOW (read-only)"

# ---------------------------------------------------------------------------
# Case 5: rm trials.db — no unlock → DENY
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "rm market_data/trials.db" \
    | bash "$BLOCK_HOOK")
expect_deny "$out" "Case 5: rm trials.db — no unlock → DENY"

# ---------------------------------------------------------------------------
# Case 6: cat splits.json → ALLOW (cat is in read-only allowlist)
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "cat market_data/splits.json" \
    | bash "$BLOCK_HOOK")
expect_allow "$out" "Case 6: cat splits.json → ALLOW (cat is allowlisted)"

# ---------------------------------------------------------------------------
# Case 7: python -m research.registry.api → ALLOW (allowlisted module)
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "python -m research.registry.api --list" \
    | bash "$BLOCK_HOOK")
expect_allow "$out" "Case 7: python -m research.registry.api → ALLOW (allowlisted)"

# ---------------------------------------------------------------------------
# Case 8: cwd outside PROJECT_ROOT → ALLOW (pass-through), even for a command
# that names a protected path.
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "rm market_data/trials.db" "/tmp" \
    | bash "$BLOCK_HOOK")
expect_allow "$out" "Case 8: cwd=/tmp (outside project) → ALLOW (pass-through)"

# ---------------------------------------------------------------------------
# Case 9: PostToolUse audit_log — run_backtest in command → log grows by 1
# ---------------------------------------------------------------------------
before_audit=0
if [[ -f "$AUDIT_LOG" ]]; then
    before_audit=$(wc -l < "$AUDIT_LOG" | tr -d ' ')
fi

make_posttool_payload "python research/agents/tools/run_backtest.py spec.json" 0 \
    | bash "$AUDIT_HOOK"

after_audit=0
if [[ -f "$AUDIT_LOG" ]]; then
    after_audit=$(wc -l < "$AUDIT_LOG" | tr -d ' ')
fi

if [[ "$after_audit" -gt "$before_audit" ]]; then
    echo "PASS: Case 9: audit_log appended for run_backtest command"
    PASS=$((PASS + 1))
else
    echo "FAIL: Case 9: audit_log did NOT grow (before=${before_audit}, after=${after_audit})"
    FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Case 10: PostToolUse audit_log — ls command → log does NOT grow
# ---------------------------------------------------------------------------
before_audit2=0
if [[ -f "$AUDIT_LOG" ]]; then
    before_audit2=$(wc -l < "$AUDIT_LOG" | tr -d ' ')
fi

make_posttool_payload "ls market_data/" 0 \
    | bash "$AUDIT_HOOK"

after_audit2=0
if [[ -f "$AUDIT_LOG" ]]; then
    after_audit2=$(wc -l < "$AUDIT_LOG" | tr -d ' ')
fi

if [[ "$after_audit2" -eq "$before_audit2" ]]; then
    echo "PASS: Case 10: audit_log NOT appended for ls command"
    PASS=$((PASS + 1))
else
    echo "FAIL: Case 10: audit_log incorrectly grew for ls command (before=${before_audit2}, after=${after_audit2})"
    FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Case 11: portable root derivation — fake-clone hook DENIES a protected write
# when cwd is inside the FAKE clone root (proves the root comes from the
# hook's own location, not a hardcoded operator path).
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "rm market_data/trials.db" "$FAKE_ROOT" \
    | bash "$FAKE_HOOK")
expect_deny "$out" "Case 11: fake clone — rm trials.db with cwd inside fake root → DENY"

# ---------------------------------------------------------------------------
# Case 12: portable root derivation — fake-clone hook PASSES THROUGH when cwd
# is outside the fake clone root (the real project root is "outside" for it).
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "rm market_data/trials.db" "$PROJECT_ROOT" \
    | bash "$FAKE_HOOK")
expect_allow "$out" "Case 12: fake clone — cwd outside fake root → ALLOW (pass-through)"

# ---------------------------------------------------------------------------
# Case 13: portable root derivation — cwd in a SUBDIRECTORY of the fake clone
# still counts as inside the root (prefix semantics preserved).
# ---------------------------------------------------------------------------
out=$(make_pretool_payload "cat market_data/lake_test/foo.parquet" "${FAKE_ROOT}/research" \
    | bash "$FAKE_HOOK")
expect_deny "$out" "Case 13: fake clone — cwd in subdir of fake root → DENY"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "======================================================================"
echo "Results: ${PASS} PASS, ${FAIL} FAIL"
echo "======================================================================"

if [[ "$FAIL" -eq 0 ]]; then
    exit 0
else
    exit 1
fi
