"""WordPress REST API publisher for automated campaigns."""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


def _auth_header(username: str, app_password: str) -> Dict[str, str]:
    token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class WordPressPublisher:
    def __init__(self, base_url: str, username: str, app_password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.app_password = app_password
        self.session = requests.Session()
        self.session.headers.update(_auth_header(username, app_password))

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.username and self.app_password)

    def _endpoint(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def upload_media(self, file_path: str, alt_text: str = "") -> Dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            return {"ok": False, "error": f"File not found: {file_path}"}

        mime_type, _ = mimetypes.guess_type(path.name)
        if not mime_type:
            mime_type = "application/octet-stream"

        url = self._endpoint("/wp-json/wp/v2/media")
        headers = {
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "Content-Type": mime_type,
        }
        binary = path.read_bytes()

        try:
            response = self.session.post(url, headers=headers, data=binary, timeout=45)
            payload = response.json() if response.content else {}
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "error": payload.get("message", "WordPress media upload failed"),
                    "response": payload,
                }

            media_id = payload.get("id")
            if media_id and alt_text.strip():
                self.session.post(
                    self._endpoint(f"/wp-json/wp/v2/media/{media_id}"),
                    json={"alt_text": alt_text.strip()},
                    timeout=30,
                )

            return {
                "ok": True,
                "media_id": media_id,
                "source_url": payload.get("source_url"),
                "response": payload,
            }
        except Exception as exc:
            logger.exception("WordPress upload_media failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def create_post(
        self,
        *,
        title: str,
        content_html: str,
        excerpt: str,
        status: str = "draft",
        category_id: int = 0,
        author_id: int = 0,
        featured_media_id: Optional[int] = None,
        tags: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        if not title.strip() or not content_html.strip():
            return {"ok": False, "error": "title/content is required"}

        payload: Dict[str, Any] = {
            "title": title.strip(),
            "content": content_html.strip(),
            "excerpt": excerpt.strip(),
            "status": (status or "draft").strip(),
            "comment_status": "open",
            "ping_status": "open",
        }

        if category_id > 0:
            payload["categories"] = [int(category_id)]
        if author_id > 0:
            payload["author"] = int(author_id)
        if featured_media_id and int(featured_media_id) > 0:
            payload["featured_media"] = int(featured_media_id)

        # Core WP REST uses tag IDs; we keep free-text tags in meta to avoid hard dependency.
        if tags:
            payload["meta"] = {"campaign_tags_text": ", ".join(tags[:12])}

        try:
            response = self.session.post(
                self._endpoint("/wp-json/wp/v2/posts"),
                json=payload,
                timeout=45,
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "error": data.get("message", "WordPress create post failed"),
                    "response": data,
                }

            return {
                "ok": True,
                "post_id": data.get("id"),
                "post_url": data.get("link"),
                "response": data,
            }
        except Exception as exc:
            logger.exception("WordPress create_post failed: %s", exc)
            return {"ok": False, "error": str(exc)}
