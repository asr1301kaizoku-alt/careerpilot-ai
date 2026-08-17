import html
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.emails import routes as email_routes
from app.emails.navigation import EMAIL_LIST_PATH, safe_email_list_return_url
from app.emails.pagination import encode_page_history
from app.integrations.credential_store import GoogleCredentialStore
from app.integrations.gmail_service import (
    GmailMessageDetail,
    GmailMessagePage,
    GmailMessageSummary,
    GmailServiceError,
)


def make_credentials():
    return SimpleNamespace(
        token="gmail-access",
        refresh_token="gmail-refresh",
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


def summary():
    return GmailMessageSummary(
        message_id="message-1",
        subject="面接日程のご案内",
        sender="採用担当 <recruit@example.com>",
        received_at=datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc),
        date_header="Sat, 8 Aug 2026 10:30:00 +0000",
        snippet="面接日程をご確認ください。",
    )


def detail():
    return GmailMessageDetail(
        message_id="message-1",
        subject="面接日程のご案内",
        sender="採用担当 <recruit@example.com>",
        recipient="student@example.com",
        received_at=datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc),
        date_header="Sat, 8 Aug 2026 10:30:00 +0000",
        snippet="面接日程をご確認ください。",
        body_text="面接日時をご確認ください。",
    )


class MailService:
    def __init__(self):
        self.list_calls = []
        self.detail_calls = []

    def list_messages(self, query, page_token):
        self.list_calls.append((query, page_token))
        return GmailMessagePage((summary(),), None)

    def get_message(self, message_id):
        self.detail_calls.append(message_id)
        return detail()


def _detail_url(response):
    html_text = html.unescape(response.get_data(as_text=True))
    match = re.search(r'href="([^"]*/emails/message-1\?[^"]+)"', html_text)
    assert match is not None
    return match.group(1)


def _return_to_from_detail_url(detail_url):
    return parse_qs(urlsplit(detail_url).query)["return_to"][0]


def _back_href(response):
    html_text = html.unescape(response.get_data(as_text=True))
    match = re.search(
        r'<a class="text-decoration-none small" href="([^"]+)">',
        html_text,
    )
    assert match is not None
    return match.group(1)


def _prepare(client, app, monkeypatch):
    with app.app_context():
        save_gmail_credential()
    service = MailService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)
    return service


def test_unfiltered_list_detail_returns_to_default_list(
    client,
    app,
    monkeypatch,
):
    _prepare(client, app, monkeypatch)

    list_response = client.get("/emails")
    detail_url = _detail_url(list_response)
    detail_response = client.get(detail_url)

    assert _return_to_from_detail_url(detail_url) == EMAIL_LIST_PATH
    assert _back_href(detail_response) == EMAIL_LIST_PATH
    assert "就活メール一覧へ戻る" in detail_response.get_data(as_text=True)


def test_search_result_detail_returns_to_same_query(client, app, monkeypatch):
    _prepare(client, app, monkeypatch)

    list_response = client.get("/emails", query_string={"q": "面接"})
    detail_url = _detail_url(list_response)
    return_to = _return_to_from_detail_url(detail_url)
    detail_response = client.get(detail_url)

    assert parse_qs(urlsplit(return_to).query) == {"q": ["面接"]}
    assert _back_href(detail_response) == return_to
    assert "検索結果へ戻る" in detail_response.get_data(as_text=True)


def test_second_page_detail_preserves_page_token_and_history(
    client,
    app,
    monkeypatch,
):
    _prepare(client, app, monkeypatch)
    history = encode_page_history([""])

    list_response = client.get(
        "/emails",
        query_string={"page_token": "page-2", "history": history},
    )
    return_to = _return_to_from_detail_url(_detail_url(list_response))
    parameters = parse_qs(urlsplit(return_to).query)

    assert parameters["page_token"] == ["page-2"]
    assert parameters["history"] == [history]


def test_query_and_page_token_are_both_preserved(client, app, monkeypatch):
    _prepare(client, app, monkeypatch)

    list_response = client.get(
        "/emails",
        query_string={"q": "説明会", "page_token": "page-3"},
    )
    return_to = _return_to_from_detail_url(_detail_url(list_response))

    assert parse_qs(urlsplit(return_to).query) == {
        "q": ["説明会"],
        "page_token": ["page-3"],
    }


def test_returning_from_detail_keeps_cache_key_and_does_not_refresh(
    client,
    app,
    monkeypatch,
):
    service = _prepare(client, app, monkeypatch)
    history = encode_page_history([""])
    list_response = client.get(
        "/emails",
        query_string={
            "q": "面接",
            "page_token": "page-2",
            "history": history,
            "refresh": "1",
        },
    )
    detail_url = _detail_url(list_response)
    return_to = _return_to_from_detail_url(detail_url)

    assert "refresh=" not in return_to
    detail_response = client.get(detail_url)
    returned = client.get(_back_href(detail_response))

    assert returned.status_code == 200
    assert service.list_calls == [("面接", "page-2")]
    assert service.detail_calls == ["message-1"]


def test_detail_without_return_to_falls_back_to_email_list(
    client,
    app,
    monkeypatch,
):
    _prepare(client, app, monkeypatch)

    response = client.get("/emails/message-1")

    assert response.status_code == 200
    assert _back_href(response) == EMAIL_LIST_PATH


@pytest.mark.parametrize(
    "unsafe_return_to",
    [
        "https://example.com/emails",
        "//example.com/emails",
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "/applications",
        "/emails/message-1",
        "/emails/?refresh=1",
        "/emails/?q=one&q=two",
    ],
)
def test_unsafe_or_non_list_return_url_falls_back_to_email_list(
    client,
    app,
    monkeypatch,
    unsafe_return_to,
):
    _prepare(client, app, monkeypatch)

    response = client.get(
        "/emails/message-1",
        query_string={"return_to": unsafe_return_to},
    )

    assert response.status_code == 200
    assert _back_href(response) == EMAIL_LIST_PATH
    assert "example.com" not in _back_href(response)


def test_safe_return_url_canonicalizes_only_allowed_parameters():
    history = encode_page_history(["", "page-1"])
    safe = safe_email_list_return_url(
        f"/emails?q=面接&page_token=page-2&history={history}"
    )

    assert parse_qs(urlsplit(safe).query) == {
        "q": ["面接"],
        "page_token": ["page-2"],
        "history": [history],
    }


def test_return_to_contains_no_message_identifier(client, app, monkeypatch):
    _prepare(client, app, monkeypatch)

    response = client.get(
        "/emails",
        query_string={"q": "面接", "page_token": "page-2"},
    )
    return_to = _return_to_from_detail_url(_detail_url(response))

    assert "message-1" not in return_to


def test_deleted_message_404_keeps_safe_return_link(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        save_gmail_credential()
    not_found = RuntimeError("private Gmail response")
    not_found.status_code = 404

    class MissingMessageService:
        def get_message(self, message_id):
            raise GmailServiceError("gmail_message_get", not_found)

    monkeypatch.setattr(
        email_routes,
        "get_gmail_service",
        MissingMessageService,
    )
    return_to = "/emails/?q=%E9%9D%A2%E6%8E%A5&page_token=page-2"

    response = client.get(
        "/emails/message-1",
        query_string={"return_to": return_to},
    )

    assert response.status_code == 200
    assert "見つかりませんでした" in response.get_data(as_text=True)
    assert _back_href(response) == safe_email_list_return_url(return_to)
