"""Cross-venue match verification and labeling (Phase A).

Adds a semantic safety layer on top of the lexical matcher so a candidate pair
is only trusted for execution once outcome-polarity and resolution-criteria
have been checked.
"""

from .labels import (
    FALSE_MATCH,
    TRUE_MATCH,
    LabeledPair,
    load_labeled_pairs,
    save_labeled_pairs,
)
from .llm_tiebreaker import (
    LLMTiebreaker,
    TiebreakResult,
    resolve_uncertain_batch,
)
from .verification import (
    ALIGNED,
    INVERTED,
    UNKNOWN,
    AllowlistVerifier,
    CompositeVerifier,
    DistinguishingEntityVerifier,
    MatchVerdict,
    MatchVerifier,
    OutcomePolarityVerifier,
    ResolutionCriteriaVerifier,
    default_verifiers,
)

__all__ = [
    "ALIGNED",
    "INVERTED",
    "UNKNOWN",
    "AllowlistVerifier",
    "CompositeVerifier",
    "DistinguishingEntityVerifier",
    "MatchVerdict",
    "MatchVerifier",
    "OutcomePolarityVerifier",
    "ResolutionCriteriaVerifier",
    "default_verifiers",
    "LLMTiebreaker",
    "TiebreakResult",
    "resolve_uncertain_batch",
    "LabeledPair",
    "TRUE_MATCH",
    "FALSE_MATCH",
    "load_labeled_pairs",
    "save_labeled_pairs",
]
