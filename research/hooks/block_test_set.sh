#!/usr/bin/env bash
# block_test_set.sh — PreToolUse Bash hook
# Denies Bash commands that touch protected test-set / registry paths unless
# the appropriate unlock env vars are set.
#
# Input:  JSON from stdin (Claude Code PreToolUse payload)
# Output: JSON to stdout  (permissionDecision)
# Exit 0 always (hook ran OK); non-zero would be treated as soft-fail by Claude Code.

set -euo pipefail

# Portable root derivation: this script lives at <root>/research/hooks/, so the
# project root is two directories up from its own location. This keeps fresh
# clones, worktrees, and CI guarded without an absolute operator path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AUDIT_LOG="${PROJECT_ROOT}/market_data/test_unlocks.log"
ERROR_LOG="${HOME}/.claude/hook-errors.log"

# ---------------------------------------------------------------------------
# Helper: emit a PreToolUse JSON response
# ---------------------------------------------------------------------------
allow_response() {
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
}

deny_response() {
    local reason="$1"
    # Use python3 to safely escape the reason string as valid JSON
    local escaped
    escaped=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$reason" 2>/dev/null) || escaped="\"${reason}\""
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}' "$escaped"
}

# ---------------------------------------------------------------------------
# Parse stdin payload
# ---------------------------------------------------------------------------
payload=""
if read -t 5 -r -d '' payload 2>/dev/null || true; then
    :
fi
# Fallback: read normally if the above got nothing (stdin may not be null-terminated)
if [[ -z "$payload" ]]; then
    payload=$(cat)
fi

if [[ -z "$payload" ]]; then
    # No input — pass through
    allow_response
    exit 0
fi

# Extract fields using jq (available) or python3 fallback
parse_field() {
    local field="$1"
    if command -v jq &>/dev/null 2>&1; then
        printf '%s' "$payload" | jq -r "$field // empty" 2>/dev/null || true
    else
        python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    parts = sys.argv[2].lstrip('.').split('.')
    v = d
    for p in parts:
        v = v[p]
    print(v if v is not None else '', end='')
except Exception:
    pass
" "$payload" "$field" 2>/dev/null || true
    fi
}

tool_name=$(parse_field '.tool_name')
command_str=$(parse_field '.tool_input.command')
cwd_str=$(parse_field '.cwd')

# ---------------------------------------------------------------------------
# Guard: only apply to Bash tool
# ---------------------------------------------------------------------------
if [[ "$tool_name" != "Bash" ]]; then
    allow_response
    exit 0
fi

# ---------------------------------------------------------------------------
# Guard: only apply when cwd is inside PROJECT_ROOT
# ---------------------------------------------------------------------------
if [[ -n "$cwd_str" && "$cwd_str" != "${PROJECT_ROOT}"* ]]; then
    allow_response
    exit 0
fi

# ---------------------------------------------------------------------------
# Protected path patterns (regex matched against the full command string)
# ---------------------------------------------------------------------------
PROTECTED_PATTERNS=(
    'market_data/lake_test(/|\b)'
    'market_data/trials\.db\b'
    'market_data/splits\.json\b'
    'market_data/test_unlocks\.log\b'
    'market_data/lake_test/\.expected_sha256\b'
)

# Check whether command matches any protected pattern
matched_pattern=""
for pat in "${PROTECTED_PATTERNS[@]}"; do
    if echo "$command_str" | grep -qE "$pat" 2>/dev/null; then
        matched_pattern="$pat"
        break
    fi
done

# ---------------------------------------------------------------------------
# If no protected path matched — ALLOW
# ---------------------------------------------------------------------------
if [[ -z "$matched_pattern" ]]; then
    allow_response
    exit 0
fi

# ---------------------------------------------------------------------------
# Allowlist: commands that may touch protected paths without unlock
# These are matched against the first substantive token or the full command.
# ---------------------------------------------------------------------------

# Extract first word (command name, skip leading whitespace/env var assignments)
first_word=$(echo "$command_str" | sed 's/^[[:space:]]*//' | awk '{print $1}')

is_allowlisted() {
    local cmd="$command_str"
    local fw="$first_word"

    # 1. run_backtest via any python — both .py path form AND `-m` module form
    if echo "$cmd" | grep -qE '(python[0-9.]?|python3)[[:space:]].*research/agents/tools/run_backtest\.py'; then
        return 0
    fi
    if echo "$cmd" | grep -qE 'kvenv/bin/python3?[[:space:]].*research/agents/tools/run_backtest\.py'; then
        return 0
    fi
    if echo "$cmd" | grep -qE '(python[0-9.]?|python3)[[:space:]]+-m[[:space:]]+research\.agents\.tools\.run_backtest\b'; then
        return 0
    fi
    if echo "$cmd" | grep -qE 'kvenv/bin/python3?[[:space:]]+-m[[:space:]]+research\.agents\.tools\.run_backtest\b'; then
        return 0
    fi

    # 2. sqlite3 read-only queries: .schema or SELECT
    if echo "$cmd" | grep -qE '^[[:space:]]*sqlite3[[:space:]].*market_data/trials\.db[[:space:]]*"(\.|SELECT)'; then
        return 0
    fi
    if echo "$cmd" | grep -qiE '^[[:space:]]*sqlite3[[:space:]].*market_data/trials\.db[[:space:]]*"SELECT'; then
        return 0
    fi

    # 3. python -m research.registry*
    if echo "$cmd" | grep -qE '(python[0-9.]?|python3)[[:space:]]+-m[[:space:]]+research\.registry'; then
        return 0
    fi

    # 4. python -m research.scripts.check_phase1_gates
    if echo "$cmd" | grep -qE '(python[0-9.]?|python3)[[:space:]]+-m[[:space:]]+research\.scripts\.check_phase1_gates'; then
        return 0
    fi

    # 5. python -m research.promotion.review_cli
    if echo "$cmd" | grep -qE '(python[0-9.]?|python3)[[:space:]]+-m[[:space:]]+research\.promotion\.review_cli'; then
        return 0
    fi

    # 6. Bare read-only inspection: ls, cat, wc, head, tail on NON-lake_test paths.
    #    lake_test/ holds the actual held-out parquets; those require the unlock even for reads.
    if echo "$cmd" | grep -qE 'market_data/lake_test(/|\b)'; then
        # lake_test path matched — DO NOT allow bare reads; fall through to unlock/deny
        :
    else
        case "$fw" in
            ls|cat|wc|head|tail) return 0 ;;
        esac
    fi

    return 1
}

if is_allowlisted; then
    allow_response
    exit 0
fi

# ---------------------------------------------------------------------------
# Check unlock env vars
# ---------------------------------------------------------------------------
unlock_test="${RESEARCH_UNLOCK_TEST:-}"
unlock_hash="${RESEARCH_UNLOCK_SPEC_HASH:-}"

if [[ -n "$unlock_test" && -n "$unlock_hash" ]]; then
    # UNLOCKED — append burn entry to audit log
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ")
    hook_token_prefix="${unlock_test:0:8}"
    log_entry="${ts} ${unlock_hash} attempt hook:${hook_token_prefix}"

    # Best-effort append; failures logged to error log, not propagated
    if ! echo "$log_entry" >> "${AUDIT_LOG}" 2>/dev/null; then
        echo "[block_test_set] warn: could not append to ${AUDIT_LOG}" >> "${ERROR_LOG}" 2>/dev/null || true
    fi

    allow_response
    exit 0
fi

# ---------------------------------------------------------------------------
# DENY
# ---------------------------------------------------------------------------
# Extract a clean path description from the matched pattern for the message
path_label=$(echo "$matched_pattern" | sed 's/\\b//g; s/\\//g; s/(\///g; s/)//g; s/[\\()|]//g')
deny_response "hook:block_test_set: command touches protected path '${path_label}' without RESEARCH_UNLOCK_TEST env"
exit 0
