"""Global execution kill switch (Phase B).

A single place every order-placement path consults before firing. It trips on
any of:

  * ``Config.EXECUTION_ENABLED is False``         (master flag)
  * a sentinel file under ``Config.DATA_DIR``      (operator / out-of-band halt)
  * an in-process flag                             (set by the circuit breaker
                                                    or on an unwind failure)

Operator controls (``execution/operator.py``) write/remove the sentinel file so
a halt survives even if the process is restarted.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from ..config import Config

logger = logging.getLogger(__name__)


class KillSwitch:
    """Process-wide halt for all order placement."""

    _instance: Optional["KillSwitch"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._tripped = False
        self._reason: Optional[str] = None
        self._tripped_at: Optional[float] = None

    @classmethod
    def instance(cls) -> "KillSwitch":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def sentinel_path(self) -> str:
        return os.path.join(Config.DATA_DIR, Config.EXECUTION_HALT_SENTINEL)

    def is_active(self) -> tuple:
        """Return (active: bool, reason: Optional[str])."""
        if not Config.EXECUTION_ENABLED:
            return True, "execution_disabled"
        if self._tripped:
            return True, self._reason or "tripped"
        if os.path.exists(self.sentinel_path):
            return True, "halt_sentinel_present"
        return False, None

    def trip(self, reason: str) -> None:
        """Trip the in-process flag AND drop the sentinel file (durable halt)."""
        self._tripped = True
        self._reason = reason
        self._tripped_at = time.time()
        try:
            os.makedirs(Config.DATA_DIR, exist_ok=True)
            with open(self.sentinel_path, "w") as fh:
                fh.write(f"{reason}\n{time.time()}\n")
        except OSError as exc:
            logger.error("Could not write halt sentinel: %s", exc)
        logger.critical("EXECUTION KILL SWITCH TRIPPED: %s", reason)

    def reset(self) -> None:
        """Clear the in-process flag and remove the sentinel (operator action)."""
        self._tripped = False
        self._reason = None
        self._tripped_at = None
        try:
            if os.path.exists(self.sentinel_path):
                os.remove(self.sentinel_path)
        except OSError as exc:
            logger.error("Could not remove halt sentinel: %s", exc)
        logger.warning("Execution kill switch reset")
