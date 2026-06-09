"""Tests for the usage-limits parser + budget governor (no binary needed)."""
from __future__ import annotations

import time

from research.agents.usage.limits import (
    GovernorPolicy,
    decide_workers,
    parse_snapshot,
)

_RESET = time.time() + 86400

FULL = {
    "agents": {
        "codex": {"limits": [{"id": "codex", "windows": [
            {"name": "primary", "used_percent": 30.0, "resets_at_epoch": _RESET},
            {"name": "secondary", "used_percent": 50.0, "resets_at_epoch": _RESET},
        ]}]},
        "claude": {"limits": [{"name": "five_hour", "used_percent": 12.0}]},
    }
}
STALE = {"agents": {"codex": {"limits": [{"id": "codex", "windows": [
    {"name": "primary", "used_percent": None},
    {"name": "secondary", "used_percent": None},
]}]}}}


def test_parse_full():
    s = parse_snapshot(FULL)
    assert s.available and s.gpt55_has_data
    assert s.gpt55_5h.used_percent == 30.0
    assert s.gpt55_7d.used_percent == 50.0
    assert s.claude_5h_used == 12.0


def test_parse_stale_is_unknown():
    s = parse_snapshot(STALE)
    assert not s.gpt55_has_data  # None percents => unknown, not zero


def test_parse_empty():
    s = parse_snapshot({})
    assert not s.available and not s.gpt55_has_data


def test_governor_runs_base_when_ample():
    d = decide_workers(parse_snapshot(FULL), base_workers=4)
    assert d.gpt55_workers == 4 and d.sleep_until_epoch is None


def test_governor_optimistic_when_stale():
    d = decide_workers(parse_snapshot(STALE), base_workers=3)
    assert d.gpt55_workers == 3 and d.sleep_until_epoch is None


def test_governor_throttles_when_high():
    raw = {"agents": {"codex": {"limits": [{"id": "codex", "windows": [
        {"name": "primary", "used_percent": 40.0},
        {"name": "secondary", "used_percent": 75.0, "resets_at_epoch": _RESET},
    ]}]}}}
    d = decide_workers(parse_snapshot(raw), base_workers=4)
    assert d.gpt55_workers == 2 and d.sleep_until_epoch is None


def test_governor_pauses_when_tapped():
    raw = {"agents": {"codex": {"limits": [{"id": "codex", "windows": [
        {"name": "primary", "used_percent": 40.0},
        {"name": "secondary", "used_percent": 95.0, "resets_at_epoch": _RESET},
    ]}]}}}
    d = decide_workers(parse_snapshot(raw), base_workers=4)
    assert d.gpt55_workers == 0 and d.sleep_until_epoch == _RESET


def test_governor_caps_at_policy_max():
    d = decide_workers(parse_snapshot(FULL), base_workers=99, policy=GovernorPolicy(max_workers=4))
    assert d.gpt55_workers == 4
