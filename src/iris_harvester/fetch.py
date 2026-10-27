"""HTTP retrieval: polite, conditional, retrying, robots-aware.

Access restrictions (robots.txt disallow, 401/403/451) are *reported*, never
worked around: no header spoofing, no proxies, no alternative mirrors.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Network failure or unexpected HTTP status after retries."""


class AccessRestrictedError(FetchError):
    """The source refuses automated access (robots.txt, 401, 403, 451)."""


@dataclass
class FetchedPage:
    url: str
    status: int
    content: bytes | None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def gone(self) -> bool:
        """WordPress answers 404 for ?paged=N beyond the last page."""
        return self.status in (404, 410)


def page_url(feed_url: str, page: int) -> str:
    """Return the URL for feed page ``page`` (1-based) using WordPress' ``paged`` parameter."""
    if page <= 1:
        return feed_url
    parts = urlsplit(feed_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "paged"]
    query.append(("paged", str(page)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def build_session(settings: Settings) -> requests.Session:
    retry = Retry(
        total=settings.max_retries,
        connect=settings.max_retries,
        read=settings.max_retries,
        status=settings.max_retries,
        backoff_factor=settings.backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": settings.user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8",
        }
    )
    return session


class FeedFetcher:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or build_session(settings)
        self._robots: dict[str, RobotFileParser | None] = {}

    # -- robots.txt (RFC 9309) ------------------------------------------------
    def robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:  # unreachable robots.txt => RFC 9309: assume complete disallow
            return False
        return parser.can_fetch(self.settings.user_agent, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser(robots_url)
        try:
            resp = self.session.get(robots_url, timeout=self.settings.timeout_s)
        except requests.RequestException as exc:
            log.warning("robots.txt unreachable (%s): treating as disallow", exc)
            return None
        if 400 <= resp.status_code < 500:
            # RFC 9309 §2.3.1.3: "unavailable" robots.txt => crawler may access any resource
            parser.parse([])
        elif resp.status_code >= 500:
            log.warning("robots.txt returned %s: treating as disallow", resp.status_code)
            return None
        else:
            parser.parse(resp.text.splitlines())
        return parser

    # -- feed pages -------------------------------------------------------------
    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchedPage:
        if not self.robots_allows(url):
            raise AccessRestrictedError(f"robots.txt disallows automated access to {url}")

        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            resp = self.session.get(url, headers=headers, timeout=self.settings.timeout_s)
        except requests.RequestException as exc:
            raise FetchError(f"network error fetching {url}: {exc}") from exc

        status = resp.status_code
        if status in (401, 403, 451):
            raise AccessRestrictedError(f"{url} refused access with HTTP {status}")
        if status == 304 or status in (404, 410):
            return FetchedPage(url=url, status=status, content=None)
        if status >= 400:
            raise FetchError(f"{url} returned HTTP {status} after {self.settings.max_retries} retries")

        return FetchedPage(
            url=url,
            status=status,
            content=resp.content,
            content_type=resp.headers.get("Content-Type"),
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )
