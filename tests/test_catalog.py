"""Tests for hdhub4u.catalog."""

from __future__ import annotations

import httpx
import pytest

from hdhub4u import catalog
from hdhub4u.catalog import (
    MediaOption,
    classify_kind,
    download_options,
    extract_options,
    extract_resolution,
    extract_size_label,
    normalize_permalink,
    popular_searches,
    registrable_host,
    result_count,
    search_catalog,
    search_page,
    title_quality,
)
from hdhub4u.errors import NetworkError, ParseError


def _response(
    payload: object,
    status_code: int = 200,
) -> httpx.Response:
    """Build a JSON response for a fake transport."""

    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(
            "GET",
            "https://search.pingora.fyi/x",
        ),
    )


def _client(
    handler,
) -> httpx.Client:
    """Return a client whose requests are answered by a handler."""

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


SEARCH_PAYLOAD = {
    "found": 2,
    "hits": [
        {
            "document": {
                "post_title": (
                    "Dune: Part Two (2024) WEB-DL 1080p"
                ),
                "permalink": (
                    "https://new1.hdhub4u.af/dune-part-two/"
                ),
                "post_thumbnail": "https://img.test/a.jpg",
                "category": ["Action", "HollyWood"],
                "stars": ["Denis Villeneuve"],
                "imdb_id": "tt15239678",
            },
        },
        {
            "document": {
                "post_title": "Dune: Prophecy Season 1",
                "permalink": (
                    "https://new1.hdhub4u.af/dune-prophecy/"
                ),
                "category": ["WEB-Series"],
                "imdb_id": "",
            },
        },
    ],
}


class TestSearchCatalog:
    def test_returns_items_in_site_order(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _response(SEARCH_PAYLOAD)

        client = _client(handler)

        items = search_catalog("dune", client=client)

        assert [item.url for item in items] == [
            "https://new1.hdhub4u.free/dune-part-two/",
            "https://new1.hdhub4u.free/dune-prophecy/",
        ]

    def test_sends_the_headers_the_endpoint_requires(
        self,
    ) -> None:
        """
        The endpoint answers 403 without a site Origin, so the
        browser-shaped headers are part of the contract.
        """

        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return _response(SEARCH_PAYLOAD)

        search_catalog("dune", client=_client(handler))

        assert seen["origin"] == catalog.SITE_ORIGIN
        assert "search.html" in seen["referer"]

    def test_sends_the_sites_query_parameters(
        self,
    ) -> None:
        params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            params.update(dict(request.url.params))
            return _response(SEARCH_PAYLOAD)

        search_catalog("dune", limit=7, page=3, client=_client(handler))

        assert params["q"] == "dune"
        assert params["query_by"] == catalog.QUERY_BY
        assert params["query_by_weights"] == catalog.QUERY_BY_WEIGHTS
        assert params["sort_by"] == "sort_by_date:desc"
        assert params["limit"] == "7"
        assert params["page"] == "3"

    def test_extracts_the_year_from_the_title(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _response(SEARCH_PAYLOAD)

        items = search_catalog("dune", client=_client(handler))

        assert items[0].year == "2024"
        assert items[1].year == ""

    def test_blank_query_makes_no_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not reach the network")

        assert search_catalog("   ", client=_client(handler)) == []

    def test_limit_is_clamped(self) -> None:
        params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            params.update(dict(request.url.params))
            return _response({"found": 0, "hits": []})

        search_catalog("dune", limit=5000, client=_client(handler))

        assert params["limit"] == str(catalog.MAX_LIMIT)

    def test_skips_hits_without_a_title(self) -> None:
        payload = {
            "found": 2,
            "hits": [
                {"document": {"permalink": "https://x.test/a/"}},
                {
                    "document": {
                        "post_title": "Real",
                        "permalink": "https://x.test/b/",
                    },
                },
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(payload)

        items = search_catalog("x", client=_client(handler))

        assert [item.title for item in items] == ["Real"]

    def test_reports_a_rejected_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                request=request,
            )

        with pytest.raises(NetworkError) as info:
            search_catalog("dune", client=_client(handler))

        assert "403" in str(info.value)

    def test_reports_a_non_json_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html>nope</html>",
                request=request,
            )

        with pytest.raises(ParseError):
            search_catalog("dune", client=_client(handler))

    def test_reads_a_bare_list_payload(self) -> None:
        payload = [
            {
                "document": {
                    "post_title": "Bare",
                    "permalink": "https://x.test/bare/",
                },
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(payload)

        items = search_catalog("bare", client=_client(handler))

        assert items[0].title == "Bare"


class TestPopularSearches:
    def test_returns_unique_terms(self) -> None:
        payload = {
            "hits": [
                {"document": {"q": "dune", "count": 9}},
                {"document": {"q": "dune", "count": 4}},
                {"document": {"q": "firr", "count": 2}},
                {"document": {"q": "", "count": 1}},
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(payload)

        assert popular_searches(client=_client(handler)) == [
            "dune",
            "firr",
        ]

    def test_a_broken_endpoint_is_not_fatal(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="not json",
                request=request,
            )

        assert popular_searches(client=_client(handler)) == []


class TestPermalinks:
    def test_rebases_onto_the_live_origin(self) -> None:
        """
        The index stores a sibling domain that rotates. The site
        itself uses only the path, so the path is what is used.
        """

        assert normalize_permalink(
            "https://new1.hdhub4u.af/dune-part-two/"
        ) == "https://new1.hdhub4u.free/dune-part-two/"

    def test_keeps_the_query_string(self) -> None:
        """
        A permalink's query is part of the address. Dropping it
        silently asked the site for a different post, and the
        caller had no way to tell the result was not what it
        searched for.
        """

        assert normalize_permalink(
            "https://new1.hdhub4u.af/dune-part-two/?p=2&lang=en"
        ) == (
            "https://new1.hdhub4u.free/dune-part-two/?p=2&lang=en"
        )

    def test_keeps_a_relative_permalink_query(self) -> None:
        assert normalize_permalink(
            "dune-part-two?p=3"
        ) == "https://new1.hdhub4u.free/dune-part-two?p=3"

    def test_a_bare_question_mark_adds_nothing(self) -> None:
        """
        ``?`` with nothing after it is not a query, and appending it
        would make an address that differs from the clean one for no
        reason.
        """

        assert normalize_permalink(
            "https://new1.hdhub4u.af/dune-part-two/?"
        ) == "https://new1.hdhub4u.free/dune-part-two/"

    def test_keeps_a_relative_permalink(self) -> None:
        assert normalize_permalink("/dune/") == (
            "https://new1.hdhub4u.free/dune/"
        )

    def test_defaults_to_the_root(self) -> None:
        assert normalize_permalink("") == (
            "https://new1.hdhub4u.free/"
        )


class TestHostClassification:
    def test_recognises_file_hosts(self) -> None:
        assert (
            classify_kind("https://hubcdn.club/file/abc")
            == catalog.DOWNLOAD_KIND
        )
        assert (
            classify_kind("https://hubdrive.pics/file/123")
            == catalog.DOWNLOAD_KIND
        )

    def test_recognises_player_hosts(self) -> None:
        assert (
            classify_kind("https://greenmotors.club/?id=x")
            == catalog.STREAMING_KIND
        )
        assert (
            classify_kind("https://hdstream4u.com/file/x")
            == catalog.STREAMING_KIND
        )

    def test_a_quality_label_does_not_imply_a_download(
        self,
    ) -> None:
        """
        Both hosts carry identical-looking labels. Only the host
        says which one serves bytes.
        """

        assert classify_kind(
            "https://hubcdn.club/file/a"
        ) != classify_kind("https://greenmotors.club/?id=a")

    def test_unknown_hosts_are_neither(self) -> None:
        assert (
            classify_kind("https://example.com/1080p.mkv")
            == catalog.UNKNOWN_KIND
        )

    def test_a_subdomain_matches_its_parent(self) -> None:
        assert (
            classify_kind("https://hubcdn.wiki/file/a")
            == catalog.DOWNLOAD_KIND
        )

    def test_www_is_ignored(self) -> None:
        assert (
            classify_kind("https://www.hubdrive.pics/file/1")
            == catalog.DOWNLOAD_KIND
        )

    def test_strips_port_and_credentials(self) -> None:
        assert (
            registrable_host("https://user:pw@hubcdn.club:443/f")
            == "hubcdn.club"
        )


POST_HTML = """
<html><body>
  <h3><a href="https://hubcdn.club/file/AAA">480p x264 [1.5GB]</a></h3>
  <h4><em><a href="https://hubdrive.pics/file/BBB">
      1080p 10Bit HEVC
      [1.8GB]</a></em></h4>
  <a href="https://greenmotors.club/?id=CCC">720p x264 [830MB]</a>
  <a href="https://hdstream4u.com/file/DDD">WATCH</a>
  <a href="https://4khdhub.one/another-post/">Another Post</a>
  <a href="https://catimages.org/image/ZZ">4K</a>
  <a href="/related-movie/">Related 1080p</a>
  <a href="https://hubcdn.club/file/AAA">480p duplicate</a>
</body></html>
"""


class TestExtractOptions:
    def test_finds_downloads_and_streams(self) -> None:
        options = extract_options(POST_HTML)

        assert [option.label for option in options] == [
            "480p x264 [1.5GB]",
            "1080p 10Bit HEVC [1.8GB]",
            "720p x264 [830MB]",
            "WATCH",
        ]

    def test_ignores_links_to_other_pages(self) -> None:
        hosts = {option.host for option in extract_options(POST_HTML)}

        assert "4khdhub.one" not in hosts
        assert "catimages.org" not in hosts

    def test_ignores_same_site_links(self) -> None:
        urls = [option.url for option in extract_options(POST_HTML)]

        assert not any(
            url.startswith(catalog.SITE_ORIGIN)
            for url in urls
        )

    def test_collapses_whitespace_in_labels(self) -> None:
        labels = [
            option.label for option in extract_options(POST_HTML)
        ]

        assert "1080p 10Bit HEVC [1.8GB]" in labels

    def test_deduplicates_repeated_urls(self) -> None:
        urls = [option.url for option in extract_options(POST_HTML)]

        assert len(urls) == len(set(urls))

    def test_parses_size_and_resolution(self) -> None:
        option = extract_options(POST_HTML)[1]

        assert option.resolution == "1080p"
        assert option.size_label == "1.8GB"

    def test_download_options_filters_streams(self) -> None:
        options = extract_options(POST_HTML)

        assert [option.label for option in download_options(options)] == [
            "480p x264 [1.5GB]",
            "1080p 10Bit HEVC [1.8GB]",
        ]

    def test_a_page_with_no_options(self) -> None:
        assert extract_options("<html><body></body></html>") == []

    def test_relative_gate_urls_resolve(self) -> None:
        html = '<a href="/file/x">1080p [1GB]</a>'

        assert extract_options(html, "https://hubcdn.club/")[0].url == (
            "https://hubcdn.club/file/x"
        )


class TestLabelHelpers:
    def test_reads_a_resolution(self) -> None:
        assert extract_resolution("4K/2160p SDR WEB-DL") == "2160p"
        assert extract_resolution("720p x264") == "720p"

    def test_reads_a_size(self) -> None:
        assert extract_size_label("480p [1.1GB]") == "1.1GB"
        assert extract_size_label("480p⚡[440MB]") == "440MB"

    def test_returns_nothing_when_absent(self) -> None:
        assert extract_resolution("Full Movie") == ""
        assert extract_size_label("Full Movie") == ""


class TestTitleQuality:
    """
    The site's titles carry their own quality, so it is read off the
    title rather than making a second request to ask. It reaches the
    terminal table and the --json escape hatch alike, so both agree.
    """

    def test_reads_what_the_title_says(self) -> None:
        title = "Dune (2024) 4K 1080p 720p 480p Dual Audio"

        assert title_quality(title) == "4K 1080P 720P 480P"

    def test_reports_in_the_order_written(self) -> None:
        """
        Newest first is how the site writes it, and a release is
        listed largest-first for a reason: the order is information.
        """

        assert (
            title_quality("Show 1080p 720p 480p")
            == "1080P 720P 480P"
        )

    def test_repeats_are_reported_once(self) -> None:
        assert title_quality("Show 720p 720p") == "720P"

    def test_a_title_naming_nothing_gives_nothing(self) -> None:
        """
        The caller turns an empty answer into a placeholder, so it
        must be empty rather than a guess.
        """

        assert title_quality("Dune Full Movie") == ""

    def test_only_whole_tokens_count(self) -> None:
        """
        "1080px" is not a resolution the release advertises, and
        reading it as one would report a quality that is not there.
        """

        assert title_quality("Show 1080px") == ""

    def test_bracketed_tokens_are_understood(self) -> None:
        assert title_quality("Show [1080p] (720p)") == "1080P 720P"

    def test_a_slash_does_not_hide_a_resolution(self) -> None:
        assert title_quality("Show 1080p/720p") == "1080P 720P"

    def test_the_count_is_capped(self) -> None:
        assert (
            len(title_quality("Show 480p 720p 1080p 2160p 4k").split())
            == 4
        )


class TestResultCount:
    def test_reads_found(self) -> None:
        assert result_count({"found": 12}) == 12

    def test_tolerates_junk(self) -> None:
        assert result_count({"found": "x"}) == 0
        assert result_count([]) == 0
        assert result_count(None) == 0


class TestSearchPage:
    def test_keeps_the_total_the_service_reported(self) -> None:
        """
        The page size and the search size are different numbers, and
        the only way a footer can report the size of a search is if
        something carries it out of the payload.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(SEARCH_PAYLOAD)

        found = search_page("dune", client=_client(handler))

        assert found.total == 2
        assert len(found.items) == 2

    def test_an_uncounted_payload_reports_no_total(self) -> None:
        """
        A missing count is not a count of zero. Zero results is an
        empty page; an uncounted one is a full page beside no number.
        """

        payload = {
            "hits": SEARCH_PAYLOAD["hits"],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(payload)

        found = search_page("dune", client=_client(handler))

        assert found.total == 0
        assert len(found.items) == 2

    def test_an_empty_query_is_an_empty_uncounted_page(self) -> None:
        found = search_page("   ")

        assert list(found) == []
        assert found.total == 0

    def test_it_behaves_like_a_sequence(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _response(SEARCH_PAYLOAD)

        found = search_page("dune", client=_client(handler))

        assert [item.title for item in found] == [
            "Dune: Part Two (2024) WEB-DL 1080p",
            "Dune: Prophecy Season 1",
        ]
        assert [item.year for item in found] == ["2024", ""]

    def test_search_catalog_still_returns_only_the_items(self) -> None:
        """
        The count is only useful to a caller that renders a list, so
        the plain list accessor keeps its old shape.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(SEARCH_PAYLOAD)

        items = search_catalog("dune", client=_client(handler))

        assert isinstance(items, list)
        assert len(items) == 2
        assert items[0].year == "2024"


def test_option_dataclass_defaults() -> None:
    option = MediaOption(
        label="720p",
        url="https://hubcdn.club/f",
        kind=catalog.DOWNLOAD_KIND,
        host="hubcdn.club",
    )

    assert option.is_download is True
    assert option.resolution == ""
    assert option.size_label == ""
