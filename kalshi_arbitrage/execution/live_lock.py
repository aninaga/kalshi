"""Live-trading arming lock — the mechanism saved for after validation.

The whole system is designed to run end-to-end *up to but not including* the
placement of a real order. Real order placement is the single most dangerous
action, so it sits behind a **deliberate, hard-to-trip-by-accident lock** that
is independent of the ordinary config flags.

A real order can only reach a venue when ALL of these hold:
  1. ``Config.EXECUTION_ENABLED`` is True            (executor runs at all)
  2. ``Config.EXECUTION_MODE == "live"``             (live, not paper)
  3. the live-trading lock is ARMED                  (this module)

Arming is intentionally awkward: you must create an arming file containing an
exact confirmation phrase (or set the env override to that same phrase). Flipping
a config bool is not enough. Until then every real gateway refuses to POST and
the bot runs as a full paper simulation.

CLI::

    python -m kalshi_arbitrage.execution.live_lock status
    python -m kalshi_arbitrage.execution.live_lock arm     # after validation only
    python -m kalshi_arbitrage.execution.live_lock disarm
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Tuple

from ..config import Config

logger = logging.getLogger(__name__)

# The exact phrase the arming file (or env override) must contain. Deliberately
# explicit so it cannot be set by accident or by a stray default.
ARM_PHRASE = "I_HAVE_VALIDATED_AND_ACCEPT_REAL_MONEY_RISK"


class LiveTradingLock:
    """Process-wide gate on REAL order placement (paper is always allowed)."""

    _instance: Optional["LiveTradingLock"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._warned = False

    @classmethod
    def instance(cls) -> "LiveTradingLock":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def arm_file(self) -> str:
        return os.path.join(Config.DATA_DIR, Config.LIVE_TRADING_ARM_FILE)

    def _arm_token_present(self) -> bool:
        """True if the arming file or env override carries the exact phrase."""
        env = os.getenv("ARB_LIVE_TRADING_ARM", "")
        if env.strip() == ARM_PHRASE:
            return True
        try:
            with open(self.arm_file) as fh:
                return ARM_PHRASE in fh.read()
        except OSError:
            return False

    def is_armed(self) -> bool:
        """Real orders are permitted only when fully armed.

        Requires live mode AND the explicit arm token. Returns False (locked)
        in every other situation, including paper mode.
        """
        if not Config.EXECUTION_ENABLED:
            return False
        if Config.EXECUTION_MODE != "live":
            return False
        return self._arm_token_present()

    def assert_or_warn(self, venue: str) -> Tuple[bool, Optional[str]]:
        """For gateways: (armed, reason_if_locked). Logs once when locked."""
        if self.is_armed():
            return True, None
        if not self._warned:
            self._warned = True
            logger.warning(
                "LIVE TRADING LOCKED — real %s orders are blocked. The bot runs "
                "as a paper simulation until you arm the lock (after validation): "
                "create %s containing %r, or set ARB_LIVE_TRADING_ARM to it.",
                venue, self.arm_file, ARM_PHRASE,
            )
        return False, "live_trading_locked"

    # -- explicit operator actions ------------------------------------------ #

    def arm(self) -> None:
        os.makedirs(Config.DATA_DIR, exist_ok=True)
        with open(self.arm_file, "w") as fh:
            fh.write(ARM_PHRASE + "\n")
        self._warned = False
        logger.critical("LIVE TRADING ARMED via %s — real orders are now permitted "
                        "when EXECUTION_MODE=live.", self.arm_file)

    def disarm(self) -> None:
        try:
            if os.path.exists(self.arm_file):
                os.remove(self.arm_file)
        except OSError as exc:
            logger.error("Could not remove arm file: %s", exc)
        self._warned = False
        logger.warning("Live trading DISARMED — real orders blocked again.")

    def status(self) -> str:
        parts = [
            f"EXECUTION_ENABLED={Config.EXECUTION_ENABLED}",
            f"EXECUTION_MODE={Config.EXECUTION_MODE!r}",
            f"arm_token_present={self._arm_token_present()}",
            f"=> ARMED={self.is_armed()}",
        ]
        return " | ".join(parts)


def main(argv=None) -> int:  # pragma: no cover - thin CLI
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    lock = LiveTradingLock.instance()
    cmd = argv[0] if argv else "status"
    if cmd == "arm":
        lock.arm()
    elif cmd == "disarm":
        lock.disarm()
    print(lock.status())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
