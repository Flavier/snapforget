from __future__ import annotations

import shutil
import re
import time
import uuid
from pathlib import Path

from .models import DATA_DIR, UPLOAD_DIR

# First production: local files on the same EU VPS as the app (data/uploads).
# Switch to S3-compatible object storage (Cloudflare R2 or Hetzner Object Storage)
# when there is more than one app process or the disk outgrows a single VPS.

TMP_DIR = DATA_DIR / "tmp"
TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
TMP_TTL_SECONDS = 2 * 60 * 60
MAX_STASH_PER_USER = 8


def ensure_storage() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_tmp()


def cleanup_tmp(now: float | None = None) -> None:
    now = now or time.time()
    if not TMP_DIR.exists():
        return
    for path in TMP_DIR.rglob("*.jpg"):
        try:
            if now - path.stat().st_mtime > TMP_TTL_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def stash_jpeg(user_id: int, jpeg: bytes) -> str:
    token = uuid.uuid4().hex
    folder = TMP_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    existing = sorted(folder.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    extra = len(existing) - MAX_STASH_PER_USER + 1
    for path in existing[: max(extra, 0)]:
        path.unlink(missing_ok=True)
    (folder / f"{token}.jpg").write_bytes(jpeg)
    return token


def persist_jpeg(user_id: int, jpeg: bytes) -> str:
    name = uuid.uuid4().hex
    folder = UPLOAD_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jpg"
    path.write_bytes(jpeg)
    return str(path.relative_to(DATA_DIR)).replace("\\", "/")


def claim_stash(user_id: int, token: str) -> str | None:
    token = (token or "").strip().lower()
    if not TOKEN_RE.match(token):
        return None
    src = TMP_DIR / str(user_id) / f"{token}.jpg"
    if not src.is_file():
        return None
    folder = UPLOAD_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{uuid.uuid4().hex}.jpg"
    src.replace(dest)
    return str(dest.relative_to(DATA_DIR)).replace("\\", "/")


def photo_on_disk(user_id: int, relative: str | None):
    if not relative:
        return None
    try:
        resolved = (DATA_DIR / relative).resolve()
        root = (UPLOAD_DIR / str(user_id)).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if resolved.is_file() and resolved.suffix.lower() in {".jpg", ".jpeg"}:
        return resolved
    return None


def delete_photo(relative: str | None) -> None:
    if not relative:
        return
    try:
        resolved = (DATA_DIR / relative).resolve()
        resolved.relative_to(UPLOAD_DIR.resolve())
    except (OSError, ValueError):
        return
    if resolved.is_file():
        resolved.unlink(missing_ok=True)


def delete_user_files(user_id: int) -> None:
    for root in (UPLOAD_DIR / str(user_id), TMP_DIR / str(user_id)):
        try:
            folder = root.resolve()
            folder.relative_to(DATA_DIR.resolve())
        except (OSError, ValueError):
            continue
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
