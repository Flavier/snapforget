from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .activity import record, setup_logging
from .admin import (
    admin_configured,
    is_admin,
    list_customers,
    parse_filters,
    query_string,
    verify_admin,
)
from .billing import (
    apply_event,
    checkout_url,
    drop_customer,
    format_price,
    parse_webhook,
    payments_ready,
    portal_url,
    sync_checkout_session,
    sync_user_from_stripe,
    webhook_secret,
)
from .protect import (
    EMAIL_MAX,
    NOTES_MAX,
    PASSWORD_MAX,
    PASSWORD_MIN,
    TITLE_MAX,
    SecurityHeadersMiddleware,
    allowed_hosts,
    client_ip,
    clip,
    cookie_secure,
    ellipsize,
    cron_secret_ok,
    csrf_fail,
    csrf_matches,
    csrf_token,
    debug_mode,
    over_limit,
    rotate_session,
    scan_blocked,
    too_many,
)
from .calendar_ics import document_to_ics, google_calendar_url
from .google_cal import (
    authorize_url,
    calendar_connected,
    delete_event,
    event_url,
    exchange_code,
    oauth_enabled,
    save_tokens,
    upsert_event,
)
from .i18n import (
    DEFAULT_LOCALE,
    LOCALES,
    days_label,
    format_date,
    html_lang,
    kinds,
    locale_choices,
    locale_path,
    guess_locale,
    normalize_locale,
    og_locale,
    skip_locale_prefix,
    t,
    with_locale_prefix,
)
from .locale_url import LocalePrefixMiddleware, remember_locale
from .icons import ensure_icons
from .images import ImageError, jpeg_for_vision, normalize_photo
from .legal import legal_contact
from .mail import alert_ops, app_base_url, delete_mail_dumps, reminder_html, send_mail
from .models import DATA_DIR, ROOT, Document, EventLog, MailLog, PasswordReset, SessionLocal, User, init_db
from .reminders import cron_stale, run_reminders
from .security import hash_password, hash_token, secret_key, verify_password
from .storage import (
    claim_stash,
    delete_photo,
    delete_user_files,
    ensure_storage,
    persist_jpeg,
    photo_on_disk,
    stash_jpeg,
)
from .vision import VisionError, read_document_photo, vision_enabled

load_dotenv(ROOT / ".env", override=True, interpolate=False)

FREE_DOCUMENT_LIMIT = 3
MAX_PHOTO_BYTES = 8 * 1024 * 1024
_NOINDEX_PREFIXES = (
    "/app",
    "/admin",
    "/internal",
    "/forgot",
    "/reset",
    "/upgrade",
    "/offline",
)
ALLOWED_PHOTO = {
    "image/jpeg",
    "image/jpg",
    "image/pjpeg",
    "image/png",
    "image/webp",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(
    title="Snap & Forget",
    docs_url="/docs" if debug_mode() else None,
    redoc_url=None,
    openapi_url="/openapi.json" if debug_mode() else None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=secret_key(DATA_DIR / "secret.key"),
    same_site="lax",
    https_only=cookie_secure(),
    max_age=60 * 60 * 24 * 30,
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())
app.add_middleware(LocalePrefixMiddleware)

TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_locale(request: Request) -> str:
    loc = getattr(request.state, "locale", None)
    if loc:
        return normalize_locale(loc)
    return guess_locale(request.cookies.get("locale"), request.headers.get("accept-language"))


def loc_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=locale_path(get_locale(request), path), status_code=status_code)


def db_user(request: Request) -> User | None:
    user_id = request.session.get("uid")
    if not user_id:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return request.state.db.get(User, uid)


def flash_pop(request: Request) -> str | None:
    return request.session.pop("flash", None)


def _noindex_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _NOINDEX_PREFIXES)


def canonical_url(request: Request) -> str:
    origin = app_base_url().rstrip("/")
    bare = getattr(request.state, "bare_path", None) or request.url.path or "/"
    if skip_locale_prefix(bare):
        path = bare
    else:
        path = locale_path(get_locale(request), bare)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return origin if path == "/" else f"{origin}{path}"


def hreflang_alternates(request: Request) -> list[tuple[str, str]]:
    bare = getattr(request.state, "bare_path", None) or request.url.path or "/"
    if skip_locale_prefix(bare) or _noindex_path(bare):
        return []
    origin = app_base_url().rstrip("/")
    items = [(code, f"{origin}{locale_path(code, bare)}") for code in LOCALES]
    items.append(("x-default", f"{origin}{locale_path(DEFAULT_LOCALE, bare)}"))
    return items


def software_json_ld(locale: str) -> dict:
    origin = app_base_url()
    return {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": t(locale, "brand"),
        "alternateName": ["Snap & Forget", "Odfoť a zabudni"],
        "description": t(locale, "meta.description"),
        "url": f"{origin.rstrip('/')}{locale_path(locale, '/')}",
        "applicationCategory": "UtilitiesApplication",
        "operatingSystem": "Web",
        "inLanguage": html_lang(locale),
        "offers": {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "EUR",
            "description": t(locale, "landing.price_free"),
        },
    }


def flash_set(request: Request, key: str) -> None:
    request.session["flash"] = key


def heal_billing(request: Request, user: User) -> None:
    if too_many(f"stripe-sync:{user.id}", 1, 900):
        return
    try:
        result = sync_user_from_stripe(request.state.db, user)
    except Exception as exc:
        record(action="pay.sync", status="fail", user=user, detail="heal")
        alert_ops(
            "stripe-heal-fail",
            "Snap & Forget: Stripe sync failed",
            f"Could not check Stripe for user {user.id}.\n{exc}",
            every_seconds=3600,
        )
        return
    request.state.db.refresh(user)
    if result == "paid":
        flash_set(request, "flash.paid")
        record(action="pay.sync", user=user, detail="heal paid")
        alert_ops(
            f"pay-paid-{user.id}",
            "Snap & Forget: paid in Stripe, was free in the app",
            f"User {user.id} ({user.email}) had an active subscription in Stripe "
            "but was still free here. The app unlocked them. "
            "Usually a closed checkout tab or a missed webhook.",
            every_seconds=21600,
        )
    elif result == "unpaid":
        record(action="pay.sync", user=user, detail="heal unpaid")
        alert_ops(
            f"pay-unpaid-{user.id}",
            "Snap & Forget: Stripe says not paying, app had paid",
            f"User {user.id} ({user.email}) was marked paid here, but Stripe "
            "has no active subscription. The app switched them to free.",
            every_seconds=21600,
        )


def warn_if_cron_stale() -> None:
    if not cron_stale():
        return
    alert_ops(
        "cron-stale",
        "Snap & Forget: daily job has not run",
        "The reminder/billing job has not completed in 36 hours. "
        "Expiry pings will not go out until cron hits "
        f"{app_base_url()}/internal/reminders",
        every_seconds=86400,
    )


def warn_if_webhook_off() -> None:
    if not payments_ready():
        return
    if len(webhook_secret()) >= 16:
        return
    alert_ops(
        "webhook-off",
        "Snap & Forget: Stripe webhook is not set",
        "STRIPE_WEBHOOK_SECRET is missing. Checkout can still mark someone paid "
        "if they return to the success page, but cancel/renewal will not update "
        "the app until the daily job or the next login. Set the webhook to "
        f"{app_base_url()}/internal/stripe",
        every_seconds=86400,
    )


def render(request: Request, name: str, status_code: int = 200, **ctx):
    locale = get_locale(request)
    user = db_user(request)
    if user and user.locale != locale:
        user.locale = locale
        request.state.db.commit()
    names = dict(locale_choices())
    bare = getattr(request.state, "bare_path", None) or request.url.path or "/"
    query = request.url.query

    def u(path: str) -> str:
        return locale_path(locale, path)

    def lang_url(code: str) -> str:
        url = locale_path(normalize_locale(code), bare)
        return f"{url}?{query}" if query else url

    response = templates.TemplateResponse(
        request,
        name,
        {
            "t": lambda key, **kwargs: t(locale, key, **{"price": format_price(locale), **kwargs}),
            "locale": locale,
            "html_lang": html_lang(locale),
            "locale_choices": locale_choices(),
            "locale_name": names.get(locale, locale),
            "u": u,
            "lang_url": lang_url,
            "hreflang": hreflang_alternates(request),
            "og_locale_alternates": [og_locale(code) for code in LOCALES if code != locale],
            "user": user,
            "is_admin": is_admin(request),
            "kinds": kinds(locale),
            "kind_label": dict(kinds(locale)),
            "format_date": lambda d: format_date(locale, d),
            "days_label": lambda d: days_label(locale, d),
            "card_title": ellipsize,
            "flash": flash_pop(request),
            "now": datetime.utcnow(),
            "google_oauth": oauth_enabled(),
            "google_connected": calendar_connected(user),
            "csrf_token": csrf_token(request),
            "legal_name": legal_contact()["name"],
            "legal_email": legal_contact()["email"],
            "legal_address": legal_contact()["address"],
            "canonical": canonical_url(request),
            "og_locale": og_locale(locale),
            "og_image": f"{app_base_url()}/static/og.png",
            "json_ld": software_json_ld(locale),
            "noindex": _noindex_path(request.url.path),
            **ctx,
        },
        status_code=status_code,
    )
    path = request.url.path
    if _noindex_path(path):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def login_redirect(request: Request) -> RedirectResponse:
    return loc_redirect(request, "/login")


def require_user(request: Request) -> User | RedirectResponse:
    user = db_user(request)
    if user:
        return user
    return login_redirect(request)


def _reset_secret() -> str:
    return secret_key(DATA_DIR / "secret.key")


def issue_reset_token(db, user: User) -> str:
    db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
    raw = secrets.token_urlsafe(32)
    db.add(
        PasswordReset(
            user_id=user.id,
            token_hash=hash_token(raw, _reset_secret()),
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
    )
    db.commit()
    return raw


def user_from_reset(db, raw: str) -> User | None:
    token = (raw or "").strip()
    if not token or len(token) > 80:
        return None
    digest = hash_token(token, _reset_secret())
    row = (
        db.query(PasswordReset)
        .filter(PasswordReset.token_hash == digest, PasswordReset.expires_at > datetime.utcnow())
        .one_or_none()
    )
    if not row:
        return None
    return db.get(User, row.user_id)


def send_reset_email(user: User, token: str, locale: str) -> None:
    url = f"{app_base_url()}/reset/{token}"
    subject = t(locale, "mail.reset_subject")
    heading = t(locale, "mail.reset_heading")
    lead = t(locale, "mail.reset_lead")
    cta = t(locale, "mail.reset_cta")
    footer = t(locale, "mail.reset_footer")
    text = "\n".join([heading, "", lead, "", url, "", footer])
    html_body = reminder_html(
        brand=t(locale, "brand"),
        heading=heading,
        lead=lead,
        when_line="",
        cta=cta,
        url=url,
        footer=footer,
    )
    send_mail(user.email, subject, text, html_body, user_id=user.id, note="password reset")


def erase_account(db, user: User) -> None:
    uid = user.id
    email = user.email
    customer_id = user.stripe_customer_id
    for doc in list(user.documents):
        delete_event(user, doc)
        delete_photo(doc.photo_path)
    db.query(PasswordReset).filter(PasswordReset.user_id == uid).delete()
    db.query(EventLog).filter(or_(EventLog.user_id == uid, EventLog.email == email)).delete(
        synchronize_session=False
    )
    db.query(MailLog).filter(MailLog.to_email == email).delete(synchronize_session=False)
    db.delete(user)
    db.commit()
    drop_customer(customer_id or "")
    delete_user_files(uid)
    delete_mail_dumps(email)


def save_photo(
    user_id: int,
    upload: UploadFile | None,
    scan_token: str = "",
) -> tuple[str | None, str | None]:
    token = (scan_token or "").strip()
    if token:
        claimed = claim_stash(user_id, token)
        if claimed:
            return claimed, None
    if upload is None or not upload.filename:
        return None, None
    content_type = (upload.content_type or "").lower().split(";")[0].strip()
    if content_type and content_type not in ALLOWED_PHOTO:
        return None, "form.error_photo"
    data = upload.file.read()
    if not data or len(data) > MAX_PHOTO_BYTES:
        return None, "form.error_photo"
    try:
        jpeg = normalize_photo(data)
    except ImageError:
        return None, "form.error_photo"
    return persist_jpeg(user_id, jpeg), None


def owned_document(request: Request, user: User, doc_id: int) -> Document | None:
    return (
        request.state.db.query(Document)
        .filter(Document.id == doc_id, Document.user_id == user.id)
        .one_or_none()
    )


def _calendar_copy(request: Request, doc: Document) -> tuple[str, str]:
    locale = get_locale(request)
    summary = t(locale, "detail.ics_summary", title=doc.title)
    details = t(locale, "detail.ics_desc")
    if doc.notes:
        details = f"{details}\n{doc.notes}"
    return summary, details


def _sync_google(request: Request, user: User, doc: Document) -> None:
    if not calendar_connected(user):
        flash_set(request, "flash.saved")
        return
    summary, details = _calendar_copy(request, doc)
    ok = upsert_event(user, doc, summary, details)
    record(
        action="google.sync",
        status="ok" if ok else "fail",
        user=user,
        document_id=doc.id,
        detail=doc.title,
    )
    flash_set(request, "flash.saved_cal" if ok else "flash.google_fail")


def over_free_limit(request: Request, user: User) -> bool:
    if user.is_paid:
        return False
    count = request.state.db.query(Document).filter(Document.user_id == user.id).count()
    return count >= FREE_DOCUMENT_LIMIT


@app.on_event("startup")
def on_startup() -> None:
    setup_logging()
    init_db()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "css").mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "js").mkdir(parents=True, exist_ok=True)
    ensure_icons(STATIC_DIR)
    ensure_storage()


@app.middleware("http")
async def db_session(request: Request, call_next):
    db = SessionLocal()
    request.state.db = db
    try:
        response = await call_next(request)
        return response
    finally:
        db.close()


def _safe_back(request: Request, fallback: str = "/") -> str:
    referer = request.headers.get("referer") or fallback
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return fallback
    path = parsed.path or fallback
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def _set_lang_response(request: Request, code: str) -> RedirectResponse:
    locale = normalize_locale(code)
    user = db_user(request)
    if user and user.locale != locale:
        user.locale = locale
        request.state.db.commit()
        record(action="locale.set", user=user, detail=locale)
    dest = with_locale_prefix(_safe_back(request), locale)
    response = RedirectResponse(url=dest, status_code=303)
    remember_locale(response, locale)
    return response


@app.get("/lang")
def set_lang_query(request: Request, code: str = "en"):
    return _set_lang_response(request, code)


@app.get("/lang/{code}")
def set_lang(code: str, request: Request):
    return _set_lang_response(request, code)


@app.get("/manifest.webmanifest")
def manifest(request: Request):
    locale = get_locale(request)
    name = t(locale, "brand")
    payload = {
        "id": "/",
        "name": name,
        "short_name": t(locale, "brand_short"),
        "description": t(locale, "meta.description"),
        "start_url": locale_path(locale, "/app"),
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#f3eee4",
        "theme_color": "#f3eee4",
        "lang": html_lang(locale),
        "icons": [
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/icons/icon-512-maskable.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    path = STATIC_DIR / "js" / "sw.js"
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/offline")
def offline(request: Request):
    return render(request, "offline.html")


@app.get("/")
def landing(request: Request):
    if db_user(request):
        return loc_redirect(request, "/app")
    return render(request, "landing.html")


@app.get("/login")
def login_form(request: Request):
    if db_user(request):
        return loc_redirect(request, "/app")
    return render(request, "login.html", error=None, email="")


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    email = clip(email.strip().lower(), EMAIL_MAX)
    ip = client_ip(request)
    if over_limit(f"login-ip:{ip}", 25, 900) or over_limit(f"login-email:{email}", 8, 900):
        record(action="auth.login", status="fail", email=email, detail="rate limit")
        return render(request, "login.html", error="auth.error_lock", email=email, status_code=429)
    user = request.state.db.query(User).filter(User.email == email).one_or_none()
    if not user or not verify_password(password[:PASSWORD_MAX], user.password_hash):
        too_many(f"login-ip:{ip}", 25, 900)
        too_many(f"login-email:{email}", 8, 900)
        record(
            action="auth.login",
            status="fail",
            user=user,
            email=email,
            detail="unknown email" if not user else "bad password",
        )
        return render(request, "login.html", error="auth.error_bad", email=email, status_code=400)
    rotate_session(request)
    request.session["uid"] = user.id
    if not user.locale:
        user.locale = get_locale(request)
        request.state.db.commit()
    record(action="auth.login", user=user)
    return loc_redirect(request, "/app")


@app.get("/register")
def register_form(request: Request):
    if db_user(request):
        return loc_redirect(request, "/app")
    return render(request, "register.html", error=None, email="")


@app.post("/register")
def register_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    website: str = Form(""),
    accept: str = Form(""),
    csrf_token: str = Form(""),
):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    email = clip(email.strip().lower(), EMAIL_MAX)
    ip = client_ip(request)
    if too_many(f"register-ip:{ip}", 8, 3600):
        record(action="auth.register", status="fail", email=email, detail="rate limit")
        return render(request, "register.html", error="auth.error_lock", email=email, status_code=429)
    if (website or "").strip():
        record(action="auth.register", status="fail", email=email, detail="honeypot")
        return loc_redirect(request, "/login")
    if accept != "1":
        record(action="auth.register", status="fail", email=email, detail="no privacy accept")
        return render(request, "register.html", error="auth.error_privacy", email=email, status_code=400)
    if not EMAIL_RE.match(email) or len(email) > EMAIL_MAX:
        record(action="auth.register", status="fail", email=email, detail="bad email")
        return render(request, "register.html", error="auth.error_email", email=email, status_code=400)
    if len(password) < PASSWORD_MIN:
        record(action="auth.register", status="fail", email=email, detail="short password")
        return render(request, "register.html", error="auth.error_short", email=email, status_code=400)
    if len(password) > PASSWORD_MAX:
        record(action="auth.register", status="fail", email=email, detail="long password")
        return render(request, "register.html", error="auth.error_short", email=email, status_code=400)
    if request.state.db.query(User).filter(User.email == email).one_or_none():
        record(action="auth.register", status="fail", email=email, detail="already exists")
        return render(request, "register.html", error="auth.error_exists", email=email, status_code=400)
    user = User(
        email=email,
        password_hash=hash_password(password),
        locale=get_locale(request),
        privacy_at=datetime.utcnow(),
    )
    request.state.db.add(user)
    request.state.db.commit()
    request.state.db.refresh(user)
    rotate_session(request)
    request.session["uid"] = user.id
    record(action="auth.register", user=user, detail=user.locale or "")
    return loc_redirect(request, "/app")


@app.get("/privacy")
def privacy_page(request: Request):
    return render(request, "privacy.html")


@app.get("/terms")
def terms_page(request: Request):
    return render(request, "terms.html")


@app.get("/robots.txt")
def robots_txt():
    origin = app_base_url()
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /app\n"
        "Disallow: /*/app\n"
        "Disallow: /admin\n"
        "Disallow: /internal\n"
        "Disallow: /forgot\n"
        "Disallow: /*/forgot\n"
        "Disallow: /reset\n"
        "Disallow: /*/reset\n"
        "Disallow: /upgrade\n"
        "Disallow: /*/upgrade\n"
        "Disallow: /offline\n"
        "Disallow: /*/offline\n"
        "Disallow: /lang\n"
        f"Sitemap: {origin}/sitemap.xml\n"
    )
    return Response(body, media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    origin = app_base_url().rstrip("/")
    today = date.today().isoformat()
    urls = (
        ("/", "weekly", "1.0"),
        ("/register", "monthly", "0.6"),
        ("/login", "monthly", "0.4"),
        ("/privacy", "yearly", "0.3"),
        ("/terms", "yearly", "0.3"),
    )
    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]
    for path, freq, priority in urls:
        for loc in LOCALES:
            page = f"{origin}{locale_path(loc, path)}"
            alternates = "".join(
                f'<xhtml:link rel="alternate" hreflang="{code}" '
                f'href="{origin}{locale_path(code, path)}"/>'
                for code in LOCALES
            )
            alternates += (
                f'<xhtml:link rel="alternate" hreflang="x-default" '
                f'href="{origin}{locale_path(DEFAULT_LOCALE, path)}"/>'
            )
            chunks.append(
                "<url>"
                f"<loc>{page}</loc>"
                f"<lastmod>{today}</lastmod>"
                f"<changefreq>{freq}</changefreq>"
                f"<priority>{priority}</priority>"
                f"{alternates}"
                "</url>"
            )
    chunks.append("</urlset>")
    return Response("\n".join(chunks) + "\n", media_type="application/xml")


def _admin_gone():
    raise HTTPException(status_code=404)


def _need_admin(request: Request) -> RedirectResponse | None:
    if not admin_configured():
        _admin_gone()
    if not is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


@app.get("/admin/login")
def admin_login_form(request: Request):
    if not admin_configured():
        _admin_gone()
    if is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    return render(request, "admin_login.html", error=None)


@app.post("/admin/login")
def admin_login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    website: str = Form(""),
    csrf_token: str = Form(""),
):
    if not admin_configured():
        _admin_gone()
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    ip = client_ip(request)
    if website.strip() or too_many(f"admin-ip:{ip}", 8, 3600):
        record(action="admin.login", status="fail", detail="rate or bot")
        return render(request, "admin_login.html", error="admin.error_bad", status_code=400)
    if not verify_admin(username, password):
        record(action="admin.login", status="fail", detail="bad credentials")
        return render(request, "admin_login.html", error="admin.error_bad", status_code=400)
    rotate_session(request)
    request.session["admin"] = True
    record(action="admin.login", detail="ok")
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/logout")
def admin_logout(request: Request, csrf_token: str = Form("")):
    if not admin_configured():
        _admin_gone()
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    if is_admin(request):
        record(action="admin.logout")
    rotate_session(request)
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin")
def admin_home(
    request: Request,
    paid: str = "",
    kind: str = "",
    q: str = "",
    page: str = "1",
):
    bounced = _need_admin(request)
    if bounced:
        return bounced
    flt = parse_filters(paid, kind, q, page)
    listing = list_customers(request.state.db, flt["paid"], flt["kind"], flt["q"], flt["page"])

    def qs(page_n: int | None = None) -> str:
        return query_string(flt["paid"], flt["kind"], flt["q"], page_n)

    return render(
        request,
        "admin.html",
        filters=flt,
        listing=listing,
        qs=qs,
        payments_ready=payments_ready(),
    )


@app.get("/forgot")
def forgot_form(request: Request):
    if db_user(request):
        return loc_redirect(request, "/app")
    return render(request, "forgot.html", error=None, email="")


@app.post("/forgot")
def forgot_submit(request: Request, email: str = Form(""), csrf_token: str = Form("")):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    email = clip(email.strip().lower(), EMAIL_MAX)
    ip = client_ip(request)
    if too_many(f"forgot-ip:{ip}", 8, 3600) or too_many(f"forgot-email:{email}", 5, 3600):
        record(action="auth.reset", status="fail", email=email, detail="rate limit")
        flash_set(request, "flash.reset_sent")
        return loc_redirect(request, "/login")
    user = request.state.db.query(User).filter(User.email == email).one_or_none()
    if user:
        try:
            token = issue_reset_token(request.state.db, user)
            send_reset_email(user, token, get_locale(request))
            record(action="auth.reset", user=user, detail="mail sent")
        except Exception:
            record(action="auth.reset", status="fail", user=user, detail="mail failed")
    else:
        record(action="auth.reset", status="fail", email=email, detail="unknown email")
    flash_set(request, "flash.reset_sent")
    return loc_redirect(request, "/login")


@app.get("/reset/{token}")
def reset_form(request: Request, token: str):
    if db_user(request):
        return loc_redirect(request, "/app")
    if not user_from_reset(request.state.db, token):
        return render(request, "forgot.html", error="auth.error_reset", email="", status_code=400)
    return render(request, "reset.html", error=None, token=token)


@app.post("/reset/{token}")
def reset_submit(
    request: Request,
    token: str,
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = user_from_reset(request.state.db, token)
    if not user:
        return render(request, "forgot.html", error="auth.error_reset", email="", status_code=400)
    if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
        return render(request, "reset.html", error="auth.error_short", token=token, status_code=400)
    user.password_hash = hash_password(password)
    request.state.db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
    request.state.db.commit()
    rotate_session(request)
    request.session["uid"] = user.id
    record(action="auth.reset", user=user, detail="password changed")
    flash_set(request, "flash.reset_ok")
    return loc_redirect(request, "/app")


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = db_user(request)
    rotate_session(request)
    if user:
        record(action="auth.logout", user=user)
    flash_set(request, "flash.logged_out")
    return loc_redirect(request, "/")


@app.get("/app")
def dashboard(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    heal_billing(request, user)
    warn_if_cron_stale()
    warn_if_webhook_off()
    docs = (
        request.state.db.query(Document)
        .filter(Document.user_id == user.id)
        .order_by(Document.expires_on.asc())
        .all()
    )
    return render(
        request,
        "dashboard.html",
        documents=docs,
        at_limit=over_free_limit(request, user),
        free_limit=FREE_DOCUMENT_LIMIT,
    )


@app.get("/app/account")
def account_page(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    heal_billing(request, user)
    return render(request, "account.html", error=None)


@app.post("/app/account/delete")
def account_delete(
    request: Request,
    password: str = Form(""),
    confirm: str = Form(""),
    csrf_token: str = Form(""),
):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if confirm != "1" or not verify_password(password[:PASSWORD_MAX], user.password_hash):
        record(action="auth.delete", status="fail", user=user, detail="bad confirm")
        return render(request, "account.html", error="account.error_delete", status_code=400)
    uid = user.id
    erase_account(request.state.db, user)
    rotate_session(request)
    record(action="auth.delete", detail=f"user={uid}")
    flash_set(request, "flash.account_deleted")
    return loc_redirect(request, "/")


@app.get("/app/google/connect")
def google_connect(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if not oauth_enabled():
        flash_set(request, "flash.google_need")
        return loc_redirect(request, "/app")
    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    return RedirectResponse(url=authorize_url(request, state), status_code=303)


@app.get("/app/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    expected = request.session.pop("google_oauth_state", None)
    if error or not code or not expected or state != expected:
        record(action="google.connect", status="fail", user=user, detail=error or "bad oauth state")
        flash_set(request, "flash.google_fail")
        return loc_redirect(request, "/app")
    try:
        payload = exchange_code(request, code)
        save_tokens(user, payload)
        if not user.google_refresh_token:
            raise RuntimeError("no refresh token")
        request.state.db.commit()
        record(action="google.connect", user=user)
        flash_set(request, "flash.google_on")
    except Exception as exc:
        record(action="google.connect", status="fail", user=user, detail=str(exc)[:200])
        flash_set(request, "flash.google_fail")
    return loc_redirect(request, "/app")


@app.post("/app/google/disconnect")
def google_disconnect(request: Request, csrf_token: str = Form("")):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    user.google_refresh_token = None
    user.google_access_token = None
    user.google_token_exp = None
    request.state.db.commit()
    record(action="google.disconnect", user=user)
    flash_set(request, "flash.google_off")
    return loc_redirect(request, "/app")


@app.get("/app/new")
def new_form(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if over_free_limit(request, user):
        record(action="doc.limit", user=user, detail=f"free cap {FREE_DOCUMENT_LIMIT}")
        return loc_redirect(request, "/upgrade")
    return render(
        request,
        "document_form.html",
        mode="new",
        document=None,
        error=None,
        values={
            "kind": "passport",
            "title": "",
            "expires_on": "",
            "notes": "",
            "remind_30": True,
            "remind_7": True,
            "remind_1": True,
        },
    )


@app.post("/app/scan")
async def scan_photo(request: Request, photo: UploadFile | None = File(None)):
    if not csrf_matches(request):
        return csrf_fail(request)
    user = db_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    if scan_blocked(request, user.id):
        record(action="scan.read", status="fail", user=user, detail="limit")
        return JSONResponse({"ok": False, "error": "limit"}, status_code=429)
    if photo is None or not photo.filename:
        return JSONResponse({"ok": False, "error": "fail"}, status_code=400)
    content_type = (photo.content_type or "").lower().split(";")[0].strip()
    if content_type and content_type not in ALLOWED_PHOTO:
        return JSONResponse({"ok": False, "error": "fail"}, status_code=400)
    data = await photo.read()
    if not data or len(data) > MAX_PHOTO_BYTES:
        return JSONResponse({"ok": False, "error": "fail"}, status_code=400)
    try:
        jpeg = normalize_photo(data)
    except ImageError:
        return JSONResponse({"ok": False, "error": "fail"}, status_code=400)
    token = stash_jpeg(user.id, jpeg)
    payload = {"token": token, "kind": None, "title": None, "expires_on": None, "reason": None}
    if not vision_enabled():
        record(action="scan.read", status="fail", user=user, detail="vision off")
        return JSONResponse({"ok": False, "error": "off", **payload})
    try:
        result = read_document_photo(
            jpeg_for_vision(jpeg), "image/jpeg", locale=get_locale(request)
        )
    except VisionError as exc:
        record(action="scan.read", status="fail", user=user, detail=exc.code)
        return JSONResponse({"ok": False, "error": exc.code, **payload})
    except Exception:
        record(action="scan.read", status="fail", user=user, detail="fail")
        return JSONResponse({"ok": False, "error": "fail", **payload})
    kind = result.get("kind") or "-"
    expires = result.get("expires_on") or "no date"
    record(action="scan.read", user=user, detail=f"{kind} · {expires}")
    return JSONResponse({"ok": True, **payload, **result})


def _form_values(form: dict, existing: Document | None = None) -> dict:
    def flag(name: str, default: bool) -> bool:
        if name in form:
            return form.get(name) == "1"
        if existing is None:
            return default
        return bool(getattr(existing, name))

    return {
        "kind": form.get("kind") or (existing.kind if existing else "passport"),
        "title": clip(form.get("title") or "", TITLE_MAX),
        "expires_on": form.get("expires_on") or "",
        "notes": clip(form.get("notes") or "", NOTES_MAX),
        "remind_30": flag("remind_30", True),
        "remind_14": False,
        "remind_7": flag("remind_7", True),
        "remind_1": flag("remind_1", True),
    }


@app.post("/app/new")
async def new_submit(
    request: Request,
    kind: str = Form("other"),
    title: str = Form(""),
    expires_on: str = Form(""),
    notes: str = Form(""),
    remind_30: str | None = Form(None),
    remind_14: str | None = Form(None),
    remind_7: str | None = Form(None),
    remind_1: str | None = Form(None),
    photo: UploadFile | None = File(None),
    scan_token: str = Form(""),
    csrf_token: str = Form(""),
):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if over_free_limit(request, user):
        record(action="doc.limit", user=user, detail=f"free cap {FREE_DOCUMENT_LIMIT}")
        return loc_redirect(request, "/upgrade")
    form = {
        "kind": kind,
        "title": title,
        "expires_on": expires_on,
        "notes": notes,
        "remind_30": remind_30 or "",
        "remind_14": remind_14 or "",
        "remind_7": remind_7 or "",
        "remind_1": remind_1 or "",
    }
    values = _form_values(form)
    parsed = _parse_date(expires_on)
    if not parsed:
        return render(
            request,
            "document_form.html",
            mode="new",
            document=None,
            error="form.error_date",
            values=values,
            status_code=400,
        )
    photo_path, photo_error = save_photo(user.id, photo, scan_token)
    if photo_error:
        return render(
            request,
            "document_form.html",
            mode="new",
            document=None,
            error=photo_error,
            values=values,
            status_code=400,
        )
    kind_ok = kind if kind in dict(kinds(DEFAULT_LOCALE)) else "other"
    label = dict(kinds(get_locale(request))).get(kind_ok, kind_ok)
    doc = Document(
        user_id=user.id,
        kind=kind_ok,
        title=values["title"] or label,
        expires_on=parsed,
        notes=values["notes"],
        photo_path=photo_path,
        remind_30=values["remind_30"],
        remind_14=values["remind_14"],
        remind_7=values["remind_7"],
        remind_1=values["remind_1"],
    )
    request.state.db.add(doc)
    request.state.db.commit()
    record(
        action="doc.create",
        user=user,
        document_id=doc.id,
        detail=f"{doc.kind} · {doc.title} · {doc.expires_on.isoformat()}",
    )
    _sync_google(request, user, doc)
    request.state.db.commit()
    return loc_redirect(request, f"/app/{doc.id}")


@app.get("/app/{doc_id}")
def document_detail(request: Request, doc_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    doc = owned_document(request, user, doc_id)
    if not doc:
        return loc_redirect(request, "/app")
    summary, details = _calendar_copy(request, doc)
    return render(
        request,
        "document_detail.html",
        document=doc,
        google_url=google_calendar_url(summary, doc.expires_on, details),
        google_event_url=event_url(doc),
    )


@app.get("/app/{doc_id}/edit")
def edit_form(request: Request, doc_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    doc = owned_document(request, user, doc_id)
    if not doc:
        return loc_redirect(request, "/app")
    return render(
        request,
        "document_form.html",
        mode="edit",
        document=doc,
        error=None,
        values={
            "kind": doc.kind,
            "title": doc.title,
            "expires_on": doc.expires_on.isoformat(),
            "notes": doc.notes or "",
            "remind_30": doc.remind_30,
            "remind_14": doc.remind_14,
            "remind_7": doc.remind_7,
            "remind_1": doc.remind_1,
        },
    )


@app.post("/app/{doc_id}/edit")
async def edit_submit(
    request: Request,
    doc_id: int,
    kind: str = Form("other"),
    title: str = Form(""),
    expires_on: str = Form(""),
    notes: str = Form(""),
    remind_30: str | None = Form(None),
    remind_14: str | None = Form(None),
    remind_7: str | None = Form(None),
    remind_1: str | None = Form(None),
    photo: UploadFile | None = File(None),
    scan_token: str = Form(""),
    csrf_token: str = Form(""),
):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    doc = owned_document(request, user, doc_id)
    if not doc:
        return loc_redirect(request, "/app")
    form = {
        "kind": kind,
        "title": title,
        "expires_on": expires_on,
        "notes": notes,
        "remind_30": remind_30 or "",
        "remind_14": remind_14 or "",
        "remind_7": remind_7 or "",
        "remind_1": remind_1 or "",
    }
    values = _form_values(form, doc)
    parsed = _parse_date(expires_on)
    if not parsed:
        return render(
            request,
            "document_form.html",
            mode="edit",
            document=doc,
            error="form.error_date",
            values=values,
            status_code=400,
        )
    photo_path, photo_error = save_photo(user.id, photo, scan_token)
    if photo_error:
        return render(
            request,
            "document_form.html",
            mode="edit",
            document=doc,
            error=photo_error,
            values=values,
            status_code=400,
        )
    kind_ok = kind if kind in dict(kinds(DEFAULT_LOCALE)) else "other"
    label = dict(kinds(get_locale(request))).get(kind_ok, kind_ok)
    doc.kind = kind_ok
    doc.title = values["title"] or label
    doc.expires_on = parsed
    doc.notes = values["notes"]
    doc.remind_30 = values["remind_30"]
    doc.remind_14 = values["remind_14"]
    doc.remind_7 = values["remind_7"]
    doc.remind_1 = values["remind_1"]
    if photo_path:
        if doc.photo_path and doc.photo_path != photo_path:
            delete_photo(doc.photo_path)
        doc.photo_path = photo_path
    request.state.db.commit()
    record(
        action="doc.update",
        user=user,
        document_id=doc.id,
        detail=f"{doc.kind} · {doc.title} · {doc.expires_on.isoformat()}",
    )
    _sync_google(request, user, doc)
    request.state.db.commit()
    return loc_redirect(request, f"/app/{doc.id}")


@app.post("/app/{doc_id}/delete")
def delete_document(request: Request, doc_id: int, csrf_token: str = Form("")):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    doc = owned_document(request, user, doc_id)
    if doc:
        record(
            action="doc.delete",
            user=user,
            document_id=doc.id,
            detail=f"{doc.kind} · {doc.title}",
        )
        delete_event(user, doc)
        delete_photo(doc.photo_path)
        request.state.db.delete(doc)
        request.state.db.commit()
        flash_set(request, "flash.deleted")
    return loc_redirect(request, "/app")


@app.get("/app/{doc_id}/ics")
def download_ics(request: Request, doc_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    doc = owned_document(request, user, doc_id)
    if not doc:
        return loc_redirect(request, "/app")
    record(action="cal.ics", user=user, document_id=doc.id, detail=doc.title)
    locale = get_locale(request)
    summary = t(locale, "detail.ics_summary", title=doc.title)
    details = t(locale, "detail.ics_desc")
    if doc.notes:
        details = f"{details}\n{doc.notes}"
    body = document_to_ics(doc, summary, details)
    filename = f"snap-forget-{doc.id}.ics"
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/app/{doc_id}/photo")
def document_photo(request: Request, doc_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    doc = owned_document(request, user, doc_id)
    if not doc or not doc.photo_path:
        return Response(status_code=404)
    path = photo_on_disk(user.id, doc.photo_path)
    if not path:
        return Response(status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="document.jpg"',
        },
    )


@app.get("/upgrade")
def upgrade(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    heal_billing(request, user)
    record(action="upgrade.view", user=user)
    return render(
        request,
        "upgrade.html",
        payments_on=payments_ready(),
        can_manage=bool(user.is_paid and user.stripe_customer_id),
    )


@app.post("/upgrade/checkout")
def upgrade_checkout(request: Request, csrf_token: str = Form("")):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if user.is_paid:
        return loc_redirect(request, "/app")
    if not payments_ready():
        flash_set(request, "flash.pay_off")
        return loc_redirect(request, "/upgrade")
    if too_many(f"pay:{user.id}", 8, 3600):
        flash_set(request, "flash.pay_fail")
        return loc_redirect(request, "/upgrade")
    try:
        url = checkout_url(user, get_locale(request))
    except Exception as exc:
        record(action="pay.checkout", status="fail", user=user, detail=str(exc)[:400])
        flash_set(request, "flash.pay_fail")
        return loc_redirect(request, "/upgrade")
    record(action="pay.checkout", user=user)
    return RedirectResponse(url=url, status_code=303)


@app.get("/upgrade/success")
def upgrade_success(request: Request, session_id: str = ""):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if session_id:
        try:
            sync_checkout_session(request.state.db, user, session_id.strip())
        except Exception:
            record(action="pay.sync", status="fail", user=user, detail="session")
    request.state.db.refresh(user)
    if user.is_paid:
        flash_set(request, "flash.paid")
        return loc_redirect(request, "/app")
    return render(request, "upgrade.html", payments_on=payments_ready(), pending=True, can_manage=False)


@app.post("/app/billing")
def billing_portal(request: Request, csrf_token: str = Form("")):
    if not csrf_matches(request, csrf_token):
        return csrf_fail(request)
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        url = portal_url(user)
    except Exception:
        url = None
    if not url:
        flash_set(request, "flash.pay_fail")
        return loc_redirect(request, "/app/account")
    record(action="pay.portal", user=user)
    return RedirectResponse(url=url, status_code=303)


@app.post("/internal/stripe")
async def stripe_webhook(request: Request):
    if len(webhook_secret()) < 16:
        return JSONResponse({"ok": False, "error": "off"}, status_code=404)
    payload = await request.body()
    signature = request.headers.get("stripe-signature") or ""
    try:
        event = parse_webhook(payload, signature)
    except Exception:
        alert_ops(
            "webhook-sig",
            "Snap & Forget: Stripe webhook signature failed",
            "A call to /internal/stripe did not match STRIPE_WEBHOOK_SECRET. "
            "If you just rotated the secret, update .env. If this is random, ignore.",
            every_seconds=3600,
        )
        return JSONResponse({"ok": False, "error": "sig"}, status_code=400)
    result = apply_event(request.state.db, event)
    record(action="pay.webhook", detail=f"{event['type']} {result}")
    if result == "no-user":
        alert_ops(
            "webhook-nouser",
            "Snap & Forget: Stripe event had no matching user",
            f"Event {event['type']} did not match an account. "
            "Someone may have paid without getting access until they log in.",
            every_seconds=3600,
        )
    return JSONResponse({"ok": True})


def _parse_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat((value or "").strip())
    except ValueError:
        return None
    if parsed.year < 1980 or parsed.year > 2100:
        return None
    return parsed


@app.api_route("/internal/reminders", methods=["GET", "POST"])
def cron_reminders(request: Request):
    expected = (os.getenv("CRON_SECRET") or "").strip()
    if len(expected) < 16:
        return JSONResponse({"ok": False, "error": "off"}, status_code=404)
    header = request.headers.get("x-cron-secret") or ""
    if not cron_secret_ok(header, expected):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    if too_many("cron:reminders", 4, 60):
        return JSONResponse({"ok": False, "error": "limit"}, status_code=429)
    result = run_reminders()
    billing = result.get("billing") or {}
    record(
        action="reminder.job",
        detail=(
            f"sent={result.get('sent')} skipped={result.get('skipped')} "
            f"errors={result.get('errors')} stripe={billing.get('checked')}"
        ),
    )
    return JSONResponse(result)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
