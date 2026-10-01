"""Logging setup.

The CLI draws with rich, which owns stdout. Log records therefore go
to stderr so they never corrupt a rendered table, and the default
level is WARNING so a normal run stays quiet.
"""

from __future__ import annotations

import logging
import os
import sys

LOGGER_NAME = "hdhub4u"

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

_VERBOSE_ENV = "HDHUB_LOG_LEVEL"


def get_logger(
    name: str,
) -> logging.Logger:
    """Return a logger namespaced under the application."""

    if name.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(name)

    if name == LOGGER_NAME:
        return logging.getLogger(name)

    return logging.getLogger(
        f"{LOGGER_NAME}.{name}"
    )


def configure_logging(
    level: int | str | None = None,
) -> None:
    """
    Attach a stderr handler once, honouring HDHUB_LOG_LEVEL.

    Safe to call repeatedly; handlers are only added the first time.
    """

    root = logging.getLogger(LOGGER_NAME)

    if root.handlers:
        return

    if level is None:
        level = os.environ.get(
            _VERBOSE_ENV,
            "WARNING",
        ).upper()

    if isinstance(level, str):
        level = getattr(
            logging,
            level.upper(),
            logging.WARNING,
        )

    handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(
        logging.Formatter(LOG_FORMAT)
    )

    root.addHandler(handler)
    root.setLevel(level)

    # The HTTP libraries log every request at DEBUG, which floods
    # the terminal during a crawl.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(
            logging.WARNING
        )
