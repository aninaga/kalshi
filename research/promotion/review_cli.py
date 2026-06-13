"""Human-in-the-loop promotion CLI.

After the autoresearch system finds a trial passing the scorer gate AND
Claude has written an adversarial review, the user reviews it here and
decides whether to BURN one of the 5 lifetime test-set unlocks to verify.

Usage::

    python -m research.promotion.review_cli --list
    python -m research.promotion.review_cli --inspect <spec_hash>
    python -m research.promotion.review_cli --burn-unlock <spec_hash> [--yes]
    python -m research.promotion.review_cli --unlock-budget
    python -m research.promotion.review_cli --history
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from research.registry.api import (
    TrialRow,
    query,
    query_pending_promotion,
    query_promoted,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Location of the test-unlock log relative to the project root.
_TEST_UNLOCKS_LOG = Path("market_data/test_unlocks.log")

# Total lifetime test-set unlock budget.
_MAX_UNLOCKS = 5

# Cost profiles run_backtest accepts (see research/harness/cost_profile.py).
_COST_PROFILE_CHOICES = (
    "pessimistic", "live_pm", "zero", "calibrated_pm", "official_2026",
)

# Default profile for the sanctioned test-set burn: 'official_2026' is the
# profile wired to the official venue fee schedules (venue_fees rates —
# parabolic per-category PM taker fee, NO flat piece, full Kalshi curve).
# The legacy 'pessimistic' profile charges a fictional 2% flat PM fee; burning
# one of the 5 lifetime unlocks under fictional costs would waste it.
_DEFAULT_BURN_COST_PROFILE = "official_2026"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_ts(ts: int) -> str:
    """Convert a unix timestamp to a short ISO-8601 string (no seconds)."""
    try:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def _count_burns(log_path: Path) -> int:
    """Count rows containing the word 'burn' in the unlock log."""
    if not log_path.exists():
        return 0
    count = 0
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Each row: <iso_ts> <spec_hash> <attempt|burn> <reason>
            parts = stripped.split()
            if len(parts) >= 3 and parts[2].lower() == "burn":
                count += 1
    return count


def _remaining_unlocks(log_path: Path = _TEST_UNLOCKS_LOG) -> int:
    return _MAX_UNLOCKS - _count_burns(log_path)


def _append_attempt_row(
    spec_hash: str,
    log_path: Path = _TEST_UNLOCKS_LOG,
) -> None:
    """Append a non-burn ``attempt`` row recording that this CLI initiated
    a sanctioned test run.

    Deliberately NOT a ``burn`` row: the single canonical burn is written by
    ``research.agents.tools.run_backtest`` (the security boundary) into the
    same log (we pass ``--unlock-log``). Writing ``burn`` here too would
    double-count one sanctioned run against the 5-lifetime budget.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} {spec_hash} attempt promotion_cli\n")


def _find_trial_by_hash(spec_hash: str, db_path: Path | str | None = None) -> TrialRow | None:
    """Return the most recent trial matching spec_hash, or None."""
    rows = query(spec_hash=spec_hash, limit=1, db_path=db_path)
    return rows[0] if rows else None


def _print_table(rows: list[dict[str, Any]], columns: list[str], widths: list[int]) -> None:
    """Print a minimal fixed-width table without rich."""
    # Header
    header = "  ".join(str(c).ljust(w) for c, w in zip(columns, widths))
    print(header)
    print("-" * len(header))
    for row in rows:
        line = "  ".join(str(row.get(c, "")).ljust(w) for c, w in zip(columns, widths))
        print(line)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_list(db_path: Path | str | None = None) -> int:
    """Print all promotable candidates (gate=1, claude_review set, test_pnl_net null)."""
    candidates = query_pending_promotion(db_path=db_path)
    if not candidates:
        print(
            "no candidates pending promotion "
            "(gate_passed=1 AND claude_review IS NOT NULL AND test_pnl_net IS NULL)"
        )
        return 0

    columns = ["ID", "NAME", "SPEC_HASH", "VAL_SHARPE", "VAL_N_TRADES", "CLAUDE_REVIEWED", "TS_CREATED"]
    widths = [5, 35, 16, 10, 12, 15, 18]

    rows: list[dict[str, Any]] = []
    for t in candidates:
        short_hash = t.spec_hash[:16] + "..."
        sharpe_str = (
            f"+{t.val_sharpe_net:.3f}" if t.val_sharpe_net is not None and t.val_sharpe_net >= 0
            else f"{t.val_sharpe_net:.3f}" if t.val_sharpe_net is not None
            else "N/A"
        )
        rows.append({
            "ID": str(t.id),
            "NAME": t.spec_name,
            "SPEC_HASH": short_hash,
            "VAL_SHARPE": sharpe_str,
            "VAL_N_TRADES": str(t.val_n_trades) if t.val_n_trades is not None else "N/A",
            "CLAUDE_REVIEWED": "yes" if t.claude_review else "no",
            "TS_CREATED": _format_ts(t.ts_created),
        })

    _print_table(rows, columns, widths)
    return 0


def cmd_inspect(spec_hash: str, db_path: Path | str | None = None) -> int:
    """Print full detail for a trial by spec_hash."""
    trial = _find_trial_by_hash(spec_hash, db_path=db_path)
    if trial is None:
        print(f"not found: spec_hash={spec_hash!r}", file=sys.stderr)
        return 1

    # 1. Trial summary
    print(f"=== TRIAL SUMMARY ===")
    print(f"  id:         {trial.id}")
    print(f"  name:       {trial.spec_name}")
    print(f"  ts_created: {_format_ts(trial.ts_created)}")
    print(f"  agent_id:   {trial.agent_id}")
    print()

    # Pull full-row columns we need (spec_json, mechanistic_writeup, scorer fields)
    # These aren't in TrialRow — we need a raw query.
    from research.registry.db import connect as _db_connect
    conn = _db_connect(db_path)
    try:
        raw = conn.execute(
            "SELECT spec_json, mechanistic_writeup, "
            "       scorer_block_bootstrap_ci_lo, scorer_block_bootstrap_ci_hi, "
            "       scorer_dsr, scorer_dsr_pvalue, scorer_promotion_gate_passed, "
            "       scorer_promotion_gate_reasons, spec_name "
            "FROM trials WHERE spec_hash = ? "
            "ORDER BY ts_created DESC, id DESC LIMIT 1;",
            (spec_hash,),
        ).fetchone()
    finally:
        conn.close()

    if raw is None:
        print(f"not found in db: spec_hash={spec_hash!r}", file=sys.stderr)
        return 1

    # 2. Spec JSON
    spec_json_str = raw["spec_json"]
    print("=== SPEC JSON ===")
    try:
        parsed = json.loads(spec_json_str)
        print(json.dumps(parsed, indent=2, sort_keys=True))
    except (json.JSONDecodeError, TypeError):
        print(spec_json_str)
    print()

    # 3. Mechanistic writeup
    print("=== MECHANISTIC WRITEUP ===")
    writeup = raw["mechanistic_writeup"]
    print(writeup if writeup else "(none)")
    print()

    # 4. Scorer report
    print("=== SCORER REPORT ===")
    ci_lo = raw["scorer_block_bootstrap_ci_lo"]
    ci_hi = raw["scorer_block_bootstrap_ci_hi"]
    dsr = raw["scorer_dsr"]
    dsr_p = raw["scorer_dsr_pvalue"]
    gate = raw["scorer_promotion_gate_passed"]
    reasons_json = raw["scorer_promotion_gate_reasons"]
    ci_str = (
        f"[{ci_lo:.4f}, {ci_hi:.4f}]"
        if ci_lo is not None and ci_hi is not None
        else "N/A"
    )
    print(f"  block_bootstrap_CI: {ci_str}")
    print(f"  DSR:                {dsr:.4f}" if dsr is not None else "  DSR:                N/A")
    print(f"  DSR p-value:        {dsr_p:.4f}" if dsr_p is not None else "  DSR p-value:        N/A")
    print(f"  gate_passed:        {bool(gate) if gate is not None else 'N/A'}")
    if reasons_json:
        try:
            reasons = json.loads(reasons_json)
            print(f"  reasons:            {reasons}")
        except (json.JSONDecodeError, TypeError):
            print(f"  reasons:            {reasons_json}")
    else:
        print("  reasons:            (none)")
    print()

    # 5. Claude review
    print("=== CLAUDE REVIEW ===")
    print(trial.claude_review if trial.claude_review else "(no review yet)")
    print()

    # 6. Sibling trials
    siblings = query(spec_hash=spec_hash, limit=50, db_path=db_path)
    if len(siblings) > 1:
        print("=== SIBLING TRIALS (same spec_name) ===")
        print(f"  {'id':>6}  {'gate_passed':>11}  {'val_sharpe_net':>14}")
        print(f"  {'------':>6}  {'-----------':>11}  {'--------------':>14}")
        for s in siblings:
            gp = str(s.scorer_promotion_gate_passed) if s.scorer_promotion_gate_passed is not None else "None"
            sh = (
                f"+{s.val_sharpe_net:.4f}" if s.val_sharpe_net is not None and s.val_sharpe_net >= 0
                else f"{s.val_sharpe_net:.4f}" if s.val_sharpe_net is not None
                else "N/A"
            )
            print(f"  {s.id:>6}  {gp:>11}  {sh:>14}")
        print()

    return 0


def cmd_unlock_budget(log_path: Path = _TEST_UNLOCKS_LOG) -> int:
    """Print remaining unlock budget."""
    remaining = _remaining_unlocks(log_path)
    print(f"Remaining unlocks: {remaining} / {_MAX_UNLOCKS}")
    return 0


def cmd_history(db_path: Path | str | None = None) -> int:
    """Print all promoted trials (test_pnl_net IS NOT NULL)."""
    rows = query_promoted(db_path=db_path)
    if not rows:
        print("no promoted trials yet (test_pnl_net IS NULL for all rows)")
        return 0

    columns = ["ID", "NAME", "SPEC_HASH", "TEST_PNL_NET", "TEST_SHARPE", "TS_CREATED"]
    widths = [5, 35, 16, 12, 12, 18]

    table_rows: list[dict[str, Any]] = []
    for t in rows:
        short_hash = t.spec_hash[:16] + "..."
        pnl_str = (
            f"+{t.test_pnl_net:.4f}" if t.test_pnl_net is not None and t.test_pnl_net >= 0
            else f"{t.test_pnl_net:.4f}" if t.test_pnl_net is not None
            else "N/A"
        )
        table_rows.append({
            "ID": str(t.id),
            "NAME": t.spec_name,
            "SPEC_HASH": short_hash,
            "TEST_PNL_NET": pnl_str,
            "TEST_SHARPE": "N/A",  # not in TrialRow; kept for shape
            "TS_CREATED": _format_ts(t.ts_created),
        })

    _print_table(table_rows, columns, widths)
    return 0


def cmd_burn_unlock(
    spec_hash: str,
    yes: bool = False,
    log_path: Path = _TEST_UNLOCKS_LOG,
    db_path: Path | str | None = None,
    cost_profile: str = _DEFAULT_BURN_COST_PROFILE,
) -> int:
    """Interactive: review and burn one test-set unlock.

    The burn row itself is written by the ``run_backtest`` subprocess (the
    single canonical burn-writer); this command records an ``attempt`` row.
    The backtest runs under ``cost_profile`` (default ``official_2026`` —
    the official venue_fees rates), not the fictional ``pessimistic`` default.
    """

    # 1. Budget check.
    remaining = _remaining_unlocks(log_path)
    if remaining <= 0:
        print(
            "ERROR: no unlock budget remaining (0 / 5). "
            "All 5 lifetime test-set unlocks have been spent.",
            file=sys.stderr,
        )
        return 1

    # 2. Find the trial.
    trial = _find_trial_by_hash(spec_hash, db_path=db_path)
    if trial is None:
        print(f"ERROR: spec_hash not found: {spec_hash!r}", file=sys.stderr)
        return 1

    # Extract "Recommendation" line from claude_review if present.
    rec_line = "(no review)"
    if trial.claude_review:
        for line in trial.claude_review.splitlines():
            if "recommendation" in line.lower():
                rec_line = line.strip()
                break
        else:
            # No Recommendation line found; show first non-empty line
            for line in trial.claude_review.splitlines():
                if line.strip():
                    rec_line = line.strip()[:120]
                    break

    # 3. Summary print.
    print()
    print("=== BURN UNLOCK CONFIRMATION ===")
    print(f"  Spec name:          {trial.spec_name}")
    print(f"  Spec hash:          {spec_hash}")
    print(f"  Reviewer said:      {rec_line}")
    print(f"  Cost profile:       {cost_profile}")
    print(f"  Unlocks remaining:  {remaining} / {_MAX_UNLOCKS}")
    print()
    print("This will run run_backtest on the TEST split. This action is IRREVERSIBLE.")
    print()

    # 4. Interactive confirmation.
    if not yes:
        try:
            answer = input("Type 'BURN' to confirm: ").strip()
        except EOFError:
            answer = ""
        if answer != "BURN":
            print("Cancelled (did not type 'BURN').")
            return 1

    # 5. Compute unlock token and write spec to tempfile.
    from research.registry.db import connect as _db_connect
    conn = _db_connect(db_path)
    try:
        raw = conn.execute(
            "SELECT spec_json FROM trials WHERE spec_hash = ? "
            "ORDER BY ts_created DESC, id DESC LIMIT 1;",
            (spec_hash,),
        ).fetchone()
    finally:
        conn.close()

    if raw is None:
        print(f"ERROR: spec_json not found for spec_hash={spec_hash!r}", file=sys.stderr)
        return 1

    spec_json_str = raw["spec_json"]
    unlock_token = hashlib.sha256(spec_json_str.encode("utf-8")).hexdigest()

    # Write spec to temp file.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(spec_json_str)
        spec_file = tf.name

    # 6. Shell out to run_backtest. We pass --unlock-log so the subprocess —
    # the single canonical burn-writer — appends its ONE burn row to the same
    # log this CLI counts; and --cost-profile so the burn does not execute
    # under the fictional 'pessimistic' default.
    print(
        f"Running backtest on test split "
        f"(token={unlock_token[:16]}..., cost_profile={cost_profile})..."
    )
    cmd = [
        sys.executable,
        "-m", "research.agents.tools.run_backtest",
        "--spec-file", spec_file,
        "--split", "test",
        "--unlock-test-token", unlock_token,
        "--agent-id", "promotion_cli",
        "--unlock-log", str(log_path),
        "--cost-profile", cost_profile,
    ]
    if db_path is not None:
        cmd += ["--registry-db", str(db_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: run_backtest failed (exit {exc.returncode}):", file=sys.stderr)
        print(exc.stderr, file=sys.stderr)
        return 1
    finally:
        # Always clean up the temp file.
        try:
            Path(spec_file).unlink()
        except OSError:
            pass

    # 7. Parse stdout JSON and extract metrics.
    stdout_text = result.stdout.strip()
    test_pnl_net: float | None = None
    test_sharpe_net: float | None = None
    test_n_trades: int | None = None
    try:
        out = json.loads(stdout_text)
        test_pnl_net = out.get("test_pnl_net") or out.get("pnl_net")
        test_sharpe_net = out.get("test_sharpe_net") or out.get("sharpe_net")
        test_n_trades = out.get("test_n_trades") or out.get("n_trades")
    except (json.JSONDecodeError, TypeError, AttributeError):
        # Fallback: print raw output and leave metrics as None.
        print("(run_backtest stdout was not parseable JSON; raw output follows)")
        print(stdout_text)

    # 8. Append the promotion-CLI *attempt* row. The burn row was already
    # written by the run_backtest subprocess (the single canonical burn-writer)
    # into the same log, so the remaining-count below reflects exactly one
    # burned unlock for this sanctioned run.
    _append_attempt_row(spec_hash, log_path=log_path)
    new_remaining = _remaining_unlocks(log_path)

    # 9. Print test result summary.
    pnl_str = f"+{test_pnl_net:.4f}" if test_pnl_net is not None and test_pnl_net >= 0 else (
        f"{test_pnl_net:.4f}" if test_pnl_net is not None else "N/A"
    )
    sharpe_str = f"+{test_sharpe_net:.3f}" if test_sharpe_net is not None and test_sharpe_net >= 0 else (
        f"{test_sharpe_net:.3f}" if test_sharpe_net is not None else "N/A"
    )
    n_str = str(test_n_trades) if test_n_trades is not None else "N/A"
    print()
    print(
        f"promoted: test_pnl_net={pnl_str}, "
        f"test_sharpe={sharpe_str}, "
        f"n_trades={n_str}. "
        f"Unlocks remaining: {new_remaining}."
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m research.promotion.review_cli",
        description="Human-in-the-loop promotion CLI for the NBA autoresearch system.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list",
        dest="cmd_list",
        action="store_true",
        help="List all promotable candidates (gate=1, claude_review set, test_pnl_net null).",
    )
    group.add_argument(
        "--inspect",
        metavar="SPEC_HASH",
        help="Show full detail for a trial: spec JSON, writeup, scorer report, claude_review.",
    )
    group.add_argument(
        "--burn-unlock",
        metavar="SPEC_HASH",
        help="Interactive: burn one test-set unlock to verify a trial.",
    )
    group.add_argument(
        "--unlock-budget",
        dest="cmd_unlock_budget",
        action="store_true",
        help="Show remaining unlock budget.",
    )
    group.add_argument(
        "--history",
        dest="cmd_history",
        action="store_true",
        help="Show all promoted trials (test_pnl_net IS NOT NULL).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip confirmation prompt for --burn-unlock (for scripting/testing).",
    )
    p.add_argument(
        "--db-path",
        default=None,
        help="Override the registry DB path (default: market_data/trials.db).",
    )
    p.add_argument(
        "--log-path",
        default=None,
        help="Override the test_unlocks.log path (default: market_data/test_unlocks.log).",
    )
    p.add_argument(
        "--cost-profile",
        default=_DEFAULT_BURN_COST_PROFILE,
        choices=_COST_PROFILE_CHOICES,
        help="Cost profile for the sanctioned --burn-unlock backtest. "
        f"Default: {_DEFAULT_BURN_COST_PROFILE!r} — the official venue fee "
        "schedules (venue_fees rates: parabolic per-category Polymarket "
        "taker fee, no flat piece, full Kalshi curve), i.e. the most "
        "realistic built-in profile. 'calibrated_pm' additionally uses "
        "real-trade-calibrated book depth. 'pessimistic' charges a "
        "fictional 2%% flat PM fee — do not burn an unlock under it.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db_path) if args.db_path else None
    log_path = Path(args.log_path) if args.log_path else _TEST_UNLOCKS_LOG

    if args.cmd_list:
        return cmd_list(db_path=db_path)
    if args.inspect:
        return cmd_inspect(args.inspect, db_path=db_path)
    if args.burn_unlock:
        return cmd_burn_unlock(
            args.burn_unlock,
            yes=args.yes,
            log_path=log_path,
            db_path=db_path,
            cost_profile=args.cost_profile,
        )
    if args.cmd_unlock_budget:
        return cmd_unlock_budget(log_path=log_path)
    if args.cmd_history:
        return cmd_history(db_path=db_path)

    # Should be unreachable — argparse enforces mutual exclusion.
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
