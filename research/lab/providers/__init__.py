"""research.lab.providers — registry of MarketDataProvider implementations.

One provider per event-class FAMILY (nba, weather, ...). The family name is
also the governance partition key: ``lab.governance`` deflates the Sharpe
hurdle per family, so independent searches in independent event classes do not
inflate each other's multiple-testing N.

Built-in providers are imported LAZILY (first ``get``/``for_market`` call), so
importing this package stays cheap and safe under the test suite's
``research.lab.*`` module purge (see ``research/lab/conftest.py``).

Public API::

    register(provider)        -> provider     # add/override an instance
    get(family)               -> provider     # lazy-instantiates builtins
    for_market(market)        -> provider     # which family owns this market
    families()                -> list[str]    # registered + builtin names
    family_of(market)         -> str | None   # convenience: family name only
"""
from __future__ import annotations

import importlib
from typing import Optional

# family -> (module, class) — instantiated on first use. Order matters for
# for_market resolution when two families ever share a market string (they
# should not): explicit registrations win, then builtins in this order.
_BUILTINS: dict = {
    "nba": ("research.lab.providers.nba", "NBAProvider"),
    "weather": ("research.lab.providers.weather", "WeatherProvider"),
    "crypto": ("research.lab.providers.crypto", "CryptoProvider"),
}

# family -> provider instance (explicit registrations + materialized builtins)
_instances: dict = {}


def register(provider):
    """Register (or override) a provider instance under ``provider.family``."""
    fam = getattr(provider, "family", None)
    if not fam or not isinstance(fam, str):
        raise ValueError("provider must define a non-empty string .family")
    _instances[fam] = provider
    return provider


def families() -> list:
    """All known family names (explicitly registered + builtin), sorted."""
    return sorted(set(_instances) | set(_BUILTINS))


def get(family: str):
    """Return the provider for ``family``, lazily building a builtin."""
    if family in _instances:
        return _instances[family]
    if family in _BUILTINS:
        mod_name, cls_name = _BUILTINS[family]
        mod = importlib.import_module(mod_name)
        prov = getattr(mod, cls_name)()
        _instances[family] = prov
        return prov
    raise KeyError(
        f"unknown provider family {family!r}; known: {families()}")


def for_market(market: str):
    """Resolve which family's provider owns the ``market`` string.

    Explicit registrations are checked first (in registration order), then
    builtins (in declaration order). Raises ``ValueError`` for an unknown
    market — mirroring the historical ``data.load_panel`` behavior.
    """
    seen = []
    for fam in list(_instances) + [f for f in _BUILTINS if f not in _instances]:
        try:
            prov = get(fam)
        except Exception:  # noqa: BLE001 — a broken provider must not mask others
            continue
        seen.append(fam)
        if market in getattr(prov, "markets", ()):
            return prov
    raise ValueError(
        f"unknown market {market!r}; no provider in families {seen} owns it")


def family_of(market: str) -> Optional[str]:
    """The family name owning ``market``, or ``None`` if unowned."""
    try:
        return for_market(market).family
    except ValueError:
        return None
