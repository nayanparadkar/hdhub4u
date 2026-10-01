"""Download quality options, matching and automatic selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

#: Resolutions we recognise, largest first.
KNOWN_RESOLUTIONS = (
    2160,
    1440,
    1080,
    720,
    480,
    360,
    240,
)

_RESOLUTION_PATTERN = re.compile(
    r"(?<!\d)("
    + "|".join(str(r) for r in KNOWN_RESOLUTIONS)
    + r")p(?!\d)",
    re.IGNORECASE,
)

_SIZE_PATTERN = re.compile(
    r"\[\s*(\d+(?:\.\d+)?)\s*(gb|mb)\s*\]",
    re.IGNORECASE,
)

_SIZE_UNITS = {
    "gb": 1024**3,
    "mb": 1024**2,
}


@dataclass(frozen=True)
class QualityOption:
    """A selectable download quality."""

    key: str
    label: str
    value: str


#: Pick the smallest available file without prompting.
MINIMUM_QUALITY = QualityOption(
    key="a",
    label="Smallest available",
    value="minimum",
)

QUALITY_OPTIONS: tuple[QualityOption, ...] = (
    QualityOption(
        key="1",
        label="Best available",
        value="best",
    ),
    QualityOption(
        key="2",
        label="1080p",
        value="1080p",
    ),
    QualityOption(
        key="3",
        label="720p",
        value="720p",
    ),
    QualityOption(
        key="4",
        label="480p",
        value="480p",
    ),
    QualityOption(
        key="5",
        label="360p",
        value="360p",
    ),
    MINIMUM_QUALITY,
)

ALL_QUALITY_CHOICES = QUALITY_OPTIONS


def get_quality_options() -> tuple[QualityOption, ...]:
    """Return the qualities offered in the interactive menu."""

    return QUALITY_OPTIONS


def get_quality(
    key: str,
) -> QualityOption | None:
    """Look up a quality by its menu key."""

    key = key.strip()

    for option in ALL_QUALITY_CHOICES:
        if option.key == key:
            return option

    return None


def extract_resolution(
    title: str,
) -> int | None:
    """
    Extract a vertical resolution from an option title.

    Returns None when the title carries no resolution marker.
    """

    match = _RESOLUTION_PATTERN.search(title)

    if not match:
        return None

    return int(match.group(1))


def extract_size_bytes(
    title: str,
) -> int | None:
    """
    Extract a bracketed file size such as [1.1GB] or [780MB].

    Returns None when the title carries no size marker. Size is used
    only as a tie-breaker between equal resolutions.
    """

    match = _SIZE_PATTERN.search(title)

    if not match:
        return None

    value, unit = match.groups()

    return int(
        float(value) * _SIZE_UNITS[unit.lower()]
    )


def matches_quality(
    title: str,
    quality: QualityOption,
) -> bool:
    """Return True when an option title suits this quality."""

    if quality.value in {"best", "minimum"}:
        return True

    resolution = extract_resolution(title)

    if resolution is None:
        return False

    return resolution == int(
        quality.value.removesuffix("p")
    )


def find_quality_options(
    options: Sequence[dict[str, str]],
    quality: QualityOption,
) -> list[dict[str, str]]:
    """Return every option matching the requested quality."""

    if quality.value in {"best", "minimum"}:
        return list(options)

    return [
        option
        for option in options
        if matches_quality(
            option.get("title", ""),
            quality,
        )
    ]


def sort_by_size(
    options: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Order options smallest first.

    Resolution decides the order. Options without a resolution sort
    last because their true size is unknown. Equal resolutions are
    broken by file size when present, so a smaller encode of the same
    resolution wins.
    """

    def key(
        option: dict[str, str],
    ) -> tuple[int, int]:
        title = option.get("title", "")

        resolution = extract_resolution(title)

        if resolution is None:
            return (1, 0)

        size = extract_size_bytes(title)

        if size is None:
            return (0, resolution)

        return (0, size)

    return sorted(options, key=key)


def select_smallest(
    options: Sequence[dict[str, str]],
) -> dict[str, str] | None:
    """
    Return the smallest option, or None when none qualify.

    An option qualifies when it declares a resolution. Options with
    no recognisable resolution are skipped because the caller asked
    for the minimum and silently downloading an unknown size would
    defeat that.
    """

    if not options:
        return None

    qualified = [
        option
        for option in options
        if extract_resolution(
            option.get("title", "")
        )
        is not None
    ]

    if not qualified:
        return None

    ordered = sort_by_size(qualified)

    return ordered[0]
