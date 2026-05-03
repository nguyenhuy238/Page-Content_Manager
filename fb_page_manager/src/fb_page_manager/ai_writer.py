"""AI writer utilities using Anthropic SDK."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from anthropic import Anthropic

from .config import CLAUDE_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_TEMPLATE = """Bạn là chuyên gia biên tập nội dung Facebook cho niche: {niche}.

Nhiệm vụ:
- Viết caption tiếng Việt từ bài gốc bên dưới.
- Giữ đúng thông tin, dễ đọc, tự nhiên.
- Dùng giọng văn: {tone}.
- Thêm 2-4 emoji phù hợp.
- Kết thúc bằng câu kêu gọi tương tác.

Bài gốc:
- Tiêu đề: {title}
- Tóm tắt: {summary}
- Nguồn: {source}
- URL: {url}

Yêu cầu output:
- 120-220 từ
- Không dùng markdown
- Có dòng "Nguồn: {source}" ở cuối
"""


def generate_caption(
    article: Dict[str, Any],
    tone: str,
    niche: str,
    prompt_template: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Generate caption from article using Claude API.

    Args:
        article: dict containing title/url/source/summary.
        tone: writing tone (e.g., hài hước, chuyên nghiệp).
        niche: target page niche.
        prompt_template: optional custom prompt template.
        model: optional model override.
    """

    title = str(article.get("title") or "").strip()
    summary = str(article.get("summary") or "").strip()
    source = str(article.get("source") or "unknown").strip()
    url = str(article.get("url") or "").strip()

    if not title:
        return ""

    if not CLAUDE_API_KEY:
        logger.warning("CLAUDE_API_KEY is missing. Returning fallback caption.")
        return f"{title}\n\n{summary}\n\nNguồn: {source}\n{url}".strip()

    template = prompt_template or _DEFAULT_PROMPT_TEMPLATE
    prompt = template.format(
        niche=niche,
        tone=tone,
        title=title,
        summary=summary,
        source=source,
        url=url,
    )

    try:
        client = Anthropic(api_key=CLAUDE_API_KEY)
        response = client.messages.create(
            model=model or CLAUDE_MODEL,
            max_tokens=700,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )

        chunks = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                chunks.append(block.text)

        caption = "\n".join(chunks).strip()
        if caption:
            return caption

        logger.warning("Claude returned empty output. Using fallback.")
        return f"{title}\n\n{summary}\n\nNguồn: {source}\n{url}".strip()
    except Exception as exc:
        logger.exception("generate_caption failed: %s", exc)
        return f"{title}\n\n{summary}\n\nNguồn: {source}\n{url}".strip()


def quality_check(caption: str) -> Dict[str, Any]:
    """Evaluate quick quality signals for generated caption."""
    text = caption.strip()
    words = [w for w in re.split(r"\s+", text) if w]
    word_count = len(words)
    has_emoji = bool(re.search(r"[\U0001F300-\U0001FAFF]", text))
    has_cta = bool(re.search(r"\?|bình luận|comment|chia sẻ|share|bạn nghĩ sao", text, re.IGNORECASE))

    score = 0
    if 100 <= word_count <= 300:
        score += 4
    elif 70 <= word_count <= 350:
        score += 2

    if has_emoji:
        score += 3
    if has_cta:
        score += 3

    return {
        "word_count": word_count,
        "has_emoji": has_emoji,
        "has_cta": has_cta,
        "score": score,
    }


class AIWriter:
    """Compatibility wrapper for older pipeline code."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    def rewrite_caption(self, title: str, summary: str, url: str) -> str:
        article = {
            "title": title,
            "summary": summary,
            "url": url,
            "source": "unknown",
        }
        return generate_caption(article=article, tone="chuyên nghiệp", niche="Công nghệ & AI")

