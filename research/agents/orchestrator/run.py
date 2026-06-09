"""Opus 4.7 orchestrator entry point.

Run with::

    python -m research.agents.orchestrator.run \\
        --duration-hours 6 --max-trials 30 --codex-workers 2

The orchestrator is pure Python; it does NOT depend on being inside a Claude
Code session. (The historical design called for spawning Opus reviewer
subagents via Claude Code's ``Agent`` tool, which is unavailable from raw
Python. The compromise: this script ENQUEUES review packets via
``research.agents.reviewers.claude_review.review_trial``, and a separate
Claude-Code-side process drains the queue.)

Loop shape::

    startup_gate() → init Budget → repeat {
        read aggregate registry stats
        pick research direction (rotate through plan v2 §2.2 list)
        spawn N codex workers, wait for them
        for each completed worker:
            parse result.md (JSON trailing block)
            if gate_passed: enqueue review packet
        update trial count
        record_usage on Budget
        if rate-limited: sleep 600s and reduce concurrency (floor 1)
        log a summary line
        check stop conditions
    } → write summary.md → exit

Stop conditions (any of):

- trial count >= ``--max-trials``
- elapsed wall-clock >= ``--duration-hours``
- ``Budget.would_exceed_weekly(next_estimate)`` returns True
- a hard error in :func:`codex_dispatcher.spawn_workers` (e.g., codex binary
  missing) — the orchestrator logs and exits without further spawns

Dry-run mode (``--dry-run``) skips the actual codex spawn and instead writes
a stub ``result.md`` for one fake worker so the rest of the wiring runs end
to end. This is the test-friendly smoke path.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from research.agents.orchestrator.budget import (
    TOKENS_PER_ORCHESTRATOR_OVERHEAD,
    TOKENS_PER_REVIEWER_CALL,
    Budget,
)
from research.agents.orchestrator import claude_dispatcher, codex_dispatcher
from research.agents.orchestrator.codex_dispatcher import DEFAULT_TIMEOUT_SEC


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PHASE1_GATE_SCRIPT = _REPO_ROOT / "research" / "scripts" / "check_phase1_gates.sh"
DEFAULT_STATE_PATH = _REPO_ROOT / "market_data" / "orchestrator_state.json"
DEFAULT_REPORT_BASE = _REPO_ROOT / "research" / "reports"
RATE_LIMIT_PAUSE_SEC = 600  # 10 minutes per plan v2 §codex_dispatcher


# Plan v2 §2.2 research-direction rotation. Kept here as a list of short
# markdown blurbs; the actual codex worker prompt is generic and reads the
# direction text from ``direction.md`` in its scratch dir.
#
# NO hardcoded rotation, by design. Which strategies get pursued is decided at
# runtime by the agent layer: research.lab.scout ORIGINATES hypotheses from
# research.lab.eda diagnostics and research.lab.director PRIORITIZES the open
# pool against the research ledger. This orchestrator only carries that
# machinery — never a menu of strategies. (Kept empty intentionally.)
DIRECTION_ROTATION: list[dict[str, str]] = []


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _setup_logging(report_dir: Path) -> logging.Logger:
    """Configure a per-run logger that tees to file + stdout.

    The summary log is the human's primary monitoring surface (per the
    prompt.md ``tail -f research/reports/<date>/orchestrator.log``).
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    log_path = report_dir / "orchestrator.log"

    logger = logging.getLogger("orchestrator")
    logger.setLevel(logging.INFO)
    # Strip any handlers from prior runs (the test suite re-uses the process).
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(log_path, mode="a")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # Disable propagation so tests that capture root logger output don't see
    # duplicate lines.
    logger.propagate = False
    return logger


# --------------------------------------------------------------------------- #
# Rotation state
# --------------------------------------------------------------------------- #


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _hypothesis_to_direction(h) -> dict[str, str]:
    """Map a ``research.lab.types.Hypothesis`` row onto a rotation entry.

    The registry stores a structured idea (mechanism / observable signal /
    pre-registered direction); flatten it into the ``{name, blurb}`` shape the
    rest of the loop (and :func:`_compose_direction_md`) expects. ``name`` uses
    the hypothesis id (stable dedupe hash) so the direction log / packets trace
    back to the registry row.
    """
    parts = [h.mechanism.strip()]
    if getattr(h, "signal_desc", ""):
        parts.append(f"Signal: {h.signal_desc.strip()}")
    if getattr(h, "direction", ""):
        parts.append(f"Pre-registered direction: {h.direction.strip()}")
    return {
        "name": (h.id or h.hash()),
        "blurb": " ".join(parts),
        "market": h.market,
    }


def _select_directions(originate: bool = True) -> list[dict[str, str]]:
    """Directions to pursue — AGENT-DECIDED, never hardcoded.

    ``research.lab.scout`` ORIGINATES hypotheses from data (when the open pool is
    empty) and ``research.lab.director`` PRIORITIZES them. Returns ``[]`` when
    there is genuinely nothing open — there is no canned rotation to fall back to
    (that is the point). The caller must run the scout (with a live agent) to
    populate the pool.
    """
    try:
        from research.lab import director, hypothesis as _hyp  # lazy
    except ImportError:
        return []
    try:
        if originate and not _hyp.open_hypotheses():
            from research.lab import scout
            scout.propose()  # agent origination from EDA diagnostics
        chosen = director.select(k=8)
    except Exception:  # noqa: BLE001 — never let the layer crash the loop
        return []
    return [_hypothesis_to_direction(h) for h in chosen]


def _pick_direction(
    state: dict[str, Any],
    rotation: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    """Return the next director-ranked direction and advance state.

    ``rotation`` defaults to :func:`_select_directions` (agent-decided). Raises
    if the pool is empty — there is no hardcoded menu to fall back to; originate
    ideas via the scout first. Mutates ``state`` in-place (caller persists).
    """
    if rotation is None:
        rotation = _select_directions()
    if not rotation:
        raise RuntimeError(
            "no open hypotheses to pursue — run research.lab.scout with a live "
            "agent to originate ideas; nothing hardcoded to fall back to.")
    idx = int(state.get("direction_rotation_index", 0)) % len(rotation)
    direction = rotation[idx]
    state["direction_rotation_index"] = (idx + 1) % len(rotation)
    return direction


# --------------------------------------------------------------------------- #
# Phase 1 gate
# --------------------------------------------------------------------------- #


def _run_phase1_gate(script_path: Path, logger: logging.Logger) -> int:
    """Invoke ``check_phase1_gates.sh`` and return its exit code.

    stdout + stderr are captured and logged at INFO so the human running the
    orchestrator can see all four gate statuses without rerunning by hand.
    """
    if not script_path.exists():
        logger.error("phase1 gate script not found at %s", script_path)
        return 1
    try:
        proc = subprocess.run(  # noqa: S603 — argv is trusted
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        logger.error("phase1 gate script timed out after 600s")
        return 1
    if proc.stdout:
        for line in proc.stdout.rstrip().splitlines():
            logger.info("gate-stdout: %s", line)
    if proc.stderr:
        for line in proc.stderr.rstrip().splitlines():
            logger.info("gate-stderr: %s", line)
    return int(proc.returncode)


# --------------------------------------------------------------------------- #
# Worker result parsing
# --------------------------------------------------------------------------- #


# A worker writes ``result.md`` ending in a fenced JSON block. We tolerate
# multiple trailing fences and pick the LAST valid JSON object.
_JSON_BLOCK_RX = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ParsedResult:
    """The fields the orchestrator extracts from a worker's result.md."""

    spec_hash: str | None
    trial_id: int | None
    gate_passed: bool
    raw_block: dict[str, Any] | None


def _parse_result_md(text: str | None) -> ParsedResult:
    """Best-effort parse of the trailing JSON block in a worker result.md.

    Workers are instructed to write a fenced ```json``` block with at least
    ``spec_hash``, ``trial_id``, ``gate_passed``. Anything else is ignored.

    A missing/unparseable block is treated as gate_passed=False with all
    fields None. We do NOT raise — a malformed worker shouldn't abort the
    orchestrator's loop.
    """
    if text is None:
        return ParsedResult(None, None, False, None)
    matches = _JSON_BLOCK_RX.findall(text)
    if not matches:
        return ParsedResult(None, None, False, None)
    # Use the LAST block — workers may include illustrative examples earlier.
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        spec_hash = obj.get("spec_hash")
        if not isinstance(spec_hash, str) or not spec_hash:
            spec_hash = None
        trial_id_raw = obj.get("trial_id")
        if isinstance(trial_id_raw, bool) or not isinstance(trial_id_raw, int):
            trial_id = None
        else:
            trial_id = int(trial_id_raw)
        gate_raw = obj.get("gate_passed")
        gate_passed = bool(gate_raw) if gate_raw is not None else False
        return ParsedResult(
            spec_hash=spec_hash,
            trial_id=trial_id,
            gate_passed=gate_passed,
            raw_block=obj,
        )
    return ParsedResult(None, None, False, None)


# --------------------------------------------------------------------------- #
# Review packet enqueuing
# --------------------------------------------------------------------------- #


def _enqueue_review_packet(
    spec_hash: str,
    logger: logging.Logger,
) -> str | None:
    """Build a review packet for ``spec_hash`` and return its path on disk.

    Wraps :func:`research.agents.reviewers.claude_review.review_trial` so the
    orchestrator can swallow exceptions (a single bad packet should not kill
    the loop). The actual Opus subagent spawn is decoupled — a separate
    in-session process reads ``market_data/review_packets/`` and drives the
    Agent tool.
    """
    try:
        from research.agents.reviewers.claude_review import review_trial
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not import reviewer module; review for spec_hash=%s "
            "deferred. error=%r",
            spec_hash,
            exc,
        )
        return None
    try:
        status = review_trial(spec_hash=spec_hash)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "review_trial raised for spec_hash=%s; deferred. error=%r",
            spec_hash,
            exc,
        )
        return None
    if not isinstance(status, dict):
        return None
    return status.get("packet_path")


# --------------------------------------------------------------------------- #
# Dry-run worker stub
# --------------------------------------------------------------------------- #


def _spawn_dry_run_workers(
    direction_md: str,
    worker_count: int,
    base_dir: Path,
) -> list[dict]:
    """Synthesize ``worker_count`` worker result dicts without invoking codex.

    Each fake worker writes a deterministic ``result.md`` with
    gate_passed=False so the orchestrator's review-enqueue path is exercised
    but no real registry rows are produced.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for _ in range(worker_count):
        worker_id = uuid.uuid4().hex[:8]
        worker_dir = base_dir / f"codex_{worker_id}_dryrun"
        worker_dir.mkdir(parents=True, exist_ok=True)
        (worker_dir / "direction.md").write_text(direction_md, encoding="utf-8")
        result_md_text = (
            "# DRY RUN result\n\n"
            "No real backtest was run; the orchestrator is in --dry-run mode.\n"
            "\n"
            "```json\n"
            + json.dumps(
                {
                    "spec_hash": None,
                    "trial_id": None,
                    "gate_passed": False,
                    "note": "dry-run stub",
                },
                indent=2,
            )
            + "\n```\n"
        )
        (worker_dir / "result.md").write_text(result_md_text, encoding="utf-8")
        out.append({
            "worker_id": worker_id,
            "worker_dir": str(worker_dir.resolve()),
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_sec": 0.0,
            "result_md_text": result_md_text,
            "rate_limited": False,
            "timed_out": False,
        })
    return out


# --------------------------------------------------------------------------- #
# Registry summary helpers
# --------------------------------------------------------------------------- #


def _safe_registry_summary(logger: logging.Logger) -> dict[str, Any]:
    """Read aggregate registry stats with broad exception handling.

    The registry may be absent on a fresh setup. We log + return empty rather
    than crashing the orchestrator.
    """
    summary: dict[str, Any] = {
        "total_trials": None,
        "top_specs": [],
    }
    try:
        from research.registry import api as registry_api

        summary["total_trials"] = registry_api.count_total_trials()
        rows = registry_api.query(limit=200)
        # Top 5 by val_sharpe_net (skip rows missing the metric).
        scored = [r for r in rows if r.val_sharpe_net is not None]
        scored.sort(key=lambda r: r.val_sharpe_net or float("-inf"), reverse=True)
        summary["top_specs"] = [
            {
                "id": r.id,
                "spec_hash": r.spec_hash,
                "spec_name": r.spec_name,
                "val_sharpe_net": r.val_sharpe_net,
                "val_pnl_net": r.val_pnl_net,
                "val_n_trades": r.val_n_trades,
                "gate_passed": r.scorer_promotion_gate_passed,
            }
            for r in scored[:5]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("registry summary unavailable: %r", exc)
    return summary


# --------------------------------------------------------------------------- #
# Summary report
# --------------------------------------------------------------------------- #


def _write_summary_report(
    report_dir: Path,
    *,
    started_at: float,
    ended_at: float,
    exit_reason: str,
    trials_produced: int,
    gate_passed_count: int,
    pending_review_packets: list[str],
    budget_status: dict[str, Any],
    registry_summary: dict[str, Any],
    direction_log: list[str],
) -> Path:
    """Compose and write ``summary.md``. Returns its path."""
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / "summary.md"

    duration_sec = max(0.0, ended_at - started_at)
    duration_min = duration_sec / 60.0

    lines: list[str] = []
    lines.append(f"# Orchestrator run summary — {date.today().isoformat()}\n")
    lines.append(
        f"- started_at: {datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()}\n"
        f"- ended_at:   {datetime.fromtimestamp(ended_at, tz=timezone.utc).isoformat()}\n"
        f"- duration:   {duration_min:.1f} min\n"
        f"- exit_reason: **{exit_reason}**\n"
    )

    lines.append("\n## Trials\n")
    lines.append(
        f"- trials_produced (worker returns): {trials_produced}\n"
        f"- scorer-gate-passed count: {gate_passed_count}\n"
        f"- pending review packets: {len(pending_review_packets)}\n"
    )

    if pending_review_packets:
        lines.append("\n### Pending review packets\n")
        for p in pending_review_packets:
            lines.append(f"- `{p}`\n")

    lines.append("\n## Top 5 candidates by val_sharpe_net (registry-wide)\n")
    top_specs = registry_summary.get("top_specs") or []
    if not top_specs:
        lines.append("_no scored trials in registry yet_\n")
    else:
        for r in top_specs:
            lines.append(
                f"- id={r['id']} spec_name={r['spec_name']} "
                f"val_sharpe_net={r['val_sharpe_net']} "
                f"val_n_trades={r['val_n_trades']} "
                f"gate_passed={r['gate_passed']} hash=`{r['spec_hash']}`\n"
            )

    lines.append("\n## Budget\n")
    weekly_max = budget_status.get("weekly_max_budget_tokens") or 0
    cap_tokens = budget_status.get("weekly_cap_tokens") or 0
    used = budget_status.get("rolling_token_total") or 0
    pct = (used / cap_tokens) if cap_tokens else 0.0
    lines.append(
        f"- weekly_max_budget_tokens (assumed): {weekly_max}\n"
        f"- weekly_cap_tokens (40% default): {cap_tokens}\n"
        f"- rolling_token_total (7d): {used}\n"
        f"- pct of cap used: {pct:.1%}\n"
        f"- rolling_codex_calls (7d): {budget_status.get('rolling_codex_calls')}\n"
    )

    lines.append("\n## Directions used (chronological)\n")
    if not direction_log:
        lines.append("_none_\n")
    else:
        for d in direction_log:
            lines.append(f"- {d}\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


@dataclass
class LoopConfig:
    duration_hours: float
    max_trials: int
    codex_workers: int
    claude_workers: int
    dry_run: bool
    phase1_gate_script: Path
    skip_gate: bool
    report_dir: Path
    state_path: Path
    weekly_cap_pct: float
    codex_timeout_sec: int
    claude_timeout_sec: int
    experiments_base: Path
    creative_every: int  # creative-mode on iteration if iter % creative_every == 0
    claude_model: str | None = None
    cost_profile: str | None = None
    codex_model: str | None = None
    claude_prompt: str | None = None


def run_loop(cfg: LoopConfig, logger: logging.Logger) -> int:
    """Execute the orchestrator main loop. Returns process exit code."""
    started_at = time.time()
    end_at = started_at + cfg.duration_hours * 3600.0

    # --- Startup gate -----------------------------------------------------
    if cfg.skip_gate:
        logger.warning(
            "==== --skip-gate ACTIVE — Phase 1 startup gate BYPASSED. "
            "DEV ONLY. Test-set hash drift, leakage, and overfit canaries "
            "are NOT being verified. ===="
        )
    else:
        rc = _run_phase1_gate(cfg.phase1_gate_script, logger)
        if rc != 0:
            logger.error("Phase 1 startup gate FAILED (rc=%d); refusing to proceed", rc)
            _write_summary_report(
                report_dir=cfg.report_dir,
                started_at=started_at,
                ended_at=time.time(),
                exit_reason="phase1_gate_failed",
                trials_produced=0,
                gate_passed_count=0,
                pending_review_packets=[],
                budget_status={},
                registry_summary={},
                direction_log=[],
            )
            return 1

    # --- Budget init ------------------------------------------------------
    budget = Budget(weekly_cap_pct=cfg.weekly_cap_pct)
    bs = budget.status()
    logger.info(
        "budget initialized: weekly_cap=%d tokens (=%.0f%% of %d weekly_max); "
        "current 7d usage=%d",
        budget.weekly_cap_tokens,
        bs.weekly_cap_pct * 100,
        bs.weekly_max_budget_tokens,
        bs.rolling_token_total,
    )

    # --- Rotation state ---------------------------------------------------
    state = _load_state(cfg.state_path)
    rotation = _select_directions()
    using_registry = rotation != DIRECTION_ROTATION
    logger.info(
        "direction rotation: %d entries (%s)",
        len(rotation),
        "registry-backed" if using_registry else "legacy fallback",
    )

    # --- Loop -------------------------------------------------------------
    trial_count = 0
    gate_passed_count = 0
    pending_packets: list[str] = []
    direction_log: list[str] = []
    exit_reason = "completed"
    current_codex_workers = max(0, int(cfg.codex_workers))
    current_claude_workers = max(0, int(cfg.claude_workers))
    iteration_idx = 0

    if current_codex_workers + current_claude_workers < 1:
        logger.error("no workers configured (both pools at 0); refusing to run")
        return 1

    while True:
        # Stop checks at top of loop so an exhausted-on-entry config exits cleanly.
        now = time.time()
        if now >= end_at:
            exit_reason = "duration_elapsed"
            logger.info("stop: duration elapsed (%.1f min)", (now - started_at) / 60)
            break
        if trial_count >= cfg.max_trials:
            exit_reason = "max_trials_reached"
            logger.info("stop: max_trials reached (%d)", trial_count)
            break
        next_estimate = (
            TOKENS_PER_ORCHESTRATOR_OVERHEAD
            + TOKENS_PER_REVIEWER_CALL  # worst-case: this batch yields a reviewable trial
        )
        if budget.would_exceed_weekly(next_estimate):
            exit_reason = "weekly_budget_cap"
            logger.info(
                "stop: weekly budget cap would be exceeded by next iteration "
                "(estimate=%d)",
                next_estimate,
            )
            break

        # --- Pick direction (creative mode every Nth iteration) ---
        direction = _pick_direction(state, rotation)
        _save_state(cfg.state_path, state)
        creative_now = (
            cfg.creative_every > 0
            and iteration_idx > 0
            and (iteration_idx % cfg.creative_every == 0)
        )
        direction_log.append(
            f"{direction['name']}{' [creative]' if creative_now else ''}"
        )
        registry_summary = _safe_registry_summary(logger)
        direction_md = _compose_direction_md(
            direction, registry_summary, creative_mode=creative_now
        )

        logger.info(
            "iteration %d: direction=%s codex_workers=%d claude_workers=%d "
            "creative=%s trial_count=%d",
            iteration_idx,
            direction["name"],
            current_codex_workers,
            current_claude_workers,
            creative_now,
            trial_count,
        )

        # --- Spawn / dry-run ---
        worker_results: list[dict]
        if cfg.dry_run:
            worker_results = _spawn_dry_run_workers(
                direction_md=direction_md,
                worker_count=current_codex_workers + current_claude_workers,
                base_dir=cfg.experiments_base,
            )
        else:
            codex_handles: list = []
            claude_handles: list = []
            spawn_error: str | None = None

            if current_codex_workers > 0:
                try:
                    codex_handles = codex_dispatcher.spawn_workers(
                        direction=direction_md,
                        worker_count=current_codex_workers,
                        base_dir=cfg.experiments_base,
                        timeout_sec=cfg.codex_timeout_sec,
                        cost_profile=cfg.cost_profile,
                        model_flag=(f"-m {cfg.codex_model} -c model_reasoning_effort=xhigh" if cfg.codex_model else None),
                    )
                except FileNotFoundError as exc:
                    logger.error("cannot spawn codex workers: %r", exc)
                    spawn_error = "codex_unavailable"
                except Exception as exc:  # noqa: BLE001
                    logger.error("codex spawn_workers raised %r", exc)
                    spawn_error = "spawn_error"

            if current_claude_workers > 0 and spawn_error is None:
                try:
                    claude_handles = claude_dispatcher.spawn_workers(
                        direction=direction_md,
                        worker_count=current_claude_workers,
                        base_dir=cfg.experiments_base,
                        timeout_sec=cfg.claude_timeout_sec,
                        model=cfg.claude_model,
                        cost_profile=cfg.cost_profile,
                        prompt_path=(Path(cfg.claude_prompt) if cfg.claude_prompt
                                     else claude_dispatcher.DEFAULT_PROMPT_PATH),
                    )
                except FileNotFoundError as exc:
                    logger.warning(
                        "cannot spawn claude workers: %r — continuing with "
                        "codex-only", exc
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "claude spawn_workers raised %r — continuing with "
                        "codex-only", exc
                    )

            if spawn_error is not None and not codex_handles and not claude_handles:
                exit_reason = spawn_error
                break

            codex_results = (
                codex_dispatcher.wait_for_workers(
                    codex_handles, timeout_sec=cfg.codex_timeout_sec
                )
                if codex_handles
                else []
            )
            claude_results = (
                claude_dispatcher.wait_for_workers(
                    claude_handles, timeout_sec=cfg.claude_timeout_sec
                )
                if claude_handles
                else []
            )
            worker_results = codex_results + claude_results

        # --- Parse results, enqueue reviews ---
        batch_gate_passed = 0
        codex_rate_limited = False
        claude_rate_limited = False
        for res in worker_results:
            trial_count += 1
            parsed = _parse_result_md(res.get("result_md_text"))
            engine = res.get("engine", "?")
            logger.info(
                "  worker[%s]=%s exit=%s timed_out=%s rate_limited=%s "
                "gate_passed=%s spec_hash=%s trial_id=%s",
                engine,
                res["worker_id"],
                res["exit_code"],
                res["timed_out"],
                res["rate_limited"],
                parsed.gate_passed,
                parsed.spec_hash,
                parsed.trial_id,
            )
            if res["rate_limited"]:
                if engine == "codex":
                    codex_rate_limited = True
                elif engine == "claude":
                    claude_rate_limited = True
            if parsed.gate_passed and parsed.spec_hash:
                batch_gate_passed += 1
                gate_passed_count += 1
                pkt = _enqueue_review_packet(parsed.spec_hash, logger)
                if pkt:
                    pending_packets.append(pkt)
                    logger.info("  enqueued review packet: %s", pkt)
        batch_rate_limited = codex_rate_limited or claude_rate_limited

        # --- Budget bookkeeping ---
        # Charge: per-trial overhead for every worker that returned; one
        # reviewer call estimate per gate-passed trial. Codex calls counted
        # but don't move the Anthropic-side budget.
        batch_token_charge = (
            len(worker_results) * TOKENS_PER_ORCHESTRATOR_OVERHEAD
            + batch_gate_passed * TOKENS_PER_REVIEWER_CALL
        )
        budget.record_usage(
            tokens=batch_token_charge,
            codex_calls=len(worker_results),
            label=f"iter:{direction['name']}",
        )

        # --- Per-iteration log line ---
        iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(
            "%s trial_count=%d worker_returns=%d passed=%d "
            "codex_rate_limit=%s claude_rate_limit=%s",
            iso,
            trial_count,
            len(worker_results),
            batch_gate_passed,
            codex_rate_limited,
            claude_rate_limited,
        )

        # --- Rate-limit backoff (PER-ENGINE) ---
        # codex (ChatGPT Pro) and claude (Max-20x) have independent quotas;
        # a rate-limit on one must NOT throttle the other. The unaffected
        # pool keeps running at full concurrency — that's the asymmetric
        # fallback ("if claude caps, lean harder on codex" and vice versa).
        # We still take a global 10-min pause to let the throttled engine
        # cool off, but ONLY if at least one engine actually flagged it.
        if batch_rate_limited and not cfg.dry_run:
            new_codex = current_codex_workers
            new_claude = current_claude_workers
            if codex_rate_limited:
                new_codex = max(0, current_codex_workers - 1)
            if claude_rate_limited:
                new_claude = max(0, current_claude_workers - 1)
            # Keep at least one worker per pool that started with >0 — but
            # only if THAT pool wasn't the one rate-limited. The rate-limited
            # pool is allowed to go to 0 (the other pool keeps producing).
            if current_codex_workers > 0 and not codex_rate_limited:
                new_codex = max(1, new_codex)
            if current_claude_workers > 0 and not claude_rate_limited:
                new_claude = max(1, new_claude)
            logger.warning(
                "rate-limit: codex_hit=%s claude_hit=%s — sleeping %ds, "
                "codex %d->%d, claude %d->%d",
                codex_rate_limited, claude_rate_limited,
                RATE_LIMIT_PAUSE_SEC,
                current_codex_workers, new_codex,
                current_claude_workers, new_claude,
            )
            time.sleep(RATE_LIMIT_PAUSE_SEC)
            current_codex_workers = new_codex
            current_claude_workers = new_claude
            if current_codex_workers + current_claude_workers < 1:
                logger.error(
                    "both worker pools throttled to 0 — exiting loop cleanly"
                )
                exit_reason = "both_pools_throttled_to_zero"
                break

        iteration_idx += 1

    # --- Write summary -----------------------------------------------------
    ended_at = time.time()
    final_status = budget.status()
    budget_status_dict = {
        "rolling_token_total": final_status.rolling_token_total,
        "rolling_codex_calls": final_status.rolling_codex_calls,
        "weekly_cap_tokens": budget.weekly_cap_tokens,
        "weekly_max_budget_tokens": final_status.weekly_max_budget_tokens,
    }
    summary_path = _write_summary_report(
        report_dir=cfg.report_dir,
        started_at=started_at,
        ended_at=ended_at,
        exit_reason=exit_reason,
        trials_produced=trial_count,
        gate_passed_count=gate_passed_count,
        pending_review_packets=pending_packets,
        budget_status=budget_status_dict,
        registry_summary=_safe_registry_summary(logger),
        direction_log=direction_log,
    )
    logger.info("summary written to %s", summary_path)
    return 0


# --------------------------------------------------------------------------- #
# Direction.md composition
# --------------------------------------------------------------------------- #


def _compose_direction_md(
    direction: dict[str, str],
    registry_summary: dict[str, Any],
    creative_mode: bool = False,
) -> str:
    """Compose the markdown that each worker reads from direction.md.

    Includes a short snapshot of registry state so the worker has multiple-
    testing awareness baked into its proposal step. When ``creative_mode`` is
    True, an additional block invites the worker to step outside the most
    obvious framing of the direction — used to broaden the search.
    """
    parts: list[str] = []
    parts.append(f"# Research direction: {direction['name']}\n\n")
    parts.append(direction["blurb"] + "\n\n")

    parts.append("## Registry context\n\n")
    total = registry_summary.get("total_trials")
    parts.append(f"- total trials in registry so far: {total}\n")
    top = registry_summary.get("top_specs") or []
    if top:
        parts.append("- current top val_sharpe_net specs (avoid duplicating):\n")
        for r in top:
            parts.append(
                f"  - {r['spec_name']} (val_sharpe_net={r['val_sharpe_net']}, "
                f"n_trades={r['val_n_trades']})\n"
            )
    else:
        parts.append("- registry is empty or has no scored specs yet.\n")

    if creative_mode:
        parts.append("\n## Creative mode\n\n")
        parts.append(
            "- This iteration is in **creative mode**. The default proposal "
            "shape (single-feature entry on the obvious side) has been "
            "well-sampled. Now: propose something **unusual but defensible**. "
            "Options that count as creative:\n"
            "  * **Inverted side** for this direction (e.g. `long_cold` "
            "instead of `long_hot`).\n"
            "  * **Multi-signal confluence** — combine TWO live-safe features "
            "in `entry_condition` (still simple, but not single-feature). "
            "Each clause must be mechanistically justified in `writeup.md`.\n"
            "  * **Unusual hold horizon** — very short (60-180s) or very "
            "long (settle-style with `time_stop_sec=7200`).\n"
            "  * **Threshold extremes** — fire only at the tails of a feature "
            "distribution (e.g. `pm_implied_wp < 0.10` or `> 0.90`).\n"
            "- Still: ONE entry condition, well-formed JSON, all features "
            "live-safe, validate before backtest. Be bold in design but "
            "honest in `writeup.md` about why and how it could fail.\n"
        )

    parts.append("\n## Scope rules\n\n")
    parts.append(
        "- Propose ONE concrete `StrategySpec` JSON aligned with the direction above.\n"
        "- Only reference features with `is_live_safe=True`.\n"
        "- Run on `--split val` only. Do NOT request `--split test`.\n"
        "- Write `result.md` ending with a fenced ```json``` block containing "
        "at minimum: `{\"spec_hash\": ..., \"trial_id\": ..., \"gate_passed\": ...}`.\n"
    )
    return "".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="research.agents.orchestrator.run",
        description="Opus 4.7 autoresearch orchestrator entry point.",
    )
    p.add_argument(
        "--duration-hours",
        type=float,
        default=6.0,
        help="Soft wall-clock cap on the run (default: 6.0).",
    )
    p.add_argument(
        "--max-trials",
        type=int,
        default=30,
        help="Hard cap on worker-returned trials (default: 30).",
    )
    p.add_argument(
        "--codex-workers",
        type=int,
        default=2,
        help="Concurrent codex workers per iteration (default: 2). "
        "Auto-reduces on rate-limit detection. Set to 0 to disable codex pool.",
    )
    p.add_argument(
        "--claude-workers",
        type=int,
        default=0,
        help="Concurrent claude (headless `claude -p`) workers per iteration "
        "(default: 0 = disabled). When > 0, runs in parallel with the codex "
        "pool — both engines propose specs against the same direction.md.",
    )
    p.add_argument(
        "--claude-model",
        type=str,
        default=None,
        help="Optional --model override for claude workers (e.g. "
        "claude-opus-4-7). Default: user's configured model.",
    )
    p.add_argument(
        "--creative-every",
        type=int,
        default=3,
        help="Inject a 'creative mode' framing into direction.md on every "
        "Nth iteration (default: 3). Set 0 to disable.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip codex spawn; use stub workers for end-to-end wiring smoke.",
    )
    p.add_argument(
        "--phase1-gate-script",
        type=Path,
        default=DEFAULT_PHASE1_GATE_SCRIPT,
        help="Path to check_phase1_gates.sh (default: %(default)s).",
    )
    p.add_argument(
        "--skip-gate",
        action="store_true",
        help="DEV ONLY: bypass the Phase 1 startup gate. Warns loudly.",
    )
    p.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Output directory for orchestrator.log and summary.md "
        "(default: research/reports/<today>/).",
    )
    p.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="JSON file to persist the direction-rotation index across runs.",
    )
    p.add_argument(
        "--weekly-cap-pct",
        type=float,
        default=0.40,
        help="Self-imposed weekly Anthropic budget cap as a fraction of Max-20x quota.",
    )
    p.add_argument(
        "--codex-timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help="Per-codex-worker timeout in seconds (default: %(default)d).",
    )
    p.add_argument(
        "--claude-timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help="Per-claude-worker timeout in seconds (default: %(default)d).",
    )
    p.add_argument(
        "--experiments-base",
        type=Path,
        default=Path("research/experiments"),
        help="Where per-worker scratch directories live.",
    )
    p.add_argument(
        "--cost-profile",
        type=str,
        default=None,
        choices=("pessimistic", "live_pm", "zero"),
        help="Cost profile passed to every spawned worker via "
        "RESEARCH_COST_PROFILE env var. Default: each worker's own default "
        "(pessimistic). Use 'live_pm' for realistic Polymarket cost.",
    )
    p.add_argument(
        "--codex-model",
        type=str,
        default=None,
        help="Codex model slug passed to codex workers via '-m <model>' "
        "(e.g. 'gpt-5.3-codex-spark' for the fresh-budget Spark bucket, or "
        "'gpt-5.5' for the smarter/scarcer bucket). Default: codex config.toml.",
    )
    p.add_argument(
        "--claude-prompt",
        type=str,
        default="research/agents/workers/claude_worker_prompt.md",
        help="Prompt file for Claude workers (default: the creative/autonomous "
        "claude_worker_prompt.md, distinct from the codex single-shot prompt).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    report_dir = args.report_dir
    if report_dir is None:
        report_dir = DEFAULT_REPORT_BASE / date.today().isoformat()
    logger = _setup_logging(report_dir)
    cfg = LoopConfig(
        duration_hours=float(args.duration_hours),
        max_trials=int(args.max_trials),
        codex_workers=int(args.codex_workers),
        claude_workers=int(args.claude_workers),
        dry_run=bool(args.dry_run),
        phase1_gate_script=Path(args.phase1_gate_script),
        skip_gate=bool(args.skip_gate),
        report_dir=report_dir,
        state_path=Path(args.state_path),
        weekly_cap_pct=float(args.weekly_cap_pct),
        codex_timeout_sec=int(args.codex_timeout_sec),
        claude_timeout_sec=int(args.claude_timeout_sec),
        experiments_base=Path(args.experiments_base),
        creative_every=int(args.creative_every),
        claude_model=args.claude_model,
        cost_profile=args.cost_profile,
        codex_model=args.codex_model,
        claude_prompt=args.claude_prompt,
    )
    logger.info(
        "starting orchestrator: duration=%.1fh max_trials=%d codex_workers=%d "
        "claude_workers=%d creative_every=%d cost_profile=%s "
        "dry_run=%s skip_gate=%s",
        cfg.duration_hours,
        cfg.max_trials,
        cfg.codex_workers,
        cfg.claude_workers,
        cfg.creative_every,
        cfg.cost_profile or "(default)",
        cfg.dry_run,
        cfg.skip_gate,
    )
    try:
        return run_loop(cfg, logger)
    except KeyboardInterrupt:
        logger.warning("interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
