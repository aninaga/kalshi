"""propose_hypothesis — validate a StrategySpec JSON before submitting to the harness.

CLI::

    python -m research.agents.tools.propose_hypothesis \\
        --spec-file <path-to-spec.json> \\
        [--strict]

Outputs a JSON object ``{valid: bool, spec_hash: str | null, errors: list[str]}``.
Exits 0 if valid, 1 if not.

``--strict`` additionally synthesises a dummy bar and calls
``REGISTRY.compute(name, dummy_bar, game_clock_sec=600.0)`` for each declared
feature, catching ``RuntimeError``, ``KeyError``, and ``AttributeError``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_spec_json(spec_file: str) -> tuple[Any, list[str]]:
    """Read and JSON-parse the spec file.  Returns (raw_dict, errors)."""
    path = Path(spec_file)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, IOError) as exc:
        return None, [f"Cannot read spec file {spec_file!r}: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"Spec file is not valid JSON: {exc}"]

    if not isinstance(data, dict):
        return None, ["Spec file must be a JSON object (dict), not a list or scalar."]

    return data, []


def _validate_spec(spec_file: str, strict: bool) -> dict:
    """Core validation logic. Returns the result dict."""
    # --- load raw JSON ---
    raw, load_errors = _load_spec_json(spec_file)
    if load_errors:
        return {"valid": False, "spec_hash": None, "errors": load_errors}

    # --- parse via StrategySpec.from_json ---
    errors: list[str] = []
    spec = None
    spec_hash: str | None = None

    try:
        from research.harness.strategy_spec import StrategySpec
        spec = StrategySpec.from_dict(raw)
        spec_hash = spec.spec_hash()
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"StrategySpec.from_dict failed: {exc}")
        return {"valid": False, "spec_hash": None, "errors": errors}

    # --- spec.validate() ---
    try:
        spec.validate()
    except ValueError as exc:
        errors.append(f"StrategySpec.validate() failed: {exc}")

    # --- strict mode: smoke-test each feature via REGISTRY.compute ---
    if strict and not errors:
        try:
            from research.features.registry import REGISTRY
        except ImportError as exc:
            errors.append(f"Could not import REGISTRY: {exc}")
        else:
            # Dummy bar with common field names the registry computers may read.
            dummy_bar: dict = {
                "margin": -5,
                "pm_implied_wp": 0.4,
                "kalshi_implied_wp": 0.42,
                "home_score": 45,
                "away_score": 50,
                "elapsed_game_sec": 600.0,
                "time_remaining": 2280.0,
                "pace_ppm": 4.5,
                "recent_run_signed": 2.0,
                "lineup_hash": 12345,
                "home_stars_on": 2,
                "away_stars_on": 1,
                "lead_changes_cum": 7,
            }
            for feature_name in spec.features:
                try:
                    REGISTRY.compute(feature_name, dummy_bar, game_clock_sec=600.0)
                except (RuntimeError, KeyError, AttributeError) as exc:
                    errors.append(
                        f"REGISTRY.compute({feature_name!r}, dummy_bar, 600.0) "
                        f"raised {type(exc).__name__}: {exc}"
                    )

    valid = len(errors) == 0
    return {"valid": valid, "spec_hash": spec_hash, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a StrategySpec JSON file.",
        prog="python -m research.agents.tools.propose_hypothesis",
    )
    parser.add_argument(
        "--spec-file",
        required=True,
        metavar="PATH",
        help="Path to the spec JSON file.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help=(
            "Also exercise each feature via REGISTRY.compute on a dummy bar "
            "to catch resolution errors early."
        ),
    )
    args = parser.parse_args(argv)

    result = _validate_spec(args.spec_file, args.strict)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
