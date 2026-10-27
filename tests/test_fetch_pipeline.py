"""HTTP behaviour and end-to-end pipeline behaviour without a database (HTTP is mocked)."""
import pytest
import requests
import responses

from iris_harvester.fetch import AccessRestrictedError, FeedFetcher, FetchError, page_url
from iris_harvester.pipeline import Harvester

from .conftest import FEED_URL

ROBOTS = "https://www.absolut-report.example/robots.txt"
RSS = "application/rss+xml; charset=UTF-8"


def _robots(rsps, body="User-agent: *\nDisallow: /wp-admin/\n", status=200):
    rsps.add(responses.GET, ROBOTS, body=body, status=status)


def _page(n):
    return page_url(FEED_URL, n)


def test_page_url_uses_wordpress_paged_param():
    assert page_url(FEED_URL, 1) == FEED_URL
    assert page_url(FEED_URL, 3) == FEED_URL + "?paged=3"
    assert page_url(FEED_URL + "?paged=9&x=1", 2) == FEED_URL + "?x=1&paged=2"


# --- fetcher ----------------------------------------------------------------

@responses.activate
def test_robots_disallow_is_respected(settings):
    _robots(responses, body="User-agent: *\nDisallow: /feed/\n")
    with pytest.raises(AccessRestrictedError):
        FeedFetcher(settings).fetch(FEED_URL)
    assert len(responses.calls) == 1  # the feed itself was never requested


@responses.activate
def test_missing_robots_txt_allows_access(settings):
    _robots(responses, status=404)
    responses.add(responses.GET, FEED_URL, body="<rss version='2.0'><channel/></rss>", content_type=RSS)
    assert FeedFetcher(settings).fetch(FEED_URL).status == 200


@responses.activate
def test_unreachable_robots_txt_is_treated_as_disallow(settings):
    responses.add(responses.GET, ROBOTS, body=requests.ConnectionError("dns"))
    with pytest.raises(AccessRestrictedError):
        FeedFetcher(settings).fetch(FEED_URL)


@responses.activate
@pytest.mark.parametrize("status", [401, 403, 451])
def test_access_refusals_are_not_retried_or_bypassed(settings, status):
    _robots(responses)
    responses.add(responses.GET, FEED_URL, status=status)
    with pytest.raises(AccessRestrictedError):
        FeedFetcher(settings).fetch(FEED_URL)
    assert len([c for c in responses.calls if c.request.url == FEED_URL]) == 1


@responses.activate
def test_transient_5xx_is_retried(settings):
    _robots(responses)
    responses.add(responses.GET, FEED_URL, status=503)
    responses.add(responses.GET, FEED_URL, body="<rss version='2.0'><channel/></rss>", content_type=RSS)
    assert FeedFetcher(settings).fetch(FEED_URL).status == 200


@responses.activate
def test_persistent_5xx_raises_after_retries(settings):
    _robots(responses)
    for _ in range(settings.max_retries + 1):
        responses.add(responses.GET, FEED_URL, status=500)
    with pytest.raises(FetchError):
        FeedFetcher(settings).fetch(FEED_URL)


@responses.activate
def test_network_error_raises_fetch_error(settings):
    _robots(responses)
    responses.add(responses.GET, FEED_URL, body=requests.ConnectionError("connection reset"))
    with pytest.raises(FetchError):
        FeedFetcher(settings).fetch(FEED_URL)


@responses.activate
def test_conditional_request_headers_and_304(settings):
    _robots(responses)
    responses.add(
        responses.GET, FEED_URL, status=304,
        match=[responses.matchers.header_matcher({"If-None-Match": '"abc"',
                                                  "If-Modified-Since": "Mon, 26 Oct 2026 09:00:00 GMT"})],
    )
    page = FeedFetcher(settings).fetch(FEED_URL, etag='"abc"', last_modified="Mon, 26 Oct 2026 09:00:00 GMT")
    assert page.not_modified


# --- pipeline (no DB) ------------------------------------------------------

def _live(settings):
    return Harvester(settings, fetcher=FeedFetcher(settings))


@responses.activate
def test_live_pagination_until_404(settings, fixtures_dir):
    _robots(responses)
    responses.add(responses.GET, _page(1), body=(fixtures_dir / "feed_page1.xml").read_bytes(), content_type=RSS,
                  headers={"ETag": '"v1"', "Last-Modified": "Mon, 26 Oct 2026 09:00:00 GMT"})
    responses.add(responses.GET, _page(2), body=(fixtures_dir / "feed_page2.xml").read_bytes(), content_type=RSS)
    responses.add(responses.GET, _page(3), status=404)

    report = _live(settings).run()
    assert [p["page"] for p in report.pages] == [1, 2, 3]
    assert report.stats["entries_seen"] == 15
    assert report.stats["duplicates_in_batch"] == 2
    assert len(report.records) == 10
    assert len({r["source_record_id"] for r in report.records}) == 10
    assert report.status == "partial"  # one entry without title/link is rejected and reported
    assert report.rejected[0]["reason"] == "missing_title"
    assert report.exit_code == 4


@responses.activate
def test_server_ignoring_paged_param_does_not_loop(settings, fixtures_dir):
    _robots(responses)
    body = (fixtures_dir / "feed_page2.xml").read_bytes()
    for n in range(1, 6):
        responses.add(responses.GET, _page(n), body=body, content_type=RSS)
    report = _live(settings).run()
    assert report.stats["pagination_loop_detected"] == 1
    assert len(report.pages) == 2


@responses.activate
def test_max_pages_is_honoured(settings, fixtures_dir):
    from dataclasses import replace
    s = replace(settings, max_pages=1)
    _robots(responses)
    responses.add(responses.GET, _page(1), body=(fixtures_dir / "feed_page2.xml").read_bytes(), content_type=RSS)
    report = Harvester(s, fetcher=FeedFetcher(s)).run()
    assert len(report.pages) == 1 and report.status == "succeeded"


@responses.activate
def test_failure_on_page_two_keeps_page_one_and_reports_partial(settings, fixtures_dir):
    _robots(responses)
    responses.add(responses.GET, _page(1), body=(fixtures_dir / "feed_page2.xml").read_bytes(), content_type=RSS)
    responses.add(responses.GET, _page(2), body=requests.ConnectionError("reset"))
    report = _live(settings).run()
    assert report.status == "partial" and len(report.records) == 3
    assert report.errors[0]["type"] == "network" and report.errors[0]["page"] == 2


@responses.activate
def test_obsolete_feed_endpoint_fails_visibly(settings):
    _robots(responses)
    responses.add(responses.GET, _page(1), status=404)
    report = _live(settings).run()
    assert report.status == "failed" and report.errors[0]["type"] == "feed_not_found"
    assert report.exit_code == 1


@responses.activate
def test_html_instead_of_feed_fails_with_malformed(settings, fixtures_dir):
    _robots(responses)
    responses.add(responses.GET, _page(1), body=(fixtures_dir / "feed_malformed.html").read_bytes(),
                  content_type="text/html")
    report = _live(settings).run()
    assert report.status == "failed" and report.exit_code == 3


@responses.activate
def test_access_restricted_exit_code(settings):
    _robots(responses)
    responses.add(responses.GET, _page(1), status=403)
    report = _live(settings).run()
    assert report.status == "failed" and report.exit_code == 2


def test_empty_feed_succeeds_with_zero_records(settings, fixtures_dir):
    report = Harvester(settings).run(fixtures=[fixtures_dir / "feed_empty.xml"], dry_run=True)
    assert report.status == "succeeded" and report.records == [] and report.stats["empty_feed"] == 1


def test_truncated_feed_records_are_flagged(settings, fixtures_dir):
    report = Harvester(settings).run(fixtures=[fixtures_dir / "feed_truncated.xml"], dry_run=True)
    assert report.stats["pages_with_xml_warnings"] == 1
    assert report.records and all("feed_xml_warning" in r["quality_flags"] for r in report.records)


def test_fixture_run_filters_and_geocodes(settings, fixtures_dir):
    report = Harvester(settings).run(
        fixtures=[fixtures_dir / "feed_page1.xml", fixtures_dir / "feed_page2.xml"], dry_run=True)
    by_title = {r["title"]: r for r in report.records}
    assert "Schweizer Pensionskassen erhöhen Hypothekenanteil" not in by_title
    assert "Marktkommentar: Volatilität an den Aktienmärkten bleibt hoch" not in by_title
    assert by_title["Pensionskasse in Köln erhöht Immobilienquote auf 18 Prozent"]["geo_status"] == "resolved"
    assert by_title["Versorgungswerk aus Münster und Stiftung aus Hamburg starten Private-Debt-Mandat"][
        "geo_status"] == "ambiguous"
    assert by_title["Bayerische Sparkassen bündeln Infrastruktur-Spezialfonds"]["geo_status"] == "region_only"
    assert by_title["German insurers lift real estate allocations under Solvency II"]["language"] == "en"
    for r in report.records:
        assert (r["geo_status"] == "resolved") == (r["x"] is not None)
        assert r["content_hash"] and len(r["content_hash"]) == 64
