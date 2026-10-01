"""Tests for the error hierarchy."""

from __future__ import annotations

import pytest

from hdhub4u.errors import (
    ConfigurationError,
    DownloadError,
    HdHubError,
    NetworkError,
    NoDownloadOption,
    ParseError,
    ResolutionError,
    StorageError,
)


@pytest.mark.parametrize(
    "error_type",
    [
        ConfigurationError,
        NetworkError,
        ParseError,
        StorageError,
        NoDownloadOption,
        DownloadError,
        ResolutionError,
    ],
)
def test_every_error_derives_from_base(
    error_type: type[HdHubError],
) -> None:
    assert issubclass(error_type, HdHubError)


def test_resolution_error_is_not_a_download_error() -> None:
    """
    Resolution failures must stay distinguishable from transfer
    failures, so the terminal can report them differently.
    """

    assert not issubclass(
        ResolutionError,
        DownloadError,
    )


def test_resolution_error_carries_context() -> None:
    error = ResolutionError(
        "link is gated",
        url="https://gate.test/a",
        content_type="text/html",
        strategy="direct",
    )

    assert str(error) == "link is gated"
    assert error.url == "https://gate.test/a"
    assert error.content_type == "text/html"
    assert error.strategy == "direct"


def test_resolution_error_context_defaults_empty() -> None:
    error = ResolutionError("nope")

    assert error.url == ""
    assert error.content_type == ""
    assert error.strategy == ""
