"""Labeled cross-venue pairs for matching backtests (Phase A).

The repo has no historical labeled-pair dataset and the NBA-game data lake
contains no matched-pair ground truth, so matching precision/recall must be
measured against a human-labeled set built from the matcher's own candidate
output plus injected hard negatives. This module is the storage format.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional

TRUE_MATCH = "true_match"
FALSE_MATCH = "false_match"


@dataclass
class LabeledPair:
    """A human-adjudicated (Kalshi, Polymarket) candidate pair."""

    kalshi_id: str
    polymarket_id: str
    kalshi_title: str
    polymarket_title: str
    label: str                       # TRUE_MATCH | FALSE_MATCH
    polarity: str = "unknown"        # aligned | inverted | unknown (for true matches)
    kalshi_rules: str = ""
    polymarket_rules: str = ""
    kalshi_close_time: Optional[str] = None
    polymarket_close_time: Optional[str] = None
    notes: str = ""
    source: str = ""                 # e.g. "candidate_capture" | "hard_negative"

    def is_true_match(self) -> bool:
        return self.label == TRUE_MATCH

    def to_market_dicts(self) -> tuple:
        """Rebuild the (kalshi_market, polymarket_market) dicts a verifier expects."""
        kalshi = {
            "id": self.kalshi_id,
            "title": self.kalshi_title,
            "clean_title": "",
            "close_time": self.kalshi_close_time,
            "raw_data": {"rules_primary": self.kalshi_rules},
        }
        polymarket = {
            "id": self.polymarket_id,
            "title": self.polymarket_title,
            "clean_title": "",
            "close_time": self.polymarket_close_time,
            "raw_data": {"description": self.polymarket_rules},
        }
        return kalshi, polymarket


def save_labeled_pairs(pairs: Iterable[LabeledPair], path: str) -> None:
    """Write pairs as JSONL (one record per line)."""
    with open(path, "w") as fh:
        for pair in pairs:
            fh.write(json.dumps(asdict(pair)) + "\n")


def load_labeled_pairs(path: str) -> List[LabeledPair]:
    """Load pairs from a JSONL file."""
    pairs: List[LabeledPair] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            pairs.append(LabeledPair(**data))
    return pairs


def iter_labeled_pairs(path: str) -> Iterator[LabeledPair]:
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield LabeledPair(**json.loads(line))
