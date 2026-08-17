from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.integrations import calendar_service
from app.integrations.calendar_service import GoogleCalendarService
from app.integrations.credential_store import GoogleCredentialStore
from app.models import Application, GoogleCredential
from config import Config


def make_credentials(token, refresh_token):
    return SimpleNamespace(
        token=token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["openid"],
        granted_scopes=None,
        expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


def test_google_credential_has_connection_type_and_composite_unique(app):
    with app.app_context():
        columns = inspect(db.engine).get_columns("google_credentials")
        connection_column = next(
            column for column in columns if column["name"] == "connection_type"
        )
        unique_names = {
            constraint["name"]
            for constraint in inspect(db.engine).get_unique_constraints(
                "google_credentials"
            )
        }

        assert connection_column["nullable"] is False
        assert (
            "uq_google_credentials_owner_provider_connection" in unique_names
        )


def test_same_owner_can_store_calendar_and_gmail_credentials(app):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        calendar_record = store.save_calendar_credential(
            make_credentials("calendar-access", "calendar-refresh"),
            email="calendar@example.com",
        )
        gmail_record = store.save_gmail_credential(
            make_credentials("gmail-access", "gmail-refresh"),
            email="gmail@example.com",
        )

        assert GoogleCredential.query.count() == 2
        assert calendar_record.id != gmail_record.id
        assert calendar_record.connection_type == "calendar"
        assert gmail_record.connection_type == "gmail"
        assert store.get_calendar_credential().google_account_email == (
            "calendar@example.com"
        )
        assert store.get_gmail_credential().google_account_email == (
            "gmail@example.com"
        )


def test_gmail_save_does_not_overwrite_calendar_credential(app):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        store.save_calendar_credential(
            make_credentials("calendar-access", "calendar-refresh")
        )
        store.save_gmail_credential(
            make_credentials("gmail-access", "gmail-refresh")
        )
        store.save_gmail_credential(
            make_credentials("gmail-access-new", None)
        )

        assert store.get_calendar_credential().access_token == "calendar-access"
        assert store.get_calendar_credential().refresh_token == (
            "calendar-refresh"
        )
        assert store.get_gmail_credential().access_token == "gmail-access-new"
        assert store.get_gmail_credential().refresh_token == "gmail-refresh"


def test_missing_gmail_credential_returns_none(app):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        store.save_calendar_credential(
            make_credentials("calendar-access", "calendar-refresh")
        )

        assert store.get_calendar_credential() is not None
        assert store.get_gmail_credential() is None


def test_duplicate_connection_type_is_rejected_by_unique_constraint(app):
    with app.app_context():
        base_values = {
            "owner_key": "duplicate-owner",
            "provider": "google",
            "connection_type": "calendar",
            "access_token": "test-access",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": "[]",
        }
        db.session.add(GoogleCredential(**base_values))
        db.session.commit()
        db.session.add(GoogleCredential(**base_values))

        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_calendar_service_uses_only_calendar_credential(app, monkeypatch):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        store.save_calendar_credential(
            make_credentials("calendar-access", "calendar-refresh")
        )
        store.save_gmail_credential(
            make_credentials("gmail-access", "gmail-refresh")
        )

        class FakeRequest:
            def execute(self):
                return {"id": "created-event"}

        class FakeEvents:
            def insert(self, **kwargs):
                return FakeRequest()

        class FakeCalendar:
            def events(self):
                return FakeEvents()

        def fake_build(api_name, version, **kwargs):
            assert (api_name, version) == ("calendar", "v3")
            assert kwargs["credentials"].token == "calendar-access"
            return FakeCalendar()

        monkeypatch.setattr(calendar_service, "build", fake_build)
        application = Application(
            company_name="Credential Test",
            status="面接",
            interview_at=datetime(2026, 8, 12, 10, 0),
        )

        event_id = GoogleCalendarService(
            store,
            "client-id",
            "client-secret",
        ).create_interview_event(application)

        assert event_id == "created-event"


def test_calendar_disconnect_preserves_gmail_credential(client, app):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        store.save_calendar_credential(
            make_credentials("calendar-access", "calendar-refresh")
        )
        store.save_gmail_credential(
            make_credentials("gmail-access", "gmail-refresh")
        )

    response = client.post(
        "/integrations/google/disconnect",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        assert store.get_calendar_credential() is None
        assert store.get_gmail_credential() is not None


def test_settings_separates_calendar_and_future_gmail_connections(client, app):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        store.save_calendar_credential(
            make_credentials("calendar-access", "calendar-refresh"),
            email="calendar@example.com",
        )
        store.save_gmail_credential(
            make_credentials("gmail-access", "gmail-refresh"),
            email="gmail@example.com",
        )

    html = client.get("/settings/integrations").get_data(as_text=True)

    assert "Googleカレンダー連携" in html
    assert "calendar@example.com" in html
    assert "Gmail連携" in html
    assert "Googleカレンダーと別のGoogleアカウントを利用できます。" in html
    assert "gmail@example.com" in html


def test_connection_type_migration_preserves_existing_credential(tmp_path):
    database_path = tmp_path / "google-credential-connection-migration.db"

    class MigrationConfig(Config):
        TESTING = True
        WTF_CSRF_ENABLED = False
        AUTO_CREATE_DATABASE = False
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"

    test_app = create_app(MigrationConfig)
    migrations_path = Path(__file__).resolve().parents[1] / "migrations"

    with test_app.app_context():
        upgrade(directory=str(migrations_path), revision="0005")
        db.session.execute(
            text(
                """
                INSERT INTO google_credentials (
                    owner_key, provider, google_account_email,
                    access_token, refresh_token, token_uri, scopes,
                    created_at, updated_at
                ) VALUES (
                    'migration-owner', 'google', 'calendar@example.com',
                    'preserved-access', 'preserved-refresh',
                    'https://oauth2.googleapis.com/token', '[]',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        db.session.commit()

        upgrade(directory=str(migrations_path), revision="head")
        migrated = db.session.execute(
            text(
                """
                SELECT connection_type, google_account_email,
                       access_token, refresh_token
                FROM google_credentials
                """
            )
        ).one()
        assert tuple(migrated) == (
            "calendar",
            "calendar@example.com",
            "preserved-access",
            "preserved-refresh",
        )

        downgrade(directory=str(migrations_path), revision="0005")
        downgraded_columns = {
            column["name"]
            for column in inspect(db.engine).get_columns("google_credentials")
        }
        downgraded = db.session.execute(
            text(
                """
                SELECT google_account_email, access_token, refresh_token
                FROM google_credentials
                """
            )
        ).one()
        assert "connection_type" not in downgraded_columns
        assert tuple(downgraded) == (
            "calendar@example.com",
            "preserved-access",
            "preserved-refresh",
        )

        upgrade(directory=str(migrations_path), revision="head")
        reupgraded = db.session.execute(
            text(
                """
                SELECT connection_type, access_token, refresh_token
                FROM google_credentials
                """
            )
        ).one()
        assert tuple(reupgraded) == (
            "calendar",
            "preserved-access",
            "preserved-refresh",
        )
        db.session.remove()
        db.engine.dispose()
