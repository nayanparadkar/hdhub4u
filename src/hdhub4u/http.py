"""Resilient HTTP helpers with retry and backoff.

Sites under load answer with 429 or 5xx more often than not, and a
crawl that gives up on the first hiccup indexes almost nothing.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from .errors import NetworkError
from .http_types import USER_AGENT
from .logging_setup import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: Status codes worth retrying. Everything else is a real answer.
RETRYABLE_STATUS = frozenset({
    408,
    425,
    429,
    500,
    502,
    503,
    504,
})

DEFAULT_ATTEMPTS = 3

DEFAULT_BACKOFF = 0.5


def should_retry_status(
    status_code: int,
) -> bool:
    """Return True for transient server-side failures."""

    return status_code in RETRYABLE_STATUS


def compute_backoff(
    attempt: int,
    base: float = DEFAULT_BACKOFF,
    *,
    jitter: bool = True,
) -> float:
    """
    Return the delay before the next attempt.

    Exponential growth with optional jitter, so concurrent workers
    do not retry in lockstep.
    """

    delay = base * (2**attempt)

    if not jitter:
        return delay

    return delay * (0.5 + random.random() / 2)


def with_retries(
    operation: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    sleep: Callable[[float], None] = time.sleep,
    retry_on_status: Callable[
        [T],
        bool,
    ] | None = None,
) -> T:
    """
    Run an operation, retrying transient failures.

    ``retry_on_status`` lets a caller reject a result that looks
    successful but is not usable, such as a 200 carrying HTML.
    """

    if attempts < 1:
        raise ValueError(
            "attempts must be at least 1"
        )

    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            result = operation()

        except httpx.TimeoutException as error:
            last_error = error

            logger.warning(
                "timeout on attempt %d/%d: %s",
                attempt + 1,
                attempts,
                error,
            )

        except httpx.HTTPStatusError as error:
            last_error = error

            if not should_retry_status(
                error.response.status_code
            ):
                raise

            logger.warning(
                "HTTP %d on attempt %d/%d",
                error.response.status_code,
                attempt + 1,
                attempts,
            )

        except httpx.RequestError as error:
            last_error = error

            logger.warning(
                "request error on attempt %d/%d: %s",
                attempt + 1,
                attempts,
                error,
            )

        else:
            if (
                retry_on_status is None
                or not retry_on_status(result)
            ):
                return result

            logger.warning(
                "unusable result on attempt %d/%d",
                attempt + 1,
                attempts,
            )

            last_error = NetworkError(
                "retryable result rejected"
            )

        if attempt + 1 < attempts:
            delay = compute_backoff(attempt, backoff)

            logger.info(
                "retrying in %.2fs",
                delay,
            )

            sleep(delay)

    raise NetworkError(
        f"gave up after {attempts} attempts: "
        f"{last_error}"
    ) from last_error


def build_client(
    *,
    timeout: float = 15.0,
    connect_timeout: float = 5.0,
    user_agent: str = USER_AGENT,
    follow_redirects: bool = True,
) -> httpx.Client:
    """Return an httpx client with the project defaults."""

    return httpx.Client(
        follow_redirects=follow_redirects,
        timeout=httpx.Timeout(
            timeout,
            connect=connect_timeout,
        ),
        headers={
            "User-Agent": user_agent,
        },
    )
