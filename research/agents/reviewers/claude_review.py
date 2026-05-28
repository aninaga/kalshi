"""Claude adversarial reviewer — packet builder and registry write helper.

Architecture note
-----------------
This module is the **packet builder + registry-writer** half of the reviewer.
The actual subagent spawn (via Claude Code's ``Agent`` tool) is performed by
the **orchestrator** Wave 3b, NOT here. Reason: the ``Agent`` tool is a Claude
Code surface, not a Python API; from a plain Python script there is no way to
spawn an Opus subagent. The clean separation is:

  1. Orchestrator detects a trial that passed the scorer gate.
  2. Orchestrator calls :func:`review_trial` (this module) which writes a
     review packet markdown file under
     ``market_data/review_packets/<spec_hash>_<ts>.md``.
  3. Orchestrator (which IS inside a Claude Code session and HAS the
     ``Agent`` tool) reads the packet, invokes the subagent itself with
     ``prompt = reviewer_prompt + packet contents``.
  4. Orchestrator calls :func:`record_review_text` with the subagent's
     response.

This module never imports or depends on any Claude SDK or Agent tool — it is
plain Python that can run under unittest in CI.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from research.harness.strategy_spec import StrategySpec
from research.registry import api as registry_api
from research.registry.db import connect


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

# The packet directory. Kept under ``market_data/`` alongside the registry
# and audit log so all autoresearch artefacts live in one place.
DEFAULT_PACKET_DIR = Path("market_data/review_packets")

# The reviewer prompt is colocated with this module. Resolved at call time so
# tests can substitute their own (and so the module imports cleanly even if the
# prompt file is briefly absent during development).
DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompt.md"


# --------------------------------------------------------------------------- #
# review_trial — build the packet, do NOT spawn the subagent
# --------------------------------------------------------------------------- #


def _sibling_failed_count(
    spec_name: str,
    spec_hash: str,
    db_path: Path | str | None,
) -> int:
    """Count rows with the same ``spec_name`` but a different ``spec_hash``
    that failed the scorer gate.

    The "same family of hypothesis" heuristic is spec_name equality. Different
    spec_hashes mean different parameterisations of the same idea; gate_passed
    = 0 means we rejected them. The sibling count widens the multiple-testing
    null when reviewing this candidate.

    ``scorer_promotion_gate_passed IS NULL`` rows (not yet scored) don't
    count — we want explicit failures only.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trials "
            "WHERE spec_name = ? "
            "  AND spec_hash != ? "
            "  AND scorer_promotion_gate_passed = 0",
            (spec_name, spec_hash),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row is not None else 0


def _fetch_trial_full_row(
    spec_hash: str,
    db_path: Path | str | None,
) -> dict[str, Any]:
    """Read the full latest trial row for a spec_hash as a plain dict.

    :func:`registry_api.query` returns a ``TrialRow`` projection that omits a
    bunch of columns we want in the packet (spec_json, mechanistic_writeup,
    scorer reasons). We read the raw row here instead.

    Raises ``ValueError`` if no row exists for the spec_hash.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM trials WHERE spec_hash = ? "
            "ORDER BY ts_created DESC, id DESC LIMIT 1",
            (spec_hash,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(
            f"no trial row found for spec_hash={spec_hash!r}; "
            "cannot build review packet."
        )
    return {k: row[k] for k in row.keys()}


def _render_spec_section(spec_json: str | None) -> str:
    """Pretty-print the spec JSON for the packet.

    The trial row stores spec_json as a compact (sorted-keys, no-whitespace)
    string. For the reviewer we re-indent it so the numeric thresholds and
    feature names are eyeball-able.
    """
    if spec_json is None:
        return "_spec_json missing_\n"
    try:
        spec_dict = json.loads(spec_json)
    except (TypeError, ValueError):
        # If it isn't valid JSON for any reason (defensive — record_trial
        # accepts whatever to_json returns) fall back to the raw string.
        return f"```\n{spec_json}\n```\n"
    pretty = json.dumps(spec_dict, indent=2, sort_keys=True)
    return f"```json\n{pretty}\n```\n"


def _render_scorer_summary(row: dict[str, Any]) -> str:
    """Compose the scorer-stats portion of the packet."""
    ci_lo = row.get("scorer_block_bootstrap_ci_lo")
    ci_hi = row.get("scorer_block_bootstrap_ci_hi")
    dsr = row.get("scorer_dsr")
    dsr_p = row.get("scorer_dsr_pvalue")
    gate = row.get("scorer_promotion_gate_passed")
    reasons_raw = row.get("scorer_promotion_gate_reasons")
    reasons: list[str]
    if reasons_raw is None:
        reasons = []
    else:
        try:
            reasons = list(json.loads(reasons_raw))
        except (TypeError, ValueError):
            reasons = [str(reasons_raw)]

    lines = [
        f"- block_bootstrap_ci_lo: {ci_lo}",
        f"- block_bootstrap_ci_hi: {ci_hi}",
        f"- dsr: {dsr}",
        f"- dsr_pvalue: {dsr_p}",
        f"- promotion_gate_passed: {bool(gate) if gate is not None else None}",
    ]
    if reasons:
        lines.append("- promotion_gate_reasons: " + ", ".join(reasons))
    return "\n".join(lines) + "\n"


def _render_batch_summary(row: dict[str, Any], split: str) -> str:
    """Render train/val/test summary stats from the row."""
    keys = [
        ("pnl_gross", f"{split}_pnl_gross"),
        ("pnl_net", f"{split}_pnl_net"),
        ("sharpe_net", f"{split}_sharpe_net"),
        ("n_trades", f"{split}_n_trades"),
        ("win_rate", f"{split}_win_rate"),
    ]
    lines = []
    for label, col in keys:
        v = row.get(col)
        lines.append(f"- {label}: {v}")
    return "\n".join(lines) + "\n"


def _build_packet_markdown(
    row: dict[str, Any],
    sibling_failed_count: int,
    total_trials: int,
) -> str:
    """Assemble the full packet markdown.

    Sections in this order:
      1. Header (trial id, spec_hash, agent, codex_worker_id, timestamp).
      2. Spec JSON, pretty-printed.
      3. Mechanistic writeup (codex worker authored).
      4. Scorer summary (block-bootstrap CI, DSR, gate verdict).
      5. Train / Val / Test batch summaries.
      6. Survivorship context (``total_trials``, ``sibling_failed_count``).
      7. Per-game distribution (Wave 4 placeholder).
    """
    spec_hash = str(row.get("spec_hash") or "")
    spec_name = str(row.get("spec_name") or "")
    agent_id = str(row.get("agent_id") or "")
    codex_worker_id = row.get("codex_worker_id")
    ts_created = row.get("ts_created")
    spec_json = row.get("spec_json")
    writeup = row.get("mechanistic_writeup") or "_no mechanistic writeup provided_"

    parts: list[str] = []
    parts.append(f"# Review packet for trial {row.get('id')}\n")
    parts.append(
        f"- spec_hash: `{spec_hash}`\n"
        f"- spec_name: {spec_name}\n"
        f"- agent_id: {agent_id}\n"
        f"- codex_worker_id: {codex_worker_id}\n"
        f"- ts_created: {ts_created}\n"
    )

    parts.append("\n## Spec JSON\n")
    parts.append(_render_spec_section(spec_json))

    parts.append("\n## Mechanistic writeup\n")
    parts.append(f"{writeup}\n")

    parts.append("\n## Scorer summary\n")
    parts.append(_render_scorer_summary(row))

    parts.append("\n## Train summary\n")
    parts.append(_render_batch_summary(row, "train"))

    parts.append("\n## Val summary\n")
    parts.append(_render_batch_summary(row, "val"))

    parts.append("\n## Test summary (should be empty pre-promotion)\n")
    parts.append(_render_batch_summary(row, "test"))

    parts.append("\n## Survivorship context\n")
    parts.append(
        f"- total_trials in registry: {total_trials}\n"
        f"- siblings (same spec_name, gate_passed=0): "
        f"{sibling_failed_count}\n"
    )

    parts.append("\n## Per-game contribution distribution\n")
    parts.append(
        "_Wave 4 deferral_: per-game top-10/bottom-10 contributions are not "
        "yet stored on the trial row. For now, assess concentration risk "
        "from `val_n_trades` and `val_sharpe_net` together; if both are "
        "needed and absent, say so in the Concentration risk section.\n"
    )

    return "".join(parts)


def review_trial(
    spec_hash: str,
    db_path: Path | str | None = None,
    reviewer_prompt_path: Path | str | None = None,
    dry_run: bool = False,
    packet_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build the adversarial-review packet for a trial.

    Steps:
      1. Load the latest registry row for ``spec_hash``.
      2. Count sibling failures (same spec_name, different spec_hash,
         gate_passed=0) and total trials.
      3. Compose a markdown packet (header + spec JSON + writeup +
         scorer summary + train/val/test stats + survivorship context).
      4. If ``dry_run`` is True, return a stub status dict without touching
         the filesystem.
      5. Otherwise write the packet to
         ``<packet_dir>/<spec_hash>_<ts>.md`` and return a status dict
         pointing to it.

    The Opus subagent spawn happens in the orchestrator, NOT here — see
    module docstring.

    Parameters
    ----------
    spec_hash:
        The trial's spec_hash. The most recent row for this hash is reviewed.
    db_path:
        Optional override for the registry sqlite path (testing).
    reviewer_prompt_path:
        Optional override for the reviewer prompt markdown (testing). The
        path is validated to exist; its contents are not currently embedded
        in the packet (the orchestrator concatenates them at spawn time).
    dry_run:
        If True, build the packet in memory and return a status dict but
        do NOT write a file.
    packet_dir:
        Optional override for the packet output directory (testing).

    Returns
    -------
    dict
        On dry_run: ``{"status": "dry_run", "spec_hash": ..., "packet_size": int,
        "sibling_failed_count": int, "total_trials": int}``.
        Otherwise: ``{"status": "pending_human_spawn", "spec_hash": ...,
        "packet_path": str, "packet_size": int, "sibling_failed_count": int,
        "total_trials": int}``.
    """
    # Resolve the reviewer prompt path — we don't embed it in the packet,
    # but we validate its existence here so the orchestrator's later
    # concat step doesn't fail silently.
    if reviewer_prompt_path is None:
        prompt_path = DEFAULT_PROMPT_PATH
    else:
        prompt_path = Path(reviewer_prompt_path)
    if not prompt_path.exists():
        raise ValueError(
            f"reviewer prompt not found at {prompt_path}; the orchestrator "
            "needs this file to spawn the subagent."
        )

    # Load the trial row (raises ValueError if missing).
    row = _fetch_trial_full_row(spec_hash, db_path)

    # Validate that the spec JSON round-trips through StrategySpec when
    # possible. We don't fail the review build on a round-trip error
    # (the writeup may still be useful) — but we do log it into the
    # packet via the markdown rendering path.
    spec_json = row.get("spec_json")
    if spec_json is not None:
        try:
            StrategySpec.from_json(spec_json)
        except Exception:  # noqa: BLE001 — best-effort sanity check
            # Intentionally swallow: we still want to surface the packet to
            # the reviewer even if the spec is unusual.
            pass

    # Sibling-failure / total-trial denominators for multiple-testing risk.
    siblings = _sibling_failed_count(
        spec_name=str(row.get("spec_name") or ""),
        spec_hash=spec_hash,
        db_path=db_path,
    )
    total = registry_api.count_total_trials(db_path=db_path)

    packet_md = _build_packet_markdown(
        row=row,
        sibling_failed_count=siblings,
        total_trials=total,
    )

    if dry_run:
        return {
            "status": "dry_run",
            "spec_hash": spec_hash,
            "packet_size": len(packet_md),
            "sibling_failed_count": siblings,
            "total_trials": total,
        }

    # Write the packet to disk.
    out_dir = Path(packet_dir) if packet_dir is not None else DEFAULT_PACKET_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    out_path = out_dir / f"{spec_hash}_{ts}.md"
    out_path.write_text(packet_md, encoding="utf-8")

    return {
        "status": "pending_human_spawn",
        "spec_hash": spec_hash,
        "packet_path": str(out_path),
        "packet_size": len(packet_md),
        "sibling_failed_count": siblings,
        "total_trials": total,
    }


# --------------------------------------------------------------------------- #
# record_review_text — thin wrapper over registry_api.record_claude_review
# --------------------------------------------------------------------------- #


def record_review_text(
    spec_hash: str,
    review_text: str,
    model: str = "opus-4.7",
    db_path: Path | str | None = None,
) -> int:
    """Idempotent write of the adversarial review back to the registry.

    Thin wrapper over :func:`research.registry.api.record_claude_review`. The
    underlying call is the ONLY sanctioned mutation path against the trials
    table — see the registry's api.py docstring for the append-only invariant
    discussion.

    Raises ``ValueError`` if:
      - No trial row exists for ``spec_hash``.
      - The most recent row for this spec_hash already has a Claude review
        attached (idempotency: we never overwrite a prior verdict).
    """
    return registry_api.record_claude_review(
        spec_hash=spec_hash,
        review_text=review_text,
        review_model=model,
        db_path=db_path,
    )


__all__ = [
    "DEFAULT_PACKET_DIR",
    "DEFAULT_PROMPT_PATH",
    "record_review_text",
    "review_trial",
]
