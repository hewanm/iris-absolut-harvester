.PHONY: install db test test-db demo validate check-source
install:   ## editable install with test deps
	pip install -e ".[dev]"
db:        ## start PostGIS 16/3.4
	docker compose up -d db
test:      ## unit + pipeline tests (DB tests skip without TEST_DATABASE_URL)
	pytest -q
test-db:   ## full suite incl. PostGIS integration tests
	set -a; . ./.env; set +a; pytest -q
demo:      ## fixture run into the database, run twice to show idempotency
	set -a; . ./.env; set +a; iris-harvester init-db && \
	iris-harvester run --fixture tests/fixtures/feed_page1.xml --fixture tests/fixtures/feed_page2.xml --export-dir out; \
	iris-harvester run --fixture tests/fixtures/feed_page1.xml --fixture tests/fixtures/feed_page2.xml
validate:  ## validation queries
	set -a; . ./.env; set +a; psql "$$DATABASE_URL" -f sql/validation.sql
check-source: ## verify the live endpoint (read-only, honours robots.txt)
	iris-harvester check-source
