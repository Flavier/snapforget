from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import date


from dotenv import load_dotenv

from .i18n import kinds, normalize_locale
from .models import ROOT

load_dotenv(ROOT / ".env", override=True)

log = logging.getLogger("snapforget.vision")

KIND_KEYS = {key for key, _ in kinds("en")}
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_SECRET = re.compile(r"(AIza[0-9A-Za-z_-]{8,}|AQ\.[0-9A-Za-z_-]{8,}|key=[^&\s\"']+)", re.I)

# gemini-2.0-flash shut down 2026-06-01. 3.5 Flash-Lite + thinkingLevel minimal
# is fast enough for stamps/dates. 2.5-lite is missing on some keys (404).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_FALLBACKS = (
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

_PROMPT_LANG = {
    "en": "English",
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "pt": "Portuguese",
    "it": "Italian",
    "pl": "Polish",
    "nl": "Dutch",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
}

class VisionError(Exception):
    def __init__(self, code: str, detail: str = "", http_status: int | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.http_status = http_status


def _safe_detail(text: str) -> str:
    return _SECRET.sub("[redacted]", text or "")[:400]


def vision_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def _prompt(today: date, locale: str = "en") -> str:
    locale = normalize_locale(locale)
    language = _PROMPT_LANG.get(locale, "English")
    kinds_list = ", ".join(sorted(KIND_KEYS))
    return (
        "Read a photo of a personal document, receipt, stamp, sticker, or warranty card. "
        "Return JSON only:\n"
        "{"
        f'"kind": one of {kinds_list}, '
        f'"title": short name max 80 chars in {language}, '
        '"expires_on": "YYYY-MM-DD" or null, '
        f'"reason": one short sentence in {language}'
        "}\n"
        f"Write title and reason in {language}. Keep JSON keys in English.\n"
        "Rules:\n"
        "- expires_on is the next reminder date: printed expiry, next MOT/TÜV/STK/ITV, "
        "warranty end, insurance end, vignette, vaccine due, boiler check due.\n"
        "- Inspection done but no next date: add the usual interval (often 2 years for "
        "passenger cars in SK/CZ/EU; 1 year if the paper says so).\n"
        "- Warranty with purchase date and no period: 24 months (typical EU goods).\n"
        "- No evidence of a date: expires_on is null. Still explain why in reason.\n"
        "- No ID numbers, VIN, full addresses, or surnames in title.\n"
        f"- Today is {today.isoformat()}.\n"
        "- kind stk = MOT / STK / TÜV / HU / ITV / contrôle technique / APK."
    )


def _parse_payload(raw: str) -> dict:
    text = (raw or "").strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise VisionError("fail", "no json")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise VisionError("fail", "bad json") from exc
    kind = str(data.get("kind") or "other").strip().lower()
    if kind not in KIND_KEYS:
        kind = "other"
    title = str(data.get("title") or "").strip()[:200]
    expires = data.get("expires_on")
    if expires in ("", "null", None):
        expires_on = None
    else:
        try:
            expires_on = date.fromisoformat(str(expires)[:10]).isoformat()
        except ValueError:
            expires_on = None
    reason = str(data.get("reason") or "").strip()[:300]
    return {"kind": kind, "title": title, "expires_on": expires_on, "reason": reason}


def _http_json(url: str, payload: dict, headers: dict, timeout: int = 20) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _safe_detail(exc.read().decode("utf-8", errors="replace"))
        raise VisionError("fail", detail, http_status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise VisionError("fail", str(exc.reason)) from exc


def _openai(image_b64: str, mime: str, today: date, locale: str) -> dict:
    key = os.getenv("OPENAI_API_KEY") or ""
    model = os.getenv("OPENAI_VISION_MODEL") or "gpt-4o-mini"
    data = _http_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _prompt(today, locale)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                        },
                    ],
                }
            ],
        },
        {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        raw = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionError("fail", "bad openai response") from exc
    return _parse_payload(raw)


def _gemini_models() -> list[str]:
    chosen = (os.getenv("GEMINI_VISION_MODEL") or "").strip()
    models: list[str] = []
    if chosen:
        models.append(chosen)
    for name in GEMINI_FALLBACKS:
        if name not in models:
            models.append(name)
    return models


def _missing_model(err: VisionError) -> bool:
    if err.http_status == 404:
        return True
    detail = (err.detail or "").lower()
    return "not found" in detail or "not supported" in detail


def _gemini_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionError("fail", "bad gemini response") from exc
    texts: list[str] = []
    thoughts: list[str] = []
    for part in parts or []:
        text = str((part or {}).get("text") or "")
        if not text:
            continue
        if part.get("thought"):
            thoughts.append(text)
        else:
            texts.append(text)
    raw = "\n".join(texts) or "\n".join(thoughts)
    if not raw:
        raise VisionError("fail", "empty gemini response")
    return raw


def _thinking_config(model: str) -> dict:
    if model.startswith("gemini-3"):
        return {"thinkingLevel": "minimal"}
    return {"thinkingBudget": 0}


def _bad_thinking(err: VisionError) -> bool:
    if err.http_status != 400:
        return False
    detail = (err.detail or "").lower()
    return "thinking" in detail or "thought" in detail


def _gemini(image_b64: str, mime: str, today: date, locale: str) -> dict:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    prompt = _prompt(today, locale)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": key,
    }
    last: VisionError | None = None
    for model in _gemini_models():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 256,
                "responseMimeType": "application/json",
                "thinkingConfig": _thinking_config(model),
            },
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inlineData": {"mimeType": mime, "data": image_b64}},
                    ]
                }
            ],
        }
        try:
            data = _http_json(url, payload, headers)
            return _parse_payload(_gemini_text(data))
        except VisionError as exc:
            last = exc
            if _bad_thinking(exc):
                payload["generationConfig"].pop("thinkingConfig", None)
                try:
                    data = _http_json(url, payload, headers)
                    return _parse_payload(_gemini_text(data))
                except VisionError as retry_exc:
                    last = retry_exc
                    if _missing_model(retry_exc):
                        log.warning("gemini model %s unavailable, trying next", model)
                        continue
                    log.warning("gemini failed (%s): %s", model, _safe_detail(retry_exc.detail))
                    raise
            if _missing_model(exc):
                log.warning("gemini model %s unavailable, trying next", model)
                continue
            log.warning("gemini failed (%s): %s", model, _safe_detail(exc.detail))
            raise
    if last:
        log.warning("gemini failed: %s", _safe_detail(last.detail))
        raise last
    raise VisionError("fail", "no gemini model")


def read_document_photo(
    image_bytes: bytes,
    mime: str,
    today: date | None = None,
    locale: str = "en",
) -> dict:
    if not vision_enabled():
        raise VisionError("off")
    today = today or date.today()
    locale = normalize_locale(locale)
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    if os.getenv("OPENAI_API_KEY"):
        return _openai(image_b64, mime, today, locale)
    return _gemini(image_b64, mime, today, locale)
