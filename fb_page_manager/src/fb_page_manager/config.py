"""Configuration loader for Facebook Page Manager.

This module loads environment variables from .env and exports
runtime config constants used by crawler, AI writer, poster, and scheduler.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env() -> None:
    """Load .env file once if present."""
    try:
        load_dotenv(dotenv_path=ENV_PATH if ENV_PATH.exists() else None, override=False)
    except Exception as exc:
        logger.exception("Failed to load .env: %s", exc)


_load_env()


def _first_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _parse_csv(value: str) -> List[str]:
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_times(value: str) -> List[str]:
    times = _parse_csv(value)
    valid: List[str] = []
    for item in times:
        try:
            hh, mm = item.split(":", 1)
            h = int(hh)
            m = int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid.append(f"{h:02d}:{m:02d}")
        except Exception:
            logger.warning("Invalid posting time format skipped: %s", item)
    return valid


PAGE_ID: str = _first_env("PAGE_ID", "FB_PAGE_ID")
ACCESS_TOKEN: str = _first_env("ACCESS_TOKEN", "FB_PAGE_ACCESS_TOKEN")
CLAUDE_API_KEY: str = _first_env("CLAUDE_API_KEY")
NEWS_API_KEY: str = _first_env("NEWS_API_KEY", "NEWSAPI_KEY")
RSS_URLS: List[str] = _parse_csv(_first_env("RSS_URLS", "RSS_FEEDS"))
POSTING_TIMES: List[str] = _parse_times(_first_env("POSTING_TIMES", default="07:00,11:30,20:00"))

# Additional optional config values used internally.
CLAUDE_MODEL: str = _first_env("CLAUDE_MODEL", default="claude-3-5-sonnet-latest")
DB_PATH: str = _first_env("DB_PATH", default="data/fb_page_manager.db")
NEWS_KEYWORD: str = _first_env("NEWS_KEYWORD", "NEWSAPI_QUERY", default="technology")
NEWS_LANGUAGE: str = _first_env("NEWS_LANGUAGE", "NEWSAPI_LANGUAGE", default="en")
NEWS_PAGE_SIZE: int = int(_first_env("NEWS_PAGE_SIZE", "NEWSAPI_PAGE_SIZE", default="10"))


@dataclass(frozen=True)
class Settings:
    """Compatibility settings object for modules needing structured config."""

    page_id: str
    access_token: str
    claude_api_key: str
    claude_model: str
    news_api_key: str
    rss_urls: List[str]
    posting_times: List[str]
    db_path: str
    news_keyword: str
    news_language: str
    news_page_size: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return immutable settings object."""
    return Settings(
        page_id=PAGE_ID,
        access_token=ACCESS_TOKEN,
        claude_api_key=CLAUDE_API_KEY,
        claude_model=CLAUDE_MODEL,
        news_api_key=NEWS_API_KEY,
        rss_urls=list(RSS_URLS),
        posting_times=list(POSTING_TIMES),
        db_path=DB_PATH,
        news_keyword=NEWS_KEYWORD,
        news_language=NEWS_LANGUAGE,
        news_page_size=NEWS_PAGE_SIZE,
    )


def reload_config() -> None:
    """Reload .env and refresh module-level constants.

    Useful for web APIs that update .env at runtime.
    """

    global PAGE_ID, ACCESS_TOKEN, CLAUDE_API_KEY, NEWS_API_KEY, RSS_URLS, POSTING_TIMES
    global CLAUDE_MODEL, DB_PATH, NEWS_KEYWORD, NEWS_LANGUAGE, NEWS_PAGE_SIZE

    _load_env()
    PAGE_ID = _first_env("PAGE_ID", "FB_PAGE_ID")
    ACCESS_TOKEN = _first_env("ACCESS_TOKEN", "FB_PAGE_ACCESS_TOKEN")
    CLAUDE_API_KEY = _first_env("CLAUDE_API_KEY")
    NEWS_API_KEY = _first_env("NEWS_API_KEY", "NEWSAPI_KEY")
    RSS_URLS = _parse_csv(_first_env("RSS_URLS", "RSS_FEEDS"))
    POSTING_TIMES = _parse_times(_first_env("POSTING_TIMES", default="07:00,11:30,20:00"))
    CLAUDE_MODEL = _first_env("CLAUDE_MODEL", default="claude-3-5-sonnet-latest")
    DB_PATH = _first_env("DB_PATH", default="data/fb_page_manager.db")
    NEWS_KEYWORD = _first_env("NEWS_KEYWORD", "NEWSAPI_QUERY", default="technology")
    NEWS_LANGUAGE = _first_env("NEWS_LANGUAGE", "NEWSAPI_LANGUAGE", default="en")
    NEWS_PAGE_SIZE = int(_first_env("NEWS_PAGE_SIZE", "NEWSAPI_PAGE_SIZE", default="10"))
    get_settings.cache_clear()

