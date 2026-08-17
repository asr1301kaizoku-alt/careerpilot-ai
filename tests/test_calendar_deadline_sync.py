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
    build_event_payload,
)
from app.models import Application, CalendarSync, GoogleCredential
from config import TestConfig


DEADLINE_CASES = (
    pytest.param(
        CalendarSync.EVENT_ES_DEADLINE,
        "es_deadline",
        "es-deadline",
        "ES締切",
        id="es-deadline",
    ),
    pytest.param(
        CalendarSync.EVENT_WEB_TEST_DEADLINE,
        "web_test_deadline",
        "web-test",
        "Webテスト期限",
        id="web-test-deadline",
    ),
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


def add_application(event_type, *, with_datetime=True, event_id=None):
    application = Application(
        company_name="Fictional Zenith Labs",
        position_name="AIエンジニア",
        status="ES提出済み",
        priority=5,
        es_deadline=(
            datetime(2026, 9, 10, 17, 0)
            if with_datetime and event_type == CalendarSync.EVENT_ES_DEADLINE
            else None
        ),
        web_test_deadline=(
            datetime(2026, 9, 12, 18, 30)
            if with_datetime
            and event_type == CalendarSync.EVENT_WEB_TEST_DEADLINE
            else None
        ),
        memo="提出前に内容を最終確認",
    )
    db.session.add(application)
    db.session.flush()
    if event_id is not None:
        db.session.add(
            CalendarSync(
                application=application,
                event_type=event_type,
                provider=CalendarSync.PROVIDER_GOOGLE,
                calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
                external_event_id=event_id,
            )
        )
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


def get_sync(application_id, event_type):
    return db.session.scalar(
        db.select(CalendarSync).where(
            CalendarSync.application_id == application_id,
            CalendarSync.event_type == event_type,
            CalendarSync.provider == CalendarSync.PROVIDER_GOOGLE,
        )
    )


class SuccessfulDeadlineCalendarService:
    def __init__(self, event_id="deadline-event-id"):
        self.event_id = event_id
        self.create_calls = []
        self.update_calls = []
        self.delete_calls = []

    def create_calendar_event(self, application, event_type):
        self.create_calls.append((application.id, event_type))
        return self.event_id

    def update_calendar_event(self, application, event_type, event_id):
        self.update_calls.append((application.id, event_type, event_id))
        return event_id

    def delete_calendar_event(self, event_id):
        self.delete_calls.append(event_id)


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_deadline_event_payload_is_a_thirty_minute_jst_event(
    event_type,
    attribute,
    slug,
    label,
):
    application = Application(
        company_name="Fictional Zenith Labs",
        position_name="AIエンジニア",
        status="ES提出済み",
        memo="提出前に最終確認",
    )
    setattr(application, attribute, datetime(2026, 9, 10, 17, 0))

    event = build_event_payload(application, event_type)

    assert event["summary"] == f"Fictional Zenith Labs {label}"
    assert event["start"] == {
        "dateTime": "2026-09-10T17:00:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    assert event["end"] == {
        "dateTime": "2026-09-10T17:30:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    assert "CareerPilot AIから登録" in event["description"]
    assert "会社名: Fictional Zenith Labs" in event["description"]
    assert "応募職種: AIエンジニア" in event["description"]
    assert "現在ステータス: ES提出済み" in event["description"]
    assert f"期限種別: {label}" in event["description"]
    assert "メモ: 提出前に最終確認" in event["description"]
    assert "location" not in event


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_deadline_create_saves_calendar_sync(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
):
    service = SuccessfulDeadlineCalendarService()
    with app.app_context():
        application_id = add_application(event_type)
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/create",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Googleカレンダーへ登録しました。" in response.get_data(as_text=True)
    assert service.create_calls == [(application_id, event_type)]
    with app.app_context():
        sync = get_sync(application_id, event_type)
        assert sync is not None
        assert sync.event_type == event_type
        assert sync.provider == CalendarSync.PROVIDER_GOOGLE
        assert sync.calendar_id == CalendarSync.DEFAULT_CALENDAR_ID
        assert sync.external_event_id == "deadline-event-id"


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_missing_deadline_does_not_call_google(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
):
    service = SuccessfulDeadlineCalendarService()
    with app.app_context():
        application_id = add_application(event_type, with_datetime=False)
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/create",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert f"{label}を登録してから" in response.get_data(as_text=True)
    assert service.create_calls == []


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_duplicate_deadline_sync_does_not_create_google_event(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
):
    service = SuccessfulDeadlineCalendarService()
    with app.app_context():
        application_id = add_application(
            event_type,
            event_id="existing-deadline-event",
        )
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/create",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "すでにGoogleカレンダーへ登録済みです。" in response.get_data(
        as_text=True
    )
    assert service.create_calls == []
    with app.app_context():
        assert get_sync(application_id, event_type).external_event_id == (
            "existing-deadline-event"
        )


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_deadline_calendar_event_can_be_updated(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
):
    service = SuccessfulDeadlineCalendarService()
    with app.app_context():
        application_id = add_application(
            event_type,
            event_id="existing-deadline-event",
        )
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/update",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Googleカレンダーの予定を更新しました。" in response.get_data(
        as_text=True
    )
    assert service.update_calls == [
        (application_id, event_type, "existing-deadline-event")
    ]
    with app.app_context():
        assert get_sync(application_id, event_type) is not None


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_deadline_calendar_event_can_be_deleted(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
):
    service = SuccessfulDeadlineCalendarService()
    with app.app_context():
        application_id = add_application(
            event_type,
            event_id="existing-deadline-event",
        )
        add_connected_credential()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/delete",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Googleカレンダーの予定を削除しました。" in response.get_data(
        as_text=True
    )
    assert service.delete_calls == ["existing-deadline-event"]
    with app.app_context():
        application = db.session.get(Application, application_id)
        assert getattr(application, attribute) is not None
        assert get_sync(application_id, event_type) is None


@pytest.mark.parametrize("operation", ("update", "delete"))
@pytest.mark.parametrize("remote_state", (404, 410, "cancelled"))
@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_deleted_google_deadline_clears_only_calendar_sync(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
    remote_state,
    operation,
):
    with app.app_context():
        application_id = add_application(
            event_type,
            event_id="missing-deadline-event",
        )
        add_connected_credential()
    create_calls = []

    class MissingDeadlineService:
        def create_calendar_event(self, application, requested_event_type):
            create_calls.append((application.id, requested_event_type))

        def update_calendar_event(
            self, application, requested_event_type, event_id
        ):
            self._raise_missing("get")

        def delete_calendar_event(self, event_id):
            self._raise_missing("delete")

        @staticmethod
        def _raise_missing(api_operation):
            if remote_state == "cancelled":
                raise CalendarEventNotFoundError(
                    "calendar_event_status_check",
                    CalendarEventCancelledError(),
                )
            GoogleCalendarService._raise_operation_error(
                api_operation,
                google_http_error(remote_state),
            )

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: MissingDeadlineService(),
    )

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/{operation}",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "同期状態を解除しました" in response.get_data(as_text=True)
    assert create_calls == []
    with app.app_context():
        application = db.session.get(Application, application_id)
        assert getattr(application, attribute) is not None
        assert get_sync(application_id, event_type) is None


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
@pytest.mark.parametrize("operation", ("update", "delete"))
def test_deadline_api_error_preserves_calendar_sync(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
    operation,
):
    with app.app_context():
        application_id = add_application(
            event_type,
            event_id="existing-deadline-event",
        )
        add_connected_credential()

    class FailingDeadlineService:
        def update_calendar_event(
            self, application, requested_event_type, event_id
        ):
            error = RuntimeError("sensitive response existing-deadline-event")
            error.resp = SimpleNamespace(status=503)
            raise CalendarServiceError("calendar_event_get", error)

        def delete_calendar_event(self, event_id):
            error = RuntimeError("sensitive response existing-deadline-event")
            error.resp = SimpleNamespace(status=503)
            raise CalendarServiceError("calendar_event_delete", error)

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: FailingDeadlineService(),
    )

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/{operation}",
        follow_redirects=True,
    )

    assert response.status_code == 200
    expected_message = (
        "Googleカレンダーの予定を更新できませんでした。"
        if operation == "update"
        else "Googleカレンダーの予定を削除できませんでした。"
    )
    assert expected_message in (
        response.get_data(as_text=True)
    )
    with app.app_context():
        assert get_sync(application_id, event_type).external_event_id == (
            "existing-deadline-event"
        )


@pytest.mark.parametrize(
    ("event_type", "attribute", "slug", "label"),
    DEADLINE_CASES,
)
def test_unconnected_deadline_create_redirects_without_google_call(
    client,
    app,
    monkeypatch,
    event_type,
    attribute,
    slug,
    label,
):
    service = SuccessfulDeadlineCalendarService()
    with app.app_context():
        application_id = add_application(event_type)
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    response = client.post(
        f"/applications/{application_id}/calendar/{slug}/create"
    )

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.create_calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/applications/1/calendar/es-deadline/create",
        "/applications/1/calendar/es-deadline/update",
        "/applications/1/calendar/es-deadline/delete",
        "/applications/1/calendar/web-test/create",
        "/applications/1/calendar/web-test/update",
        "/applications/1/calendar/web-test/delete",
    ],
)
def test_deadline_calendar_routes_are_post_only(client, path):
    assert client.get(path).status_code == 405


def test_detail_displays_three_independent_calendar_sync_states(client, app):
    with app.app_context():
        application = Application(
            company_name="Three Sync States",
            status="面接",
            priority=4,
            interview_at=datetime(2026, 9, 20, 10, 0),
            es_deadline=datetime(2026, 9, 10, 17, 0),
            web_test_deadline=datetime(2026, 9, 12, 18, 30),
        )
        db.session.add(application)
        db.session.flush()
        db.session.add_all(
            [
                CalendarSync(
                    application=application,
                    event_type=CalendarSync.EVENT_INTERVIEW,
                    provider=CalendarSync.PROVIDER_GOOGLE,
                    calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
                    external_event_id="interview-event",
                ),
                CalendarSync(
                    application=application,
                    event_type=CalendarSync.EVENT_WEB_TEST_DEADLINE,
                    provider=CalendarSync.PROVIDER_GOOGLE,
                    calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
                    external_event_id="web-test-event",
                ),
            ]
        )
        db.session.commit()
        application_id = application.id

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    for label in ("面接", "ES締切", "Webテスト期限"):
        assert label in html
    assert "interview-event" in html
    assert "web-test-event" in html
    assert f"/applications/{application_id}/calendar/es-deadline/create" in html
    assert f"/applications/{application_id}/calendar/web-test/update" in html
    assert f"/applications/{application_id}/calendar/web-test/delete" in html
    assert 'id="deleteCalendarEventModal"' in html
    assert 'id="deleteWebTestCalendarEventModal"' in html
    assert "Googleカレンダーへ登録済みの予定は自動削除されません。" in html


def test_application_delete_cascades_all_three_calendar_syncs(client, app):
    with app.app_context():
        application = Application(
            company_name="Cascade Three Syncs",
            status="面接",
            priority=4,
        )
        db.session.add(application)
        db.session.flush()
        for event_type in (
            CalendarSync.EVENT_INTERVIEW,
            CalendarSync.EVENT_ES_DEADLINE,
            CalendarSync.EVENT_WEB_TEST_DEADLINE,
        ):
            db.session.add(
                CalendarSync(
                    application=application,
                    event_type=event_type,
                    provider=CalendarSync.PROVIDER_GOOGLE,
                    calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
                    external_event_id=f"{event_type}-event",
                )
            )
        db.session.commit()
        application_id = application.id

    response = client.post(
        f"/applications/{application_id}/delete",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert CalendarSync.query.count() == 0


def test_deadline_routes_require_csrf_tokens(monkeypatch):
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "deadline-calendar-csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    service = SuccessfulDeadlineCalendarService()
    monkeypatch.setattr(calendar_routes, "get_calendar_service", lambda: service)

    with test_app.app_context():
        db.create_all()
        application_id = add_application(CalendarSync.EVENT_ES_DEADLINE)
        application = db.session.get(Application, application_id)
        application.web_test_deadline = datetime(2026, 9, 12, 18, 30)
        db.session.commit()
        add_connected_credential()

        paths = [
            f"/applications/{application_id}/calendar/es-deadline/{action}"
            for action in ("create", "update", "delete")
        ] + [
            f"/applications/{application_id}/calendar/web-test/{action}"
            for action in ("create", "update", "delete")
        ]
        for path in paths:
            assert client.post(path).status_code == 400

        html = client.get(f"/applications/{application_id}").get_data(as_text=True)
        for path in (
            f"/applications/{application_id}/calendar/es-deadline/create",
            f"/applications/{application_id}/calendar/web-test/create",
        ):
            form = re.search(
                rf'<form class="d-grid" method="post" '
                rf'action="{re.escape(path)}">(.*?)</form>',
                html,
                re.DOTALL,
            )
            assert form is not None
            assert 'name="csrf_token"' in form.group(1)

        db.session.remove()
        db.drop_all()


def test_deadline_failure_log_includes_type_without_secrets(
    client,
    app,
    monkeypatch,
    caplog,
):
    with app.app_context():
        application_id = add_application(CalendarSync.EVENT_ES_DEADLINE)
        add_connected_credential()

    class FailingDeadlineService:
        def create_calendar_event(self, application, event_type):
            raise CalendarServiceError(
                "calendar_event_create",
                RuntimeError(
                    "secret-token secret-client-secret secret-event-id "
                    "提出前に内容を最終確認"
                ),
            )

    monkeypatch.setattr(
        calendar_routes,
        "get_calendar_service",
        lambda: FailingDeadlineService(),
    )

    with caplog.at_level("ERROR"):
        response = client.post(
            f"/applications/{application_id}/calendar/es-deadline/create",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "operation=create" in caplog.text
    assert "event_type=es_deadline" in caplog.text
    assert "stage=calendar_event_create" in caplog.text
    for secret in (
        "secret-token",
        "secret-client-secret",
        "secret-event-id",
        "提出前に内容を最終確認",
    ):
        assert secret not in caplog.text
