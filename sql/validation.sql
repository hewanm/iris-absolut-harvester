-- IRIS-SIG-49 validation queries for iris_staging.signal_ingest
-- Run: psql "$DATABASE_URL" -f sql/validation.sql
-- Every check is written so that an EMPTY result / zero count means "pass".

\echo '1. Duplicate source identifiers (expect 0 rows)'
SELECT source_system, source_record_id, count(*)
FROM iris_staging.signal_ingest
GROUP BY 1, 2
HAVING count(*) > 1;

\echo '2. Same canonical URL under different identifiers (expect 0 rows; flagged duplicate_url_different_guid)'
SELECT source_url_canonical, array_agg(source_record_id) AS ids
FROM iris_staging.signal_ingest
GROUP BY 1
HAVING count(*) > 1;

\echo '3. Geometry problems: wrong SRID, invalid, or outside Germany (expect 0)'
SELECT count(*) AS bad_geometries
FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL
  AND (ST_SRID(geom) <> 25832 OR NOT ST_IsValid(geom)
       OR NOT ST_Intersects(geom, ST_MakeEnvelope(230000, 5230000, 1000000, 6130000, 25832)));

\echo '4. Geometry / status mismatch (expect 0)'
SELECT count(*) AS mismatches
FROM iris_staging.signal_ingest
WHERE (geo_status = 'resolved') <> (geom IS NOT NULL);

\echo '5. Resolved points back-projected to WGS84 (spot check)'
SELECT location_text,
       round(ST_X(geom)::numeric, 1) AS e_25832, round(ST_Y(geom)::numeric, 1) AS n_25832,
       round(ST_X(ST_Transform(geom, 4326))::numeric, 5) AS lon,
       round(ST_Y(ST_Transform(geom, 4326))::numeric, 5) AS lat,
       geo_method, geo_confidence
FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL
ORDER BY location_text;

\echo '6. Missing / suspicious fields (visible, not silently dropped)'
SELECT count(*) FILTER (WHERE published_at IS NULL)              AS missing_pubdate,
       count(*) FILTER (WHERE published_at > now() + interval '1 day') AS future_pubdate,
       count(*) FILTER (WHERE summary IS NULL)                   AS missing_summary,
       count(*) FILTER (WHERE language IS NULL)                  AS unknown_language,
       count(*) FILTER (WHERE cardinality(quality_flags) > 0)    AS rows_with_flags,
       count(*)                                                  AS total
FROM iris_staging.signal_ingest;

\echo '7. Quality flag distribution'
SELECT flag, count(*)
FROM iris_staging.signal_ingest, unnest(quality_flags) AS flag
GROUP BY flag
ORDER BY count(*) DESC;

\echo '8. Geographic resolution outcome'
SELECT geo_status, geo_method, count(*)
FROM iris_staging.signal_ingest
GROUP BY 1, 2
ORDER BY 3 DESC;

\echo '9. Ambiguous / unresolved locations to review'
SELECT source_record_id, title, geo_status, location_text, location_candidates
FROM iris_staging.signal_ingest
WHERE geo_status IN ('ambiguous', 'region_only', 'invalid')
ORDER BY published_at DESC NULLS LAST;

\echo '10. Domain filter check: every row carries an objective topic and an allowed geo scope (expect 0)'
SELECT count(*) AS off_objective
FROM iris_staging.signal_ingest
WHERE NOT (topics && ARRAY['institutional_investors', 'regulation_quotas', 'real_assets'])
   OR geo_scope NOT IN ('DE', 'DACH', 'UNKNOWN');

\echo '11. Topic distribution'
SELECT topic, count(*)
FROM iris_staging.signal_ingest, unnest(topics) AS topic
GROUP BY topic
ORDER BY count(*) DESC;

\echo '12. Traceability: every row links to existing runs (expect 0)'
SELECT count(*) AS orphan_rows
FROM iris_staging.signal_ingest s
LEFT JOIN iris_staging.ingest_run r ON r.run_id = s.last_run_id
WHERE r.run_id IS NULL;

\echo '13. Run history'
SELECT run_id, mode, status, started_at, finished_at - started_at AS duration,
       stats->>'entries_seen' AS seen, stats->>'inserted' AS inserted,
       stats->>'updated' AS updated, stats->>'unchanged' AS unchanged,
       jsonb_array_length(errors) AS errors
FROM iris_staging.ingest_run
ORDER BY started_at DESC
LIMIT 20;
