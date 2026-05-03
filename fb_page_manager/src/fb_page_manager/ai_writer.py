from __future__ import annotations

import logging

from anthropic import Anthropic

from .config import Settings

logger = logging.getLogger(__name__)


class AIWriter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = Anthropic(api_key=settings.claude_api_key) if settings.claude_api_key else None

    def rewrite_caption(self, title: str, summary: str, url: str) -> str:
        if not self.client:
            logger.warning("CLAUDE_API_KEY is empty. Using original title as caption.")
            return f"{title}\n\nRead more: {url}"

        prompt = (
            "Rewrite this news caption in Vietnamese for a Facebook Page.\n"
            "Requirements:\n"
            "- Natural, short, clear\n"
            "- Add 1 call-to-action sentence\n"
            "- Keep factual accuracy\n"
            "- Do not use markdown\n\n"
            f"Title: {title}\n"
            f"Summary: {summary}\n"
            f"URL: {url}\n"
        )

        try:
            response = self.client.messages.create(
                model=self.settings.claude_model,
                max_tokens=300,
                temperature=0.7,
                messages=[{"role": "user", "content": prompt}],
            )

            text_parts = []
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    text_parts.append(block.text)

            caption = "\n".join(text_parts).strip()
            if not caption:
                return f"{title}\n\nRead more: {url}"
            return caption
        except Exception as exc:
            logger.exception("Claude rewrite failed: %s", exc)
            return f"{title}\n\nRead more: {url}"

