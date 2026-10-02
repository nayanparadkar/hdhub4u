"""SQLite storage for the local media index."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import StorageError
from .logging_setup import get_logger
from .project import get_database_path

logger = get_logger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    image TEXT DEFAULT ''
)
"""

INDEX_TITLE = (
    "CREATE INDEX IF NOT EXISTS idx_media_title "
    "ON media(title)"
)


@contextmanager
def connection(
    path: Path | None = None,
) -> Iterator[sqlite3.Connection]:
    """
    Yield a configured connection, committing on success.

    Raises:
        StorageError: when the index cannot be opened or written.
    """

    database_path = path or get_database_path()

    try:
        database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        conn = sqlite3.connect(database_path)

    except (OSError, sqlite3.Error) as error:
        raise StorageError(
            f"Could not open the index at {database_path}: "
            f"{error}"
        ) from error

    try:
        conn.row_factory = sqlite3.Row

        yield conn

        conn.commit()

    except sqlite3.Error as error:
        conn.rollback()

        raise StorageError(
            f"Index operation failed: {error}"
        ) from error

    finally:
        conn.close()


def initialize_database() -> None:
    """Create the media table and its index if missing."""

    with connection() as conn:
        conn.execute(SCHEMA)
        conn.execute(INDEX_TITLE)

    logger.debug("index ready")


def add_media(
    title: str,
    url: str,
    media_type: str,
    image: str = "",
) -> None:
    """Insert a media row, updating it if the URL is already known."""

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO media (
                title,
                url,
                type,
                image
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                type = excluded.type,
                image = excluded.image
            """,
            (
                title,
                url,
                media_type,
                image,
            ),
        )


def add_media_batch(
    rows: list[tuple[str, str, str, str]],
) -> int:
    """
    Insert many rows in one transaction.

    Returns:
        The number of rows written.

    Indexing commits per row, which on SQLite is the dominant cost
    over a full crawl.
    """

    if not rows:
        return 0

    with connection() as conn:
        cursor = conn.executemany(
            """
            INSERT INTO media (
                title, url, type, image
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                type = excluded.type,
                image = excluded.image
            """,
            rows,
        )

        written = cursor.rowcount

    logger.info("indexed %d rows", written)

    return max(written, 0)


def get_media_count() -> int:
    """Return the number of indexed rows."""

    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM media"
        ).fetchone()

    return int(row[0])


def newest_media(
    limit: int = 30,
) -> list[dict[str, object]]:
    """
    Return the most recently indexed rows.

    Rows are stored in crawl order, so a descending id is the
    cheapest way to show what was added last without a timestamp
    column.

    Returns an empty list when the table has not been created yet,
    because browsing an unbuilt index is a normal first-run state,
    not an error.
    """

    if not get_database_path().exists():
        return []

    try:
        with connection() as conn:
            return _fetch_newest(conn, limit)

    except StorageError as error:
        logger.debug("index not readable: %s", error)

        return []


def _fetch_newest(
    conn: sqlite3.Connection,
    limit: int,
) -> list[dict[str, object]]:
    """Read the newest rows from an open connection."""

    rows = conn.execute(
        """
        SELECT
            id,
            title,
            url,
            type,
            image
        FROM media
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "id": row["id"],
            "title": row["title"],
            "url": row["url"],
            "type": row["type"],
            "image": row["image"],
        }
        for row in rows
    ]


def placeholder_title_count() -> int:
    """Return how many rows still hold a placeholder title."""

    with connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM media
            WHERE title IS NULL
               OR TRIM(title) = ''
               OR title = '[no title]'
            """
        ).fetchone()

    return int(row[0])


def backfill_titles(
    database_path: Path | None = None,
) -> int:
    """
    Repair rows whose title is missing or a placeholder.

    The title is derived from the URL slug. Earlier indexing runs
    stored a placeholder instead of the real title, which made every
    search result useless.

    Returns:
        The number of rows updated.
    """

    # Imported here to avoid a circular import: parser imports
    # nothing from database, but keeping the import local documents
    # that the title derivation lives in the parser layer.
    from .parser import extract_title_from_url

    path = database_path or get_database_path()

    if not path.exists():
        return 0

    conn = sqlite3.connect(path)

    try:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT id, title, url
            FROM media
            WHERE title IS NULL
               OR TRIM(title) = ''
               OR title = '[no title]'
            """
        ).fetchall()

        updates = [
            (
                extract_title_from_url(row["url"])
                or row["url"],
                row["id"],
            )
            for row in rows
        ]

        if updates:
            conn.executemany(
                "UPDATE media SET title = ? WHERE id = ?",
                updates,
            )

        conn.commit()

    except sqlite3.Error as error:
        conn.rollback()

        raise StorageError(
            f"Could not repair titles: {error}"
        ) from error

    finally:
        conn.close()

    if updates:
        logger.info("repaired %d titles", len(updates))

    return len(updates)
