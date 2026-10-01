"""Error hierarchy.

Services raise these; the CLI catches them and renders a precise
message instead of printing a traceback. Resolution failures are kept
separate from download failures because when the target site changes,
those two logs tell you which half broke.
"""

from __future__ import annotations


class HdHubError(Exception):
    """Base class for every error raised by this application."""


class ConfigurationError(HdHubError):
    """Raised when settings are missing or invalid."""


class NetworkError(HdHubError):
    """A request failed or timed out."""


class ParseError(HdHubError):
    """A page could not be understood."""


class StorageError(HdHubError):
    """The index could not be read or written."""


class NoDownloadOption(HdHubError):
    """No option matched the requested quality."""


class DownloadError(HdHubError):
    """The transfer itself failed."""


class ResolutionError(HdHubError):
    """
    A download URL could not be turned into a media link.

    Distinct from :class:`DownloadError` so the terminal can report
    "this link is gated" instead of "the transfer broke".
    """

    def __init__(
        self,
        message: str,
        *,
        url: str = "",
        content_type: str = "",
        strategy: str = "",
    ) -> None:
        super().__init__(message)

        self.url = url
        self.content_type = content_type
        self.strategy = strategy
