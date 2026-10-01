"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hdhub4u.project import ensure_directories


@pytest.fixture
def media_html() -> str:
    """A small page with category and content links."""

    return """
    <html>
      <head><title>Sample Page</title></head>
      <body>
        <a href="/category/hollywood-movies/">Hollywood</a>
        <a href="/how-to-download/">How to download</a>
        <a href="/batman-robin-1997-hindi-bluray-full-movie/">
          Batman &amp; Robin
        </a>
        <a href="/fir-london-hdrip-hindi/">
          <img src="/x.jpg" alt="F.I.R. Full Movie">
        </a>
        <a href="https://example.com/1080p-x264-2gb/">
          1080p x264 [2GB]
        </a>
        <a href="https://example.com/720p-x264-700mb/">
          720p x264 [700MB]
        </a>
      </body>
    </html>
    """


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point the project's data paths at a temporary directory."""

    home = tmp_path / "home"

    monkeypatch.setenv("HDHUB_HOME", str(home))

    ensure_directories()

    return home


@pytest.fixture
def database_path(
    tmp_path: Path,
) -> Path:
    """An empty SQLite database with the media table created."""

    path = tmp_path / "test.db"

    conn = sqlite3.connect(path)

    conn.execute(
        """
        CREATE TABLE media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            image TEXT DEFAULT ''
        )
        """
    )

    conn.commit()
    conn.close()

    return path


@pytest.fixture
def seeded_database(
    database_path: Path,
) -> Path:
    """A database holding three media rows."""

    conn = sqlite3.connect(database_path)

    conn.executemany(
        """
        INSERT INTO media (title, url, type, image)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                "Big Boss Full Series",
                "https://site.test/big-boss/",
                "webseries",
                "",
            ),
            (
                "Batman & Robin Full Movie",
                "https://site.test/batman-robin/",
                "movie",
                "",
            ),
            (
                "[no title]",
                "https://site.test/fir-london-hdrip/",
                "movie",
                "",
            ),
        ],
    )

    conn.commit()
    conn.close()

    return database_path


@pytest.fixture
def download_options() -> list[dict[str, str]]:
    """A representative set of download options."""

    return [
        {
            "title": "Big Boss S01E01 1080p [700MB]",
            "url": "https://cdn.test/1080p-700mb",
            "type": "Download",
        },
        {
            "title": "Big Boss S01E01 720p [400MB]",
            "url": "https://cdn.test/720p-400mb",
            "type": "Download",
        },
        {
            "title": "Big Boss S01E01 480p [180MB]",
            "url": "https://cdn.test/480p-180mb",
            "type": "Download",
        },
        {
            "title": "Big Boss S01E01 360p [90MB]",
            "url": "https://cdn.test/360p-90mb",
            "type": "Download",
        },
    ]
