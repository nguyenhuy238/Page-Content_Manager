"""Posting scheduler based on `schedule` package.

Flow per slot:
1. Pull next queued post from SQLite
2. Post to Facebook Page
3. Update status + insights in DB
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import schedule

from .config import ACCESS_TOKEN, PAGE_ID, POSTING_TIMES, get_settings
from .database import Database
from .fb_poster import get_post_insights, post_to_page

logger = logging.getLogger(__name__)


def _pick_next_queued(db: Database) -> Optional[dict]:
    try:
        queue_items = db.get_queue(status="queued", limit=100)
        if not queue_items:
            return None

        now = datetime.now(timezone.utc)

        due_items = []
        fallback_items = []
        for item in queue_items:
            scheduled_time = item.get("scheduled_time")
            if not scheduled_time:
                fallback_items.append(item)
                continue
            try:
                dt = datetime.fromisoformat(str(scheduled_time))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= now:
                    due_items.append(item)
                else:
                    fallback_items.append(item)
            except Exception:
                fallback_items.append(item)

        if due_items:
            due_items.sort(key=lambda x: x.get("scheduled_time") or "")
            return due_items[0]

        fallback_items.sort(key=lambda x: x.get("scheduled_time") or "")
        return fallback_items[0] if fallback_items else None
    except Exception as exc:
        logger.exception("Failed to pick queued post: %s", exc)
        return None


def process_posting_slot(db_path: Optional[str] = None) -> None:
    """Process one posting slot job."""
    settings = get_settings()
    db = Database(db_path or settings.db_path)

    try:
        next_item = _pick_next_queued(db)
        if not next_item:
            logger.info("No queued post found for this slot")
            return

        post_id = int(next_item["id"])
        caption = str(next_item.get("caption") or "").strip()
        if not caption:
            logger.warning("Queued post id=%s has empty caption", post_id)
            db.update_post_status(post_id=post_id, status="failed")
            return

        result = post_to_page(
            page_id=PAGE_ID,
            token=ACCESS_TOKEN,
            message=caption,
            image_url=None,
        )

        if not result.get("ok"):
            logger.error("Posting failed for post id=%s: %s", post_id, result)
            db.update_post_status(post_id=post_id, status="failed")
            return

        posted_at = datetime.now(timezone.utc).isoformat()
        db.update_post_status(post_id=post_id, status="posted", posted_at=posted_at)

        fb_post_id = str(result.get("post_id") or "")
        if fb_post_id:
            insights = get_post_insights(post_id=fb_post_id, token=ACCESS_TOKEN)
            db.update_post_status(
                post_id=post_id,
                status="posted",
                posted_at=posted_at,
                reach=int(insights.get("reach", 0)),
                engagement=int(insights.get("engagement", 0)),
            )

        logger.info("Posted queue item id=%s successfully", post_id)
    except Exception as exc:
        logger.exception("process_posting_slot failed: %s", exc)
    finally:
        db.close()


def run_scheduler(db_path: Optional[str] = None) -> None:
    """Register daily posting jobs and run forever."""
    times = POSTING_TIMES or ["07:00", "11:30", "20:00"]

    for slot in times:
        try:
            schedule.every().day.at(slot).do(process_posting_slot, db_path=db_path)
            logger.info("Registered posting slot: %s", slot)
        except Exception as exc:
            logger.exception("Failed to register slot %s: %s", slot, exc)

    logger.info("Scheduler loop started with slots: %s", ", ".join(times))
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")
            break
        except Exception as exc:
            logger.exception("Scheduler loop error: %s", exc)
            time.sleep(60)


class ContentScheduler:
    """Compatibility class wrapper around the function-based scheduler."""

    def __init__(
        self,
        settings: Any = None,
        db: Any = None,
        crawler: Any = None,
        ai_writer: Any = None,
        fb_poster: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db
        self.crawler = crawler
        self.ai_writer = ai_writer
        self.fb_poster = fb_poster

    def run_cycle(self) -> None:
        process_posting_slot(db_path=getattr(self.settings, "db_path", None))

    def start(self) -> None:
        run_scheduler(db_path=getattr(self.settings, "db_path", None))

