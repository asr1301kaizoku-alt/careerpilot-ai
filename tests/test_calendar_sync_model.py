from pathlib import Path

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.integrations.calendar_sync_service import (
    CalendarSyncAlreadyExistsError,
    CalendarSyncService,
)
from app.models import Application, CalendarSync, ChecklistItem
from config import Config


def add_application_with_item():
    application = Application(
        company_name="Calendar Sync Test",
        status="面接",
        priority=3,
    )
    item = ChecklistItem(title="面接準備をする")
    application.checklist_items.append(item)
    db.session.add(application)
    db.session.commit()
    return application, item


def test_calendar_sync_model_supports_application_and_checklist_owners(app):
    with app.app_context():
        application, item = add_application_with_item()
        application_sync = CalendarSync(
            application=application,
            event_type=CalendarSync.EVENT_INTERVIEW,
            provider=CalendarSync.PROVIDER_GOOGLE,
            calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
            external_event_id="application-event",
        )
        checklist_sync = CalendarSync(
            checklist_item=item,
            event_type=CalendarSync.EVENT_CHECKLIST_DUE,
            provider=CalendarSync.PROVIDER_GOOGLE,
            calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
            external_event_id="checklist-event",
        )
        db.session.add_all([application_sync, checklist_sync])
        db.session.commit()

        assert application_sync.owner_type == CalendarSync.OWNER_APPLICATION
        assert application_sync.owner_id == application.id
        assert checklist_sync.owner_type == CalendarSync.OWNER_CHECKLIST_ITEM
        assert checklist_sync.owner_id == item.id


def test_calendar_sync_requires_exactly_one_database_owner(app):
    with app.app_context():
        sync = CalendarSync(
            event_type=CalendarSync.EVENT_INTERVIEW,
            provider=CalendarSync.PROVIDER_GOOGLE,
            calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
            external_event_id="orphan-event",
        )
        db.session.add(sync)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_calendar_sync_unique_constraint_prevents_duplicate_sync(app):
    with app.app_context():
        application, _ = add_application_with_item()
        values = {
            "application_id": application.id,
            "event_type": CalendarSync.EVENT_INTERVIEW,
            "provider": CalendarSync.PROVIDER_GOOGLE,
            "calendar_id": CalendarSync.DEFAULT_CALENDAR_ID,
        }
        db.session.add(
            CalendarSync(**values, external_event_id="first-event")
        )
        db.session.commit()
        db.session.add(
            CalendarSync(**values, external_event_id="second-event")
        )

        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert CalendarSync.query.count() == 1


def test_calendar_sync_service_rejects_duplicate_before_commit(app):
    with app.app_context():
        application, _ = add_application_with_item()
        service = CalendarSyncService()
        service.create_application_interview(application.id, "first-event")

        with pytest.raises(CalendarSyncAlreadyExistsError):
            service.create_application_interview(application.id, "second-event")
        assert CalendarSync.query.count() == 1


def test_application_delete_cascades_calendar_sync(app):
    with app.app_context():
        application, _ = add_application_with_item()
        CalendarSyncService().create_application_interview(
            application.id, "application-event"
        )

        db.session.delete(application)
        db.session.commit()

        assert CalendarSync.query.count() == 0


def test_checklist_delete_cascades_calendar_sync(app):
    with app.app_context():
        _, item = add_application_with_item()
        db.session.add(
            CalendarSync(
                checklist_item=item,
                event_type=CalendarSync.EVENT_CHECKLIST_DUE,
                provider=CalendarSync.PROVIDER_GOOGLE,
                calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
                external_event_id="checklist-event",
            )
        )
        db.session.commit()

        db.session.delete(item)
        db.session.commit()

        assert CalendarSync.query.count() == 0


def test_calendar_sync_migration_preserves_event_id_through_round_trip(tmp_path):
    database_path = tmp_path / "calendar-sync-migration.db"

    class MigrationConfig(Config):
        TESTING = True
        WTF_CSRF_ENABLED = False
        AUTO_CREATE_DATABASE = False
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"

    test_app = create_app(MigrationConfig)
    migrations_path = Path(__file__).resolve().parents[1] / "migrations"

    with test_app.app_context():
        upgrade(directory=str(migrations_path), revision="0004")
        db.session.execute(
            text(
                """
                INSERT INTO applications (
                    company_name, status, priority, created_at, updated_at,
                    google_calendar_event_id
                ) VALUES (
                    'Migration Test', '面接', 3, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, 'legacy-event-id'
                )
                """
            )
        )
        db.session.execute(
            text(
                """
                INSERT INTO checklist_items (
                    application_id, title, is_completed, sort_order,
                    created_at, updated_at
                ) VALUES (
                    1, 'Migration Checklist', 0, 0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        db.session.execute(
            text(
                """
                INSERT INTO google_credentials (
                    owner_key, provider, access_token, token_uri, scopes,
                    created_at, updated_at
                ) VALUES (
                    'migration-test', 'google', 'test-token',
                    'https://oauth2.googleapis.com/token', '[]',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        db.session.commit()

        upgrade(directory=str(migrations_path), revision="head")
        columns = {column["name"] for column in inspect(db.engine).get_columns("applications")}
        migrated = db.session.execute(
            text(
                """
                SELECT event_type, provider, calendar_id, external_event_id
                FROM calendar_syncs
                """
            )
        ).one()
        assert "google_calendar_event_id" not in columns
        assert tuple(migrated) == (
            "interview",
            "google",
            "primary",
            "legacy-event-id",
        )
        assert db.session.execute(
            text("SELECT COUNT(*) FROM checklist_items")
        ).scalar_one() == 1
        assert db.session.execute(
            text("SELECT COUNT(*) FROM google_credentials")
        ).scalar_one() == 1

        downgrade(directory=str(migrations_path), revision="0004")
        restored_event_id = db.session.execute(
            text("SELECT google_calendar_event_id FROM applications")
        ).scalar_one()
        assert restored_event_id == "legacy-event-id"
        assert db.session.execute(
            text("SELECT COUNT(*) FROM checklist_items")
        ).scalar_one() == 1

        upgrade(directory=str(migrations_path), revision="head")
        assert db.session.execute(
            text("SELECT COUNT(*) FROM calendar_syncs")
        ).scalar_one() == 1
        assert db.session.execute(
            text("SELECT COUNT(*) FROM checklist_items")
        ).scalar_one() == 1
        db.session.remove()
        db.engine.dispose()
