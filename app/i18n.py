from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# Canonical product language is English. Native names stay native in the picker.
# UI copy lives in /locales.
LOCALES = ("en", "de", "es", "fr", "pt", "it", "pl", "nl", "cs", "sk", "hu")
DEFAULT_LOCALE = "en"
LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

_HTML_LANG = {
    "en": "en",
    "de": "de",
    "es": "es",
    "fr": "fr",
    "pt": "pt",
    "it": "it",
    "pl": "pl",
    "nl": "nl",
    "cs": "cs",
    "sk": "sk",
    "hu": "hu",
}

_NATIVE_NAME = {
    "en": "English",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
    "pt": "Português",
    "it": "Italiano",
    "pl": "Polski",
    "nl": "Nederlands",
    "cs": "Čeština",
    "sk": "Slovenčina",
    "hu": "Magyar",
}

_ALIASES = {
    "cz": "cs",
    "cs": "cs",
    "czech": "cs",
    "sk": "sk",
    "slovak": "sk",
    "en": "en",
    "eng": "en",
    "de": "de",
    "ger": "de",
    "es": "es",
    "spa": "es",
    "fr": "fr",
    "pt": "pt",
    "pt-br": "pt",
    "pt-pt": "pt",
    "it": "it",
    "pl": "pl",
    "nl": "nl",
    "hu": "hu",
}

KIND_ORDER = (
    "passport",
    "id_card",
    "warranty",
    "insurance",
    "stk",
    "emissions",
    "vignette",
    "pet",
    "boiler",
    "other",
)
_KIND_ORDER = KIND_ORDER


def _bundle(locale: str) -> dict:
    path = LOCALES_DIR / f"{locale}.json"
    return json.loads(path.read_text(encoding="utf-8"))


_OG_LOCALE = {
    "en": "en_US",
    "de": "de_DE",
    "es": "es_ES",
    "fr": "fr_FR",
    "pt": "pt_PT",
    "it": "it_IT",
    "pl": "pl_PL",
    "nl": "nl_NL",
    "cs": "cs_CZ",
    "sk": "sk_SK",
    "hu": "hu_HU",
}


def html_lang(locale: str) -> str:
    return _HTML_LANG.get(normalize_locale(locale), "en")


def og_locale(locale: str) -> str:
    return _OG_LOCALE.get(normalize_locale(locale), "en_US")


def locale_choices() -> list[tuple[str, str]]:
    return [(code, _NATIVE_NAME[code]) for code in LOCALES]


def _from_tag(tag: str) -> str | None:
    tag = tag.lower().replace("_", "-").strip()
    if not tag or tag == "*":
        return None
    if tag in LOCALES:
        return tag
    if tag in _ALIASES:
        return _ALIASES[tag]
    primary = tag.split("-", 1)[0]
    if primary in LOCALES:
        return primary
    if primary in _ALIASES:
        return _ALIASES[primary]
    return None


def normalize_locale(value: str | None) -> str:
    if not value:
        return DEFAULT_LOCALE
    matched = _from_tag(value.strip())
    return matched or DEFAULT_LOCALE


def pick_locale(header: str | None) -> str:
    if not header:
        return DEFAULT_LOCALE
    ranked: list[tuple[float, str]] = []
    for item in header.split(","):
        item = item.strip()
        if not item:
            continue
        if ";q=" in item:
            lang, raw_q = item.split(";q=", 1)
            try:
                quality = float(raw_q.strip())
            except ValueError:
                quality = 0.0
        else:
            lang, quality = item, 1.0
        ranked.append((quality, lang.strip()))
    ranked.sort(key=lambda pair: -pair[0])
    for _, lang in ranked:
        matched = _from_tag(lang)
        if matched:
            return matched
    return DEFAULT_LOCALE


def t(locale: str, key: str, **kwargs) -> str:
    locale = normalize_locale(locale)
    data = _bundle(locale)
    fallback = _bundle(DEFAULT_LOCALE)
    node: object = data
    fb: object = fallback
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        fb = fb.get(part) if isinstance(fb, dict) else None
    text = node if isinstance(node, str) else fb
    if not isinstance(text, str):
        return key
    if "{price}" in text and "price" not in kwargs:
        from .billing import format_price

        kwargs["price"] = format_price(locale)
    return text.format(**kwargs) if kwargs else text


def kinds(locale: str) -> list[tuple[str, str]]:
    locale = normalize_locale(locale)
    items = _bundle(locale).get("kinds", {})
    return [(key, items.get(key, key)) for key in _KIND_ORDER]


def polish_few(n: int) -> bool:
    mod10 = n % 10
    mod100 = n % 100
    return mod10 in (2, 3, 4) and mod100 not in (12, 13, 14)


def format_date(locale: str, value: date) -> str:
    locale = normalize_locale(locale)
    if locale == "en":
        return value.strftime("%d %b %Y")
    if locale in ("es", "fr", "it", "pt"):
        return f"{value.day:02d}/{value.month:02d}/{value.year}"
    return f"{value.day}. {value.month}. {value.year}"


def days_label(locale: str, days: int) -> str:
    locale = normalize_locale(locale)
    if days < 0:
        return t(locale, "app.days_overdue", n=abs(days))
    if days == 0:
        return t(locale, "app.days_today")
    if days == 1:
        return t(locale, "app.days_one")
    if locale in ("sk", "cs") and days in (2, 3, 4):
        return t(locale, "app.days_few", n=days)
    if locale == "pl" and polish_few(days):
        return t(locale, "app.days_few", n=days)
    return t(locale, "app.days_many", n=days)


_SKIP_LOCALE_EXACT = frozenset(
    {
        "/robots.txt",
        "/sitemap.xml",
        "/manifest.webmanifest",
        "/sw.js",
        "/openapi.json",
        "/app/google/callback",
    }
)
_SKIP_LOCALE_PREFIXES = (
    "/static",
    "/internal",
    "/admin",
    "/docs",
    "/lang",
)


def locale_path(locale: str, path: str) -> str:
    locale = normalize_locale(locale)
    if not path:
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        return f"/{locale}"
    return f"/{locale}{path}"


def split_locale_prefix(path: str) -> tuple[str | None, str]:
    if not path or path == "/":
        return None, "/"
    parts = path.strip("/").split("/", 1)
    first = (parts[0] or "").lower()
    if first not in LOCALES:
        return None, path if path.startswith("/") else f"/{path}"
    rest = f"/{parts[1]}" if len(parts) > 1 and parts[1] else "/"
    if rest != "/" and rest.endswith("/"):
        rest = rest.rstrip("/")
    return first, rest


def skip_locale_prefix(path: str) -> bool:
    if path in _SKIP_LOCALE_EXACT:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _SKIP_LOCALE_PREFIXES)


def guess_locale(cookie: str | None, accept_language: str | None) -> str:
    if cookie:
        return normalize_locale(cookie)
    return pick_locale(accept_language)


def with_locale_prefix(path_and_query: str, locale: str) -> str:
    path, _, query = path_and_query.partition("?")
    if not path:
        path = "/"
    found, bare = split_locale_prefix(path)
    logical = bare if found else path
    if skip_locale_prefix(logical) and not logical.startswith("/admin"):
        return path_and_query
    new_path = locale_path(locale, logical)
    return f"{new_path}?{query}" if query else new_path
