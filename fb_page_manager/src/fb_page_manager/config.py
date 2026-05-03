from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    rss_feeds: List[str]
    newsapi_key: str
    newsapi_query: str
    newsapi_language: str
    newsapi_page_size: int
    claude_api_key: str
    claude_model: str
    fb_page_id: str
    fb_page_access_token: str
    fb_api_version: str
    post_interval_minutes: int
    max_posts_per_run: int
    timezone: str
    log_level: str
    db_path: str


def _parse_csv_env(key: str, default: str = "") -> List[str]:
    raw = os.getenv(key, default).strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()

    return Settings(
        rss_feeds=_parse_csv_env("RSS_FEEDS"),
        newsapi_key=os.getenv("NEWSAPI_KEY", ""),
        newsapi_query=os.getenv("NEWSAPI_QUERY", "technology"),
        newsapi_language=os.getenv("NEWSAPI_LANGUAGE", "en"),
        newsapi_page_size=int(os.getenv("NEWSAPI_PAGE_SIZE", "10")),
        claude_api_key=os.getenv("CLAUDE_API_KEY", ""),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest"),
        fb_page_id=os.getenv("FB_PAGE_ID", ""),
        fb_page_access_token=os.getenv("FB_PAGE_ACCESS_TOKEN", ""),
        fb_api_version=os.getenv("FB_API_VERSION", "v22.0"),
        post_interval_minutes=int(os.getenv("POST_INTERVAL_MINUTES", "60")),
        max_posts_per_run=int(os.getenv("MAX_POSTS_PER_RUN", "5")),
        timezone=os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        db_path=os.getenv("DB_PATH", "data/fb_page_manager.db"),
    )

