# IRIS-SIG-49 · Absolut|report Institutional Asset Allocation Harvester

Harvests an RSS/Atom feed, keeps only signals about **German institutional capital allocation,
Solvency II / BaFin quotas and real assets**, resolves locations to **EPSG:25832** points where the text
allows it, and loads the result idempotently into `iris_staging.signal_ingest`
(PostgreSQL 16 / PostGIS 3.4). Everything runs **without network or production access** using the
synthetic fixtures in `tests/fixtures/`.

**Source status (verified 28 Oct 2026):** the feed in the brief, `https://www.absolut-report.de/feed/`,
returns **404**. The domain now redirects to `absolut-research.de`, which publishes no RSS/Atom feed.
This is documented, not bypassed. Evidence and alternatives are in [docs/DESIGN.md §2](docs/DESIGN.md#2-data-source-and-access-status).

## Setup

Requirements: Python ≥ 3.11 and Docker (or a local PostgreSQL 16 + PostGIS 3.4).

```bash
python -m venv .venv && source .venv/bin/activate
pip install ".[dev]"
cp .env.example .env
docker compose up -d db
set -a; source .env; set +a
```

`.env.example` contains local demo values only. `.env` is git-ignored.

## Run

```bash
iris-harvester init-db
iris-harvester run --fixture tests/fixtures/feed_page1.xml --fixture tests/fixtures/feed_page2.xml
iris-harvester run --fixture tests/fixtures/feed_page1.xml --fixture tests/fixtures/feed_page2.xml
psql "$DATABASE_URL" -f sql/validation.sql
```

* The first run inserts 10 rows. The second run inserts **0** and reports 10 unchanged (idempotent).
* Status is `partial` (exit code 4) on purpose: one fixture entry has no title or link and is rejected
  and logged rather than dropped silently.
* Without a database: `iris-harvester run --dry-run --fixture … --export-dir out` writes JSONL, CSV,
  GeoJSON and a run report.
* Live source: `iris-harvester check-source` (read-only) and `iris-harvester run`. Both honour
  robots.txt and report access restrictions instead of working around them.

## Tests

```bash
python -m pytest -q
```

With `TEST_DATABASE_URL` set (it is in `.env`), all **70 tests** run. Without it, the 10 database tests are
skipped. The database tests use a separate, auto-created `iris_test` database, so they never touch
the data from the run above. They cover parsing (RSS/Atom, malformed and empty feeds), filtering and
geocoding decisions, HTTP behaviour (robots.txt, retries, 304, pagination), and against real PostGIS:
re-runs insert nothing, changed records update in place, and invalid coordinates are rejected.

## Assumptions

* The feed `<guid>` is the stable record identifier (fallback: canonical URL, flagged).
* A signal is "German" if its text names a German place, institution or regulatory term. Swiss- or
  Austrian-only items are filtered out, and items with no geographic signal are kept and flagged.
* Locations come from the text: exactly one German city gives a point. Several cities (`ambiguous`) or a
  federal state only (`region_only`) give no point, so there is no false precision.
* Only feed metadata and the publisher's own excerpt are stored. Article pages are never fetched.
* The schema is a proposal, because the brief names the table but gives no DDL.

Known limitations: keyword-based classification, a curated 45-city gazetteer, and no live source
(see above). Details are in [docs/DESIGN.md](docs/DESIGN.md).

## More

* [docs/DESIGN.md](docs/DESIGN.md): source verification, configuration, data model and idempotency,
  filter and geocoding rules, failure handling, test coverage
* [QUALITY_REPORT.md](QUALITY_REPORT.md): run statistics and data-quality summary
* `samples/`: example output, `sql/validation.sql`: validation queries

> macOS: if an editable install (`pip install -e`) fails with `ModuleNotFoundError: iris_harvester`, use
> the regular install shown above. Python ≥ 3.13 ignores hidden `.pth` files in iCloud-synced folders.

---
Submitted for evaluation of assignment IRIS-SIG-49 only; no licence for production use is granted.
