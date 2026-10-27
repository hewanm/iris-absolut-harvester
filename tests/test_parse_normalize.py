from datetime import datetime, timezone

import pytest

from iris_harvester.normalize import RejectedEntry, canonical_url, detect_language, html_to_text, normalize_entry
from iris_harvester.parse import MalformedFeedError, RawEntry, parse_feed


def test_parse_rss_page(fixtures_dir):
    feed = parse_feed((fixtures_dir / "feed_page1.xml").read_bytes())
    assert feed.version == "rss20"
    assert feed.language == "de-DE"
    assert len(feed.entries) == 12
    first = feed.entries[0]
    assert first.guid == "https://www.absolut-report.example/?p=900101"
    assert first.published_at == datetime(2026, 10, 26, 8, 15, tzinfo=timezone.utc)
    assert first.categories == ["Immobilien", "Asset Allocation"]
    assert first.authors == ["Redaktion Test"]


def test_parse_atom_normalises_timezone_to_utc(fixtures_dir):
    feed = parse_feed((fixtures_dir / "feed_atom.xml").read_bytes())
    assert feed.version == "atom10"
    assert feed.entries[0].published_at == datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
    assert feed.entries[1].published_at is None and feed.entries[1].updated_at is not None


def test_empty_but_valid_channel_is_not_an_error(fixtures_dir):
    feed = parse_feed((fixtures_dir / "feed_empty.xml").read_bytes())
    assert feed.entries == [] and feed.version == "rss20"


@pytest.mark.parametrize("payload", [b"", b"   ", None])
def test_empty_body_is_malformed(payload):
    with pytest.raises(MalformedFeedError):
        parse_feed(payload)


def test_html_page_is_malformed(fixtures_dir):
    with pytest.raises(MalformedFeedError):
        parse_feed((fixtures_dir / "feed_malformed.html").read_bytes())


def test_truncated_xml_is_recovered_but_flagged(fixtures_dir):
    feed = parse_feed((fixtures_dir / "feed_truncated.xml").read_bytes())
    assert feed.bozo is True
    assert len(feed.entries) >= 1


def test_canonical_url_strips_tracking_and_fragment():
    url = "HTTPS://WWW.Absolut-Report.example:443/2026/10/x/?utm_source=rss&b=2&a=1#comments"
    assert canonical_url(url) == "https://www.absolut-report.example/2026/10/x/?a=1&b=2"


def test_html_to_text_strips_markup_and_wordpress_suffix():
    raw = "<p>Erste&nbsp;Zeile<br/>zweite &amp; dritte [&#8230;]</p>"
    assert html_to_text(raw) == "Erste Zeile zweite & dritte …"
    assert html_to_text("Text. The post X appeared first on Y.") == "Text."


def test_language_detection_per_item_with_channel_fallback():
    assert detect_language("The investors and the insurers are in Germany", "de-DE") == "en"
    assert detect_language("Die Kasse und der Fonds sind für die Anlage", None) == "de"
    assert detect_language("Solvency II", "de-DE") == "de"


def _entry(**kw):
    base = dict(guid="g1", link="https://a.example/x", title="Titel", summary_html="Text",
                published_at=datetime(2026, 10, 1, tzinfo=timezone.utc), updated_at=None, raw={})
    base.update(kw)
    return RawEntry(**base)


def test_missing_title_is_rejected():
    with pytest.raises(RejectedEntry) as exc:
        normalize_entry(_entry(title="  "), feed_url=None, feed_language="de")
    assert exc.value.reason == "missing_title"


def test_missing_identifier_is_rejected():
    with pytest.raises(RejectedEntry) as exc:
        normalize_entry(_entry(guid=None, link=None), feed_url=None, feed_language="de")
    assert exc.value.reason == "missing_identifier"


def test_missing_guid_falls_back_to_canonical_url():
    cand = normalize_entry(_entry(guid=None, link="https://a.example/x/?utm_medium=rss"),
                           feed_url=None, feed_language="de")
    assert cand.source_record_id == "https://a.example/x/"
    assert "guid_missing_used_url" in cand.quality_flags


def test_missing_and_future_dates_are_flagged_not_dropped():
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    cand = normalize_entry(_entry(published_at=None), feed_url=None, feed_language="de", now=now)
    assert cand.published_at is None and "missing_pubdate" in cand.quality_flags
    cand = normalize_entry(_entry(published_at=None, raw={"published": "gestern"}),
                           feed_url=None, feed_language="de", now=now)
    assert "unparseable_pubdate" in cand.quality_flags
    cand = normalize_entry(_entry(published_at=datetime(2027, 1, 1, tzinfo=timezone.utc)),
                           feed_url=None, feed_language="de", now=now)
    assert "future_pubdate" in cand.quality_flags


def test_long_summary_is_truncated_and_flagged():
    cand = normalize_entry(_entry(summary_html="x" * 5000), feed_url=None, feed_language="de",
                           summary_max_chars=100)
    assert len(cand.summary) == 100 and "summary_truncated" in cand.quality_flags


def test_content_hash_is_stable_and_content_sensitive():
    a = normalize_entry(_entry(), feed_url=None, feed_language="de")
    b = normalize_entry(_entry(), feed_url=None, feed_language="de")
    c = normalize_entry(_entry(title="Anderer Titel"), feed_url=None, feed_language="de")
    assert a.content_hash() == b.content_hash() != c.content_hash()
