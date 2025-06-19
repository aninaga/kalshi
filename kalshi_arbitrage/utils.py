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
        # Ensure score is a float (fuzzywuzzy should return int, but let's be safe)
        return float(score)
    except Exception:
        return 0.0
