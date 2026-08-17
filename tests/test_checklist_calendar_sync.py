import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from app import create_app
from app.checklists import calendar_routes as checklist_calendar_routes
from app.extensions import db
from app.integrations import calendar_routes as application_calendar_routes
from app.integrations.calendar_service import (
    CalendarEventCancelledError,
    CalendarEventNotFoundError,
    CalendarServiceError,
    GoogleCalendarService,
    build_checklist_due_event_payload,
)
from app.models import Application, CalendarSync, ChecklistItem, GoogleCredential
from config import TestConfig


DEFAULT_DUE_AT = datetime(2026, 8, 15, 18, 0)


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


def add_application_and_item(*, due_at=DEFAULT_DUE_AT, title="ES最終確認"):
    application = Application(
        company_name="Fictional Zenith Labs",
        position_name="AIエンジニア",
        status="ES提出済み",
        priority=5,
    )
    item = ChecklistItem(
        application=application,
        title=title,
        due_at=due_at,
        sort_order=0,
    )
    db.session.add(application)
    db.session.commit()
    return application.id, item.id


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


def add_sync(item_id, event_id="checklist-google-event"):
    sync = CalendarSync(
        checklist_item_id=item_id,
        event_type=CalendarSync.EVENT_CHECKLIST_DUE,
        provider=CalendarSync.PROVIDER_GOOGLE,
        calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
        external_event_id=event_id,
    )
    db.session.add(sync)
    db.session.commit()
    return sync.id


def get_sync(item_id):
    return db.session.scalar(
        db.select(CalendarSync).where(
            CalendarSync.checklist_item_id == item_id,
            CalendarSync.event_type == CalendarSync.EVENT_CHECKLIST_DUE,
            CalendarSync.provider == CalendarSync.PROVIDER_GOOGLE,
        )
    )


class RecordingChecklistCalendarService:
    def __init__(self, *, update_state="confirmed", delete_state="confirmed"):
        self.update_state = update_state
        self.delete_state = delete_state
        self.create_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.delete_execute_calls = []

    def create_checklist_due_event(self, item):
        self.create_calls.append(build_checklist_due_event_payload(item))
        return "created-checklist-event"

    def update_checklist_due_event(self, item, event_id):
        self.update_calls.append(
            (build_checklist_due_event_payload(item), event_id)
        )
        self._raise_for_state(self.update_state, "update")
        return event_id

    def delete_checklist_due_event(self, event_id):
        self.delete_calls.append(event_id)
        self._raise_for_state(self.delete_state, "delete")
        self.delete_execute_calls.append(event_id)

    @staticmethod
    def _raise_for_state(state, operation):
        if state in {404, 410}:
            GoogleCalendarService._raise_operation_error(
                "get",
                google_http_error(state),
            )
        if state == "cancelled":
            raise CalendarEventNotFoundError(
                "calendar_event_status_check",
                CalendarEventCancelledError(),
            )
        if state == "error":
            error = RuntimeError(
                "secret-access-token secret-client-secret "
                "secret-event-id secret-event-body"
            )
            error.resp = SimpleNamespace(status=503)
            raise CalendarServiceError(f"calendar_event_{operation}", error)


def csrf_token_for(html, path):
    form = re.search(
        rf'<form[^>]*method="post"[^>]*action="{re.escape(path)}"[^>]*>'
        r"(.*?)</form>",
        html,
        re.DOTALL,
    )
    assert form is not None
    token = re.search(
        r'name="csrf_token" type="hidden" value="([^"]+)"',
        form.group(1),
    )
    assert token is not None
    return token.group(1)


def test_checklist_due_payload_is_a_thirty_minute_jst_event(app):
    with app.app_context():
        _, item_id = add_application_and_item()
        item = db.session.get(ChecklistItem, item_id)

        event = build_checklist_due_event_payload(item)

    assert event["summary"] == "Fictional Zenith Labs - ES最終確認"
    assert event["start"] == {
        "dateTime": "2026-08-15T18:00:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    assert event["end"] == {
        "dateTime": "2026-08-15T18:30:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    for detail in (
        "CareerPilot AIから登録",
        "会社名: Fictional Zenith Labs",
        "応募職種: AIエンジニア",
        "タスク名: ES最終確認",
        "応募ステータス: ES提出済み",
        "タスク完了状態: 未完了",
        "期限: 2026/08/15 18:00",
    ):
        assert detail in event["description"]


def test_checklist_due_create_saves_checklist_owned_calendar_sync(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        _, item_id = add_application_and_item()
        add_connected_credential()
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/checklist/{item_id}/calendar/create",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "タスク期限をGoogleカレンダーへ登録しました。" in response.get_data(
        as_text=True
    )
    assert len(service.create_calls) == 1
    with app.app_context():
        sync = get_sync(item_id)
        assert sync is not None
        assert sync.event_type == CalendarSync.EVENT_CHECKLIST_DUE
        assert sync.checklist_item_id == item_id
        assert sync.application_id is None
        assert sync.provider == CalendarSync.PROVIDER_GOOGLE
        assert sync.calendar_id == CalendarSync.DEFAULT_CALENDAR_ID
        assert sync.external_event_id == "created-checklist-event"


def test_checklist_due_create_requires_due_at(client, app, monkeypatch):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        _, item_id = add_application_and_item(due_at=None)
        add_connected_credential()
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/checklist/{item_id}/calendar/create",
        follow_redirects=True,
    )

    assert "期限を設定してから" in response.get_data(as_text=True)
    assert service.create_calls == []
    with app.app_context():
        assert get_sync(item_id) is None


def test_duplicate_checklist_due_create_does_not_call_google(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        _, item_id = add_application_and_item()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/checklist/{item_id}/calendar/create",
        follow_redirects=True,
    )

    assert "すでにGoogleカレンダーへ登録済み" in response.get_data(as_text=True)
    assert service.create_calls == []
    with app.app_context():
        assert CalendarSync.query.count() == 1


def test_unconnected_checklist_due_create_redirects_without_google_call(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        _, item_id = add_application_and_item()
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(f"/checklist/{item_id}/calendar/create")

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.create_calls == []


def test_checklist_due_update_uses_latest_task_fields(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        _, item_id = add_application_and_item()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    client.post(
        f"/checklist/{item_id}/edit",
        data={
            "title": "最終版ESを提出",
            "due_at": "2026-08-18T20:30",
            "sort_order": "0",
        },
    )
    client.post(f"/checklist/{item_id}/toggle")
    assert service.update_calls == []

    response = client.post(
        f"/checklist/{item_id}/calendar/update",
        follow_redirects=True,
    )

    assert "Googleカレンダーのタスク予定を更新しました。" in response.get_data(
        as_text=True
    )
    event, event_id = service.update_calls[0]
    assert event_id == "checklist-google-event"
    assert event["summary"] == "Fictional Zenith Labs - 最終版ESを提出"
    assert event["start"]["dateTime"] == "2026-08-18T20:30:00+09:00"
    assert "タスク完了状態: 完了" in event["description"]


@pytest.mark.parametrize("remote_state", (404, 410, "cancelled"))
def test_missing_google_task_event_clears_sync_without_recreation_on_update(
    client,
    app,
    monkeypatch,
    remote_state,
):
    service = RecordingChecklistCalendarService(update_state=remote_state)
    with app.app_context():
        _, item_id = add_application_and_item()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/checklist/{item_id}/calendar/update",
        follow_redirects=True,
    )

    assert "同期状態を解除しました" in response.get_data(as_text=True)
    assert len(service.update_calls) == 1
    assert service.create_calls == []
    with app.app_context():
        assert get_sync(item_id) is None


def test_general_checklist_due_update_error_preserves_sync(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService(update_state="error")
    with app.app_context():
        _, item_id = add_application_and_item()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/checklist/{item_id}/calendar/update",
        follow_redirects=True,
    )

    assert "更新できませんでした" in response.get_data(as_text=True)
    with app.app_context():
        assert get_sync(item_id) is not None


def test_checklist_due_delete_removes_sync_but_keeps_item(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        application_id, item_id = add_application_and_item()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/checklist/{item_id}/calendar/delete",
        follow_redirects=True,
    )

    assert "Googleカレンダーのタスク予定を削除しました。" in response.get_data(
        as_text=True
    )
    assert service.delete_calls == ["checklist-google-event"]
    with app.app_context():
        assert get_sync(item_id) is None
        item = db.session.get(ChecklistItem, item_id)
        assert item is not None
        assert item.application_id == application_id
        assert item.title == "ES最終確認"
        assert item.due_at == DEFAULT_DUE_AT


@pytest.mark.parametrize("remote_state", (404, 410, "cancelled"))
def test_already_deleted_google_task_event_removes_sync_without_delete(
    client,
    app,
    monkeypatch,
    remote_state,
):
    service = RecordingChecklistCalendarService(delete_state=remote_state)
    with app.app_context():
        _, item_id = add_application_and_item()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    response = client.post(
        f"/checklist/{item_id}/calendar/delete",
        follow_redirects=True,
    )

    assert "すでに削除されていたため" in response.get_data(as_text=True)
    assert service.delete_execute_calls == []
    with app.app_context():
        assert get_sync(item_id) is None
        assert db.session.get(ChecklistItem, item_id) is not None


def test_general_checklist_due_delete_error_preserves_sync(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService(delete_state="error")
    with app.app_context():
        _, item_id = add_application_and_item()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    client.post(
        f"/checklist/{item_id}/calendar/delete",
        follow_redirects=True,
    )

    with app.app_context():
        assert get_sync(item_id) is not None


@pytest.mark.parametrize("operation", ("create", "update", "delete"))
def test_checklist_calendar_routes_are_post_only(client, app, operation):
    with app.app_context():
        _, item_id = add_application_and_item()

    assert client.get(f"/checklist/{item_id}/calendar/{operation}").status_code == 405


@pytest.mark.parametrize("operation", ("create", "update", "delete"))
def test_unknown_checklist_calendar_item_returns_404(client, operation):
    assert client.post(f"/checklist/999/calendar/{operation}").status_code == 404


def test_checklist_calendar_forms_work_with_csrf_enabled(monkeypatch):
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "checklist-calendar-csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    service = RecordingChecklistCalendarService()
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    with test_app.app_context():
        db.create_all()
        application_id, create_item_id = add_application_and_item(title="登録対象")
        existing_item = ChecklistItem(
            application_id=application_id,
            title="更新削除対象",
            due_at=DEFAULT_DUE_AT,
            sort_order=1,
        )
        db.session.add(existing_item)
        db.session.commit()
        add_sync(existing_item.id)
        add_connected_credential()

        create_path = f"/checklist/{create_item_id}/calendar/create"
        update_path = f"/checklist/{existing_item.id}/calendar/update"
        delete_path = f"/checklist/{existing_item.id}/calendar/delete"
        for path in (create_path, update_path, delete_path):
            assert client.post(path).status_code == 400

        html = client.get(f"/applications/{application_id}").get_data(as_text=True)
        create_token = csrf_token_for(html, create_path)
        update_token = csrf_token_for(html, update_path)
        delete_token = csrf_token_for(html, delete_path)

        assert client.post(
            create_path,
            data={"csrf_token": create_token},
        ).status_code == 302
        assert client.post(
            update_path,
            data={"csrf_token": update_token},
        ).status_code == 302
        assert client.post(
            delete_path,
            data={"csrf_token": delete_token},
        ).status_code == 302
        assert len(service.create_calls) == 1
        assert len(service.update_calls) == 1
        assert len(service.delete_calls) == 1

        db.session.remove()
        db.drop_all()


def test_checklist_calendar_ui_states_and_delete_warning(client, app):
    with app.app_context():
        application_id, synced_item_id = add_application_and_item(title="同期済み")
        unsynced_item = ChecklistItem(
            application_id=application_id,
            title="未同期",
            due_at=DEFAULT_DUE_AT,
            sort_order=1,
        )
        no_due_item = ChecklistItem(
            application_id=application_id,
            title="期限なし",
            due_at=None,
            sort_order=2,
        )
        db.session.add_all([unsynced_item, no_due_item])
        db.session.commit()
        unsynced_item_id = unsynced_item.id
        no_due_item_id = no_due_item.id
        add_sync(synced_item_id)

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert "✓ Googleカレンダー同期済み" in html
    assert f"/checklist/{synced_item_id}/calendar/update" in html
    assert f"/checklist/{synced_item_id}/calendar/delete" in html
    assert f"/checklist/{unsynced_item_id}/calendar/create" in html
    assert f"/checklist/{no_due_item_id}/calendar/create" not in html
    assert "期限を設定するとGoogleカレンダーへ登録できます。" in html
    assert "タスク内容・期限・完了状態を変更した場合は、Googleカレンダーを手動更新してください。" in html
    assert "Googleカレンダーへ登録済みの予定は自動削除されません。" in html
    assert 'id="deleteChecklistCalendarModal' in html
    assert "CareerPilot AIのチェックリスト項目は削除されません。" in html


def test_checklist_calendar_ui_has_mobile_action_rules():
    css_path = Path(__file__).parents[1] / "app" / "static" / "css" / "style.css"
    css = css_path.read_text(encoding="utf-8")

    assert ".checklist-calendar-panel" in css
    assert ".checklist-calendar-actions" in css
    assert "flex-direction: column" in css
    assert ".checklist-calendar-actions form, .checklist-calendar-actions .btn" in css


def test_deleting_checklist_item_cascades_sync_without_google_call(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        _, item_id = add_application_and_item()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    client.post(f"/checklist/{item_id}/delete")

    assert service.delete_calls == []
    with app.app_context():
        assert db.session.get(ChecklistItem, item_id) is None
        assert CalendarSync.query.count() == 0


def test_deleting_application_cascades_checklist_sync_without_google_call(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        application_id, item_id = add_application_and_item()
        add_sync(item_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    client.post(f"/applications/{application_id}/delete")

    assert service.delete_calls == []
    with app.app_context():
        assert db.session.get(Application, application_id) is None
        assert db.session.get(ChecklistItem, item_id) is None
        assert CalendarSync.query.count() == 0


def test_application_bulk_operations_do_not_include_checklist_sync(
    client,
    app,
    monkeypatch,
):
    service = RecordingChecklistCalendarService()
    with app.app_context():
        application_id, item_id = add_application_and_item()
        application = db.session.get(Application, application_id)
        application.interview_at = None
        application.es_deadline = None
        application.web_test_deadline = None
        db.session.commit()
        add_connected_credential()
        add_sync(item_id)
    monkeypatch.setattr(
        application_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    for operation in ("bulk-create", "bulk-update", "bulk-delete"):
        response = client.post(
            f"/applications/{application_id}/calendar/{operation}",
            follow_redirects=True,
        )
        assert response.status_code == 200

    assert service.create_calls == []
    assert service.update_calls == []
    assert service.delete_calls == []
    with app.app_context():
        assert get_sync(item_id) is not None


def test_checklist_calendar_failure_log_contains_no_secrets(
    client,
    app,
    monkeypatch,
    caplog,
):
    secret_event_id = "secret-checklist-event-id"
    service = RecordingChecklistCalendarService(update_state="error")
    with app.app_context():
        _, item_id = add_application_and_item(title="secret-task-body")
        add_connected_credential()
        add_sync(item_id, event_id=secret_event_id)
    monkeypatch.setattr(
        checklist_calendar_routes,
        "get_calendar_service",
        lambda: service,
    )

    with caplog.at_level("INFO"):
        response = client.post(
            f"/checklist/{item_id}/calendar/update",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "operation=update" in caplog.text
    assert "event_type=checklist_due" in caplog.text
    for secret in (
        "secret-access-token",
        "secret-client-secret",
        "secret-event-id",
        "secret-event-body",
        secret_event_id,
        "secret-task-body",
    ):
        assert secret not in caplog.text
