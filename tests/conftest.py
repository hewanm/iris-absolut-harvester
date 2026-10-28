import os
from pathlib import Path

import pytest

from iris_harvester.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
FEED_URL = "https://www.absolut-report.example/feed/"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def settings() -> Settings:
    # fast, deterministic settings for tests: no politeness delay, no retry backoff
    return Settings(database_url=os.environ.get("TEST_DATABASE_URL"), feed_url=FEED_URL,
                    max_retries=2, backoff_factor=0, page_delay_s=0, max_pages=5, timeout_s=5)


def _ensure_test_database(url: str) -> None:
    """Create the test database on first use (same server, maintenance db ``postgres``)."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        psycopg.connect(url).close()
        return
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise
    params = conninfo_to_dict(url)
    name = params["dbname"]
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture
def db_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set (start PostGIS: docker compose up -d db)")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    # The tests drop and recreate iris_staging, so never run them against the working database.
    app_url = os.environ.get("DATABASE_URL")
    if app_url and conninfo_to_dict(app_url).get("dbname") == conninfo_to_dict(url).get("dbname") \
            and conninfo_to_dict(app_url).get("host") == conninfo_to_dict(url).get("host"):
        pytest.fail("TEST_DATABASE_URL must point to a separate database (e.g. iris_test), not DATABASE_URL")

    _ensure_test_database(url)
    # every DB test starts from an empty staging schema
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS iris_staging CASCADE")
    return url
