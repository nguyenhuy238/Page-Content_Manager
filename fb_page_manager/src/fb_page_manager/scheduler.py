from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from .ai_writer import AIWriter
from .config import Settings
from .crawler import Crawler
from .database import Database
from .fb_poster import FacebookPoster

logger = logging.getLogger(__name__)


class ContentScheduler:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        crawler: Crawler,
        ai_writer: AIWriter,
        fb_poster: FacebookPoster,
    ) -> None:
        self.settings = settings
        self.db = db
        self.crawler = crawler
        self.ai_writer = ai_writer
        self.fb_poster = fb_poster

    def run_cycle(self) -> None:
        logger.info("Starting pipeline cycle")
        now = datetime.now(timezone.utc)

        crawled_items = self.crawler.collect()
        inserted = 0
        for item in crawled_items:
            post_id = self.db.insert_crawled_post(item)
            if post_id is not None:
                inserted += 1
        logger.info("Collected %s items, inserted %s new posts", len(crawled_items), inserted)

        crawled_posts = self.db.get_posts_by_status("crawled", self.settings.max_posts_per_run)
        for post in crawled_posts:
            try:
                rewritten = self.ai_writer.rewrite_caption(
                    title=post["title"],
                    summary=post.get("summary") or "",
                    url=post["source_url"],
                )
                self.db.mark_rewritten(post["id"], rewritten)
            except Exception as exc:
                self.db.mark_failed(post["id"], f"Rewrite failed: {exc}")

        rewritten_posts = self.db.get_posts_by_status("rewritten", self.settings.max_posts_per_run)
        next_time = self._get_initial_slot(now)
        for post in rewritten_posts:
            self.db.mark_scheduled(post["id"], next_time.isoformat())
            next_time = next_time + timedelta(minutes=self.settings.post_interval_minutes)

        due_posts = self.db.get_due_scheduled_posts(now.isoformat(), self.settings.max_posts_per_run)
        for post in due_posts:
            message = self._build_message(post)
            fb_post_id = self.fb_poster.post_to_page(message)
            if fb_post_id:
                self.db.mark_posted(post["id"], fb_post_id)
                logger.info("Posted id=%s to Facebook as %s", post["id"], fb_post_id)
            else:
                self.db.mark_failed(post["id"], "Facebook post failed")

        logger.info("Cycle complete")

    def start(self) -> None:
        self.run_cycle()
        scheduler = BlockingScheduler(timezone=self.settings.timezone)
        scheduler.add_job(
            self.run_cycle,
            trigger="interval",
            minutes=self.settings.post_interval_minutes,
            id="fb_content_pipeline",
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Scheduler started: every %s minutes",
            self.settings.post_interval_minutes,
        )
        scheduler.start()

    def _get_initial_slot(self, now: datetime) -> datetime:
        latest = self.db.get_latest_scheduled_time()
        if latest is None:
            return now
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        if latest > now:
            return latest + timedelta(minutes=self.settings.post_interval_minutes)
        return now

    @staticmethod
    def _build_message(post: dict) -> str:
        caption = post.get("rewritten_caption") or post.get("original_caption") or post["title"]
        if post["source_url"] not in caption:
            return f"{caption}\n\nNguon: {post['source_url']}"
        return caption

