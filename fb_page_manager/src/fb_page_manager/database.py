"""SQLite data access layer for Facebook Page Manager.

Schema:
- articles(id, title, url, source, summary, fetched_at, used)
- posts(id, article_id, caption, scheduled_time, posted_at, status, reach, engagement)
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from .config import DB_PATH

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin SQLite wrapper with explicit app operations."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS articles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        url TEXT NOT NULL UNIQUE,
                        source TEXT NOT NULL,
                        summary TEXT,
                        fetched_at TEXT NOT NULL,
                        used INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS posts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        article_id INTEGER,
                        caption TEXT NOT NULL,
                        scheduled_time TEXT,
                        posted_at TEXT,
                        status TEXT NOT NULL DEFAULT 'queued',
                        reach INTEGER NOT NULL DEFAULT 0,
                        engagement INTEGER NOT NULL DEFAULT 0,
                        FOREIGN KEY(article_id) REFERENCES articles(id)
                    )
                    """
                )
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS campaigns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        article_id INTEGER,
                        source_url TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        headline TEXT,
                        package_json TEXT,
                        image_path TEXT,
                        wordpress_post_id INTEGER,
                        wordpress_url TEXT,
                        facebook_post_id TEXT,
                        facebook_comment_id TEXT,
                        status TEXT NOT NULL DEFAULT 'generated',
                        created_at TEXT NOT NULL,
                        updated_at TEXT,
                        FOREIGN KEY(article_id) REFERENCES articles(id)
                    )
                    """
                )
                self._migrate_posts_table_if_needed()
        except Exception as exc:
            logger.exception("Failed to initialize database schema: %s", exc)
            raise

    def _migrate_posts_table_if_needed(self) -> None:
        """Migrate legacy `posts` table schema to the current queue/post schema."""
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if not columns:
            return

        required_columns = {
            "article_id",
            "caption",
            "scheduled_time",
            "posted_at",
            "reach",
            "engagement",
        }
        if required_columns.issubset(columns):
            return

        # Legacy schema marker from older versions:
        # posts(source, title, source_url, rewritten_caption, scheduled_for, ...)
        legacy_columns = {"source_url", "rewritten_caption", "scheduled_for"}
        if not legacy_columns.issubset(columns):
            logger.warning("Unknown posts schema detected, skipping migration. columns=%s", columns)
            return

        logger.info("Detected legacy posts schema, starting migration to current schema")
        self.conn.execute("ALTER TABLE posts RENAME TO posts_legacy")
        self.conn.execute(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                caption TEXT NOT NULL,
                scheduled_time TEXT,
                posted_at TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reach INTEGER NOT NULL DEFAULT 0,
                engagement INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(article_id) REFERENCES articles(id)
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO posts (
                id,
                article_id,
                caption,
                scheduled_time,
                posted_at,
                status,
                reach,
                engagement
            )
            SELECT
                p.id,
                (
                    SELECT a.id
                    FROM articles a
                    WHERE a.url = p.source_url
                    LIMIT 1
                ) AS article_id,
                COALESCE(
                    NULLIF(TRIM(p.rewritten_caption), ''),
                    NULLIF(TRIM(p.original_caption), ''),
                    ''
                ) AS caption,
                p.scheduled_for AS scheduled_time,
                CASE
                    WHEN p.status = 'posted' THEN COALESCE(p.updated_at, p.created_at)
                    ELSE NULL
                END AS posted_at,
                CASE
                    WHEN p.status IN ('posted', 'failed', 'queued') THEN p.status
                    WHEN p.status IN ('scheduled', 'crawled', 'generated') THEN 'queued'
                    ELSE 'queued'
                END AS status,
                0 AS reach,
                0 AS engagement
            FROM posts_legacy p
            WHERE COALESCE(
                NULLIF(TRIM(p.rewritten_caption), ''),
                NULLIF(TRIM(p.original_caption), ''),
                ''
            ) != ''
            """
        )
        self.conn.execute("DROP TABLE posts_legacy")
        self.conn.execute(
            """
            UPDATE sqlite_sequence
            SET seq = COALESCE((SELECT MAX(id) FROM posts), 0)
            WHERE name = 'posts'
            """
        )
        logger.info("Legacy posts migration completed")

    def save_article(self, article: Dict[str, Any]) -> Optional[int]:
        """Save article if URL is not duplicate. Returns article id or None."""
        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        source = str(article.get("source") or "unknown").strip()
        summary = str(article.get("summary") or "").strip()

        if not title or not url:
            logger.warning("Skipping invalid article payload: %s", article)
            return None

        if self.is_duplicate(url):
            return None

        try:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT INTO articles (title, url, source, summary, fetched_at, used)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (title, url, source, summary, utc_now_iso()),
                )
            return int(cur.lastrowid)
        except Exception as exc:
            logger.exception("Failed to save article url=%s: %s", url, exc)
            return None

    def is_duplicate(self, url: str) -> bool:
        """Return True if article URL already exists."""
        try:
            row = self.conn.execute(
                "SELECT 1 FROM articles WHERE url = ? LIMIT 1",
                (url.strip(),),
            ).fetchone()
            return row is not None
        except Exception as exc:
            logger.exception("Duplicate check failed for url=%s: %s", url, exc)
            return False

    def add_to_queue(
        self,
        article_id: int,
        caption: str,
        scheduled_time: Optional[str] = None,
        status: str = "queued",
    ) -> Optional[int]:
        """Add generated caption to posting queue."""
        if not caption.strip():
            logger.warning("Empty caption cannot be queued")
            return None

        try:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT INTO posts (article_id, caption, scheduled_time, status, posted_at, reach, engagement)
                    VALUES (?, ?, ?, ?, NULL, 0, 0)
                    """,
                    (article_id, caption.strip(), scheduled_time, status),
                )
                self.conn.execute("UPDATE articles SET used = 1 WHERE id = ?", (article_id,))
            return int(cur.lastrowid)
        except Exception as exc:
            logger.exception("Failed to add post to queue article_id=%s: %s", article_id, exc)
            return None

    def get_queue(self, status: str = "queued", limit: int = 50) -> List[Dict[str, Any]]:
        """Get queue items with article metadata."""
        try:
            rows = self.conn.execute(
                """
                SELECT
                    p.id,
                    p.article_id,
                    p.caption,
                    p.scheduled_time,
                    p.posted_at,
                    p.status,
                    p.reach,
                    p.engagement,
                    a.title,
                    a.url,
                    a.source,
                    a.summary,
                    a.fetched_at
                FROM posts p
                LEFT JOIN articles a ON a.id = p.article_id
                WHERE p.status = ?
                ORDER BY
                    CASE WHEN p.scheduled_time IS NULL THEN 1 ELSE 0 END,
                    p.scheduled_time ASC,
                    p.id ASC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.exception("Failed to get queue status=%s: %s", status, exc)
            return []

    def update_post_status(
        self,
        post_id: int,
        status: str,
        posted_at: Optional[str] = None,
        reach: Optional[int] = None,
        engagement: Optional[int] = None,
    ) -> bool:
        """Update post status and optional metrics."""
        try:
            existing = self.conn.execute(
                "SELECT posted_at, reach, engagement FROM posts WHERE id = ? LIMIT 1",
                (post_id,),
            ).fetchone()
            if existing is None:
                logger.warning("Post id=%s does not exist", post_id)
                return False

            final_posted_at = posted_at if posted_at is not None else existing["posted_at"]
            final_reach = int(reach) if reach is not None else int(existing["reach"] or 0)
            final_engagement = (
                int(engagement) if engagement is not None else int(existing["engagement"] or 0)
            )

            with self.conn:
                self.conn.execute(
                    """
                    UPDATE posts
                    SET status = ?, posted_at = ?, reach = ?, engagement = ?
                    WHERE id = ?
                    """,
                    (status, final_posted_at, final_reach, final_engagement, post_id),
                )
            return True
        except Exception as exc:
            logger.exception("Failed to update post status id=%s: %s", post_id, exc)
            return False

    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """Return aggregated queue/post metrics for the given day window."""
        days = max(1, int(days))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            totals = self.conn.execute(
                """
                SELECT
                    COUNT(*) AS total_posts,
                    SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) AS posted,
                    SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                    COALESCE(SUM(reach), 0) AS reach,
                    COALESCE(SUM(engagement), 0) AS engagement
                FROM posts
                """
            ).fetchone()

            timeline_rows = self.conn.execute(
                """
                SELECT
                    DATE(COALESCE(posted_at, scheduled_time)) AS day,
                    COUNT(*) AS count
                FROM posts
                WHERE COALESCE(posted_at, scheduled_time) IS NOT NULL
                  AND COALESCE(posted_at, scheduled_time) >= ?
                GROUP BY DATE(COALESCE(posted_at, scheduled_time))
                ORDER BY day ASC
                """,
                (since,),
            ).fetchall()

            top_articles = self.conn.execute(
                """
                SELECT a.title, p.reach, p.engagement, p.posted_at
                FROM posts p
                JOIN articles a ON a.id = p.article_id
                WHERE p.status='posted'
                ORDER BY p.reach DESC, p.engagement DESC
                LIMIT 5
                """
            ).fetchall()

            return {
                "summary": {
                    "total_posts": int(totals["total_posts"] or 0),
                    "posted": int(totals["posted"] or 0),
                    "queued": int(totals["queued"] or 0),
                    "failed": int(totals["failed"] or 0),
                    "reach": int(totals["reach"] or 0),
                    "engagement": int(totals["engagement"] or 0),
                },
                "timeline": [dict(r) for r in timeline_rows],
                "top_posts": [dict(r) for r in top_articles],
            }
        except Exception as exc:
            logger.exception("Failed to compute stats: %s", exc)
            return {
                "summary": {
                    "total_posts": 0,
                    "posted": 0,
                    "queued": 0,
                    "failed": 0,
                    "reach": 0,
                    "engagement": 0,
                },
                "timeline": [],
                "top_posts": [],
            }

    def fetch_unused_articles(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return available articles not yet used in queue."""
        try:
            rows = self.conn.execute(
                """
                SELECT * FROM articles
                WHERE used = 0
                ORDER BY fetched_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.exception("Failed to fetch unused articles: %s", exc)
            return []

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception as exc:
            logger.exception("Failed to close DB connection: %s", exc)

    def create_campaign(
        self,
        *,
        article_id: Optional[int],
        source_url: str,
        source_type: str,
        headline: str,
        package_json: str,
        status: str = "generated",
    ) -> Optional[int]:
        try:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT INTO campaigns (
                        article_id,
                        source_url,
                        source_type,
                        headline,
                        package_json,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article_id,
                        source_url,
                        source_type,
                        headline,
                        package_json,
                        status,
                        utc_now_iso(),
                        utc_now_iso(),
                    ),
                )
            return int(cur.lastrowid)
        except Exception as exc:
            logger.exception("Failed to create campaign: %s", exc)
            return None

    def update_campaign_result(
        self,
        campaign_id: int,
        *,
        image_path: Optional[str] = None,
        wordpress_post_id: Optional[int] = None,
        wordpress_url: Optional[str] = None,
        facebook_post_id: Optional[str] = None,
        facebook_comment_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> bool:
        try:
            existing = self.conn.execute(
                """
                SELECT image_path, wordpress_post_id, wordpress_url, facebook_post_id,
                       facebook_comment_id, status
                FROM campaigns
                WHERE id = ?
                LIMIT 1
                """,
                (campaign_id,),
            ).fetchone()
            if existing is None:
                logger.warning("Campaign id=%s not found", campaign_id)
                return False

            final_image_path = image_path if image_path is not None else existing["image_path"]
            final_wp_post_id = (
                int(wordpress_post_id)
                if wordpress_post_id is not None
                else existing["wordpress_post_id"]
            )
            final_wp_url = wordpress_url if wordpress_url is not None else existing["wordpress_url"]
            final_fb_post_id = (
                str(facebook_post_id) if facebook_post_id is not None else existing["facebook_post_id"]
            )
            final_fb_comment_id = (
                str(facebook_comment_id)
                if facebook_comment_id is not None
                else existing["facebook_comment_id"]
            )
            final_status = status if status is not None else existing["status"]

            with self.conn:
                self.conn.execute(
                    """
                    UPDATE campaigns
                    SET image_path = ?,
                        wordpress_post_id = ?,
                        wordpress_url = ?,
                        facebook_post_id = ?,
                        facebook_comment_id = ?,
                        status = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        final_image_path,
                        final_wp_post_id,
                        final_wp_url,
                        final_fb_post_id,
                        final_fb_comment_id,
                        final_status,
                        utc_now_iso(),
                        campaign_id,
                    ),
                )
            return True
        except Exception as exc:
            logger.exception("Failed to update campaign id=%s: %s", campaign_id, exc)
            return False

    def get_recent_campaigns(self, limit: int = 30) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute(
                """
                SELECT *
                FROM campaigns
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.exception("Failed to get recent campaigns: %s", exc)
            return []


@contextmanager
def database_context(db_path: str = DB_PATH) -> Generator[Database, None, None]:
    db = Database(db_path)
    try:
        yield db
    finally:
        db.close()

