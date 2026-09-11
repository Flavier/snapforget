from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session, joinedload

from .activity import record, setup_logging
from .i18n import days_label, format_date, kinds, normalize_locale, t
from .mail import alert_ops, app_base_url, reminder_html, send_mail
from .billing import reconcile_subscriptions
from .models import DATA_DIR, Document, ReminderSend, SessionLocal, init_db

CRON_STAMP = DATA_DIR / "cron.stamp"
CRON_STALE_HOURS = 36

log = logging.getLogger("snapforget.reminders")
MAX_WINDOW = 30


def _already_sent(db: Session, doc: Document, offsets: list[int]) -> set[int]:
    if not offsets:
        return set()
    rows = (
        db.query(ReminderSend.offset_days)
        .filter(
            ReminderSend.document_id == doc.id,
            ReminderSend.expires_on == doc.expires_on,
            ReminderSend.offset_days.in_(offsets),
        )
        .all()
    )
    return {row[0] for row in rows}


def _mark_sent(db: Session, doc: Document, offsets: list[int]) -> None:
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    for offset in offsets:
        db.add(
            ReminderSend(
                document_id=doc.id,
                offset_days=offset,
                expires_on=doc.expires_on,
                sent_at=now,
            )
        )


def send_document_reminder(doc: Document, today: date) -> str:
    user = doc.user
    locale = normalize_locale(user.locale)
    days = doc.days_left(today)
    kind = dict(kinds(locale)).get(doc.kind, doc.kind)
    when = days_label(locale, days)
    subject = t(locale, "mail.subject", title=doc.title, when=when)
    heading = t(locale, "mail.heading")
    lead = t(locale, "mail.lead", kind=kind, title=doc.title)
    when_line = t(locale, "mail.when", date=format_date(locale, doc.expires_on), when=when)
    cta = t(locale, "mail.cta")
    footer = t(locale, "mail.footer")
    url = f"{app_base_url()}/app/{doc.id}"
    text = "\n".join([heading, "", lead, when_line, "", f"{cta}: {url}", "", footer])
    html_body = reminder_html(
        brand=t(locale, "brand"),
        heading=heading,
        lead=lead,
        when_line=when_line,
        cta=cta,
        url=url,
        footer=footer,
    )
    return send_mail(
        user.email,
        subject,
        text,
        html_body,
        document_id=doc.id,
        user_id=user.id,
        note=f"{days}d · {doc.title}",
    )


def due_offsets(doc: Document, today: date) -> list[int]:
    days = doc.days_left(today)
    if days < 0:
        return []
    return [offset for offset in doc.reminder_offsets() if days <= offset]


def touch_cron_stamp() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CRON_STAMP.write_text(datetime.now(tz=timezone.utc).isoformat(), encoding="utf-8")


def cron_stale() -> bool:
    if not CRON_STAMP.exists():
        return True
    try:
        stamp = datetime.fromisoformat(CRON_STAMP.read_text(encoding="utf-8").strip())
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = datetime.now(tz=timezone.utc) - stamp
    return age > timedelta(hours=CRON_STALE_HOURS)


def run_reminders(today: date | None = None) -> dict:
    init_db()
    today = today or date.today()
    db = SessionLocal()
    sent = 0
    skipped = 0
    errors = 0
    try:
        latest = today + timedelta(days=MAX_WINDOW)
        docs = (
            db.query(Document)
            .options(joinedload(Document.user), joinedload(Document.reminder_sends))
            .filter(Document.expires_on >= today, Document.expires_on <= latest)
            .all()
        )
        for doc in docs:
            due = due_offsets(doc, today)
            pending = [offset for offset in due if offset not in _already_sent(db, doc, due)]
            if not pending:
                skipped += 1
                continue
            try:
                via = send_document_reminder(doc, today)
                _mark_sent(db, doc, pending)
                db.commit()
                sent += 1
                log.info("reminded doc %s via %s", doc.id, via)
            except Exception as exc:
                db.rollback()
                errors += 1
                log.exception("reminder failed for document %s", doc.id)
                record(
                    action="reminder.sent",
                    status="fail",
                    user=doc.user,
                    document_id=doc.id,
                    detail=str(exc)[:200],
                )
        billing = {"checked": 0, "paid": 0, "unpaid": 0, "errors": 0}
        try:
            billing = reconcile_subscriptions(db)
        except Exception:
            log.exception("stripe reconcile failed")
            billing["errors"] = billing.get("errors", 0) + 1
        touch_cron_stamp()
        if errors:
            alert_ops(
                "reminder-errors",
                "Snap & Forget: reminder job had errors",
                f"sent={sent} skipped={skipped} errors={errors} today={today.isoformat()}",
                every_seconds=21600,
            )
        if billing.get("paid") or billing.get("unpaid") or billing.get("errors"):
            alert_ops(
                "billing-reconcile",
                "Snap & Forget: daily Stripe reconcile changed accounts",
                (
                    f"checked={billing.get('checked')} unlocked={billing.get('paid')} "
                    f"set-free={billing.get('unpaid')} stripe-errors={billing.get('errors')}"
                ),
                every_seconds=21600,
            )
        return {
            "sent": sent,
            "skipped": skipped,
            "errors": errors,
            "today": today.isoformat(),
            "billing": billing,
        }
    finally:
        db.close()


def main() -> None:
    setup_logging()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run_reminders()
    record(
        action="reminder.job",
        detail=f"sent={result.get('sent')} skipped={result.get('skipped')} errors={result.get('errors')}",
    )
    print(result)


if __name__ == "__main__":
    main()
