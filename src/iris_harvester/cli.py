"""Command line entry point: ``iris-harvester {init-db,run,check-source}``."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .fetch import AccessRestrictedError, FeedFetcher, FetchError
from .load import Repository
from .parse import MalformedFeedError, parse_feed
from .pipeline import Harvester, RunReport

EXPORT_COLUMNS = [
    "source_system", "source_record_id", "title", "published_at", "source_url", "topics", "relevance_score",
    "geo_scope", "geo_status", "geo_method", "location_text", "geo_confidence", "x", "y", "quality_flags",
]


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def export(report: RunReport, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    p = out_dir / "signals.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        for r in report.records:
            fh.write(json.dumps(r, ensure_ascii=False, default=_json_default) + "\n")
    paths.append(p)

    p = out_dir / "signals.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        for r in report.records:
            row = []
            for col in EXPORT_COLUMNS:
                v = r.get(col)
                row.append("|".join(v) if isinstance(v, list) else (_json_default(v) if v is not None else ""))
            w.writerow(row)
    paths.append(p)

    # GeoJSON (RFC 7946) must be WGS84; the EPSG:25832 coordinates are kept as properties.
    features = []
    for r in report.records:
        if r.get("geo_status") != "resolved":
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "source_record_id": r["source_record_id"], "title": r["title"],
                "published_at": _json_default(r["published_at"]) if r["published_at"] else None,
                "location": r["location_text"], "geo_method": r["geo_method"],
                "epsg25832_x": r["x"], "epsg25832_y": r["y"], "topics": r["topics"],
            },
        })
    p = out_dir / "signals_resolved.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    paths.append(p)

    p = out_dir / "run_report.json"
    p.write_text(json.dumps(report.summary(), ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    paths.append(p)
    return paths


def cmd_init_db(settings: Settings, _args) -> int:
    if not settings.database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    with Repository(settings.database_url) as repo:
        repo.ensure_schema()
    print("schema iris_staging ready")
    return 0


def cmd_run(settings: Settings, args) -> int:
    if args.max_pages:
        settings = Settings(**{**settings.__dict__, "max_pages": args.max_pages})
    fixtures = [Path(p) for p in args.fixture] if args.fixture else None
    if not args.dry_run and not settings.database_url:
        print("DATABASE_URL is not set (use --dry-run to run without a database)", file=sys.stderr)
        return 1

    repo = None if args.dry_run else Repository(settings.database_url)
    try:
        harvester = Harvester(settings, fetcher=None if fixtures else FeedFetcher(settings), repository=repo)
        report = harvester.run(fixtures=fixtures, dry_run=args.dry_run)
    finally:
        if repo is not None:
            repo.close()

    if args.export_dir:
        for p in export(report, Path(args.export_dir)):
            logging.info("wrote %s", p)
    print(json.dumps({k: v for k, v in report.summary().items() if k not in ("filtered_out",)},
                     ensure_ascii=False, indent=2, default=_json_default))
    return report.exit_code


def cmd_check_source(settings: Settings, _args) -> int:
    """Verify the live endpoint (robots.txt, HTTP status, format, item dates). Writes nothing."""
    fetcher = FeedFetcher(settings)
    result = {"feed_url": settings.feed_url, "user_agent": settings.user_agent}
    try:
        result["robots_allows"] = fetcher.robots_allows(settings.feed_url)
        page = fetcher.fetch(settings.feed_url)
        result.update(http_status=page.status, content_type=page.content_type,
                      etag=page.etag, last_modified=page.last_modified)
        if page.content:
            parsed = parse_feed(page.content)
            dates = sorted(e.published_at for e in parsed.entries if e.published_at)
            result.update(format=parsed.version, feed_title=parsed.title, language=parsed.language,
                          entries=len(parsed.entries), xml_warnings=parsed.bozo,
                          newest=dates[-1].isoformat() if dates else None,
                          oldest=dates[0].isoformat() if dates else None)
        code = 0
    except AccessRestrictedError as exc:
        result.update(error="access_restricted", message=str(exc))
        code = 2
    except (FetchError, MalformedFeedError) as exc:
        result.update(error=type(exc).__name__, message=str(exc))
        code = 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iris-harvester", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create/upgrade the iris_staging schema (idempotent)")

    run = sub.add_parser("run", help="harvest the feed (or fixtures) into iris_staging.signal_ingest")
    run.add_argument("--fixture", action="append", metavar="FILE",
                     help="read feed page(s) from local files instead of the network (repeatable, in page order)")
    run.add_argument("--dry-run", action="store_true", help="do everything except writing to the database")
    run.add_argument("--export-dir", metavar="DIR", help="write signals.jsonl/.csv/.geojson + run_report.json")
    run.add_argument("--max-pages", type=int, help="override IRIS_MAX_PAGES")

    sub.add_parser("check-source", help="verify the live feed endpoint without writing anything")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    settings = Settings.from_env()
    handler = {"init-db": cmd_init_db, "run": cmd_run, "check-source": cmd_check_source}[args.command]
    return handler(settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
