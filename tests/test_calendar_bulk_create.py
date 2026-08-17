import re
from datetime import datetime
from types import SimpleNamespace

from app import create_app
from app.extensions import db
from app.integrations import calendar_routes
from app.integrations.calendar_service import CalendarServiceError
from app.models import Application, CalendarSync, GoogleCredential
from config import TestConfig


DEFAULT_INTERVIEW_AT = datetime(2026, 8, 12, 12, 30)
DEFAULT_ES_DEADLINE = datetime(2026, 8, 9, 23, 59)
DEFAULT_WEB_TEST_DEADLINE = datetime(2026, 8, 10, 23, 59)


def add_application(
    *,
    interview_at=DEFAULT_INTERVIEW_AT,
    es_deadline=DEFAULT_ES_DEADLINE,
    web_test_deadline=DEFAULT_WEB_TEST_DEADLINE,
):
    application = Application(
        company_name="Bulk Calendar Test",
        position_name="エンジニア",
        status="面接",
        priority=4,
        interview_at=interview_at,
        es_deadline=es_deadline,
        web_test_deadline=web_test_deadline,
        memo="一括登録テスト用メモ",
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


def add_sync(application_id, event_type):
    db.session.add(
        CalendarSync(
            application_id=application_id,
            event_type=event_type,
            provider=CalendarSync.PROVIDER_GOOGLE,
            calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
            external_event_id=f"existing-{event_type}-event",
        )
    )
    db.session.commit()


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


class RecordingCalendarService:
    def __init__(self, fail_event_types=()):
        self.fail_event_types = set(fail_event_types)
        self.create_calls = []

    def create_calendar_event(self, application, event_type):
        self.create_calls.append(event_type)
        if event_type in self.fail_event_types:
            error = RuntimeError(
                "secret-access-token secret-client-secret "
                "secret-event-id secret-event-body"
            )
            error.resp = SimpleNamespace(status=503)
            raise CalendarServiceError("calendar_event_create", error)
        return f"created-{event_type}-event"


def post_bulk_create(client, application_id):
    return client.post(
        f"/applications/{application_id}/calendar/bulk-create",
        follow_redirects=True,
    )


def test_bulk_create_creates_all_three_unsynced_dated_events(
    client,
    app,
    monkeypatch,
):
    service = RecordingCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_create(client, application_id)

    assert response.status_code == 200
    assert "Googleカレンダーへ3件登録しました。" in response.get_data(as_text=True)
    assert service.create_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    with app.app_context():
        assert get_sync_types(application_id) == {
            CalendarSync.EVENT_INTERVIEW,
            CalendarSync.EVENT_ES_DEADLINE,
            CalendarSync.EVENT_WEB_TEST_DEADLINE,
        }


def test_bulk_create_skips_an_already_synced_interview(client, app, monkeypatch):
    service = RecordingCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        add_sync(application_id, CalendarSync.EVENT_INTERVIEW)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_create(client, application_id)
    html = response.get_data(as_text=True)

    assert service.create_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
    ]
    assert "Googleカレンダーへ2件登録しました。" in html
    assert "1件は同期済みまたは日時未設定のためスキップしました。" in html


def test_bulk_create_skips_an_event_without_a_datetime(client, app, monkeypatch):
    service = RecordingCalendarService()
    with app.app_context():
        application_id = add_application(es_deadline=None)
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_create(client, application_id)
    html = response.get_data(as_text=True)

    assert service.create_calls == [
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    assert "Googleカレンダーへ2件登録しました。" in html
    assert "1件は同期済みまたは日時未設定のためスキップしました。" in html


def test_bulk_create_calls_no_api_when_all_events_are_synced(
    client,
    app,
    monkeypatch,
):
    service = RecordingCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
        for event_type in (
            CalendarSync.EVENT_INTERVIEW,
            CalendarSync.EVENT_ES_DEADLINE,
            CalendarSync.EVENT_WEB_TEST_DEADLINE,
        ):
            add_sync(application_id, event_type)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_create(client, application_id)
    html = response.get_data(as_text=True)

    assert service.create_calls == []
    assert "登録できる未同期の予定はありません。" in html
    assert "Googleカレンダーへ一括登録" not in html


def test_bulk_create_calls_no_api_when_all_datetimes_are_missing(
    client,
    app,
    monkeypatch,
):
    service = RecordingCalendarService()
    with app.app_context():
        application_id = add_application(
            interview_at=None,
            es_deadline=None,
            web_test_deadline=None,
        )
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_create(client, application_id)
    html = response.get_data(as_text=True)

    assert service.create_calls == []
    assert "登録できる未同期の予定はありません。" in html
    assert "Googleカレンダーへ一括登録" not in html


def test_bulk_create_keeps_successes_when_one_api_call_fails(
    client,
    app,
    monkeypatch,
):
    service = RecordingCalendarService(
        fail_event_types={CalendarSync.EVENT_WEB_TEST_DEADLINE}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = post_bulk_create(client, application_id)
    html = response.get_data(as_text=True)

    assert service.create_calls == [
        CalendarSync.EVENT_ES_DEADLINE,
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        CalendarSync.EVENT_INTERVIEW,
    ]
    assert "Googleカレンダーへ2件登録しました。" in html
    assert "1件は登録できませんでした。" in html
    with app.app_context():
        assert get_sync_types(application_id) == {
            CalendarSync.EVENT_ES_DEADLINE,
            CalendarSync.EVENT_INTERVIEW,
        }


def test_failed_bulk_event_type_remains_unsynced(client, app, monkeypatch):
    service = RecordingCalendarService(
        fail_event_types={CalendarSync.EVENT_ES_DEADLINE}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    post_bulk_create(client, application_id)

    with app.app_context():
        assert CalendarSync.EVENT_ES_DEADLINE not in get_sync_types(application_id)


def test_repeating_bulk_create_does_not_create_duplicate_events(
    client,
    app,
    monkeypatch,
):
    service = RecordingCalendarService()
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    first_response = post_bulk_create(client, application_id)
    second_response = post_bulk_create(client, application_id)

    assert "Googleカレンダーへ3件登録しました。" in first_response.get_data(
        as_text=True
    )
    assert "登録できる未同期の予定はありません。" in second_response.get_data(
        as_text=True
    )
    assert len(service.create_calls) == 3
    with app.app_context():
        assert CalendarSync.query.count() == 3


def test_unconnected_bulk_create_redirects_to_integrations_without_api_call(
    client,
    app,
    monkeypatch,
):
    service = RecordingCalendarService()
    with app.app_context():
        application_id = add_application()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/bulk-create"
    )

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.create_calls == []


def test_bulk_create_route_is_post_only(client, app):
    with app.app_context():
        application_id = add_application()

    response = client.get(
        f"/applications/{application_id}/calendar/bulk-create"
    )

    assert response.status_code == 405


def test_bulk_create_form_works_with_csrf_enabled(monkeypatch):
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "bulk-calendar-csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    service = RecordingCalendarService()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    with test_app.app_context():
        db.create_all()
        application_id = add_application()
        add_connected_credential()
        path = f"/applications/{application_id}/calendar/bulk-create"

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
        assert len(service.create_calls) == 3
        db.session.remove()
        db.drop_all()


def test_bulk_ui_preserves_datetime_order_and_individual_create_forms(client, app):
    with app.app_context():
        application_id = add_application()

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)
    labels = re.findall(
        r'<section class="calendar-sync-entry">.*?'
        r'<h3 class="h6 fw-bold mb-1">([^<]+)</h3>',
        html,
        re.DOTALL,
    )

    assert labels == ["ES締切", "Webテスト期限", "面接"]
    assert "Googleカレンダーへ一括登録" in html
    for path in (
        f"/applications/{application_id}/calendar/create",
        f"/applications/{application_id}/calendar/es-deadline/create",
        f"/applications/{application_id}/calendar/web-test/create",
    ):
        assert path in html


def test_bulk_create_failure_log_contains_no_secrets(
    client,
    app,
    monkeypatch,
    caplog,
):
    service = RecordingCalendarService(
        fail_event_types={CalendarSync.EVENT_WEB_TEST_DEADLINE}
    )
    with app.app_context():
        application_id = add_application()
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    with caplog.at_level("INFO"):
        response = post_bulk_create(client, application_id)

    assert response.status_code == 200
    assert "operation=bulk_create" in caplog.text
    assert "event_type=web_test_deadline" in caplog.text
    assert "created_count=2" in caplog.text
    assert "skipped_count=0" in caplog.text
    assert "failed_count=1" in caplog.text
    for secret in (
        "secret-access-token",
        "secret-client-secret",
        "secret-event-id",
        "secret-event-body",
        "created-es_deadline-event",
    ):
        assert secret not in caplog.text
