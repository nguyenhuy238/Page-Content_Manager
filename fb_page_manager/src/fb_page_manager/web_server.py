"""Flask web server for Facebook Page Manager dashboard.

This server keeps a stable `/api/...` contract for the frontend dashboard,
while using the new database API/schema (`articles`, `posts`) underneath.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import google.generativeai as genai
from flask import Flask, jsonify, render_template, request

from .ai_writer import generate_caption, optimize_prompt_template, quality_check
from .campaign_pipeline import CampaignAutomationPipeline
from .config import DB_PATH, get_settings, reload_config
from .crawler import fetch_newsapi, fetch_rss
from .database import Database
from .fb_poster import GRAPH_VERSION, get_post_insights, post_to_page
from .source_collector import collect_article_urls, collect_youtube_channels, resolve_source_lists

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "templates"
ENV_FILE = PROJECT_ROOT / ".env"


def _success(payload: Dict[str, Any] | List[Any] | None = None, status: int = 200):
    body = {"ok": True, "data": payload if payload is not None else {}}
    return jsonify(body), status


def _error(message: str, status: int = 400, details: Any = None):
    body: Dict[str, Any] = {"ok": False, "error": message}
    if details is not None:
        body["details"] = details
    return jsonify(body), status


def _open_db() -> Database:
    try:
        settings = get_settings()
        return Database(settings.db_path)
    except Exception:
        return Database(DB_PATH)


def _ensure_env_file() -> None:
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")


def _save_env_values(values: Dict[str, str]) -> None:
    _ensure_env_file()
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    pending = dict(values)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue

        key, _ = line.split("=", 1)
        key = key.strip()
        if key in pending:
            lines[idx] = f"{key}={pending[key]}"
            del pending[key]

    for key, value in pending.items():
        lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _serialize_article(row: Dict[str, Any]) -> Dict[str, Any]:
    fetched_at = row.get("fetched_at")
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "url": row.get("url") or "",
        "source": row.get("source") or "unknown",
        "summary": row.get("summary") or "",
        "published_at": fetched_at,
        "created_at": fetched_at,
        "used": bool(row.get("used")),
    }


def _status_label(status: str) -> str:
    mapping = {
        "queued": "Đã lên lịch",
        "posted": "Đã đăng",
        "failed": "Thất bại",
    }
    return mapping.get(status, status)


def _status_for_ui(status: str) -> str:
    if status == "queued":
        return "scheduled"
    return status


def _try_fb_status() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.page_id or not settings.access_token:
        return False, "Thiếu PAGE_ID hoặc ACCESS_TOKEN"

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{settings.page_id}"
    params = {
        "fields": "id,name",
        "access_token": settings.access_token,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return False, "Facebook API trả dữ liệu không hợp lệ"
        return True, f"Kết nối OK: {payload.get('name', 'Unknown Page')}"
    except Exception as exc:
        logger.exception("Facebook status check failed: %s", exc)
        return False, f"Facebook API lỗi: {exc}"


def _fetch_facebook_pages(access_token: str) -> Tuple[List[Dict[str, str]], Optional[str]]:
    token = (access_token or "").strip()
    if not token:
        return [], "Thiếu ACCESS_TOKEN để tải danh sách Page"

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/me/accounts"
    params = {
        "fields": "id,name",
        "access_token": token,
        "limit": 200,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data") if isinstance(payload, dict) else []
        pages: List[Dict[str, str]] = []
        if isinstance(items, list):
            for row in items:
                if not isinstance(row, dict):
                    continue
                page_id = str(row.get("id") or "").strip()
                page_name = str(row.get("name") or "").strip()
                if page_id:
                    pages.append({"id": page_id, "name": page_name or page_id})
        return pages, None
    except Exception as exc:
        logger.exception("Cannot fetch facebook pages: %s", exc)
        return [], f"Không thể tải danh sách Page: {exc}"


def _try_gemini_status() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.gemini_api_key:
        return False, "Thiếu GEMINI_API_KEY"

    try:
        genai.configure(api_key=settings.gemini_api_key)
        models = list(genai.list_models())
        if not models:
            return False, "Gemini API trả danh sách model rỗng"
        return True, "Kết nối Gemini API OK"
    except Exception as exc:
        logger.exception("Gemini status check failed: %s", exc)
        return False, f"Gemini API lỗi: {exc}"


def _try_openai_status() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.openai_api_key:
        return False, "Thiếu OPENAI_API_KEY"

    try:
        response = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=15,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            message = payload.get("error", {}).get("message", f"HTTP {response.status_code}")
            return False, f"OpenAI API lỗi: {message}"
        models = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(models, list) or not models:
            return False, "OpenAI API trả danh sách model rỗng"
        return True, "Kết nối OpenAI API OK"
    except Exception as exc:
        logger.exception("OpenAI status check failed: %s", exc)
        return False, f"OpenAI API lỗi: {exc}"


def _try_ai_status() -> Tuple[bool, str]:
    settings = get_settings()
    provider = str(settings.ai_text_provider or "gemini").strip().lower()
    if provider == "openai":
        return _try_openai_status()
    return _try_gemini_status()


def _parse_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_times(value: str) -> List[str]:
    valid: List[str] = []
    for item in _parse_csv(value):
        try:
            hh, mm = item.split(":", 1)
            h = int(hh)
            m = int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid.append(f"{h:02d}:{m:02d}")
        except Exception:
            continue
    return valid


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _to_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _normalize_http_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _extract_domains(urls: List[str]) -> List[str]:
    domains: List[str] = []
    seen: set[str] = set()
    for raw in urls:
        normalized = _normalize_http_url(raw)
        parsed = urlparse(normalized)
        domain = (parsed.netloc or "").lower().strip()
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def _get_article_id_by_url(db: Database, url: str) -> Optional[int]:
    row = db.conn.execute(
        "SELECT id FROM articles WHERE url = ? LIMIT 1",
        (url,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


def _refresh_article_summary_if_empty(
    db: Database,
    *,
    url: str,
    title: str,
    source: str,
    summary: str,
) -> Optional[int]:
    clean_url = str(url or "").strip()
    clean_summary = str(summary or "").strip()
    if not clean_url or not clean_summary:
        return None

    row = db.conn.execute(
        """
        SELECT id, COALESCE(summary, '') AS summary
        FROM articles
        WHERE url = ?
        LIMIT 1
        """,
        (clean_url,),
    ).fetchone()
    if row is None:
        return None

    existing_summary = str(row["summary"] or "").strip()
    if existing_summary:
        return int(row["id"])

    with db.conn:
        db.conn.execute(
            """
            UPDATE articles
            SET title = COALESCE(NULLIF(?, ''), title),
                source = COALESCE(NULLIF(?, ''), source),
                summary = ?,
                fetched_at = ?
            WHERE id = ?
            """,
            (
                str(title or "").strip(),
                str(source or "").strip(),
                clean_summary,
                datetime.now().isoformat(),
                int(row["id"]),
            ),
        )
    return int(row["id"])


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))

    @app.get("/")
    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/articles")
    @app.post("/api/articles")
    def api_articles():
        settings = get_settings()
        db = _open_db()

        try:
            payload = request.get_json(silent=True) or {}
            fetch_rss_enabled = _to_bool(payload.get("fetch_rss"), default=True)
            fetch_newsapi_enabled = _to_bool(payload.get("fetch_newsapi"), default=True)
            has_custom_urls_filter = "custom_news_urls" in payload
            has_youtube_filter = "youtube_channel_urls" in payload
            requested_custom_urls = _to_str_list(payload.get("custom_news_urls"))
            requested_youtube_urls = _to_str_list(payload.get("youtube_channel_urls"))
            selected_custom_urls = (
                requested_custom_urls if has_custom_urls_filter else list(settings.custom_news_urls)
            )
            selected_youtube_urls = (
                requested_youtube_urls
                if has_youtube_filter
                else list(settings.youtube_channel_urls)
            )

            youtube_urls, article_urls = resolve_source_lists(
                youtube_urls=[u.strip() for u in selected_youtube_urls if str(u).strip()],
                article_urls=[u.strip() for u in selected_custom_urls if str(u).strip()],
            )
            rss_items = fetch_rss(urls=settings.rss_urls, db=db) if fetch_rss_enabled else []
            news_items = (
                fetch_newsapi(
                    keyword=settings.news_keyword,
                    api_key=settings.news_api_key,
                    db=db,
                    language=settings.news_language,
                    page_size=settings.news_page_size,
                )
                if fetch_newsapi_enabled
                else []
            )
            youtube_items = (
                collect_youtube_channels(
                    channel_urls=youtube_urls,
                    transcript_languages=list(settings.youtube_transcript_langs),
                    max_videos_per_channel=max(1, int(settings.youtube_max_videos_per_channel)),
                    db=db,
                )
                if _to_bool(payload.get("fetch_youtube"), default=True)
                else []
            )
            custom_url_items = collect_article_urls(article_urls, db=db) if article_urls else []

            inserted = 0
            refreshed = 0
            fetched_now: List[Dict[str, Any]] = []
            for article in rss_items + news_items + youtube_items + custom_url_items:
                article_id = db.save_article(article)
                if article_id is not None:
                    inserted += 1
                    fetched_now.append(
                        {
                            "id": article_id,
                            "title": str(article.get("title") or ""),
                            "url": str(article.get("url") or ""),
                            "source": str(article.get("source") or "unknown"),
                            "summary": str(article.get("summary") or ""),
                            "published_at": str(
                                article.get("published_at") or datetime.now().isoformat()
                            ),
                            "created_at": str(
                                article.get("published_at") or datetime.now().isoformat()
                            ),
                            "used": False,
                        }
                    )
                    continue

                refreshed_id = _refresh_article_summary_if_empty(
                    db,
                    url=str(article.get("url") or ""),
                    title=str(article.get("title") or ""),
                    source=str(article.get("source") or "unknown"),
                    summary=str(article.get("summary") or ""),
                )
                if refreshed_id is not None:
                    refreshed += 1
                    fetched_now.append(
                        {
                            "id": refreshed_id,
                            "title": str(article.get("title") or ""),
                            "url": str(article.get("url") or ""),
                            "source": str(article.get("source") or "unknown"),
                            "summary": str(article.get("summary") or ""),
                            "published_at": datetime.now().isoformat(),
                            "created_at": datetime.now().isoformat(),
                            "used": False,
                        }
                    )

            data = fetched_now
            if not data:
                fallback_limit = max(1, min(int(payload.get("fallback_limit") or 20), 100))
                where_parts: List[str] = []
                where_params: List[Any] = []

                if fetch_rss_enabled:
                    where_parts.append("source LIKE ?")
                    where_params.append("rss:%")
                if fetch_newsapi_enabled:
                    where_parts.append("source LIKE ?")
                    where_params.append("newsapi:%")
                if _to_bool(payload.get("fetch_youtube"), default=True):
                    where_parts.append("source LIKE ?")
                    where_params.append("youtube:%")
                if article_urls:
                    selected_domains = _extract_domains(article_urls)
                    if selected_domains:
                        domain_filters = " OR ".join(["source = ?"] * len(selected_domains))
                        where_parts.append(f"({domain_filters})")
                        where_params.extend([f"news:{d}" for d in selected_domains])
                    url_filters = " OR ".join(["url = ?"] * len(article_urls))
                    where_parts.append(f"({url_filters})")
                    where_params.extend(article_urls)

                if where_parts:
                    query_with_summary = f"""
                        SELECT id, title, url, source, summary, fetched_at, used
                        FROM articles
                        WHERE ({" OR ".join(where_parts)})
                          AND TRIM(COALESCE(summary, '')) != ''
                        ORDER BY fetched_at DESC
                        LIMIT ?
                    """
                    rows = db.conn.execute(query_with_summary, (*where_params, fallback_limit)).fetchall()
                    if not rows:
                        query_any = f"""
                            SELECT id, title, url, source, summary, fetched_at, used
                            FROM articles
                            WHERE ({" OR ".join(where_parts)})
                            ORDER BY fetched_at DESC
                            LIMIT ?
                        """
                        rows = db.conn.execute(query_any, (*where_params, fallback_limit)).fetchall()
                    data = [_serialize_article(dict(row)) for row in rows]

            logger.info(
                "Articles fetched: rss=%s newsapi=%s youtube=%s custom_urls=%s inserted=%s refreshed=%s returned_now=%s fallback=%s",
                len(rss_items),
                len(news_items),
                len(youtube_items),
                len(custom_url_items),
                inserted,
                refreshed,
                len(data),
                0 if fetched_now else len(data),
            )
            return _success(data)
        except Exception as exc:
            logger.exception("Failed to fetch articles: %s", exc)
            return _error("Không thể thu thập bài viết", 500, str(exc))
        finally:
            db.close()

    @app.post("/api/generate")
    def api_generate():
        payload = request.get_json(silent=True) or {}
        article = payload.get("article")
        tone = str(payload.get("tone") or "hài hước").strip()
        niche = str(payload.get("niche") or "Công nghệ & AI").strip()
        prompt_template = payload.get("prompt_template")

        if not isinstance(article, dict):
            return _error("Thiếu dữ liệu article", 400)

        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        if not title or not url:
            return _error("Article cần có title và url", 400)

        try:
            caption = generate_caption(
                article=article,
                tone=tone,
                niche=niche,
                prompt_template=prompt_template if isinstance(prompt_template, str) else None,
            )
            qc = quality_check(caption)
            return _success({"caption": caption, "quality": qc})
        except Exception as exc:
            logger.exception("Generate caption failed: %s", exc)
            return _error("Không thể tạo caption", 500, str(exc))

    @app.post("/api/prompt/optimize")
    def api_prompt_optimize():
        payload = request.get_json(silent=True) or {}
        article = payload.get("article")
        tone = str(payload.get("tone") or "hài hước").strip()
        niche = str(payload.get("niche") or "Công nghệ & AI").strip()
        current_prompt = str(payload.get("current_prompt") or "").strip()

        if not isinstance(article, dict):
            return _error("Thiếu dữ liệu article", 400)

        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        if not title or not url:
            return _error("Article cần có title và url", 400)

        try:
            optimized_prompt = optimize_prompt_template(
                current_prompt=current_prompt,
                article=article,
                tone=tone,
                niche=niche,
            )
            return _success({"prompt_template": optimized_prompt})
        except Exception as exc:
            logger.exception("Prompt optimize failed: %s", exc)
            return _error("Không thể tối ưu prompt", 500, str(exc))

    @app.get("/api/queue")
    def api_queue():
        db = _open_db()
        try:
            queued_rows = db.get_queue(status="queued", limit=100)
            posted_rows = db.get_queue(status="posted", limit=100)
            failed_rows = db.get_queue(status="failed", limit=100)

            merged_rows = posted_rows + queued_rows + failed_rows
            items = []
            for row in merged_rows:
                raw_status = str(row.get("status") or "queued")
                items.append(
                    {
                        "id": row.get("id"),
                        "title": row.get("title") or "",
                        "caption": row.get("caption") or "",
                        "source": row.get("source") or "unknown",
                        "url": row.get("url") or "",
                        "scheduled_for": row.get("scheduled_time"),
                        "status": _status_for_ui(raw_status),
                        "status_label": _status_label(raw_status),
                        "facebook_post_id": None,
                    }
                )

            posted_today = db.conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM posts
                WHERE status='posted' AND DATE(posted_at)=DATE('now', 'localtime')
                """
            ).fetchone()
            posted_count = int((posted_today["n"] if posted_today else 0) or 0)

            pending_count = len(queued_rows)
            next_item = next(
                (
                    row
                    for row in queued_rows
                    if row.get("scheduled_time")
                ),
                queued_rows[0] if queued_rows else None,
            )

            return _success(
                {
                    "items": items,
                    "today": {
                        "posted_count": posted_count,
                        "pending_count": pending_count,
                        "next_scheduled_for": next_item.get("scheduled_time") if next_item else None,
                    },
                }
            )
        except Exception as exc:
            logger.exception("Failed to load queue: %s", exc)
            return _error("Không thể tải hàng đợi", 500, str(exc))
        finally:
            db.close()

    @app.post("/api/queue/add")
    def api_queue_add():
        payload = request.get_json(silent=True) or {}
        article = payload.get("article") or {}
        caption = str(payload.get("caption") or "").strip()
        scheduled_for = str(payload.get("scheduled_for") or "").strip()

        if not caption:
            return _error("Thiếu caption", 400)

        if not scheduled_for:
            scheduled_for = (datetime.now() + timedelta(minutes=30)).isoformat()

        db = _open_db()
        try:
            article_id = article.get("id")
            if not isinstance(article_id, int):
                source_url = str(article.get("url") or "").strip()
                title = str(article.get("title") or "Bài chưa đặt tiêu đề").strip()

                if not source_url:
                    source_url = f"local://caption/{int(datetime.now().timestamp())}"

                normalized_article = {
                    "title": title,
                    "url": source_url,
                    "source": str(article.get("source") or "manual").strip() or "manual",
                    "summary": str(article.get("summary") or "").strip(),
                    "published_at": str(article.get("published_at") or datetime.now().isoformat()),
                }

                saved_id = db.save_article(normalized_article)
                if saved_id is not None:
                    article_id = saved_id
                else:
                    article_id = _get_article_id_by_url(db, source_url)

                if not isinstance(article_id, int):
                    return _error("Không thể xác định article_id để thêm queue", 500)

            post_id = db.add_to_queue(
                article_id=article_id,
                caption=caption,
                scheduled_time=scheduled_for,
                status="queued",
            )
            if post_id is None:
                return _error("Không thể thêm vào hàng đợi", 500)

            return _success(
                {
                    "id": post_id,
                    "status": "scheduled",
                    "scheduled_for": scheduled_for,
                }
            )
        except Exception as exc:
            logger.exception("Failed to add queue item: %s", exc)
            return _error("Không thể thêm vào hàng đợi", 500, str(exc))
        finally:
            db.close()

    @app.post("/api/post-now")
    def api_post_now():
        payload = request.get_json(silent=True) or {}
        message = str(payload.get("caption") or "").strip()
        image_url = payload.get("image_url")
        queue_post_id = payload.get("post_id")

        if not message:
            return _error("Thiếu caption để đăng", 400)

        settings = get_settings()
        result = post_to_page(
            page_id=settings.page_id,
            token=settings.access_token,
            message=message,
            image_url=str(image_url).strip() if isinstance(image_url, str) and image_url.strip() else None,
        )

        if not result.get("ok"):
            return _error(
                "Đăng Facebook thất bại. Kiểm tra cấu hình và quyền token.",
                500,
                result.get("error") or result,
            )

        fb_post_id = str(result.get("post_id") or "")

        if isinstance(queue_post_id, int):
            db = _open_db()
            try:
                target_post_id: Optional[int] = None
                by_id = db.conn.execute(
                    "SELECT id FROM posts WHERE id = ? LIMIT 1",
                    (queue_post_id,),
                ).fetchone()
                if by_id is not None:
                    target_post_id = int(by_id["id"])
                else:
                    by_article = db.conn.execute(
                        """
                        SELECT id
                        FROM posts
                        WHERE article_id = ? AND status = 'queued'
                        ORDER BY
                            CASE WHEN scheduled_time IS NULL THEN 1 ELSE 0 END,
                            scheduled_time ASC,
                            id ASC
                        LIMIT 1
                        """,
                        (queue_post_id,),
                    ).fetchone()
                    if by_article is not None:
                        target_post_id = int(by_article["id"])

                if target_post_id is None:
                    logger.warning(
                        "Cannot map post_id=%s to queued post row for status update",
                        queue_post_id,
                    )
                    return _success({"facebook_post_id": fb_post_id})

                posted_at = datetime.now().isoformat()
                insights = get_post_insights(post_id=fb_post_id, token=settings.access_token)
                db.update_post_status(
                    post_id=target_post_id,
                    status="posted",
                    posted_at=posted_at,
                    reach=int(insights.get("reach", 0)),
                    engagement=int(insights.get("engagement", 0)),
                )
            finally:
                db.close()

        return _success({"facebook_post_id": fb_post_id})

    @app.get("/api/stats")
    def api_stats():
        db = _open_db()
        try:
            stats = db.get_stats(days=7)
            summary = stats.get("summary", {})
            timeline = stats.get("timeline", [])

            article_total_row = db.conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()
            article_total = int((article_total_row["n"] if article_total_row else 0) or 0)

            by_hour_rows = db.conn.execute(
                """
                SELECT
                    strftime('%H:%M', posted_at) AS hour,
                    COUNT(*) AS total
                FROM posts
                WHERE status='posted' AND posted_at IS NOT NULL
                GROUP BY strftime('%H:%M', posted_at)
                ORDER BY total DESC
                LIMIT 5
                """
            ).fetchall()
            by_hour = [dict(row) for row in by_hour_rows]

            max_hour_total = max([int(row["total"]) for row in by_hour], default=1)
            hourly_perf = [
                {
                    "hour": row["hour"] or "00:00",
                    "count": int(row["total"]),
                    "percent": int(round((int(row["total"]) / max_hour_total) * 100)),
                }
                for row in by_hour
            ]

            top_sources_rows = db.conn.execute(
                """
                SELECT
                    a.source AS source,
                    COUNT(*) AS posted_count
                FROM posts p
                LEFT JOIN articles a ON a.id = p.article_id
                WHERE p.status='posted'
                GROUP BY a.source
                ORDER BY posted_count DESC
                LIMIT 5
                """
            ).fetchall()
            top_sources = [
                {
                    "source": (row["source"] or "unknown"),
                    "posted_count": int(row["posted_count"]),
                }
                for row in top_sources_rows
            ]

            top_posts_rows = db.conn.execute(
                """
                SELECT
                    a.title AS title,
                    a.source AS source,
                    p.posted_at AS posted_at,
                    p.reach AS reach,
                    p.engagement AS engagement
                FROM posts p
                LEFT JOIN articles a ON a.id = p.article_id
                WHERE p.status='posted'
                ORDER BY p.reach DESC, p.engagement DESC, p.posted_at DESC
                LIMIT 5
                """
            ).fetchall()
            top_posts = [
                {
                    "title": row["title"] or "",
                    "source": row["source"] or "unknown",
                    "posted_at": row["posted_at"],
                    "reach": int(row["reach"] or 0),
                    "engagement": int(row["engagement"] or 0),
                }
                for row in top_posts_rows
            ]

            response = {
                "summary": {
                    "posted_total": int(summary.get("posted") or 0),
                    "scheduled_total": int(summary.get("queued") or 0),
                    "failed_total": int(summary.get("failed") or 0),
                    "crawled_total": article_total,
                    "last7days_total": int(sum(int(x.get("count") or 0) for x in timeline)),
                    "reach_total": int(summary.get("reach") or 0),
                    "engagement_total": int(summary.get("engagement") or 0),
                },
                "last7days": [
                    {
                        "day": row.get("day"),
                        "count": int(row.get("count") or 0),
                    }
                    for row in timeline
                ],
                "hourly_performance": hourly_perf,
                "top_posts": top_posts,
                "top_sources": top_sources,
            }

            return _success(response)
        except Exception as exc:
            logger.exception("Failed to fetch stats: %s", exc)
            return _error("Không thể tải thống kê", 500, str(exc))
        finally:
            db.close()

    @app.post("/api/campaign/run")
    def api_campaign_run():
        payload = request.get_json(silent=True) or {}
        limit = payload.get("limit", 4)
        dry_run = payload.get("dry_run", None)

        settings = get_settings()
        if isinstance(dry_run, bool):
            settings = replace(settings, pipeline_dry_run=dry_run)

        try:
            pipeline = CampaignAutomationPipeline(settings=settings)
            result = pipeline.run_once(limit=max(1, int(limit)))
            if not result.get("ok"):
                return _error("Campaign pipeline thất bại", 500, result.get("error"))
            return _success(result)
        except Exception as exc:
            logger.exception("Campaign run failed: %s", exc)
            return _error("Không thể chạy campaign pipeline", 500, str(exc))

    @app.get("/api/campaign/recent")
    def api_campaign_recent():
        db = _open_db()
        try:
            rows = db.get_recent_campaigns(limit=50)
            return _success(rows)
        except Exception as exc:
            logger.exception("Failed to load campaign history: %s", exc)
            return _error("Không thể tải lịch sử campaign", 500, str(exc))
        finally:
            db.close()

    @app.post("/api/config/save")
    def api_config_save():
        payload = request.get_json(silent=True) or {}

        page_id = str(payload.get("page_id") or "").strip()
        access_token = str(payload.get("access_token") or "").strip()
        gemini_key = str(payload.get("gemini_api_key") or payload.get("claude_api_key") or "").strip()
        ai_text_provider = str(payload.get("ai_text_provider") or "").strip().lower()
        ai_text_model = str(payload.get("ai_text_model") or "").strip()

        has_news_api_key_field = "news_api_key" in payload
        news_api_key = str(payload.get("news_api_key") or "").strip()
        news_keyword_value = str(payload.get("news_keyword") or "").strip()
        news_language_value = str(payload.get("news_language") or "").strip()
        news_page_size_value = payload.get("news_page_size")
        wp_base_url = str(payload.get("wp_base_url") or "").strip()
        wp_username = str(payload.get("wp_username") or "").strip()
        wp_app_password = str(payload.get("wp_app_password") or "").strip()
        openai_api_key = str(payload.get("openai_api_key") or "").strip()
        youtube_channels_value = payload.get("youtube_channel_urls")
        custom_news_value = payload.get("custom_news_urls")
        has_youtube_channels_field = "youtube_channel_urls" in payload
        has_custom_news_field = "custom_news_urls" in payload
        has_rss_urls_field = "rss_urls" in payload
        has_news_keyword_field = "news_keyword" in payload
        has_news_language_field = "news_language" in payload
        has_news_page_size_field = "news_page_size" in payload
        has_youtube_max_videos_field = "youtube_max_videos_per_channel" in payload
        has_youtube_transcript_langs_field = "youtube_transcript_langs" in payload

        pipeline_dry_run_value = payload.get("pipeline_dry_run")
        pipeline_auto_fb_value = payload.get("pipeline_auto_post_facebook")
        pipeline_auto_wp_value = payload.get("pipeline_auto_post_wordpress")

        rss_urls_value = payload.get("rss_urls")
        youtube_max_videos_value = payload.get("youtube_max_videos_per_channel")
        youtube_transcript_langs_value = payload.get("youtube_transcript_langs")
        posting_times_value = payload.get("posting_times")

        values: Dict[str, str] = {}
        if page_id:
            values["PAGE_ID"] = page_id
            values["FB_PAGE_ID"] = page_id
        if access_token:
            values["ACCESS_TOKEN"] = access_token
            values["FB_PAGE_ACCESS_TOKEN"] = access_token
        if gemini_key:
            values["GEMINI_API_KEY"] = gemini_key
        if ai_text_provider in {"gemini", "openai"}:
            values["AI_TEXT_PROVIDER"] = ai_text_provider
        if ai_text_model:
            values["OPENAI_TEXT_MODEL"] = ai_text_model
        if has_news_api_key_field:
            values["NEWS_API_KEY"] = news_api_key
            values["NEWSAPI_KEY"] = news_api_key
        if has_news_keyword_field:
            values["NEWS_KEYWORD"] = news_keyword_value
            values["NEWSAPI_QUERY"] = news_keyword_value
        if has_news_language_field:
            values["NEWS_LANGUAGE"] = news_language_value
            values["NEWSAPI_LANGUAGE"] = news_language_value
        if has_news_page_size_field:
            try:
                page_size = max(1, min(int(news_page_size_value), 100))
            except Exception:
                page_size = 10
            values["NEWS_PAGE_SIZE"] = str(page_size)
            values["NEWSAPI_PAGE_SIZE"] = str(page_size)
        if wp_base_url:
            values["WP_BASE_URL"] = wp_base_url
        if wp_username:
            values["WP_USERNAME"] = wp_username
        if wp_app_password:
            values["WP_APP_PASSWORD"] = wp_app_password
        if openai_api_key:
            values["OPENAI_API_KEY"] = openai_api_key

        if isinstance(pipeline_dry_run_value, bool):
            values["PIPELINE_DRY_RUN"] = "true" if pipeline_dry_run_value else "false"
        if isinstance(pipeline_auto_fb_value, bool):
            values["PIPELINE_AUTO_POST_FACEBOOK"] = (
                "true" if pipeline_auto_fb_value else "false"
            )
        if isinstance(pipeline_auto_wp_value, bool):
            values["PIPELINE_AUTO_POST_WORDPRESS"] = (
                "true" if pipeline_auto_wp_value else "false"
            )

        if has_rss_urls_field:
            if isinstance(rss_urls_value, list):
                rss_clean = [str(x).strip() for x in rss_urls_value if str(x).strip()]
                values["RSS_URLS"] = ",".join(rss_clean)
                values["RSS_FEEDS"] = values["RSS_URLS"]
            elif isinstance(rss_urls_value, str):
                values["RSS_URLS"] = ",".join(_parse_csv(rss_urls_value))
                values["RSS_FEEDS"] = values["RSS_URLS"]
            else:
                values["RSS_URLS"] = ""
                values["RSS_FEEDS"] = ""

        if has_youtube_channels_field:
            if isinstance(youtube_channels_value, list):
                yt_clean = [str(x).strip() for x in youtube_channels_value if str(x).strip()]
                values["YOUTUBE_CHANNEL_URLS"] = ",".join(yt_clean)
            elif isinstance(youtube_channels_value, str):
                values["YOUTUBE_CHANNEL_URLS"] = ",".join(_parse_csv(youtube_channels_value))
            else:
                values["YOUTUBE_CHANNEL_URLS"] = ""

        if has_custom_news_field:
            if isinstance(custom_news_value, list):
                news_clean = [str(x).strip() for x in custom_news_value if str(x).strip()]
                values["CUSTOM_NEWS_URLS"] = ",".join(news_clean)
            elif isinstance(custom_news_value, str):
                values["CUSTOM_NEWS_URLS"] = ",".join(_parse_csv(custom_news_value))
            else:
                values["CUSTOM_NEWS_URLS"] = ""

        if has_youtube_max_videos_field:
            try:
                max_videos = max(1, min(int(youtube_max_videos_value), 10))
            except Exception:
                max_videos = 2
            values["YOUTUBE_MAX_VIDEOS_PER_CHANNEL"] = str(max_videos)

        if has_youtube_transcript_langs_field:
            if isinstance(youtube_transcript_langs_value, list):
                langs_clean = [
                    str(x).strip()
                    for x in youtube_transcript_langs_value
                    if str(x).strip()
                ]
                values["YOUTUBE_TRANSCRIPT_LANGS"] = ",".join(langs_clean)
            elif isinstance(youtube_transcript_langs_value, str):
                values["YOUTUBE_TRANSCRIPT_LANGS"] = ",".join(
                    _parse_csv(youtube_transcript_langs_value)
                )
            else:
                values["YOUTUBE_TRANSCRIPT_LANGS"] = ""

        if isinstance(posting_times_value, list):
            times = _parse_times(",".join(str(x).strip() for x in posting_times_value if str(x).strip())
            )
            if times:
                values["POSTING_TIMES"] = ",".join(times)
        elif isinstance(posting_times_value, str) and posting_times_value.strip():
            times = _parse_times(posting_times_value)
            if times:
                values["POSTING_TIMES"] = ",".join(times)

        if not values:
            return _error("Không có dữ liệu cấu hình để lưu", 400)

        try:
            _save_env_values(values)
            for key, value in values.items():
                os.environ[key] = value

            reload_config()
            return _success({"saved_keys": list(values.keys())})
        except Exception as exc:
            logger.exception("Failed to save config: %s", exc)
            return _error("Không thể lưu cấu hình", 500, str(exc))

    @app.get("/api/config/status")
    def api_config_status():
        settings = get_settings()
        fb_ok, fb_message = _try_fb_status()
        gemini_ok, gemini_message = _try_gemini_status()
        openai_ok, openai_message = _try_openai_status()
        ai_ok, ai_message = _try_ai_status()
        effective_youtube_urls, effective_article_urls = resolve_source_lists(
            youtube_urls=list(settings.youtube_channel_urls),
            article_urls=list(settings.custom_news_urls),
        )

        return _success(
            {
                "page_id": settings.page_id,
                "has_page_id": bool(settings.page_id),
                "has_access_token": bool(settings.access_token),
                "has_gemini_key": bool(settings.gemini_api_key),
                "ai_text_provider": str(settings.ai_text_provider or "gemini"),
                "ai_text_model": (
                    str(settings.openai_text_model or "gpt-4o-mini")
                    if str(settings.ai_text_provider or "gemini").lower() == "openai"
                    else str(settings.gemini_model or "gemini-2.5-flash")
                ),
                "has_news_api_key": bool(settings.news_api_key),
                "has_wp_config": bool(
                    settings.wp_base_url and settings.wp_username and settings.wp_app_password
                ),
                "has_openai_image_key": bool(settings.openai_api_key),
                "pipeline_dry_run": bool(settings.pipeline_dry_run),
                "rss_urls": list(settings.rss_urls),
                "news_keyword": settings.news_keyword,
                "news_language": settings.news_language,
                "news_page_size": int(settings.news_page_size),
                "youtube_channel_urls": list(settings.youtube_channel_urls),
                "youtube_max_videos_per_channel": int(settings.youtube_max_videos_per_channel),
                "youtube_transcript_langs": list(settings.youtube_transcript_langs),
                "custom_news_urls": list(settings.custom_news_urls),
                "effective_source_urls": {
                    "youtube_channel_urls": effective_youtube_urls,
                    "article_urls": effective_article_urls,
                },
                "ai": {"ok": ai_ok, "message": ai_message},
                "facebook": {"ok": fb_ok, "message": fb_message},
                "gemini": {"ok": gemini_ok, "message": gemini_message},
                "openai": {"ok": openai_ok, "message": openai_message},
            }
        )

    @app.get("/api/config/options")
    def api_config_options():
        settings = get_settings()
        token = str(request.args.get("access_token") or settings.access_token or "").strip()
        pages, pages_error = _fetch_facebook_pages(token)

        return _success(
            {
                "facebook_pages": pages,
                "facebook_pages_error": pages_error,
                "news_languages": [
                    "ar",
                    "de",
                    "en",
                    "es",
                    "fr",
                    "he",
                    "it",
                    "nl",
                    "no",
                    "pt",
                    "ru",
                    "sv",
                    "ud",
                    "zh",
                ],
                "news_keywords": [
                    "technology",
                    "artificial intelligence",
                    "business",
                    "finance",
                    "startup",
                    "science",
                    "health",
                    "sports",
                    "entertainment",
                    "world",
                ],
                "news_page_sizes": [5, 10, 20, 30, 50, 100],
                "ai_text_providers": ["gemini", "openai"],
                "gemini_models": [
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                    "gemini-1.5-flash",
                ],
                "openai_text_models": [
                    "gpt-4o-mini",
                    "gpt-4.1-mini",
                    "gpt-4.1",
                ],
                "youtube_transcript_languages": [
                    "vi",
                    "en",
                    "es",
                    "fr",
                    "de",
                    "pt",
                    "ja",
                    "ko",
                    "zh-Hans",
                    "zh-Hant",
                ],
            }
        )

    return app


def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
