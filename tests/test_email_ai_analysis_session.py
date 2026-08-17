import logging
import re
from datetime import datetime, timezone
from html import unescape
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.emails import routes as email_routes
from app.emails.analysis_calendar import build_calendar_candidate_data
from app.emails.analysis_session_store import (
    EmailAnalysisSessionStore,
    gmail_connection_key,
)
from app.extensions import db
from app.integrations.gmail_service import GmailMessageDetail
from app.models import Application, ChecklistItem, EmailCalendarRegistration
from app.services.email_ai_service import validate_analysis_payload


MESSAGE_ID = "analysis-session-message"
RETURN_TO = "/emails/?q=選考&page_token=page-session"


def analysis_result():
    start = "2026-08-22T10:00:00+09:00"
    return validate_analysis_payload(
        {
            "company_name": "株式会社セッション",
            "mail_category": "event",
            "es_deadline": None,
            "web_test_deadline": None,
            "interview_datetime": None,
            "event_datetime": start,
            "event_start_datetime": start,
            "event_end_datetime": "2026-08-22T17:00:00+09:00",
            "es_deadline_text": None,
            "web_test_deadline_text": None,
            "interview_datetime_text": None,
            "event_datetime_text": None,
            "action_items": ["参加方法を確認する"],
            "important_notes": ["オンライン開催"],
            "summary": "イベント参加案内です。",
            "confidence": "high",
            "evidence": {
                "company_name": "株式会社セッション",
                "es_deadline": None,
                "web_test_deadline": None,
                "interview_datetime": None,
                "event_datetime": "8月22日10:00～17:00",
            },
        }
    )


def email_detail(message_id=MESSAGE_ID):
    return GmailMessageDetail(
        message_id=message_id,
        subject="秘密のイベント案内",
        sender="採用担当 <private@example.com>",
        recipient="student@example.com",
        received_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
        date_header="Sun, 16 Aug 2026 09:00:00 +0900",
        snippet="イベントのご案内です。",
        body_text="保存してはいけない秘密のメール本文",
    )


class FakeGmailService:
    def __init__(self):
        self.calls = 0

    def get_message(self, message_id):
        self.calls += 1
        return email_detail(message_id)


class FakeAIService:
    is_configured = True

    def __init__(self):
        self.calls = 0

    def analyze(self, **kwargs):
        self.calls += 1
        return analysis_result(), sum(len(str(value)) for value in kwargs.values())


class FakeCredentialStore:
    gmail_credential = SimpleNamespace(
        google_account_email="jobs@example.com"
    )
    calendar_credential = SimpleNamespace(
        google_account_email="calendar@example.com"
    )

    def get_gmail_credential(self):
        return self.gmail_credential

    def get_calendar_credential(self):
        return self.calendar_credential


class RecordingCalendarService:
    def __init__(self):
        self.calls = []

    def create_reviewed_event(self, title, start, end, description):
        self.calls.append((title, start, end))
        return f"google-event-{len(self.calls)}"


def prepare(monkeypatch):
    gmail = FakeGmailService()
    ai = FakeAIService()
    calendar = RecordingCalendarService()
    credential_store = FakeCredentialStore()
    monkeypatch.setattr(
        email_routes,
        "get_credential_store",
        lambda: credential_store,
    )
    monkeypatch.setattr(
        email_routes,
        "get_gmail_service",
        lambda: gmail,
    )
    monkeypatch.setattr(
        email_routes,
        "get_email_ai_service",
        lambda: ai,
    )
    monkeypatch.setattr(
        email_routes,
        "get_google_calendar_service",
        lambda: calendar,
    )
    return gmail, ai, calendar, credential_store


def token_for(html, endpoint):
    match = re.search(
        rf'href="([^"]*{re.escape(endpoint)}[^"]*)"',
        html,
    )
    assert match is not None
    return parse_qs(urlsplit(unescape(match.group(1))).query)["token"][0]


def analyze(client):
    return client.post(
        f"/emails/{MESSAGE_ID}/analyze",
        data={"return_to": RETURN_TO},
    )


def application_data(token):
    return {
        "token": token,
        "return_to": RETURN_TO,
        "apply_mode": "new",
        "application_id": "-1",
        "company_name": "株式会社セッション",
        "position_name": "企画職",
        "status": "応募予定",
        "priority": "3",
        "es_deadline": "",
        "web_test_deadline": "",
        "interview_at": "",
        "memo": "AI結果を確認して登録",
    }


def checklist_data(token, application_id):
    return {
        "token": token,
        "return_to": RETURN_TO,
        "application_id": str(application_id),
        "candidates-0-selected": "y",
        "candidates-0-title": "参加方法を確認する",
        "candidates-0-due_at": "",
    }


def calendar_data(token, application_id):
    candidate = build_calendar_candidate_data(analysis_result())[0]
    return {
        "token": token,
        "return_to": RETURN_TO,
        "application_id": str(application_id),
        "candidates-0-selected": "y",
        "candidates-0-event_type": candidate["event_type"],
        "candidates-0-title": candidate["title"],
        "candidates-0-start_at": candidate["start_at"].strftime(
            "%Y-%m-%dT%H:%M"
        ),
        "candidates-0-end_at": candidate["end_at"].strftime(
            "%Y-%m-%dT%H:%M"
        ),
    }


def master_token(app, application_token):
    with app.app_context():
        entry = app.extensions["email_analysis_apply_store"].get(
            application_token,
            MESSAGE_ID,
        )
        return entry.analysis_session_token


def test_one_analysis_session_supports_all_three_actions_without_new_gemini_call(
    client,
    app,
    monkeypatch,
):
    _, ai, calendar, _ = prepare(monkeypatch)
    analyzed = analyze(client)
    analyzed_html = analyzed.get_data(as_text=True)
    application_token = token_for(analyzed_html, "/analysis/apply")
    session_token = master_token(app, application_token)

    application_response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_data(application_token),
    )
    assert application_response.status_code == 302
    assert "analysis_session=" in application_response.location

    with app.app_context():
        assert app.extensions["email_analysis_apply_store"].get(
            application_token,
            MESSAGE_ID,
        ) is None
        application = db.session.scalar(db.select(Application))
        application_id = application.id
        session_entry = app.extensions["email_analysis_session_store"].get(
            session_token,
            MESSAGE_ID,
            gmail_connection_key("test-user", FakeCredentialStore.gmail_credential),
        )
        assert session_entry.state.application_completed is True
        assert session_entry.state.application_id == application_id

    after_application = client.get(application_response.location)
    after_application_html = after_application.get_data(as_text=True)
    assert "応募先への反映が完了しました" in after_application_html
    checklist_token = token_for(
        after_application_html,
        "/analysis/checklist",
    )
    checklist_response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_data(checklist_token, application_id),
    )
    assert checklist_response.status_code == 302
    assert "analysis_session=" in checklist_response.location
    with app.app_context():
        assert app.extensions["email_analysis_checklist_store"].get(
            checklist_token,
            MESSAGE_ID,
        ) is None

    after_checklist = client.get(checklist_response.location)
    after_checklist_html = after_checklist.get_data(as_text=True)
    assert "チェックリストへ1件追加しました" in after_checklist_html
    calendar_token = token_for(
        after_checklist_html,
        "/analysis/calendar",
    )
    calendar_response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/calendar",
        data=calendar_data(calendar_token, application_id),
    )
    assert calendar_response.status_code == 302
    assert "analysis_session=" in calendar_response.location
    with app.app_context():
        assert app.extensions["email_analysis_calendar_store"].get(
            calendar_token,
            MESSAGE_ID,
        ) is None

    final = client.get(calendar_response.location)
    final_html = final.get_data(as_text=True)
    assert "Googleカレンダー登録を実行しました（登録1件）" in final_html
    assert "登録した応募先を見る" in final_html
    assert "追加先を見る" in final_html
    assert "検索結果へ戻る" in final_html
    assert ai.calls == 1
    assert len(calendar.calls) == 1
    with app.app_context():
        assert ChecklistItem.query.filter_by(
            title="参加方法を確認する"
        ).count() == 1
        assert EmailCalendarRegistration.query.count() == 1
        assert app.extensions["email_analysis_session_store"].get(
            session_token,
            MESSAGE_ID,
            gmail_connection_key("test-user", FakeCredentialStore.gmail_credential),
        ) is not None


def test_reopened_session_reuses_result_and_issues_purpose_scoped_tokens(
    client,
    app,
    monkeypatch,
):
    _, ai, _, _ = prepare(monkeypatch)
    first = analyze(client)
    first_html = first.get_data(as_text=True)
    application_token = token_for(first_html, "/analysis/apply")
    checklist_token = token_for(first_html, "/analysis/checklist")
    session_token = master_token(app, application_token)

    wrong_purpose = client.get(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        query_string={"token": application_token, "return_to": RETURN_TO},
    )
    first_reopen = client.get(
        f"/emails/{MESSAGE_ID}",
        query_string={
            "analysis_session": session_token,
            "return_to": RETURN_TO,
        },
    )
    second_reopen = client.get(
        f"/emails/{MESSAGE_ID}",
        query_string={
            "analysis_session": session_token,
            "return_to": RETURN_TO,
        },
    )

    assert wrong_purpose.status_code == 302
    assert "AI解析結果" in first_reopen.get_data(as_text=True)
    assert "AIで再解析" in first_reopen.get_data(as_text=True)
    assert "AI解析結果" in second_reopen.get_data(as_text=True)
    assert ai.calls == 1
    with app.app_context():
        assert app.extensions["email_analysis_apply_store"].get(
            application_token,
            MESSAGE_ID,
        ) is not None
        assert app.extensions["email_analysis_checklist_store"].get(
            checklist_token,
            MESSAGE_ID,
        ) is not None


def test_session_rejects_other_message_connection_tamper_and_expiry(
    client,
    app,
    monkeypatch,
):
    clock = [100.0]
    app.extensions["email_analysis_session_store"] = EmailAnalysisSessionStore(
        ttl_seconds=600,
        clock=lambda: clock[0],
    )
    _, ai, _, _ = prepare(monkeypatch)
    analyzed = analyze(client)
    application_token = token_for(
        analyzed.get_data(as_text=True),
        "/analysis/apply",
    )
    session_token = master_token(app, application_token)

    other_message = client.get(
        "/emails/other-message",
        query_string={"analysis_session": session_token},
    )
    tampered = client.get(
        f"/emails/{MESSAGE_ID}",
        query_string={"analysis_session": "tampered-token"},
    )
    clock[0] = 701.0
    expired = client.get(
        f"/emails/{MESSAGE_ID}",
        query_string={"analysis_session": session_token},
    )

    for response in (other_message, tampered, expired):
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "AI解析結果の有効期限が切れました" in html
        assert 'id="ai-analysis-result"' not in html
    assert ai.calls == 1


def test_expired_master_rejects_still_live_derived_token(
    client,
    app,
    monkeypatch,
):
    clock = [10.0]
    app.extensions["email_analysis_session_store"] = EmailAnalysisSessionStore(
        ttl_seconds=10,
        clock=lambda: clock[0],
    )
    prepare(monkeypatch)
    analyzed = analyze(client)
    application_token = token_for(
        analyzed.get_data(as_text=True),
        "/analysis/apply",
    )
    clock[0] = 21.0

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_data(application_token),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "AI解析結果の有効期限が切れました" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert Application.query.count() == 0


def test_explicit_reanalysis_creates_new_session_without_overwriting_old(
    client,
    app,
    monkeypatch,
):
    _, ai, _, _ = prepare(monkeypatch)
    first = analyze(client)
    first_application_token = token_for(
        first.get_data(as_text=True),
        "/analysis/apply",
    )
    first_session = master_token(app, first_application_token)
    second = analyze(client)
    second_application_token = token_for(
        second.get_data(as_text=True),
        "/analysis/apply",
    )
    second_session = master_token(app, second_application_token)

    assert first_session != second_session
    assert ai.calls == 2
    assert len(app.extensions["email_analysis_session_store"]) == 2


def test_session_stores_no_body_cookie_or_database_record(
    client,
    app,
    monkeypatch,
):
    prepare(monkeypatch)
    analyzed = analyze(client)
    application_token = token_for(
        analyzed.get_data(as_text=True),
        "/analysis/apply",
    )
    session_token = master_token(app, application_token)

    with client.session_transaction() as cookie_session:
        serialized_cookie = repr(dict(cookie_session))
        assert session_token not in serialized_cookie
        assert email_detail().body_text not in serialized_cookie
    with app.app_context():
        entry = app.extensions["email_analysis_session_store"].get(
            session_token,
            MESSAGE_ID,
            gmail_connection_key("test-user", FakeCredentialStore.gmail_credential),
        )
        assert not hasattr(entry, "body_text")
        assert email_detail().body_text not in repr(entry)
        assert MESSAGE_ID not in repr(entry)
        table_names = set(db.inspect(db.engine).get_table_names())
        assert "email_analysis_sessions" not in table_names
        assert "email_analysis_results" not in table_names


def test_analysis_session_logs_are_safe(
    client,
    app,
    monkeypatch,
    caplog,
):
    _, _, _, _ = prepare(monkeypatch)
    with caplog.at_level(logging.INFO):
        analyzed = analyze(client)
        application_token = token_for(
            analyzed.get_data(as_text=True),
            "/analysis/apply",
        )
        session_token = master_token(app, application_token)
        client.get(
            f"/emails/{MESSAGE_ID}",
            query_string={"analysis_session": session_token},
        )

    assert "analysis session created" in caplog.text
    assert "analysis session reused" in caplog.text
    assert MESSAGE_ID not in caplog.text
    assert session_token not in caplog.text
    assert email_detail().subject not in caplog.text
    assert email_detail().body_text not in caplog.text
    assert email_detail().sender not in caplog.text


def test_analysis_session_store_uses_ten_minute_default_and_fixed_expiry(app):
    clock = [50.0]
    store = EmailAnalysisSessionStore(clock=lambda: clock[0])
    connection_key = gmail_connection_key(
        "test-user",
        FakeCredentialStore.gmail_credential,
    )
    token = store.save(
        MESSAGE_ID,
        connection_key,
        analysis_result(),
        RETURN_TO,
    )

    assert app.config["EMAIL_ANALYSIS_SESSION_TTL_SECONDS"] == 600
    clock[0] = 649.9
    assert store.get(token, MESSAGE_ID, connection_key) is not None
    clock[0] = 650.0
    assert store.get(token, MESSAGE_ID, connection_key) is None


def test_analysis_session_store_enforces_connection_size_and_entry_limits():
    tokens = iter(("first-token", "second-token"))
    store = EmailAnalysisSessionStore(
        max_entries=1,
        token_factory=lambda: next(tokens),
    )
    first_connection = gmail_connection_key(
        "first-user",
        FakeCredentialStore.gmail_credential,
    )
    second_connection = gmail_connection_key(
        "second-user",
        FakeCredentialStore.gmail_credential,
    )
    first = store.save(
        "first-message",
        first_connection,
        analysis_result(),
        RETURN_TO,
    )
    second = store.save(
        "second-message",
        second_connection,
        analysis_result(),
        RETURN_TO,
    )

    assert store.get(first, "first-message", first_connection) is None
    assert store.get(second, "second-message", first_connection) is None
    assert store.get(second, "second-message", second_connection) is not None
    assert len(store) == 1

    too_small = EmailAnalysisSessionStore(max_payload_bytes=128)
    with pytest.raises(ValueError, match="too large"):
        too_small.save(
            MESSAGE_ID,
            first_connection,
            analysis_result(),
            RETURN_TO,
        )
