"""Live tests against the real host.

These reach https://new1.hdhub4u.free/ over the network, so they
are marked ``live`` and excluded from the default run. Execute
them with::

    pytest -m live

They are read-only: nothing here downloads media, and the probes
only ever resolve a link far enough to read its content type.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from hdhub4u.database import (
    add_media_batch,
    get_media_count,
    initialize_database,
    newest_media,
)
from hdhub4u.parser import (
    downloadable_options,
    extract_content_links,
    extract_download_options,
    is_category_url,
)
from hdhub4u.resolver import DirectResolver
from hdhub4u.search import (
    calculate_score,
    normalize_text,
    search_media,
)

pytestmark = pytest.mark.live

HOST = "https://new1.hdhub4u.free/"

#: A page known to carry download options.
MOVIE_URL = (
    f"{HOST}"
    "batman-robin-1997-hindi-bluray-full-movie/"
)


@pytest.fixture(scope="module")
def _client():
    with httpx.Client(
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        yield client


@pytest.fixture(scope="module")
def homepage(_client: httpx.Client) -> httpx.Response:
    """Fetch the homepage once for the whole module."""

    response = _client.get(HOST)

    assert response.status_code == 200, response.status_code

    return response


@pytest.fixture(scope="module")
def movie_page(_client: httpx.Client) -> httpx.Response:
    """Fetch one media page once for the whole module."""

    response = _client.get(MOVIE_URL)

    assert response.status_code == 200, response.status_code

    return response


def flaky(fn, attempts: int = 3):
    """
    Retry a network call, for transient failures.

    A handshake timeout against a third-party host is not a defect
    in this code, so a live test should try again rather than report
    a failure the project did not cause.
    """

    last: Exception | None = None

    for attempt in range(attempts):
        try:
            return fn()

        except (httpx.TransportError, OSError) as error:
            last = error

            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))

    raise AssertionError(
        f"gave up after {attempts} attempts: {last}"
    )


@pytest.fixture(scope="module")
def live_options(movie_page: httpx.Response):
    """Download options for the module's media page."""

    return downloadable_options(
        extract_download_options(
            movie_page.text,
            str(movie_page.url),
        )
    )


class TestLiveHostReachable:
    def test_homepage_answers_with_html(
        self,
        homepage: httpx.Response,
    ) -> None:
        assert homepage.status_code == 200
        assert homepage.headers["content-type"].startswith(
            "text/html"
        )
        assert homepage.text

    def test_homepage_carries_a_title(
        self,
        homepage: httpx.Response,
    ) -> None:
        assert "<title" in homepage.text.lower()

    def test_homepage_points_at_the_same_host(
        self,
        homepage: httpx.Response,
    ) -> None:
        assert str(homepage.url).startswith(
            HOST.rstrip("/")
        )


class TestLiveIndexParsing:
    def test_extracts_content_links(
        self,
        homepage: httpx.Response,
    ) -> None:
        links = extract_content_links(
            homepage.text,
            str(homepage.url),
        )

        assert links

    def test_every_link_is_same_host(
        self,
        homepage: httpx.Response,
    ) -> None:
        links = extract_content_links(
            homepage.text,
            str(homepage.url),
        )

        for link in links:
            assert str(
                link["url"]
            ).startswith(HOST.rstrip("/"))

    def test_links_carry_a_usable_type(
        self,
        homepage: httpx.Response,
    ) -> None:
        links = extract_content_links(
            homepage.text,
            str(homepage.url),
        )

        for link in links:
            assert link["type"] in {
                "movie",
                "webseries",
                "episode",
            }
            assert str(link["title"]).strip()

    def test_category_links_are_recognised(
        self,
        homepage: httpx.Response,
    ) -> None:
        assert is_category_url(
            f"{HOST}category/hollywood-movies/"
        )

    def test_urls_are_deduplicated(
        self,
        homepage: httpx.Response,
    ) -> None:
        links = extract_content_links(
            homepage.text,
            str(homepage.url),
        )

        urls = [
            link["url"]
            for link in links
        ]

        assert len(urls) == len(set(urls))


class TestLiveDownloadOptions:
    def test_options_are_found(
        self,
        movie_page: httpx.Response,
    ) -> None:
        options = extract_download_options(
            movie_page.text,
            str(movie_page.url),
        )

        assert options

    def test_options_are_off_site(
        self,
        movie_page: httpx.Response,
    ) -> None:
        options = extract_download_options(
            movie_page.text,
            str(movie_page.url),
        )

        for option in options:
            assert not str(
                option["url"]
            ).startswith(HOST.rstrip("/"))

    def test_streaming_is_excluded(
        self,
        movie_page: httpx.Response,
    ) -> None:
        options = extract_download_options(
            movie_page.text,
            str(movie_page.url),
        )

        downloadable = downloadable_options(
            options
        )

        assert len(downloadable) <= len(options)

        for option in downloadable:
            assert option["type"] != "Streaming"

    def test_download_options_declare_a_type(
        self,
        movie_page: httpx.Response,
    ) -> None:
        options = extract_download_options(
            movie_page.text,
            str(movie_page.url),
        )

        for option in options:
            assert option["type"] in {
                "Download",
                "Streaming",
            }


class TestLiveSearch:
    def test_scores_a_known_title(self) -> None:
        score = calculate_score(
            "batman robin",
            "Batman Robin Full Movie",
        )

        assert score > 0

    def test_unrelated_query_scores_zero(
        self,
    ) -> None:
        assert (
            calculate_score(
                "zzzzqqqxyzzy",
                "Batman Robin Full Movie",
            )
            == 0
        )

    def test_normalization_drops_punctuation(
        self,
    ) -> None:
        assert normalize_text(
            "Batman & Robin!"
        ) == "batman robin"

    def test_search_scores_real_titles(
        self,
        isolated_home: Path,
        homepage: httpx.Response,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Seed from the live homepage so the ranking is exercised
        # against real titles rather than a fixed fixture.
        links = extract_content_links(
            homepage.text,
            str(homepage.url),
        )

        assert links

        monkeypatch.setenv(
            "HDHUB_HOME",
            str(isolated_home),
        )

        initialize_database()

        add_media_batch([
            (
                link["title"],
                link["url"],
                link["type"],
                "",
            )
            for link in links
        ])

        assert get_media_count() == len(links)

        for link in links[:5]:
            words = normalize_text(
                str(link["title"])
            ).split()

            if not words:
                continue

            found = search_media(words[0], limit=5)

            assert found

    def test_newest_rows_are_ordered(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "HDHUB_HOME",
            str(isolated_home),
        )

        initialize_database()

        add_media_batch([
            (
                f"Film {index}",
                f"https://site.test/film-{index}/",
                "movie",
                "",
            )
            for index in range(5)
        ])

        rows = newest_media(3)

        ids = [int(str(row["id"])) for row in rows]

        assert ids == sorted(ids, reverse=True)


class TestLiveMediaHost:
    """A host that does serve files directly."""

    MP4 = (
        "https://download.samplelib.com/mp4/"
        "sample-5s.mp4"
    )

    def test_direct_resolver_finds_media(
        self,
    ) -> None:
        result = flaky(
            lambda: DirectResolver(
                timeout=20.0
            ).resolve(self.MP4)
        )

        assert result.is_media

    def test_html_page_is_not_media(self) -> None:
        result = flaky(
            lambda: DirectResolver(
                timeout=20.0
            ).resolve(HOST)
        )

        assert not result.is_media

    def test_direct_media_reports_an_extension(
        self,
    ) -> None:
        from hdhub4u.http_types import (
            resolve_extension,
        )

        result = flaky(
            lambda: DirectResolver(
                timeout=20.0
            ).resolve(self.MP4)
        )

        assert (
            resolve_extension(
                result.final_url,
                result.content_type,
            )
            == ".mp4"
        )
