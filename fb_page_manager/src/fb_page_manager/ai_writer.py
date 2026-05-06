"""AI writer utilities using Gemini SDK."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

import google.generativeai as genai

from .config import GEMINI_API_KEY, GEMINI_MODEL

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
    """Generate caption from article using Gemini API.

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

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is missing. Returning fallback caption.")
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
        genai.configure(api_key=GEMINI_API_KEY)
        gm = genai.GenerativeModel(model_name=model or GEMINI_MODEL)
        response = gm.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                max_output_tokens=700,
            ),
        )

        caption = (getattr(response, "text", None) or "").strip()
        if caption:
            return caption

        logger.warning("Gemini returned empty output. Using fallback.")
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


def _extract_json_object(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)

    match = re.search(r"\{[\s\S]*\}", stripped)
    if not match:
        raise ValueError("No JSON object found in model output")
    return json.loads(match.group(0))


def generate_campaign_package(
    story: Dict[str, Any],
    *,
    target_language: str = "es-MX",
    target_country: str = "Mexico",
    target_audience: str = "Audiencia mexicana interesada en celebridades y cronicas",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a full campaign package for WordPress + Facebook."""

    title = str(story.get("title") or "").strip()
    source = str(story.get("source") or "unknown").strip()
    url = str(story.get("url") or "").strip()
    summary = str(story.get("summary") or "").strip()
    content = str(story.get("content") or "").strip()
    content_type = str(story.get("content_type") or "article").strip()

    if not title:
        return {}

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY missing. Returning fallback campaign package.")
        short_summary = summary or (content[:480] + "..." if len(content) > 480 else content)
        return {
            "headline": title,
            "facebook_hook": f"{title}\n\n{short_summary}",
            "facebook_cta": "Te leo en comentarios. Si quieres la historia completa, pide el enlace.",
            "article_title": title,
            "article_excerpt": short_summary,
            "article_html": f"<p>{short_summary}</p><p><strong>Fuente:</strong> <a href=\"{url}\">{source}</a></p>",
            "image_prompt": f"Poster editorial dramatico sobre: {title}. Estilo periodistico cinematografico, enfocado en audiencia mexicana.",
            "tags": ["celebridades", "mexico", "viral"],
            "category": "Entertainment",
            "source_url": url,
            "source_name": source,
        }

    prompt = f"""
Actua como editor senior de contenido viral para Facebook + WordPress.
Tu publico principal es de {target_country}.
Idioma obligatorio: {target_language}.
Audiencia: {target_audience}.

Debes transformar la fuente en un paquete de publicacion de ALTO ENGANCHE
sin inventar hechos y sin afirmar rumores como hechos confirmados.
Si la fuente es incompleta, usa frases prudentes como "segun reportes" o
"de acuerdo con lo publicado por la fuente original".

FUENTE:
- Tipo: {content_type}
- Titulo: {title}
- Resumen: {summary}
- Contenido base: {content}
- Fuente: {source}
- URL: {url}

Devuelve SOLO JSON valido con estas claves exactas:
{{
  "headline": "titular corto y fuerte para Facebook",
  "facebook_hook": "texto 90-150 palabras, tono intenso, cierre con CTA a comentar",
  "facebook_cta": "frase corta para invitar a comentar y pedir enlace",
  "article_title": "titulo SEO para WordPress",
  "article_excerpt": "resumen 30-55 palabras",
  "article_html": "cuerpo en HTML con 5-8 parrafos y subtitulos <h2>, estilo periodistico narrativo",
  "image_prompt": "prompt detallado para IA generativa, formato vertical 2:3, estilo dramatico",
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "category": "Entertainment o News",
  "risk_note": "breve nota sobre que parte es confirmada y que parte es contexto"
}}
"""

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gm = genai.GenerativeModel(model_name=model or GEMINI_MODEL)
        response = gm.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.8,
                max_output_tokens=1800,
            ),
        )
        raw = (getattr(response, "text", None) or "").strip()
        payload = _extract_json_object(raw)
    except Exception as exc:
        logger.exception("generate_campaign_package failed: %s", exc)
        short_summary = summary or (content[:480] + "..." if len(content) > 480 else content)
        payload = {
            "headline": title,
            "facebook_hook": f"{title}\n\n{short_summary}",
            "facebook_cta": "Comenta si quieres la historia completa y contexto detallado.",
            "article_title": title,
            "article_excerpt": short_summary,
            "article_html": f"<p>{short_summary}</p>",
            "image_prompt": f"Editorial dramatic image about {title}, cinematic lighting, vertical 2:3.",
            "tags": ["celebridades", "mexico"],
            "category": "Entertainment",
            "risk_note": "fallback_without_gemini",
        }

    payload["source_url"] = url
    payload["source_name"] = source
    payload["source_title"] = title
    return payload

