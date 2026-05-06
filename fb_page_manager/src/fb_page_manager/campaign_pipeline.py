"""End-to-end automation pipeline for Mexico-focused story campaigns."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .ai_writer import generate_campaign_package
from .config import get_settings
from .crawler import fetch_newsapi, fetch_rss
from .database import Database
from .fb_poster import comment_on_post, post_to_page
from .image_generator import generate_image
from .source_collector import (
    collect_article_urls,
    collect_youtube_channels,
    resolve_source_lists,
)
from .wordpress_publisher import WordPressPublisher

logger = logging.getLogger(__name__)


def _article_id_by_url(db: Database, url: str) -> Optional[int]:
    row = db.conn.execute(
        "SELECT id FROM articles WHERE url = ? LIMIT 1",
        (url,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


class CampaignAutomationPipeline:
    def __init__(self, settings: Any = None) -> None:
        self.settings = settings or get_settings()

    def _collect_candidates(self, db: Database) -> List[Dict[str, Any]]:
        s = self.settings
        youtube_urls, article_urls = resolve_source_lists(
            youtube_urls=list(getattr(s, "youtube_channel_urls", []) or []),
            article_urls=list(getattr(s, "custom_news_urls", []) or []),
        )

        youtube_items = collect_youtube_channels(
            channel_urls=youtube_urls,
            transcript_languages=list(getattr(s, "youtube_transcript_langs", ["es", "en"])),
            max_videos_per_channel=int(getattr(s, "youtube_max_videos_per_channel", 2)),
            db=db,
        )
        article_items = collect_article_urls(article_urls, db=db)

        rss_items = fetch_rss(urls=list(getattr(s, "rss_urls", []) or []), db=db)
        for item in rss_items:
            item["content_type"] = "rss"
            item["content"] = str(item.get("summary") or "")

        newsapi_items = fetch_newsapi(
            keyword=str(getattr(s, "news_keyword", "celebrity")),
            api_key=str(getattr(s, "news_api_key", "")),
            db=db,
            language=str(getattr(s, "news_language", "es")),
            page_size=int(getattr(s, "news_page_size", 10)),
        )
        for item in newsapi_items:
            item["content_type"] = "newsapi"
            item["content"] = str(item.get("summary") or "")

        combined = youtube_items + article_items + rss_items + newsapi_items
        combined.sort(key=lambda x: len(str(x.get("content") or "")), reverse=True)

        logger.info(
            "Collected candidates: youtube=%s article_urls=%s rss=%s newsapi=%s total=%s",
            len(youtube_items),
            len(article_items),
            len(rss_items),
            len(newsapi_items),
            len(combined),
        )
        return combined

    def run_once(self, limit: Optional[int] = None) -> Dict[str, Any]:
        s = self.settings
        batch_limit = max(1, int(limit or getattr(s, "pipeline_batch_size", 4)))

        db = Database(getattr(s, "db_path", "data/fb_page_manager.db"))
        wp = WordPressPublisher(
            base_url=str(getattr(s, "wp_base_url", "")),
            username=str(getattr(s, "wp_username", "")),
            app_password=str(getattr(s, "wp_app_password", "")),
        )

        dry_run = bool(getattr(s, "pipeline_dry_run", True))
        auto_wp = bool(getattr(s, "pipeline_auto_post_wordpress", False))
        auto_fb = bool(getattr(s, "pipeline_auto_post_facebook", False))
        auto_comment = bool(getattr(s, "pipeline_auto_comment_on_facebook", True))

        results: List[Dict[str, Any]] = []
        counters = {
            "candidates": 0,
            "generated": 0,
            "image_created": 0,
            "wp_posted": 0,
            "fb_posted": 0,
            "comments_posted": 0,
            "failed": 0,
        }

        try:
            candidates = self._collect_candidates(db)
            counters["candidates"] = len(candidates)
            for story in candidates[:batch_limit]:
                story_url = str(story.get("url") or "").strip()
                story_type = str(story.get("content_type") or "unknown").strip()
                story_title = str(story.get("title") or "").strip()
                article_id = db.save_article(story)
                if article_id is None:
                    article_id = _article_id_by_url(db, story_url)

                package = generate_campaign_package(
                    story,
                    target_language=str(getattr(s, "target_language", "es-MX")),
                    target_country=str(getattr(s, "target_country", "Mexico")),
                    target_audience=str(
                        getattr(s, "target_audience", "Audiencia mexicana interesada en celebridades")
                    ),
                )
                if not package:
                    counters["failed"] += 1
                    continue

                counters["generated"] += 1
                campaign_id = db.create_campaign(
                    article_id=article_id,
                    source_url=story_url,
                    source_type=story_type,
                    headline=str(package.get("headline") or story_title),
                    package_json=json.dumps(package, ensure_ascii=False),
                    status="generated",
                )

                item_result: Dict[str, Any] = {
                    "campaign_id": campaign_id,
                    "source_url": story_url,
                    "source_title": story_title,
                    "status": "generated",
                    "wordpress_url": None,
                    "facebook_post_id": None,
                    "facebook_comment_id": None,
                    "image_path": None,
                }

                image_path: Optional[str] = None
                image_public_url: Optional[str] = None

                if not dry_run and str(getattr(s, "openai_api_key", "")).strip():
                    image_prompt = str(package.get("image_prompt") or "").strip()
                    if image_prompt:
                        image_response = generate_image(
                            prompt=image_prompt,
                            api_key=str(getattr(s, "openai_api_key", "")),
                            model=str(getattr(s, "openai_image_model", "gpt-image-1")),
                            size=str(getattr(s, "openai_image_size", "1024x1536")),
                            output_dir=str(getattr(s, "generated_image_dir", "data/generated_images")),
                            file_stem=str(package.get("article_title") or story_title),
                        )
                        if image_response.get("ok"):
                            image_path = str(image_response.get("path") or "")
                            item_result["image_path"] = image_path
                            counters["image_created"] += 1
                            if campaign_id:
                                db.update_campaign_result(
                                    campaign_id,
                                    image_path=image_path,
                                    status="image_created",
                                )

                if not dry_run and auto_wp and wp.is_configured:
                    featured_media_id: Optional[int] = None
                    if image_path:
                        upload = wp.upload_media(
                            file_path=image_path,
                            alt_text=str(package.get("article_title") or story_title),
                        )
                        if upload.get("ok"):
                            media_id = upload.get("media_id")
                            if isinstance(media_id, int):
                                featured_media_id = media_id
                            source_url = upload.get("source_url")
                            if isinstance(source_url, str) and source_url.strip():
                                image_public_url = source_url.strip()

                    wp_result = wp.create_post(
                        title=str(package.get("article_title") or story_title),
                        content_html=str(package.get("article_html") or ""),
                        excerpt=str(package.get("article_excerpt") or ""),
                        status=str(getattr(s, "wp_post_status", "draft")),
                        category_id=int(getattr(s, "wp_default_category_id", 0)),
                        author_id=int(getattr(s, "wp_default_author_id", 0)),
                        featured_media_id=featured_media_id,
                        tags=[str(x).strip() for x in package.get("tags", []) if str(x).strip()],
                    )
                    if wp_result.get("ok"):
                        counters["wp_posted"] += 1
                        item_result["wordpress_url"] = wp_result.get("post_url")
                        item_result["status"] = "wp_posted"
                        if campaign_id:
                            db.update_campaign_result(
                                campaign_id,
                                wordpress_post_id=wp_result.get("post_id"),
                                wordpress_url=wp_result.get("post_url"),
                                status="wp_posted",
                            )

                if not dry_run and auto_fb:
                    fb_message = str(package.get("facebook_hook") or "").strip()
                    cta = str(package.get("facebook_cta") or "").strip()
                    if cta:
                        fb_message = f"{fb_message}\n\n{cta}".strip()

                    fb_result = post_to_page(
                        page_id=str(getattr(s, "page_id", "")),
                        token=str(getattr(s, "access_token", "")),
                        message=fb_message,
                        image_url=image_public_url,
                    )
                    if fb_result.get("ok"):
                        counters["fb_posted"] += 1
                        fb_post_id = str(fb_result.get("post_id") or "")
                        item_result["facebook_post_id"] = fb_post_id
                        item_result["status"] = "fb_posted"
                        if campaign_id:
                            db.update_campaign_result(
                                campaign_id,
                                facebook_post_id=fb_post_id,
                                status="fb_posted",
                            )

                        wp_url = item_result.get("wordpress_url")
                        if auto_comment and wp_url and fb_post_id:
                            template = str(
                                getattr(
                                    s,
                                    "fb_comment_template",
                                    "Lee la historia completa aqui: {url}",
                                )
                            )
                            comment_text = template.format(url=wp_url)
                            cm = comment_on_post(
                                post_id=fb_post_id,
                                token=str(getattr(s, "access_token", "")),
                                message=comment_text,
                            )
                            if cm.get("ok"):
                                counters["comments_posted"] += 1
                                item_result["facebook_comment_id"] = cm.get("comment_id")
                                item_result["status"] = "completed"
                                if campaign_id:
                                    db.update_campaign_result(
                                        campaign_id,
                                        facebook_comment_id=cm.get("comment_id"),
                                        status="completed",
                                    )

                if dry_run:
                    item_result["status"] = "dry_run_preview"
                    if campaign_id:
                        db.update_campaign_result(campaign_id, status="dry_run_preview")

                results.append(item_result)

            return {
                "ok": True,
                "dry_run": dry_run,
                "auto_post_wordpress": auto_wp,
                "auto_post_facebook": auto_fb,
                "summary": counters,
                "items": results,
            }
        except Exception as exc:
            logger.exception("Campaign pipeline failed: %s", exc)
            return {"ok": False, "error": str(exc), "summary": counters, "items": results}
        finally:
            db.close()
