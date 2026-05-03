from __future__ import annotations

import logging
from typing import Optional

import requests

from .config import Settings

logger = logging.getLogger(__name__)


class FacebookPoster:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def post_to_page(self, message: str) -> Optional[str]:
        if not self.settings.fb_page_id or not self.settings.fb_page_access_token:
            logger.error("Facebook credentials are missing.")
            return None

        url = (
            f"https://graph.facebook.com/{self.settings.fb_api_version}/"
            f"{self.settings.fb_page_id}/feed"
        )
        payload = {
            "message": message,
            "access_token": self.settings.fb_page_access_token,
        }

        try:
            response = requests.post(url, data=payload, timeout=20)
            response.raise_for_status()
            data = response.json()
            return data.get("id")
        except Exception as exc:
            logger.exception("Facebook post failed: %s", exc)
            return None

