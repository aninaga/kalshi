import re
from fuzzywuzzy import fuzz

# Canonical sport stat keywords for player-prop disambiguation
_STAT_KEYWORDS = frozenset({
    # Basketball
    'points', 'assists', 'rebounds', 'steals', 'blocks', 'threes', 'turnovers',
    # Baseball
    'strikeouts', 'hits', 'homers', 'rbis', 'walks', 'pitches', 'innings',
    'runs', 'earned',
    # Football
    'touchdowns', 'completions', 'receptions', 'interceptions', 'sacks',
    'passing', 'rushing', 'receiving', 'yards',
    # Hockey / Soccer
    'saves', 'shots', 'goals', 'fouls', 'corners',
})


def extract_stat_types(clean_title_str):
    """Return the set of sport-stat keywords found in a cleaned title.

    Used to reject false-positive matches where the same player has
    multiple prop lines (e.g. assists vs rebounds).
    """
    words = set(clean_title_str.lower().split())
    return words & _STAT_KEYWORDS


def extract_prop_threshold(title):
    """Extract the numeric threshold from a player-prop market title.

    Handles both formats:
      Kalshi:      "Player Name: 8+" → 8 (means ≥8, equivalent to Over 7.5)
      Polymarket:  "Player Name: Rebounds O/U 4.5" → 4.5

    Returns a float threshold normalized to the Polymarket convention
    (the O/U half-integer), or None if no threshold is found.

    Normalization: Kalshi "N+" → N - 0.5  (e.g. "8+" → 7.5)
    """
    t = title.strip()

    # Pattern 1: Kalshi style "N+" (integer, means N or more = Over N-0.5)
    m = re.search(r'(\d+)\+', t)
    if m:
        return float(m.group(1)) - 0.5

    # Pattern 2: Polymarket style "O/U N.5" or "Over N.5" or just "N.5"
    m = re.search(r'(\d+\.5)', t)
    if m:
        return float(m.group(1))

    # Pattern 3: Integer threshold without "+" (e.g. "Over 8")
    m = re.search(r'(?:over|under|ou)\s+(\d+)\b', t, re.IGNORECASE)
    if m:
        return float(m.group(1))

    return None


def thresholds_compatible(title1, title2):
    """Check if two player-prop markets have compatible thresholds.

    Returns True if:
      - Either title has no extractable threshold (can't verify → pass)
      - Both thresholds are within 1.0 of each other (accounts for
        rounding: Kalshi "8+" = 7.5, Poly "O/U 8.5" differ by 1.0)
    """
    t1 = extract_prop_threshold(title1)
    t2 = extract_prop_threshold(title2)

    if t1 is None or t2 is None:
        return True  # Can't verify — don't reject

    return abs(t1 - t2) <= 1.0


def clean_title(title):
    """Cleans and standardizes a market title."""
    if not title or not isinstance(title, str):
        return ""
    
    title = title.lower()
    title = re.sub(r'[\/]', ' ', title)  # Replace slashes with spaces
    title = re.sub(r'[^a-z0-9\s]', '', title)  # Remove special characters
    title = re.sub(r'\s+', ' ', title).strip()  # Normalize whitespace
    return title

def get_similarity_score(title1, title2):
    """Calculates a similarity score between two titles."""
    # Ensure both titles are valid strings
    if not title1 or not title2 or not isinstance(title1, str) or not isinstance(title2, str):
        return 0.0
    
    # Special handling for Kalshi ticker format
    score = _calculate_enhanced_similarity(title1, title2)
    
    # Fallback to original algorithm if enhanced doesn't work
    if score < 0.1:
        score = _calculate_basic_similarity(title1, title2)
    
    return float(score)

def _calculate_enhanced_similarity(title1, title2):
    """Enhanced similarity calculation for Kalshi ticker format."""
    # Detect if one title is a Kalshi ticker (uppercase, contains dashes/numbers)
    def is_kalshi_ticker(title):
        return title.isupper() and any(c in title for c in ['-', '2'])
    
    kalshi_title = title1 if is_kalshi_ticker(title1) else title2 if is_kalshi_ticker(title2) else None
    human_title = title2 if kalshi_title == title1 else title1 if kalshi_title == title2 else None
    
    if not kalshi_title or not human_title:
        return 0.0
    
    # Extract key terms from Kalshi ticker
    kalshi_terms = _extract_kalshi_terms(kalshi_title)
    human_terms = set(human_title.lower().split())

    total_key_terms = len(kalshi_terms)
    if total_key_terms == 0:
        return 0.0

    # Generic term overlap: count a ticker term as matched if it appears in the
    # human title exactly or shares a 4+ char prefix with one of its words
    # (handles e.g. "republican"/"republicans", "govern"/"governor").
    matches = 0
    for term in kalshi_terms:
        if term in human_terms:
            matches += 1
        elif len(term) >= 4 and any(
            w.startswith(term[:4]) or term.startswith(w[:4]) for w in human_terms if len(w) >= 4
        ):
            matches += 0.5

    return min(matches / total_key_terms, 1.0)

# Kalshi ticker boilerplate: series-type prefixes and structural tokens that
# carry no event-distinguishing meaning.
_TICKER_STOPWORDS = frozenset({
    'kx', 'kxp', 'the', 'and', 'for', 'will', 'be', 'to', 'of', 't', 'b',
})


def _extract_kalshi_terms(ticker):
    """Extract meaningful word terms from a Kalshi ticker, generically.

    Kalshi tickers are opaque codes like ``KXPRESPARTY-28-DEM`` or
    ``FEDDECISION-25DEC``. Split on delimiters and letter/number boundaries,
    then keep alphabetic tokens of length >= 3 that aren't structural
    boilerplate. No hardcoded subject names — that was brittle whack-a-mole.
    """
    ticker = (ticker or "").lower()
    # Split on any non-alphanumeric delimiter, then on letter<->digit boundaries.
    raw_segments = re.split(r'[^a-z0-9]+', ticker)
    terms = set()
    for seg in raw_segments:
        if not seg:
            continue
        # Break "fed25dec" -> ["fed", "25", "dec"]
        for piece in re.findall(r'[a-z]+|[0-9]+', seg):
            if piece.isalpha() and len(piece) >= 3 and piece not in _TICKER_STOPWORDS:
                terms.add(piece)
    return terms

def _calculate_basic_similarity(title1, title2):
    """Original similarity calculation method."""
    # Fast pre-check: if titles are too different in length, likely not similar
    len_ratio = min(len(title1), len(title2)) / max(len(title1), len(title2))
    if len_ratio < 0.3:  # Very different lengths
        return 0.0

    # Fast pre-check: count common words
    words1 = set(title1.split())
    words2 = set(title2.split())
    common_words = len(words1 & words2)
    total_words = len(words1 | words2)

    if total_words == 0 or common_words / total_words < 0.2:  # Less than 20% overlap
        return 0.0

    try:
        score = fuzz.token_sort_ratio(title1, title2) / 100.0

        # Penalize matches where the distinguishing terms diverge.
        # "Will the Republican party win the governorship in Delaware?"
        # vs "Will the Republican Party win the IN-02 House seat?"
        # share high fuzzy score because of boilerplate, but the unique
        # terms ("delaware"/"governorship" vs "house"/"seat") differ.
        if 0.85 <= score < 1.0:
            score = _penalize_divergent_terms(score, words1, words2)

        return score
    except Exception:
        return 0.0


# Common boilerplate words that appear in many market titles and should
# not count toward distinguishing two markets from each other.
_BOILERPLATE = frozenset({
    # Determiners / prepositions / conjunctions
    'will', 'the', 'be', 'in', 'on', 'at', 'to', 'of', 'a', 'an',
    'and', 'or', 'for', 'by', 'is', 'are', 'it', 'its', 'as', 'if',
    'not', 'no', 'yes', 'do', 'does', 'did', 'has', 'have', 'had',
    'was', 'were', 'been', 'being', 'with', 'from', 'into', 'than',
    'but', 'so', 'yet', 'nor', 'both', 'either', 'neither',
    # Common verbs / adjectives in market titles
    'win', 'run', 'become', 'release', 'score', 'get', 'make',
    'next', 'new', 'more', 'most', 'first', 'last', 'other',
    'before', 'after', 'between', 'over', 'under', 'above', 'below',
    'increase', 'decrease', 'how', 'many', 'much', 'who', 'what',
    'this', 'that', 'their', 'year', 'during',
    # Event-template verbs that recur across UNRELATED markets and so do not
    # distinguish the subject ("Will <X> be arrested/indicted before 2027",
    # "<A> and <B> normalize relations"). Without these the shared template
    # inflates the distinguishing-overlap of different subjects.
    'arrested', 'indicted', 'convicted', 'sanctioned', 'impeached',
    'resign', 'resigns', 'normalize', 'normalise', 'relations', 'announce',
    'announces', 'step', 'down', 'out',
    # Politics / elections
    'party', 'presidential', 'nomination', 'nominate', 'nominated',
    'race', 'election', 'seat', 'vote', 'votes', 'voting',
    'republican', 'democratic', 'democrats', 'republicans', 'democratics',
    'house', 'senate', 'governor', 'governorship', 'congress',
    'prime', 'minister', 'president', 'chancellor', 'general',
    'parliament', 'parliamentary',
    # Economics / finance
    'price', 'range', 'rate', 'rates', 'funds', 'fund', 'federal',
    'reserve', 'gdp', 'growth', 'real', 'upper', 'bound',
    # Sports
    'championship', 'conference', 'game', 'match', 'season',
    # NOTE (A8): years are intentionally NOT boilerplate — a year (2024 vs 2028)
    # DEFINES the event, so it must count as a distinguishing term that can
    # penalize a divergent match rather than being ignored.
})


def _penalize_divergent_terms(score, words1, words2):
    """Reduce fuzzy score when the distinguishing terms don't match.

    Boilerplate words (will/the/party/win/republican/...) are ignored.
    Of the remaining "distinguishing" terms, we check what fraction
    is shared.  A word counts as matched if it appears exactly in the
    other set OR shares a 4+ char prefix (handles republican/republicans,
    governor/governorship, etc.).

    If < 60% of the smaller set's terms have a match, the score is
    reduced below the 0.85 threshold.
    """
    d1 = words1 - _BOILERPLATE
    d2 = words2 - _BOILERPLATE

    # If either side has no distinguishing terms, can't verify — let it pass
    if not d1 or not d2:
        return score

    # Use the smaller set as reference (stricter check)
    smaller, larger = (d1, d2) if len(d1) <= len(d2) else (d2, d1)

    # Count matches: exact OR shared 4-char prefix
    matched = 0
    for word in smaller:
        if word in larger:
            matched += 1
        elif len(word) >= 4:
            prefix = word[:4]
            if any(lw.startswith(prefix) for lw in larger):
                matched += 1

    containment = matched / len(smaller)

    if containment < 0.6:
        # Scale score down proportionally (e.g. 0.87 * 0.4/0.6 ≈ 0.58)
        return score * (containment / 0.6)

    return score
