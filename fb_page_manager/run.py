from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Tuple

import requests
import google.generativeai as genai

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from fb_page_manager.config import get_settings
from fb_page_manager.campaign_pipeline import CampaignAutomationPipeline
from fb_page_manager.crawler import Crawler, fetch_newsapi, fetch_rss
from fb_page_manager.source_collector import collect_article_urls, resolve_source_lists
from fb_page_manager.scheduler import run_scheduler
from fb_page_manager.web_server import create_app


def setup_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def str_to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def run_web() -> int:
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = str_to_bool(os.getenv("FLASK_DEBUG", "false"))

    app = create_app()
    app.run(host="0.0.0.0", port=port, debug=debug)
    return 0


def run_fetch_once() -> int:
    logger = logging.getLogger("run.fetch")
    settings = get_settings()
    crawler = Crawler(settings)

    try:
        items = crawler.collect()
        logger.info("Fetch completed. New articles saved: %s", len(items))
        return 0
    except Exception as exc:
        logger.exception("Fetch failed: %s", exc)
        return 1


def _test_gemini() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.gemini_api_key:
        return False, "GEMINI_API_KEY đang trống"

    try:
        genai.configure(api_key=settings.gemini_api_key)
        models = list(genai.list_models())
        if models:
            return True, "Gemini API: OK"
        return False, "Gemini API: phản hồi rỗng"
    except Exception as exc:
        return False, f"Gemini API lỗi: {exc}"


def _test_openai() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.openai_api_key:
        return False, "OPENAI_API_KEY đang trống"

    try:
        resp = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            message = data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return False, f"OpenAI API lỗi: {message}"
        models = data.get("data") if isinstance(data, dict) else []
        if not isinstance(models, list) or not models:
            return False, "OpenAI API: danh sách model rỗng"
        return True, "OpenAI API: OK"
    except Exception as exc:
        return False, f"OpenAI API lỗi: {exc}"


def _test_facebook() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.page_id or not settings.access_token:
        return False, "Thiếu PAGE_ID hoặc ACCESS_TOKEN"

    url = f"https://graph.facebook.com/v19.0/{settings.page_id}"
    try:
        resp = requests.get(
            url,
            params={"fields": "id,name", "access_token": settings.access_token},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            message = data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return False, f"Facebook API lỗi: {message}"
        return True, f"Facebook API: OK ({data.get('name', 'Unknown Page')})"
    except Exception as exc:
        return False, f"Facebook API lỗi: {exc}"


def _test_news_sources() -> Tuple[bool, str]:
    settings = get_settings()

    try:
        _, article_urls = resolve_source_lists(
            youtube_urls=[],
            article_urls=list(settings.custom_news_urls),
        )
        rss_items = fetch_rss(settings.rss_urls)
        custom_url_items = collect_article_urls(article_urls)
        parts = [f"RSS={len(rss_items)} bài", f"URL={len(custom_url_items)} bài"]

        if settings.news_api_key:
            news_items = fetch_newsapi(
                keyword=settings.news_keyword,
                api_key=settings.news_api_key,
                language=settings.news_language,
                page_size=1,
            )
            parts.append(f"NewsAPI={len(news_items)} bài")
        else:
            parts.append("NEWS_API_KEY trống (bỏ qua NewsAPI)")

        if not settings.rss_urls and not settings.custom_news_urls and not settings.news_api_key:
            return False, "Chưa cấu hình nguồn RSS, URL hoặc NewsAPI"

        return True, "Nguồn tin: " + " | ".join(parts)
    except Exception as exc:
        return False, f"Nguồn tin lỗi: {exc}"


def run_test_connections() -> int:
    logger = logging.getLogger("run.test")

    checks = [
        ("Facebook", _test_facebook),
        ("Gemini", _test_gemini),
        ("OpenAI", _test_openai),
        ("Sources", _test_news_sources),
    ]

    has_error = False
    for name, fn in checks:
        ok, msg = fn()
        if ok:
            logger.info("[%s] %s", name, msg)
        else:
            has_error = True
            logger.error("[%s] %s", name, msg)

    return 1 if has_error else 0


def run_campaign(limit: int, live: bool) -> int:
    logger = logging.getLogger("run.campaign")
    settings = get_settings()
    if live and settings.pipeline_dry_run:
        settings = replace(settings, pipeline_dry_run=False)

    pipeline = CampaignAutomationPipeline(settings=settings)
    result = pipeline.run_once(limit=limit)
    if not result.get("ok"):
        logger.error("Campaign pipeline failed: %s", result.get("error"))
        return 1

    summary = result.get("summary", {})
    logger.info(
        "Campaign pipeline done | dry_run=%s | candidates=%s generated=%s wp=%s fb=%s comments=%s failed=%s",
        result.get("dry_run"),
        summary.get("candidates", 0),
        summary.get("generated", 0),
        summary.get("wp_posted", 0),
        summary.get("fb_posted", 0),
        summary.get("comments_posted", 0),
        summary.get("failed", 0),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FB Page Manager runner")
    parser.add_argument(
        "mode",
        choices=["web", "scheduler", "fetch", "test", "campaign"],
        help="Mode: web | scheduler | fetch | test | campaign",
    )
    parser.add_argument("--limit", type=int, default=4, help="Max stories to process in campaign mode")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force campaign mode to run live even when PIPELINE_DRY_RUN=true",
    )
    return parser


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()

    if args.mode == "web":
        return run_web()
    if args.mode == "scheduler":
        run_scheduler()
        return 0
    if args.mode == "fetch":
        return run_fetch_once()
    if args.mode == "test":
        return run_test_connections()
    if args.mode == "campaign":
        return run_campaign(limit=max(1, int(args.limit)), live=bool(args.live))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
