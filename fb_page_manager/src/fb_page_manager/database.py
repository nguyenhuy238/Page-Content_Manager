from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    source_url TEXT NOT NULL UNIQUE,
                    published_at TEXT,
                    original_caption TEXT,
                    rewritten_caption TEXT,
                    status TEXT NOT NULL DEFAULT 'crawled',
                    scheduled_for TEXT,
                    facebook_post_id TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def insert_crawled_post(self, item: Dict[str, Any]) -> Optional[int]:
        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO posts (
                    source, title, summary, source_url, published_at,
                    original_caption, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'crawled', ?, ?)
                """,
                (
                    item["source"],
                    item["title"],
                    item.get("summary"),
                    item["url"],
                    item.get("published_at"),
                    item.get("original_caption") or item["title"],
                    now,
                    now,
                ),
            )
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)

    def get_posts_by_status(self, status: str, limit: int) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT * FROM posts
            WHERE status = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (status, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    def mark_rewritten(self, post_id: int, rewritten_caption: str) -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE posts
                SET rewritten_caption = ?, status = 'rewritten', updated_at = ?
                WHERE id = ?
                """,
                (rewritten_caption, now, post_id),
            )

    def mark_scheduled(self, post_id: int, scheduled_for: str) -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE posts
                SET scheduled_for = ?, status = 'scheduled', updated_at = ?
                WHERE id = ?
                """,
                (scheduled_for, now, post_id),
            )

    def get_latest_scheduled_time(self) -> Optional[datetime]:
        cur = self.conn.execute(
            """
            SELECT scheduled_for FROM posts
            WHERE status IN ('scheduled', 'posted') AND scheduled_for IS NOT NULL
            ORDER BY scheduled_for DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["scheduled_for"])

    def get_due_scheduled_posts(self, now_iso: str, limit: int) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT * FROM posts
            WHERE status = 'scheduled' AND scheduled_for <= ?
            ORDER BY scheduled_for ASC
            LIMIT ?
            """,
            (now_iso, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    def mark_posted(self, post_id: int, facebook_post_id: str) -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE posts
                SET facebook_post_id = ?, status = 'posted', updated_at = ?
                WHERE id = ?
                """,
                (facebook_post_id, now, post_id),
            )

    def mark_failed(self, post_id: int, error_message: str) -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE posts
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_message[:1000], now, post_id),
            )

    def close(self) -> None:
        self.conn.close()

