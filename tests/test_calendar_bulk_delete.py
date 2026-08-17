import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from app import create_app
from app.extensions import db
from app.integrations import calendar_routes
from app.integrations.calendar_service import (
    CalendarEventCancelledError,
    CalendarEventNotFoundError,
    CalendarServiceError,
    GoogleCalendarService,
)
from app.models import Application, CalendarSync, GoogleCredential
from config import TestConfig


DEFAULT_INTERVIEW_AT = datetime(2026, 8, 12, 12, 30)
DEFAULT_ES_DEADLINE = datetime(2026, 8, 9, 23, 59)
DEFAULT_WEB_TEST_DEADLINE = datetime(2026, 8, 10, 23, 59)
ALL_EVENT_TYPES = (
    CalendarSync.EVENT_INTERVIEW,
    CalendarSync.EVENT_ES_DEADLINE,
    CalendarSync.EVENT_WEB_TEST_DEADLINE,
)


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


def add_application(
    *,
    interview_at=DEFAULT_INTERVIEW_AT,
    es_deadline=DEFAULT_ES_DEADLINE,
    web_test_deadline=DEFAULT_WEB_TEST_DEADLINE,
):
    application = Application(
        company_name="Bulk Delete Test",
        position_name="エンジニア",
        application_source="企業サイト",
        status="面接",
        priority=4,
        interview_at=interview_at,
        es_deadline=es_deadline,
        web_test_deadline=web_test_deadline,
        memo="一括削除後も残すメモ",
    )
    db.session.add(application)
    db.session.commit()
    return application.id


def add_connected_credential():
    db.session.add(
        GoogleCredential(
            owner_key="test-user",
            provider="google",
            google_account_email="student@example.com",
            access_token="stored-access-token",
            refresh_token="stored-refresh-token",
            token_uri="https://oauth2.googleapis.com/token",
            scopes='["https://www.googleapis.com/auth/calendar.events"]',
            expires_at=datetime(2099, 1, 1, 9, 0),
        )
    )
    db.session.commit()


def add_sync(application_id, event_type, event_id=None):
    db.session.add(
        CalendarSync(
            application_id=application_id,
            event_type=event_type,
            provider=CalendarSync.PROVIDER_GOOGLE,
            calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
            external_event_id=event_id or event_type,
        )
    )
    db.session.commit()


def add_syncs(application_id, event_types=ALL_EVENT_TYPES):
    for event_type in event_types:
        add_sync(application_id, event_type)


def get_sync_types(application_id):
    return {
        sync.event_type
        for sync in db.session.scalars(
            db.select(CalendarSync).where(
                CalendarSync.application_id == application_id,
                CalendarSync.provider == CalendarSync.PROVIDER_GOOGLE,
            )
        ).all()
    }


class RecordingDeleteCalendarService:
    def __init__(self, remote_states=None):
        self.remote_states = remote_states or {}
        self.delete_calls = []
        self.delete_execute_calls = []

    def delete_calendar_event(self, event_id):
        self.delete_calls.append(event_id)
        remote_state = self.remote_states.get(event_id, "confirmed")
        if remote_state in {404, 410}:
            GoogleCalendarService._raise_operation_error(
                "get",
                google_http_error(remote_state),
            )
        if remote_state == "cancelled":
            raise CalendarEventNotFoundError(
                "calendar_event_status_check",
                CalendarEventCancelledError(),
            )
        if remote_state == "error":
            error = RuntimeError(
                "secret-access-token secret-client-secret "
                "secret-event-id secret-event-body"
            )
            error.resp = SimpleNamespace(status=503)
            raise CalendarServiceError("calendar_event_delete", error)
        self.delete_execute_calls.append(event_id)


def post_bulk_delete(client, application_id):
    return client.post(
        f"/applications/{application_id}/calendar/bulk-delete",
        follow_redirects=True,
    )


def test_bulk_delete_removes_all_three_google_events_and_syncs(
    client,
    app,
    monkeypatch,
):
    service = RecordingDeleteCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_delete(client, application_id)

    assert response.status_code == 200
    assert "Googleカレンダーの予定を3件削除しました。" in response.get_data(
        as_text=True
    )
    assert service.delete_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    assert service.delete_execute_calls == service.delete_calls
    with app.app_context():
        assert get_sync_types(application_id) == set()
        application = db.session.get(Application, application_id)
        assert application is not None
        assert application.company_name == "Bulk Delete Test"
        assert application.position_name == "エンジニア"
        assert application.application_source == "企業サイト"
        assert application.status == "面接"
        assert application.memo == "一括削除後も残すメモ"
        assert application.interview_at == DEFAULT_INTERVIEW_AT
        assert application.es_deadline == DEFAULT_ES_DEADLINE
        assert application.web_test_deadline == DEFAULT_WEB_TEST_DEADLINE


def test_bulk_delete_removes_only_the_synced_interview(client, app, monkeypatch):
    service = RecordingDeleteCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_sync(application_id, CalendarSync.EVENT_INTERVIEW)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_delete(client, application_id)

    assert service.delete_calls == [CalendarSync.EVENT_INTERVIEW]
    assert "Googleカレンダーの予定を1件削除しました。" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert get_sync_types(application_id) == set()


def test_bulk_delete_calls_no_api_when_no_calendar_sync_exists(
    client,
    app,
    monkeypatch,
):
    service = RecordingDeleteCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_delete(client, application_id)
    html = response.get_data(as_text=True)

    assert service.delete_calls == []
    assert "削除できる同期済みの予定はありません。" in html
    assert "Googleカレンダーから一括削除" not in html


def test_bulk_delete_targets_syncs_even_when_application_datetimes_are_missing(
    client,
    app,
    monkeypatch,
):
    service = RecordingDeleteCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=None,
            es_deadline=None,
            web_test_deadline=None,
        )
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_delete(client, application_id)

    assert response.status_code == 200
    assert service.delete_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
    ]
    with app.app_context():
        assert get_sync_types(application_id) == set()
        application = db.session.get(Application, application_id)
        assert application.interview_at is None
        assert application.es_deadline is None
        assert application.web_test_deadline is None


@pytest.mark.parametrize("remote_state", (404, 410, "cancelled"))
def test_already_deleted_google_event_removes_calendar_sync_without_delete(
    client,
    app,
    monkeypatch,
    remote_state,
):
    service = RecordingDeleteCalendarService(
        {CalendarSync.EVENT_ES_DEADLINE: remote_state}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_sync(application_id, CalendarSync.EVENT_ES_DEADLINE)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_delete(client, application_id)
    html = response.get_data(as_text=True)

    assert service.delete_calls == [CalendarSync.EVENT_ES_DEADLINE]
    assert service.delete_execute_calls == []
    assert "1件はGoogleカレンダー上ですでに削除されていました。" in html
    with app.app_context():
        assert get_sync_types(application_id) == set()


def test_general_bulk_delete_error_preserves_calendar_sync(
    client,
    app,
    monkeypatch,
):
    service = RecordingDeleteCalendarService(
        {CalendarSync.EVENT_ES_DEADLINE: "error"}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_sync(application_id, CalendarSync.EVENT_ES_DEADLINE)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_delete(client, application_id)

    assert "1件は削除できませんでした。" in response.get_data(as_text=True)
    with app.app_context():
        assert get_sync_types(application_id) == {
            CalendarSync.EVENT_ES_DEADLINE
        }


def test_bulk_delete_keeps_failed_sync_and_removes_successful_syncs(
    client,
    app,
    monkeypatch,
):
    service = RecordingDeleteCalendarService(
        {
            CalendarSync.EVENT_WEB_TEST_DEADLINE: 404,
            CalendarSync.EVENT_INTERVIEW: "error",
        }
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_delete(client, application_id)
    html = response.get_data(as_text=True)

    assert "Googleカレンダーの予定を1件削除しました。" in html
    assert "1件はGoogleカレンダー上ですでに削除されていました。" in html
    assert "1件は削除できませんでした。" in html
    with app.app_context():
        assert get_sync_types(application_id) == {
            CalendarSync.EVENT_INTERVIEW
        }


def test_unconnected_bulk_delete_preserves_syncs_and_redirects(
    client,
    app,
    monkeypatch,
):
    service = RecordingDeleteCalendarService()
    with app.app_context():
        application_id = add_application()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/bulk-delete"
    )

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.delete_calls == []
    with app.app_context():
        assert get_sync_types(application_id) == set(ALL_EVENT_TYPES)


def test_bulk_delete_route_is_post_only(client, app):
    with app.app_context():
        application_id = add_application()

    response = client.get(
        f"/applications/{application_id}/calendar/bulk-delete"
    )

    assert response.status_code == 405


def test_bulk_delete_confirmation_modal_explains_scope(client, app):
    with app.app_context():
        application_id = add_application(interview_at=None)
        add_sync(application_id, CalendarSync.EVENT_INTERVIEW)

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert 'data-bs-target="#bulkDeleteCalendarEventsModal"' in html
    assert 'id="bulkDeleteCalendarEventsModal"' in html
    assert "Googleカレンダーへ同期済みの予定をまとめて削除します。" in html
    assert "この操作はGoogleカレンダー上の予定のみを削除します。" in html
    assert "CareerPilot AIの面接日時・ES締切・Webテスト期限・応募先情報は削除されません。" in html
    assert f"/applications/{application_id}/calendar/bulk-delete" in html
    assert 'class="btn btn-danger"' in html


def test_bulk_delete_form_works_with_csrf_enabled(monkeypatch):
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "bulk-delete-calendar-csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    service = RecordingDeleteCalendarService()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    with test_app.app_context():
        db.create_all()
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
        path = f"/applications/{application_id}/calendar/bulk-delete"

        assert client.post(path).status_code == 400

        html = client.get(f"/applications/{application_id}").get_data(as_text=True)
        form = re.search(
            rf'<form method="post" action="{re.escape(path)}">(.*?)</form>',
            html,
            re.DOTALL,
        )
        assert form is not None
        csrf_token = re.search(
            r'name="csrf_token" type="hidden" value="([^"]+)"',
            form.group(1),
        )
        assert csrf_token is not None

        response = client.post(
            path,
            data={"csrf_token": csrf_token.group(1)},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert len(service.delete_calls) == 3
        assert CalendarSync.query.count() == 0
        db.session.remove()
        db.drop_all()


def test_bulk_delete_failure_log_contains_no_secrets(
    client,
    app,
    monkeypatch,
    caplog,
):
    secret_event_id = "secret-external-event-id"
    service = RecordingDeleteCalendarService({secret_event_id: "error"})
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_sync(
            application_id,
            CalendarSync.EVENT_ES_DEADLINE,
            event_id=secret_event_id,
        )
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    with caplog.at_level("INFO"):
        response = post_bulk_delete(client, application_id)

    assert response.status_code == 200
    assert "operation=bulk_delete" in caplog.text
    assert "event_type=es_deadline" in caplog.text
    assert "deleted_count=0" in caplog.text
    assert "already_deleted_count=0" in caplog.text
    assert "failed_count=1" in caplog.text
    for secret in (
        "secret-access-token",
        "secret-client-secret",
        "secret-event-id",
        "secret-event-body",
        secret_event_id,
        "一括削除後も残すメモ",
    ):
        assert secret not in caplog.text
