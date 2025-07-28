import re
from fuzzywuzzy import fuzz

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
    
    # Check for specific patterns
    matches = 0
    total_key_terms = len(kalshi_terms)
    
    if total_key_terms == 0:
        return 0.0
    
    for term in kalshi_terms:
        if term in human_terms:
            matches += 1
        # Special case: "leave" + "powell" should match "powell out"
        elif term == 'leave' and any('out' in t or 'leave' in t for t in human_terms):
            matches += 0.8
        elif term == 'powell' and any('powell' in t for t in human_terms):
            matches += 1
    
    return min(matches / total_key_terms, 1.0)

def _extract_kalshi_terms(ticker):
    """Extract meaningful terms from Kalshi ticker format."""
    ticker = ticker.lower()
    terms = set()
    
    # Common Kalshi patterns
    if 'leavepowell' in ticker:
        terms.update(['leave', 'powell'])
    if 'powell' in ticker:
        terms.add('powell')
    if 'fed' in ticker:
        terms.add('fed')
    if 'chair' in ticker:
        terms.add('chair')
    if 'admin' in ticker:
        terms.add('admin')
    if 'leave' in ticker:
        terms.add('leave')
    
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
        return score
    except Exception:
        return 0.0
