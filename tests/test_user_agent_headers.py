"""Regression guard: every outbound HTTP/WS session must send a browser User-Agent.

Polymarket's Cloudflare 403s the default Python/aiohttp UA, which silently
starved the bot to ~100 markets and 0 arbs. These tests assert the UA is wired
in so that bug can't come back unnoticed.
"""

import re
from pathlib import Path

import pytest

from kalshi_arbitrage.config import Config

PKG = Path(__file__).resolve().parents[1] / "kalshi_arbitrage"

# Files that open aiohttp sessions / websockets.
SESSION_FILES = [
    "api_clients.py",
    "websocket_client.py",
    "kalshi_executor.py",
    "mock_execution.py",
]


def test_config_default_headers_sets_user_agent():
    headers = Config.default_headers()
    assert "User-Agent" in headers
    assert "Mozilla" in headers["User-Agent"]


def test_default_headers_merges_extra_without_dropping_ua():
    merged = Config.default_headers({"KALSHI-ACCESS-KEY": "abc"})
    assert merged["User-Agent"]
    assert merged["KALSHI-ACCESS-KEY"] == "abc"


@pytest.mark.parametrize("filename", SESSION_FILES)
def test_every_clientsession_has_user_agent(filename):
    """No bare aiohttp.ClientSession() — each must pass headers/default_headers."""
    src = (PKG / filename).read_text()
    # Find every ClientSession( ... ) construction and assert it isn't bare.
    for m in re.finditer(r"aiohttp\.ClientSession\(([^)]*)\)", src):
        args = m.group(1)
        assert "headers" in args, (
            f"{filename}: bare aiohttp.ClientSession({args!r}) — must set headers "
            f"(use Config.default_headers()) or Polymarket will 403"
        )


def test_kalshi_auth_headers_include_user_agent():
    """The signed Kalshi header dict must also carry the UA."""
    src = (PKG / "kalshi_executor.py").read_text()
    # The auth-header return dict should reference the UA.
    assert "User-Agent" in src and "HTTP_USER_AGENT" in src
