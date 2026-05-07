"""AI writer utilities using Gemini SDK."""

from __future__ import annotations

import json
import logging
import re
import time
from hashlib import sha256
from typing import Any, Dict, Optional

import google.generativeai as genai
import requests

from . import config as app_config

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

_MODEL_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
_GEMINI_COOLDOWN_UNTIL = 0.0
_GENERATE_CACHE: dict[str, tuple[float, str]] = {}
_OPTIMIZE_CACHE: dict[str, tuple[float, str]] = {}
_GENERATE_CACHE_TTL_SEC = 20 * 60
_OPTIMIZE_CACHE_TTL_SEC = 30 * 60
_AVAILABLE_MODELS_CACHE: tuple[float, list[str]] = (0.0, [])
_AVAILABLE_MODELS_TTL_SEC = 10 * 60
_OPENAI_FALLBACKS = [
    "gpt-4o-mini",
    "gpt-4.1-mini",
    "gpt-4.1",
]
_LAST_GENERATION_WARNING: str = ""


def _now_ts() -> float:
    return time.time()


def _set_last_generation_warning(message: str) -> None:
    global _LAST_GENERATION_WARNING
    _LAST_GENERATION_WARNING = str(message or "").strip()


def get_last_generation_warning() -> str:
    return _LAST_GENERATION_WARNING


def _cache_get(store: dict[str, tuple[float, str]], key: str, ttl_sec: int) -> Optional[str]:
    item = store.get(key)
    if not item:
        return None
    created_at, value = item
    if _now_ts() - created_at > ttl_sec:
        store.pop(key, None)
        return None
    return value


def _cache_set(store: dict[str, tuple[float, str]], key: str, value: str) -> None:
    store[key] = (_now_ts(), value)


def _make_cache_key(parts: list[str]) -> str:
    payload = "\n||\n".join(parts)
    return sha256(payload.encode("utf-8")).hexdigest()


def _ordered_models(preferred: Optional[str] = None) -> list[str]:
    ordered: list[str] = []
    if preferred and preferred.strip():
        ordered.append(preferred.strip())

    cfg_model = str(getattr(app_config, "GEMINI_MODEL", "") or "").strip()
    if cfg_model and cfg_model not in ordered:
        ordered.append(cfg_model)

    for fallback in _MODEL_FALLBACKS:
        if fallback not in ordered:
            ordered.append(fallback)
    return ordered


def _list_available_generate_models(api_key: str) -> list[str]:
    global _AVAILABLE_MODELS_CACHE
    ts, cached = _AVAILABLE_MODELS_CACHE
    if cached and (_now_ts() - ts) <= _AVAILABLE_MODELS_TTL_SEC:
        return cached

    try:
        genai.configure(api_key=api_key)
        models = list(genai.list_models())
        available: list[str] = []
        for item in models:
            methods = [str(m) for m in getattr(item, "supported_generation_methods", [])]
            if "generateContent" not in methods:
                continue
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            if name.startswith("models/"):
                name = name.split("/", 1)[1]
            available.append(name)
        _AVAILABLE_MODELS_CACHE = (_now_ts(), available)
        return available
    except Exception as exc:
        logger.warning("Cannot list Gemini models, keep fallback order: %s", exc)
        return []


def _extract_retry_delay_seconds(error_text: str) -> int:
    match = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", error_text, flags=re.IGNORECASE)
    if not match:
        return 30
    return max(1, int(float(match.group(1))))


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "resourceexhausted" in text or "quota exceeded" in text or "429" in text


def _cooldown_remaining_sec() -> int:
    remain = int(max(0.0, _GEMINI_COOLDOWN_UNTIL - _now_ts()))
    return remain


def _set_cooldown_from_exception(exc: Exception) -> None:
    global _GEMINI_COOLDOWN_UNTIL
    retry_after = _extract_retry_delay_seconds(str(exc))
    _GEMINI_COOLDOWN_UNTIL = max(_GEMINI_COOLDOWN_UNTIL, _now_ts() + retry_after)
    logger.warning("Gemini cooldown enabled for %ss due to quota/rate-limit.", retry_after)


def _generate_with_fallback_models(
    *,
    api_key: str,
    prompt: str,
    preferred_model: Optional[str],
    temperature: float,
    max_output_tokens: int,
) -> str:
    global _GEMINI_COOLDOWN_UNTIL
    remain = _cooldown_remaining_sec()
    if remain > 0:
        raise RuntimeError(f"Gemini cooldown active. Retry in {remain}s.")

    available = _list_available_generate_models(api_key)
    candidates = _ordered_models(preferred_model)
    if available:
        candidates = [m for m in candidates if m in available]
        if not candidates:
            raise RuntimeError("No configured Gemini candidate model is available for generateContent.")

    genai.configure(api_key=api_key)
    last_exc: Optional[Exception] = None
    for selected_model in candidates:
        try:
            gm = genai.GenerativeModel(model_name=selected_model)
            response = gm.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                ),
            )
            text = (getattr(response, "text", None) or "").strip()
            if text:
                return text
        except Exception as exc:
            last_exc = exc
            if _is_quota_error(exc):
                _set_cooldown_from_exception(exc)
                continue
            logger.warning("Gemini call failed on model %s: %s", selected_model, exc)
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Gemini returned empty output from all candidate models.")


def _pick_model(preferred: Optional[str] = None) -> str:
    """Resolve a usable Gemini model name for generate_content."""
    ordered: list[str] = []
    if preferred and preferred.strip():
        ordered.append(preferred.strip())

    cfg_model = str(getattr(app_config, "GEMINI_MODEL", "") or "").strip()
    if cfg_model:
        ordered.append(cfg_model)

    for fallback in _MODEL_FALLBACKS:
        if fallback not in ordered:
            ordered.append(fallback)

    api_key = str(getattr(app_config, "GEMINI_API_KEY", "") or "").strip()
    if not api_key:
        return ordered[0]

    try:
        genai.configure(api_key=api_key)
        models = list(genai.list_models())
        available = []
        for item in models:
            methods = [str(m) for m in getattr(item, "supported_generation_methods", [])]
            if "generateContent" not in methods:
                continue
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            if name.startswith("models/"):
                name = name.split("/", 1)[1]
            available.append(name)
        for candidate in ordered:
            if candidate in available:
                return candidate
    except Exception as exc:
        logger.warning("Cannot resolve Gemini model list, fallback to configured model: %s", exc)

    return ordered[0]


def _ordered_openai_models(preferred: Optional[str] = None) -> list[str]:
    ordered: list[str] = []
    if preferred and preferred.strip():
        ordered.append(preferred.strip())

    cfg_model = str(getattr(app_config, "OPENAI_TEXT_MODEL", "") or "").strip()
    if cfg_model and cfg_model not in ordered:
        ordered.append(cfg_model)

    for fallback in _OPENAI_FALLBACKS:
        if fallback not in ordered:
            ordered.append(fallback)
    return ordered


def _openai_chat_completion(
    *,
    api_key: str,
    prompt: str,
    preferred_model: Optional[str],
    temperature: float,
    max_tokens: int,
) -> str:
    candidates = _ordered_openai_models(preferred_model)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error = ""
    for selected_model in candidates:
        try:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json={
                    "model": selected_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=90,
            )
            payload = response.json() if response.content else {}
            if response.status_code >= 400:
                last_error = str(payload.get("error", {}).get("message") or f"HTTP {response.status_code}")
                logger.warning("OpenAI call failed on model %s: %s", selected_model, last_error)
                continue

            choices = payload.get("choices") if isinstance(payload, dict) else None
            first = choices[0] if isinstance(choices, list) and choices else {}
            message = first.get("message") if isinstance(first, dict) else {}
            text = (message.get("content") if isinstance(message, dict) else "") or ""
            text = str(text).strip()
            if text:
                return text
        except Exception as exc:
            last_error = str(exc)
            logger.warning("OpenAI call exception on model %s: %s", selected_model, exc)
            continue

    raise RuntimeError(f"OpenAI returned no text from all candidate models. Last error: {last_error}")


def _active_text_provider() -> str:
    provider = str(getattr(app_config, "AI_TEXT_PROVIDER", "") or "").strip().lower()
    if provider in {"gemini", "openai"}:
        return provider
    return "gemini"


def _is_openai_quota_or_rate_error(error_text: str) -> bool:
    text = str(error_text or "").lower()
    return (
        "exceeded your current quota" in text
        or "insufficient_quota" in text
        or "rate limit" in text
        or "429" in text
    )


def _generate_text(
    *,
    prompt: str,
    preferred_model: Optional[str],
    temperature: float,
    max_output_tokens: int,
) -> str:
    provider = _active_text_provider()
    if provider == "openai":
        api_key = str(getattr(app_config, "OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is missing.")
        try:
            return _openai_chat_completion(
                api_key=api_key,
                prompt=prompt,
                preferred_model=preferred_model,
                temperature=temperature,
                max_tokens=max_output_tokens,
            )
        except Exception as exc:
            # If OpenAI is temporarily unavailable due to quota/rate limits,
            # fall back to Gemini when a Gemini key exists.
            if _is_openai_quota_or_rate_error(str(exc)):
                gemini_key = str(getattr(app_config, "GEMINI_API_KEY", "") or "").strip()
                if gemini_key:
                    logger.warning(
                        "OpenAI quota/rate-limit encountered. Falling back to Gemini."
                    )
                    return _generate_with_fallback_models(
                        api_key=gemini_key,
                        prompt=prompt,
                        preferred_model=None,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    )
            raise

    api_key = str(getattr(app_config, "GEMINI_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing.")
    return _generate_with_fallback_models(
        api_key=api_key,
        prompt=prompt,
        preferred_model=preferred_model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


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
        _set_last_generation_warning("Thiếu tiêu đề bài viết, không thể tạo caption.")
        return ""

    provider = _active_text_provider()
    has_provider_key = bool(
        str(
            getattr(app_config, "OPENAI_API_KEY", "")
            if provider == "openai"
            else getattr(app_config, "GEMINI_API_KEY", "")
        ).strip()
    )
    if not has_provider_key:
        logger.warning("AI key missing for provider=%s. Returning fallback caption.", provider)
        _set_last_generation_warning(
            f"Thiếu API key cho provider '{provider}', đã dùng caption dự phòng."
        )
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

    fallback_caption = f"{title}\n\n{summary}\n\nNguồn: {source}\n{url}".strip()
    cache_key = _make_cache_key(
        [
            "generate_caption",
            title,
            summary,
            source,
            url,
            str(tone or ""),
            str(niche or ""),
            str(template or ""),
            provider,
            str(model or ""),
        ]
    )
    cached = _cache_get(_GENERATE_CACHE, cache_key, _GENERATE_CACHE_TTL_SEC)
    if cached:
        _set_last_generation_warning("")
        return cached

    try:
        caption = _generate_text(
            prompt=prompt,
            preferred_model=model,
            temperature=0.7,
            max_output_tokens=700,
        )
        _cache_set(_GENERATE_CACHE, cache_key, caption)
        _set_last_generation_warning("")
        return caption
    except Exception as exc:
        logger.exception("generate_caption failed: %s", exc)
        _set_last_generation_warning(f"AI tạo nội dung lỗi: {exc}. Đã dùng caption dự phòng.")
        return fallback_caption


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


def optimize_prompt_template(
    *,
    current_prompt: str,
    article: Dict[str, Any],
    tone: str,
    niche: str,
    model: Optional[str] = None,
) -> str:
    """Optimize prompt template for better Facebook caption output quality."""

    title = str(article.get("title") or "").strip()
    summary = str(article.get("summary") or "").strip()
    source = str(article.get("source") or "unknown").strip()
    url = str(article.get("url") or "").strip()
    cleaned_prompt = str(current_prompt or "").strip()

    fallback = f"""Bạn là biên tập viên Facebook chuyên niche "{niche}".

Mục tiêu:
- Chuyển bài gốc thành caption tiếng Việt giàu cảm xúc, dễ đọc, giữ đúng dữ kiện.
- Giọng văn: {tone}
- Ưu tiên mở bài bằng hook mạnh và kết bằng CTA tự nhiên.

Dữ liệu đầu vào:
- Tiêu đề: {title}
- Tóm tắt: {summary}
- Nguồn: {source}
- URL: {url}

Yêu cầu output:
- 140-260 từ
- 2-5 emoji đúng ngữ cảnh
- Không markdown, không bịa thông tin
- Có câu hỏi/kêu gọi bình luận cuối bài
- Dòng cuối: "Nguồn: {source}"
"""

    provider = _active_text_provider()
    has_provider_key = bool(
        str(
            getattr(app_config, "OPENAI_API_KEY", "")
            if provider == "openai"
            else getattr(app_config, "GEMINI_API_KEY", "")
        ).strip()
    )
    if not has_provider_key:
        return fallback

    optimizer_prompt = f"""
Bạn là chuyên gia prompt engineering cho tác vụ viết caption Facebook.
Hãy tối ưu prompt dưới đây để mô hình viết caption hay hơn nhưng vẫn an toàn và đúng dữ kiện.

PROMPT HIỆN TẠI:
{cleaned_prompt or "(trống)"}

NGỮ CẢNH:
- Niche: {niche}
- Tone: {tone}
- Tiêu đề: {title}
- Tóm tắt: {summary}
- Nguồn: {source}
- URL: {url}

YÊU CẦU CHO PROMPT MỚI:
- Viết bằng tiếng Việt.
- Rõ vai trò + nhiệm vụ + ràng buộc output.
- Tăng chất lượng hook, cấu trúc, CTA.
- Nhắc tránh bịa thông tin.
- Chỉ trả về nội dung prompt hoàn chỉnh, không giải thích.
"""
    cache_key = _make_cache_key(
        [
            "optimize_prompt",
            title,
            summary,
            source,
            url,
            str(tone or ""),
            str(niche or ""),
            cleaned_prompt,
            provider,
            str(model or ""),
        ]
    )
    cached = _cache_get(_OPTIMIZE_CACHE, cache_key, _OPTIMIZE_CACHE_TTL_SEC)
    if cached:
        return cached

    try:
        optimized = _generate_text(
            prompt=optimizer_prompt,
            preferred_model=model,
            temperature=0.4,
            max_output_tokens=900,
        )
        optimized = optimized.strip() or fallback
        _cache_set(_OPTIMIZE_CACHE, cache_key, optimized)
        return optimized
    except Exception as exc:
        logger.exception("optimize_prompt_template failed: %s", exc)
        return fallback


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

    provider = _active_text_provider()
    has_provider_key = bool(
        str(
            getattr(app_config, "OPENAI_API_KEY", "")
            if provider == "openai"
            else getattr(app_config, "GEMINI_API_KEY", "")
        ).strip()
    )
    if not has_provider_key:
        logger.warning("AI key missing for provider=%s. Returning fallback campaign package.", provider)
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
        raw = _generate_text(
            prompt=prompt,
            preferred_model=model,
            temperature=0.8,
            max_output_tokens=1800,
        )
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
