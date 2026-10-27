"""Feed parsing (RSS 2.0 / Atom 1.0) into plain, JSON-safe entries."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import feedparser


class MalformedFeedError(ValueError):
    """The payload is not a usable RSS/Atom document."""


@dataclass
class RawEntry:
    guid: str | None
    link: str | None
    title: str | None
    summary_html: str | None
    published_at: datetime | None
    updated_at: datetime | None
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedFeed:
    version: str
    title: str | None
    language: str | None
    entries: list[RawEntry]
    bozo: bool = False
    bozo_reason: str | None = None


def _to_utc(struct) -> datetime | None:
    if not struct:
        return None
    try:
        # feedparser normalises *_parsed fields to UTC struct_time
        return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
    except (OverflowError, ValueError, TypeError):
        return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_feed(content: bytes | str) -> ParsedFeed:
    if content is None or (isinstance(content, (bytes, str)) and not content.strip()):
        raise MalformedFeedError("empty response body")

    parsed = feedparser.parse(content)
    version = parsed.get("version") or ""
    bozo = bool(parsed.get("bozo"))
    reason = repr(parsed.get("bozo_exception")) if bozo else None

    if not version and not parsed.entries:
        raise MalformedFeedError(f"not an RSS/Atom document ({reason or 'unknown format'})")

    entries: list[RawEntry] = []
    for e in parsed.entries:
        summary = e.get("summary")
        if not summary and e.get("content"):
            summary = e["content"][0].get("value")
        authors = [a.get("name") for a in e.get("authors", []) if a.get("name")]
        if not authors and e.get("author"):
            authors = [e["author"]]
        categories = [t.get("term") for t in e.get("tags", []) if t.get("term")]
        raw = {
            "id": e.get("id"),
            "link": e.get("link"),
            "title": e.get("title"),
            "summary": summary,
            "published": e.get("published"),
            "updated": e.get("updated"),
            "authors": authors,
            "categories": categories,
        }
        entries.append(
            RawEntry(
                guid=_clean(e.get("id")),
                link=_clean(e.get("link")),
                title=_clean(e.get("title")),
                summary_html=summary,
                published_at=_to_utc(e.get("published_parsed")),
                updated_at=_to_utc(e.get("updated_parsed")),
                authors=[str(a).strip() for a in authors if str(a).strip()],
                categories=[str(c).strip() for c in categories if str(c).strip()],
                raw=raw,
            )
        )

    feed = parsed.get("feed", {})
    return ParsedFeed(
        version=version or "unknown",
        title=_clean(feed.get("title")),
        language=_clean(feed.get("language")),
        entries=entries,
        bozo=bozo,
        bozo_reason=reason,
    )
