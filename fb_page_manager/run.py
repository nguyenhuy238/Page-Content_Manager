from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Tuple

import requests
from anthropic import Anthropic

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from fb_page_manager.config import get_settings
from fb_page_manager.crawler import Crawler, fetch_newsapi, fetch_rss
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


def _test_claude() -> Tuple[bool, str]:
    settings = get_settings()
    if not settings.claude_api_key:
        return False, "CLAUDE_API_KEY đang trống"

    try:
        client = Anthropic(api_key=settings.claude_api_key)
        result = client.models.list(limit=1)
        has_data = bool(getattr(result, "data", []))
        if has_data:
            return True, "Claude API: OK"
        return False, "Claude API: phản hồi rỗng"
    except Exception as exc:
        return False, f"Claude API lỗi: {exc}"


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
    if not settings.rss_urls:
        return False, "RSS_URLS đang trống"

    try:
        rss_items = fetch_rss(settings.rss_urls)
        parts = [f"RSS={len(rss_items)} bài"]

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

        return True, "Nguồn tin: " + " | ".join(parts)
    except Exception as exc:
        return False, f"Nguồn tin lỗi: {exc}"


def run_test_connections() -> int:
    logger = logging.getLogger("run.test")

    checks = [
        ("Facebook", _test_facebook),
        ("Claude", _test_claude),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FB Page Manager runner")
    parser.add_argument(
        "mode",
        choices=["web", "scheduler", "fetch", "test"],
        help="Mode: web | scheduler | fetch | test",
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
