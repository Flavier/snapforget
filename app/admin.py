from __future__ import annotations

import hmac
import os
import unicodedata
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from .i18n import KIND_ORDER
from .models import ROOT, Document, User
from .protect import clip

ADMIN_PASSWORD_MIN = 16
PAGE_SIZE = 50
SEARCH_MAX = 80
_ENV_MTIME = None


def _reload_env() -> None:
    global _ENV_MTIME
    path = ROOT / ".env"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return
    if _ENV_MTIME == stamp:
        return
    load_dotenv(path, override=True, interpolate=False)
    _ENV_MTIME = stamp


def _clean_secret(value: str) -> str:
    text = (value or "").replace("\x00", "").strip()
    return unicodedata.normalize("NFC", text)


def admin_password() -> str:
    _reload_env()
    return _clean_secret(os.getenv("ADMIN_PASSWORD") or "")


def admin_username() -> str:
    _reload_env()
    return _clean_secret(os.getenv("ADMIN_USER") or "admin") or "admin"


def admin_configured() -> bool:
    return len(admin_password()) >= ADMIN_PASSWORD_MIN


def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin")) and admin_configured()


def _compare(got: str, expected: str) -> bool:
    if not got or not expected:
        return False
    left = got.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def verify_admin(username: str, password: str) -> bool:
    if not admin_configured():
        return False
    user_ok = _compare(_clean_secret(username)[:64].casefold(), admin_username()[:64].casefold())
    pwd_ok = _compare(_clean_secret(password)[:128], admin_password())
    return user_ok and pwd_ok


def _like_contains(raw: str) -> str:
    text = clip(raw.lower(), SEARCH_MAX)
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_filters(paid: str, kind: str, q: str, page: str) -> dict:
    paid_key = paid.strip()
    if paid_key not in ("", "1", "0"):
        paid_key = ""
    kind_key = kind.strip()
    if kind_key not in KIND_ORDER:
        kind_key = ""
    query = clip(q.strip().lower(), SEARCH_MAX)
    try:
        page_n = int(page)
    except (TypeError, ValueError):
        page_n = 1
    if page_n < 1:
        page_n = 1
    return {"paid": paid_key, "kind": kind_key, "q": query, "page": page_n}


def filter_query(db: Session, paid: str, kind: str, q: str):
    rows = db.query(User)
    if paid == "1":
        rows = rows.filter(User.is_paid.is_(True))
    elif paid == "0":
        rows = rows.filter(User.is_paid.is_(False))
    if kind:
        with_kind = db.query(Document.user_id).filter(Document.kind == kind).distinct()
        rows = rows.filter(User.id.in_(with_kind))
    if q:
        rows = rows.filter(User.email.like(f"%{_like_contains(q)}%", escape="\\"))
    return rows


def _kinds_for_users(db: Session, user_ids: list[int]) -> dict[int, list[tuple[str, int]]]:
    if not user_ids:
        return {}
    rows = (
        db.query(Document.user_id, Document.kind, func.count(Document.id))
        .filter(Document.user_id.in_(user_ids))
        .group_by(Document.user_id, Document.kind)
        .all()
    )
    rank = {key: i for i, key in enumerate(KIND_ORDER)}
    grouped: dict[int, list[tuple[str, int]]] = {}
    for user_id, kind, n in rows:
        grouped.setdefault(user_id, []).append((kind, int(n)))
    for user_id, items in grouped.items():
        items.sort(key=lambda pair: (rank.get(pair[0], 99), pair[0]))
    return grouped


def list_customers(db: Session, paid: str, kind: str, q: str, page: int) -> dict:
    filtered = filter_query(db, paid, kind, q)
    total = filtered.count()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        page = pages
    counts = (
        db.query(Document.user_id, func.count(Document.id).label("docs"))
        .group_by(Document.user_id)
        .subquery()
    )
    grouped = (
        filtered.outerjoin(counts, User.id == counts.c.user_id)
        .with_entities(User, func.coalesce(counts.c.docs, 0))
        .order_by(User.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    kinds_map = _kinds_for_users(db, [user.id for user, _docs in grouped])
    rows = [
        {"user": user, "docs": docs, "kinds": kinds_map.get(user.id, [])}
        for user, docs in grouped
    ]
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": PAGE_SIZE,
    }


def query_string(paid: str, kind: str, q: str, page: int | None = None) -> str:
    data = {}
    if paid:
        data["paid"] = paid
    if kind:
        data["kind"] = kind
    if q:
        data["q"] = q
    if page and page > 1:
        data["page"] = str(page)
    return urlencode(data)
