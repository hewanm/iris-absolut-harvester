"""Integration tests against a real PostgreSQL 16 / PostGIS 3.4 database.

Run with:  TEST_DATABASE_URL=postgresql://iris:<password>@localhost:5432/iris_test pytest -m db
(the database is created automatically; it must differ from DATABASE_URL)
Skipped automatically when TEST_DATABASE_URL is not set.
"""
import uuid
from dataclasses import replace

import psycopg
import pytest
import responses

from iris_harvester.fetch import FeedFetcher, page_url
from iris_harvester.load import Repository
from iris_harvester.pipeline import Harvester

from .conftest import FEED_URL

pytestmark = pytest.mark.db


def _run(settings, db_url, fixtures):
    with Repository(db_url) as repo:
        return Harvester(settings, repository=repo).run(fixtures=fixtures)


def _q(db_url, sql, *args):
    with psycopg.connect(db_url) as conn:
        return conn.execute(sql, args).fetchall()


def test_rerun_does_not_duplicate(settings, db_url, fixtures_dir):
    pages = [fixtures_dir / "feed_page1.xml", fixtures_dir / "feed_page2.xml"]

    first = _run(settings, db_url, pages)
    assert first.stats["inserted"] == 10

    second = _run(settings, db_url, pages)
    assert (second.stats["inserted"], second.stats["updated"], second.stats["unchanged"]) == (0, 0, 10)

    rows, distinct = _q(db_url, "SELECT count(*), count(DISTINCT (source_system, source_record_id)) "
                                "FROM iris_staging.signal_ingest")[0]
    assert rows == distinct == 10
    # unchanged rows are touched (last_seen/last_run) but not rewritten
    assert _q(db_url, "SELECT count(*) FROM iris_staging.signal_ingest WHERE last_run_id = %s",
              uuid.UUID(second.run_id))[0][0] == 10
    assert _q(db_url, "SELECT max(change_count) FROM iris_staging.signal_ingest")[0][0] == 0


def test_changed_source_record_is_updated_in_place(settings, db_url, fixtures_dir):
    _run(settings, db_url, [fixtures_dir / "feed_page1.xml", fixtures_dir / "feed_page2.xml"])
    report = _run(settings, db_url, [fixtures_dir / "feed_page1_updated.xml", fixtures_dir / "feed_page2.xml"])
    assert (report.stats["inserted"], report.stats["updated"]) == (0, 1)

    title, changes, first_run, last_run = _q(
        db_url, "SELECT title, change_count, first_run_id, last_run_id FROM iris_staging.signal_ingest "
                "WHERE source_record_id = 'https://www.absolut-report.example/?p=900101'")[0]
    assert "20 Prozent" in title and changes == 1 and first_run != last_run
    assert _q(db_url, "SELECT count(*) FROM iris_staging.signal_ingest")[0][0] == 10


def test_geometry_is_epsg25832_and_consistent_with_status(settings, db_url, fixtures_dir):
    _run(settings, db_url, [fixtures_dir / "feed_page1.xml", fixtures_dir / "feed_page2.xml"])
    rows = _q(db_url, "SELECT geo_status, ST_SRID(geom), ST_IsValid(geom) FROM iris_staging.signal_ingest")
    for status, srid, valid in rows:
        if status == "resolved":
            assert srid == 25832 and valid
        else:
            assert srid is None
    # Köln round-trips back to WGS84 within ~1 m
    lon, lat = _q(db_url, "SELECT ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326)) "
                          "FROM iris_staging.signal_ingest WHERE location_text = 'Köln'")[0]
    assert lon == pytest.approx(6.9603, abs=1e-5) and lat == pytest.approx(50.9375, abs=1e-5)


def _raw_insert(db_url, geom_sql, status="resolved"):
    with psycopg.connect(db_url) as conn:
        run = uuid.uuid4()
        conn.execute("INSERT INTO iris_staging.ingest_run (run_id, source_system, mode) VALUES (%s,'t','fixture')",
                     (run,))
        conn.execute(
            f"""INSERT INTO iris_staging.signal_ingest (source_system, source_record_id, source_url,
                    source_url_canonical, signal_type, title, topics, relevance_score, geo_scope, geo_status,
                    geom, content_hash, raw_payload, first_run_id, last_run_id)
                VALUES ('t', %s, 'https://x', 'https://x', 't', 'T', '{{real_assets}}', 5, 'DE', %s,
                        {geom_sql}, repeat('0', 64), '{{}}', %s, %s)""",
            (str(uuid.uuid4()), status, run, run))


@pytest.mark.parametrize("geom_sql", [
    "ST_Transform(ST_SetSRID(ST_MakePoint(2.35, 48.86), 4326), 25832)",   # Paris: outside Germany
    "ST_SetSRID(ST_MakePoint(6.96, 50.94), 25832)",                        # lon/lat mistaken for metres
])
def test_database_rejects_invalid_coordinates(settings, db_url, fixtures_dir, geom_sql):
    _run(settings, db_url, [fixtures_dir / "feed_empty.xml"])  # creates schema
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(db_url, geom_sql)


def test_database_rejects_wrong_srid(settings, db_url, fixtures_dir):
    _run(settings, db_url, [fixtures_dir / "feed_empty.xml"])
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _raw_insert(db_url, "ST_SetSRID(ST_MakePoint(6.96, 50.94), 4326)")


def test_database_rejects_point_without_resolved_status(settings, db_url, fixtures_dir):
    _run(settings, db_url, [fixtures_dir / "feed_empty.xml"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(db_url, "ST_SetSRID(ST_MakePoint(356689, 5644856), 25832)", status="ambiguous")


def test_run_log_records_rejections_and_status(settings, db_url, fixtures_dir):
    report = _run(settings, db_url, [fixtures_dir / "feed_page1.xml"])
    status, errors, stats = _q(db_url, "SELECT status, errors, stats FROM iris_staging.ingest_run WHERE run_id=%s",
                               uuid.UUID(report.run_id))[0]
    assert status == "partial"
    assert any(e["type"] == "rejected_entry" and e["reason"] == "missing_title" for e in errors)
    assert stats["staged_records"] == len(report.records)


def test_concurrent_run_is_refused(settings, db_url, fixtures_dir):
    with Repository(db_url) as holder:
        holder.ensure_schema()
        assert holder.try_lock()
        report = _run(settings, db_url, [fixtures_dir / "feed_page2.xml"])
        assert report.status == "failed" and report.errors[0]["type"] == "concurrent_run"
        holder.unlock()


@responses.activate
def test_live_incremental_run_uses_http_validators(settings, db_url, fixtures_dir):
    s = replace(settings, max_pages=1)
    responses.add(responses.GET, "https://www.absolut-report.example/robots.txt", body="User-agent: *\nAllow: /\n")
    responses.add(responses.GET, page_url(FEED_URL, 1), body=(fixtures_dir / "feed_page2.xml").read_bytes(),
                  content_type="application/rss+xml", headers={"ETag": '"v1"'})
    with Repository(db_url) as repo:
        first = Harvester(s, fetcher=FeedFetcher(s), repository=repo).run()
    assert first.status == "succeeded" and first.stats["inserted"] == 3

    etag, watermark = _q(db_url, "SELECT etag, watermark_pub_at FROM iris_staging.harvest_state")[0]
    assert etag == '"v1"' and watermark is not None

    responses.replace(responses.GET, page_url(FEED_URL, 1), status=304,
                      match=[responses.matchers.header_matcher({"If-None-Match": '"v1"'})])
    with Repository(db_url) as repo:
        second = Harvester(s, fetcher=FeedFetcher(s), repository=repo).run()
    assert second.status == "not_modified" and second.exit_code == 0
    assert _q(db_url, "SELECT count(*) FROM iris_staging.signal_ingest")[0][0] == 3
