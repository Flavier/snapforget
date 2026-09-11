from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import Document


def _fold(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )
    return escaped


def document_to_ics(doc: Document, summary: str, description: str) -> str:
    uid = f"snapforget-{doc.id}-{doc.user_id}@snapandforget.app"
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    day = doc.expires_on.strftime("%Y%m%d")
    next_day = (doc.expires_on + timedelta(days=1)).strftime("%Y%m%d")
    alarms = []
    for days in doc.reminder_offsets():
        alarms.append(
            "\r\n".join(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{_fold(summary)}",
                    f"TRIGGER:-P{days}D",
                    "END:VALARM",
                ]
            )
        )
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Snap & Forget//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day}",
        f"DTEND;VALUE=DATE:{next_day}",
        f"SUMMARY:{_fold(summary)}",
        f"DESCRIPTION:{_fold(description)}",
        *alarms,
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(body)


def google_calendar_url(summary: str, expires_on: date, details: str) -> str:
    start = expires_on.strftime("%Y%m%d")
    end = (expires_on + timedelta(days=1)).strftime("%Y%m%d")
    from urllib.parse import quote

    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={quote(summary)}"
        f"&dates={start}/{end}"
        f"&details={quote(details)}"
    )
