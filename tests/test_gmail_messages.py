import base64
import html
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.emails import routes as email_routes
from app.emails.pagination import encode_page_history
from app.integrations import gmail_service
from app.integrations.credential_store import GoogleCredentialStore
from app.integrations.gmail_service import (
    GMAIL_MAX_RESULTS,
    GMAIL_RECENT_QUERY,
    MAX_BODY_CHARS,
    GmailMessageDetail,
    GmailMessagePage,
    GmailMessageSummary,
    GmailServiceError,
    GoogleGmailService,
    build_gmail_search_query,
    extract_body_text,
    parse_message_detail,
)


def encode_body(value):
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip(
        "="
    )


def make_credentials(
    token="gmail-access",
    refresh_token="gmail-refresh",
):
    return SimpleNamespace(
        token=token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        granted_scopes=None,
        expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


def save_gmail_credential(email="jobs@example.com"):
    return GoogleCredentialStore("test-user").save_gmail_credential(
        make_credentials(),
        email=email,
    )


def summary(message_id="message-1"):
    return GmailMessageSummary(
        message_id=message_id,
        subject="面接日程のご案内",
        sender="採用担当 <recruit@example.com>",
        received_at=datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc),
        date_header="Sat, 8 Aug 2026 10:30:00 +0000",
        snippet="面接日程をご確認ください。",
    )


class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeGmailAPI:
    def __init__(self, list_response=None, resources=None, list_error=None):
        self.list_response = list_response or {"messages": []}
        self.resources = resources or {}
        self.list_error = list_error
        self.list_calls = []
        self.get_calls = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeRequest(self.list_response, self.list_error)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        resource = self.resources.get(kwargs["id"])
        if isinstance(resource, Exception):
            return FakeRequest(error=resource)
        return FakeRequest(resource)


def gmail_resource(message_id="message-1", body="本文です"):
    return {
        "id": message_id,
        "internalDate": "1786156200000",
        "snippet": "メールの短いプレビュー",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "選考結果のお知らせ"},
                {
                    "name": "From",
                    "value": "採用担当 <recruit@example.com>",
                },
                {"name": "To", "value": "student@example.com"},
                {"name": "Date", "value": "Sat, 8 Aug 2026 10:30:00 +0900"},
            ],
            "body": {"data": encode_body(body)},
        },
    }


def test_email_list_requires_gmail_connection_without_api_call(
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        email_routes,
        "get_gmail_service",
        lambda: pytest.fail("Gmail API must not be called"),
    )

    response = client.get("/emails")

    assert response.status_code == 200
    html_text = response.get_data(as_text=True)
    assert "Gmailが未連携です" in html_text
    assert "/settings/integrations" in html_text


def test_connected_email_list_is_displayed(client, app, monkeypatch):
    with app.app_context():
        save_gmail_credential()

    class FakeService:
        def list_messages(self, query, page_token):
            return GmailMessagePage((summary(),), None)

    monkeypatch.setattr(email_routes, "get_gmail_service", FakeService)

    response = client.get("/emails")

    assert response.status_code == 200
    html_text = response.get_data(as_text=True)
    assert "就活メール" in html_text
    assert "jobs@example.com" in html_text
    assert "面接日程のご案内" in html_text
    assert "採用担当" in html_text
    assert "面接日程をご確認ください。" in html_text
    assert "message-1" in html_text
    assert "Message ID:" not in html_text


def test_empty_gmail_search_has_clear_reset_action(client, app, monkeypatch):
    with app.app_context():
        save_gmail_credential()

    class EmptyService:
        def list_messages(self, query, page_token):
            return GmailMessagePage((), None)

    monkeypatch.setattr(email_routes, "get_gmail_service", EmptyService)

    response = client.get("/emails", query_string={"q": "見つからない条件"})
    html_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "条件に一致する就活メールはありません" in html_text
    assert "検索条件をリセット" in html_text
    assert 'href="/emails/"' in html_text


def test_search_query_is_sent_to_gmail_api_without_local_filtering(
    client,
    app,
    monkeypatch,
):
    captured = {}
    with app.app_context():
        save_gmail_credential()

    class FakeService:
        def list_messages(self, query, page_token):
            captured.update(query=query, page_token=page_token)
            return GmailMessagePage((summary(),), None)

    monkeypatch.setattr(email_routes, "get_gmail_service", FakeService)

    response = client.get(
        "/emails",
        query_string={"q": "  面接  ", "page_token": "next-token"},
    )

    assert response.status_code == 200
    assert captured == {"query": "面接", "page_token": "next-token"}
    assert 'value="面接"' in response.get_data(as_text=True)


def test_gmail_query_always_includes_recent_90_days():
    assert build_gmail_search_query("") == GMAIL_RECENT_QUERY
    assert build_gmail_search_query("面接") == "newer_than:90d (面接)"


def test_service_limits_list_to_50_and_uses_gmail_query(app, monkeypatch):
    resources = {
        f"message-{index}": gmail_resource(f"message-{index}")
        for index in range(55)
    }
    api = FakeGmailAPI(
        list_response={
            "messages": [{"id": message_id} for message_id in resources],
            "nextPageToken": "next-page",
        },
        resources=resources,
    )
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        save_gmail_credential()
        monkeypatch.setattr(gmail_service, "build", lambda *args, **kwargs: api)

        page = GoogleGmailService(
            store,
            "client-id",
            "client-secret",
        ).list_messages("説明会", page_token="current-page")

    assert len(page.messages) == GMAIL_MAX_RESULTS
    assert page.next_page_token == "next-page"
    assert len(api.get_calls) == GMAIL_MAX_RESULTS
    assert api.list_calls == [
        {
            "userId": "me",
            "maxResults": 50,
            "q": "newer_than:90d (説明会)",
            "fields": "messages/id,nextPageToken",
            "pageToken": "current-page",
        }
    ]
    assert all(call["format"] == "metadata" for call in api.get_calls)
    assert all(
        call["fields"] == "id,internalDate,snippet,payload/headers"
        for call in api.get_calls
    )


def test_next_and_previous_page_links_preserve_search(client, app, monkeypatch):
    with app.app_context():
        save_gmail_credential()

    class FakeService:
        def list_messages(self, query, page_token):
            return GmailMessagePage((summary(),), "next-token")

    monkeypatch.setattr(email_routes, "get_gmail_service", FakeService)
    history = encode_page_history(["", "first-token"])

    response = client.get(
        "/emails",
        query_string={
            "q": "面接",
            "page_token": "second-token",
            "history": history,
        },
    )

    html_text = unquote(html.unescape(response.get_data(as_text=True)))
    assert "次へ →" in html_text
    assert "← 戻る" in html_text
    assert "page_token=next-token" in html_text
    assert "page_token=first-token" in html_text
    assert "q=面接" in html_text


def test_invalid_page_token_is_handled_without_500(client, app):
    with app.app_context():
        save_gmail_credential()

    response = client.get(
        "/emails",
        query_string={"page_token": "x" * 3000},
    )

    assert response.status_code == 200
    assert "指定されたページまたはメール情報が正しくありません。" in (
        response.get_data(as_text=True)
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "再認証または権限"),
        (403, "再認証または権限"),
        (404, "見つかりませんでした"),
        (429, "一時的に制限"),
        (503, "一時的な問題"),
    ],
)
def test_gmail_api_errors_are_displayed_safely(
    client,
    app,
    monkeypatch,
    status,
    expected,
):
    with app.app_context():
        save_gmail_credential()
    api_error = RuntimeError("sensitive Gmail API response")
    api_error.status_code = status

    class FailingService:
        def list_messages(self, query, page_token):
            raise GmailServiceError("gmail_messages_list", api_error)

    monkeypatch.setattr(email_routes, "get_gmail_service", FailingService)

    response = client.get("/emails")

    assert response.status_code == 200
    html_text = response.get_data(as_text=True)
    assert expected in html_text
    assert "sensitive Gmail API response" not in html_text


def test_detail_requests_full_message_and_extracts_headers(app, monkeypatch):
    resource = gmail_resource()
    api = FakeGmailAPI(resources={"message-1": resource})
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        save_gmail_credential()
        monkeypatch.setattr(gmail_service, "build", lambda *args, **kwargs: api)

        detail = GoogleGmailService(
            store,
            "client-id",
            "client-secret",
        ).get_message("message-1")

    assert api.get_calls == [
        {"userId": "me", "id": "message-1", "format": "full"}
    ]
    assert detail.subject == "選考結果のお知らせ"
    assert detail.sender == "採用担当 <recruit@example.com>"
    assert detail.recipient == "student@example.com"
    assert detail.date_header == "Sat, 8 Aug 2026 10:30:00 +0900"
    assert detail.snippet == "メールの短いプレビュー"
    assert detail.body_text == "本文です"


def test_text_plain_body_is_preferred_in_multipart_message():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/html",
                "body": {"data": encode_body("<p>HTML本文</p>")},
            },
            {
                "mimeType": "text/plain",
                "body": {"data": encode_body("プレーン本文")},
            },
        ],
    }

    assert extract_body_text(payload) == "プレーン本文"


def test_nested_multipart_plain_body_is_extracted():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": encode_body("ネスト本文")},
                    }
                ],
            }
        ],
    }

    assert extract_body_text(payload) == "ネスト本文"


def test_html_body_is_converted_to_safe_text_without_script():
    payload = {
        "mimeType": "text/html",
        "body": {
            "data": encode_body(
                "<h1>面接案内</h1><script>secretScript()</script>"
                "<p>日時：8月10日&nbsp;10時</p>"
            )
        },
    }

    body = extract_body_text(payload)

    assert "面接案内" in body
    assert "日時：8月10日 10時" in body
    assert "secretScript" not in body
    assert "<h1>" not in body


def test_base64url_body_without_padding_is_decoded():
    encoded = encode_body("日本語の本文")
    assert not encoded.endswith("=")

    body = extract_body_text(
        {"mimeType": "text/plain", "body": {"data": encoded}}
    )

    assert body == "日本語の本文"


def test_body_charset_header_is_respected():
    encoded = base64.urlsafe_b64encode(
        "日本語メール".encode("iso-2022-jp")
    ).decode("ascii")
    payload = {
        "mimeType": "text/plain",
        "headers": [
            {
                "name": "Content-Type",
                "value": "text/plain; charset=iso-2022-jp",
            }
        ],
        "body": {"data": encoded},
    }

    assert extract_body_text(payload) == "日本語メール"


def test_attachment_body_is_not_fetched_or_displayed():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": encode_body("表示本文")},
            },
            {
                "mimeType": "text/plain",
                "filename": "secret.txt",
                "body": {
                    "attachmentId": "attachment-id",
                    "data": encode_body("添付内容"),
                },
            },
        ],
    }

    body = extract_body_text(payload)

    assert body == "表示本文"
    assert "添付内容" not in body


def test_long_body_is_truncated_to_display_limit():
    body = extract_body_text(
        {
            "mimeType": "text/plain",
            "body": {"data": encode_body("長" * (MAX_BODY_CHARS + 100))},
        }
    )

    assert len(body) == MAX_BODY_CHARS
    assert body.endswith("…")


def test_detail_page_escapes_service_output(client, app, monkeypatch):
    with app.app_context():
        save_gmail_credential()

    unsafe_detail = GmailMessageDetail(
        message_id="message-1",
        subject="<img src=x onerror=alert(1)>",
        sender="sender@example.com",
        recipient="student@example.com",
        received_at=None,
        date_header="Date",
        snippet="<b>snippet</b>",
        body_text="<script>alert('xss')</script>",
    )

    class FakeService:
        def get_message(self, message_id):
            return unsafe_detail

    monkeypatch.setattr(email_routes, "get_gmail_service", FakeService)

    response = client.get("/emails/message-1")

    html_text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "<script>alert" not in html_text
    assert "&lt;script&gt;alert" in html_text
    assert "<img src=x" not in html_text
    assert "|safe" not in html_text


def test_expired_gmail_token_is_refreshed_and_saved_without_calendar(
    monkeypatch,
):
    saved = []
    gmail_record = SimpleNamespace(google_account_email="jobs@example.com")
    calendar_record = SimpleNamespace(access_token="calendar-access")
    credentials = SimpleNamespace(
        expired=True,
        refresh_token="gmail-refresh",
        token="expired-gmail-access",
    )

    def refresh(request):
        credentials.expired = False
        credentials.token = "refreshed-gmail-access"

    credentials.refresh = refresh

    class StrictStore:
        def get_gmail_credential(self):
            return gmail_record

        def get_calendar_credential(self):
            pytest.fail("Calendar Credential must not be requested")

        def to_google_credentials(self, record, client_id, client_secret):
            assert record is gmail_record
            return credentials

        def save_gmail_credential(self, value, email=None):
            saved.append((value.token, email))

        def save_calendar_credential(self, *args, **kwargs):
            pytest.fail("Calendar Credential must not be updated")

    api = FakeGmailAPI(list_response={"messages": []})
    monkeypatch.setattr(gmail_service, "Request", lambda: object())
    monkeypatch.setattr(gmail_service, "build", lambda *args, **kwargs: api)

    page = GoogleGmailService(
        StrictStore(),
        "client-id",
        "client-secret",
    ).list_messages()

    assert page.messages == ()
    assert saved == [("refreshed-gmail-access", "jobs@example.com")]
    assert calendar_record.access_token == "calendar-access"


def test_gmail_api_uses_only_gmail_credential(monkeypatch):
    gmail_record = SimpleNamespace(google_account_email="jobs@example.com")
    credentials = SimpleNamespace(expired=False, refresh_token="gmail-refresh")

    class StrictStore:
        def get_gmail_credential(self):
            return gmail_record

        def get_calendar_credential(self):
            pytest.fail("Calendar Credential must not be requested")

        def to_google_credentials(self, record, client_id, client_secret):
            assert record is gmail_record
            return credentials

    api = FakeGmailAPI(list_response={"messages": []})

    def fake_build(api_name, version, **kwargs):
        assert (api_name, version) == ("gmail", "v1")
        assert kwargs["credentials"] is credentials
        assert kwargs["cache_discovery"] is False
        return api

    monkeypatch.setattr(gmail_service, "build", fake_build)

    GoogleGmailService(
        StrictStore(),
        "client-id",
        "client-secret",
    ).list_messages()


def test_gmail_failure_logs_exclude_mail_and_token_data(
    client,
    app,
    monkeypatch,
    caplog,
):
    secrets = (
        "Confidential interview subject",
        "sender-secret@example.com",
        "private email body",
        "secret-access-token",
        "full-message-id-secret",
    )
    with app.app_context():
        save_gmail_credential()
    error = RuntimeError(" ".join(secrets))
    error.status_code = 500

    class FailingService:
        def list_messages(self, query, page_token):
            raise GmailServiceError("gmail_messages_list", error)

    monkeypatch.setattr(email_routes, "get_gmail_service", FailingService)
    caplog.set_level(logging.ERROR)

    response = client.get("/emails")

    assert response.status_code == 200
    assert "operation=list" in caplog.text
    assert "stage=gmail_messages_list" in caplog.text
    for secret in secrets:
        assert secret not in caplog.text


def test_navigation_contains_job_hunting_email_link(client):
    html_text = client.get("/").get_data(as_text=True)
    assert "就活メール" in html_text
    assert re.search(r'href="/emails/?"', html_text)


def test_parse_message_detail_uses_safe_defaults():
    detail = parse_message_detail({"id": "message-1", "payload": {}})

    assert detail.subject == "（件名なし）"
    assert detail.sender == "送信者不明"
    assert detail.recipient == "宛先不明"
    assert detail.body_text == "本文を取得できませんでした。"
