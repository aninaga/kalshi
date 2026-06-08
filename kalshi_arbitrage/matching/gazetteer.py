"""Geography / scope gazetteer for match verification.

The dominant cross-venue false-positive class is *scope mismatch*: a national
market ("Will Republicans win the U.S. Senate") matched against a single-state
market ("Will the Republicans win the Montana Senate race"), or a country swap
("Israel and Qatar normalize" vs "Israel and Saudi Arabia normalize"). The
distinguishing token is a place name. This module extracts the set of
geography/scope entities from a cleaned title so a verifier can require them to
agree.

Pure-Python frozensets — no dependencies. Operates on the output of
``utils.clean_title`` (lowercased, punctuation stripped, whitespace collapsed),
so "U.S." arrives as "us" and "South Dakota" as the tokens ["south", "dakota"].
"""

from __future__ import annotations

from typing import Set

# --- US states (single-token + multi-token, canonicalized with underscore) --- #
_US_STATE_SINGLE = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "ohio", "oklahoma", "oregon",
    "pennsylvania", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "wisconsin", "wyoming",
})
# Multi-word states, matched as adjacent bigrams on the cleaned token list.
_US_STATE_PHRASE = {
    ("new", "hampshire"): "new_hampshire",
    ("new", "mexico"): "new_mexico",
    ("new", "jersey"): "new_jersey",
    ("new", "york"): "new_york",
    ("north", "carolina"): "north_carolina",
    ("north", "dakota"): "north_dakota",
    ("south", "carolina"): "south_carolina",
    ("south", "dakota"): "south_dakota",
    ("west", "virginia"): "west_virginia",
    ("rhode", "island"): "rhode_island",
}

# --- Countries / nationalities (single + multi-word) -------------------------- #
_COUNTRY_SINGLE = frozenset({
    "israel", "qatar", "lebanon", "iran", "iraq", "syria", "egypt", "jordan",
    "ukraine", "russia", "china", "taiwan", "japan", "india", "pakistan",
    "afghanistan", "germany", "france", "italy", "spain", "poland", "turkey",
    "venezuela", "cuba", "mexico", "canada", "brazil", "argentina", "nigeria",
    "ethiopia", "sudan", "yemen", "libya", "greece", "hungary", "romania",
})
_COUNTRY_PHRASE = {
    ("saudi", "arabia"): "saudi_arabia",
    ("united", "kingdom"): "united_kingdom",
    ("south", "korea"): "south_korea",
    ("north", "korea"): "north_korea",
    ("south", "africa"): "south_africa",
}

# Tokens meaning "national / whole-country scope" — NOT a sub-scope.
NATIONAL_SCOPE_TOKENS = frozenset({
    "us", "usa", "national", "nationwide", "federal", "countrywide",
})


def extract_scope_entities(clean_text: str) -> Set[str]:
    """Return geography/scope entities present in a cleaned title.

    Returns canonical keys, e.g. {"montana"}, {"saudi_arabia"},
    {"israel", "qatar"}, or an empty set when no place is named. Multi-word
    places are detected as adjacent bigrams and collapsed to one key; their
    component tokens are then NOT also reported as singles.
    """
    tokens = clean_text.split()
    entities: Set[str] = set()
    consumed = set()  # indices consumed by a phrase match

    # Bigram phrases first (so "south dakota" doesn't also yield bare matches).
    for i in range(len(tokens) - 1):
        if i in consumed:
            continue
        bigram = (tokens[i], tokens[i + 1])
        key = _US_STATE_PHRASE.get(bigram) or _COUNTRY_PHRASE.get(bigram)
        if key:
            entities.add(key)
            consumed.add(i)
            consumed.add(i + 1)

    # Single-token places, skipping tokens consumed by a phrase.
    for i, tok in enumerate(tokens):
        if i in consumed:
            continue
        if tok in _US_STATE_SINGLE or tok in _COUNTRY_SINGLE:
            entities.add(tok)

    return entities


def is_national_scope(clean_text: str) -> bool:
    """True if the title names a national/whole-country scope token."""
    return bool(set(clean_text.split()) & NATIONAL_SCOPE_TOKENS)
