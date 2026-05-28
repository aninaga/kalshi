#!/usr/bin/env bash
# audit_log.sh — PostToolUse Bash hook
# If the executed command contains the substring "run_backtest", appends a
# JSONL row to market_data/audit.log.
#
# Input:  JSON from stdin (Claude Code PostToolUse payload)
# Output: none required (informational hook; stdout/exit do not gate tool result)
# Exit 0 always — best-effort logging must never block downstream.

set -uo pipefail
# Note: no -e; we intentionally swallow all errors to stay best-effort.

PROJECT_ROOT="/Users/anirudh/Desktop/Projects/kalshi"
AUDIT_LOG="${PROJECT_ROOT}/market_data/audit.log"
ERROR_LOG="${HOME}/.claude/hook-errors.log"

# ---------------------------------------------------------------------------
# Read stdin payload
# ---------------------------------------------------------------------------
payload=""
if ! payload=$(cat 2>/dev/null); then
    exit 0
fi

if [[ -z "$payload" ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Helper: parse a JSON field with jq or python3 fallback
# ---------------------------------------------------------------------------
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
        if isinstance(v, dict):
            v = v.get(p)
        else:
            v = None
    if v is not None:
        print(v, end='')
except Exception:
    pass
" "$payload" "$field" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Extract needed fields
# ---------------------------------------------------------------------------
tool_name=$(parse_field '.tool_name')
command_str=$(parse_field '.tool_input.command')
cwd_str=$(parse_field '.cwd')
session_id=$(parse_field '.session_id')
exit_code_raw=$(parse_field '.tool_response.exit_code')

# ---------------------------------------------------------------------------
# Guard: only for Bash tool
# ---------------------------------------------------------------------------
if [[ "$tool_name" != "Bash" ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Guard: only when cwd is inside PROJECT_ROOT
# ---------------------------------------------------------------------------
if [[ -n "$cwd_str" && "$cwd_str" != "${PROJECT_ROOT}"* ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Only log if command contains "run_backtest"
# ---------------------------------------------------------------------------
if [[ "$command_str" != *run_backtest* ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Normalise exit_code — default to null if not an integer
# ---------------------------------------------------------------------------
exit_code="null"
if [[ -n "$exit_code_raw" ]] && [[ "$exit_code_raw" =~ ^-?[0-9]+$ ]]; then
    exit_code="$exit_code_raw"
fi

# ---------------------------------------------------------------------------
# Build JSONL row safely via python3 (handles escaping)
# ---------------------------------------------------------------------------
ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date +"%Y-%m-%dT%H:%M:%SZ")

jsonl_row=$(python3 -c "
import json, sys
row = {
    'ts':         sys.argv[1],
    'session_id': sys.argv[2],
    'command':    sys.argv[3],
    'exit_code':  int(sys.argv[4]) if sys.argv[4] != 'null' else None,
    'cwd':        sys.argv[5],
}
print(json.dumps(row))
" "$ts" "${session_id:-}" "${command_str:-}" "$exit_code" "${cwd_str:-}" 2>/dev/null) || {
    echo "[audit_log] warn: failed to build JSONL row at ${ts}" >> "${ERROR_LOG}" 2>/dev/null || true
    exit 0
}

# ---------------------------------------------------------------------------
# Append to audit log — best-effort
# ---------------------------------------------------------------------------
if ! echo "$jsonl_row" >> "${AUDIT_LOG}" 2>/dev/null; then
    echo "[audit_log] warn: could not append to ${AUDIT_LOG} at ${ts}" >> "${ERROR_LOG}" 2>/dev/null || true
fi

exit 0
