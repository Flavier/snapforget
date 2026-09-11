from __future__ import annotations

import os
from typing import Any

import stripe
from dotenv import load_dotenv

from sqlalchemy import or_

from .mail import app_base_url
from .models import ROOT, User

_DEFAULT_CENTS = 1990
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

PAID_STATUSES = frozenset({"active", "trialing", "past_due"})
_STRIPE_LOCALE = {
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


def _secret() -> str:
    _reload_env()
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip()


def _price_id() -> str:
    _reload_env()
    return (os.getenv("STRIPE_PRICE_ID") or "").strip()


def amount_cents() -> int | None:
    _reload_env()
    raw = (os.getenv("STRIPE_AMOUNT_CENTS") or "").strip()
    if not raw:
        return None
    try:
        cents = int(raw)
    except ValueError:
        return None
    if cents < 50:
        return None
    return cents


def display_cents() -> int:
    return amount_cents() or _DEFAULT_CENTS


def format_price(locale: str) -> str:
    cents = display_cents()
    euros, rest = divmod(cents, 100)
    if locale == "en":
        return f"€{euros}.{rest:02d}"
    return f"{euros},{rest:02d} €"


def webhook_secret() -> str:
    _reload_env()
    return (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()


def payments_ready() -> bool:
    if not _secret().startswith("sk_"):
        return False
    if amount_cents() is not None:
        return True
    return _price_id().startswith("price_")


def _line_item() -> dict[str, Any]:
    cents = amount_cents()
    pid = _price_id()
    if cents is None:
        return {"price": pid, "quantity": 1}
    data: dict[str, Any] = {
        "currency": "eur",
        "unit_amount": cents,
        "recurring": {"interval": "year"},
    }
    if pid.startswith("price_"):
        data["product"] = stripe.Price.retrieve(pid).product
    else:
        data["product_data"] = {"name": "Snap & Forget"}
    return {"price_data": data, "quantity": 1}


def _api() -> None:
    stripe.api_key = _secret()


def drop_customer(customer_id: str) -> None:
    if not payments_ready() or not customer_id:
        return
    _api()
    try:
        stripe.Customer.delete(customer_id)
    except stripe.StripeError:
        try:
            stripe.Customer.modify(customer_id, metadata={"deleted": "1"})
        except stripe.StripeError:
            return


def checkout_url(user: User, locale: str) -> str:
    _api()
    params: dict[str, Any] = {
        "mode": "subscription",
        "success_url": app_base_url() + "/upgrade/success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": app_base_url() + "/upgrade",
        "client_reference_id": str(user.id),
        "line_items": [_line_item()],
        "allow_promotion_codes": True,
        "branding_settings": {"display_name": "Snap & Forget"},
        "metadata": {"user_id": str(user.id)},
        "subscription_data": {"metadata": {"user_id": str(user.id)}},
    }
    stripe_locale = _STRIPE_LOCALE.get(locale)
    if stripe_locale:
        params["locale"] = stripe_locale
    if user.stripe_customer_id:
        params["customer"] = user.stripe_customer_id
    else:
        params["customer_email"] = user.email
    session = stripe.checkout.Session.create(**params)
    url = session.url
    if not url:
        raise stripe.StripeError("no checkout url")
    return url


def portal_url(user: User) -> str | None:
    if not user.stripe_customer_id or not payments_ready():
        return None
    _api()
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=app_base_url() + "/app/account",
    )
    return session.url


def parse_webhook(payload: bytes, signature: str) -> stripe.Event:
    secret = webhook_secret()
    if len(secret) < 16:
        raise ValueError("webhook off")
    return stripe.Webhook.construct_event(payload, signature, secret)


def _id(value) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", None) or None


def _user_id_from(obj: dict) -> int | None:
    meta = obj.get("metadata") or {}
    raw = meta.get("user_id") or obj.get("client_reference_id")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def find_user(db, obj: dict) -> User | None:
    uid = _user_id_from(obj)
    if uid:
        user = db.get(User, uid)
        if user:
            return user
    customer = _id(obj.get("customer"))
    if customer:
        user = db.query(User).filter(User.stripe_customer_id == customer).one_or_none()
        if user:
            return user
    sub = _id(obj.get("subscription")) or _id(obj.get("id") if str(obj.get("object") or "") == "subscription" else None)
    if sub:
        return db.query(User).filter(User.stripe_subscription_id == sub).one_or_none()
    return None


def mark_paid(db, user: User, *, customer_id: str | None, subscription_id: str | None, paid: bool) -> None:
    if customer_id:
        user.stripe_customer_id = customer_id
    if subscription_id:
        user.stripe_subscription_id = subscription_id
    user.is_paid = paid
    db.commit()


def apply_event(db, event: stripe.Event) -> str:
    etype = event["type"]
    raw = event["data"]["object"]
    obj = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
    if etype == "checkout.session.completed":
        user = find_user(db, obj)
        if not user:
            return "no-user"
        if obj.get("mode") != "subscription":
            return "skip"
        mark_paid(
            db,
            user,
            customer_id=_id(obj.get("customer")),
            subscription_id=_id(obj.get("subscription")),
            paid=True,
        )
        return "paid"
    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        user = find_user(db, obj)
        if not user:
            return "no-user"
        status = obj.get("status") or ""
        paid = etype != "customer.subscription.deleted" and status in PAID_STATUSES
        mark_paid(
            db,
            user,
            customer_id=_id(obj.get("customer")),
            subscription_id=_id(obj.get("id")),
            paid=paid,
        )
        return "paid" if paid else "unpaid"
    if etype == "invoice.paid":
        user = find_user(db, obj)
        if not user:
            return "no-user"
        mark_paid(
            db,
            user,
            customer_id=_id(obj.get("customer")),
            subscription_id=_id(obj.get("subscription")),
            paid=True,
        )
        return "paid"
    return "ignore"


def _best_subscription(customer_id: str):
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=20)
    paid_sub = None
    other = None
    for sub in subs.data:
        if (sub.status or "") in PAID_STATUSES:
            paid_sub = sub
            break
        other = sub
    return paid_sub or other


def sync_user_from_stripe(db, user: User) -> str:
    if not payments_ready():
        return "skip"
    _api()
    customer_id = user.stripe_customer_id
    if not customer_id and user.stripe_subscription_id:
        try:
            hinted = stripe.Subscription.retrieve(user.stripe_subscription_id)
            customer_id = _id(hinted.customer)
        except stripe.InvalidRequestError:
            pass
    if not customer_id:
        listed = stripe.Customer.list(email=user.email, limit=3)
        if listed.data:
            customer_id = listed.data[0].id
    sub = _best_subscription(customer_id) if customer_id else None
    if sub is None:
        if customer_id and user.is_paid:
            mark_paid(db, user, customer_id=customer_id, subscription_id=None, paid=False)
            return "unpaid"
        if customer_id and not user.stripe_customer_id:
            user.stripe_customer_id = customer_id
            db.commit()
        return "skip"
    paid = (sub.status or "") in PAID_STATUSES
    was = bool(user.is_paid)
    mark_paid(
        db,
        user,
        customer_id=customer_id or _id(sub.customer),
        subscription_id=_id(sub.id) or user.stripe_subscription_id,
        paid=paid,
    )
    if paid and not was:
        return "paid"
    if not paid and was:
        return "unpaid"
    return "ok"


def reconcile_subscriptions(db) -> dict:
    if not payments_ready():
        return {"checked": 0, "paid": 0, "unpaid": 0, "errors": 0}
    rows = (
        db.query(User)
        .filter(
            or_(
                User.is_paid.is_(True),
                User.stripe_customer_id.isnot(None),
                User.stripe_subscription_id.isnot(None),
            )
        )
        .all()
    )
    paid = unpaid = errors = 0
    for user in rows:
        try:
            result = sync_user_from_stripe(db, user)
        except stripe.StripeError:
            errors += 1
            continue
        if result == "paid":
            paid += 1
        elif result == "unpaid":
            unpaid += 1
    return {"checked": len(rows), "paid": paid, "unpaid": unpaid, "errors": errors}


def sync_checkout_session(db, user: User, session_id: str) -> bool:
    if not session_id.startswith("cs_"):
        return False
    _api()
    session = stripe.checkout.Session.retrieve(session_id)
    if str(session.client_reference_id or "") != str(user.id):
        return False
    if session.status != "complete":
        return False
    mark_paid(
        db,
        user,
        customer_id=_id(session.customer),
        subscription_id=_id(session.subscription),
        paid=True,
    )
    return True
