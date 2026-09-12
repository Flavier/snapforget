from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .models import EventLog

MAX_REQUEST_BYTES = 10 * 1024 * 1024
PASSWORD_MIN = 8
PASSWORD_MAX = 128
EMAIL_MAX = 254
TITLE_MAX = 200
NOTES_MAX = 4000
SCAN_PER_DAY = int(os.getenv("SCAN_PER_DAY") or "20")
_CARD_TITLE_DEFAULT = 32
SCAN_BURST = 6
SCAN_BURST_SECONDS = 300
CRON_SECRET_MIN = 16

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


def debug_mode() -> bool:
    return (os.getenv("APP_DEBUG") or "").strip().lower() in ("1", "true", "yes")


def cookie_secure() -> bool:
    flag = (os.getenv("COOKIE_SECURE") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return (os.getenv("APP_BASE_URL") or "").lower().startswith("https://")


def allowed_hosts() -> list[str]:
    extra = [h.strip() for h in (os.getenv("ALLOWED_HOSTS") or "").split(",") if h.strip()]
    if not extra:
        return ["*"]
    hosts = ["localhost", "127.0.0.1", "testserver", *extra]
    base = urlparse(os.getenv("APP_BASE_URL") or "")
    if base.hostname:
        hosts.append(base.hostname)
    return list(dict.fromkeys(hosts))


def client_ip(request: Request) -> str:
    if (os.getenv("TRUST_PROXY") or "").strip().lower() in ("1", "true", "yes"):
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


def over_limit(key: str, limit: int, window_seconds: int) -> bool:
    now = time.monotonic()
    with _lock:
        bucket = _hits[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket) >= limit


def too_many(key: str, limit: int, window_seconds: int) -> bool:
    if over_limit(key, limit, window_seconds):
        return True
    now = time.monotonic()
    with _lock:
        _hits[key].append(now)
        return False


def scans_today(db, user_id: int) -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(EventLog)
        .filter(
            EventLog.user_id == user_id,
            EventLog.action == "scan.read",
            EventLog.created_at >= start,
        )
        .count()
    )


def scan_blocked(request: Request, user_id: int) -> bool:
    ip = client_ip(request)
    if too_many(f"scan-burst:{user_id}", SCAN_BURST, SCAN_BURST_SECONDS):
        return True
    if too_many(f"scan-ip:{ip}", SCAN_BURST * 3, SCAN_BURST_SECONDS):
        return True
    return scans_today(request.state.db, user_id) >= SCAN_PER_DAY


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token or not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def rotate_session(request: Request) -> None:
    request.session.clear()
    request.session["csrf"] = secrets.token_urlsafe(32)


def _csrf_ok(got: str, expected: str) -> bool:
    if not got or not expected:
        return False
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)


def csrf_matches(request: Request, token: str = "") -> bool:
    expected = str(request.session.get("csrf") or "")
    got = (token or request.headers.get("x-csrf-token") or "").strip()
    return _csrf_ok(got, expected)


def locale_href(request: Request, path: str) -> str:
    from .i18n import guess_locale, locale_path

    loc = getattr(request.state, "locale", None) or guess_locale(
        request.cookies.get("locale"),
        request.headers.get("accept-language"),
    )
    return locale_path(loc, path)


def csrf_fail(request: Request) -> Response:
    path = getattr(request.state, "bare_path", None) or request.url.path
    if path.startswith("/admin"):
        return RedirectResponse(url="/admin/login", status_code=303)
    if path.endswith("/scan") or "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)
    return RedirectResponse(url=locale_href(request, "/login"), status_code=303)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.secure = cookie_secure()

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_REQUEST_BYTES:
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)
        path = request.url.path
        if path.startswith("/internal/"):
            pass
        elif request.method not in ("GET", "HEAD", "OPTIONS") and not path.startswith("/static/"):
            if too_many(f"flood:{client_ip(request)}", 120, 60):
                return Response(status_code=429)
        response = await call_next(request)
        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self' https://checkout.stripe.com https://billing.stripe.com; "
            "frame-ancestors 'none'"
        )
        if self.secure:
            csp += "; upgrade-insecure-requests"
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(self), microphone=(), geolocation=(), payment=(), usb=()"
        )
        if path.startswith("/app") or path.startswith("/admin") or path.startswith("/reset") or path in (
            "/login",
            "/register",
            "/upgrade",
            "/forgot",
        ):
            response.headers["Cache-Control"] = "private, no-store"
        return response


def clip(value: str, limit: int) -> str:
    text = (value or "").replace("\x00", "").strip()
    return text[:limit]


def card_title_max() -> int:
    raw = (os.getenv("CARD_TITLE_MAX") or str(_CARD_TITLE_DEFAULT)).strip()
    try:
        n = int(raw)
    except ValueError:
        n = _CARD_TITLE_DEFAULT
    return max(8, min(n, TITLE_MAX))


def ellipsize(value: str, limit: int | None = None) -> str:
    """Shorten for cards: last whole word, then an ellipsis. Full text stays stored."""
    text = " ".join((value or "").split())
    cap = card_title_max() if limit is None else limit
    if cap <= 0 or len(text) <= cap:
        return text
    if cap == 1:
        return "…"
    budget = cap - 1
    snippet = text[:budget]
    if budget < len(text) and not text[budget].isspace():
        space = snippet.rfind(" ")
        min_keep = max(4, budget // 2)
        if space >= min_keep:
            snippet = snippet[:space]
    snippet = snippet.rstrip(" .,;:!?–-/")
    if not snippet:
        snippet = text[:budget].rstrip()
    return f"{snippet}…"


def cron_secret_ok(got: str, expected: str) -> bool:
    if len(expected) < CRON_SECRET_MIN or not got:
        return False
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)
