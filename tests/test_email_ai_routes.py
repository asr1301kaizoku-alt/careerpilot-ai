import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app
from app.emails import routes as email_routes
from app.extensions import db
from app.integrations.credential_store import GoogleCredentialStore
from app.integrations.gmail_service import (
    GmailMessageDetail,
    GmailMessagePage,
    GmailServiceError,
)
from app.services.email_ai_service import (
    EmailAIServiceError,
    validate_analysis_payload,
)
from config import TestConfig


def make_credentials():
    return SimpleNamespace(
        token="gmail-access-token",
        refresh_token="gmail-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        granted_scopes=None,
        expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


def save_gmail_credential():
    return GoogleCredentialStore("test-user").save_gmail_credential(
        make_credentials(),
        email="jobs@example.com",
    )


def email_detail():
    return GmailMessageDetail(
        message_id="message-1",
        subject="一次面接のご案内",
        sender="採用担当 <recruit@example.com>",
        recipient="student@example.com",
        received_at=datetime(2026, 8, 9, 9, 0, tzinfo=timezone.utc),
        date_header="Sun, 9 Aug 2026 18:00:00 +0900",
        snippet="一次面接をご予約ください。",
        body_text="一次面接は8月20日13時です。マイページから予約してください。",
    )


def analysis_payload(**overrides):
    payload = {
        "company_name": "株式会社キャリアパイロット",
        "mail_category": "interview",
        "es_deadline": None,
        "web_test_deadline": None,
        "interview_datetime": "2026-08-20T13:00:00+09:00",
        "event_datetime": None,
        "es_deadline_text": None,
        "web_test_deadline_text": None,
        "interview_datetime_text": None,
        "event_datetime_text": None,
        "action_items": ["マイページから面接を予約する"],
        "important_notes": ["オンライン面接"],
        "summary": "一次面接の日程案内です。予約手続きが必要です。",
        "confidence": "medium",
        "evidence": {
            "company_name": "株式会社キャリアパイロット",
            "es_deadline": None,
            "web_test_deadline": None,
            "interview_datetime": "一次面接は8月20日13時です",
            "event_datetime": None,
        },
    }
    payload.update(overrides)
    return payload


class FakeGmailService:
    def __init__(self, error=None):
        self.error = error
        self.list_calls = 0
        self.detail_calls = 0

    def list_messages(self, query, page_token):
        self.list_calls += 1
        return GmailMessagePage((), None)

    def get_message(self, message_id):
        self.detail_calls += 1
        if self.error:
            raise self.error
        return email_detail()


class FakeAIService:
    is_configured = True

    def __init__(self, result=None, error=None):
        self.result = result or validate_analysis_payload(analysis_payload())
        self.error = error
        self.calls = []

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result, sum(len(str(value or "")) for value in kwargs.values())


def prepare(app, monkeypatch, ai_service=None, gmail_service=None):
    with app.app_context():
        save_gmail_credential()
    gmail_service = gmail_service or FakeGmailService()
    ai_service = ai_service or FakeAIService()
    monkeypatch.setattr(
        email_routes,
        "get_gmail_service",
        lambda: gmail_service,
    )
    monkeypatch.setattr(
        email_routes,
        "get_email_ai_service",
        lambda: ai_service,
    )
    return gmail_service, ai_service


def test_email_list_does_not_call_gemini(client, app, monkeypatch):
    with app.app_context():
        save_gmail_credential()
    gmail = FakeGmailService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: gmail)
    monkeypatch.setattr(
        email_routes,
        "get_email_ai_service",
        lambda: pytest.fail("Gemini service must not be created for list view"),
    )

    response = client.get("/emails")

    assert response.status_code == 200
    assert gmail.list_calls == 1


def test_email_detail_only_does_not_call_gemini_and_shows_privacy_notice(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    gmail, _ = prepare(app, monkeypatch)
    monkeypatch.setattr(
        email_routes,
        "get_email_ai_service",
        lambda: pytest.fail("Gemini service must not be created for GET detail"),
    )

    response = client.get("/emails/message-1")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert gmail.detail_calls == 1
    assert "AIでメールを解析" in html
    assert "Gemini APIへ送信されます" in html
    assert 'method="post"' in html
    assert '/emails/message-1/analyze' in html
    assert "AI解析結果" not in html


def test_email_detail_orders_preview_ai_and_collapsed_body_last(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    prepare(app, monkeypatch)

    response = client.get("/emails/message-1")
    html = response.get_data(as_text=True)

    preview_position = html.index("email-preview-box")
    ai_position = html.index('id="email-ai-section"')
    body_position = html.index('id="emailBodyAccordion"')
    assert preview_position < ai_position < body_position
    assert 'class="accordion-button collapsed"' in html
    assert 'data-bs-toggle="collapse"' in html
    assert 'data-bs-target="#emailBodyCollapse"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="emailBodyCollapse"' in html
    assert 'class="accordion-collapse collapse"' in html
    assert 'class="accordion-collapse collapse show"' not in html
    assert 'data-bs-parent="#emailBodyAccordion"' in html
    collapse_position = html.index('id="emailBodyCollapse"')
    message_position = html.index(email_detail().body_text)
    assert body_position < collapse_position < message_position


def test_analysis_route_is_post_only(client):
    assert client.get("/emails/message-1/analyze").status_code == 405


def test_analysis_post_calls_gmail_then_ai_and_displays_structured_result(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    gmail, ai = prepare(app, monkeypatch)

    response = client.post(
        "/emails/message-1/analyze",
        data={"return_to": "/emails/?q=面接&page_token=page-2"},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert gmail.detail_calls == 1
    assert len(ai.calls) == 1
    sent = ai.calls[0]
    assert sent == {
        "subject": email_detail().subject,
        "sender": email_detail().sender,
        "received_at": email_detail().received_at,
        "body_text": email_detail().body_text,
    }
    assert "AI解析結果" in html
    assert "株式会社キャリアパイロット" in html
    assert "面接" in html
    assert "2026/08/20 13:00" in html
    assert "マイページから面接を予約する" in html
    assert "オンライン面接" in html
    assert "信頼度：中" in html
    assert "必ず元メールを確認" in html
    assert "応募先へ反映" in html
    assert "/emails/message-1/analysis/apply?" in html
    assert "必要な対応をチェックリストへ追加" in html
    assert "/emails/message-1/analysis/checklist?" in html
    assert "予定をGoogleカレンダーへ追加" in html
    assert "/emails/message-1/analysis/calendar?" in html
    assert len(app.extensions["email_analysis_apply_store"]) == 1
    assert len(app.extensions["email_analysis_checklist_store"]) == 1
    assert len(app.extensions["email_analysis_calendar_store"]) == 1


def test_analysis_result_displays_explicit_general_event_range(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    start = "2026-08-22T10:00:00+09:00"
    result = validate_analysis_payload(
        analysis_payload(
            interview_datetime=None,
            event_datetime=start,
            event_start_datetime=start,
            event_end_datetime="2026-08-22T17:00:00+09:00",
            evidence={
                "company_name": "株式会社キャリアパイロット",
                "es_deadline": None,
                "web_test_deadline": None,
                "interview_datetime": None,
                "event_datetime": "2026年8月22日10:00～17:00",
            },
        )
    )
    prepare(app, monkeypatch, ai_service=FakeAIService(result=result))

    response = client.post("/emails/message-1/analyze")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "2026/08/22 10:00 ～ 2026/08/22 17:00" in html


def test_analysis_result_precedes_body_and_post_uses_feedback_anchor(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    prepare(app, monkeypatch)

    response = client.post(
        "/emails/message-1/analyze",
        data={"return_to": "/emails/?q=面接&page_token=page-2"},
    )
    html = response.get_data(as_text=True)

    assert (
        'action="/emails/message-1/analyze#ai-analysis-feedback"'
        in html
    )
    ai_position = html.index('id="email-ai-section"')
    feedback_position = html.index('id="ai-analysis-feedback"')
    result_position = html.index('id="ai-analysis-result"')
    body_position = html.index('id="emailBodyAccordion"')
    assert ai_position < feedback_position < result_position < body_position
    assert 'class="ai-analysis-footer"' in html
    assert (
        'name="return_to" type="hidden" '
        'value="/emails/?q=%E9%9D%A2%E6%8E%A5&amp;page_token=page-2"'
        in html
    )


def test_analysis_post_preserves_return_to_and_result_is_not_persisted(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    _, ai = prepare(app, monkeypatch)
    return_to = "/emails/?q=面接&page_token=page-2"

    analyzed = client.post(
        "/emails/message-1/analyze",
        data={"return_to": return_to},
    )
    reloaded = client.get(
        "/emails/message-1",
        query_string={"return_to": return_to},
    )

    analyzed_html = analyzed.get_data(as_text=True)
    expected = "/emails/?q=%E9%9D%A2%E6%8E%A5&amp;page_token=page-2"
    assert f'href="{expected}"' in analyzed_html
    assert f'name="return_to" type="hidden" value="{expected}"' in analyzed_html
    assert "AI解析結果" in analyzed_html
    assert "AI解析結果" not in reloaded.get_data(as_text=True)
    assert len(ai.calls) == 1


def test_missing_api_key_disables_button_and_post_fails_without_500(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = ""
    gmail, _ = prepare(app, monkeypatch)
    monkeypatch.undo()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: gmail)

    detail_response = client.get("/emails/message-1")
    post_response = client.post("/emails/message-1/analyze", data={})

    assert detail_response.status_code == post_response.status_code == 200
    assert "AI解析は現在利用できません" in detail_response.get_data(as_text=True)
    assert "disabled" in detail_response.get_data(as_text=True)
    assert "Gemini APIキーの設定が必要" in post_response.get_data(as_text=True)


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("api_error", "AI解析に失敗しました"),
        ("rate_limited", "一時的に制限"),
        ("timeout", "タイムアウト"),
        ("model_not_found_or_unsupported", "モデルを利用できません"),
        ("unknown_not_found", "接続先またはリクエスト設定"),
        ("invalid_structured_response", "AI解析に失敗しました"),
        ("empty_or_blocked_response", "AI解析に失敗しました"),
    ],
)
def test_ai_errors_are_displayed_without_500(
    client,
    app,
    monkeypatch,
    classification,
    expected,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    original = RuntimeError("private Gemini response")
    error = EmailAIServiceError("api_request", original, classification)
    ai = FakeAIService(error=error)
    prepare(app, monkeypatch, ai_service=ai)

    response = client.post("/emails/message-1/analyze", data={})

    assert response.status_code == 200
    assert expected in response.get_data(as_text=True)
    assert "private Gemini response" not in response.get_data(as_text=True)


def test_analysis_error_is_placed_at_feedback_anchor_before_body(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    error = EmailAIServiceError(
        "api_request",
        RuntimeError("private Gemini response"),
        "api_error",
    )
    prepare(app, monkeypatch, ai_service=FakeAIService(error=error))

    response = client.post("/emails/message-1/analyze", data={})
    html = response.get_data(as_text=True)

    feedback_position = html.index('id="ai-analysis-feedback"')
    error_position = html.index("AI解析に失敗しました")
    body_position = html.index('id="emailBodyAccordion"')
    assert feedback_position < error_position < body_position
    assert 'id="ai-analysis-result"' not in html


def test_email_ai_and_accordion_mobile_css_remains_scoped():
    css = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "css"
        / "style.css"
    ).read_text(encoding="utf-8")

    assert ".email-ai-panel form, .email-ai-panel .btn { width: 100%; }" in css
    assert ".ai-analysis-result .card-body { padding: 1rem !important; }" in css
    assert ".email-body-accordion .accordion-button" in css
    assert ".email-body-accordion .email-body-text" in css


def test_deleted_gmail_message_does_not_call_gemini_and_keeps_return_link(
    client,
    app,
    monkeypatch,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    not_found = RuntimeError("private Gmail response")
    not_found.status_code = 404
    gmail_error = GmailServiceError("gmail_message_get", not_found)
    gmail = FakeGmailService(error=gmail_error)
    with app.app_context():
        save_gmail_credential()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: gmail)
    monkeypatch.setattr(
        email_routes,
        "get_email_ai_service",
        lambda: pytest.fail("Gemini must not run when Gmail message is missing"),
    )
    return_to = "/emails/?q=面接&page_token=page-2"

    response = client.post(
        "/emails/message-1/analyze",
        data={"return_to": return_to},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "見つかりませんでした" in html
    assert (
        'href="/emails/?q=%E9%9D%A2%E6%8E%A5&amp;page_token=page-2"'
        in html
    )


def test_ai_result_is_html_escaped(client, app, monkeypatch):
    app.config["GEMINI_API_KEY"] = "configured-key"
    payload = analysis_payload(
        company_name="<script>company()</script>",
        summary="<img src=x onerror=alert(1)>",
        evidence={
            **analysis_payload()["evidence"],
            "company_name": "<script>evidence()</script>",
        },
    )
    ai = FakeAIService(result=validate_analysis_payload(payload))
    prepare(app, monkeypatch, ai_service=ai)

    response = client.post("/emails/message-1/analyze", data={})
    html = response.get_data(as_text=True)

    assert "<script>company" not in html
    assert "&lt;script&gt;company" in html
    assert "<img src=x" not in html
    assert "|safe" not in html


def test_ai_logs_exclude_mail_prompt_output_and_secrets(
    client,
    app,
    monkeypatch,
    caplog,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    ai = FakeAIService()
    prepare(app, monkeypatch, ai_service=ai)

    with caplog.at_level(logging.INFO):
        response = client.post("/emails/message-1/analyze", data={})

    assert response.status_code == 200
    assert "operation=email_ai_analysis" in caplog.text
    for secret in (
        "一次面接のご案内",
        "recruit@example.com",
        "student@example.com",
        "一次面接は8月20日",
        "株式会社キャリアパイロット",
        "マイページから面接を予約する",
        "message-1",
        "configured-key",
        "gmail-access-token",
        "gmail-refresh-token",
    ):
        assert secret not in caplog.text


def test_404_failure_log_uses_safe_classification_without_private_data(
    client,
    app,
    monkeypatch,
    caplog,
):
    app.config["GEMINI_API_KEY"] = "configured-key"
    original = RuntimeError(
        "private response with configured-key and 一次面接のご案内"
    )
    original.code = 404
    error = EmailAIServiceError(
        "api_request",
        original,
        "model_not_found_or_unsupported",
    )
    prepare(app, monkeypatch, ai_service=FakeAIService(error=error))

    with caplog.at_level(logging.ERROR):
        response = client.post("/emails/message-1/analyze", data={})

    assert response.status_code == 200
    assert "http_status=404" in caplog.text
    assert "classification=model_not_found_or_unsupported" in caplog.text
    assert "configured-key" not in caplog.text
    assert "一次面接のご案内" not in caplog.text
    assert "private response" not in caplog.text


def test_analysis_form_requires_csrf_when_enabled(monkeypatch):
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "email-ai-csrf-secret"
        GEMINI_API_KEY = "configured-key"

    csrf_app = create_app(CsrfConfig)
    gmail = FakeGmailService()
    ai = FakeAIService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: gmail)
    monkeypatch.setattr(email_routes, "get_email_ai_service", lambda: ai)

    with csrf_app.app_context():
        db.create_all()
        save_gmail_credential()
        test_client = csrf_app.test_client()
        assert test_client.post("/emails/message-1/analyze").status_code == 400

        detail_response = test_client.get("/emails/message-1")
        token_match = re.search(
            r'name="csrf_token" type="hidden" value="([^"]+)"',
            detail_response.get_data(as_text=True),
        )
        assert token_match is not None

        response = test_client.post(
            "/emails/message-1/analyze",
            data={
                "csrf_token": token_match.group(1),
                "return_to": "/emails/?q=面接",
            },
        )

        assert response.status_code == 200
        assert "AI解析結果" in response.get_data(as_text=True)
        db.session.remove()
        db.drop_all()
