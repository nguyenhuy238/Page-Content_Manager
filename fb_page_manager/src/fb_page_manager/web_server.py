from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from anthropic import Anthropic
from flask import Flask, jsonify, render_template, request
import requests

from .ai_writer import AIWriter
from .config import get_settings
from .crawler import Crawler
from .database import Database
from .fb_poster import FacebookPoster

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
    settings = get_settings()
    return Database(settings.db_path)


def _ensure_env_file() -> None:
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")


def _save_env_values(values: Dict[str, str]) -> None:
    _ensure_env_file()
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    updated = dict(values)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in updated:
            lines[idx] = f"{key}={updated[key]}"
            del updated[key]

    for key, value in updated.items():
        lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _try_fb_status() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.fb_page_id or not settings.fb_page_access_token:
        return False, "Thiếu FB_PAGE_ID hoặc FB_PAGE_ACCESS_TOKEN"

    url = f"https://graph.facebook.com/{settings.fb_api_version}/{settings.fb_page_id}"
    params = {
        "fields": "id,name",
        "access_token": settings.fb_page_access_token,
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


def _try_claude_status() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.claude_api_key:
        return False, "Thiếu CLAUDE_API_KEY"

    try:
        client = Anthropic(api_key=settings.claude_api_key)
        models_page = client.models.list(limit=1)
        has_data = bool(getattr(models_page, "data", []))
        if not has_data:
            return False, "Claude API trả danh sách model rỗng"
        return True, "Kết nối Claude API OK"
    except Exception as exc:
        logger.exception("Claude status check failed: %s", exc)
        return False, f"Claude API lỗi: {exc}"


def _serialize_article(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "source": row.get("source") or "unknown",
        "title": row.get("title") or "",
        "summary": row.get("summary") or "",
        "url": row.get("source_url") or row.get("url") or "",
        "published_at": row.get("published_at"),
        "created_at": row.get("created_at"),
    }


def _queue_status_label(status: str) -> str:
    mapping = {
        "posted": "Đã đăng",
        "scheduled": "Đã lên lịch",
        "failed": "Thất bại",
        "rewritten": "Đang chờ",
        "crawled": "Mới thu thập",
    }
    return mapping.get(status, status)


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))

    @app.get("/")
    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/articles")
    def api_articles():
        settings = get_settings()
        crawler = Crawler(settings)

        try:
            collected = crawler.collect()
            inserted_rows: List[Dict[str, Any]] = []

            db = _open_db()
            try:
                for item in collected:
                    post_id = db.insert_crawled_post(item)
                    if post_id is None:
                        cur = db.conn.execute(
                            "SELECT * FROM posts WHERE source_url = ? LIMIT 1",
                            (item["url"],),
                        )
                        row = cur.fetchone()
                        if row:
                            inserted_rows.append(dict(row))
                        continue

                    cur = db.conn.execute(
                        "SELECT * FROM posts WHERE id = ? LIMIT 1",
                        (post_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        inserted_rows.append(dict(row))
            finally:
                db.close()

            inserted_rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
            return _success([_serialize_article(row) for row in inserted_rows])
        except Exception as exc:
            logger.exception("Failed to fetch articles: %s", exc)
            return _error("Không thể thu thập bài viết", 500, str(exc))

    @app.post("/api/generate")
    def api_generate():
        payload = request.get_json(silent=True) or {}
        article = payload.get("article")
        tone = str(payload.get("tone") or "hài hước").strip()
        niche = str(payload.get("niche") or "Công nghệ & AI").strip()

        if not isinstance(article, dict):
            return _error("Thiếu dữ liệu article", 400)

        title = str(article.get("title") or "").strip()
        summary = str(article.get("summary") or "").strip()
        url = str(article.get("url") or "").strip()

        if not title or not url:
            return _error("Article cần có title và url", 400)

        settings = get_settings()
        writer = AIWriter(settings)

        # Keep AIWriter as requested while injecting dashboard context into summary.
        summary_with_context = (
            f"{summary}\n\n"
            f"Bối cảnh trang: {niche}. Giọng văn mong muốn: {tone}."
        ).strip()

        caption = writer.rewrite_caption(title=title, summary=summary_with_context, url=url)

        article_id = article.get("id")
        if isinstance(article_id, int):
            db = _open_db()
            try:
                db.mark_rewritten(article_id, caption)
            except Exception as exc:
                logger.exception("Failed to mark rewritten post id=%s: %s", article_id, exc)
            finally:
                db.close()

        return _success({"caption": caption})

    @app.get("/api/queue")
    def api_queue():
        db = _open_db()
        try:
            cur = db.conn.execute(
                """
                SELECT id, title, rewritten_caption, original_caption, source, source_url,
                       scheduled_for, status, facebook_post_id, updated_at
                FROM posts
                WHERE status IN ('scheduled', 'posted', 'failed', 'rewritten')
                ORDER BY
                    CASE WHEN status = 'posted' THEN 0 ELSE 1 END,
                    COALESCE(scheduled_for, updated_at) ASC
                LIMIT 100
                """
            )
            rows = [dict(row) for row in cur.fetchall()]

            queue_items = []
            for row in rows:
                queue_items.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "caption": row.get("rewritten_caption") or row.get("original_caption") or row["title"],
                        "source": row.get("source") or "unknown",
                        "url": row.get("source_url") or "",
                        "scheduled_for": row.get("scheduled_for"),
                        "status": row.get("status"),
                        "status_label": _queue_status_label(str(row.get("status") or "")),
                        "facebook_post_id": row.get("facebook_post_id"),
                    }
                )

            posted_today = db.conn.execute(
                """
                SELECT COUNT(*) AS n FROM posts
                WHERE status='posted' AND DATE(updated_at)=DATE('now', 'localtime')
                """
            ).fetchone()["n"]

            pending = sum(1 for row in queue_items if row["status"] in {"scheduled", "rewritten"})
            next_item = next((row for row in queue_items if row["status"] == "scheduled"), None)

            return _success(
                {
                    "items": queue_items,
                    "today": {
                        "posted_count": int(posted_today or 0),
                        "pending_count": int(pending),
                        "next_scheduled_for": next_item["scheduled_for"] if next_item else None,
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

            if isinstance(article_id, int):
                post_id = article_id
            else:
                source_url = str(article.get("url") or "").strip()
                title = str(article.get("title") or "Bài chưa đặt tiêu đề").strip()
                if not source_url:
                    source_url = f"local://caption/{int(datetime.now().timestamp())}"

                insert_item = {
                    "source": str(article.get("source") or "manual").strip() or "manual",
                    "title": title,
                    "summary": str(article.get("summary") or "").strip(),
                    "url": source_url,
                    "published_at": str(article.get("published_at") or datetime.now().isoformat()),
                }
                inserted = db.insert_crawled_post(insert_item)
                if inserted is None:
                    row = db.conn.execute(
                        "SELECT id FROM posts WHERE source_url = ? LIMIT 1",
                        (source_url,),
                    ).fetchone()
                    if row is None:
                        return _error("Không thể thêm bài vào cơ sở dữ liệu", 500)
                    post_id = int(row["id"])
                else:
                    post_id = inserted

            db.mark_rewritten(post_id, caption)
            db.mark_scheduled(post_id, scheduled_for)

            row = db.conn.execute(
                "SELECT * FROM posts WHERE id = ? LIMIT 1",
                (post_id,),
            ).fetchone()
            data = dict(row) if row else {"id": post_id}
            return _success(
                {
                    "id": data.get("id", post_id),
                    "status": data.get("status", "scheduled"),
                    "scheduled_for": data.get("scheduled_for", scheduled_for),
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
        post_id = payload.get("post_id")

        if not message:
            return _error("Thiếu caption để đăng", 400)

        settings = get_settings()
        poster = FacebookPoster(settings)
        fb_post_id = poster.post_to_page(message)
        if not fb_post_id:
            return _error("Đăng Facebook thất bại. Kiểm tra cấu hình và quyền token.", 500)

        if isinstance(post_id, int):
            db = _open_db()
            try:
                db.mark_posted(post_id, fb_post_id)
            finally:
                db.close()

        return _success({"facebook_post_id": fb_post_id})

    @app.get("/api/stats")
    def api_stats():
        db = _open_db()
        try:
            totals_row = db.conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) AS posted_total,
                    SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END) AS scheduled_total,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_total,
                    SUM(CASE WHEN status='crawled' THEN 1 ELSE 0 END) AS crawled_total
                FROM posts
                """
            ).fetchone()
            totals = dict(totals_row) if totals_row else {}

            last7 = [
                dict(row)
                for row in db.conn.execute(
                """
                SELECT
                    DATE(COALESCE(scheduled_for, updated_at)) AS day,
                    COUNT(*) AS total
                FROM posts
                WHERE status='posted'
                  AND DATE(COALESCE(scheduled_for, updated_at)) >= DATE('now','-6 day')
                GROUP BY day
                ORDER BY day ASC
                """
                ).fetchall()
            ]

            by_hour = [
                dict(row)
                for row in db.conn.execute(
                """
                SELECT
                    strftime('%H:%M', COALESCE(scheduled_for, updated_at)) AS hour,
                    COUNT(*) AS total
                FROM posts
                WHERE status='posted'
                GROUP BY hour
                ORDER BY total DESC
                LIMIT 5
                """
                ).fetchall()
            ]

            top_posts = [
                dict(row)
                for row in db.conn.execute(
                """
                SELECT title, source, COALESCE(scheduled_for, updated_at) AS posted_at
                FROM posts
                WHERE status='posted'
                ORDER BY COALESCE(scheduled_for, updated_at) DESC
                LIMIT 5
                """
                ).fetchall()
            ]

            top_sources = [
                dict(row)
                for row in db.conn.execute(
                """
                SELECT source, COUNT(*) AS posted_count
                FROM posts
                WHERE status='posted'
                GROUP BY source
                ORDER BY posted_count DESC
                LIMIT 5
                """
                ).fetchall()
            ]

            max_hour_total = max([int(row["total"]) for row in by_hour], default=1)
            hour_perf = [
                {
                    "hour": row["hour"] or "00:00",
                    "count": int(row["total"]),
                    "percent": int(round((int(row["total"]) / max_hour_total) * 100)),
                }
                for row in by_hour
            ]

            return _success(
                {
                    "summary": {
                        "posted_total": int(totals.get("posted_total") or 0),
                        "scheduled_total": int(totals.get("scheduled_total") or 0),
                        "failed_total": int(totals.get("failed_total") or 0),
                        "crawled_total": int(totals.get("crawled_total") or 0),
                        "last7days_total": int(sum(int(row["total"]) for row in last7)),
                    },
                    "last7days": [
                        {"day": row["day"], "count": int(row["total"])} for row in last7
                    ],
                    "hourly_performance": hour_perf,
                    "top_posts": [
                        {
                            "title": row["title"],
                            "source": row.get("source") or "unknown",
                            "posted_at": row.get("posted_at"),
                        }
                        for row in top_posts
                    ],
                    "top_sources": [
                        {
                            "source": row.get("source") or "unknown",
                            "posted_count": int(row["posted_count"]),
                        }
                        for row in top_sources
                    ],
                }
            )
        except Exception as exc:
            logger.exception("Failed to fetch stats: %s", exc)
            return _error("Không thể tải thống kê", 500, str(exc))
        finally:
            db.close()

    @app.post("/api/config/save")
    def api_config_save():
        payload = request.get_json(silent=True) or {}
        page_id = str(payload.get("page_id") or "").strip()
        access_token = str(payload.get("access_token") or "").strip()
        claude_key = str(payload.get("claude_api_key") or "").strip()

        if not page_id and not access_token and not claude_key:
            return _error("Không có dữ liệu cấu hình để lưu", 400)

        values: Dict[str, str] = {}
        if page_id:
            values["FB_PAGE_ID"] = page_id
        if access_token:
            values["FB_PAGE_ACCESS_TOKEN"] = access_token
        if claude_key:
            values["CLAUDE_API_KEY"] = claude_key

        try:
            _save_env_values(values)
            for key, value in values.items():
                os.environ[key] = value
            get_settings.cache_clear()
            _ = get_settings()
            return _success({"saved_keys": list(values.keys())})
        except Exception as exc:
            logger.exception("Failed to save config: %s", exc)
            return _error("Không thể lưu cấu hình", 500, str(exc))

    @app.get("/api/config/status")
    def api_config_status():
        settings = get_settings()
        fb_ok, fb_message = _try_fb_status()
        claude_ok, claude_message = _try_claude_status()

        return _success(
            {
                "has_page_id": bool(settings.fb_page_id),
                "has_access_token": bool(settings.fb_page_access_token),
                "has_claude_key": bool(settings.claude_api_key),
                "facebook": {"ok": fb_ok, "message": fb_message},
                "claude": {"ok": claude_ok, "message": claude_message},
            }
        )

    return app


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
