"""Tests for retry and backoff."""

from __future__ import annotations

import httpx
import pytest

from hdhub4u.errors import NetworkError
from hdhub4u.http import (
    compute_backoff,
    should_retry_status,
    with_retries,
)


def _status_error(
    code: int,
) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://x.test/")

    response = httpx.Response(
        code,
        request=request,
    )

    return httpx.HTTPStatusError(
        "boom",
        request=request,
        response=response,
    )


@pytest.mark.parametrize(
    "code",
    [408, 425, 429, 500, 502, 503, 504],
)
def test_retryable_statuses(code: int) -> None:
    assert should_retry_status(code)


@pytest.mark.parametrize(
    "code",
    [200, 301, 302, 400, 401, 403, 404, 410],
)
def test_permanent_statuses_are_not_retried(
    code: int,
) -> None:
    assert not should_retry_status(code)


def test_backoff_grows_exponentially() -> None:
    assert compute_backoff(0, jitter=False) == 0.5
    assert compute_backoff(1, jitter=False) == 1.0
    assert compute_backoff(2, jitter=False) == 2.0


def test_jitter_stays_within_half_to_full() -> None:
    for attempt in range(4):
        expected = compute_backoff(
            attempt,
            jitter=False,
        )

        for _ in range(20):
            value = compute_backoff(attempt)

            assert expected * 0.5 <= value <= expected


def test_returns_first_success() -> None:
    slept: list[float] = []

    result = with_retries(
        lambda: "ok",
        sleep=slept.append,
    )

    assert result == "ok"
    assert slept == []


def test_retries_timeouts_then_succeeds() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def operation() -> str:
        calls["n"] += 1

        if calls["n"] < 3:
            raise httpx.ReadTimeout("slow")

        return "ok"

    assert with_retries(
        operation,
        sleep=slept.append,
    ) == "ok"

    assert calls["n"] == 3
    assert len(slept) == 2


def test_retries_retryable_status() -> None:
    calls = {"n": 0}

    def operation() -> str:
        calls["n"] += 1

        if calls["n"] == 1:
            raise _status_error(503)

        return "ok"

    assert with_retries(
        operation,
        sleep=lambda _: None,
    ) == "ok"

    assert calls["n"] == 2


def test_permanent_status_raises_immediately() -> None:
    calls = {"n": 0}

    def operation() -> str:
        calls["n"] += 1

        raise _status_error(404)

    with pytest.raises(httpx.HTTPStatusError):
        with_retries(
            operation,
            sleep=lambda _: None,
        )

    assert calls["n"] == 1


def test_request_errors_are_retried() -> None:
    calls = {"n": 0}

    def operation() -> str:
        calls["n"] += 1

        if calls["n"] < 2:
            raise httpx.ConnectError("no route")

        return "ok"

    assert with_retries(
        operation,
        sleep=lambda _: None,
    ) == "ok"


def test_exhausts_attempts_then_raises() -> None:
    def operation() -> str:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(NetworkError):
        with_retries(
            operation,
            attempts=2,
            sleep=lambda _: None,
        )


def test_retry_on_status_rejects_soft_failure() -> None:
    """
    A 200 that carries HTML is a failure, so the guard lets the
    caller retry instead of accepting an interstitial.
    """

    results = iter([
        "text/html",
        "video/mp4",
    ])

    value = with_retries(
        lambda: next(results),
        sleep=lambda _: None,
        retry_on_status=lambda r: r.startswith("text/"),
    )

    assert value == "video/mp4"


def test_soft_failure_exhausts_attempts() -> None:
    with pytest.raises(NetworkError):
        with_retries(
            lambda: "text/html",
            attempts=2,
            sleep=lambda _: None,
            retry_on_status=lambda r: True,
        )


def test_zero_attempts_is_rejected() -> None:
    with pytest.raises(ValueError):
        with_retries(lambda: "ok", attempts=0)


def test_non_transient_exceptions_propagate() -> None:
    def operation() -> str:
        raise TypeError("bug")

    with pytest.raises(TypeError):
        with_retries(operation)
