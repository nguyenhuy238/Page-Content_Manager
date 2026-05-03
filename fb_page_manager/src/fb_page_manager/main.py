from __future__ import annotations

import argparse
import logging

from .ai_writer import AIWriter
from .config import get_settings
from .crawler import Crawler
from .database import Database
from .fb_poster import FacebookPoster
from .scheduler import ContentScheduler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Facebook Page content manager")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one pipeline cycle and exit",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    db = Database(settings.db_path)
    scheduler = ContentScheduler(
        settings=settings,
        db=db,
        crawler=Crawler(settings),
        ai_writer=AIWriter(settings),
        fb_poster=FacebookPoster(settings),
    )

    try:
        if args.once:
            scheduler.run_cycle()
        else:
            scheduler.start()
    finally:
        db.close()


if __name__ == "__main__":
    main()

