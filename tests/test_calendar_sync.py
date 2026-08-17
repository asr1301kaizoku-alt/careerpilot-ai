import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response
from sqlalchemy.exc import SQLAlchemyError

from app import create_app
from app.extensions import db
from app.integrations import calendar_routes, calendar_service
from app.integrations.calendar_service import (
    CalendarEventCancelledError,
    CalendarEventNotFoundError,
    CalendarServiceError,
    GoogleCalendarService,
    build_interview_event,
)
from app.integrations.credential_store import GoogleCredentialStore
from app.integrations.diagnostics import classify_google_api_error
from app.models import Application, CalendarSync, GoogleCredential
from config import TestConfig


def google_http_error(status):
    reason = "Gone" if status == 410 else "Not Found"
    response = Response({"status": str(status), "reason": reason})
    content = (
        '{"error":{"code":%d,"message":"sensitive Google response"}}'
        % status
    ).encode()
    return HttpError(
        response,
        content,
        uri="https://www.googleapis.com/calendar/v3/redacted",
    )


def add_application(*, interview_at=None, event_id=None):
    application = Application(
        company_name="Fictional Zenith Labs",
        position_name="AIエンジニア",
        status="面接",
        priority=5,
        interview_at=interview_at,
        memo="面接担当者への質問を確認",
    )
    db.session.add(application)
    db.session.flush()
    if event_id is not None:
        db.session.add(
            CalendarSync(
                application=application,
                event_type=CalendarSync.EVENT_INTERVIEW,
                provider=CalendarSync.PROVIDER_GOOGLE,
                calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
                external_event_id=event_id,
            )
        )
    db.session.commit()
    return application.id


def get_interview_sync(application_id):
    return db.session.scalar(
        db.select(CalendarSync).where(
            CalendarSync.application_id == application_id,
            CalendarSync.event_type == CalendarSync.EVENT_INTERVIEW,
            CalendarSync.provider == CalendarSync.PROVIDER_GOOGLE,
        )
    )


def add_connected_credential():
    credential = GoogleCredential(
        owner_key="test-user",
        provider="google",
        google_account_email="student@example.com",
        access_token="stored-access-token",
        refresh_token="stored-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        scopes='["https://www.googleapis.com/auth/calendar.events"]',
        expires_at=datetime(2099, 1, 1, 9, 0),
    )
    db.session.add(credential)
    db.session.commit()


class SuccessfulCalendarService:
    def __init__(self, event_id="google-event-123"):
        self.event_id = event_id
        self.calls = []
        self.update_calls = []
        self.delete_calls = []

    def create_interview_event(self, application):
        self.calls.append(application.id)
        return self.event_id

    def update_interview_event(self, application, event_id):
        self.update_calls.append((application.id, event_id))
        return event_id

    def delete_interview_event(self, event_id):
        self.delete_calls.append(event_id)


def test_calendar_event_creation_saves_event_id(client, app, monkeypatch):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 10, 14, 0)
        )
        add_connected_credential()

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )
    response = client.post(
        f"/applications/{application_id}/calendar/create",
        follow_redirects=True,
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Googleカレンダーへ登録しました。" in html
    assert "同期済み" in html
    assert "google-event-123" in html
    assert service.calls == [application_id]
    with app.app_context():
        assert get_interview_sync(application_id).external_event_id == "google-event-123"


def test_calendar_event_body_contains_interview_details():
    application = Application(
        company_name="Fictional Zenith Labs",
        position_name="AIエンジニア",
        status="最終面接",
        interview_at=datetime(2026, 8, 10, 14, 30),
        memo="ポートフォリオを準備",
    )

    event = build_interview_event(application)

    assert event["summary"] == "Fictional Zenith Labs 面接"
    assert event["start"] == {
        "dateTime": "2026-08-10T14:30:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    assert event["end"] == {
        "dateTime": "2026-08-10T15:30:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    assert "CareerPilot AIから登録" in event["description"]
    assert "会社名: Fictional Zenith Labs" in event["description"]
    assert "応募職種: AIエンジニア" in event["description"]
    assert "現在ステータス: 最終面接" in event["description"]
    assert "メモ: ポートフォリオを準備" in event["description"]
    assert "location" not in event


def test_unsynced_application_displays_calendar_create_button(client, app):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 10, 14, 0)
        )

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert "Googleカレンダー" in html
    assert "未同期" in html
    assert "Googleカレンダーへ登録" in html


def test_calendar_service_uses_primary_calendar_and_returns_event_id(
    monkeypatch,
):
    captured = {}
    credentials = SimpleNamespace(expired=False, refresh_token="refresh-token")
    record = SimpleNamespace(google_account_email="student@example.com")

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            assert saved_record is record
            assert client_id == "client-id"
            assert client_secret == "client-secret"
            return credentials

    class FakeInsert:
        def execute(self):
            return {"id": "created-event-id"}

    class FakeEvents:
        def insert(self, **kwargs):
            captured.update(kwargs)
            return FakeInsert()

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    def fake_build(api_name, version, **kwargs):
        assert (api_name, version) == ("calendar", "v3")
        assert kwargs["credentials"] is credentials
        assert kwargs["cache_discovery"] is False
        return FakeCalendar()

    monkeypatch.setattr(calendar_service, "build", fake_build)
    application = Application(
        company_name="Fictional Zenith Labs",
        status="面接",
        interview_at=datetime(2026, 8, 10, 14, 0),
    )

    event_id = GoogleCalendarService(
        FakeStore(), "client-id", "client-secret"
    ).create_interview_event(application)

    assert event_id == "created-event-id"
    assert captured["calendarId"] == "primary"
    assert captured["body"]["summary"] == "Fictional Zenith Labs 面接"


def test_expired_google_token_is_refreshed_and_saved(monkeypatch):
    saved = []
    credentials = SimpleNamespace(
        expired=True,
        refresh_token="refresh-token",
        token="expired-access-token",
    )
    record = SimpleNamespace(google_account_email="student@example.com")

    def refresh(request):
        credentials.expired = False
        credentials.token = "refreshed-access-token"

    credentials.refresh = refresh

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            return credentials

        def save_calendar_credential(
            self,
            refreshed_credentials,
            email=None,
        ):
            saved.append((refreshed_credentials.token, email))

    class FakeExecute:
        def execute(self):
            return {"id": "refreshed-event-id"}

    class FakeEvents:
        def insert(self, **kwargs):
            assert kwargs["body"]["summary"] == "更新確認株式会社 面接"
            return FakeExecute()

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(calendar_service, "Request", lambda: object())
    monkeypatch.setattr(
        calendar_service,
        "build",
        lambda *args, **kwargs: FakeCalendar(),
    )
    application = Application(
        company_name="更新確認株式会社",
        status="面接",
        interview_at=datetime(2026, 8, 10, 14, 0),
    )

    event_id = GoogleCalendarService(
        FakeStore(), "client-id", "client-secret"
    ).create_interview_event(application)

    assert event_id == "refreshed-event-id"
    assert saved == [("refreshed-access-token", "student@example.com")]


def test_stored_expiry_is_restored_for_google_auth(app):
    with app.app_context():
        add_connected_credential()
        store = GoogleCredentialStore("test-user")
        credentials = store.to_google_credentials(
            store.get_calendar_credential(),
            "client-id",
            "client-secret",
        )

        assert credentials.expiry == datetime(2099, 1, 1, 0, 0)
        assert credentials.expiry.tzinfo is None
        assert credentials.expired is False


def test_unconnected_user_is_redirected_to_integration_settings(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 10, 14, 0)
        )
    service = SuccessfulCalendarService()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/create",
    )

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.calls == []


def test_missing_interview_datetime_does_not_create_event(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    service = SuccessfulCalendarService()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/create",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "面接日時を登録してから" in response.get_data(as_text=True)
    assert service.calls == []


def test_calendar_api_error_is_safe_and_does_not_save_event(
    client,
    app,
    monkeypatch,
    caplog,
):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 10, 14, 0)
        )
        add_connected_credential()

    class FailingService:
        def create_interview_event(self, application):
            raise CalendarServiceError(
                "calendar_event_create",
                RuntimeError(
                    "access-token=secret-token code=secret-code "
                    "refresh-token=secret-refresh client-secret=secret-client"
                ),
            )

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: FailingService(),
    )
    with caplog.at_level("ERROR"):
        response = client.post(
            f"/applications/{application_id}/calendar/create",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "Googleカレンダー登録に失敗しました。" in response.get_data(
        as_text=True
    )
    assert "calendar_event_create" in caplog.text
    for secret in (
        "secret-token",
        "secret-code",
        "secret-refresh",
        "secret-client",
    ):
        assert secret not in caplog.text
    with app.app_context():
        assert get_interview_sync(application_id) is None


def test_existing_event_id_prevents_duplicate_creation(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 10, 14, 0),
            event_id="existing-event-id",
        )
        add_connected_credential()
    service = SuccessfulCalendarService()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/create",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "すでにGoogleカレンダーへ登録済みです。" in response.get_data(
        as_text=True
    )
    assert service.calls == []
    with app.app_context():
        assert get_interview_sync(application_id).external_event_id == "existing-event-id"


def test_calendar_create_is_post_only(client, app):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 10, 14, 0)
        )

    response = client.get(f"/applications/{application_id}/calendar/create")

    assert response.status_code == 405


def test_calendar_create_form_works_with_csrf_enabled(monkeypatch):
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "calendar-csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    service = SuccessfulCalendarService()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )
    with test_app.app_context():
        db.create_all()
        application_id = add_application(
            interview_at=datetime(2026, 8, 10, 14, 0)
        )
        add_connected_credential()

        response = client.post(
            f"/applications/{application_id}/calendar/create"
        )
        assert response.status_code == 400

        html = client.get(
            f"/applications/{application_id}"
        ).get_data(as_text=True)
        match = re.search(
            r'name="csrf_token" type="hidden" value="([^"]+)"',
            html,
        )
        assert match is not None
        response = client.post(
            f"/applications/{application_id}/calendar/create",
            data={"csrf_token": match.group(1)},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert service.calls == [application_id]
        assert get_interview_sync(application_id).external_event_id == "google-event-123"
        db.session.remove()
        db.drop_all()


def test_calendar_service_updates_gets_and_deletes_existing_event(monkeypatch):
    captured = []
    credentials = SimpleNamespace(expired=False, refresh_token="refresh-token")
    record = SimpleNamespace(google_account_email="student@example.com")

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            return credentials

    class FakeRequest:
        def __init__(self, operation, kwargs):
            self.operation = operation
            self.kwargs = kwargs

        def execute(self):
            captured.append((self.operation, self.kwargs))
            if self.operation == "get":
                return {
                    "id": self.kwargs["eventId"],
                    "status": "confirmed",
                }
            return None

    class FakeEvents:
        def patch(self, **kwargs):
            return FakeRequest("update", kwargs)

        def get(self, **kwargs):
            return FakeRequest("get", kwargs)

        def delete(self, **kwargs):
            return FakeRequest("delete", kwargs)

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(
        calendar_service,
        "build",
        lambda *args, **kwargs: FakeCalendar(),
    )
    application = Application(
        company_name="更新後株式会社",
        position_name="プロダクト職",
        status="最終面接",
        interview_at=datetime(2026, 8, 20, 10, 30),
        memo="役員への質問を準備",
    )
    service = GoogleCalendarService(FakeStore(), "client-id", "client-secret")

    assert service.update_interview_event(application, "existing-event-id") == (
        "existing-event-id"
    )
    assert service.get_interview_event("existing-event-id") == {
        "id": "existing-event-id",
        "status": "confirmed",
    }
    assert service.delete_interview_event("existing-event-id") is None

    assert captured[0] == (
        "get",
        {"calendarId": "primary", "eventId": "existing-event-id"},
    )
    update_operation, update_request = captured[1]
    assert update_operation == "update"
    assert update_request["calendarId"] == "primary"
    assert update_request["eventId"] == "existing-event-id"
    assert update_request["body"]["summary"] == "更新後株式会社 面接"
    assert update_request["body"]["start"]["dateTime"] == (
        "2026-08-20T10:30:00+09:00"
    )
    assert update_request["body"]["end"]["dateTime"] == (
        "2026-08-20T11:30:00+09:00"
    )
    assert "応募職種: プロダクト職" in update_request["body"]["description"]
    assert captured[2] == (
        "get",
        {"calendarId": "primary", "eventId": "existing-event-id"},
    )
    assert captured[3] == (
        "get",
        {"calendarId": "primary", "eventId": "existing-event-id"},
    )
    assert captured[4] == (
        "delete",
        {"calendarId": "primary", "eventId": "existing-event-id"},
    )


@pytest.mark.parametrize("status", [404, 410])
def test_calendar_service_classifies_missing_google_event(monkeypatch, status):
    credentials = SimpleNamespace(expired=False, refresh_token="refresh-token")
    record = SimpleNamespace(google_account_email="student@example.com")

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            return credentials

    class MissingEventRequest:
        def execute(self):
            raise google_http_error(status)

    class FakeEvents:
        def get(self, **kwargs):
            return MissingEventRequest()

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(
        calendar_service,
        "build",
        lambda *args, **kwargs: FakeCalendar(),
    )
    application = Application(
        company_name="404確認株式会社",
        status="面接",
        interview_at=datetime(2026, 8, 20, 10, 30),
    )

    with pytest.raises(CalendarEventNotFoundError) as error_info:
        GoogleCalendarService(
            FakeStore(), "client-id", "client-secret"
        ).update_interview_event(application, "missing-event-id")

    assert error_info.value.stage == "calendar_event_get"


def test_cancelled_event_is_not_patched(monkeypatch):
    credentials = SimpleNamespace(expired=False, refresh_token="refresh-token")
    record = SimpleNamespace(google_account_email="student@example.com")
    operations = []

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            return credentials

    class FakeRequest:
        def execute(self):
            return {
                "id": "redacted-event-id",
                "status": "cancelled",
                "summary": "sensitive event summary",
            }

    class FakeEvents:
        def get(self, **kwargs):
            operations.append("get")
            return FakeRequest()

        def patch(self, **kwargs):
            operations.append("patch")
            raise AssertionError("PATCH must not run for a cancelled event.")

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(
        calendar_service,
        "build",
        lambda *args, **kwargs: FakeCalendar(),
    )
    application = Application(
        company_name="キャンセル確認株式会社",
        status="面接",
        interview_at=datetime(2026, 8, 20, 10, 30),
    )

    with pytest.raises(CalendarEventNotFoundError) as error_info:
        GoogleCalendarService(
            FakeStore(), "client-id", "client-secret"
        ).update_interview_event(application, "redacted-event-id")

    assert error_info.value.stage == "calendar_event_status_check"
    assert operations == ["get"]


def test_cancelled_patch_response_is_not_success(monkeypatch):
    credentials = SimpleNamespace(expired=False, refresh_token="refresh-token")
    record = SimpleNamespace(google_account_email="student@example.com")

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            return credentials

    class FakeRequest:
        def __init__(self, event):
            self.event = event

        def execute(self):
            return self.event

    class FakeEvents:
        def get(self, **kwargs):
            return FakeRequest(
                {"id": "redacted-event-id", "status": "confirmed"}
            )

        def patch(self, **kwargs):
            return FakeRequest(
                {"id": "redacted-event-id", "status": "cancelled"}
            )

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(
        calendar_service,
        "build",
        lambda *args, **kwargs: FakeCalendar(),
    )
    application = Application(
        company_name="PATCH確認株式会社",
        status="面接",
        interview_at=datetime(2026, 8, 20, 10, 30),
    )

    with pytest.raises(CalendarEventNotFoundError) as error_info:
        GoogleCalendarService(
            FakeStore(), "client-id", "client-secret"
        ).update_interview_event(application, "redacted-event-id")

    assert error_info.value.stage == "calendar_event_status_check"


def test_event_without_status_is_not_patched(monkeypatch):
    credentials = SimpleNamespace(expired=False, refresh_token="refresh-token")
    record = SimpleNamespace(google_account_email="student@example.com")
    patch_calls = []

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            return credentials

    class FakeRequest:
        def execute(self):
            return {"id": "redacted-event-id"}

    class FakeEvents:
        def get(self, **kwargs):
            return FakeRequest()

        def patch(self, **kwargs):
            patch_calls.append(True)
            raise AssertionError("PATCH must not run without an event status.")

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(
        calendar_service,
        "build",
        lambda *args, **kwargs: FakeCalendar(),
    )
    application = Application(
        company_name="status未設定株式会社",
        status="面接",
        interview_at=datetime(2026, 8, 20, 10, 30),
    )

    with pytest.raises(CalendarServiceError) as error_info:
        GoogleCalendarService(
            FakeStore(), "client-id", "client-secret"
        ).update_interview_event(application, "redacted-event-id")

    assert error_info.value.stage == "calendar_event_status_check"
    assert patch_calls == []


def test_cancelled_event_is_not_deleted_again(monkeypatch):
    credentials = SimpleNamespace(expired=False, refresh_token="refresh-token")
    record = SimpleNamespace(google_account_email="student@example.com")
    operations = []

    class FakeStore:
        def get_calendar_credential(self):
            return record

        def to_google_credentials(self, saved_record, client_id, client_secret):
            return credentials

    class FakeRequest:
        def execute(self):
            return {"id": "redacted-event-id", "status": "cancelled"}

    class FakeEvents:
        def get(self, **kwargs):
            operations.append("get")
            return FakeRequest()

        def delete(self, **kwargs):
            operations.append("delete")
            raise AssertionError("DELETE must not run for a cancelled event.")

    class FakeCalendar:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(
        calendar_service,
        "build",
        lambda *args, **kwargs: FakeCalendar(),
    )

    with pytest.raises(CalendarEventNotFoundError) as error_info:
        GoogleCalendarService(
            FakeStore(), "client-id", "client-secret"
        ).delete_interview_event("redacted-event-id")

    assert error_info.value.stage == "calendar_event_status_check"
    assert operations == ["get"]


@pytest.mark.parametrize(
    ("status", "classification"),
    [
        (404, "not_found"),
        (410, "gone"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (429, "rate_limited"),
        (503, "server_error"),
    ],
)
def test_google_api_status_is_safely_classified(status, classification):
    error = RuntimeError("sensitive response")
    error.resp = SimpleNamespace(status=status)

    assert classify_google_api_error(error) == classification


def test_synced_calendar_event_can_be_updated(
    client,
    app,
    monkeypatch,
    caplog,
):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="existing-event-id",
        )
        add_connected_credential()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    with caplog.at_level("INFO"):
        response = client.post(
            f"/applications/{application_id}/calendar/update",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "Googleカレンダーの予定を更新しました。" in response.get_data(
        as_text=True
    )
    assert service.update_calls == [(application_id, "existing-event-id")]
    assert "operation=update stage=route_start" in caplog.text
    assert "operation=update stage=completed" in caplog.text
    with app.app_context():
        assert get_interview_sync(application_id).external_event_id == "existing-event-id"


def test_calendar_update_requires_interview_datetime(client, app, monkeypatch):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(event_id="existing-event-id")
        add_connected_credential()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/update",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "面接日時を登録してから" in response.get_data(as_text=True)
    assert service.update_calls == []


def test_calendar_update_requires_google_connection(client, app, monkeypatch):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="existing-event-id",
        )
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/update",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "先にGoogleカレンダーと連携してください。" in response.get_data(
        as_text=True
    )
    assert service.update_calls == []


def test_calendar_update_requires_event_id(client, app, monkeypatch):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30)
        )
        add_connected_credential()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/update",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Googleカレンダーへ未登録です。" in response.get_data(as_text=True)
    assert service.update_calls == []


@pytest.mark.parametrize(
    ("status", "classification"),
    [(404, "not_found"), (410, "gone")],
)
def test_calendar_update_missing_event_clears_event_id(
    client,
    app,
    monkeypatch,
    caplog,
    status,
    classification,
):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="missing-event-id",
        )
        add_connected_credential()

    create_calls = []

    class MissingEventService:
        def update_interview_event(self, application, event_id):
            GoogleCalendarService._raise_operation_error(
                "update",
                google_http_error(status),
            )

        def create_interview_event(self, application):
            create_calls.append(application.id)

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: MissingEventService(),
    )
    with caplog.at_level("WARNING"):
        response = client.post(
            f"/applications/{application_id}/calendar/update",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "同期状態を解除しました。再度登録してください。" in (
        response.get_data(as_text=True)
    )
    assert "operation=update" in caplog.text
    assert f"http_status={status}" in caplog.text
    assert f"api_error={classification}" in caplog.text
    assert "sync_state_cleared=true" in caplog.text
    assert "missing-event-id" not in caplog.text
    assert "sensitive Google response" not in caplog.text
    assert create_calls == []
    with app.app_context():
        assert get_interview_sync(application_id) is None


def test_calendar_update_cancelled_event_clears_sync_state(
    client,
    app,
    monkeypatch,
    caplog,
):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="cancelled-event-id",
        )
        add_connected_credential()
    create_calls = []

    class CancelledEventService:
        def update_interview_event(self, application, event_id):
            raise CalendarEventNotFoundError(
                "calendar_event_status_check",
                CalendarEventCancelledError(),
            )

        def create_interview_event(self, application):
            create_calls.append(application.id)

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: CancelledEventService(),
    )
    with caplog.at_level("WARNING"):
        response = client.post(
            f"/applications/{application_id}/calendar/update",
            follow_redirects=True,
        )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Googleカレンダー上の予定が削除されていたため" in html
    assert "同期状態を解除しました。再度登録してください。" in html
    assert "未同期" in html
    assert "stage=calendar_event_status_check" in caplog.text
    assert "event_status=cancelled" in caplog.text
    assert "sync_state_cleared=true" in caplog.text
    assert "cancelled-event-id" not in caplog.text
    assert create_calls == []
    with app.app_context():
        assert get_interview_sync(application_id) is None


def test_calendar_update_api_error_preserves_event_id(
    client,
    app,
    monkeypatch,
    caplog,
):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="existing-event-id",
        )
        add_connected_credential()

    class FailingUpdateService:
        def update_interview_event(self, application, event_id):
            error = RuntimeError("memo全文 secret-token existing-event-id")
            error.resp = SimpleNamespace(status=503)
            raise CalendarServiceError("calendar_event_get", error)

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: FailingUpdateService(),
    )
    with caplog.at_level("ERROR"):
        response = client.post(
            f"/applications/{application_id}/calendar/update",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "Googleカレンダーの予定を更新できませんでした。" in (
        response.get_data(as_text=True)
    )
    assert "operation=update" in caplog.text
    assert "stage=calendar_event_get" in caplog.text
    assert "api_error=server_error" in caplog.text
    for secret in ("memo全文", "secret-token", "existing-event-id"):
        assert secret not in caplog.text
    with app.app_context():
        assert get_interview_sync(application_id).external_event_id == "existing-event-id"


def test_calendar_update_clear_commit_failure_displays_flash(
    client,
    app,
    monkeypatch,
    caplog,
):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="existing-event-id",
        )
        add_connected_credential()

    class MissingEventService:
        def update_interview_event(self, application, event_id):
            GoogleCalendarService._raise_operation_error(
                "update",
                google_http_error(410),
            )

    def fail_commit():
        raise SQLAlchemyError("sensitive database details")

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: MissingEventService(),
    )
    monkeypatch.setattr(db.session, "commit", fail_commit)
    with caplog.at_level("ERROR"):
        response = client.post(
            f"/applications/{application_id}/calendar/update",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "Googleカレンダーの同期状態を更新できませんでした。" in (
        response.get_data(as_text=True)
    )
    assert "stage=calendar_sync_delete" in caplog.text
    assert "sync_state_cleared=false" in caplog.text
    assert "sensitive database details" not in caplog.text
    with app.app_context():
        assert get_interview_sync(application_id).external_event_id == "existing-event-id"


def test_calendar_event_can_be_deleted(client, app, monkeypatch):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="existing-event-id",
        )
        add_connected_credential()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/delete",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Googleカレンダーの予定を削除しました。" in response.get_data(
        as_text=True
    )
    assert service.delete_calls == ["existing-event-id"]
    with app.app_context():
        assert get_interview_sync(application_id) is None


@pytest.mark.parametrize("status", [404, 410])
def test_calendar_delete_missing_event_clears_event_id(
    client,
    app,
    monkeypatch,
    status,
):
    with app.app_context():
        application_id = add_application(event_id="missing-event-id")
        add_connected_credential()

    class MissingEventService:
        def delete_interview_event(self, event_id):
            GoogleCalendarService._raise_operation_error(
                "delete",
                google_http_error(status),
            )

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: MissingEventService(),
    )

    response = client.post(
        f"/applications/{application_id}/calendar/delete",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "すでに削除されていたため、同期状態を解除しました。" in (
        response.get_data(as_text=True)
    )
    with app.app_context():
        assert get_interview_sync(application_id) is None


def test_calendar_delete_cancelled_event_clears_event_id(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        application_id = add_application(event_id="cancelled-event-id")
        add_connected_credential()
    delete_calls = []

    class CancelledEventService:
        def delete_interview_event(self, event_id):
            delete_calls.append(event_id)
            raise CalendarEventNotFoundError(
                "calendar_event_status_check",
                CalendarEventCancelledError(),
            )

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: CancelledEventService(),
    )

    response = client.post(
        f"/applications/{application_id}/calendar/delete",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "同期状態を解除しました。" in response.get_data(as_text=True)
    assert delete_calls == ["cancelled-event-id"]
    with app.app_context():
        assert get_interview_sync(application_id) is None


def test_calendar_delete_requires_google_connection(client, app, monkeypatch):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(event_id="existing-event-id")
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/calendar/delete"
    )

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.delete_calls == []


def test_calendar_delete_api_error_preserves_event_id(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        application_id = add_application(event_id="existing-event-id")
        add_connected_credential()

    class FailingDeleteService:
        def delete_interview_event(self, event_id):
            error = RuntimeError("sensitive Google response")
            error.resp = SimpleNamespace(status=403)
            raise CalendarServiceError("calendar_event_delete", error)

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: FailingDeleteService(),
    )

    response = client.post(
        f"/applications/{application_id}/calendar/delete",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Googleカレンダーの予定を削除できませんでした。" in (
        response.get_data(as_text=True)
    )
    with app.app_context():
        assert get_interview_sync(application_id).external_event_id == "existing-event-id"


@pytest.mark.parametrize("action", ["update", "delete"])
def test_calendar_update_and_delete_are_post_only(client, app, action):
    with app.app_context():
        application_id = add_application(event_id="existing-event-id")

    response = client.get(f"/applications/{application_id}/calendar/{action}")

    assert response.status_code == 405


def test_synced_calendar_ui_displays_actions_and_delete_warnings(client, app):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="existing-event-id",
        )

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert "Googleカレンダーを更新" in html
    assert "Googleカレンダーから削除" in html
    assert "応募先情報を変更した場合は" in html
    assert "CareerPilot AIの応募先情報は削除されません。" in html
    assert "Googleカレンダーへ登録済みの予定は自動削除されません。" in html
    assert 'id="deleteCalendarEventModal"' in html


def test_application_delete_does_not_delete_google_event(
    client,
    app,
    monkeypatch,
):
    service = SuccessfulCalendarService()
    with app.app_context():
        application_id = add_application(event_id="existing-event-id")
        add_connected_credential()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/applications/{application_id}/delete",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert service.delete_calls == []
    with app.app_context():
        assert db.session.get(Application, application_id) is None
        assert CalendarSync.query.count() == 0


def test_calendar_update_and_delete_forms_work_with_csrf_enabled(monkeypatch):
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "calendar-actions-csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    service = SuccessfulCalendarService()
    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: service,
    )
    with test_app.app_context():
        db.create_all()
        application_id = add_application(
            interview_at=datetime(2026, 8, 20, 10, 30),
            event_id="existing-event-id",
        )
        add_connected_credential()
        update_path = f"/applications/{application_id}/calendar/update"
        delete_path = f"/applications/{application_id}/calendar/delete"

        assert client.post(update_path).status_code == 400
        assert client.post(delete_path).status_code == 400
        assert service.update_calls == []
        assert service.delete_calls == []

        html = client.get(
            f"/applications/{application_id}"
        ).get_data(as_text=True)
        update_form = re.search(
            rf'<form class="d-grid" method="post" '
            rf'action="{re.escape(update_path)}">(.*?)</form>',
            html,
            re.DOTALL,
        )
        assert update_form is not None
        assert 'name="csrf_token"' in update_form.group(1)
        assert 'type="submit"' in update_form.group(1)
        assert "<form" not in update_form.group(1)
        token = re.search(
            r'name="csrf_token" type="hidden" value="([^"]+)"',
            html,
        ).group(1)
        update_response = client.post(
            update_path,
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert update_response.status_code == 200
        assert service.update_calls == [(application_id, "existing-event-id")]

        delete_response = client.post(
            delete_path,
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert delete_response.status_code == 200
        assert service.delete_calls == ["existing-event-id"]
        assert get_interview_sync(application_id) is None
        db.session.remove()
        db.drop_all()
