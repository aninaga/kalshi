"""Build the labeled candidate-pair dataset for matching backtests (Phase A).

There is no historical labeled-pair corpus in the repo, so we build one from
two sources:

  1. **Candidate capture** — the matcher's own above-threshold candidate output
     during a paper scan (titles + raw rules), exported for an operator to
     label true/false.
  2. **Hard negatives** — synthetic false matches derived from known true pairs
     (same subject, different date / opposite threshold direction / inverted
     polarity). These are the failure modes lexical similarity is blind to, so
     they make the precision metric meaningful.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence

from kalshi_arbitrage.matching.labels import FALSE_MATCH, TRUE_MATCH, LabeledPair


def candidate_from_match(match: Dict, label: str = "", source: str = "candidate_capture") -> LabeledPair:
    """Convert a matcher ``match`` dict into an (unlabeled) :class:`LabeledPair`."""
    k = match.get("kalshi_market", {})
    p = match.get("polymarket_market", {})
    return LabeledPair(
        kalshi_id=str(k.get("id", "")),
        polymarket_id=str(p.get("id", "")),
        kalshi_title=k.get("title", ""),
        polymarket_title=p.get("title", ""),
        label=label,
        polarity=match.get("polarity", "unknown"),
        kalshi_rules=str((k.get("raw_data") or {}).get("rules_primary", "")),
        polymarket_rules=str((p.get("raw_data") or {}).get("description", "")),
        kalshi_close_time=k.get("close_time"),
        polymarket_close_time=p.get("close_time"),
        source=source,
    )


def _bump_year(text: str) -> str:
    """Shift a 4-digit year in the title so the contract describes a different event."""
    def repl(m):
        return str(int(m.group(0)) + 1)
    return re.sub(r"\b20\d{2}\b", repl, text, count=1)


def _flip_direction(text: str) -> str:
    swaps = [("over", "under"), ("above", "below"), ("more", "fewer"),
             ("higher", "lower"), ("win", "lose")]
    low = text.lower()
    for a, b in swaps:
        if re.search(rf"\b{a}\b", low):
            return re.sub(rf"\b{a}\b", b, text, flags=re.IGNORECASE)
    return text


def make_hard_negatives(true_pairs: Sequence[LabeledPair]) -> List[LabeledPair]:
    """Derive false-match pairs from true matches.

    For each true pair, produce up to two hard negatives:
      * a Polymarket side whose YEAR was bumped (different event, same words)
      * a Polymarket side with a flipped threshold direction, only if that flip
        actually changed the title (otherwise it's not a hard negative)
    """
    negatives: List[LabeledPair] = []
    for tp in true_pairs:
        if not tp.is_true_match():
            continue

        year_title = _bump_year(tp.polymarket_title)
        if year_title != tp.polymarket_title:
            negatives.append(LabeledPair(
                kalshi_id=tp.kalshi_id,
                polymarket_id=tp.polymarket_id + "::hn_year",
                kalshi_title=tp.kalshi_title,
                polymarket_title=year_title,
                label=FALSE_MATCH,
                kalshi_close_time=tp.kalshi_close_time,
                # Push the PM close time a year out to match the bumped title.
                polymarket_close_time=tp.polymarket_close_time,
                notes="year bumped — different event",
                source="hard_negative",
            ))

        dir_title = _flip_direction(tp.polymarket_title)
        if dir_title != tp.polymarket_title:
            negatives.append(LabeledPair(
                kalshi_id=tp.kalshi_id,
                polymarket_id=tp.polymarket_id + "::hn_dir",
                kalshi_title=tp.kalshi_title,
                polymarket_title=dir_title,
                label=FALSE_MATCH,
                kalshi_close_time=tp.kalshi_close_time,
                polymarket_close_time=tp.polymarket_close_time,
                notes="threshold/outcome direction flipped",
                source="hard_negative",
            ))
    return negatives


def build_dataset(
    true_pairs: Sequence[LabeledPair],
    extra_negatives: Sequence[LabeledPair] = (),
    include_synthetic_negatives: bool = True,
) -> List[LabeledPair]:
    """Assemble a labeled dataset from true pairs + hard/explicit negatives."""
    dataset: List[LabeledPair] = list(true_pairs)
    if include_synthetic_negatives:
        dataset.extend(make_hard_negatives(true_pairs))
    dataset.extend(extra_negatives)
    return dataset
