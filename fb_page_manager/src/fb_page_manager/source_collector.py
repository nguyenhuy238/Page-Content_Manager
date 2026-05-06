"""Collect long-form source material from article URLs and YouTube channels."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

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
    if not paragraphs:
        for selector in ["div[itemprop='articleBody']", ".article-body", ".entry-content", ".post-content"]:
            block = soup.select_one(selector)
            if not block:
                continue
            alt = [_clean_text(p.get_text(" ", strip=True)) for p in block.find_all("p")]
            alt = [p for p in alt if len(p) >= 40]
            if alt:
                paragraphs = alt
                break
    text = "\n\n".join(paragraphs[:60]).strip()
    if text:
        return title, text

    # Fallback 1: structured data (JSON-LD).
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        nodes: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                nodes.extend([x for x in data.get("@graph", []) if isinstance(x, dict)])
            nodes.append(data)
        elif isinstance(data, list):
            nodes.extend([x for x in data if isinstance(x, dict)])

        for node in nodes:
            article_body = _clean_text(str(node.get("articleBody") or ""))
            description = _clean_text(str(node.get("description") or ""))
            text_candidate = article_body or description
            if len(text_candidate) >= 120:
                return title, text_candidate

    # Fallback 2: OpenGraph / meta description.
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc and og_desc.get("content"):
        meta_text = _clean_text(str(og_desc.get("content") or ""))
        if len(meta_text) >= 80:
            return title, meta_text

    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        meta_text = _clean_text(str(meta_desc.get("content") or ""))
        if len(meta_text) >= 80:
            return title, meta_text

    return title, text


def _summary_from_text(text: str, max_words: int = 80) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).strip() + "..."


def _normalize_http_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _is_probable_domain_seed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    path = (parsed.path or "").strip()
    if path in {"", "/"}:
        return True
    lowered = path.lower()
    if lowered.endswith((".xml", ".rss", ".atom", ".json")):
        return False
    # Category/section pages can also be used as seeds.
    segments = [p for p in path.split("/") if p]
    return len(segments) <= 2


def _looks_like_article_path(path: str) -> bool:
    lowered = (path or "").lower()
    if not lowered or lowered in {"/", ""}:
        return False
    if lowered.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".mp4")):
        return False
    if any(x in lowered for x in ["/tag/", "/author/", "/category/", "/search", "/video/", "/videos/"]):
        return False
    if any(
        x in lowered
        for x in [
            "/about",
            "/contact",
            "/privacy",
            "/politica-cookies",
            "/newsletter",
            "/suscripcion",
            "/shopping",
            "/beauty-addict",
            "/horoscopo",
        ]
    ):
        return False
    if re.search(r"/\d{4}/\d{1,2}/", lowered):
        return True
    segments = [p for p in lowered.split("/") if p]
    return len(segments) >= 2 and any("-" in seg for seg in segments)


def _extract_candidate_article_links(page_url: str, page_html: str, limit: int = 15) -> List[str]:
    parsed_seed = urlparse(page_url)
    seed_host = (parsed_seed.netloc or "").lower()
    soup = BeautifulSoup(page_html, "html.parser")
    seen: set[str] = set()
    links: List[str] = []

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        host = (parsed.netloc or "").lower()
        if host != seed_host:
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        if clean in seen:
            continue
        seen.add(clean)
        if _looks_like_article_path(parsed.path):
            links.append(clean)
            if len(links) >= limit:
                break
    return links


def _is_probable_article_html(page_html: str, url: str, text: str) -> bool:
    clean_len = len(_clean_text(text))
    if clean_len < 60:
        return False

    soup = BeautifulSoup(page_html, "html.parser")
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    # HOLA article URLs usually contain a long numeric slug segment.
    if "hola.com" in host and re.search(r"/\d{8,}/", path):
        return True

    og_type = soup.find("meta", attrs={"property": "og:type"})
    og_value = str(og_type.get("content") or "").lower() if og_type else ""
    if "article" in og_value:
        return True

    if soup.find("article") is not None:
        return True

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        lowered = raw.lower()
        if any(kind in lowered for kind in ['"@type":"newsarticle"', '"@type":"article"', '"@type":"blogposting"']):
            return True

    return _looks_like_article_path(parsed.path)


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


def _filter_new_items(
    items: Iterable[Dict[str, Any]],
    db: Database,
    *,
    skip_db_duplicates: bool = True,
) -> List[Dict[str, Any]]:
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
        if skip_db_duplicates:
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
            raw_url = _normalize_http_url(url)
            if not raw_url:
                continue
            try:
                response = session.get(raw_url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                target_urls = [raw_url]
                if _is_probable_domain_seed(raw_url):
                    discovered = _extract_candidate_article_links(
                        page_url=raw_url,
                        page_html=response.text,
                        limit=15,
                    )
                    if discovered:
                        target_urls = discovered
                        logger.info(
                            "Discovered %s article links from seed domain %s",
                            len(discovered),
                            raw_url,
                        )

                for article_url in target_urls:
                    try:
                        article_res = (
                            response if article_url == raw_url and target_urls == [raw_url]
                            else session.get(
                                article_url,
                                timeout=25,
                                headers={"User-Agent": "Mozilla/5.0"},
                            )
                        )
                        article_res.raise_for_status()
                        title, full_text = _extract_article_text(article_res.text)
                        if not title:
                            title = article_url
                        if not _is_probable_article_html(article_res.text, article_url, full_text):
                            logger.info("Skip non-article or thin content url=%s", article_url)
                            continue
                        summary = _summary_from_text(full_text, max_words=220)
                        domain = (urlparse(article_url).netloc or "url_list").lower()
                        output.append(
                            {
                                "title": title,
                                "url": article_url,
                                "source": f"news:{domain}",
                                "summary": summary,
                                "content": full_text,
                                "published_at": _utc_iso(),
                                "content_type": "article",
                            }
                        )
                    except Exception as article_exc:
                        logger.warning("Failed to collect article %s: %s", article_url, article_exc)
            except Exception as exc:
                logger.warning("Failed to collect article %s: %s", raw_url, exc)
        # Keep duplicate URLs so caller can refresh empty summaries for existing rows.
        return _filter_new_items(output, local_db, skip_db_duplicates=False)
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

        return _filter_new_items(output, local_db, skip_db_duplicates=True)
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
