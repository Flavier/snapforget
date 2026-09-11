from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from .models import DATA_DIR, ROOT, EventLog, MailLog, SessionLocal, User, init_db

load_dotenv(ROOT / ".env", override=True)

log = logging.getLogger("snapforget")
LOG_DIR = DATA_DIR / "logs"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("snapforget")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=40,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream)
    root.propagate = False


def retention_days() -> int | None:
    raw = (os.getenv("EVENT_LOG_DAYS") or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def record(
    *,
    action: str,
    status: str = "ok",
    user: User | None = None,
    user_id: int | None = None,
    email: str | None = None,
    document_id: int | None = None,
    detail: str = "",
) -> None:
    uid = user_id if user_id is not None else (user.id if user else None)
    addr = (email or (user.email if user else None) or "").strip().lower() or None
    note = (detail or "").replace("\n", " ").strip()[:400]
    line = f"{status} {action} user={uid or '-'} {addr or '-'} {note}".strip()
    if status == "ok":
        log.info("%s", line)
    else:
        log.warning("%s", line)
    init_db()
    db = SessionLocal()
    try:
        db.add(
            EventLog(
                created_at=datetime.now(tz=timezone.utc).replace(tzinfo=None),
                user_id=uid,
                email=addr,
                action=action[:40],
                status=status[:12],
                document_id=document_id,
                detail=note,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("event log write failed")
    finally:
        db.close()


def recent(*, email: str | None = None, user_id: int | None = None, limit: int = 80) -> list[EventLog]:
    init_db()
    db = SessionLocal()
    try:
        q = db.query(EventLog)
        if user_id is not None:
            q = q.filter(EventLog.user_id == user_id)
        if email:
            q = q.filter(EventLog.email == email.strip().lower())
        return q.order_by(EventLog.id.desc()).limit(limit).all()
    finally:
        db.close()


def _fmt(row: EventLog) -> str:
    when = row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "-"
    who = row.email or (f"user={row.user_id}" if row.user_id else "-")
    doc = f" doc={row.document_id}" if row.document_id else ""
    extra = f"  {row.detail}" if row.detail else ""
    return f"{when}  {row.status:4}  {row.action:<18}  {who}{doc}{extra}"


def purge(*, days: int, apply: bool) -> dict:
    init_db()
    cutoff = datetime.utcnow() - timedelta(days=days)
    db = SessionLocal()
    try:
        events = db.query(EventLog).filter(EventLog.created_at < cutoff).count()
        mails = db.query(MailLog).filter(MailLog.created_at < cutoff).count()
        if apply:
            db.query(EventLog).filter(EventLog.created_at < cutoff).delete(synchronize_session=False)
            db.query(MailLog).filter(MailLog.created_at < cutoff).delete(synchronize_session=False)
            db.commit()
        return {"cutoff": cutoff.isoformat(timespec="seconds"), "events": events, "mails": mails, "applied": apply}
    finally:
        db.close()


def main() -> None:
    setup_logging()
    init_db()
    args = sys.argv[1:]
    if args and args[0] in ("user", "email"):
        if len(args) < 2:
            print("Usage: python -m app.activity user you@email.com")
            sys.exit(2)
        rows = recent(email=args[1], limit=200)
    elif args and args[0] == "purge":
        days = retention_days()
        apply = False
        i = 1
        while i < len(args):
            if args[i] == "--days" and i + 1 < len(args):
                days = int(args[i + 1])
                i += 2
                continue
            if args[i] in ("--yes", "--apply"):
                apply = True
                i += 1
                continue
            i += 1
        if not days:
            print("Logs are kept forever until you set EVENT_LOG_DAYS or pass --days.")
            print("Example later: python -m app.activity purge --days 730")
            print("Then apply:     python -m app.activity purge --days 730 --yes")
            sys.exit(0)
        result = purge(days=days, apply=apply)
        verb = "Deleted" if apply else "Would delete"
        print(
            f"{verb} {result['events']} events and {result['mails']} mail rows "
            f"older than {days} days (before {result['cutoff']} UTC)."
        )
        if not apply and (result["events"] or result["mails"]):
            print("Re-run with --yes to apply. Leave unset while you have few users.")
        return
    else:
        rows = recent(limit=80)
    if not rows:
        print("No events yet.")
        return
    for row in reversed(rows):
        print(_fmt(row))
    print(f"\n{len(rows)} row(s).  python -m app.activity user EMAIL")


if __name__ == "__main__":
    main()
