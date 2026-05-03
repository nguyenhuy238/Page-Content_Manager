from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import feedparser
import requests
from dateutil import parser as date_parser

from .config import Settings

logger = logging.getLogger(__name__)


class Crawler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def collect(self) -> List[Dict[str, Any]]:
        items = self._collect_rss()
        items.extend(self._collect_newsapi())
        return self._deduplicate(items)

    def _collect_rss(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []

        for feed_url in self.settings.rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries:
                    url = entry.get("link")
                    title = entry.get("title", "").strip()
                    if not url or not title:
                        continue

                    summary = entry.get("summary", "") or entry.get("description", "")
                    published = (
                        entry.get("published")
                        or entry.get("updated")
                        or datetime.now(timezone.utc).isoformat()
                    )

                    results.append(
                        {
                            "source": f"rss:{feed.feed.get('title', 'unknown')}",
                            "title": title,
                            "summary": summary,
                            "url": url,
                            "published_at": self._normalize_datetime(published),
                            "original_caption": title,
                        }
                    )
            except Exception as exc:
                logger.exception("RSS collection failed for %s: %s", feed_url, exc)

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
            articles = payload.get("articles", [])
        except Exception as exc:
            logger.exception("NewsAPI request failed: %s", exc)
            return []

        results: List[Dict[str, Any]] = []
        for article in articles:
            url = article.get("url")
            title = (article.get("title") or "").strip()
            if not url or not title:
                continue

            source_name = article.get("source", {}).get("name", "newsapi")
            results.append(
                {
                    "source": f"newsapi:{source_name}",
                    "title": title,
                    "summary": article.get("description", ""),
                    "url": url,
                    "published_at": self._normalize_datetime(
                        article.get("publishedAt") or datetime.now(timezone.utc).isoformat()
                    ),
                    "original_caption": title,
                }
            )

        return results

    def _normalize_datetime(self, value: str) -> str:
        try:
            dt = date_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _deduplicate(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        unique: List[Dict[str, Any]] = []

        for item in items:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            unique.append(item)

        return unique

