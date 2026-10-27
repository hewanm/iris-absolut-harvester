"""Runtime configuration.

Everything is read from environment variables (optionally loaded from a local
``.env`` file). No credentials live in source code; see ``.env.example``.
robots.txt is always honoured in live mode and is deliberately not configurable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from dotenv import load_dotenv

DATA_DIR = resources.files("iris_harvester") / "data"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


@dataclass(frozen=True)
class Settings:
    database_url: str | None = None
    feed_url: str = "https://www.absolut-report.de/feed/"
    source_system: str = "absolut_report"
    signal_type: str = "institutional_asset_allocation"
    user_agent: str = "IRIS-SIG-49-harvester/0.1 (+contact: set IRIS_CONTACT)"
    timeout_s: float = 20.0
    max_retries: int = 3
    backoff_factor: float = 1.0
    max_pages: int = 5
    page_delay_s: float = 1.0
    summary_max_chars: int = 2000
    taxonomy_path: Path = field(default_factory=lambda: Path(str(DATA_DIR / "taxonomy.yaml")))
    gazetteer_path: Path = field(default_factory=lambda: Path(str(DATA_DIR / "gazetteer_de.csv")))
    institutions_path: Path = field(default_factory=lambda: Path(str(DATA_DIR / "institutions_de.csv")))

    @classmethod
    def from_env(cls, dotenv: bool = True) -> "Settings":
        if dotenv:
            load_dotenv(override=False)
        contact = _env("IRIS_CONTACT")
        default_ua = cls.user_agent if not contact else f"IRIS-SIG-49-harvester/0.1 (+{contact})"
        defaults = cls()
        return cls(
            database_url=_env("DATABASE_URL"),
            feed_url=_env("IRIS_FEED_URL", defaults.feed_url),
            source_system=_env("IRIS_SOURCE_SYSTEM", defaults.source_system),
            user_agent=_env("IRIS_USER_AGENT", default_ua),
            timeout_s=_env_float("IRIS_TIMEOUT_S", defaults.timeout_s),
            max_retries=_env_int("IRIS_MAX_RETRIES", defaults.max_retries),
            backoff_factor=_env_float("IRIS_BACKOFF_FACTOR", defaults.backoff_factor),
            max_pages=_env_int("IRIS_MAX_PAGES", defaults.max_pages),
            page_delay_s=_env_float("IRIS_PAGE_DELAY_S", defaults.page_delay_s),
            summary_max_chars=_env_int("IRIS_SUMMARY_MAX_CHARS", defaults.summary_max_chars),
            taxonomy_path=Path(_env("IRIS_TAXONOMY_PATH", str(defaults.taxonomy_path))),
            gazetteer_path=Path(_env("IRIS_GAZETTEER_PATH", str(defaults.gazetteer_path))),
            institutions_path=Path(_env("IRIS_INSTITUTIONS_PATH", str(defaults.institutions_path))),
        )
