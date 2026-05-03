from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
from dateutil import parser as date_parser

from .config import Settings

logger = logging.getLogger(__name__)


class Crawler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def collect(self) -> List[Dict[str, Any]]:
        logger.info("Starting content collection")
        rss_items = self._collect_rss()
        newsapi_items = self._collect_newsapi()
        merged_items = rss_items + newsapi_items

        unique_items = self._deduplicate_in_batch(merged_items)
        fresh_items = self._filter_existing_urls(unique_items)

        logger.info(
            "Collection finished: rss=%s, newsapi=%s, merged=%s, unique=%s, fresh=%s",
            len(rss_items),
            len(newsapi_items),
            len(merged_items),
            len(unique_items),
            len(fresh_items),
        )
        return fresh_items

    def _collect_rss(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if not self.settings.rss_feeds:
            logger.warning("RSS_FEEDS is empty. Skipping RSS collection.")
            return results

        for feed_url in self.settings.rss_feeds:
            normalized_feed_url = self._normalize_url(feed_url)
            if not normalized_feed_url:
                logger.warning("Skipping invalid RSS URL: %r", feed_url)
                continue

            try:
                feed = feedparser.parse(normalized_feed_url)
                if getattr(feed, "bozo", False):
                    logger.warning(
                        "RSS feed has parse issues: %s | %s",
                        normalized_feed_url,
                        getattr(feed, "bozo_exception", "unknown parse error"),
                    )

                feed_title = (
                    str(feed.feed.get("title", "unknown")).strip()
                    if getattr(feed, "feed", None)
                    else "unknown"
                )
                feed_source = f"rss:{feed_title or 'unknown'}"

                for entry in feed.entries:
                    url = self._normalize_url(entry.get("link"))
                    title = str(entry.get("title", "")).strip()
                    if not url or not title:
                        continue

                    summary_raw = (
                        entry.get("summary")
                        or entry.get("description")
                        or entry.get("subtitle")
                        or ""
                    )
                    published = (
                        entry.get("published")
                        or entry.get("updated")
                        or entry.get("created")
                        or datetime.now(timezone.utc).isoformat()
                    )

                    results.append(
                        {
                            "source": feed_source,
                            "title": title,
                            "summary": str(summary_raw).strip(),
                            "url": url,
                            "published_at": self._normalize_datetime(published),
                        }
                    )
            except Exception as exc:
                logger.exception("RSS collection failed for %s: %s", normalized_feed_url, exc)

        return results

    def _collect_newsapi(self) -> List[Dict[str, Any]]:
        if not self.settings.newsapi_key:
            logger.warning("NEWSAPI_KEY is empty. Skipping NewsAPI.")
            return []

        endpoint = "https://newsapi.org/v2/everything"
        params = {
            "q": self.settings.newsapi_query,
            "language": self.settings.newsapi_language,
            "pageSize": self.settings.newsapi_page_size,
            "sortBy": "publishedAt",
        }
        headers = {"X-Api-Key": self.settings.newsapi_key}

        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                logger.error("NewsAPI payload is not a JSON object. Skipping.")
                return []

            status = payload.get("status")
            if status != "ok":
                logger.error(
                    "NewsAPI returned non-ok status: %s | message=%s",
                    status,
                    payload.get("message", ""),
                )
                return []

            articles = payload.get("articles", [])
            if not isinstance(articles, list):
                logger.error("NewsAPI 'articles' field is not a list. Skipping.")
                return []
        except requests.RequestException as exc:
            logger.exception("NewsAPI request failed: %s", exc)
            return []
        except ValueError as exc:
            logger.exception("NewsAPI JSON decode failed: %s", exc)
            return []
        except Exception as exc:
            logger.exception("Unexpected NewsAPI failure: %s", exc)
            return []

        results: List[Dict[str, Any]] = []
        for article in articles:
            if not isinstance(article, dict):
                continue

            url = self._normalize_url(article.get("url"))
            title = str(article.get("title") or "").strip()
            if not url or not title:
                continue

            source = article.get("source")
            source_name = "newsapi"
            if isinstance(source, dict):
                source_name = str(source.get("name") or "newsapi").strip() or "newsapi"

            results.append(
                {
                    "source": f"newsapi:{source_name}",
                    "title": title,
                    "summary": str(article.get("description") or "").strip(),
                    "url": url,
                    "published_at": self._normalize_datetime(
                        article.get("publishedAt") or datetime.now(timezone.utc).isoformat()
                    ),
                }
            )

        return results

    def _normalize_datetime(self, value: str) -> str:
        try:
            dt = date_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception as exc:
            logger.debug("Failed to parse datetime value %r: %s", value, exc)
            return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalize_url(raw_url: Any) -> Optional[str]:
        if not raw_url:
            return None
        try:
            url = str(raw_url).strip()
            if not url:
                return None
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"}:
                return None
            if not parsed.netloc:
                return None
            normalized = urlunsplit(
                (
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path or "",
                    parsed.query or "",
                    "",
                )
            )
            return normalized
        except Exception as exc:
            logger.debug("Invalid URL %r: %s", raw_url, exc)
            return None

    @staticmethod
    def _deduplicate_in_batch(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: Set[str] = set()
        unique: List[Dict[str, Any]] = []

        for item in items:
            url = item.get("url")
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            unique.append(item)

        return unique

    def _filter_existing_urls(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not items:
            return []

        urls = [item["url"] for item in items if item.get("url")]
        if not urls:
            return []

        existing_urls = self._fetch_existing_urls(urls)
        if not existing_urls:
            return items

        filtered = [item for item in items if item["url"] not in existing_urls]
        logger.info(
            "Filtered %s duplicate item(s) already in database",
            len(items) - len(filtered),
        )
        return filtered

    def _fetch_existing_urls(self, urls: Iterable[str]) -> Set[str]:
        normalized_urls = [url for url in urls if url]
        if not normalized_urls:
            return set()

        db_file = Path(self.settings.db_path)
        if not db_file.exists():
            return set()

        placeholders = ",".join("?" for _ in normalized_urls)
        sql = f"SELECT source_url FROM posts WHERE source_url IN ({placeholders})"

        try:
            with sqlite3.connect(self.settings.db_path) as conn:
                cur = conn.execute(sql, normalized_urls)
                rows = cur.fetchall()
            return {str(row[0]) for row in rows if row and row[0]}
        except sqlite3.Error as exc:
            logger.exception("Database deduplication query failed: %s", exc)
            return set()
