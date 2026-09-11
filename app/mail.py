from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from dotenv import load_dotenv

from .activity import record, setup_logging
from .models import DATA_DIR, ROOT, MailLog, SessionLocal, init_db

load_dotenv(ROOT / ".env", override=True)

log = logging.getLogger("snapforget.mail")
MAIL_DIR = DATA_DIR / "mail"


class MailConfigError(RuntimeError):
    pass


def mail_from() -> str:
    return (os.getenv("MAIL_FROM") or "").strip()


def mail_reply_to() -> str:
    return (os.getenv("MAIL_REPLY_TO") or "").strip()


def ops_email() -> str:
    for key in ("OPERATOR_EMAIL", "MAIL_REPLY_TO", "LEGAL_EMAIL"):
        raw = (os.getenv(key) or "").strip()
        _name, addr = parseaddr(raw)
        addr = (addr or raw).strip().lower()
        if addr and "@" in addr and "." in addr.rsplit("@", 1)[-1]:
            return addr
    return ""


def alert_ops(kind: str, subject: str, text: str, *, every_seconds: int = 86400) -> bool:
    from .protect import too_many

    to_addr = ops_email()
    if not to_addr:
        record(action="ops.alert", status="fail", detail=f"no OPERATOR_EMAIL · {kind}"[:400])
        return False
    if too_many(f"ops:{kind}", 1, every_seconds):
        return False
    html_body = "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"
    try:
        send_mail(to_addr, subject, text, html_body, note=f"ops:{kind}")
    except Exception as exc:
        record(action="ops.alert", status="fail", detail=f"{kind} · {exc}"[:400])
        return False
    record(action="ops.alert", detail=kind)
    return True


def app_base_url() -> str:
    return (os.getenv("APP_BASE_URL") or "http://127.0.0.1:8767").rstrip("/")


def production_mode() -> bool:
    mode = (os.getenv("MAIL_MODE") or "dev").strip().lower()
    return mode in ("prod", "production")


def mail_configured() -> bool:
    return bool((os.getenv("RESEND_API_KEY") or "").strip() or (os.getenv("SMTP_HOST") or "").strip())


def _from_ok() -> bool:
    _name, addr = parseaddr(mail_from())
    if not addr or "@" not in addr:
        return False
    host = addr.rsplit("@", 1)[-1].lower()
    if host in ("localhost", "local"):
        return False
    return "." in host


def require_mail() -> None:
    if not mail_from() or not _from_ok():
        raise MailConfigError(
            "Set MAIL_FROM to a real address (e.g. Snap & Forget <reminders@your-domain.com>)."
        )
    if production_mode() and not mail_configured():
        raise MailConfigError(
            "MAIL_MODE=production needs RESEND_API_KEY or SMTP_HOST. "
            "Create a Resend account: https://resend.com/api-keys"
        )


def _log_row(
    *,
    to_email: str,
    subject: str,
    via: str,
    status: str,
    provider_id: str | None = None,
    error: str | None = None,
    document_id: int | None = None,
) -> None:
    init_db()
    db = SessionLocal()
    try:
        db.add(
            MailLog(
                to_email=to_email,
                subject=subject[:300],
                via=via,
                status=status,
                provider_id=(provider_id or "")[:80] or None,
                error=(error or "")[:400] or None,
                document_id=document_id,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("mail log write failed")
    finally:
        db.close()


def _write_dev(to_addr: str, subject: str, text: str, html_body: str) -> str:
    MAIL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = MAIL_DIR / f"{stamp}.txt"
    path.write_text(f"To: {to_addr}\nSubject: {subject}\n\n{text}\n", encoding="utf-8")
    (MAIL_DIR / f"{stamp}.html").write_text(html_body, encoding="utf-8")
    return path.name


def delete_mail_dumps(email: str) -> None:
    target = (email or "").strip().lower()
    if not target or not MAIL_DIR.is_dir():
        return
    needle = f"to: {target}"
    for path in MAIL_DIR.glob("*.txt"):
        try:
            first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
        except OSError:
            continue
        if not first or first[0].strip().lower() != needle:
            continue
        path.unlink(missing_ok=True)
        path.with_suffix(".html").unlink(missing_ok=True)


def _send_resend(to_addr: str, subject: str, text: str, html_body: str) -> str:
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    payload: dict = {
        "from": mail_from(),
        "to": [to_addr],
        "subject": subject,
        "text": text,
        "html": html_body,
    }
    reply = mail_reply_to()
    if reply:
        payload["reply_to"] = reply
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "snap-forget/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:500]
        try:
            parsed = json.loads(raw)
            detail = parsed.get("message") or parsed.get("name") or raw
        except json.JSONDecodeError:
            detail = raw
        raise RuntimeError(f"resend {exc.code}: {detail}") from exc
    return str(data.get("id") or "")


def _send_smtp(to_addr: str, subject: str, text: str, html_body: str) -> str:
    host = (os.getenv("SMTP_HOST") or "").strip()
    port = int(os.getenv("SMTP_PORT") or "587")
    user = os.getenv("SMTP_USER") or ""
    password = os.getenv("SMTP_PASSWORD") or ""
    name, addr = parseaddr(mail_from())
    msg = EmailMessage()
    msg["From"] = formataddr((name, addr)) if name else addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=addr.rsplit("@", 1)[-1])
    reply = mail_reply_to()
    if reply:
        msg["Reply-To"] = reply
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        if os.getenv("SMTP_STARTTLS", "1") != "0":
            smtp.starttls()
            smtp.ehlo()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)
    return ""


def send_mail(
    to_addr: str,
    subject: str,
    text: str,
    html_body: str,
    *,
    document_id: int | None = None,
    user_id: int | None = None,
    note: str = "",
) -> str:
    require_mail()
    to_addr = to_addr.strip().lower()
    via = "resend" if (os.getenv("RESEND_API_KEY") or "").strip() else "smtp" if (os.getenv("SMTP_HOST") or "").strip() else "file"
    bits = [p for p in (via, note, subject) if p]
    detail = " · ".join(bits)[:400]
    try:
        if via == "resend":
            provider_id = _send_resend(to_addr, subject, text, html_body)
        elif via == "smtp":
            provider_id = _send_smtp(to_addr, subject, text, html_body)
        else:
            provider_id = _write_dev(to_addr, subject, text, html_body)
        _log_row(
            to_email=to_addr,
            subject=subject,
            via=via,
            status="sent",
            provider_id=provider_id,
            document_id=document_id,
        )
        extra = f"{detail} · id={provider_id}" if provider_id else detail
        record(
            action="mail.sent",
            user_id=user_id,
            email=to_addr,
            document_id=document_id,
            detail=extra,
        )
        return via
    except Exception as exc:
        _log_row(
            to_email=to_addr,
            subject=subject,
            via=via,
            status="error",
            error=str(exc)[:400],
            document_id=document_id,
        )
        record(
            action="mail.sent",
            status="fail",
            user_id=user_id,
            email=to_addr,
            document_id=document_id,
            detail=f"{detail} · {exc}"[:400],
        )
        raise


def reminder_html(
    *,
    brand: str,
    heading: str,
    lead: str,
    when_line: str,
    cta: str,
    url: str,
    footer: str,
) -> str:
    safe_cta = html.escape(cta)
    lead_html = f'<p style="font-size:16px;line-height:1.5;">{html.escape(lead)}</p>' if lead else ""
    when_html = (
        f'<p style="font-size:16px;line-height:1.5;"><strong>{html.escape(when_line)}</strong></p>'
        if when_line
        else ""
    )
    return f"""<!DOCTYPE html>
<html><body style="margin:0;background:#f3eee4;font-family:Georgia,serif;color:#4a3224;">
  <div style="max-width:520px;margin:24px auto;padding:28px;background:#fffaf3;border:1px solid #e4d8c4;">
    <p style="letter-spacing:.12em;text-transform:uppercase;font-size:12px;color:#8a6a4a;">{html.escape(brand)}</p>
    <h1 style="font-size:26px;line-height:1.25;margin:0 0 12px;">{html.escape(heading)}</h1>
    {lead_html}
    {when_html}
    <p style="margin:28px 0 8px;">
      <a href="{html.escape(url)}" style="display:inline-block;background:#4a3224;color:#fffaf3;text-decoration:none;padding:12px 18px;">{safe_cta}</a>
    </p>
    <p style="font-size:13px;color:#8a6a4a;margin-top:28px;">{html.escape(footer)}</p>
  </div>
</body></html>"""


def send_test(to_addr: str) -> str:
    html_body = reminder_html(
        brand="Snap & Forget",
        heading="Test email",
        lead="If you can read this, outgoing mail works.",
        when_line="This is not a document reminder.",
        cta="Open the app",
        url=app_base_url() + "/app",
        footer="You can ignore this message.",
    )
    text = "Test email from Snap & Forget. Outgoing mail works."
    return send_mail(to_addr, "Snap & Forget — test", text, html_body)


def recent_logs(limit: int = 20) -> list[MailLog]:
    init_db()
    db = SessionLocal()
    try:
        return (
            db.query(MailLog)
            .order_by(MailLog.id.desc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()


def main() -> None:
    setup_logging()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:]
    if args and args[0] == "log":
        rows = recent_logs()
        if not rows:
            print("No mail log yet.")
            return
        for row in rows:
            print(
                f"{row.created_at}  {row.status:5}  {row.via:6}  "
                f"doc={row.document_id or '-'}  {row.to_email}  {row.subject}"
                + (f"  [{row.error}]" if row.error else "")
                + (f"  id={row.provider_id}" if row.provider_id else "")
            )
        return
    if args and args[0] == "test":
        if len(args) < 2:
            print("Usage: python -m app.mail test you@email.com")
            sys.exit(2)
        print(send_test(args[1]))
        return
    print("configured", mail_configured(), "production", production_mode(), "from", mail_from() or "(empty)")
    if not mail_configured():
        print("Add RESEND_API_KEY (https://resend.com/api-keys) or SMTP_* to .env")
        sys.exit(1)


if __name__ == "__main__":
    main()
