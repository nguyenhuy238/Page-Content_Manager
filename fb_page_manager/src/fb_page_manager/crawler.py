"""Content crawler module.

Provides RSS and NewsAPI fetch helpers with duplicate filtering through database.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import feedparser
import requests

from .config import (
    CUSTOM_NEWS_URLS,
    NEWS_API_KEY,
    NEWS_KEYWORD,
    NEWS_LANGUAGE,
    NEWS_PAGE_SIZE,
    RSS_URLS,
)
from .database import Database
from .source_collector import collect_article_urls, resolve_source_lists

logger = logging.getLogger(__name__)


def _normalize_article(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    source = str(item.get("source") or "unknown").strip()
    summary = str(item.get("summary") or "").strip()
    published_at = str(item.get("published_at") or datetime.now(timezone.utc).isoformat())

    if not title or not url:
        return None

    return {
        "title": title,
        "url": url,
        "source": source,
        "summary": summary,
        "published_at": published_at,
    }


def _filter_duplicates(candidates: Iterable[Dict[str, Any]], db: Database) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    for raw in candidates:
        article = _normalize_article(raw)
        if article is None:
            continue

        url = article["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)

        try:
            if db.is_duplicate(url):
                continue
        except Exception as exc:
            logger.exception("Duplicate check failed for %s: %s", url, exc)

        filtered.append(article)

    return filtered


def fetch_rss(urls: List[str], db: Optional[Database] = None) -> List[Dict[str, Any]]:
    """Fetch RSS feeds and return standardized article dictionaries."""
    local_db = db or Database()
    close_when_done = db is None

    try:
        candidates: List[Dict[str, Any]] = []

        for feed_url in urls:
            if not str(feed_url).strip():
                continue

            try:
                feed = feedparser.parse(feed_url)
                feed_title = str(getattr(feed, "feed", {}).get("title", "RSS")).strip() or "RSS"
                source = f"rss:{feed_title}"

                for entry in getattr(feed, "entries", []):
                    link = str(entry.get("link") or "").strip()
                    title = str(entry.get("title") or "").strip()
                    if not link or not title:
                        continue

                    summary = (
                        entry.get("summary")
                        or entry.get("description")
                        or entry.get("subtitle")
                        or ""
                    )
                    published_at = (
                        entry.get("published")
                        or entry.get("updated")
                        or datetime.now(timezone.utc).isoformat()
                    )

                    candidates.append(
                        {
                            "title": title,
                            "url": link,
                            "source": source,
                            "summary": str(summary).strip(),
                            "published_at": str(published_at),
                        }
                    )
            except Exception as exc:
                logger.exception("Failed to parse RSS feed %s: %s", feed_url, exc)

        return _filter_duplicates(candidates, local_db)
    except Exception as exc:
        logger.exception("fetch_rss failed: %s", exc)
        return []
    finally:
        if close_when_done:
            local_db.close()


def fetch_newsapi(
    keyword: str,
    api_key: str,
    db: Optional[Database] = None,
    language: str = NEWS_LANGUAGE,
    page_size: int = NEWS_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """Fetch NewsAPI articles and return standardized article dictionaries."""
    if not api_key:
        logger.warning("NEWS_API_KEY missing. Skipping NewsAPI request.")
        return []

    local_db = db or Database()
    close_when_done = db is None

    try:
        endpoint = "https://newsapi.org/v2/everything"
        params = {
            "q": keyword,
            "language": language,
            "sortBy": "publishedAt",
            "pageSize": max(1, min(int(page_size), 100)),
        }
        headers = {"X-Api-Key": api_key}

        response = requests.get(endpoint, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()

        articles = payload.get("articles", []) if isinstance(payload, dict) else []
        candidates: List[Dict[str, Any]] = []

        for item in articles:
            if not isinstance(item, dict):
                continue

            link = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not link or not title:
                continue

            source_name = "newsapi"
            source_obj = item.get("source")
            if isinstance(source_obj, dict):
                source_name = str(source_obj.get("name") or "newsapi").strip() or "newsapi"

            candidates.append(
                {
                    "title": title,
                    "url": link,
                    "source": f"newsapi:{source_name}",
                    "summary": str(item.get("description") or "").strip(),
                    "published_at": str(
                        item.get("publishedAt") or datetime.now(timezone.utc).isoformat()
                    ),
                }
            )

        return _filter_duplicates(candidates, local_db)
    except Exception as exc:
        logger.exception("fetch_newsapi failed: %s", exc)
        return []
    finally:
        if close_when_done:
            local_db.close()


class Crawler:
    """Compatibility wrapper for pipeline-style collection."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    def collect(self) -> List[Dict[str, Any]]:
        """Collect from RSS + NewsAPI + configured article URLs and persist new articles into DB."""
        db = Database()
        try:
            urls = RSS_URLS
            custom_news_urls = CUSTOM_NEWS_URLS
            keyword = NEWS_KEYWORD
            api_key = NEWS_API_KEY

            if self.settings is not None:
                urls = getattr(self.settings, "rss_urls", urls)
                custom_news_urls = getattr(self.settings, "custom_news_urls", custom_news_urls)
                keyword = getattr(self.settings, "news_keyword", keyword)
                api_key = getattr(self.settings, "news_api_key", api_key)

            rss_items = fetch_rss(urls=urls, db=db)
            news_items = fetch_newsapi(keyword=keyword, api_key=api_key, db=db)
            _, article_urls = resolve_source_lists(
                youtube_urls=[],
                article_urls=list(custom_news_urls or []),
            )
            custom_url_items = collect_article_urls(article_urls, db=db)
            all_items = rss_items + news_items + custom_url_items

            saved: List[Dict[str, Any]] = []
            for article in all_items:
                article_id = db.save_article(article)
                if article_id is not None:
                    enriched = dict(article)
                    enriched["id"] = article_id
                    saved.append(enriched)

            logger.info(
                "Crawler collected rss=%s newsapi=%s custom_urls=%s saved=%s",
                len(rss_items),
                len(news_items),
                len(custom_url_items),
                len(saved),
            )
            return saved
        except Exception as exc:
            logger.exception("Crawler.collect failed: %s", exc)
            return []
        finally:
            db.close()

