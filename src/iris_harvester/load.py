"""PostgreSQL/PostGIS persistence: schema, run log, incremental state, idempotent upsert."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any, Iterable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

SCHEMA_SQL = resources.files("iris_harvester") / "sql" / "001_schema.sql"
_LOCK_KEY = 4_918_049  # pg_advisory lock id for this harvester (one writer at a time)

_UPSERT = """
INSERT INTO iris_staging.signal_ingest (
    source_system, source_record_id, source_url, source_url_canonical, feed_url,
    signal_type, title, summary, published_at, authors, source_categories, language,
    topics, matched_terms, relevance_score, geo_scope,
    location_text, location_candidates, geo_method, geo_status, geo_confidence, geom,
    quality_flags, content_hash, raw_payload, first_run_id, last_run_id
) VALUES (
    %(source_system)s, %(source_record_id)s, %(source_url)s, %(source_url_canonical)s, %(feed_url)s,
    %(signal_type)s, %(title)s, %(summary)s, %(published_at)s, %(authors)s, %(source_categories)s, %(language)s,
    %(topics)s, %(matched_terms)s, %(relevance_score)s, %(geo_scope)s,
    %(location_text)s, %(location_candidates)s, %(geo_method)s, %(geo_status)s, %(geo_confidence)s,
    CASE WHEN %(x)s::float8 IS NULL THEN NULL
         ELSE ST_SetSRID(ST_MakePoint(%(x)s::float8, %(y)s::float8), 25832) END,
    %(quality_flags)s, %(content_hash)s, %(raw_payload)s, %(run_id)s, %(run_id)s
)
ON CONFLICT (source_system, source_record_id) DO UPDATE SET
    source_url           = EXCLUDED.source_url,
    source_url_canonical = EXCLUDED.source_url_canonical,
    feed_url             = EXCLUDED.feed_url,
    signal_type          = EXCLUDED.signal_type,
    title                = EXCLUDED.title,
    summary              = EXCLUDED.summary,
    published_at         = EXCLUDED.published_at,
    authors              = EXCLUDED.authors,
    source_categories    = EXCLUDED.source_categories,
    language             = EXCLUDED.language,
    topics               = EXCLUDED.topics,
    matched_terms        = EXCLUDED.matched_terms,
    relevance_score      = EXCLUDED.relevance_score,
    geo_scope            = EXCLUDED.geo_scope,
    location_text        = EXCLUDED.location_text,
    location_candidates  = EXCLUDED.location_candidates,
    geo_method           = EXCLUDED.geo_method,
    geo_status           = EXCLUDED.geo_status,
    geo_confidence       = EXCLUDED.geo_confidence,
    geom                 = EXCLUDED.geom,
    quality_flags        = EXCLUDED.quality_flags,
    content_hash         = EXCLUDED.content_hash,
    raw_payload          = EXCLUDED.raw_payload,
    last_run_id          = EXCLUDED.last_run_id,
    last_seen_at         = now(),
    last_changed_at      = now(),
    change_count         = iris_staging.signal_ingest.change_count + 1
-- update only when the source content OR the derived enrichment changed
-- (e.g. a gazetteer/taxonomy fix); identical re-runs leave rows untouched.
WHERE (iris_staging.signal_ingest.content_hash, iris_staging.signal_ingest.topics,
       iris_staging.signal_ingest.relevance_score, iris_staging.signal_ingest.geo_scope,
       iris_staging.signal_ingest.geo_status, iris_staging.signal_ingest.location_text,
       iris_staging.signal_ingest.quality_flags)
      IS DISTINCT FROM
      (EXCLUDED.content_hash, EXCLUDED.topics, EXCLUDED.relevance_score, EXCLUDED.geo_scope,
       EXCLUDED.geo_status, EXCLUDED.location_text, EXCLUDED.quality_flags)
RETURNING (xmax = 0) AS inserted
"""

_TOUCH = """
UPDATE iris_staging.signal_ingest
   SET last_seen_at = now(), last_run_id = %(run_id)s
 WHERE source_system = %(source_system)s AND source_record_id = %(source_record_id)s
"""


@dataclass
class UpsertCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


class Repository:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, autocommit=False)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is not None:
            self.conn.rollback()
        self.close()

    # -- schema -------------------------------------------------------------
    def ensure_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        self.conn.commit()

    # -- concurrency --------------------------------------------------------
    def try_lock(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
            return bool(cur.fetchone()[0])

    def unlock(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
        self.conn.commit()

    # -- run log ------------------------------------------------------------
    def start_run(self, run_id: UUID, source_system: str, mode: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO iris_staging.ingest_run (run_id, source_system, mode) VALUES (%s, %s, %s)",
                (run_id, source_system, mode),
            )
        self.conn.commit()

    def finish_run(self, run_id: UUID, status: str, stats: dict, errors: list) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE iris_staging.ingest_run
                      SET finished_at = now(), status = %s, stats = %s, errors = %s
                    WHERE run_id = %s""",
                (status, Jsonb(stats), Jsonb(errors), run_id),
            )
        self.conn.commit()

    # -- incremental state --------------------------------------------------
    def get_state(self, feed_url: str) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT etag, last_modified, watermark_pub_at FROM iris_staging.harvest_state WHERE feed_url = %s",
                (feed_url,),
            )
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            return {}
        return {"etag": row[0], "last_modified": row[1], "watermark_pub_at": row[2]}

    def save_state(self, feed_url: str, etag: str | None, last_modified: str | None,
                   watermark: datetime | None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO iris_staging.harvest_state (feed_url, etag, last_modified, watermark_pub_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (feed_url) DO UPDATE SET
                       etag = COALESCE(EXCLUDED.etag, iris_staging.harvest_state.etag),
                       last_modified = COALESCE(EXCLUDED.last_modified, iris_staging.harvest_state.last_modified),
                       watermark_pub_at = GREATEST(EXCLUDED.watermark_pub_at, iris_staging.harvest_state.watermark_pub_at),
                       updated_at = now()""",
                (feed_url, etag, last_modified, watermark),
            )
        self.conn.commit()

    # -- signals ------------------------------------------------------------
    def upsert_signals(self, records: Iterable[dict], run_id: UUID) -> UpsertCounts:
        """Insert new records, update changed ones (content_hash differs), touch unchanged ones.

        Runs in a single transaction: either the whole batch lands or none of it.
        """
        counts = UpsertCounts()
        try:
            with self.conn.cursor() as cur:
                for rec in records:
                    params = _params(rec, run_id)
                    cur.execute(_UPSERT, params)
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(_TOUCH, params)
                        counts.unchanged += 1
                    elif row[0]:
                        counts.inserted += 1
                    else:
                        counts.updated += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return counts


def _params(rec: dict, run_id: UUID) -> dict:
    p = dict(rec)
    p["run_id"] = run_id
    for key in ("matched_terms", "location_candidates", "raw_payload"):
        p[key] = Jsonb(json.loads(json.dumps(p[key], default=str)))
    return p
