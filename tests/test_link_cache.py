"""Tests for the resolved-link cache."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hdhub4u.link_cache import (
    CACHE_VERSION,
    CacheEntry,
    LinkCache,
)


def test_put_and_get_roundtrip(
    tmp_path: Path,
) -> None:
    cache = LinkCache(tmp_path / "links.json")

    cache.put(
        "https://gate.test/a",
        "https://cdn.test/file.mp4",
        "video/mp4",
        "direct",
    )

    entry = cache.get("https://gate.test/a")

    assert entry is not None
    assert entry.final_url == "https://cdn.test/file.mp4"
    assert entry.content_type == "video/mp4"
    assert entry.strategy == "direct"


def test_get_missing_returns_none(
    tmp_path: Path,
) -> None:
    cache = LinkCache(tmp_path / "links.json")

    assert cache.get("https://absent.test/") is None


def test_stale_entry_is_not_returned(
    tmp_path: Path,
) -> None:
    """
    Cached links are short-lived, so an expired entry must be
    treated as a miss rather than replayed as a valid URL.
    """

    cache = LinkCache(
        tmp_path / "links.json",
        ttl=1,
    )

    cache.put(
        "https://gate.test/a",
        "https://cdn.test/f.mp4",
        "video/mp4",
        "direct",
    )

    assert cache.get("https://gate.test/a") is not None

    time.sleep(1.1)

    assert cache.get("https://gate.test/a") is None


def test_freshness_boundary() -> None:
    now = 1_000.0

    entry = CacheEntry(
        url="u",
        final_url="f",
        content_type="video/mp4",
        strategy="direct",
        resolved_at=now - 5,
    )

    assert entry.is_fresh(10, now=now)
    assert not entry.is_fresh(5, now=now)


def test_prune_removes_expired_only(
    tmp_path: Path,
) -> None:
    cache = LinkCache(
        tmp_path / "links.json",
        ttl=1,
    )

    cache.put("u1", "f1", "video/mp4", "direct")

    time.sleep(1.1)

    cache.put("u2", "f2", "video/mp4", "direct")

    assert cache.count() == 2
    assert cache.prune() == 1
    assert cache.count() == 1
    assert cache.get("u2") is not None


def test_clear_removes_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "links.json"
    cache = LinkCache(path)

    cache.put("u", "f", "video/mp4", "direct")

    assert path.exists()

    cache.clear()

    assert not path.exists()


def test_corrupt_file_is_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "links.json"

    path.write_text("{not json", encoding="utf-8")

    cache = LinkCache(path)

    assert cache.get("u") is None
    assert cache.count() == 0


def test_wrong_version_is_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "links.json"

    path.write_text(
        '{"version": 999, "entries": {"u": {}}}',
        encoding="utf-8",
    )

    assert LinkCache(path).count() == 0


def test_non_dict_payload_is_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "links.json"

    path.write_text("[]", encoding="utf-8")

    assert LinkCache(path).count() == 0


def test_entry_dict_roundtrip() -> None:
    entry = CacheEntry(
        url="u",
        final_url="f",
        content_type="video/mp4",
        strategy="direct",
        resolved_at=123.0,
    )

    restored = CacheEntry.from_dict(
        entry.to_dict()
    )

    assert restored == entry


def test_write_is_atomic(
    tmp_path: Path,
) -> None:
    """
    A crash mid-write must not leave a half-written cache, which
    would otherwise discard every cached link on the next run.
    """

    path = tmp_path / "links.json"
    cache = LinkCache(path)

    cache.put("u", "f", "video/mp4", "direct")

    assert not path.with_suffix(".tmp").exists()
    assert path.exists()


def test_key_is_stable() -> None:
    assert LinkCache._key("a") == LinkCache._key("a")
    assert LinkCache._key("a") != LinkCache._key("b")


@pytest.mark.parametrize("version", [CACHE_VERSION])
def test_current_version_is_int(
    version: int,
) -> None:
    assert isinstance(version, int)
