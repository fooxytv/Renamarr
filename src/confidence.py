"""Confidence scoring for API match results.

Scores range from 0-100, where:
- 90-100: Very high confidence (exact title + year match)
- 70-89:  High confidence (close title match, year matches)
- 40-69:  Medium confidence (partial match, may need review)
- 0-39:   Low confidence (poor match, likely wrong)
"""

import logging
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


def _normalize(s: str) -> str:
    """Normalize a string for comparison: lowercase, strip common noise."""
    import re
    s = s.lower().strip()
    # Remove common artifacts from filenames
    s = re.sub(r'[._\-]+', ' ', s)
    # Remove common tags like (2020), [720p], etc.
    s = re.sub(r'\[.*?\]', '', s)
    s = re.sub(r'\(.*?\)', '', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def title_similarity(parsed_title: str, api_title: str) -> float:
    """Compare parsed title to API result title. Returns 0.0-1.0."""
    if not parsed_title or not api_title:
        return 0.0
    norm_parsed = _normalize(parsed_title)
    norm_api = _normalize(api_title)
    if norm_parsed == norm_api:
        return 1.0
    return SequenceMatcher(None, norm_parsed, norm_api).ratio()


def score_movie_match(
    parsed_title: str,
    parsed_year: int | None,
    api_title: str,
    api_year: int | None,
) -> int:
    """Score a movie match from 0-100.

    Factors:
    - Title similarity (0-60 points)
    - Year match (0-30 points)
    - Bonus for exact title (10 points)
    """
    score = 0.0

    # Title similarity: up to 60 points
    sim = title_similarity(parsed_title, api_title)
    score += sim * 60

    # Year matching: up to 30 points
    if parsed_year and api_year:
        if parsed_year == api_year:
            score += 30
        elif abs(parsed_year - api_year) == 1:
            score += 15  # Off by one year (common in release vs theatrical)
    elif not parsed_year and api_year:
        # No year parsed from filename - partial credit
        score += 10

    # Exact title bonus: 10 points
    if _normalize(parsed_title or "") == _normalize(api_title or ""):
        score += 10

    return min(100, int(round(score)))


def score_episode_match(
    parsed_show: str,
    parsed_year: int | None,
    api_show: str,
    api_year: int | None,
    has_episode_match: bool = False,
) -> int:
    """Score a TV episode match from 0-100.

    Factors:
    - Show name similarity (0-50 points)
    - Year match (0-20 points)
    - Episode found (0-20 points)
    - Exact name bonus (10 points)
    """
    score = 0.0

    # Show name similarity: up to 50 points
    sim = title_similarity(parsed_show, api_show)
    score += sim * 50

    # Year matching: up to 20 points
    if parsed_year and api_year:
        if parsed_year == api_year:
            score += 20
        elif abs(parsed_year - api_year) == 1:
            score += 10
    elif not parsed_year and api_year:
        score += 5

    # Episode match: 20 points
    if has_episode_match:
        score += 20

    # Exact name bonus: 10 points
    if _normalize(parsed_show or "") == _normalize(api_show or ""):
        score += 10

    return min(100, int(round(score)))
