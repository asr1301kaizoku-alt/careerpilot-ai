import base64
import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.emails import routes as email_routes
from app.emails.cache import GmailListCache, make_gmail_list_cache_key
from app.extensions import db
from app.integrations.credential_store import GoogleCredentialStore
from app.integrations.gmail_service import (
    BODY_DECODE_ERROR_MESSAGE,
    GmailMessagePage,
    GmailMessageSummary,
    GmailServiceError,
    extract_body_text,
)


JAPANESE_BODY = "採用担当から面接日程のご案内です。"


def encode_bytes(value, charset):
    return base64.urlsafe_b64encode(value.encode(charset)).decode("ascii").rstrip(
        "="
    )


def text_payload(value, charset, declared_charset=None, mime_type="text/plain"):
    payload = {
        "mimeType": mime_type,
        "body": {"data": encode_bytes(value, charset)},
    }
    if declared_charset:
        payload["headers"] = [
            {
                "name": "Content-Type",
                "value": f"{mime_type}; charset={declared_charset}",
            }
        ]
    return payload


@pytest.mark.parametrize(
    ("declared_charset", "codec"),
    [
        ("UTF-8", "utf-8"),
        ("utf8", "utf-8"),
        ("ISO-2022-JP", "iso2022_jp"),
        ("iso2022_jp", "iso2022_jp"),
        ("Shift_JIS", "shift_jis"),
        ("Shift-JIS", "shift_jis"),
        ("SJIS", "shift_jis"),
        ("Windows-31J", "cp932"),
        ("MS932", "cp932"),
        ("CP932", "cp932"),
        ("EUC-JP", "euc_jp"),
    ],
)
def test_japanese_body_respects_mime_charset_aliases(
    declared_charset,
    codec,
):
    payload = text_payload(JAPANESE_BODY, codec, declared_charset)

    assert extract_body_text(payload) == JAPANESE_BODY


def test_cp932_specific_characters_are_decoded_from_windows_31j():
    body = "㈱キャリアから選考結果のお知らせ"

    assert extract_body_text(text_payload(body, "cp932", "Windows-31J")) == body


def test_charset_unspecified_utf8_uses_utf8_fallback():
    assert extract_body_text(text_payload(JAPANESE_BODY, "utf-8")) == JAPANESE_BODY


def test_charset_unspecified_iso2022jp_uses_escape_aware_fallback():
    payload = text_payload(JAPANESE_BODY, "iso2022_jp")

    body = extract_body_text(payload)

    assert body == JAPANESE_BODY
    assert "\ufffd" not in body


def test_invalid_declared_charset_falls_back_without_500():
    payload = text_payload(JAPANESE_BODY, "cp932", "not-a-real-charset")

    assert extract_body_text(payload) == JAPANESE_BODY


def test_charset_can_be_read_from_part_mime_metadata():
    payload = {
        "mimeType": "text/plain; charset=Shift-JIS",
        "body": {"data": encode_bytes(JAPANESE_BODY, "shift_jis")},
    }

    assert extract_body_text(payload) == JAPANESE_BODY


def test_undecodable_body_uses_safe_fallback_without_exception():
    encoded = base64.urlsafe_b64encode(b"\x81").decode("ascii").rstrip("=")
    payload = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Content-Type", "value": "text/plain; charset=utf-8"}
        ],
        "body": {"data": encoded},
    }

    body = extract_body_text(payload)

    assert body in {
        BODY_DECODE_ERROR_MESSAGE,
        "本文を取得できませんでした。",
    }


def test_nested_multipart_uses_each_plain_part_charset():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    text_payload(JAPANESE_BODY, "euc_jp", "EUC-JP"),
                    text_payload("<p>HTML本文</p>", "utf-8", None, "text/html"),
                ],
            }
        ],
    }

    assert extract_body_text(payload) == JAPANESE_BODY


def test_html_meta_charset_is_used_only_when_mime_charset_is_missing():
    html_body = (
        '<html><head><meta charset="Windows-31J"></head>'
        "<body><p>面接のご案内です。</p></body></html>"
    )
    payload = text_payload(html_body, "cp932", None, "text/html")

    body = extract_body_text(payload)

    assert body == "面接のご案内です。"
    assert "<p>" not in body


def test_html_http_equiv_charset_is_used_when_mime_charset_is_missing():
    html_body = (
        '<html><head><meta http-equiv="Content-Type" '
        'content="text/html; charset=EUC-JP"></head>'
        "<body><p>会社説明会のお知らせです。</p></body></html>"
    )
    payload = text_payload(html_body, "euc_jp", None, "text/html")

    assert extract_body_text(payload) == "会社説明会のお知らせです。"


def test_mime_charset_has_priority_over_conflicting_html_meta():
    html_body = (
        '<html><head><meta charset="UTF-8"></head>'
        "<body><p>説明会のお知らせです。</p></body></html>"
    )
    payload = text_payload(html_body, "iso2022_jp", "ISO-2022-JP", "text/html")

    assert extract_body_text(payload) == "説明会のお知らせです。"


def test_content_transfer_encoding_is_not_decoded_twice():
    payload = text_payload(JAPANESE_BODY, "utf-8", "UTF-8")
    payload["headers"].append(
        {"name": "Content-Transfer-Encoding", "value": "quoted-printable"}
    )

    assert extract_body_text(payload) == JAPANESE_BODY


def test_plain_text_still_wins_over_html_and_attachments_are_ignored():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            text_payload("<p>HTML本文</p>", "utf-8", None, "text/html"),
            text_payload(JAPANESE_BODY, "shift_jis", "Shift_JIS"),
            {
                **text_payload("秘密の添付本文", "utf-8", "UTF-8"),
                "filename": "secret.txt",
                "body": {
                    "attachmentId": "attachment-id",
                    "data": encode_bytes("秘密の添付本文", "utf-8"),
                },
            },
        ],
    }

    assert extract_body_text(payload) == JAPANESE_BODY


def make_credentials():
    return SimpleNamespace(
        token="gmail-access-secret",
        refresh_token="gmail-refresh-secret",
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


def summary(message_id="message-1", subject="面接日程のご案内"):
    return GmailMessageSummary(
        message_id=message_id,
        subject=subject,
        sender="採用担当 <recruit@example.com>",
        received_at=datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc),
        date_header="Sat, 8 Aug 2026 10:30:00 +0000",
        snippet="面接日程をご確認ください。",
    )


class CountingListService:
    def __init__(self):
        self.calls = []

    def list_messages(self, query, page_token):
        self.calls.append((query, page_token))
        number = len(self.calls)
        return GmailMessagePage(
            (summary(f"message-{number}", f"取得結果{number}"),),
            "next-page-token",
        )


def test_first_list_fetches_api_and_second_same_request_uses_cache(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        save_gmail_credential()
    service = CountingListService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)

    first = client.get("/emails")
    second = client.get("/emails")

    assert first.status_code == second.status_code == 200
    assert service.calls == [("", None)]
    assert "取得結果1" in second.get_data(as_text=True)
    assert "最終取得：" in second.get_data(as_text=True)
    assert "最新のメールを取得" in second.get_data(as_text=True)


def test_query_and_page_token_have_separate_cache_entries(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        save_gmail_credential()
    service = CountingListService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)

    client.get("/emails", query_string={"q": "面接"})
    client.get("/emails", query_string={"q": "説明会"})
    client.get("/emails", query_string={"q": "面接", "page_token": "page-2"})
    client.get("/emails", query_string={"q": "面接"})

    assert service.calls == [
        ("面接", None),
        ("説明会", None),
        ("面接", "page-2"),
    ]


def test_different_gmail_account_does_not_share_list_cache(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        credential = save_gmail_credential("first@example.com")
        credential_id = credential.id
        credential_type = type(credential)
    service = CountingListService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)

    client.get("/emails")
    with app.app_context():
        credential = db.session.get(credential_type, credential_id)
        credential.google_account_email = "second@example.com"
        db.session.commit()
    client.get("/emails")

    assert service.calls == [("", None), ("", None)]


def test_cache_ttl_expiry_refetches_from_api(client, app, monkeypatch):
    clock = [100.0]
    app.extensions["gmail_list_cache"] = GmailListCache(
        ttl_seconds=60,
        max_entries=10,
        clock=lambda: clock[0],
    )
    with app.app_context():
        save_gmail_credential()
    service = CountingListService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)

    client.get("/emails")
    clock[0] = 159.9
    client.get("/emails")
    clock[0] = 160.0
    client.get("/emails")

    assert service.calls == [("", None), ("", None)]


def test_default_gmail_list_cache_ttl_is_sixty_seconds(app):
    assert app.config["GMAIL_LIST_CACHE_TTL_SECONDS"] == 60
    assert app.extensions["gmail_list_cache"].ttl_seconds == 60
    assert GmailListCache().ttl_seconds == 60


def test_refresh_forces_fetch_and_replaces_cached_result(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        save_gmail_credential()
    service = CountingListService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)

    first = client.get("/emails")
    refreshed = client.get("/emails", query_string={"refresh": "1"})
    cached = client.get("/emails")

    assert "取得結果1" in first.get_data(as_text=True)
    assert "取得結果2" in refreshed.get_data(as_text=True)
    assert "取得結果2" in cached.get_data(as_text=True)
    assert service.calls == [("", None), ("", None)]


def test_refresh_api_error_is_safe_and_existing_cache_remains_available(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        save_gmail_credential()
    service = CountingListService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)
    client.get("/emails")

    class FailingService:
        def list_messages(self, query, page_token):
            error = RuntimeError("private response body")
            error.status_code = 503
            raise GmailServiceError("gmail_messages_list", error)

    monkeypatch.setattr(email_routes, "get_gmail_service", FailingService)

    failed = client.get("/emails", query_string={"refresh": "1"})
    normal = client.get("/emails")

    assert failed.status_code == normal.status_code == 200
    assert "一時的な問題" in failed.get_data(as_text=True)
    assert "取得結果1" in normal.get_data(as_text=True)


def test_cache_evicts_oldest_entry_when_maximum_is_exceeded():
    cache = GmailListCache(ttl_seconds=30, max_entries=2, clock=lambda: 10.0)
    page = GmailMessagePage((), None)

    cache.set("key-1", page)
    cache.set("key-2", page)
    cache.set("key-3", page)

    assert len(cache) == 2
    assert cache.get("key-1") is None
    assert cache.get("key-2") is not None
    assert cache.get("key-3") is not None


def test_cache_basic_operations_are_thread_safe():
    cache = GmailListCache(ttl_seconds=30, max_entries=128)
    page = GmailMessagePage((), None)
    errors = []

    def worker(worker_id):
        try:
            for index in range(100):
                key = f"key-{worker_id}-{index % 8}"
                cache.set(key, page)
                assert cache.get(key) is not None
        except Exception as error:  # pragma: no cover - assertion carrier
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(cache) <= 128


def test_cache_key_hides_account_query_and_page_token_and_stores_no_oauth_token():
    key = make_gmail_list_cache_key(
        "owner-secret",
        "jobs@example.com",
        "from:private@example.com",
        "gmail-page-token-secret",
        credential_id=42,
    )
    cache = GmailListCache()
    cache.set(key, GmailMessagePage((summary(),), None))
    cache_representation = repr(cache._entries)

    assert "owner-secret" not in key
    assert "jobs@example.com" not in key
    assert "from:private@example.com" not in key
    assert "gmail-page-token-secret" not in key
    assert "gmail-access-secret" not in cache_representation
    assert "gmail-refresh-secret" not in cache_representation


def test_cache_logs_do_not_contain_mail_content_or_identifiers(
    client,
    app,
    monkeypatch,
    caplog,
):
    with app.app_context():
        save_gmail_credential()
    service = CountingListService()
    monkeypatch.setattr(email_routes, "get_gmail_service", lambda: service)

    with caplog.at_level(logging.INFO):
        client.get("/emails")
        client.get("/emails")

    assert "cache_hit=False" in caplog.text
    assert "cache_hit=True" in caplog.text
    assert "取得結果1" not in caplog.text
    assert "recruit@example.com" not in caplog.text
    assert "面接日程をご確認ください" not in caplog.text
    assert "message-1" not in caplog.text
