-- IRIS-SIG-49 staging schema (PostgreSQL 16 / PostGIS 3.4)
-- Idempotent: safe to run repeatedly.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS iris_staging;

-- One row per harvester execution: lineage + run statistics + errors.
CREATE TABLE IF NOT EXISTS iris_staging.ingest_run (
    run_id        uuid PRIMARY KEY,
    source_system text        NOT NULL,
    mode          text        NOT NULL CHECK (mode IN ('live', 'fixture')),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text        NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'not_modified')),
    stats         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    errors        jsonb       NOT NULL DEFAULT '[]'::jsonb
);

-- Incremental retrieval state per feed URL (HTTP validators + date watermark).
CREATE TABLE IF NOT EXISTS iris_staging.harvest_state (
    feed_url          text PRIMARY KEY,
    etag              text,
    last_modified     text,
    watermark_pub_at  timestamptz,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS iris_staging.signal_ingest (
    signal_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- traceability / idempotency key
    source_system        text        NOT NULL,
    source_record_id     text        NOT NULL,          -- RSS <guid> / Atom <id>, fallback canonical URL
    source_url           text        NOT NULL,
    source_url_canonical text        NOT NULL,
    feed_url             text,

    -- content
    signal_type          text        NOT NULL,
    title                text        NOT NULL,
    summary              text,                          -- feed excerpt only, never scraped full text
    published_at         timestamptz,                   -- NULL is allowed but flagged (missing_pubdate)
    authors              text[]      NOT NULL DEFAULT '{}',
    source_categories    text[]      NOT NULL DEFAULT '{}',
    language             text,

    -- domain classification
    topics               text[]      NOT NULL,
    matched_terms        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    relevance_score      numeric(6,2) NOT NULL,
    geo_scope            text        NOT NULL,          -- DE | DACH | AT | CH | EU | UNKNOWN

    -- geography
    location_text        text,
    location_candidates  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    geo_method           text,                          -- place_name | institution_hq | region | none
    geo_status           text        NOT NULL
                         CHECK (geo_status IN ('resolved', 'ambiguous', 'region_only', 'not_found', 'invalid')),
    geo_confidence       numeric(3,2) CHECK (geo_confidence IS NULL OR geo_confidence BETWEEN 0 AND 1),
    geom                 geometry(Point, 25832),

    -- quality + lineage
    quality_flags        text[]      NOT NULL DEFAULT '{}',
    content_hash         char(64)    NOT NULL,
    raw_payload          jsonb       NOT NULL,
    first_run_id         uuid        NOT NULL REFERENCES iris_staging.ingest_run (run_id),
    last_run_id          uuid        NOT NULL REFERENCES iris_staging.ingest_run (run_id),
    first_seen_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_at         timestamptz NOT NULL DEFAULT now(),
    last_changed_at      timestamptz NOT NULL DEFAULT now(),
    change_count         integer     NOT NULL DEFAULT 0,

    CONSTRAINT uq_signal_source_record UNIQUE (source_system, source_record_id),

    -- A point is stored if and only if the location was resolved.
    CONSTRAINT ck_geom_matches_status CHECK ((geo_status = 'resolved') = (geom IS NOT NULL)),

    -- Invalid coordinates are never silently accepted: valid, EPSG:25832, inside the
    -- extent of Germany in UTM 32N (incl. eastern Germany beyond the zone edge).
    CONSTRAINT ck_geom_valid_de CHECK (
        geom IS NULL OR (
            ST_IsValid(geom)
            AND ST_SRID(geom) = 25832
            AND ST_X(geom) BETWEEN 230000 AND 1000000
            AND ST_Y(geom) BETWEEN 5230000 AND 6130000
        )
    ),
    CONSTRAINT ck_title_not_blank CHECK (btrim(title) <> '')
);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_geom      ON iris_staging.signal_ingest USING gist (geom);
CREATE INDEX IF NOT EXISTS ix_signal_ingest_published ON iris_staging.signal_ingest (published_at DESC);
CREATE INDEX IF NOT EXISTS ix_signal_ingest_topics    ON iris_staging.signal_ingest USING gin (topics);
CREATE INDEX IF NOT EXISTS ix_signal_ingest_url       ON iris_staging.signal_ingest (source_url_canonical);
