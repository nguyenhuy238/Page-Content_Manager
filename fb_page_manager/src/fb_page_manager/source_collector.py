"""Collect long-form source material from article URLs and YouTube channels."""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .database import Database

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except Exception:  # pragma: no cover - optional dependency fallback
    YouTubeTranscriptApi = None

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return parsed.path.strip("/").split("/")[0]
    if "youtube.com" in host:
        qs = parse_qs(parsed.query)
        values = qs.get("v") or []
        if values:
            return values[0].strip()
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1].strip()
    return ""


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _extract_article_text(page_html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(page_html, "html.parser")
    title = ""

    if soup.title and soup.title.string:
        title = _clean_text(soup.title.string)

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = _clean_text(str(og_title["content"]))

    for tag in soup(["script", "style", "noscript"]):
        tag.extract()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [_clean_text(p.get_text(" ", strip=True)) for p in root.find_all("p")]
    paragraphs = [p for p in paragraphs if len(p) >= 40]
    text = "\n\n".join(paragraphs[:60]).strip()
    return title, text


def _summary_from_text(text: str, max_words: int = 80) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).strip() + "..."


def _resolve_channel_id(channel_url: str, session: requests.Session) -> str:
    parsed = urlparse(channel_url)
    path = parsed.path.strip("/")
    if path.startswith("channel/"):
        return path.split("/", 1)[1].strip()

    target = channel_url.rstrip("/")
    if not target.endswith("/videos"):
        target = target + "/videos"

    response = session.get(
        target,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    html_text = response.text

    patterns = [
        r'"channelId":"(UC[\w-]{20,})"',
        r'"externalId":"(UC[\w-]{20,})"',
        r'channelId=(UC[\w-]{20,})',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text)
        if match:
            return match.group(1).strip()
    return ""


def _youtube_feed_url(channel_url: str, session: requests.Session) -> str:
    channel_id = _resolve_channel_id(channel_url, session)
    if not channel_id:
        return ""
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _fetch_transcript(video_id: str, languages: List[str]) -> str:
    if not video_id or YouTubeTranscriptApi is None:
        return ""
    try:
        client = YouTubeTranscriptApi()
        transcript_obj = client.fetch(video_id, languages=languages or ["es", "en"])
        segments = getattr(transcript_obj, "snippets", transcript_obj)
        combined = " ".join(_clean_text(item.text) for item in segments if getattr(item, "text", ""))
        return _summary_from_text(combined, max_words=1200)
    except Exception as exc:
        logger.info("Transcript unavailable for %s: %s", video_id, exc)
        return ""


def _filter_new_items(items: Iterable[Dict[str, Any]], db: Database) -> List[Dict[str, Any]]:
    clean: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        if url in seen:
            continue
        seen.add(url)
        try:
            if db.is_duplicate(url):
                continue
        except Exception:
            pass
        clean.append(item)
    return clean


def collect_article_urls(urls: List[str], db: Optional[Database] = None) -> List[Dict[str, Any]]:
    local_db = db or Database()
    close_when_done = db is None

    session = requests.Session()
    output: List[Dict[str, Any]] = []
    try:
        for url in urls:
            raw_url = str(url).strip()
            if not raw_url:
                continue
            try:
                response = session.get(
                    raw_url,
                    timeout=25,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
                title, full_text = _extract_article_text(response.text)
                if not title:
                    title = raw_url
                summary = _summary_from_text(full_text, max_words=100)
                output.append(
                    {
                        "title": title,
                        "url": raw_url,
                        "source": "news:url_list",
                        "summary": summary,
                        "content": full_text,
                        "published_at": _utc_iso(),
                        "content_type": "article",
                    }
                )
            except Exception as exc:
                logger.warning("Failed to collect article %s: %s", raw_url, exc)
        return _filter_new_items(output, local_db)
    finally:
        session.close()
        if close_when_done:
            local_db.close()


def collect_youtube_channels(
    channel_urls: List[str],
    transcript_languages: List[str],
    max_videos_per_channel: int = 2,
    db: Optional[Database] = None,
) -> List[Dict[str, Any]]:
    local_db = db or Database()
    close_when_done = db is None

    session = requests.Session()
    output: List[Dict[str, Any]] = []
    try:
        for channel_url in channel_urls:
            raw_channel_url = str(channel_url).strip()
            if not raw_channel_url:
                continue

            try:
                feed_url = _youtube_feed_url(raw_channel_url, session)
                if not feed_url:
                    logger.info("Cannot resolve channel ID for %s", raw_channel_url)
                    continue

                feed = feedparser.parse(feed_url)
                feed_title = str(getattr(feed, "feed", {}).get("title", "YouTube")).strip()
                entries = list(getattr(feed, "entries", []))[: max(1, int(max_videos_per_channel))]

                for entry in entries:
                    video_url = str(entry.get("link") or "").strip()
                    video_title = _clean_text(str(entry.get("title") or ""))
                    if not video_url or not video_title:
                        continue

                    video_id = _extract_video_id(video_url)
                    transcript = _fetch_transcript(video_id, transcript_languages)
                    summary = _summary_from_text(transcript, max_words=90) if transcript else video_title

                    output.append(
                        {
                            "title": video_title,
                            "url": video_url,
                            "source": f"youtube:{feed_title}",
                            "summary": summary,
                            "content": transcript,
                            "published_at": str(entry.get("published") or _utc_iso()),
                            "content_type": "youtube",
                            "video_id": video_id,
                            "channel_url": raw_channel_url,
                        }
                    )
            except Exception as exc:
                logger.warning("Failed to collect YouTube channel %s: %s", raw_channel_url, exc)

        return _filter_new_items(output, local_db)
    finally:
        session.close()
        if close_when_done:
            local_db.close()


def resolve_source_lists(
    youtube_urls: List[str],
    article_urls: List[str],
) -> Tuple[List[str], List[str]]:
    final_youtube = [u.strip() for u in youtube_urls if str(u).strip()]
    final_articles = [u.strip() for u in article_urls if str(u).strip()]
    return final_youtube, final_articles
