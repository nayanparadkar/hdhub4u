"""Filesystem locations used across the application.

Anchored to the project directory rather than the current working
directory, so the CLI behaves the same from anywhere. Override with
HDHUB_HOME when running from a copy of the data elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "HDHUB_HOME"

SOURCE_ROOT = Path(__file__).resolve().parent.parent

PROJECT_ROOT = SOURCE_ROOT.parent


def get_home() -> Path:
    """Return the directory holding the index, cache and downloads."""

    override = os.environ.get(ENV_HOME)

    if override:
        return Path(override).expanduser().resolve()

    return PROJECT_ROOT


def get_data_dir() -> Path:
    """Return the directory for the SQLite index."""

    return get_home() / "data"


def get_database_path() -> Path:
    """Return the SQLite index path."""

    return get_data_dir() / "media.db"


def get_link_cache_path() -> Path:
    """Return the resolved-link cache path."""

    return get_data_dir() / "links.json"


def get_downloads_dir() -> Path:
    """Return the root directory for downloaded media."""

    return get_home() / "downloads"


def ensure_directories() -> None:
    """Create the data and download directories."""

    get_data_dir().mkdir(
        parents=True,
        exist_ok=True,
    )

    get_downloads_dir().mkdir(
        parents=True,
        exist_ok=True,
    )
