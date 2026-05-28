"""read_my_results — query the trial registry for a given agent's trials.

CLI::

    python -m research.agents.tools.read_my_results \\
        --agent-id <string>          # e.g. "codex:abc12345" or "manual:foo"
        [--limit N]                  # default 50
        [--registry-db <path>]       # default: market_data/trials.db
        [--scope-strict]             # enforce codex:<8hex> format

Outputs a JSON array of trial rows ordered newest first::

    [
      {
        "id": 42,
        "spec_name": "buy_hot",
        "spec_hash": "abc...",
        "val_sharpe_net": 1.23,
        "gate_passed": null,
        "ts_created": 1716900000
      },
      ...
    ]

Exits 0 on success, 1 on validation/scope error.

Scope-strict rule
-----------------
When ``--scope-strict`` is passed, the ``agent_id`` must match
``codex:<8-char hex>`` exactly (case-insensitive hex digits).  This lets the
orchestrator add a guardrail when invoking the tool on behalf of a Codex
worker.  Humans querying ad-hoc do not need to set the flag.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Regex that matches the canonical worker-id format: codex: followed by
# exactly 8 hexadecimal characters (lower or upper case).
_STRICT_AGENT_ID_RE = re.compile(r"^codex:[0-9a-fA-F]{8}$")


def _scope_strict_ok(agent_id: str) -> bool:
    return bool(_STRICT_AGENT_ID_RE.match(agent_id))


def _query_trials(agent_id: str, limit: int, db_path: Path | str | None) -> list[dict]:
    """Query the registry and return serialisable dicts."""
    from research.registry.db import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                id,
                spec_name,
                spec_hash,
                val_sharpe_net,
                scorer_promotion_gate_passed AS gate_passed,
                ts_created
            FROM trials
            WHERE agent_id = ?
            ORDER BY ts_created DESC
            LIMIT ?
            """,
            (agent_id, int(limit)),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        gate_raw = row["gate_passed"]
        gate = None if gate_raw is None else bool(gate_raw)
        result.append(
            {
                "id": int(row["id"]),
                "spec_name": str(row["spec_name"]),
                "spec_hash": str(row["spec_hash"]),
                "val_sharpe_net": row["val_sharpe_net"],
                "gate_passed": gate,
                "ts_created": int(row["ts_created"]),
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read trial results for a specific agent from the registry.",
        prog="python -m research.agents.tools.read_my_results",
    )
    parser.add_argument(
        "--agent-id",
        required=True,
        metavar="AGENT_ID",
        help='Agent provenance tag, e.g. "codex:abc12345" or "manual:foo".',
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Maximum number of rows to return (default: 50).",
    )
    parser.add_argument(
        "--registry-db",
        default=None,
        metavar="PATH",
        help="Override the registry database path (default: market_data/trials.db).",
    )
    parser.add_argument(
        "--scope-strict",
        action="store_true",
        default=False,
        help=(
            "Enforce that agent_id matches codex:<8-char hex> format.  "
            "Refuse with exit 1 if it does not."
        ),
    )
    args = parser.parse_args(argv)

    # --- scope-strict check ---
    if args.scope_strict and not _scope_strict_ok(args.agent_id):
        error_payload = {
            "error": (
                f"--scope-strict: agent_id {args.agent_id!r} does not match "
                "the required format codex:<8 hex chars>."
            )
        }
        print(json.dumps(error_payload, indent=2), file=sys.stderr)
        return 1

    db_path = Path(args.registry_db) if args.registry_db else None

    try:
        rows = _query_trials(args.agent_id, args.limit, db_path)
    except Exception as exc:  # noqa: BLE001
        error_payload = {"error": f"Registry query failed: {exc}"}
        print(json.dumps(error_payload, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
