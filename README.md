# IRIS-SIG-49 · Absolut|report Institutional Asset Allocation Harvester

Ingests the Absolut|report RSS/Atom feed (integration pattern A), keeps only signals about **German
institutional capital allocation, Solvency II / BaFin quotas and real assets**, resolves locations to
**EPSG:25832** points where the text allows it, and loads the result idempotently into
`iris_staging.signal_ingest` (PostgreSQL 16 / PostGIS 3.4).

```
fetch (robots.txt, retries, ETag/If-Modified-Since, ?paged=N)
  → parse (RSS 2.0 / Atom 1.0, tolerant of broken XML)
  → validate & normalise (ids, UTC dates, canonical URLs, HTML → text, quality flags)
  → geocode (institution HQ / city / state → EPSG:25832, range-checked)
  → classify (configurable German taxonomy + geographic scope)
  → de-duplicate within the run
  → upsert ON CONFLICT (source_system, source_record_id)  +  run log
```

Everything can be demonstrated **without network or production access** using the fixtures in
`tests/fixtures/`.

---

## 1. Quick start

Requirements: Python ≥ 3.11, and either Docker (for PostGIS) or a local PostgreSQL 16 + PostGIS 3.4.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # set POSTGRES_PASSWORD etc.; .env is git-ignored

# a) no database at all – full pipeline, writes JSONL/CSV/GeoJSON + run report
iris-harvester run --dry-run \
  --fixture tests/fixtures/feed_page1.xml --fixture tests/fixtures/feed_page2.xml \
  --export-dir out

# b) with PostGIS
docker compose up -d db
set -a; . ./.env; set +a
iris-harvester init-db
iris-harvester run --fixture tests/fixtures/feed_page1.xml --fixture tests/fixtures/feed_page2.xml
iris-harvester run --fixture tests/fixtures/feed_page1.xml --fixture tests/fixtures/feed_page2.xml   # 0 inserted, 10 unchanged
psql "$DATABASE_URL" -f sql/validation.sql

# c) live source (honours robots.txt; reports, never bypasses, access restrictions)
iris-harvester check-source       # read-only verification of the endpoint
iris-harvester run                # live harvest into the database
```

`make install | db | test | test-db | demo | validate | check-source` wraps the same commands.
A containerised run is available with `docker compose --profile app up harvester`.

**Exit codes:** `0` succeeded / not modified · `4` partial (some entries rejected or a later page failed) ·
`2` access restricted (robots.txt, 401/403/451) · `3` malformed feed · `1` other failure.

## 2. Data source and access status

| | |
|---|---|
| Publisher | Absolut Research GmbH, *Absolut\|report* (trade publication for institutional asset management, DACH) |
| Endpoint in brief | `https://www.absolut-report.de/feed/` |
| Pattern | A – RSS/Atom; the `/feed/` path and `?paged=N` pagination are WordPress conventions |
| Verified from build environment (27 Oct 2026) | **HTTP 4xx client error** for automated access. Not established whether this is bot protection or a removed feed. |

In line with the brief, the implementation is therefore **fixture-based and fully tested**, and the live path
is implemented but treats any refusal as a reported failure (`access_restricted`, exit 2) – no header
spoofing, proxies, mirrors or scraping of HTML pages.

**Before relying on the live path, run `iris-harvester check-source`** from a normal network. It prints
robots.txt permission, HTTP status, content type, feed format, entry count and the newest/oldest
publication dates, and writes nothing. Paste its output here:

```
<check-source output, date>
```

Justified alternatives if the feed is not accessible: (1) ask the publisher for feed/API access or a
licensed data delivery – the code only needs `IRIS_FEED_URL` changed; (2) Absolut Research category feeds,
if offered, can be configured the same way; (3) any other RSS/Atom source with the same domain focus works
unchanged, since nothing in the pipeline is publisher-specific except `IRIS_SOURCE_SYSTEM` and the taxonomy.

**Content rights:** only metadata and the excerpt the publisher itself puts in the feed are stored
(`summary`, truncated to 2 000 characters). Article pages are never fetched.

## 3. Configuration

All settings come from environment variables (or `.env`). No credentials are in the source.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | – | target database (required unless `--dry-run`) |
| `IRIS_FEED_URL` | `https://www.absolut-report.de/feed/` | live endpoint |
| `IRIS_SOURCE_SYSTEM` | `absolut_report` | part of the idempotency key |
| `IRIS_CONTACT` | – | contact put in the User-Agent so the publisher can reach the operator |
| `IRIS_MAX_PAGES` | `5` | pagination limit per run |
| `IRIS_PAGE_DELAY_S` | `1.0` | politeness delay between pages |
| `IRIS_TIMEOUT_S` / `IRIS_MAX_RETRIES` / `IRIS_BACKOFF_FACTOR` | `20` / `3` / `1.0` | HTTP behaviour (retries on 429/5xx/connection errors, honours `Retry-After`) |
| `IRIS_TAXONOMY_PATH`, `IRIS_GAZETTEER_PATH`, `IRIS_INSTITUTIONS_PATH` | packaged files | swap the domain filter or gazetteer without code changes |

robots.txt is always honoured in live mode and is intentionally not configurable.

## 4. Data model (`src/iris_harvester/sql/001_schema.sql`)

The brief names the destination but gives no DDL, so the schema is a proposal (created idempotently by
`init-db` or on the first run):

* **`iris_staging.signal_ingest`** – one row per source record.
  * Key: `UNIQUE (source_system, source_record_id)`; `source_record_id` is the RSS `<guid>` / Atom `<id>`,
    falling back to the canonical URL (flagged `guid_missing_used_url`).
  * Traceability: `source_url`, `source_url_canonical` (tracking params stripped), `feed_url`,
    `raw_payload` (parsed entry as JSON), `first_run_id`, `last_run_id`, `first_seen_at`, `last_seen_at`,
    `last_changed_at`, `change_count`, `content_hash` (SHA-256 of normalised source content).
  * Content: `title`, `summary`, `published_at` (UTC), `authors`, `source_categories`, `language`.
  * Domain: `signal_type`, `topics[]`, `matched_terms` (why it matched), `relevance_score`, `geo_scope`.
  * Geography: `geom geometry(Point,25832)`, `geo_status`, `geo_method`, `geo_confidence`,
    `location_text`, `location_candidates`.
  * Quality: `quality_flags[]`.
  * Constraints: point ⇔ `geo_status='resolved'`; point must be valid, SRID 25832 and inside Germany's
    extent; non-blank title.
* **`iris_staging.ingest_run`** – status, stats and errors (incl. every rejected entry) per run.
* **`iris_staging.harvest_state`** – ETag / Last-Modified / publication-date watermark per feed URL.

**Idempotency & updates.** Entries repeated within a run are collapsed first. The upsert then
*inserts* new keys, *updates* a row only when the source content or the derived enrichment changed (so a
gazetteer fix re-geocodes on the next run), and otherwise only touches `last_seen_at`/`last_run_id`.
The batch is one transaction, and an advisory lock prevents two runs writing at once.

**Incremental retrieval.** Page 1 is requested with `If-None-Match` / `If-Modified-Since`; a `304` ends
the run as `not_modified`. Pagination (`?paged=2…`) stops on 404, an empty page, a page entirely older than
the stored watermark, `IRIS_MAX_PAGES`, or when the server ignores the parameter and returns the same page
again (loop protection).

## 5. Domain filter (`src/iris_harvester/data/taxonomy.yaml`)

Four topics of German (and some English) term stems, matched in title, excerpt and feed categories:

| Topic | Examples |
|---|---|
| `institutional_investors` | Versicherer, Pensionskasse, Versorgungswerk, ZVK, Sparkasse, Stiftung, Spezialfonds, bAV |
| `regulation_quotas` | Solvency II, SCR, Standardformel, BaFin, Anlageverordnung, Immobilien-/Risikokapitalquote, VAG, EIOPA |
| `real_assets` | Immobilien, Infrastruktur, Private Equity/Debt, Sachwerte, erneuerbare Energien, Rechenzentren |
| `allocation_trend` | Asset Allocation, Allokation, umschichten, aufstocken, Mandat, Kapitalanlage |

**Keep rule:** at least one of the three objective topics, geographic scope in `DE`, `DACH` or `UNKNOWN`,
and a weighted score ≥ 6 (each distinct term counts once, capped per topic; title hits × 1.5). Swiss- or
Austrian-only items (e.g. FINMA, "Schweizer Pensionskassen") are filtered out. Items with no
geographic signal are kept but flagged `geo_scope_unknown`, because the publication is DACH-focused and
dropping them would lose most sector-wide regulatory news. Every filtered item is listed with its reason in
the run report.

## 6. Geographic resolution (`geocode.py`)

News items carry no coordinates, so locations come from text, deterministically and offline:

1. **Institutions → head office** (`institutions_de.csv`, e.g. Bayerische Versorgungskammer → München).
   The match is masked so its words are not also read as a place ("Bayerische" ≠ Bayern here).
2. **Cities** incl. adjective/genitive forms (`gazetteer_de.csv`: Kölner → Köln, Hamburgs → Hamburg),
   longest match first (Frankfurt (Oder) before Frankfurt).
3. **Federal states** → `region_only`, never a point.

| Outcome | Rule | `geom` |
|---|---|---|
| `resolved` | exactly one distinct city | point, EPSG:25832 (pyproj, `always_xy`) |
| `ambiguous` | two or more cities | NULL; all candidates in `location_candidates` |
| `region_only` | only a state | NULL |
| `not_found` | nothing recognised | NULL |
| `invalid` | coordinates fail range checks | NULL + flag `invalid_coordinates` |

Documented defaults: bare "Frankfurt"/"Frankfurter" → Frankfurt am Main, "Freiburg" → Freiburg im
Breisgau (flag `location_assumption`, lower confidence). Cities whose names are common German words
(Essen, Halle) are deliberately excluded to avoid false positives.

## 7. Failure handling

| Situation | Behaviour | Where visible |
|---|---|---|
| robots.txt disallows / 401 / 403 / 451 | stop, no retry, no bypass | run errors `access_restricted`, exit 2 |
| robots.txt unreachable (5xx/network) | treated as disallow (RFC 9309) | same |
| 429 / 5xx / connection errors | retried with exponential backoff, `Retry-After` honoured | `network` error if retries exhausted |
| feed URL 404 | run fails | `feed_not_found`, exit 1 |
| page > 1 fails | earlier pages are still loaded | status `partial`, exit 4 |
| HTML or non-feed body | run fails | `malformed_feed`, exit 3 |
| broken XML with recoverable entries | entries kept | flag `feed_xml_warning` |
| valid feed, no entries | success with 0 rows | stat `empty_feed` |
| entry without title or without any URL | rejected | run errors `rejected_entry` with reason, status `partial` |
| missing/unparseable/future date | kept | flags `missing_pubdate`, `unparseable_pubdate`, `future_pubdate` |
| duplicate guid within a run | collapsed | stat `duplicates_in_batch` |
| same URL under two guids | both kept | flag `duplicate_url_different_guid` |
| concurrent run | refused | `concurrent_run` |

## 8. Tests

```bash
pytest -q                                            # 60 tests, DB tests skipped
TEST_DATABASE_URL=postgresql://iris:…@localhost:5432/iris pytest -q   # all 70 tests
```

* `test_parse_normalize.py` – RSS & Atom, UTC conversion, empty/HTML/truncated payloads, canonical URLs,
  HTML cleanup, language detection, rejection rules, date flags, content hash.
* `test_classify_geocode.py` – keep/drop decisions (off-topic, Swiss-only, weak mention, DACH), every
  geo outcome, institution masking, longest match, acronym case-sensitivity, invalid coordinates,
  projection accuracy.
* `test_fetch_pipeline.py` – HTTP mocked with `responses`: robots.txt, refusals, retries, conditional
  requests, pagination to 404, loop protection, max pages, partial failure, obsolete endpoint.
* `test_db_integration.py` – against real PostGIS: **re-run inserts nothing**, changed record updated in
  place, SRID/validity and round-trip accuracy, database rejects bad coordinates / wrong SRID / point
  without resolved status, run log, advisory lock, live incremental run with ETag → 304.

Fixtures are synthetic (fictional institutions and numbers) so no publisher content is redistributed.

## 9. Sample output and validation

`samples/` holds the output of the fixture runs: `signals.jsonl`, `signals.csv`,
`signals_resolved.geojson` (WGS84 per RFC 7946, with the EPSG:25832 coordinates as properties),
`run_report.json`, and `validation_output.txt` from `sql/validation.sql` (13 checks; every "expect 0"
check passes). A concise error/quality summary is in **[QUALITY_REPORT.md](QUALITY_REPORT.md)**.

## 10. Assumptions

* The feed is WordPress-generated (`/feed/`, `?paged=N`, `<guid>` stable per post). If not, pagination
  simply stops after page 1.
* `<guid>` is the stable identifier; if a publisher re-issues guids, rows would duplicate, which validation
  check 2 surfaces through identical canonical URLs.
* "German" is judged from the text (places, institutions, German regulatory terms, "Deutschland").
* A city mentioned in a headline is the location of the signal; HQ of a named institution is a
  reasonable proxy when no city is mentioned.

## 11. Known limitations and next steps

* Keyword taxonomy, not a trained classifier: German compounds are handled with stems, but irony,
  negation ("keine Erhöhung der Quote") and implicit references are not understood. A labelled sample of
  real items would allow precision/recall measurement and threshold tuning.
* The packaged gazetteer is a curated list of 45 cities and the 13 territorial states (Berlin, Hamburg, Bremen are cities). For production, load the
  GeoNames Germany extract (CC BY 4.0) or BKG VG250 municipalities into the same CSV format.
* Only the feed excerpt is analysed; locations or allocation figures that appear only in the full article
  are missed (by design, for content-rights reasons).
* Extracting numeric quotas (e.g. "Immobilienquote 18 %") into structured columns is a natural next step.
* Scheduling (cron / Airflow) and alerting on exit codes are left to the platform.
* Live endpoint not verified from the build environment (see §2).

---
Submitted for evaluation of assignment IRIS-SIG-49 only; no licence for production use is granted by
this submission.
