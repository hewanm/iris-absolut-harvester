"""Harvest orchestration: fetch -> parse -> normalise -> geocode -> classify -> dedupe -> load."""
from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .classify import Taxonomy
from .config import Settings
from .fetch import AccessRestrictedError, FeedFetcher, FetchError, page_url
from .geocode import Gazetteer
from .load import Repository
from .normalize import RejectedEntry, normalize_entry
from .parse import MalformedFeedError, ParsedFeed, parse_feed

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    run_id: str
    mode: str
    source_system: str
    feed_url: str
    started_at: str
    finished_at: str | None = None
    status: str = "running"
    stats: Counter = field(default_factory=Counter)
    pages: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    filtered_out: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.status in ("succeeded", "not_modified"):
            return 0
        if self.status == "partial":
            return 4
        if any(e.get("type") == "access_restricted" for e in self.errors):
            return 2
        if any(e.get("type") == "malformed_feed" for e in self.errors):
            return 3
        return 1

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "source_system": self.source_system,
            "feed_url": self.feed_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stats": dict(sorted(self.stats.items())),
            "pages": self.pages,
            "errors": self.errors,
            "rejected": self.rejected,
            "filtered_out": self.filtered_out,
        }


class Harvester:
    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: FeedFetcher | None = None,
        repository: Repository | None = None,
        taxonomy: Taxonomy | None = None,
        gazetteer: Gazetteer | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.settings = settings
        self.fetcher = fetcher
        self.repo = repository
        self.taxonomy = taxonomy or Taxonomy.load(settings.taxonomy_path)
        self.gazetteer = gazetteer or Gazetteer.load(settings.gazetteer_path, settings.institutions_path)
        self.sleep = sleep
        self.clock = clock

    # ------------------------------------------------------------------
    def run(self, fixtures: list[Path] | None = None, dry_run: bool = False) -> RunReport:
        mode = "fixture" if fixtures else "live"
        repo = None if dry_run else self.repo
        report = RunReport(
            run_id=str(uuid.uuid4()),
            mode=mode,
            source_system=self.settings.source_system,
            feed_url=self.settings.feed_url if mode == "live" else "fixture://" + ",".join(p.name for p in fixtures),
            started_at=self.clock().isoformat(),
        )
        run_id = uuid.UUID(report.run_id)

        if repo is not None:
            repo.ensure_schema()
            if not repo.try_lock():
                report.status = "failed"
                report.errors.append({"type": "concurrent_run", "message": "another harvester run holds the lock"})
                return self._finish(report, repo=None)
            repo.start_run(run_id, self.settings.source_system, mode)

        try:
            candidates = self._collect(report, fixtures, repo)
            if report.status not in ("failed", "not_modified"):
                records = self._dedupe(report, candidates)
                report.records = records
                if repo is not None:
                    counts = repo.upsert_signals(records, run_id)
                    report.stats["inserted"] = counts.inserted
                    report.stats["updated"] = counts.updated
                    report.stats["unchanged"] = counts.unchanged
                    if mode == "live":
                        watermark = max((r["published_at"] for r in records if r["published_at"]), default=None)
                        page1 = report.pages[0] if report.pages else {}
                        repo.save_state(self.settings.feed_url, page1.get("etag"), page1.get("last_modified"), watermark)
                report.status = "partial" if (report.errors or report.rejected) else "succeeded"
        except Exception as exc:  # unexpected: never swallow silently
            log.exception("harvest failed")
            report.status = "failed"
            report.errors.append({"type": "unexpected", "message": f"{type(exc).__name__}: {exc}"})

        return self._finish(report, repo)

    # ------------------------------------------------------------------
    def _finish(self, report: RunReport, repo: Repository | None) -> RunReport:
        report.finished_at = self.clock().isoformat()
        report.stats["staged_records"] = len(report.records)
        if repo is not None:
            errors = report.errors + [{"type": "rejected_entry", **r} for r in report.rejected]
            stats = dict(report.stats) | {"pages": report.pages}
            repo.finish_run(uuid.UUID(report.run_id), report.status, stats, errors)
            repo.unlock()
        return report

    def _pages(self, report: RunReport, fixtures: list[Path] | None, repo: Repository | None):
        """Yield (label, ParsedFeed) per page; record page-level problems on the report."""
        if fixtures:
            for i, path in enumerate(fixtures, start=1):
                label = f"fixture://{path.name}"
                try:
                    parsed = parse_feed(path.read_bytes())
                except MalformedFeedError as exc:
                    report.errors.append({"type": "malformed_feed", "page": i, "url": label, "message": str(exc)})
                    report.pages.append({"page": i, "url": label, "status": "malformed"})
                    if i == 1:
                        report.status = "failed"
                    return
                report.pages.append({"page": i, "url": label, "status": "ok", "entries": len(parsed.entries),
                                     "format": parsed.version, "bozo": parsed.bozo})
                yield label, parsed
            return

        if self.fetcher is None:
            raise RuntimeError("live mode requires a FeedFetcher")
        state = repo.get_state(self.settings.feed_url) if repo is not None else {}
        watermark = state.get("watermark_pub_at")
        previous_ids: set[str] | None = None

        for page in range(1, self.settings.max_pages + 1):
            url = page_url(self.settings.feed_url, page)
            if page > 1:
                self.sleep(self.settings.page_delay_s)
            try:
                fetched = self.fetcher.fetch(
                    url,
                    etag=state.get("etag") if page == 1 else None,
                    last_modified=state.get("last_modified") if page == 1 else None,
                )
            except AccessRestrictedError as exc:
                report.errors.append({"type": "access_restricted", "page": page, "url": url, "message": str(exc)})
                report.status = "failed" if page == 1 else report.status
                return
            except FetchError as exc:
                report.errors.append({"type": "network", "page": page, "url": url, "message": str(exc)})
                report.status = "failed" if page == 1 else report.status
                return

            if fetched.not_modified:
                report.pages.append({"page": page, "url": url, "status": 304})
                report.status = "not_modified"
                return
            if fetched.gone:
                report.pages.append({"page": page, "url": url, "status": fetched.status})
                if page == 1:
                    report.errors.append({"type": "feed_not_found", "url": url,
                                          "message": f"HTTP {fetched.status}: feed endpoint obsolete or moved"})
                    report.status = "failed"
                return

            try:
                parsed = parse_feed(fetched.content)
            except MalformedFeedError as exc:
                report.errors.append({"type": "malformed_feed", "page": page, "url": url, "message": str(exc),
                                      "content_type": fetched.content_type})
                report.pages.append({"page": page, "url": url, "status": "malformed"})
                if page == 1:
                    report.status = "failed"
                return

            ids = {e.guid or e.link or "" for e in parsed.entries}
            report.pages.append({"page": page, "url": url, "status": fetched.status, "entries": len(parsed.entries),
                                 "format": parsed.version, "bozo": parsed.bozo,
                                 "etag": fetched.etag, "last_modified": fetched.last_modified})
            if previous_ids is not None and ids and ids == previous_ids:
                report.stats["pagination_loop_detected"] += 1
                return
            yield url, parsed

            if not parsed.entries:
                return
            dated = [e.published_at for e in parsed.entries if e.published_at]
            if watermark and dated and max(dated) <= watermark:
                report.stats["stopped_at_watermark"] += 1
                return
            previous_ids = ids

    def _collect(self, report: RunReport, fixtures, repo) -> list[dict]:
        out: list[dict] = []
        any_entries = False
        for label, parsed in self._pages(report, fixtures, repo):
            if parsed.bozo:
                report.stats["pages_with_xml_warnings"] += 1
            for entry in parsed.entries:
                any_entries = True
                report.stats["entries_seen"] += 1
                record = self._process(report, entry, parsed, label)
                if record is not None:
                    out.append(record)
        if not any_entries and report.status == "running":
            report.stats["empty_feed"] += 1
            log.warning("feed returned no entries")
        return out

    def _process(self, report: RunReport, entry, parsed: ParsedFeed, label: str) -> dict | None:
        try:
            cand = normalize_entry(
                entry, feed_url=label, feed_language=parsed.language,
                summary_max_chars=self.settings.summary_max_chars, now=self.clock(),
            )
        except RejectedEntry as exc:
            report.stats["rejected"] += 1
            report.rejected.append({"reason": exc.reason, "page": label, "guid": entry.guid,
                                    "link": entry.link, "title": entry.title})
            return None
        if parsed.bozo:
            cand.quality_flags.append("feed_xml_warning")

        geo = self.gazetteer.resolve(cand.text)
        cls = self.taxonomy.classify(cand.title, " ".join(filter(None, [cand.summary, " ".join(cand.categories)])),
                                     has_german_place=geo.has_german_place)
        if not cls.keep:
            report.stats["filtered_out"] += 1
            report.stats[f"filtered_out:{cls.reject_reason}"] += 1
            report.filtered_out.append({"reason": cls.reject_reason, "title": cand.title, "score": cls.score,
                                        "geo_scope": cls.geo_scope, "source_record_id": cand.source_record_id})
            return None

        flags = sorted(set(cand.quality_flags + cls.flags + geo.flags))

        return {
            "source_system": self.settings.source_system,
            "source_record_id": cand.source_record_id,
            "source_url": cand.source_url,
            "source_url_canonical": cand.source_url_canonical,
            "feed_url": label,
            "signal_type": self.settings.signal_type,
            "title": cand.title,
            "summary": cand.summary,
            "published_at": cand.published_at,
            "authors": cand.authors,
            "source_categories": cand.categories,
            "language": cand.language,
            "topics": cls.topics,
            "matched_terms": cls.matched_terms,
            "relevance_score": cls.score,
            "geo_scope": cls.geo_scope,
            "location_text": geo.location_text,
            "location_candidates": geo.candidates,
            "geo_method": geo.method,
            "geo_status": geo.status,
            "geo_confidence": geo.confidence,
            "x": geo.x,
            "y": geo.y,
            "lon": geo.lon,
            "lat": geo.lat,
            "quality_flags": flags,
            "content_hash": cand.content_hash(),
            "raw_payload": {"entry": cand.raw, "page": label, "feed_title": parsed.title,
                            "feed_format": parsed.version},
        }

    @staticmethod
    def _dedupe(report: RunReport, records: list[dict]) -> list[dict]:
        """Collapse repeated identifiers within one run (feeds overlap across pages)."""
        by_id: dict[str, dict] = {}
        for rec in records:
            key = rec["source_record_id"]
            prev = by_id.get(key)
            if prev is None:
                by_id[key] = rec
                continue
            report.stats["duplicates_in_batch"] += 1
            newer = (rec["published_at"] or datetime.min.replace(tzinfo=timezone.utc)) > (
                prev["published_at"] or datetime.min.replace(tzinfo=timezone.utc))
            if newer:
                by_id[key] = rec
        url_seen: dict[str, str] = {}
        for rec in by_id.values():
            other = url_seen.setdefault(rec["source_url_canonical"], rec["source_record_id"])
            if other != rec["source_record_id"]:
                rec["quality_flags"] = sorted(set(rec["quality_flags"]) | {"duplicate_url_different_guid"})
        # quality statistics are computed on the de-duplicated set that is actually staged
        for rec in by_id.values():
            report.stats[f"geo:{rec['geo_status']}"] += 1
            for f in rec["quality_flags"]:
                report.stats[f"flag:{f}"] += 1
        return list(by_id.values())
