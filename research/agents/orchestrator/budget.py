"""Session-budget enforcer for the Opus 4.7 orchestrator.

The orchestrator imposes a soft cap on Claude (Anthropic) token usage so that
an overnight autoresearch run cannot consume the entire Max-20x weekly quota
and starve other projects of capacity. The cap here is *self-imposed* —
Anthropic's actual rate limits sit above this number and constitute the hard
ceiling; this module's job is to stop the loop early when we cross the soft
line.

Important: Claude Code's interactive sessions do NOT expose actual billed
token counts to Python code. The budget is therefore HEURISTIC. Callers pass
their own estimates to :func:`Budget.record_usage`. The defaults assume:

    tokens_per_reviewer_call           = 50_000   (Opus extended-thinking review)
    tokens_per_orchestrator_overhead   =  5_000   (per trial routed)
    weekly_max_budget_tokens           = 5_000_000

So a 40% weekly cap = 2_000_000 tokens ~= 40 reviewer calls + 400 trials of
orchestrator overhead. These are rough; the operator can tune by editing the
defaults at the bottom of this module or passing different values into the
:class:`Budget` constructor.

Codex worker usage rides on the ChatGPT-Pro subscription, NOT Anthropic's. We
track ``codex_calls`` here for awareness/rate-limit-probing, but they do NOT
count against the Anthropic budget.

The ledger format is JSON lines, one row per ``record_usage`` call:

    {"ts": 1748400000.0, "tokens": 50000, "codex_calls": 0, "label": "review:abcd1234"}

Lines older than 7 days are simply ignored at read time (the rolling 7-day
window aligns with Max-20x's weekly reset cadence). We never rewrite the file
on read; old rows accumulate harmlessly. A maintenance pass can prune later if
the file ever grows large.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LEDGER_PATH = _REPO_ROOT / "market_data" / "budget_ledger.jsonl"

# Conservative assumption about Max-20x weekly token budget. The user can
# override by passing ``weekly_max_budget_tokens`` to :class:`Budget` if their
# actual quota differs.
DEFAULT_WEEKLY_MAX_BUDGET_TOKENS = 5_000_000

# Default soft caps as a fraction of ``weekly_max_budget_tokens``.
DEFAULT_WEEKLY_CAP_PCT = 0.40
DEFAULT_DAILY_CAP_PCT = 0.20

# Rolling-window lengths in seconds.
_ONE_DAY_SEC = 24 * 60 * 60
_SEVEN_DAYS_SEC = 7 * _ONE_DAY_SEC


# --------------------------------------------------------------------------- #
# State snapshot
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BudgetState:
    """A point-in-time snapshot of budget usage.

    Attributes are pure scalars so callers can log/serialise them without
    pulling in this module's logic.
    """

    rolling_token_total: int
    """Estimated Anthropic tokens spent in the rolling 7-day window."""

    rolling_token_total_24h: int
    """Same, but for the rolling 24-hour window."""

    rolling_codex_calls: int
    """Codex/ChatGPT-Pro calls made in the rolling 7-day window."""

    session_start_ts: float
    """When this :class:`Budget` instance was constructed."""

    daily_cap_pct: float
    """Soft cap as a fraction of ``weekly_max_budget_tokens``, day window."""

    weekly_cap_pct: float
    """Soft cap as a fraction of ``weekly_max_budget_tokens``, week window."""

    weekly_max_budget_tokens: int
    """The Max-20x weekly token quota assumed by this budget."""


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


class Budget:
    """Self-imposed session budget for the orchestrator.

    The ledger is append-only JSONL on disk. On read, lines older than 7 days
    are skipped — that gives a true rolling window without ever rewriting the
    file. A restarted orchestrator will see prior usage and respect it.

    Thread/process safety: callers should treat one :class:`Budget` as
    single-threaded. The orchestrator only uses it from its main loop.
    """

    def __init__(
        self,
        ledger_path: Path | str | None = None,
        weekly_cap_pct: float = DEFAULT_WEEKLY_CAP_PCT,
        daily_cap_pct: float = DEFAULT_DAILY_CAP_PCT,
        weekly_max_budget_tokens: int = DEFAULT_WEEKLY_MAX_BUDGET_TOKENS,
    ) -> None:
        if ledger_path is None:
            self._ledger_path = DEFAULT_LEDGER_PATH
        else:
            self._ledger_path = Path(ledger_path)
        if not (0.0 < weekly_cap_pct <= 1.0):
            raise ValueError(
                f"weekly_cap_pct must be in (0, 1], got {weekly_cap_pct!r}"
            )
        if not (0.0 < daily_cap_pct <= 1.0):
            raise ValueError(
                f"daily_cap_pct must be in (0, 1], got {daily_cap_pct!r}"
            )
        if weekly_max_budget_tokens <= 0:
            raise ValueError(
                f"weekly_max_budget_tokens must be > 0, got {weekly_max_budget_tokens!r}"
            )
        self._weekly_cap_pct = float(weekly_cap_pct)
        self._daily_cap_pct = float(daily_cap_pct)
        self._weekly_max = int(weekly_max_budget_tokens)
        self._session_start_ts = time.time()

        # Ensure the parent directory exists so the first append doesn't error.
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------- #
    # Public API
    # ------------------------------------------------------------------- #

    @property
    def weekly_cap_tokens(self) -> int:
        """Absolute soft cap (tokens) for the rolling 7-day window."""
        return int(self._weekly_max * self._weekly_cap_pct)

    @property
    def daily_cap_tokens(self) -> int:
        """Absolute soft cap (tokens) for the rolling 24-hour window."""
        return int(self._weekly_max * self._daily_cap_pct)

    def would_exceed_daily(self, next_tokens_estimate: int) -> bool:
        """True if adding ``next_tokens_estimate`` would cross the 24h cap."""
        if next_tokens_estimate < 0:
            raise ValueError(
                f"next_tokens_estimate must be >= 0, got {next_tokens_estimate!r}"
            )
        current = self._sum_tokens_in_window(_ONE_DAY_SEC)
        return (current + next_tokens_estimate) > self.daily_cap_tokens

    def would_exceed_weekly(self, next_tokens_estimate: int) -> bool:
        """True if adding ``next_tokens_estimate`` would cross the 7-day cap."""
        if next_tokens_estimate < 0:
            raise ValueError(
                f"next_tokens_estimate must be >= 0, got {next_tokens_estimate!r}"
            )
        current = self._sum_tokens_in_window(_SEVEN_DAYS_SEC)
        return (current + next_tokens_estimate) > self.weekly_cap_tokens

    def record_usage(
        self,
        tokens: int,
        codex_calls: int = 0,
        label: str = "",
    ) -> None:
        """Append a usage row to the ledger.

        Parameters
        ----------
        tokens:
            Estimated Anthropic tokens used by the action being recorded.
            Must be >= 0. Codex worker usage does NOT count here — pass 0
            for ``tokens`` when only Codex was involved.
        codex_calls:
            Number of Codex/ChatGPT-Pro invocations to attribute to this row.
            Tracked for rate-limit awareness, not budget enforcement.
        label:
            Free-form provenance tag, e.g. ``"review:<spec_hash>"`` or
            ``"orchestrator_overhead"``.
        """
        if tokens < 0:
            raise ValueError(f"tokens must be >= 0, got {tokens!r}")
        if codex_calls < 0:
            raise ValueError(f"codex_calls must be >= 0, got {codex_calls!r}")

        row = {
            "ts": time.time(),
            "tokens": int(tokens),
            "codex_calls": int(codex_calls),
            "label": str(label),
        }
        line = json.dumps(row, separators=(",", ":"))
        # Open-append is atomic at the OS level for short writes on POSIX;
        # multiple concurrent orchestrators are not expected.
        with self._ledger_path.open("a") as fh:
            fh.write(line + "\n")

    def status(self) -> BudgetState:
        """Snapshot of current usage."""
        rolling_week = self._sum_tokens_in_window(_SEVEN_DAYS_SEC)
        rolling_day = self._sum_tokens_in_window(_ONE_DAY_SEC)
        rolling_codex = self._sum_codex_in_window(_SEVEN_DAYS_SEC)
        return BudgetState(
            rolling_token_total=rolling_week,
            rolling_token_total_24h=rolling_day,
            rolling_codex_calls=rolling_codex,
            session_start_ts=self._session_start_ts,
            daily_cap_pct=self._daily_cap_pct,
            weekly_cap_pct=self._weekly_cap_pct,
            weekly_max_budget_tokens=self._weekly_max,
        )

    # ------------------------------------------------------------------- #
    # Internals
    # ------------------------------------------------------------------- #

    def _iter_ledger(self) -> list[dict]:
        """Read all rows from the ledger file, skipping malformed lines.

        We tolerate malformed JSON because a half-written line at the tail
        (e.g., from a crashed process) should not poison the whole budget;
        the worst case is we under-count slightly, which fails open in the
        wrong direction. Logging is the caller's job.
        """
        if not self._ledger_path.exists():
            return []
        rows: list[dict] = []
        with self._ledger_path.open("r") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                rows.append(obj)
        return rows

    def _sum_tokens_in_window(self, window_sec: int) -> int:
        """Sum ``tokens`` across rows whose ``ts`` is within ``window_sec``
        of now.

        Rows missing a numeric ``ts`` or ``tokens`` field are skipped.
        """
        now = time.time()
        cutoff = now - window_sec
        total = 0
        for row in self._iter_ledger():
            ts = row.get("ts")
            tk = row.get("tokens")
            if not isinstance(ts, (int, float)):
                continue
            if not isinstance(tk, (int, float)):
                continue
            if ts < cutoff:
                continue
            # Future timestamps (clock skew) — clamp to 0 weight to avoid
            # crediting "the future" against the cap.
            if ts > now + 60:
                continue
            total += int(tk)
        return total

    def _sum_codex_in_window(self, window_sec: int) -> int:
        now = time.time()
        cutoff = now - window_sec
        total = 0
        for row in self._iter_ledger():
            ts = row.get("ts")
            cc = row.get("codex_calls")
            if not isinstance(ts, (int, float)):
                continue
            if not isinstance(cc, (int, float)):
                continue
            if ts < cutoff or ts > now + 60:
                continue
            total += int(cc)
        return total


# --------------------------------------------------------------------------- #
# Module-level conveniences
# --------------------------------------------------------------------------- #


# Heuristic token estimates the orchestrator uses when calling
# ``would_exceed_*``. Callers can import these or pass their own numbers.
TOKENS_PER_REVIEWER_CALL = 50_000
TOKENS_PER_ORCHESTRATOR_OVERHEAD = 5_000


__all__ = [
    "Budget",
    "BudgetState",
    "DEFAULT_LEDGER_PATH",
    "DEFAULT_WEEKLY_MAX_BUDGET_TOKENS",
    "TOKENS_PER_ORCHESTRATOR_OVERHEAD",
    "TOKENS_PER_REVIEWER_CALL",
]
