#!/usr/bin/env bash
# Wrapper for the 24/7 SHADOW run — captures REAL books (with real depth) every
# --interval seconds and records each fee-aware gap episode to a JSONL ledger.
# No capital, no orders: `monitor` without --execute never touches the executor.
# All knobs come from the .env (EnvironmentFile in the systemd unit) so the unit
# file stays static.
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root (/opt/kalshi)
export PYTHONUNBUFFERED=1

LEDGER="${SHADOW_LEDGER:-market_data/shadow/ledger.jsonl}"
ALLOWLIST="${SHADOW_ALLOWLIST:-market_data/matching/match_allowlist.json}"
INTERVAL="${SHADOW_INTERVAL:-20}"

mkdir -p "$(dirname "$LEDGER")"

# Defense-in-depth: shadow must never trade, regardless of .env.
export EXECUTION_MODE="${EXECUTION_MODE:-paper}"

# `exec` so systemd tracks the real PID and signals propagate cleanly.
exec kalshi-arb monitor \
  --allowlist "$ALLOWLIST" \
  --ledger "$LEDGER" \
  --interval "$INTERVAL"
