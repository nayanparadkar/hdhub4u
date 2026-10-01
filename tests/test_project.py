"""Tests for the shared filesystem paths."""

from __future__ import annotations

from pathlib import Path

from hdhub4u import project


def test_home_defaults_to_project_root(
    monkeypatch,
) -> None:
    monkeypatch.delenv("HDHUB_HOME", raising=False)

    assert project.get_home() == project.PROJECT_ROOT


def test_home_honours_override(
    isolated_home: Path,
) -> None:
    assert project.get_home() == isolated_home


def test_data_dir_under_home(
    isolated_home: Path,
) -> None:
    assert project.get_data_dir() == isolated_home / "data"


def test_downloads_dir_under_home(
    isolated_home: Path,
) -> None:
    assert project.get_downloads_dir() == (
        isolated_home / "downloads"
    )


def test_ensure_directories_creates_both(
    isolated_home: Path,
) -> None:
    assert project.get_data_dir().is_dir()
    assert project.get_downloads_dir().is_dir()


def test_database_and_cache_paths(
    isolated_home: Path,
) -> None:
    assert project.get_database_path() == (
        isolated_home / "data" / "media.db"
    )

    assert project.get_link_cache_path() == (
        isolated_home / "data" / "links.json"
    )
