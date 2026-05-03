"""Facebook Graph API posting and insights helpers."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from .config import ACCESS_TOKEN, PAGE_ID

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v19.0"
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"


def post_to_page(
    page_id: str,
    token: str,
    message: str,
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Post message (or image + caption) to Facebook Page via Graph API."""
    if not page_id or not token:
        return {"ok": False, "error": "Missing page_id or access token"}
    if not message.strip() and not image_url:
        return {"ok": False, "error": "Message is empty"}

    try:
        if image_url:
            endpoint = f"{GRAPH_BASE_URL}/{page_id}/photos"
            payload = {
                "url": image_url,
                "caption": message,
                "access_token": token,
            }
            response = requests.post(endpoint, data=payload, timeout=25)
        else:
            endpoint = f"{GRAPH_BASE_URL}/{page_id}/feed"
            payload = {
                "message": message,
                "access_token": token,
            }
            response = requests.post(endpoint, data=payload, timeout=25)

        data = response.json() if response.content else {}
        if response.status_code >= 400:
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": data.get("error", {}).get("message", "Facebook API error"),
                "response": data,
            }

        post_id = data.get("post_id") or data.get("id")
        return {"ok": True, "post_id": post_id, "response": data}
    except Exception as exc:
        logger.exception("post_to_page failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def get_post_insights(post_id: str, token: str) -> Dict[str, int]:
    """Get basic post reach + engagement from Graph Insights API."""
    if not post_id or not token:
        return {"reach": 0, "engagement": 0}

    metrics = "post_impressions,post_engaged_users"
    endpoint = f"{GRAPH_BASE_URL}/{post_id}/insights"

    try:
        response = requests.get(
            endpoint,
            params={"metric": metrics, "access_token": token},
            timeout=25,
        )
        payload = response.json() if response.content else {}

        if response.status_code >= 400:
            logger.error("Insights API error: %s", payload)
            return {"reach": 0, "engagement": 0}

        reach = 0
        engagement = 0
        for item in payload.get("data", []):
            name = str(item.get("name") or "")
            values = item.get("values") or []
            current_value = values[0].get("value") if values and isinstance(values[0], dict) else 0

            if name == "post_impressions":
                reach = int(current_value or 0)
            elif name == "post_engaged_users":
                engagement = int(current_value or 0)

        return {"reach": reach, "engagement": engagement}
    except Exception as exc:
        logger.exception("get_post_insights failed: %s", exc)
        return {"reach": 0, "engagement": 0}


class FacebookPoster:
    """Compatibility wrapper for old class-based usage."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    def post_to_page(self, message: str) -> Optional[str]:
        page_id = PAGE_ID
        token = ACCESS_TOKEN
        if self.settings is not None:
            page_id = getattr(self.settings, "page_id", page_id)
            token = getattr(self.settings, "access_token", token)

        result = post_to_page(page_id=page_id, token=token, message=message)
        if result.get("ok"):
            return str(result.get("post_id") or "") or None
        return None

