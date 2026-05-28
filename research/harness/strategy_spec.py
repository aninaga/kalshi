"""StrategySpec — the machine-readable strategy contract.

Every backtest, every Codex worker output, and every promotion candidate
is described by an instance of ``StrategySpec``. The shape of this contract
is therefore the public API for the rest of Wave 1+: keep edits backward
compatible or migrate everything that depends on it.

Three jobs, in priority order:

1. **Live-safety**: a spec referencing a feature with ``is_live_safe=False``
   (e.g. ESPN's leakage canary ``espn_home_winprob``) is rejected at
   ``validate()`` time. Same for expressions that read features not
   declared in ``self.features`` — every dependency must be explicit.

2. **Expression safety**: ``Condition.expr`` is a tiny DSL evaluated against
   a context dict at runtime. Because Codex workers generate these strings,
   ``eval`` is locked down behind an AST allowlist (no attribute access, no
   imports, no comprehensions, no lambdas, no arbitrary calls — only the
   whitelisted helpers ``abs/min/max/sign``).

3. **Deterministic identity**: ``spec_hash()`` is derived from a canonical
   JSON serialisation, so two equivalent specs always produce the same hash
   regardless of field declaration order.

If ``simpleeval`` is installed it is preferred at evaluation time; the AST
validator runs unconditionally either way. If not, we fall back to Python's
built-in ``eval`` with a tightly scoped namespace.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional

from research.features.registry import REGISTRY


# --------------------------------------------------------------------------- #
# Expression sandbox
# --------------------------------------------------------------------------- #

# Whitelist of AST node types that may appear inside a ``Condition.expr``.
# Everything else (Attribute, Subscript, Lambda, Import, FunctionDef, ...) is
# refused. Python's stdlib ast module exposes these as classes.
_ALLOWED_AST_NODES: tuple = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
    ast.Constant,           # numbers / True / False / None
    ast.Name,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Call,
    ast.keyword,
)

# ``ast.Num`` was deprecated in Python 3.8 (everything is now ``Constant``) and
# slated for removal in 3.14. We deliberately do NOT include it — anything
# parsed under modern Python uses ``Constant`` and accessing the attribute
# triggers a DeprecationWarning we'd rather not emit on every walk.


def _sign(x: float) -> int:
    """Sign helper — defined as a real function so AST ``Call`` to ``sign``
    resolves to it during ``eval_condition``."""
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


# Functions a ``Condition.expr`` is allowed to call. Anything else triggers
# a validation failure even if the name happens to exist in the namespace.
_ALLOWED_FUNCTIONS: Dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "sign": _sign,
}


def _validate_expr_ast(expr: str) -> ast.Expression:
    """Parse ``expr`` and walk the AST, refusing any disallowed node.

    Returns the parsed ``ast.Expression`` so the caller can compile it once
    and reuse the code object. Raises ``ValueError`` on any violation.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Condition expr is not valid Python: {expr!r} ({exc})") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise ValueError(
                f"Condition expr contains disallowed AST node "
                f"{type(node).__name__!r} in {expr!r}; only arithmetic, "
                "comparison, boolean, and whitelisted-function calls are allowed."
            )
        # Calls have an extra constraint: callee must be a Name in the function whitelist.
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError(
                    f"Condition expr Call must reference a bare Name, not "
                    f"{type(node.func).__name__!r} in {expr!r}."
                )
            if node.func.id not in _ALLOWED_FUNCTIONS:
                raise ValueError(
                    f"Condition expr calls disallowed function "
                    f"{node.func.id!r} in {expr!r}; "
                    f"only {sorted(_ALLOWED_FUNCTIONS)} are permitted."
                )

    return tree


def _extract_expr_names(expr: str) -> List[str]:
    """Return the set of bare ``Name`` identifiers referenced by ``expr``,
    minus the function whitelist. These are the context-variable / feature
    names the expression depends on.
    """
    tree = ast.parse(expr, mode="eval")
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
    return sorted(names - set(_ALLOWED_FUNCTIONS.keys()))


# Context vars that always exist at evaluation time and therefore should
# not count as "feature deps" the spec must declare. These mirror the
# bar columns / position-state keys the replay engine guarantees.
_BUILTIN_CONTEXT_KEYS: frozenset[str] = frozenset({
    # bar columns
    "home_score",
    "away_score",
    "elapsed_game_sec",
    # position state
    "pos_side",
    "pos_entry_price",
    "pos_entry_elapsed",
    "pos_size",
    # run-state aggregates
    "minutes_since_entry",
    "unrealized_pnl_bps",
    # convenience literals available in eval namespace
    "True",
    "False",
    "None",
})


# Try simpleeval first; fall back to ast-validated eval. We hold a flag so
# eval_condition() picks the right path without re-importing on every call.
try:  # pragma: no cover - environment-dependent
    import simpleeval as _simpleeval  # type: ignore[import-not-found]

    _SIMPLEEVAL_AVAILABLE = True
except ImportError:
    _simpleeval = None  # type: ignore[assignment]
    _SIMPLEEVAL_AVAILABLE = False


def eval_condition(condition: "Condition", context: Dict[str, Any]) -> bool:
    """Evaluate ``condition.expr`` against ``context`` and return a bool.

    The expression is AST-validated again here (cheap; ~microseconds) as a
    defence-in-depth measure in case the Condition was constructed by code
    that bypassed ``StrategySpec.validate``.
    """
    _validate_expr_ast(condition.expr)

    namespace: Dict[str, Any] = dict(_ALLOWED_FUNCTIONS)
    namespace.update(context)

    if _SIMPLEEVAL_AVAILABLE:
        evaluator = _simpleeval.SimpleEval(
            functions=dict(_ALLOWED_FUNCTIONS),
            names=dict(context),
        )
        result = evaluator.eval(condition.expr)
    else:
        result = eval(  # noqa: S307 - sandbox is enforced by AST validator above
            condition.expr,
            {"__builtins__": {}},
            namespace,
        )
    return bool(result)


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

# Type aliases for the spec literals. We re-export them so callers can refer
# to the canonical set without re-typing.
SizingMode = Literal["fixed_contracts", "fixed_dollars", "kelly_fraction"]
StrategySide = Literal[
    "long_home", "long_away", "long_trailing", "long_hot", "long_cold"
]
StrategyVenue = Literal["kalshi", "polymarket", "either"]

_ALLOWED_SIZING_MODES: frozenset[str] = frozenset(
    {"fixed_contracts", "fixed_dollars", "kelly_fraction"}
)
_ALLOWED_SIDES: frozenset[str] = frozenset(
    {"long_home", "long_away", "long_trailing", "long_hot", "long_cold"}
)
_ALLOWED_VENUES: frozenset[str] = frozenset({"kalshi", "polymarket", "either"})

# Game-clock cap on a hold (2 hours). Anything longer is almost certainly a
# bug — NBA regulation is 48 minutes plus OT.
_MAX_TIME_STOP_SEC: int = 7200


@dataclass(frozen=True)
class SizingRule:
    """How big to make each trade.

    ``value`` is canonicalised to ``float`` and ``max_position_contracts`` to
    ``int`` in ``__post_init__`` so that ``5`` and ``5.0`` produce identical
    JSON / hashes (the spec is part of the cache key — int/float drift would
    silently invalidate every result).
    """

    mode: SizingMode
    value: float                            # contracts / dollars / fraction depending on mode
    max_position_contracts: int = 100       # hard cap per trade regardless of mode

    def __post_init__(self) -> None:
        # Frozen dataclass — use object.__setattr__ to coerce in place.
        object.__setattr__(self, "value", float(self.value))
        object.__setattr__(
            self, "max_position_contracts", int(self.max_position_contracts)
        )


@dataclass(frozen=True)
class Condition:
    """A single trigger condition. Composable into entry/exit lists.

    ``expr`` is a small DSL string evaluated against a context dict by
    ``eval_condition``. See module docstring for the full grammar.
    """

    expr: str
    description: str = ""


@dataclass(frozen=True)
class StrategySpec:
    """The machine-readable strategy contract. See module docstring."""

    name: str
    description: str
    features: List[str]
    entry_condition: Condition
    exit_conditions: List[Condition]
    sizing: SizingRule
    time_stop_sec: int
    side: StrategySide
    venue: StrategyVenue
    cost_assumption_bps: Optional[int] = None

    def __post_init__(self) -> None:
        # Coerce numeric scalars so int/float drift doesn't change the hash.
        object.__setattr__(self, "time_stop_sec", int(self.time_stop_sec))
        if self.cost_assumption_bps is not None:
            object.__setattr__(
                self, "cost_assumption_bps", int(self.cost_assumption_bps)
            )

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation suitable for JSON serialisation."""
        return asdict(self)

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, no whitespace surprises."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategySpec":
        entry = data["entry_condition"]
        exits = data["exit_conditions"]
        sizing = data["sizing"]
        return cls(
            name=data["name"],
            description=data["description"],
            features=list(data["features"]),
            entry_condition=Condition(
                expr=entry["expr"], description=entry.get("description", "")
            ),
            exit_conditions=[
                Condition(expr=c["expr"], description=c.get("description", ""))
                for c in exits
            ],
            sizing=SizingRule(
                mode=sizing["mode"],
                value=float(sizing["value"]),
                max_position_contracts=int(sizing["max_position_contracts"]),
            ),
            time_stop_sec=int(data["time_stop_sec"]),
            side=data["side"],
            venue=data["venue"],
            cost_assumption_bps=(
                None if data.get("cost_assumption_bps") is None
                else int(data["cost_assumption_bps"])
            ),
        )

    @classmethod
    def from_json(cls, s: str) -> "StrategySpec":
        return cls.from_dict(json.loads(s))

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    def spec_hash(self) -> str:
        """Stable hex digest of the canonical JSON representation."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Validation — the live-safety + expression-safety gate
    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        """Raise ``ValueError`` if anything is off. See module docstring."""

        # ---- Literal-typed fields ----
        if self.side not in _ALLOWED_SIDES:
            raise ValueError(
                f"side={self.side!r} is not one of {sorted(_ALLOWED_SIDES)}"
            )
        if self.venue not in _ALLOWED_VENUES:
            raise ValueError(
                f"venue={self.venue!r} is not one of {sorted(_ALLOWED_VENUES)}"
            )
        if self.sizing.mode not in _ALLOWED_SIZING_MODES:
            raise ValueError(
                f"sizing.mode={self.sizing.mode!r} is not one of "
                f"{sorted(_ALLOWED_SIZING_MODES)}"
            )

        # ---- Sizing ----
        if self.sizing.value <= 0:
            raise ValueError(
                f"sizing.value must be > 0, got {self.sizing.value}"
            )
        if self.sizing.max_position_contracts < 1:
            raise ValueError(
                f"sizing.max_position_contracts must be >= 1, "
                f"got {self.sizing.max_position_contracts}"
            )

        # ---- Time stop ----
        if self.time_stop_sec < 0 or self.time_stop_sec > _MAX_TIME_STOP_SEC:
            raise ValueError(
                f"time_stop_sec={self.time_stop_sec} out of range "
                f"[0, {_MAX_TIME_STOP_SEC}]"
            )

        # ---- Cost assumption (when provided) ----
        if self.cost_assumption_bps is not None and self.cost_assumption_bps < 0:
            raise ValueError(
                f"cost_assumption_bps must be >= 0 when provided, "
                f"got {self.cost_assumption_bps}"
            )

        # ---- Features: all must be registered and live-safe ----
        if not isinstance(self.features, list):
            raise ValueError(
                f"features must be a list, got {type(self.features).__name__}"
            )
        REGISTRY.validate_strategy_features(self.features)

        # ---- Conditions: AST sandbox + declared-deps check ----
        if not isinstance(self.entry_condition, Condition):
            raise ValueError("entry_condition must be a Condition instance")
        if not isinstance(self.exit_conditions, list) or not self.exit_conditions:
            raise ValueError(
                "exit_conditions must be a non-empty list of Condition instances"
            )
        for c in self.exit_conditions:
            if not isinstance(c, Condition):
                raise ValueError(
                    "exit_conditions must all be Condition instances"
                )

        declared = set(self.features)
        for label, cond in [("entry_condition", self.entry_condition)] + [
            (f"exit_conditions[{i}]", c) for i, c in enumerate(self.exit_conditions)
        ]:
            _validate_expr_ast(cond.expr)
            referenced = set(_extract_expr_names(cond.expr))
            # The expression may legitimately reference bar columns / position
            # state — only features that aren't built-in context keys need to
            # be declared.
            undeclared_features = referenced - declared - _BUILTIN_CONTEXT_KEYS
            if undeclared_features:
                raise ValueError(
                    f"{label} expression {cond.expr!r} references "
                    f"undeclared name(s) {sorted(undeclared_features)}; "
                    "either declare them in self.features (if features) or "
                    "use a built-in context key."
                )


__all__ = [
    "Condition",
    "SizingRule",
    "StrategySpec",
    "StrategySide",
    "StrategyVenue",
    "SizingMode",
    "eval_condition",
]
