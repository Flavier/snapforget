from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "app.db"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True, default=None)
    privacy_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    google_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    google_access_token: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    google_token_exp: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None, index=True)
    documents: Mapped[list["Document"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    expires_on: Mapped[date] = mapped_column(Date, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    photo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    remind_30: Mapped[bool] = mapped_column(Boolean, default=True)
    remind_14: Mapped[bool] = mapped_column(Boolean, default=False)
    remind_7: Mapped[bool] = mapped_column(Boolean, default=True)
    remind_1: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    google_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)

    user: Mapped[User] = relationship(back_populates="documents")
    reminder_sends: Mapped[list["ReminderSend"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    def days_left(self, today: date | None = None) -> int:
        today = today or date.today()
        return (self.expires_on - today).days

    def status(self) -> str:
        days = self.days_left()
        if days < 0:
            return "overdue"
        if days <= 7:
            return "urgent"
        if days <= 30:
            return "soon"
        return "ok"

    def reminder_offsets(self) -> list[int]:
        offsets = []
        if self.remind_30:
            offsets.append(30)
        if self.remind_7:
            offsets.append(7)
        if self.remind_1:
            offsets.append(1)
        return offsets


class ReminderSend(Base):
    __tablename__ = "reminder_sends"
    __table_args__ = (
        UniqueConstraint("document_id", "offset_days", "expires_on", name="uq_reminder_once"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    offset_days: Mapped[int] = mapped_column(Integer)
    expires_on: Mapped[date] = mapped_column(Date)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document: Mapped["Document"] = relationship(back_populates="reminder_sends")


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MailLog(Base):
    __tablename__ = "mail_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    to_email: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(300), default="")
    via: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="sent")
    provider_id: Mapped[str | None] = mapped_column(String(80), nullable=True, default=None)
    error: Mapped[str | None] = mapped_column(String(400), nullable=True, default=None)
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class EventLog(Base):
    """Short customer-journey notes. Keep as long as EVENT_LOG_DAYS is unset."""

    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(12), default="ok")
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    detail: Mapped[str] = mapped_column(String(400), default="")


engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _add_column_if_missing(table: str, name: str, sql_type: str) -> None:
    cols = {col["name"] for col in inspect(engine).get_columns(table)}
    if name in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _add_column_if_missing("users", "google_refresh_token", "TEXT")
    _add_column_if_missing("users", "google_access_token", "TEXT")
    _add_column_if_missing("users", "google_token_exp", "INTEGER")
    _add_column_if_missing("documents", "google_event_id", "VARCHAR(128)")
    _add_column_if_missing("users", "locale", "VARCHAR(8)")
    _add_column_if_missing("users", "privacy_at", "DATETIME")
    _add_column_if_missing("users", "is_paid", "INTEGER DEFAULT 0")
    _add_column_if_missing("users", "stripe_customer_id", "VARCHAR(64)")
    _add_column_if_missing("users", "stripe_subscription_id", "VARCHAR(64)")
