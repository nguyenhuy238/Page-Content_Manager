"""Image generation helper for campaign assets."""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)


def _safe_file_stem(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", text.strip().lower())
    cleaned = cleaned.strip("-")
    if not cleaned:
        cleaned = "campaign-image"
    return cleaned[:80]


def generate_image(
    *,
    prompt: str,
    api_key: str,
    model: str = "gpt-image-1",
    size: str = "1024x1536",
    output_dir: str = "data/generated_images",
    file_stem: str = "campaign-image",
) -> Dict[str, Any]:
    """Generate an image with OpenAI Image API and store it locally."""

    if not prompt.strip():
        return {"ok": False, "error": "Empty image prompt"}
    if not api_key.strip():
        return {"ok": False, "error": "OPENAI_API_KEY missing"}

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_stem = _safe_file_stem(file_stem)
    file_path = out_dir / f"{final_stem}.png"

    try:
        response = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "prompt": prompt,
                "size": size,
                "output_format": "png",
            },
            timeout=90,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            return {
                "ok": False,
                "error": payload.get("error", {}).get("message", "OpenAI image API error"),
                "response": payload,
            }

        data = payload.get("data") if isinstance(payload, dict) else None
        first = data[0] if isinstance(data, list) and data else {}
        image_b64 = first.get("b64_json") if isinstance(first, dict) else None
        if not image_b64:
            return {"ok": False, "error": "Image API returned empty b64_json", "response": payload}

        binary = base64.b64decode(image_b64)
        file_path.write_bytes(binary)
        return {"ok": True, "path": str(file_path), "response": payload}
    except Exception as exc:
        logger.exception("generate_image failed: %s", exc)
        return {"ok": False, "error": str(exc)}
