# Error & quality report – IRIS-SIG-49

Generated from the fixture runs whose output is in [`samples/`](samples/):
PostgreSQL 16.15 + PostGIS 3.4.2, three consecutive runs over the same feed pages (initial, identical re-run,
corrected item), plus one malformed-feed run and one empty-feed run.
All fixture content is **synthetic** (fictional institutions and figures).

## Run outcomes

| Run | Input | Status | Exit | Inserted | Updated | Unchanged |
|---|---|---|---|---|---|---|
| 1 | page 1 + page 2 | partial¹ | 4 | 10 | 0 | 0 |
| 2 | same pages again | partial¹ | 4 | 0 | 0 | 10 |
| 3 | page 1 with corrected item + page 2 | partial¹ | 4 | 0 | 1 | 9 |
| 4 | HTML maintenance page instead of a feed | failed | 3 | – | – | – |
| 5 | valid channel with no items | succeeded | 0 | 0 | 0 | 0 |

¹ *partial* because the fixture deliberately contains one entry without title and link; it is rejected and
logged in `ingest_run.errors` instead of being dropped silently.

**Idempotency:** after all runs the table holds 10 rows for 10 distinct `(source_system, source_record_id)`
keys (validation check 1: 0 duplicates). The corrected item was updated in place (`change_count = 1`,
`first_run_id ≠ last_run_id`).

## Per-entry accounting (run 3)

| Stage | Count | Detail |
|---|---|---|
| Entries seen | 15 | 12 on page 1, 3 on page 2 |
| Rejected | 1 | `missing_title` (no title, no link) |
| Duplicates collapsed in batch | 2 | same guid twice on page 1, and page overlap |
| Filtered out | 2 | `out_of_scope_geo:CH` (Swiss pension funds), `no_objective_topic` (market commentary) |
| **Staged** | **10** | |

## Geographic resolution (10 staged rows)

| Status | Rows | Meaning |
|---|---|---|
| resolved | 6 | one German city → EPSG:25832 point (Köln, Dortmund, Berlin, Stuttgart, Frankfurt am Main, Frankfurt (Oder)) |
| not_found | 2 | no place in title/excerpt (e.g. sector-wide Solvency II item) – `geom` NULL |
| ambiguous | 1 | Münster **and** Hamburg named – both kept in `location_candidates`, no point |
| region_only | 1 | only "Bayerische…" → Bayern; a state centroid would be false precision, no point |
| invalid | 0 | (exercised in tests with out-of-Germany gazetteer coordinates) |

Spot check (validation check 5): Köln stored as E 356 689.1 / N 5 644 855.7 (EPSG:25832) and
back-projects to 6.96030 E / 50.93750 N, i.e. the gazetteer input, to 5 decimals.

## Quality flags on staged rows

| Flag | Rows | Action needed |
|---|---|---|
| `missing_pubdate` | 1 | kept with `published_at` NULL; source should be checked |
| `guid_missing_used_url` | 1 | canonical URL used as identifier (stable, but changes if the URL changes) |
| `location_assumption` | 1 | "Frankfurter" read as Frankfurt am Main (documented default) |
| `ambiguous_location` | 1 | manual review list in validation check 9 |
| `region_only` | 1 | manual review list in validation check 9 |

## Validation checks (see `samples/validation_output.txt`)

All "expect 0" checks pass: no duplicate keys, no duplicate URLs under different ids, no geometry with wrong
SRID / invalid / outside Germany, no geometry–status mismatch, every row carries an objective topic and an
allowed geographic scope, no orphaned rows without a run.

## Defence in depth for coordinates

1. Gazetteer coordinates must lie inside the WGS84 envelope of Germany (5.5–15.5 E, 47.2–55.1 N).
2. The projected point must lie inside the EPSG:25832 extent of Germany; otherwise status `invalid`.
3. Database `CHECK` constraints enforce SRID 25832, `ST_IsValid`, the same extent, and
   "point ⇔ resolved". Tests prove the database rejects a Paris point, lon/lat values mistaken for metres,
   a point in SRID 4326, and a point attached to an `ambiguous` row.

## Not verified here

* **Live endpoint:** from the build environment `https://www.absolut-report.de/feed/` returned an HTTP 4xx
  client error; whether that is bot protection or a removed feed could not be determined. See README §2.
* **Docker:** `docker compose config` validates, but no Docker daemon was available to run the stack; the
  database tests ran against a native PostgreSQL 16.15 / PostGIS 3.4.2 instead.
