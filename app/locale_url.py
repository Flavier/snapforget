from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp

from .i18n import (
    guess_locale,
    locale_path,
    skip_locale_prefix,
    split_locale_prefix,
)
from .protect import cookie_secure

_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def remember_locale(response: Response, locale: str) -> None:
    response.set_cookie(
        "locale",
        locale,
        max_age=_COOKIE_MAX_AGE,
        samesite="lax",
        secure=cookie_secure(),
        path="/",
    )


class LocalePrefixMiddleware(BaseHTTPMiddleware):
    """/{locale}/privacy is the public URL. Routes still see /privacy."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.scope.get("path") or "/"
        if skip_locale_prefix(path):
            request.state.locale = guess_locale(
                request.cookies.get("locale"),
                request.headers.get("accept-language"),
            )
            request.state.bare_path = path
            return await call_next(request)

        found, bare = split_locale_prefix(path)
        if found:
            request.state.locale = found
            request.state.bare_path = bare
            request.scope["path"] = bare
            request.scope["raw_path"] = bare.encode("utf-8")
            response = await call_next(request)
            remember_locale(response, found)
            return response

        request.state.bare_path = path
        locale = guess_locale(
            request.cookies.get("locale"),
            request.headers.get("accept-language"),
        )
        request.state.locale = locale
        if request.method in ("GET", "HEAD"):
            dest = locale_path(locale, path)
            query = request.scope.get("query_string") or b""
            if query:
                dest = f"{dest}?{query.decode('latin-1')}"
            response = RedirectResponse(url=dest, status_code=302)
            remember_locale(response, locale)
            return response
        return await call_next(request)
