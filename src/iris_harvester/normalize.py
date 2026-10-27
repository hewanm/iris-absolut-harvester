"""Validation and normalisation of raw feed entries into staging candidates."""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .parse import RawEntry

_TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref)$", re.I)
_WS = re.compile(r"\s+")
_GERMAN_HINTS = {"der", "die", "das", "und", "mit", "für", "auf", "ist", "den", "von", "zu", "im", "nicht", "sich"}
_ENGLISH_HINTS = {"the", "and", "with", "for", "is", "of", "to", "in", "on", "from", "are", "its"}


class RejectedEntry(ValueError):
    """Entry cannot be staged (no stable identifier or no title)."""

    def __init__(self, reason: str, entry: RawEntry):
        super().__init__(reason)
        self.reason = reason
        self.entry = entry


@dataclass
class Candidate:
    source_record_id: str
    source_url: str
    source_url_canonical: str
    title: str
    summary: str | None
    published_at: datetime | None
    authors: list[str]
    categories: list[str]
    language: str | None
    raw: dict
    feed_url: str | None
    quality_flags: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(filter(None, [self.title, self.summary, " ".join(self.categories)]))

    def content_hash(self) -> str:
        payload = {
            "title": self.title,
            "summary": self.summary,
            "url": self.source_url_canonical,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "categories": sorted(self.categories),
            "authors": sorted(self.authors),
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append(" ")


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # pragma: no cover - HTMLParser is very tolerant
        text = re.sub(r"<[^>]+>", " ", value)
    text = _WS.sub(" ", html.unescape(text)).strip()
    # WordPress excerpts end with "[…]" / "The post X appeared first on Y."
    text = re.sub(r"\s*(\[…\]|\[\.\.\.\]|\[&hellip;\])\s*$", " …", text)
    text = re.sub(r"\s*The post .+ appeared first on .+\.$", "", text)
    return text.strip() or None


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if parts.port and not ((scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)):
        host = f"{host}:{parts.port}"
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k))
    )
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, query, ""))


def detect_language(text: str, feed_language: str | None) -> str | None:
    """Stop-word heuristic per item; falls back to the channel language when inconclusive."""
    words = re.findall(r"[a-zäöüß]+", text.lower())
    de = sum(w in _GERMAN_HINTS for w in words)
    en = sum(w in _ENGLISH_HINTS for w in words)
    if de != en and max(de, en) >= 2:
        return "de" if de > en else "en"
    return feed_language.split("-")[0].lower() if feed_language else None


def normalize_entry(
    entry: RawEntry,
    *,
    feed_url: str | None,
    feed_language: str | None,
    summary_max_chars: int = 2000,
    now: datetime | None = None,
) -> Candidate:
    now = now or datetime.now(timezone.utc)
    flags: list[str] = []

    title = html_to_text(entry.title)
    if not title:
        raise RejectedEntry("missing_title", entry)

    if not entry.link and not entry.guid:
        raise RejectedEntry("missing_identifier", entry)

    def _is_http(value: str | None) -> bool:
        return bool(value) and urlsplit(value).scheme in ("http", "https")

    if entry.link and not _is_http(entry.link):
        flags.append("non_http_link")
    url = next((u for u in (entry.link, entry.guid) if _is_http(u)), None)
    if url is None:
        raise RejectedEntry("missing_source_url", entry)
    if url != entry.link:
        flags.append("url_from_guid")

    record_id = entry.guid
    if not record_id:
        record_id = canonical_url(url)
        flags.append("guid_missing_used_url")

    summary = html_to_text(entry.summary_html)
    if summary and len(summary) > summary_max_chars:
        summary = summary[: summary_max_chars - 1].rstrip() + "…"
        flags.append("summary_truncated")
    if not summary:
        flags.append("missing_summary")

    published = entry.published_at or entry.updated_at
    if entry.published_at is None and entry.updated_at is not None:
        flags.append("pubdate_from_updated")
    if published is None:
        flags.append("unparseable_pubdate" if entry.raw.get("published") else "missing_pubdate")
    elif published > now + timedelta(days=1):
        flags.append("future_pubdate")

    language = detect_language(" ".join(filter(None, [title, summary])), feed_language)

    return Candidate(
        source_record_id=record_id,
        source_url=url,
        source_url_canonical=canonical_url(url),
        title=title,
        summary=summary,
        published_at=published,
        authors=entry.authors,
        categories=entry.categories,
        language=language,
        raw=entry.raw,
        feed_url=feed_url,
        quality_flags=flags,
    )
