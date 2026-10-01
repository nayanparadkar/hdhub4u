"""Tests for the SQLite index."""

from __future__ import annotations

from pathlib import Path

import pytest

from hdhub4u.database import (
    add_media,
    add_media_batch,
    backfill_titles,
    connection,
    get_media_count,
    initialize_database,
    newest_media,
    placeholder_title_count,
)
from hdhub4u.errors import StorageError


def _rows(
    database_path: Path,
) -> list[tuple]:
    with connection(database_path) as conn:
        return conn.execute(
            "SELECT title, url, type FROM media"
            " ORDER BY id"
        ).fetchall()


def test_initialize_creates_table(
    isolated_home: Path,
) -> None:
    initialize_database()

    with connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table'"
            )
        }

    assert "media" in tables


def test_initialize_is_idempotent(
    isolated_home: Path,
) -> None:
    initialize_database()
    initialize_database()

    assert get_media_count() == 0


def test_add_and_count(
    isolated_home: Path,
) -> None:
    initialize_database()

    add_media("Movie", "https://x.test/m", "movie")

    assert get_media_count() == 1


def test_duplicate_url_updates_in_place(
    isolated_home: Path,
) -> None:
    initialize_database()

    add_media("Old", "https://x.test/m", "movie")
    add_media("New", "https://x.test/m", "movie")

    assert get_media_count() == 1

    with connection() as conn:
        title = conn.execute(
            "SELECT title FROM media"
        ).fetchone()["title"]

    assert title == "New"


def test_batch_insert_writes_all(
    isolated_home: Path,
) -> None:
    initialize_database()

    written = add_media_batch(
        [
            ("A", "https://x.test/a", "movie", ""),
            ("B", "https://x.test/b", "movie", ""),
            ("C", "https://x.test/c", "episode", ""),
        ]
    )

    assert written == 3
    assert get_media_count() == 3


def test_batch_insert_empty_is_a_noop(
    isolated_home: Path,
) -> None:
    initialize_database()

    assert add_media_batch([]) == 0


def test_batch_insert_deduplicates(
    isolated_home: Path,
) -> None:
    initialize_database()

    add_media_batch(
        [
            ("A", "https://x.test/a", "movie", ""),
            ("A again", "https://x.test/a", "movie", ""),
        ]
    )

    assert get_media_count() == 1


def test_newest_media_returns_latest_first(
    isolated_home: Path,
) -> None:
    initialize_database()

    add_media_batch(
        [
            ("First", "https://x.test/1", "movie", ""),
            ("Second", "https://x.test/2", "movie", ""),
            ("Third", "https://x.test/3", "movie", ""),
        ]
    )

    newest = newest_media(2)

    assert [row["title"] for row in newest] == [
        "Third",
        "Second",
    ]


def test_placeholder_count(
    isolated_home: Path,
) -> None:
    initialize_database()

    add_media_batch(
        [
            ("Real", "https://x.test/1", "movie", ""),
            ("[no title]", "https://x.test/2", "movie", ""),
            ("   ", "https://x.test/3", "movie", ""),
        ]
    )

    assert placeholder_title_count() == 2


def test_backfill_repairs_placeholders(
    seeded_database: Path,
) -> None:
    repaired = backfill_titles(seeded_database)

    assert repaired == 1

    titles = [
        row["title"] for row in _rows(seeded_database)
    ]

    assert "[no title]" not in titles


def test_backfill_derives_title_from_slug(
    seeded_database: Path,
) -> None:
    backfill_titles(seeded_database)

    titles = [
        row["title"] for row in _rows(seeded_database)
    ]

    # "hdrip" is a source marker, not part of the title, so the
    # repaired row drops it.
    assert "Fir London" in titles


def test_backfill_is_idempotent(
    seeded_database: Path,
) -> None:
    assert backfill_titles(seeded_database) == 1
    assert backfill_titles(seeded_database) == 0


def test_backfill_missing_file_is_safe(
    tmp_path: Path,
) -> None:
    assert (
        backfill_titles(tmp_path / "absent.db") == 0
    )


def test_storage_error_on_bad_path(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(StorageError):
        with connection(blocker / "nested.db"):
            pass


def test_storage_error_on_bad_query(
    seeded_database: Path,
) -> None:
    with pytest.raises(StorageError):
        with connection(seeded_database) as conn:
            conn.execute("SELECT * FROM nonexistent")
