"""On-disk cache for resolved links.

Resolution can be slow and, on metered connections, expensive. Caching
keeps repeat runs quick. Entries expire because these links are
short-lived and a stale one produces a confusing 403 rather than an
obvious failure.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger

logger = get_logger(__name__)

#: Resolved links are assumed stale after this long.
DEFAULT_TTL_SECONDS = 3600

CACHE_VERSION = 1


@dataclass(frozen=True)
class CacheEntry:
    """One cached resolution."""

    url: str
    final_url: str
    content_type: str
    strategy: str
    resolved_at: float

    def is_fresh(
        self,
        ttl: int,
        now: float | None = None,
    ) -> bool:
        """Return True when the entry is still within its TTL."""

        age = (
            time.time() if now is None else now
        ) - self.resolved_at

        return age < ttl

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-serializable form."""

        return {
            "url": self.url,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "strategy": self.strategy,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
    ) -> CacheEntry:
        """Rebuild an entry from its stored form."""

        return cls(
            url=str(data.get("url", "")),
            final_url=str(
                data.get("final_url", "")
            ),
            content_type=str(
                data.get("content_type", "")
            ),
            strategy=str(
                data.get("strategy", "")
            ),
            resolved_at=float(
                data.get("resolved_at", 0.0)
            ),
        )


class LinkCache:
    """A JSON file mapping original URLs to resolved URLs."""

    def __init__(
        self,
        path: Path,
        *,
        ttl: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.path = path
        self.ttl = ttl

    @staticmethod
    def _key(
        url: str,
    ) -> str:
        """Return a stable filename-safe key for a URL."""

        return hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()[:32]

    def get(
        self,
        url: str,
    ) -> CacheEntry | None:
        """Return a fresh entry for a URL, or None."""

        entry = self._read().get(url)

        if entry is None:
            return None

        if not entry.is_fresh(self.ttl):
            logger.debug(
                "cache miss (stale) for %s",
                url[:80],
            )

            return None

        return entry

    def put(
        self,
        url: str,
        final_url: str,
        content_type: str,
        strategy: str,
    ) -> CacheEntry:
        """Store a resolution and return the entry."""

        entry = CacheEntry(
            url=url,
            final_url=final_url,
            content_type=content_type,
            strategy=strategy,
            resolved_at=time.time(),
        )

        data = self._read()
        data[url] = entry
        self._write(data)

        return entry

    def prune(self) -> int:
        """Drop expired entries and return how many were removed."""

        data = self._read()

        fresh = {
            url: entry
            for url, entry in data.items()
            if entry.is_fresh(self.ttl)
        }

        removed = len(data) - len(fresh)

        if removed:
            self._write(fresh)

        return removed

    def count(self) -> int:
        """Return the number of stored entries, stale or not."""

        return len(self._read())

    def clear(self) -> None:
        """Remove every entry."""

        self.path.unlink(missing_ok=True)

    def _read(self) -> dict[str, CacheEntry]:
        """Load the cache file, tolerating corruption."""

        if not self.path.exists():
            return {}

        try:
            raw = json.loads(
                self.path.read_text(
                    encoding="utf-8"
                )
            )

        except (OSError, json.JSONDecodeError) as error:
            logger.warning(
                "ignoring unreadable cache %s: %s",
                self.path,
                error,
            )

            return {}

        if not isinstance(raw, dict):
            return {}

        if raw.get("version") != CACHE_VERSION:
            return {}

        entries = raw.get("entries", {})

        if not isinstance(entries, dict):
            return {}

        result: dict[str, CacheEntry] = {}

        for url, data in entries.items():
            if not isinstance(data, dict):
                continue

            result[url] = CacheEntry.from_dict(
                data
            )

        return result

    def _write(
        self,
        data: dict[str, CacheEntry],
    ) -> None:
        """Persist the cache atomically."""

        payload = {
            "version": CACHE_VERSION,
            "entries": {
                url: entry.to_dict()
                for url, entry in data.items()
            },
        }

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary = self.path.with_suffix(".tmp")

        temporary.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

        temporary.replace(self.path)
