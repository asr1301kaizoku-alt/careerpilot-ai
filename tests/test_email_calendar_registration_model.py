from pathlib import Path

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.emails.calendar_registration_service import (
    EmailCalendarRegistrationService,
    EmailCalendarRegistrationStorageError,
)
from app.extensions import db
from app.models import EmailCalendarRegistration
from config import Config


class CalendarCredential:
    def __init__(self, email):
        self.google_account_email = email


def test_email_calendar_registration_columns_and_unique_source(app):
    with app.app_context():
        service = EmailCalendarRegistrationService("owner")
        credential = CalendarCredential("calendar@example.com")
        first, created = service.create(
            "gmail-message",
            "event_datetime",
            credential,
            "google-event-1",
        )
        duplicate, duplicate_created = service.create(
            "gmail-message",
            "event_datetime",
            credential,
            "google-event-2",
        )

        assert created is True
        assert duplicate_created is False
        assert duplicate.id == first.id
        assert EmailCalendarRegistration.query.count() == 1

        db.session.add(
            EmailCalendarRegistration(
                owner_key=first.owner_key,
                provider=first.provider,
                connection_key=first.connection_key,
                message_key=first.message_key,
                event_type=first.event_type,
                calendar_id="primary",
                external_event_id="google-event-3",
            )
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_different_calendar_connections_have_independent_tracking(app):
    with app.app_context():
        service = EmailCalendarRegistrationService("owner")
        first, _ = service.create(
            "gmail-message",
            "event_datetime",
            CalendarCredential("first@example.com"),
            "google-event-1",
        )
        second, _ = service.create(
            "gmail-message",
            "event_datetime",
            CalendarCredential("second@example.com"),
            "google-event-2",
        )

        assert first.connection_key != second.connection_key
        assert EmailCalendarRegistration.query.count() == 2


def test_registration_commit_failure_rolls_back(app, monkeypatch):
    with app.app_context():
        service = EmailCalendarRegistrationService("owner")
        real_rollback = db.session.rollback
        rollback_calls = []

        def fail_commit():
            raise RuntimeError("private commit details")

        def record_rollback():
            rollback_calls.append(True)
            real_rollback()

        monkeypatch.setattr(db.session, "commit", fail_commit)
        monkeypatch.setattr(db.session, "rollback", record_rollback)

        with pytest.raises(EmailCalendarRegistrationStorageError) as raised:
            service.create(
                "gmail-message",
                "event_datetime",
                CalendarCredential("calendar@example.com"),
                "google-event-1",
            )

        assert raised.value.stage == "db_commit"
        assert rollback_calls == [True]
        assert db.session.scalar(
            db.select(db.func.count(EmailCalendarRegistration.id))
        ) == 0


def test_registration_migration_preserves_existing_data_round_trip(tmp_path):
    database_path = tmp_path / "email-calendar-registration-migration.db"

    class MigrationConfig(Config):
        TESTING = True
        WTF_CSRF_ENABLED = False
        AUTO_CREATE_DATABASE = False
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"

    test_app = create_app(MigrationConfig)
    migrations_path = Path(__file__).resolve().parents[1] / "migrations"

    with test_app.app_context():
        upgrade(directory=str(migrations_path), revision="0006")
        db.session.execute(
            text(
                """
                INSERT INTO applications (
                    company_name, status, priority, created_at, updated_at
                ) VALUES (
                    'Migration Company', '応募済み', 3,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        db.session.execute(
            text(
                """
                INSERT INTO google_credentials (
                    owner_key, provider, connection_type, access_token,
                    token_uri, scopes, created_at, updated_at
                ) VALUES (
                    'migration-owner', 'google', 'calendar', 'token-value',
                    'https://oauth2.googleapis.com/token', '[]',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        db.session.commit()

        upgrade(directory=str(migrations_path), revision="head")
        assert inspect(db.engine).has_table("email_calendar_registrations")
        assert db.session.execute(
            text("SELECT COUNT(*) FROM applications")
        ).scalar_one() == 1
        assert db.session.execute(
            text("SELECT COUNT(*) FROM google_credentials")
        ).scalar_one() == 1

        db.session.execute(
            text(
                """
                INSERT INTO email_calendar_registrations (
                    owner_key, provider, connection_key, message_key,
                    event_type, calendar_id, external_event_id,
                    created_at, updated_at
                ) VALUES (
                    'migration-owner', 'google', 'connection-digest',
                    'message-digest', 'event_datetime', 'primary',
                    'event-id', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        db.session.commit()

        downgrade(directory=str(migrations_path), revision="0006")
        assert not inspect(db.engine).has_table(
            "email_calendar_registrations"
        )
        assert db.session.execute(
            text("SELECT COUNT(*) FROM applications")
        ).scalar_one() == 1
        assert db.session.execute(
            text("SELECT COUNT(*) FROM google_credentials")
        ).scalar_one() == 1

        upgrade(directory=str(migrations_path), revision="head")
        assert inspect(db.engine).has_table("email_calendar_registrations")
        assert db.session.execute(
            text("SELECT COUNT(*) FROM applications")
        ).scalar_one() == 1
        assert db.session.execute(
            text("SELECT COUNT(*) FROM google_credentials")
        ).scalar_one() == 1
        db.session.remove()
        db.engine.dispose()
