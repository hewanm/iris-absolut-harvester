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


@pytest.fixture
def db_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set (start PostGIS: docker compose up -d db)")
    import psycopg

    # every DB test starts from an empty staging schema
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS iris_staging CASCADE")
    return url
