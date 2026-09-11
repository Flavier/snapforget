from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from dotenv import load_dotenv

from .models import ROOT, Document, User

load_dotenv(ROOT / ".env", override=True)

log = logging.getLogger("snapforget.google")

SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MAX_REMINDER_MINUTES = 4 * 7 * 24 * 60


def oauth_enabled() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def calendar_connected(user: User | None) -> bool:
    return bool(user and user.google_refresh_token)


def redirect_uri(request) -> str:
    configured = (os.getenv("GOOGLE_REDIRECT_URI") or "").strip()
    if configured:
        return configured
    return str(request.base_url).rstrip("/") + "/app/google/callback"


def authorize_url(request, state: str) -> str:
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID") or "",
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str = "GET") -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"google {exc.code}: {detail}") from exc


def exchange_code(request, code: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": os.getenv("GOOGLE_CLIENT_ID") or "",
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET") or "",
            "redirect_uri": redirect_uri(request),
            "grant_type": "authorization_code",
        }
    ).encode()
    return _http(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )


def save_tokens(user: User, payload: dict) -> None:
    access = payload.get("access_token")
    if access:
        user.google_access_token = str(access)
        expires = int(payload.get("expires_in") or 3600)
        user.google_token_exp = int(time.time()) + max(expires - 60, 30)
    refresh = payload.get("refresh_token")
    if refresh:
        user.google_refresh_token = str(refresh)


def _access_token(user: User) -> str | None:
    if user.google_access_token and user.google_token_exp and user.google_token_exp > int(time.time()):
        return user.google_access_token
    if not user.google_refresh_token:
        return None
    body = urllib.parse.urlencode(
        {
            "refresh_token": user.google_refresh_token,
            "client_id": os.getenv("GOOGLE_CLIENT_ID") or "",
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET") or "",
            "grant_type": "refresh_token",
        }
    ).encode()
    try:
        payload = _http(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    except RuntimeError:
        log.warning("google refresh failed")
        user.google_refresh_token = None
        user.google_access_token = None
        user.google_token_exp = None
        return None
    save_tokens(user, payload)
    return user.google_access_token


def _reminders(doc: Document) -> dict:
    overrides = []
    for days in doc.reminder_offsets():
        minutes = min(days * 24 * 60, MAX_REMINDER_MINUTES)
        overrides.append({"method": "popup", "minutes": minutes})
    if not overrides:
        return {"useDefault": True}
    return {"useDefault": False, "overrides": overrides[:5]}


def _event_body(doc: Document, summary: str, description: str) -> dict:
    day = doc.expires_on.isoformat()
    nxt = (doc.expires_on + timedelta(days=1)).isoformat()
    return {
        "summary": summary,
        "description": description,
        "start": {"date": day},
        "end": {"date": nxt},
        "reminders": _reminders(doc),
        "extendedProperties": {"private": {"snapforget": str(doc.id)}},
    }


def upsert_event(user: User, doc: Document, summary: str, description: str) -> bool:
    if not oauth_enabled() or not user.google_refresh_token:
        return False
    token = _access_token(user)
    if not token:
        return False
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = json.dumps(_event_body(doc, summary, description)).encode()
    try:
        if doc.google_event_id:
            data = _http(
                f"{EVENTS_URL}/{urllib.parse.quote(doc.google_event_id)}",
                data=payload,
                headers=headers,
                method="PATCH",
            )
        else:
            data = _http(EVENTS_URL, data=payload, headers=headers, method="POST")
        event_id = data.get("id")
        if event_id:
            doc.google_event_id = str(event_id)
        return True
    except RuntimeError as exc:
        if doc.google_event_id and "404" in str(exc):
            doc.google_event_id = None
            try:
                data = _http(EVENTS_URL, data=payload, headers=headers, method="POST")
                event_id = data.get("id")
                if event_id:
                    doc.google_event_id = str(event_id)
                return True
            except RuntimeError as retry_exc:
                log.warning("google event failed: %s", retry_exc)
                return False
        log.warning("google event failed: %s", exc)
        return False


def delete_event(user: User, doc: Document) -> None:
    if not doc.google_event_id or not user.google_refresh_token:
        return
    token = _access_token(user)
    if not token:
        return
    req = urllib.request.Request(
        f"{EVENTS_URL}/{urllib.parse.quote(doc.google_event_id)}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 410):
            log.warning("google delete failed: %s", exc.code)
    doc.google_event_id = None


def event_url(doc: Document) -> str | None:
    if not doc.google_event_id:
        return None
    return "https://calendar.google.com/calendar/r/eventedit/" + urllib.parse.quote(doc.google_event_id)
