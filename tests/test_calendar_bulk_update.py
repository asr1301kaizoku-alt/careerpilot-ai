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
        company_name="Bulk Update Test",
        position_name="エンジニア",
        status="面接",
        priority=4,
        interview_at=interview_at,
        es_deadline=es_deadline,
        web_test_deadline=web_test_deadline,
        memo="一括更新テスト用メモ",
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
            external_event_id=event_id or f"existing-{event_type}-event",
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


class RecordingUpdateCalendarService:
    def __init__(self, remote_states=None):
        self.remote_states = remote_states or {}
        self.update_calls = []
        self.create_calls = []

    def update_calendar_event(self, application, event_type, event_id):
        self.update_calls.append(event_type)
        remote_state = self.remote_states.get(event_type, "confirmed")
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
            raise CalendarServiceError("calendar_event_update", error)
        return event_id

    def create_calendar_event(self, application, event_type):
        self.create_calls.append(event_type)
        raise AssertionError("Bulk update must not create Google events.")


def post_bulk_update(client, application_id):
    return client.post(
        f"/applications/{application_id}/calendar/bulk-update",
        follow_redirects=True,
    )


def test_bulk_update_updates_all_three_synced_dated_events(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_update(client, application_id)

    assert response.status_code == 200
    assert "Googleカレンダーの予定を3件更新しました。" in response.get_data(
        as_text=True
    )
    assert service.update_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    assert service.create_calls == []
    with app.app_context():
        assert get_sync_types(application_id) == set(ALL_EVENT_TYPES)


def test_bulk_update_skips_an_unsynced_interview(client, app, monkeypatch):
    service = RecordingUpdateCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(
            application_id,
            (
                CalendarSync.EVENT_ES_DEADLINE,
                CalendarSync.EVENT_WEB_TEST_DEADLINE,
            ),
        )
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_update(client, application_id)
    html = response.get_data(as_text=True)

    assert service.update_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
    ]
    assert service.create_calls == []
    assert "Googleカレンダーの予定を2件更新しました。" in html
    assert "1件は未同期または日時未設定のためスキップしました。" in html


def test_bulk_update_skips_a_synced_event_without_a_datetime(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService()
    with app.app_context():
        application_id = add_application(es_deadline=None)
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_update(client, application_id)

    assert service.update_calls == [
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    assert "1件は未同期または日時未設定のためスキップしました。" in (
        response.get_data(as_text=True)
    )
    with app.app_context():
        assert get_sync_types(application_id) == set(ALL_EVENT_TYPES)


def test_bulk_update_calls_no_api_when_all_events_are_unsynced(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_update(client, application_id)
    html = response.get_data(as_text=True)

    assert service.update_calls == []
    assert service.create_calls == []
    assert "更新できる同期済みの予定はありません。" in html
    assert "Googleカレンダーへ一括更新" not in html


def test_bulk_update_has_no_targets_when_all_datetimes_are_missing(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=None,
            es_deadline=None,
            web_test_deadline=None,
        )
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_update(client, application_id)

    assert service.update_calls == []
    assert "更新できる同期済みの予定はありません。" in response.get_data(
        as_text=True
    )


def test_bulk_update_keeps_other_successes_when_one_api_call_fails(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService(
        {CalendarSync.EVENT_WEB_TEST_DEADLINE: "error"}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_update(client, application_id)
    html = response.get_data(as_text=True)

    assert service.update_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    assert "Googleカレンダーの予定を2件更新しました。" in html
    assert "1件は更新できませんでした。" in html
    with app.app_context():
        assert get_sync_types(application_id) == set(ALL_EVENT_TYPES)


def test_general_bulk_update_error_preserves_affected_sync(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService(
        {CalendarSync.EVENT_ES_DEADLINE: "error"}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    post_bulk_update(client, application_id)

    with app.app_context():
        assert CalendarSync.EVENT_ES_DEADLINE in get_sync_types(application_id)


@pytest.mark.parametrize("remote_state", (404, 410, "cancelled"))
def test_deleted_google_event_clears_only_its_sync_during_bulk_update(
    client,
    app,
    monkeypatch,
    remote_state,
):
    service = RecordingUpdateCalendarService(
        {CalendarSync.EVENT_ES_DEADLINE: remote_state}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_update(client, application_id)
    html = response.get_data(as_text=True)

    assert service.update_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    assert service.create_calls == []
    assert "Googleカレンダーの予定を2件更新しました。" in html
    assert "1件はGoogleカレンダー上で削除されていたため同期を解除しました。" in html
    with app.app_context():
        assert get_sync_types(application_id) == {
            CalendarSync.EVENT_INTERVIEW,
            CalendarSync.EVENT_WEB_TEST_DEADLINE,
        }


def test_bulk_update_response_preserves_application_datetime_order(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    html = post_bulk_update(client, application_id).get_data(as_text=True)
    labels = re.findall(
        r'<section class="calendar-sync-entry">.*?'
        r'<h3 class="h6 fw-bold mb-1">([^<]+)</h3>',
        html,
        re.DOTALL,
    )

    assert labels == ["ES締切", "Webテスト期限", "面接"]


def test_unconnected_bulk_update_redirects_without_api_call(
    client,
    app,
    monkeypatch,
):
    service = RecordingUpdateCalendarService()
    with app.app_context():
        application_id = add_application()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/bulk-update"
    )

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.update_calls == []


def test_bulk_update_route_is_post_only(client, app):
    with app.app_context():
        application_id = add_application()

    response = client.get(
        f"/applications/{application_id}/calendar/bulk-update"
    )

    assert response.status_code == 405


def test_bulk_update_form_works_with_csrf_enabled(monkeypatch):
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "bulk-update-calendar-csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    service = RecordingUpdateCalendarService()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    with test_app.app_context():
        db.create_all()
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
        path = f"/applications/{application_id}/calendar/bulk-update"

        assert client.post(path).status_code == 400

        html = client.get(f"/applications/{application_id}").get_data(as_text=True)
        form = re.search(
            rf'<form class="d-grid" method="post" action="{re.escape(path)}">'
            r"(.*?)</form>",
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
        assert len(service.update_calls) == 3
        db.session.remove()
        db.drop_all()


def test_bulk_action_area_can_show_create_and_update_separately(client, app):
    with app.app_context():
        application_id = add_application()
        add_sync(application_id, CalendarSync.EVENT_ES_DEADLINE)

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert "Googleカレンダーへ一括登録" in html
    assert "Googleカレンダーへ一括更新" in html
    assert f"/applications/{application_id}/calendar/bulk-create" in html
    assert f"/applications/{application_id}/calendar/bulk-update" in html


def test_bulk_update_failure_log_contains_no_secrets(
    client,
    app,
    monkeypatch,
    caplog,
):
    service = RecordingUpdateCalendarService(
        {CalendarSync.EVENT_WEB_TEST_DEADLINE: "error"}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_syncs(application_id)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    with caplog.at_level("INFO"):
        response = post_bulk_update(client, application_id)

    assert response.status_code == 200
    assert "operation=bulk_update" in caplog.text
    assert "event_type=web_test_deadline" in caplog.text
    assert "success_count=2" in caplog.text
    assert "skipped_count=0" in caplog.text
    assert "failed_count=1" in caplog.text
    assert "sync_cleared_count=0" in caplog.text
    for secret in (
        "secret-access-token",
        "secret-client-secret",
        "secret-event-id",
        "secret-event-body",
        "existing-web_test_deadline-event",
        "一括更新テスト用メモ",
    ):
        assert secret not in caplog.text
