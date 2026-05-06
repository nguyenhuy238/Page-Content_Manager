"""Configuration loader for Facebook and web content automation."""

from __future__ import annotations

import logging
import os
import re
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
    parts = re.split(r"[,;\n\r]+", value)
    return [item.strip() for item in parts if item.strip()]


def _parse_bool(value: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_int(value: str, *, default: int) -> int:
    try:
        return int(value.strip())
    except Exception:
        return default


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
GEMINI_API_KEY: str = _first_env("GEMINI_API_KEY", "CLAUDE_API_KEY")
NEWS_API_KEY: str = _first_env("NEWS_API_KEY", "NEWSAPI_KEY")
RSS_URLS: List[str] = _parse_csv(_first_env("RSS_URLS", "RSS_FEEDS"))
POSTING_TIMES: List[str] = _parse_times(_first_env("POSTING_TIMES", default="07:00,11:30,20:00"))

GEMINI_MODEL: str = _first_env("GEMINI_MODEL", "CLAUDE_MODEL", default="gemini-1.5-flash")
DB_PATH: str = _first_env("DB_PATH", default="data/fb_page_manager.db")
NEWS_KEYWORD: str = _first_env("NEWS_KEYWORD", "NEWSAPI_QUERY", default="technology")
NEWS_LANGUAGE: str = _first_env("NEWS_LANGUAGE", "NEWSAPI_LANGUAGE", default="en")
NEWS_PAGE_SIZE: int = _parse_int(_first_env("NEWS_PAGE_SIZE", "NEWSAPI_PAGE_SIZE", default="10"), default=10)
LOG_LEVEL: str = _first_env("LOG_LEVEL", default="INFO")

# Campaign automation settings (Mexico-focused publishing workflow)
TARGET_LANGUAGE: str = _first_env("TARGET_LANGUAGE", default="es-MX")
TARGET_COUNTRY: str = _first_env("TARGET_COUNTRY", default="Mexico")
TARGET_AUDIENCE: str = _first_env("TARGET_AUDIENCE", default="Audiencia mexicana interesada en celebridades y cronicas")

YOUTUBE_CHANNEL_URLS: List[str] = _parse_csv(_first_env("YOUTUBE_CHANNEL_URLS"))
CUSTOM_NEWS_URLS: List[str] = _parse_csv(_first_env("CUSTOM_NEWS_URLS"))
YOUTUBE_MAX_VIDEOS_PER_CHANNEL: int = _parse_int(
    _first_env("YOUTUBE_MAX_VIDEOS_PER_CHANNEL", default="2"),
    default=2,
)
YOUTUBE_TRANSCRIPT_LANGS: List[str] = _parse_csv(
    _first_env("YOUTUBE_TRANSCRIPT_LANGS", default="es,en")
)

PIPELINE_BATCH_SIZE: int = _parse_int(_first_env("PIPELINE_BATCH_SIZE", default="4"), default=4)
PIPELINE_DRY_RUN: bool = _parse_bool(_first_env("PIPELINE_DRY_RUN", default="true"), default=True)
PIPELINE_AUTO_POST_FACEBOOK: bool = _parse_bool(
    _first_env("PIPELINE_AUTO_POST_FACEBOOK", default="false"),
    default=False,
)
PIPELINE_AUTO_POST_WORDPRESS: bool = _parse_bool(
    _first_env("PIPELINE_AUTO_POST_WORDPRESS", default="false"),
    default=False,
)
PIPELINE_AUTO_COMMENT_ON_FACEBOOK: bool = _parse_bool(
    _first_env("PIPELINE_AUTO_COMMENT_ON_FACEBOOK", default="true"),
    default=True,
)

FB_COMMENT_TEMPLATE: str = _first_env(
    "FB_COMMENT_TEMPLATE",
    default="Lee la historia completa aqui: {url}",
)

WP_BASE_URL: str = _first_env("WP_BASE_URL")
WP_USERNAME: str = _first_env("WP_USERNAME")
WP_APP_PASSWORD: str = _first_env("WP_APP_PASSWORD")
WP_POST_STATUS: str = _first_env("WP_POST_STATUS", default="draft")
WP_DEFAULT_CATEGORY_ID: int = _parse_int(_first_env("WP_DEFAULT_CATEGORY_ID", default="0"), default=0)
WP_DEFAULT_AUTHOR_ID: int = _parse_int(_first_env("WP_DEFAULT_AUTHOR_ID", default="0"), default=0)

OPENAI_API_KEY: str = _first_env("OPENAI_API_KEY")
OPENAI_IMAGE_MODEL: str = _first_env("OPENAI_IMAGE_MODEL", default="gpt-image-1")
OPENAI_IMAGE_SIZE: str = _first_env("OPENAI_IMAGE_SIZE", default="1024x1536")
GENERATED_IMAGE_DIR: str = _first_env("GENERATED_IMAGE_DIR", default="data/generated_images")


@dataclass(frozen=True)
class Settings:
    """Compatibility settings object for modules needing structured config."""

    page_id: str
    access_token: str
    gemini_api_key: str
    gemini_model: str
    news_api_key: str
    rss_urls: List[str]
    posting_times: List[str]
    db_path: str
    news_keyword: str
    news_language: str
    news_page_size: int
    log_level: str
    target_language: str
    target_country: str
    target_audience: str
    youtube_channel_urls: List[str]
    custom_news_urls: List[str]
    youtube_max_videos_per_channel: int
    youtube_transcript_langs: List[str]
    pipeline_batch_size: int
    pipeline_dry_run: bool
    pipeline_auto_post_facebook: bool
    pipeline_auto_post_wordpress: bool
    pipeline_auto_comment_on_facebook: bool
    fb_comment_template: str
    wp_base_url: str
    wp_username: str
    wp_app_password: str
    wp_post_status: str
    wp_default_category_id: int
    wp_default_author_id: int
    openai_api_key: str
    openai_image_model: str
    openai_image_size: str
    generated_image_dir: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return immutable settings object."""
    return Settings(
        page_id=PAGE_ID,
        access_token=ACCESS_TOKEN,
        gemini_api_key=GEMINI_API_KEY,
        gemini_model=GEMINI_MODEL,
        news_api_key=NEWS_API_KEY,
        rss_urls=list(RSS_URLS),
        posting_times=list(POSTING_TIMES),
        db_path=DB_PATH,
        news_keyword=NEWS_KEYWORD,
        news_language=NEWS_LANGUAGE,
        news_page_size=NEWS_PAGE_SIZE,
        log_level=LOG_LEVEL,
        target_language=TARGET_LANGUAGE,
        target_country=TARGET_COUNTRY,
        target_audience=TARGET_AUDIENCE,
        youtube_channel_urls=list(YOUTUBE_CHANNEL_URLS),
        custom_news_urls=list(CUSTOM_NEWS_URLS),
        youtube_max_videos_per_channel=YOUTUBE_MAX_VIDEOS_PER_CHANNEL,
        youtube_transcript_langs=list(YOUTUBE_TRANSCRIPT_LANGS),
        pipeline_batch_size=PIPELINE_BATCH_SIZE,
        pipeline_dry_run=PIPELINE_DRY_RUN,
        pipeline_auto_post_facebook=PIPELINE_AUTO_POST_FACEBOOK,
        pipeline_auto_post_wordpress=PIPELINE_AUTO_POST_WORDPRESS,
        pipeline_auto_comment_on_facebook=PIPELINE_AUTO_COMMENT_ON_FACEBOOK,
        fb_comment_template=FB_COMMENT_TEMPLATE,
        wp_base_url=WP_BASE_URL,
        wp_username=WP_USERNAME,
        wp_app_password=WP_APP_PASSWORD,
        wp_post_status=WP_POST_STATUS,
        wp_default_category_id=WP_DEFAULT_CATEGORY_ID,
        wp_default_author_id=WP_DEFAULT_AUTHOR_ID,
        openai_api_key=OPENAI_API_KEY,
        openai_image_model=OPENAI_IMAGE_MODEL,
        openai_image_size=OPENAI_IMAGE_SIZE,
        generated_image_dir=GENERATED_IMAGE_DIR,
    )


def reload_config() -> None:
    """Reload .env and refresh module-level constants."""

    global PAGE_ID, ACCESS_TOKEN, GEMINI_API_KEY, NEWS_API_KEY, RSS_URLS, POSTING_TIMES
    global GEMINI_MODEL, DB_PATH, NEWS_KEYWORD, NEWS_LANGUAGE, NEWS_PAGE_SIZE, LOG_LEVEL
    global TARGET_LANGUAGE, TARGET_COUNTRY, TARGET_AUDIENCE
    global YOUTUBE_CHANNEL_URLS, CUSTOM_NEWS_URLS, YOUTUBE_MAX_VIDEOS_PER_CHANNEL
    global YOUTUBE_TRANSCRIPT_LANGS, PIPELINE_BATCH_SIZE, PIPELINE_DRY_RUN
    global PIPELINE_AUTO_POST_FACEBOOK, PIPELINE_AUTO_POST_WORDPRESS
    global PIPELINE_AUTO_COMMENT_ON_FACEBOOK, FB_COMMENT_TEMPLATE
    global WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD, WP_POST_STATUS
    global WP_DEFAULT_CATEGORY_ID, WP_DEFAULT_AUTHOR_ID
    global OPENAI_API_KEY, OPENAI_IMAGE_MODEL, OPENAI_IMAGE_SIZE, GENERATED_IMAGE_DIR

    _load_env()
    PAGE_ID = _first_env("PAGE_ID", "FB_PAGE_ID")
    ACCESS_TOKEN = _first_env("ACCESS_TOKEN", "FB_PAGE_ACCESS_TOKEN")
    GEMINI_API_KEY = _first_env("GEMINI_API_KEY", "CLAUDE_API_KEY")
    NEWS_API_KEY = _first_env("NEWS_API_KEY", "NEWSAPI_KEY")
    RSS_URLS = _parse_csv(_first_env("RSS_URLS", "RSS_FEEDS"))
    POSTING_TIMES = _parse_times(_first_env("POSTING_TIMES", default="07:00,11:30,20:00"))
    GEMINI_MODEL = _first_env("GEMINI_MODEL", "CLAUDE_MODEL", default="gemini-1.5-flash")
    DB_PATH = _first_env("DB_PATH", default="data/fb_page_manager.db")
    NEWS_KEYWORD = _first_env("NEWS_KEYWORD", "NEWSAPI_QUERY", default="technology")
    NEWS_LANGUAGE = _first_env("NEWS_LANGUAGE", "NEWSAPI_LANGUAGE", default="en")
    NEWS_PAGE_SIZE = _parse_int(_first_env("NEWS_PAGE_SIZE", "NEWSAPI_PAGE_SIZE", default="10"), default=10)
    LOG_LEVEL = _first_env("LOG_LEVEL", default="INFO")

    TARGET_LANGUAGE = _first_env("TARGET_LANGUAGE", default="es-MX")
    TARGET_COUNTRY = _first_env("TARGET_COUNTRY", default="Mexico")
    TARGET_AUDIENCE = _first_env(
        "TARGET_AUDIENCE",
        default="Audiencia mexicana interesada en celebridades y cronicas",
    )
    YOUTUBE_CHANNEL_URLS = _parse_csv(_first_env("YOUTUBE_CHANNEL_URLS"))
    CUSTOM_NEWS_URLS = _parse_csv(_first_env("CUSTOM_NEWS_URLS"))
    YOUTUBE_MAX_VIDEOS_PER_CHANNEL = _parse_int(
        _first_env("YOUTUBE_MAX_VIDEOS_PER_CHANNEL", default="2"),
        default=2,
    )
    YOUTUBE_TRANSCRIPT_LANGS = _parse_csv(_first_env("YOUTUBE_TRANSCRIPT_LANGS", default="es,en"))
    PIPELINE_BATCH_SIZE = _parse_int(_first_env("PIPELINE_BATCH_SIZE", default="4"), default=4)
    PIPELINE_DRY_RUN = _parse_bool(_first_env("PIPELINE_DRY_RUN", default="true"), default=True)
    PIPELINE_AUTO_POST_FACEBOOK = _parse_bool(
        _first_env("PIPELINE_AUTO_POST_FACEBOOK", default="false"),
        default=False,
    )
    PIPELINE_AUTO_POST_WORDPRESS = _parse_bool(
        _first_env("PIPELINE_AUTO_POST_WORDPRESS", default="false"),
        default=False,
    )
    PIPELINE_AUTO_COMMENT_ON_FACEBOOK = _parse_bool(
        _first_env("PIPELINE_AUTO_COMMENT_ON_FACEBOOK", default="true"),
        default=True,
    )
    FB_COMMENT_TEMPLATE = _first_env(
        "FB_COMMENT_TEMPLATE",
        default="Lee la historia completa aqui: {url}",
    )

    WP_BASE_URL = _first_env("WP_BASE_URL")
    WP_USERNAME = _first_env("WP_USERNAME")
    WP_APP_PASSWORD = _first_env("WP_APP_PASSWORD")
    WP_POST_STATUS = _first_env("WP_POST_STATUS", default="draft")
    WP_DEFAULT_CATEGORY_ID = _parse_int(_first_env("WP_DEFAULT_CATEGORY_ID", default="0"), default=0)
    WP_DEFAULT_AUTHOR_ID = _parse_int(_first_env("WP_DEFAULT_AUTHOR_ID", default="0"), default=0)

    OPENAI_API_KEY = _first_env("OPENAI_API_KEY")
    OPENAI_IMAGE_MODEL = _first_env("OPENAI_IMAGE_MODEL", default="gpt-image-1")
    OPENAI_IMAGE_SIZE = _first_env("OPENAI_IMAGE_SIZE", default="1024x1536")
    GENERATED_IMAGE_DIR = _first_env("GENERATED_IMAGE_DIR", default="data/generated_images")
    get_settings.cache_clear()

