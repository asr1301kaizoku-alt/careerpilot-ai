import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import create_app
from app.emails import routes as email_routes
from app.emails.analysis_calendar import (
    EmailAnalysisCalendarApplyService,
    build_ai_calendar_description,
    build_calendar_candidate_data,
)
from app.emails.calendar_registration_service import (
    EmailCalendarRegistrationService,
    EmailCalendarRegistrationStorageError,
)
from app.emails.analysis_apply_store import EmailAnalysisApplyStore
from app.extensions import db
from app.integrations.calendar_service import (
    CalendarEventCancelledError,
    CalendarEventNotFoundError,
    CalendarServiceError,
    build_reviewed_event_payload,
)
from app.integrations.calendar_sync_service import (
    CalendarSyncService,
    CalendarSyncStorageError,
)
from app.models import Application, CalendarSync, EmailCalendarRegistration
from app.services.email_ai_service import validate_analysis_payload
from config import TestConfig


MESSAGE_ID = "message-calendar"
RETURN_TO = "/emails/?q=面接&page_token=page-4"


def analysis_payload(**overrides):
    payload = {
        "company_name": "株式会社カレンダー",
        "mail_category": "interview",
        "es_deadline": "2026-08-20T23:59:00+09:00",
        "web_test_deadline": "2026-08-22T18:00:00+09:00",
        "interview_datetime": "2026-08-25T13:00:00+09:00",
        "event_datetime": "2026-08-28T10:00:00+09:00",
        "es_deadline_text": None,
        "web_test_deadline_text": None,
        "interview_datetime_text": None,
        "event_datetime_text": None,
        "action_items": ["予約内容を確認する"],
        "important_notes": ["受付は開始10分前です"],
        "summary": "面接と関連予定の案内です。",
        "confidence": "high",
        "evidence": {
            "company_name": "株式会社カレンダー",
            "es_deadline": "ESは8月20日23:59まで",
            "web_test_deadline": "Webテストは8月22日18:00まで",
            "interview_datetime": "面接は8月25日13:00開始",
            "event_datetime": "説明会は8月28日10:00開始",
        },
    }
    payload.update(overrides)
    return payload


def analysis_result(**overrides):
    return validate_analysis_payload(analysis_payload(**overrides))


def issue_token(app, result=None, store=None):
    with app.app_context():
        target = store or app.extensions["email_analysis_calendar_store"]
        return target.save(
            MESSAGE_ID,
            result or analysis_result(),
            RETURN_TO,
        )


def create_application(**overrides):
    values = {
        "company_name": "株式会社カレンダー",
        "position_name": "総合職",
        "status": "面接",
        "priority": 4,
        "es_deadline": datetime(2026, 8, 20, 23, 59),
        "web_test_deadline": datetime(2026, 8, 22, 18, 0),
        "interview_at": datetime(2026, 8, 25, 13, 0),
    }
    values.update(overrides)
    application = Application(**values)
    db.session.add(application)
    db.session.commit()
    return application


def select_application(client, token, application_id):
    return client.get(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        query_string={
            "token": token,
            "return_to": RETURN_TO,
            "application_id": application_id,
        },
    )


def candidate_rows(result=None):
    rows = []
    for candidate in build_calendar_candidate_data(result or analysis_result()):
        rows.append(
            {
                "selected": True,
                "event_type": candidate["event_type"],
                "title": candidate["title"],
                "start_at": candidate["start_at"].strftime("%Y-%m-%dT%H:%M"),
                "end_at": candidate["end_at"].strftime("%Y-%m-%dT%H:%M"),
            }
        )
    return rows


def calendar_post_data(token, candidates, application_id=-1, csrf_token=None):
    data = {
        "token": token,
        "return_to": RETURN_TO,
        "application_id": str(application_id),
    }
    if csrf_token:
        data["csrf_token"] = csrf_token
    for index, candidate in enumerate(candidates):
        if candidate.get("selected", True):
            data[f"candidates-{index}-selected"] = "y"
        data[f"candidates-{index}-event_type"] = candidate["event_type"]
        data[f"candidates-{index}-title"] = candidate.get("title", "")
        data[f"candidates-{index}-start_at"] = candidate.get("start_at", "")
        data[f"candidates-{index}-end_at"] = candidate.get("end_at", "")
    return data


class ConnectedCredentialStore:
    def get_calendar_credential(self):
        return object()

    def get_gmail_credential(self):
        return None


class DisconnectedCredentialStore:
    def get_calendar_credential(self):
        return None

    def get_gmail_credential(self):
        return None


class FakeResponse:
    status_code = 503


class FakeGoogleError(RuntimeError):
    response = FakeResponse()


class RecordingCalendarService:
    def __init__(self, fail_titles=()):
        self.calls = []
        self.fail_titles = set(fail_titles)

    def create_reviewed_event(self, title, start, end, description):
        self.calls.append(
            {
                "title": title,
                "start": start,
                "end": end,
                "description": description,
            }
        )
        if title in self.fail_titles:
            raise CalendarServiceError(
                "calendar_event_create",
                FakeGoogleError("private API response body"),
            )
        return f"event-{len(self.calls)}"


def prepare_connected(monkeypatch, calendar_service=None):
    service = calendar_service or RecordingCalendarService()
    monkeypatch.setattr(
        email_routes,
        "get_credential_store",
        lambda: ConnectedCredentialStore(),
    )
    monkeypatch.setattr(
        email_routes,
        "get_google_calendar_service",
        lambda: service,
    )
    return service


def test_candidate_generation_covers_all_supported_types_and_durations():
    candidates = build_calendar_candidate_data(analysis_result())

    assert [candidate["event_type"] for candidate in candidates] == [
        "es_deadline",
        "web_test_deadline",
        "interview_datetime",
        "event_datetime",
    ]
    assert [
        int((candidate["end_at"] - candidate["start_at"]).total_seconds() / 60)
        for candidate in candidates
    ] == [30, 30, 60, 60]


def test_confirmation_distinguishes_structured_range_from_source_evidence(
    client,
    app,
    monkeypatch,
):
    prepare_connected(monkeypatch)
    start = "2026-08-28T10:00:00+09:00"
    result = analysis_result(
        es_deadline=None,
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=start,
        event_start_datetime=start,
        event_end_datetime="2026-08-28T17:00:00+09:00",
        evidence={
            "company_name": "株式会社カレンダー",
            "es_deadline": None,
            "web_test_deadline": None,
            "interview_datetime": None,
            "event_datetime": "2026年8月28日10:00～17:00",
        },
    )
    token = issue_token(app, result=result)

    response = client.get(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        query_string={"token": token},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "AI抽出日時" in html
    assert "2026/08/28 10:00 ～ 2026/08/28 17:00" in html
    assert "AI根拠" in html
    assert "2026年8月28日10:00～17:00" in html
    assert 'value="2026-08-28T10:00"' in html
    assert 'value="2026-08-28T17:00"' in html


def test_candidate_generation_uses_jst_and_safe_company_titles():
    result = analysis_result(
        es_deadline="2026-08-20T14:59:00+00:00",
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=None,
    )

    candidate = build_calendar_candidate_data(result)[0]

    assert candidate["start_at"] == datetime(2026, 8, 20, 23, 59)
    assert candidate["title"] == "株式会社カレンダー ES締切"
    assert build_calendar_candidate_data(analysis_result())[-1]["title"] == (
        "株式会社カレンダー イベント"
    )
    no_company = replace(result, company_name=None)
    assert build_calendar_candidate_data(no_company)[0]["title"] == "ES締切"
    assert build_calendar_candidate_data(
        replace(analysis_result(), company_name=None)
    )[-1]["title"] == "就活イベント"


def test_reviewed_event_payload_converts_to_jst_and_keeps_edited_end():
    payload = build_reviewed_event_payload(
        "確認済みイベント",
        datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 16, 15, tzinfo=timezone.utc),
        "安全な説明",
    )

    assert payload["start"] == {
        "dateTime": "2026-08-20T23:00:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    assert payload["end"] == {
        "dateTime": "2026-08-21T01:15:00+09:00",
        "timeZone": "Asia/Tokyo",
    }
    assert payload["summary"] == "確認済みイベント"


def test_null_text_only_timezone_less_and_invalid_datetimes_are_not_candidates():
    result = analysis_result(
        es_deadline=None,
        es_deadline_text="8月20日中",
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=None,
    )
    assert build_calendar_candidate_data(result) == []
    assert build_calendar_candidate_data(
        replace(result, event_datetime="2026-08-28T10:00:00")
    ) == []
    assert build_calendar_candidate_data(
        replace(result, event_datetime="not-a-datetime")
    ) == []


def test_confirmation_ui_supports_selection_editing_evidence_and_cancel(
    client,
    app,
    monkeypatch,
):
    prepare_connected(monkeypatch)
    token = issue_token(app)

    response = client.get(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        query_string={"token": token, "return_to": RETURN_TO},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "AI解析結果から予定を登録" in html
    assert html.count("を登録する</label>") == 4
    assert 'name="candidates-0-title"' in html
    assert 'name="candidates-0-start_at"' in html
    assert 'name="candidates-0-end_at"' in html
    assert "ESは8月20日23:59まで" in html
    assert "メール詳細へ戻る" in html
    assert "page_token=page-4" in html


def test_text_only_datetime_is_reference_not_candidate(client, app, monkeypatch):
    prepare_connected(monkeypatch)
    result = analysis_result(
        es_deadline=None,
        es_deadline_text="8月20日中",
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime="2026-08-28T10:00:00+09:00",
    )
    token = issue_token(app, result=result)

    html = client.get(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        query_string={"token": token},
    ).get_data(as_text=True)

    assert html.count("を登録する</label>") == 1
    assert "日時の参考情報" in html
    assert "8月20日中" in html


def test_registers_only_selected_events_with_user_edits(client, app, monkeypatch):
    service = prepare_connected(monkeypatch)
    token = issue_token(app)
    candidates = candidate_rows()
    candidates[0]["title"] = "修正したES締切"
    candidates[0]["end_at"] = "2026-08-21T00:45"
    candidates[1]["selected"] = False
    candidates[3]["selected"] = False

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidates),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert [call["title"] for call in service.calls] == [
        "修正したES締切",
        "株式会社カレンダー 面接",
    ]
    assert service.calls[0]["end"] == datetime(2026, 8, 21, 0, 45)
    assert "2件登録しました" in response.get_data(as_text=True)


def test_registers_all_candidates_and_description_excludes_mail_secrets(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    token = issue_token(app)

    client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows()),
    )

    assert len(service.calls) == 4
    description = service.calls[0]["description"]
    assert "CareerPilot AIからAI解析結果を確認して登録" in description
    assert "メール種別: 面接" in description
    assert "予約内容を確認する" in description
    assert "受付は開始10分前です" in description
    assert MESSAGE_ID not in description
    assert "@" not in description


def test_event_datetime_end_can_be_edited_before_registration(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    result = analysis_result(
        es_deadline=None,
        web_test_deadline=None,
        interview_datetime=None,
    )
    token = issue_token(app, result=result)
    candidates = candidate_rows(result)
    candidates[0]["end_at"] = "2026-08-28T12:30"

    client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidates),
    )

    assert service.calls[0]["end"] == datetime(2026, 8, 28, 12, 30)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("start_at", "invalid", "Not a valid datetime value"),
        ("end_at", "2026-08-20T23:58", "終了日時は開始日時より後"),
    ],
)
def test_invalid_datetime_input_rejects_entire_request(
    client,
    app,
    monkeypatch,
    field,
    value,
    message,
):
    service = prepare_connected(monkeypatch)
    result = analysis_result(
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=None,
    )
    token = issue_token(app, result=result)
    candidates = candidate_rows(result)
    candidates[0][field] = value

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidates),
    )

    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
    assert service.calls == []


def test_all_unselected_and_title_too_long_are_rejected(client, app, monkeypatch):
    service = prepare_connected(monkeypatch)
    token = issue_token(app)
    candidates = candidate_rows()
    for candidate in candidates:
        candidate["selected"] = False

    unselected = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidates),
    )
    candidates[0]["selected"] = True
    candidates[0]["title"] = "長" * 201
    too_long = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidates),
    )

    assert "登録する予定を選択してください" in unselected.get_data(as_text=True)
    assert "200文字以内" in too_long.get_data(as_text=True)
    assert service.calls == []


def test_partial_google_failure_keeps_successes_and_reports_counts(
    client,
    app,
    monkeypatch,
):
    fail_title = "株式会社カレンダー Webテスト期限"
    service = prepare_connected(
        monkeypatch,
        RecordingCalendarService(fail_titles={fail_title}),
    )
    token = issue_token(app)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows()),
        follow_redirects=True,
    )
    html = response.get_data(as_text=True)

    assert len(service.calls) == 4
    assert "3件登録しました" in html
    assert "1件は登録できませんでした" in html


def test_google_not_connected_calls_no_api_and_keeps_token(
    client,
    app,
    monkeypatch,
):
    service = RecordingCalendarService()
    monkeypatch.setattr(
        email_routes,
        "get_credential_store",
        lambda: DisconnectedCredentialStore(),
    )
    monkeypatch.setattr(
        email_routes,
        "get_google_calendar_service",
        lambda: service,
    )
    token = issue_token(app)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows()),
    )

    assert response.status_code == 302
    assert response.location.endswith("/settings/integrations")
    assert service.calls == []
    assert app.extensions["email_analysis_calendar_store"].get(
        token,
        MESSAGE_ID,
    ) is not None


def test_existing_application_sync_blocks_duplicate_google_event(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    result = analysis_result(
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=None,
    )
    with app.app_context():
        application = create_application()
        application_id = application.id
        CalendarSyncService().create_application(
            application_id,
            CalendarSync.EVENT_ES_DEADLINE,
            "existing-event",
        )
    token = issue_token(app, result=result)
    select_application(client, token, application_id)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(
            token,
            candidate_rows(result),
            application_id,
        ),
        follow_redirects=True,
    )

    assert service.calls == []
    assert "すでにGoogle Calendarへ同期されています" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(CalendarSync.id))) == 1


def test_matching_application_datetime_creates_sync_without_mutating_application(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    result = analysis_result(event_datetime=None)
    with app.app_context():
        application = create_application(memo="変更しない")
        application_id = application.id
    token = issue_token(app, result=result)
    select_application(client, token, application_id)

    client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(
            token,
            candidate_rows(result),
            application_id,
        ),
    )

    assert len(service.calls) == 3
    with app.app_context():
        application = db.session.get(Application, application_id)
        syncs = CalendarSyncService().get_application_syncs(application_id)
        assert set(syncs) == {
            CalendarSync.EVENT_ES_DEADLINE,
            CalendarSync.EVENT_WEB_TEST_DEADLINE,
            CalendarSync.EVENT_INTERVIEW,
        }
        assert application.es_deadline == datetime(2026, 8, 20, 23, 59)
        assert application.web_test_deadline == datetime(2026, 8, 22, 18, 0)
        assert application.interview_at == datetime(2026, 8, 25, 13, 0)
        assert application.memo == "変更しない"


def test_mismatched_application_datetime_and_event_datetime_stay_independent(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    result = analysis_result(
        web_test_deadline=None,
        interview_datetime=None,
    )
    with app.app_context():
        application_id = create_application(
            es_deadline=datetime(2026, 8, 21, 23, 59)
        ).id
    token = issue_token(app, result=result)
    select_application(client, token, application_id)

    client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(
            token,
            candidate_rows(result),
            application_id,
        ),
    )

    assert len(service.calls) == 2
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(CalendarSync.id))) == 0
        assert db.session.get(Application, application_id).es_deadline == datetime(
            2026, 8, 21, 23, 59
        )


def test_without_application_all_events_are_independent(client, app, monkeypatch):
    service = prepare_connected(monkeypatch)
    result = analysis_result(event_datetime=None)
    token = issue_token(app, result=result)

    client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows(result)),
    )

    assert len(service.calls) == 3
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(CalendarSync.id))) == 0


def test_application_id_and_event_type_tampering_call_no_api(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    with app.app_context():
        first_id = create_application().id
        second_id = create_application(company_name="別会社").id
    token = issue_token(app)
    select_application(client, token, first_id)

    candidates = candidate_rows()
    candidates[0]["event_type"] = "invalid-event-type"
    event_type_response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidates, first_id),
    )
    application_response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows(), second_id),
    )

    assert "AI候補の内容を確認できませんでした" in event_type_response.get_data(
        as_text=True
    )
    assert "紐付け先の応募先を確認できませんでした" in (
        application_response.get_data(as_text=True)
    )
    assert service.calls == []


def test_calendar_token_is_one_time_tamper_resistant_and_independent(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    calendar_token = issue_token(app)
    with app.app_context():
        other_token = app.extensions["email_analysis_apply_store"].save(
            MESSAGE_ID,
            analysis_result(),
            RETURN_TO,
        )

    tampered = client.get(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        query_string={"token": "tampered"},
    )
    wrong_store = client.get(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        query_string={"token": other_token},
    )
    first = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(calendar_token, candidate_rows()),
    )
    reused = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(calendar_token, candidate_rows()),
    )

    assert tampered.status_code == wrong_store.status_code == 302
    assert first.status_code == reused.status_code == 302
    assert len(service.calls) == 4
    assert app.extensions["email_analysis_apply_store"].get(
        other_token,
        MESSAGE_ID,
    ) is not None


def test_expired_calendar_token_calls_no_api(client, app, monkeypatch):
    service = prepare_connected(monkeypatch)
    now = [100.0]
    store = EmailAnalysisApplyStore(ttl_seconds=10, clock=lambda: now[0])
    app.extensions["email_analysis_calendar_store"] = store
    token = issue_token(app, store=store)
    now[0] = 111.0

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows()),
    )

    assert response.status_code == 302
    assert service.calls == []


def test_safe_logs_exclude_event_payload_ids_and_secrets(
    client,
    app,
    monkeypatch,
    caplog,
):
    secret_title = "SECRET EVENT TITLE"
    service = prepare_connected(
        monkeypatch,
        RecordingCalendarService(fail_titles={secret_title}),
    )
    result = analysis_result(
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=None,
    )
    token = issue_token(app, result=result)
    candidates = candidate_rows(result)
    candidates[0]["title"] = secret_title

    with caplog.at_level(logging.INFO):
        response = client.post(
            f"/emails/{MESSAGE_ID}/analysis/calendar",
            data=calendar_post_data(token, candidates),
            follow_redirects=True,
        )

    logs = caplog.text
    assert response.status_code == 200
    assert "operation=ai_calendar_apply" in logs
    assert "http_status=503" in logs
    assert secret_title not in logs
    assert "private API response body" not in logs
    assert "event-" not in logs
    assert "token" not in logs.lower()


def test_calendar_description_is_bounded_and_contains_no_ai_summary():
    result = analysis_result(summary="PRIVATE FULL MAIL CONTENT")
    description = build_ai_calendar_description(result)

    assert len(description) <= 4_000
    assert "PRIVATE FULL MAIL CONTENT" not in description


def test_calendar_confirmation_requires_csrf_when_enabled(monkeypatch):
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "email-calendar-csrf-secret"

    csrf_app = create_app(CsrfConfig)
    service = prepare_connected(monkeypatch)
    with csrf_app.app_context():
        db.create_all()
        token = issue_token(csrf_app)
        client = csrf_app.test_client()
        confirmation = client.get(
            f"/emails/{MESSAGE_ID}/analysis/calendar",
            query_string={"token": token},
        )
        csrf_match = re.search(
            r'name="csrf_token"[^>]+value="([^"]+)"',
            confirmation.get_data(as_text=True),
        )
        assert csrf_match is not None

        rejected = client.post(
            f"/emails/{MESSAGE_ID}/analysis/calendar",
            data=calendar_post_data(token, candidate_rows()),
        )
        accepted = client.post(
            f"/emails/{MESSAGE_ID}/analysis/calendar",
            data=calendar_post_data(
                token,
                candidate_rows(),
                csrf_token=csrf_match.group(1),
            ),
        )

        assert rejected.status_code == 400
        assert accepted.status_code == 302
        assert len(service.calls) == 4
        db.session.remove()
        db.drop_all()


def test_calendar_confirmation_mobile_css_is_scoped():
    css = (Path("app/static/css/style.css")).read_text(encoding="utf-8")

    assert ".ai-calendar-apply" in css
    assert ".ai-calendar-candidate" in css
    assert ".ai-calendar-apply form > .d-flex .btn { width: 100%; }" in css


def test_service_result_tracks_independent_event_datetime_without_sync(app):
    service = RecordingCalendarService()
    sync_service = CalendarSyncService()
    result = analysis_result(
        es_deadline=None,
        web_test_deadline=None,
        interview_datetime=None,
    )
    candidate_data = build_calendar_candidate_data(result)[0]
    from app.emails.analysis_calendar import ReviewedCalendarCandidate

    candidate = ReviewedCalendarCandidate(
        event_type=candidate_data["event_type"],
        title=candidate_data["title"],
        start_at=candidate_data["start_at"],
        end_at=candidate_data["end_at"],
    )
    with app.app_context():
        applied = EmailAnalysisCalendarApplyService(
            service,
            sync_service,
        ).apply([candidate], result)

        assert applied.created_count == 1
        assert applied.independent_event_types == ["event_datetime"]
        assert db.session.scalar(db.select(db.func.count(CalendarSync.id))) == 0


def test_sync_storage_failure_does_not_rollback_created_google_event(app):
    class FailingSyncService:
        def get_application(self, application_id, event_type):
            return None

        def create_application(self, *args, **kwargs):
            raise CalendarSyncStorageError(
                "calendar_sync_save",
                RuntimeError("private database details"),
            )

    result = analysis_result(
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=None,
    )
    candidate_data = build_calendar_candidate_data(result)[0]
    from app.emails.analysis_calendar import ReviewedCalendarCandidate

    candidate = ReviewedCalendarCandidate(
        event_type=candidate_data["event_type"],
        title=candidate_data["title"],
        start_at=candidate_data["start_at"],
        end_at=candidate_data["end_at"],
    )
    calendar_service = RecordingCalendarService()
    with app.app_context():
        application = create_application()
        applied = EmailAnalysisCalendarApplyService(
            calendar_service,
            FailingSyncService(),
        ).apply([candidate], result, application)

    assert applied.created_count == 1
    assert applied.sync_failure_count == 1
    assert len(calendar_service.calls) == 1


def one_general_event_result(**overrides):
    values = {
        "es_deadline": None,
        "web_test_deadline": None,
        "interview_datetime": None,
    }
    values.update(overrides)
    return analysis_result(**values)


def register_one_general_event(client, app, result=None):
    target_result = result or one_general_event_result()
    token = issue_token(app, result=target_result)
    return client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows(target_result)),
        follow_redirects=True,
    )


def test_first_registration_persists_hashed_source_and_second_calls_no_api(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    first = register_one_general_event(client, app)

    with app.app_context():
        record = db.session.scalar(db.select(EmailCalendarRegistration))
        assert record is not None
        assert record.event_type == "event_datetime"
        assert record.message_key != MESSAGE_ID
        assert len(record.message_key) == 64

    second = register_one_general_event(client, app)

    assert first.status_code == 200
    assert second.status_code == 200
    assert "すでにGoogle Calendarへ登録されています" in second.get_data(
        as_text=True
    )
    assert len(service.calls) == 1
    with app.app_context():
        assert EmailCalendarRegistration.query.count() == 1


def test_registered_candidate_is_marked_and_unselected_on_confirmation(
    client,
    app,
    monkeypatch,
):
    prepare_connected(monkeypatch)
    register_one_general_event(client, app)
    token = issue_token(app, result=one_general_event_result())

    response = client.get(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        query_string={"token": token},
    )
    html = response.get_data(as_text=True)

    assert "登録済み・重複登録しません" in html
    assert "Google側の登録状態を確認" in html
    selected_input = re.search(
        r'<input[^>]+name="candidates-0-selected"[^>]*>',
        html,
    )
    assert selected_input is not None
    assert "disabled" in selected_input.group(0)
    assert "checked" not in selected_input.group(0)


def test_tracking_survives_new_app_instance(tmp_path):
    database_path = tmp_path / "email-calendar-registration.db"

    class PersistenceConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"

    class Credential:
        google_account_email = "calendar@example.com"

    first_app = create_app(PersistenceConfig)
    with first_app.app_context():
        service = EmailCalendarRegistrationService("persistent-owner")
        service.create(
            MESSAGE_ID,
            "event_datetime",
            Credential(),
            "persisted-event-id",
        )
        db.session.remove()
        db.engine.dispose()

    second_app = create_app(PersistenceConfig)
    with second_app.app_context():
        record = EmailCalendarRegistrationService(
            "persistent-owner"
        ).get(MESSAGE_ID, "event_datetime", Credential())
        assert record is not None
        assert record.external_event_id == "persisted-event-id"
        db.session.remove()
        db.engine.dispose()


def test_same_datetime_from_different_messages_can_be_registered(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    result = one_general_event_result()
    register_one_general_event(client, app, result)
    other_message_id = "another-gmail-message"
    with app.app_context():
        token = app.extensions["email_analysis_calendar_store"].save(
            other_message_id,
            result,
            RETURN_TO,
        )

    response = client.post(
        f"/emails/{other_message_id}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows(result)),
    )

    assert response.status_code == 302
    assert len(service.calls) == 2
    with app.app_context():
        assert EmailCalendarRegistration.query.count() == 2


def test_different_candidate_types_from_same_message_can_be_registered(
    client,
    app,
    monkeypatch,
):
    service = prepare_connected(monkeypatch)
    general = one_general_event_result()
    es_only = analysis_result(
        web_test_deadline=None,
        interview_datetime=None,
        event_datetime=None,
    )

    register_one_general_event(client, app, general)
    token = issue_token(app, result=es_only)
    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows(es_only)),
    )

    assert response.status_code == 302
    assert len(service.calls) == 2
    with app.app_context():
        assert {
            row.event_type for row in EmailCalendarRegistration.query.all()
        } == {"event_datetime", "es_deadline"}


class RemoteStatusCalendarService:
    def __init__(self, error=None, event=None):
        self.error = error
        self.event = event or {"id": "remote-event", "status": "confirmed"}
        self.get_calls = 0
        self.create_calls = 0

    def get_calendar_event(self, event_id):
        self.get_calls += 1
        if self.error:
            raise self.error
        return self.event

    def create_reviewed_event(self, *args, **kwargs):
        self.create_calls += 1
        return "unexpected-created-event"


class RemoteErrorResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class RemoteGoogleError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.response = RemoteErrorResponse(status_code)


def seed_registration(app, event_id="private-google-event-id"):
    with app.app_context():
        EmailCalendarRegistrationService("test-user").create(
            MESSAGE_ID,
            "event_datetime",
            ConnectedCredentialStore().get_calendar_credential(),
            event_id,
        )


@pytest.mark.parametrize(
    ("error", "classification"),
    [
        (
            CalendarEventNotFoundError(
                "calendar_event_get",
                RemoteGoogleError(404, "private-google-event-id message-calendar"),
            ),
            "not_found",
        ),
        (
            CalendarEventNotFoundError(
                "calendar_event_get",
                RemoteGoogleError(410, "private-google-event-id message-calendar"),
            ),
            "gone",
        ),
        (
            CalendarEventNotFoundError(
                "calendar_event_status_check",
                CalendarEventCancelledError(),
            ),
            "cancelled",
        ),
    ],
)
def test_remote_deleted_registration_is_cleared_without_recreation(
    client,
    app,
    monkeypatch,
    caplog,
    error,
    classification,
):
    prepare_connected(monkeypatch)
    seed_registration(app)
    result = one_general_event_result()
    token = issue_token(app, result=result)
    remote = RemoteStatusCalendarService(error=error)
    monkeypatch.setattr(
        email_routes,
        "get_google_calendar_service",
        lambda: remote,
    )

    with caplog.at_level(logging.INFO):
        response = client.post(
            f"/emails/{MESSAGE_ID}/analysis/calendar/status",
            data={"token": token, "return_to": RETURN_TO},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "登録済み状態を解除しました。再度登録してください" in (
        response.get_data(as_text=True)
    )
    assert remote.get_calls == 1
    assert remote.create_calls == 0
    with app.app_context():
        assert EmailCalendarRegistration.query.count() == 0
    assert "private-google-event-id" not in caplog.text
    assert MESSAGE_ID not in caplog.text
    if classification != "cancelled":
        assert f"api_error={classification}" in caplog.text
    else:
        assert "event_status=cancelled" in caplog.text


def test_general_remote_error_preserves_registered_state(
    client,
    app,
    monkeypatch,
):
    prepare_connected(monkeypatch)
    seed_registration(app)
    token = issue_token(app, result=one_general_event_result())
    remote = RemoteStatusCalendarService(
        error=CalendarServiceError(
            "calendar_event_get",
            RemoteGoogleError(503, "private response body"),
        )
    )
    monkeypatch.setattr(
        email_routes,
        "get_google_calendar_service",
        lambda: remote,
    )

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar/status",
        data={"token": token, "return_to": RETURN_TO},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "登録済み状態は維持しています" in response.get_data(as_text=True)
    assert remote.create_calls == 0
    with app.app_context():
        assert EmailCalendarRegistration.query.count() == 1


def test_google_api_failure_does_not_persist_registration(
    client,
    app,
    monkeypatch,
):
    failing = RecordingCalendarService(fail_titles={"失敗する予定"})
    prepare_connected(monkeypatch, failing)
    result = one_general_event_result()
    token = issue_token(app, result=result)
    candidates = candidate_rows(result)
    candidates[0]["title"] = "失敗する予定"

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidates),
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert EmailCalendarRegistration.query.count() == 0


def test_tracking_commit_failure_warns_google_event_may_exist(
    client,
    app,
    monkeypatch,
):
    class FailingRegistrationService:
        def get(self, *args):
            return None

        def create(self, *args):
            raise EmailCalendarRegistrationStorageError(
                "db_commit",
                RuntimeError("private database details"),
            )

    service = prepare_connected(monkeypatch)
    monkeypatch.setattr(
        email_routes,
        "get_email_calendar_registration_service",
        lambda: FailingRegistrationService(),
    )
    result = one_general_event_result()
    token = issue_token(app, result=result)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_post_data(token, candidate_rows(result)),
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert len(service.calls) == 1
    assert "Google側に予定が作成された可能性があります" in html
    assert "再操作する前にGoogle Calendarを確認してください" in html
    with app.app_context():
        assert EmailCalendarRegistration.query.count() == 0


def test_registration_status_check_requires_csrf(monkeypatch):
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "email-calendar-status-csrf-secret"

    csrf_app = create_app(CsrfConfig)
    prepare_connected(monkeypatch)
    remote = RemoteStatusCalendarService()
    monkeypatch.setattr(
        email_routes,
        "get_google_calendar_service",
        lambda: remote,
    )
    with csrf_app.app_context():
        db.create_all()
        seed_registration(csrf_app)
        token = issue_token(csrf_app, result=one_general_event_result())
        test_client = csrf_app.test_client()
        confirmation = test_client.get(
            f"/emails/{MESSAGE_ID}/analysis/calendar",
            query_string={"token": token},
        )
        status_form = re.search(
            r'<form method="post" action="[^"]+/analysis/calendar/status">'
            r'(.*?)</form>',
            confirmation.get_data(as_text=True),
            re.DOTALL,
        )
        assert status_form is not None
        csrf_match = re.search(
            r'name="csrf_token"[^>]+value="([^"]+)"',
            status_form.group(1),
        )
        assert csrf_match is not None

        rejected = test_client.post(
            f"/emails/{MESSAGE_ID}/analysis/calendar/status",
            data={"token": token, "return_to": RETURN_TO},
        )
        accepted = test_client.post(
            f"/emails/{MESSAGE_ID}/analysis/calendar/status",
            data={
                "csrf_token": csrf_match.group(1),
                "token": token,
                "return_to": RETURN_TO,
            },
        )

        assert rejected.status_code == 400
        assert accepted.status_code == 302
        assert remote.get_calls == 1
        db.session.remove()
        db.drop_all()
