from __future__ import annotations

import os
from email.utils import parseaddr

from .mail import mail_from, mail_reply_to


def legal_contact() -> dict[str, str]:
    name = (os.getenv("LEGAL_NAME") or "").strip() or "Snap & Forget"
    email = (os.getenv("LEGAL_EMAIL") or "").strip()
    if not email:
        email = (mail_reply_to() or parseaddr(mail_from())[1] or "").strip()
    address = (os.getenv("LEGAL_ADDRESS") or "").strip()
    return {"name": name, "email": email, "address": address}
