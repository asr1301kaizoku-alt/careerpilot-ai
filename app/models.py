from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlalchemy.engine import Engine

from .extensions import db


JST = timezone(timedelta(hours=9), name="JST")

STATUS_CHOICES = [
    "応募予定",
    "応募済み",
    "ES作成中",
    "ES提出済み",
    "Webテスト",
    "面接",
    "最終面接",
    "内定",
    "不合格",
    "辞退",
]


def now_jst_naive():
    return datetime.now(JST).replace(tzinfo=None)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Application(db.Model):
    __tablename__ = "applications"

    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(100), nullable=False)
    position_name = db.Column(db.String(150))
    application_url = db.Column(db.String(500))
    application_source = db.Column(db.String(100))
    status = db.Column(db.String(20), nullable=False, default="応募予定")
    es_deadline = db.Column(db.DateTime)
    web_test_deadline = db.Column(db.DateTime)
    interview_at = db.Column(db.DateTime)
    interview_format = db.Column(db.String(50))
    priority = db.Column(db.Integer, nullable=False, default=3)
    memo = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=now_jst_naive)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=now_jst_naive, onupdate=now_jst_naive
    )
    checklist_items = db.relationship(
        "ChecklistItem",
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    calendar_syncs = db.relationship(
        "CalendarSync",
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def nearest_deadline(self):
        deadlines = [
            deadline
            for deadline in (self.es_deadline, self.web_test_deadline)
            if deadline is not None
        ]
        return min(deadlines) if deadlines else None

    @property
    def checklist_total(self):
        return len(self.checklist_items)

    @property
    def checklist_completed(self):
        return sum(item.is_completed for item in self.checklist_items)

    @property
    def checklist_progress(self):
        if self.checklist_total == 0:
            return 0
        return round(self.checklist_completed * 100 / self.checklist_total)


class ChecklistItem(db.Model):
    __tablename__ = "checklist_items"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(150), nullable=False)
    due_at = db.Column(db.DateTime)
    is_completed = db.Column(db.Boolean, nullable=False, default=False, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=now_jst_naive)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=now_jst_naive, onupdate=now_jst_naive
    )
    completed_at = db.Column(db.DateTime)

    application = db.relationship("Application", back_populates="checklist_items")
    calendar_syncs = db.relationship(
        "CalendarSync",
        back_populates="checklist_item",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def toggle(self):
        self.is_completed = not self.is_completed
        self.completed_at = now_jst_naive() if self.is_completed else None

    def deadline_state(self, now=None):
        if self.due_at is None or self.is_completed:
            return "none"
        remaining = self.due_at - (now or now_jst_naive())
        if remaining.total_seconds() < 0:
            return "overdue"
        if remaining <= timedelta(days=3):
            return "urgent"
        if remaining <= timedelta(days=7):
            return "soon"
        return "later"


class CalendarSync(db.Model):
    __tablename__ = "calendar_syncs"
    __table_args__ = (
        db.CheckConstraint(
            "(application_id IS NOT NULL AND checklist_item_id IS NULL) "
            "OR (application_id IS NULL AND checklist_item_id IS NOT NULL)",
            name="ck_calendar_sync_exactly_one_owner",
        ),
        db.UniqueConstraint(
            "application_id",
            "event_type",
            "provider",
            name="uq_calendar_sync_application_event_provider",
        ),
        db.UniqueConstraint(
            "checklist_item_id",
            "event_type",
            "provider",
            name="uq_calendar_sync_checklist_event_provider",
        ),
    )

    OWNER_APPLICATION = "application"
    OWNER_CHECKLIST_ITEM = "checklist_item"
    EVENT_INTERVIEW = "interview"
    EVENT_ES_DEADLINE = "es_deadline"
    EVENT_WEB_TEST_DEADLINE = "web_test_deadline"
    EVENT_CHECKLIST_DUE = "checklist_due"
    PROVIDER_GOOGLE = "google"
    DEFAULT_CALENDAR_ID = "primary"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    checklist_item_id = db.Column(
        db.Integer,
        db.ForeignKey("checklist_items.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type = db.Column(db.String(50), nullable=False)
    provider = db.Column(db.String(50), nullable=False)
    calendar_id = db.Column(db.String(255), nullable=False)
    external_event_id = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now_jst_naive)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=now_jst_naive,
        onupdate=now_jst_naive,
    )

    application = db.relationship("Application", back_populates="calendar_syncs")
    checklist_item = db.relationship(
        "ChecklistItem", back_populates="calendar_syncs"
    )

    @property
    def owner_type(self):
        if self.application_id is not None:
            return self.OWNER_APPLICATION
        return self.OWNER_CHECKLIST_ITEM

    @property
    def owner_id(self):
        return self.application_id or self.checklist_item_id


class EmailCalendarRegistration(db.Model):
    """Track Google events created from reviewed Gmail AI candidates."""

    __tablename__ = "email_calendar_registrations"
    __table_args__ = (
        db.UniqueConstraint(
            "owner_key",
            "provider",
            "connection_key",
            "message_key",
            "event_type",
            name="uq_email_calendar_registration_source",
        ),
    )

    PROVIDER_GOOGLE = "google"
    DEFAULT_CALENDAR_ID = "primary"

    id = db.Column(db.Integer, primary_key=True)
    owner_key = db.Column(db.String(100), nullable=False, index=True)
    provider = db.Column(
        db.String(50),
        nullable=False,
        default=PROVIDER_GOOGLE,
    )
    connection_key = db.Column(db.String(64), nullable=False)
    message_key = db.Column(db.String(64), nullable=False, index=True)
    event_type = db.Column(db.String(50), nullable=False)
    calendar_id = db.Column(
        db.String(255),
        nullable=False,
        default=DEFAULT_CALENDAR_ID,
    )
    external_event_id = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now_jst_naive)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=now_jst_naive,
        onupdate=now_jst_naive,
    )


class GoogleCredential(db.Model):
    __tablename__ = "google_credentials"
    __table_args__ = (
        db.CheckConstraint(
            "connection_type IN ('calendar', 'gmail')",
            name="ck_google_credentials_connection_type",
        ),
        db.UniqueConstraint(
            "owner_key",
            "provider",
            "connection_type",
            name="uq_google_credentials_owner_provider_connection",
        ),
    )

    PROVIDER_GOOGLE = "google"
    CONNECTION_CALENDAR = "calendar"
    CONNECTION_GMAIL = "gmail"
    CONNECTION_TYPES = (CONNECTION_CALENDAR, CONNECTION_GMAIL)

    id = db.Column(db.Integer, primary_key=True)
    owner_key = db.Column(db.String(100), nullable=False, index=True)
    provider = db.Column(
        db.String(50), nullable=False, default=PROVIDER_GOOGLE
    )
    connection_type = db.Column(
        db.String(50),
        nullable=False,
        default=CONNECTION_CALENDAR,
        server_default=CONNECTION_CALENDAR,
    )
    google_account_email = db.Column(db.String(255))
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text)
    token_uri = db.Column(db.String(500), nullable=False)
    scopes = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=now_jst_naive)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=now_jst_naive,
        onupdate=now_jst_naive,
    )
