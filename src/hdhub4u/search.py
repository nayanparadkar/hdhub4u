"""Fuzzy search over the local index."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .database import connection
from .parser import extract_title_from_url

NO_TITLE = "[no title]"


def normalize_text(
    text: str,
) -> str:
    """Lowercase a string and collapse it to alphanumeric words."""

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text.lower(),
    )

    return " ".join(text.split())


def get_search_title(
    title: str,
    url: str,
) -> str:
    """
    Return a usable title for a row.

    Rows indexed without a title fall back to the URL slug so that
    search still matches something meaningful.
    """

    title = (title or "").strip()

    if title and title != NO_TITLE:
        return title

    return (
        extract_title_from_url(url)
        or url
    )


def similarity(
    first: str,
    second: str,
) -> float:
    """Return a 0..1 similarity ratio between two strings."""

    return SequenceMatcher(
        None,
        first,
        second,
    ).ratio()


def calculate_score(
    query: str,
    title: str,
) -> int:
    """Score how well a title matches a search query."""

    query_normalized = normalize_text(query)
    title_normalized = normalize_text(title)

    if not query_normalized:
        return 0

    query_words = query_normalized.split()
    title_words = title_normalized.split()

    if title_normalized == query_normalized:
        return 1000

    score = 0
    strong_matches = 0

    if query_normalized in title_normalized:
        score += 300
        strong_matches += 1

    for word in query_words:
        if word in title_words:
            score += 100
            strong_matches += 1

            continue

        if any(
            title_word.startswith(word)
            for title_word in title_words
        ):
            score += 70
            strong_matches += 1

            continue

        if len(word) < 4:
            continue

        for title_word in title_words:
            ratio = similarity(word, title_word)

            if ratio >= 0.75:
                score += 50
                strong_matches += 1
                break

    if strong_matches == 0:
        return 0

    return score


def search_media(
    query: str,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Search the local index and return scored results."""

    query = query.strip()

    if len(query) < 2:
        return []

    query_normalized = normalize_text(query)

    if not query_normalized:
        return []

    query_words = query_normalized.split()

    if all(len(word) < 3 for word in query_words):
        return []

    # Scoring happens in Python because the ranking blends substring,
    # prefix and edit-distance matches. A SQL prefilter would drop
    # the fuzzy cases the index exists to handle.
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                title,
                url,
                type,
                image
            FROM media
            """
        ).fetchall()

    results = []

    for row in rows:
        title = get_search_title(
            row["title"],
            row["url"],
        )

        score = calculate_score(
            query,
            title,
        )

        if score <= 0:
            continue

        results.append(
            {
                "id": row["id"],
                "title": title,
                "url": row["url"],
                "type": row["type"],
                "image": row["image"],
                "score": score,
            }
        )

    results.sort(
        key=lambda item: (
            -int(item["score"]),
            str(item["title"]).lower(),
        )
    )

    return results[:limit]
