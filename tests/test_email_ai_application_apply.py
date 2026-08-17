import re
from dataclasses import replace
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import create_app
from app.emails.analysis_application import ai_datetime_to_jst_naive
from app.emails.analysis_apply_store import EmailAnalysisApplyStore
from app.extensions import db
from app.models import Application, CalendarSync, ChecklistItem
from app.services.email_ai_service import validate_analysis_payload
from config import TestConfig


MESSAGE_ID = "message-1"
RETURN_TO = "/emails/?q=面接&page_token=page-2"


def analysis_payload(**overrides):
    payload = {
        "company_name": "株式会社キャリアパイロット",
        "mail_category": "interview",
        "es_deadline": "2026-08-15T18:00:00+09:00",
        "web_test_deadline": "2026-08-16T19:00:00+09:00",
        "interview_datetime": "2026-08-20T13:00:00+09:00",
        "event_datetime": "2026-08-18T10:00:00+09:00",
        "es_deadline_text": None,
        "web_test_deadline_text": None,
        "interview_datetime_text": None,
        "event_datetime_text": None,
        "action_items": ["ESを提出する", "面接を予約する"],
        "important_notes": ["オンライン面接"],
        "summary": "選考日程の案内です。期限を確認してください。",
        "confidence": "medium",
        "evidence": {
            "company_name": "株式会社キャリアパイロット",
            "es_deadline": "8月15日18時まで",
            "web_test_deadline": "8月16日19時まで",
            "interview_datetime": "8月20日13時",
            "event_datetime": "8月18日10時",
        },
    }
    payload.update(overrides)
    return payload


def analysis_result(**overrides):
    return validate_analysis_payload(analysis_payload(**overrides))


def issue_token(app, result=None, return_to=RETURN_TO, store=None):
    with app.app_context():
        target_store = store or app.extensions["email_analysis_apply_store"]
        return target_store.save(
            MESSAGE_ID,
            result or analysis_result(),
            return_to,
        )


def application_post_data(token, **overrides):
    data = {
        "token": token,
        "return_to": RETURN_TO,
        "apply_mode": "new",
        "application_id": "-1",
        "company_name": "株式会社キャリアパイロット",
        "position_name": "総合職",
        "status": "面接",
        "priority": "4",
        "es_deadline": "2026-08-15T18:00",
        "web_test_deadline": "2026-08-16T19:00",
        "interview_at": "2026-08-20T13:00",
        "memo": "ユーザーが確認したメモ",
        "create_default_checklist": "y",
    }
    data.update(overrides)
    return data


def create_existing_application(**overrides):
    values = {
        "company_name": "既存株式会社",
        "position_name": "既存職種",
        "status": "ES作成中",
        "priority": 2,
        "es_deadline": datetime(2026, 8, 30, 12, 0),
        "web_test_deadline": datetime(2026, 8, 31, 12, 0),
        "interview_at": datetime(2026, 9, 1, 12, 0),
        "application_url": "https://example.test/apply",
        "application_source": "既存経路",
        "interview_format": "オンライン",
        "memo": "既存メモ",
    }
    values.update(overrides)
    application = Application(**values)
    db.session.add(application)
    db.session.commit()
    return application


def test_ai_candidate_creates_application_with_all_editable_fields_and_checklist(
    client,
    app,
):
    token = issue_token(app)

    preview = client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={"token": token, "return_to": RETURN_TO},
    )
    html = preview.get_data(as_text=True)
    assert preview.status_code == 200
    assert 'value="2026-08-15T18:00"' in html
    assert 'value="2026-08-16T19:00"' in html
    assert 'value="2026-08-20T13:00"' in html

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(token),
    )

    assert response.status_code == 302
    with app.app_context():
        application = db.session.scalar(db.select(Application))
        assert application.company_name == "株式会社キャリアパイロット"
        assert application.position_name == "総合職"
        assert application.status == "面接"
        assert application.priority == 4
        assert application.es_deadline == datetime(2026, 8, 15, 18, 0)
        assert application.web_test_deadline == datetime(2026, 8, 16, 19, 0)
        assert application.interview_at == datetime(2026, 8, 20, 13, 0)
        assert application.memo == "ユーザーが確認したメモ"
        assert len(application.checklist_items) == 7
        assert all(
            item.title != "面接を予約する"
            for item in application.checklist_items
        )
        assert db.session.scalar(db.select(db.func.count(CalendarSync.id))) == 0


def test_new_application_can_skip_default_checklist(client, app):
    token = issue_token(app)
    data = application_post_data(token)
    data.pop("create_default_checklist")

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=data,
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 1
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_duplicate_company_warns_but_allows_new_application(client, app):
    with app.app_context():
        create_existing_application(company_name="株式会社キャリアパイロット")
    token = issue_token(app)
    data = application_post_data(token)
    data.pop("create_default_checklist")

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=data,
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "同じ会社名の応募先が既に登録されています" in html
    assert "AI解析結果を確認して応募先を登録しました" in html
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 2


def test_existing_application_shows_current_and_ai_values_then_saves_user_values(
    client,
    app,
):
    with app.app_context():
        target = create_existing_application()
        target_id = target.id
        other = create_existing_application(
            company_name="変更しない株式会社",
            status="内定",
            memo="変更しない",
        )
        other_id = other.id
    token = issue_token(app)

    preview = client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={
            "token": token,
            "return_to": RETURN_TO,
            "application_id": target_id,
        },
    )
    html = preview.get_data(as_text=True)
    assert preview.status_code == 200
    assert "現在の応募先情報" in html
    assert "既存株式会社" in html
    assert "2026/08/30 12:00" in html
    assert "AI候補：株式会社キャリアパイロット" in html
    assert "AI候補：2026/08/15 18:00" in html

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(
            token,
            apply_mode="existing",
            application_id=str(target_id),
            company_name="確認後株式会社",
            position_name="確認後職種",
            status="最終面接",
            priority="5",
            es_deadline="2026-08-25T10:30",
            web_test_deadline="",
            interview_at="2026-08-28T15:45",
            memo="ユーザーが決めた最終値",
        ),
    )

    assert response.status_code == 302
    with app.app_context():
        target = db.session.get(Application, target_id)
        other = db.session.get(Application, other_id)
        assert target.company_name == "確認後株式会社"
        assert target.position_name == "確認後職種"
        assert target.status == "最終面接"
        assert target.priority == 5
        assert target.es_deadline == datetime(2026, 8, 25, 10, 30)
        assert target.web_test_deadline is None
        assert target.interview_at == datetime(2026, 8, 28, 15, 45)
        assert target.memo == "ユーザーが決めた最終値"
        assert target.application_url == "https://example.test/apply"
        assert target.application_source == "既存経路"
        assert target.interview_format == "オンライン"
        assert other.company_name == "変更しない株式会社"
        assert other.status == "内定"
        assert other.memo == "変更しない"
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_opening_existing_preview_does_not_overwrite_application(client, app):
    with app.app_context():
        application = create_existing_application()
        application_id = application.id
    token = issue_token(app)

    response = client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={"token": token, "application_id": application_id},
    )

    assert response.status_code == 200
    with app.app_context():
        unchanged = db.session.get(Application, application_id)
        assert unchanged.company_name == "既存株式会社"
        assert unchanged.es_deadline == datetime(2026, 8, 30, 12, 0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-15T23:59:00+09:00", datetime(2026, 8, 15, 23, 59)),
        ("2026-08-15T14:59:00+00:00", datetime(2026, 8, 15, 23, 59)),
        (None, None),
        ("", None),
        ("not-an-iso-datetime", None),
        ("2026-08-15T23:59:00", None),
    ],
)
def test_ai_datetime_conversion_is_strict_and_uses_jst(value, expected):
    assert ai_datetime_to_jst_naive(value) == expected


def test_datetime_text_only_is_reference_and_does_not_fill_datetime(client, app):
    result = analysis_result(
        es_deadline=None,
        es_deadline_text="8月15日まで",
    )
    token = issue_token(app, result=result)

    response = client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={"token": token},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "AIが検出した原文：8月15日まで" in html
    es_input = re.search(r'<input[^>]+id="es_deadline"[^>]*>', html).group(0)
    assert "23:59" not in es_input
    assert 'value=""' not in es_input or "T23:59" not in es_input


def test_invalid_ai_datetime_never_prefills_datetime_local(client, app):
    result = replace(analysis_result(), es_deadline="invalid-private-value")
    token = issue_token(app, result=result)

    response = client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={"token": token},
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "invalid-private-value" not in html


def test_existing_application_id_tampering_is_rejected(client, app):
    with app.app_context():
        first = create_existing_application(company_name="第一株式会社")
        first_id = first.id
        second = create_existing_application(company_name="第二株式会社")
        second_id = second.id
    token = issue_token(app)
    client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={"token": token, "application_id": first_id},
    )

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(
            token,
            apply_mode="existing",
            application_id=str(second_id),
            company_name="改ざん後",
        ),
    )

    assert response.status_code == 200
    assert "反映先の応募先を確認できませんでした" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert db.session.get(Application, first_id).company_name == "第一株式会社"
        assert db.session.get(Application, second_id).company_name == "第二株式会社"


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"status": "不正ステータス"}, "Not a valid choice"),
        ({"company_name": "A" * 101}, "Field cannot be longer than 100"),
        ({"company_name": "   "}, "会社名を入力してください"),
        ({"priority": "9"}, "志望度は1〜5"),
        ({"es_deadline": "invalid"}, "Not a valid datetime value"),
    ],
)
def test_all_client_values_are_revalidated(
    client,
    app,
    overrides,
    expected_error,
):
    token = issue_token(app)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(token, **overrides),
    )

    assert response.status_code == 200
    assert expected_error in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 0


def test_tampered_token_is_rejected_without_database_change(client, app):
    issue_token(app)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data("tampered-token"),
        follow_redirects=False,
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 0


def test_apply_token_is_single_use_after_success(client, app):
    token = issue_token(app)
    first = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(token),
    )
    second = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(token),
    )

    assert first.status_code == second.status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 1


def test_apply_store_expires_without_waiting_and_has_size_limit():
    clock = [100.0]
    store = EmailAnalysisApplyStore(
        ttl_seconds=600,
        max_payload_bytes=32_768,
        clock=lambda: clock[0],
        token_factory=lambda: "fixed-token",
    )
    token = store.save(MESSAGE_ID, analysis_result(), RETURN_TO)
    clock[0] = 699.9
    assert store.get(token, MESSAGE_ID) is not None
    clock[0] = 700.0
    assert store.get(token, MESSAGE_ID) is None

    small_store = EmailAnalysisApplyStore(max_payload_bytes=128)
    with pytest.raises(ValueError):
        small_store.save(MESSAGE_ID, analysis_result(), RETURN_TO)


def test_ai_context_uses_no_cookie_session_or_persistent_database(client, app):
    confidential_body = "メール本文をsessionへ保存しない"
    token = issue_token(app)

    client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={"token": token},
    )

    with client.session_transaction() as session_data:
        assert confidential_body not in repr(dict(session_data))
        assert token not in repr(dict(session_data))
    with app.app_context():
        entry = app.extensions["email_analysis_apply_store"].get(
            token,
            MESSAGE_ID,
        )
        assert not hasattr(entry.result, "body_text")
        assert confidential_body not in repr(entry.result)
        table_names = set(db.inspect(db.engine).get_table_names())
        assert "email_analysis_results" not in table_names
        assert "gmail_messages" not in table_names


def test_reference_ui_contains_action_event_evidence_and_cancel_link(client, app):
    token = issue_token(app)

    response = client.get(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        query_string={"token": token, "return_to": RETURN_TO},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "新しい応募先として登録" in html
    assert "既存の応募先へ反映" in html
    assert "AI参考情報" in html
    assert "ESを提出する" in html
    assert "2026/08/18 10:00" in html
    assert "8月20日13時" in html
    assert "キャンセル" in html
    assert "メール詳細へ戻る" in html
    assert "ai-application-apply" in html
    back_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>← メール詳細へ戻る</a>', html)
    assert back_match is not None
    back_url = urlsplit(unescape(back_match.group(1)))
    assert back_url.path == f"/emails/{MESSAGE_ID}"
    assert parse_qs(back_url.query)["return_to"] == [
        "/emails/?q=面接&page_token=page-2"
    ]


def test_apply_confirmation_mobile_css_is_scoped():
    css = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "css"
        / "style.css"
    ).read_text(encoding="utf-8")

    assert ".ai-application-apply .ai-candidate-hint" in css
    assert ".ai-application-apply .card-body { padding: 1rem !important; }" in css
    assert ".ai-application-apply form > .d-flex .btn { width: 100%; }" in css


def test_expired_apply_token_redirects_without_application_change(client, app):
    clock = [10.0]
    store = EmailAnalysisApplyStore(ttl_seconds=10, clock=lambda: clock[0])
    app.extensions["email_analysis_apply_store"] = store
    token = issue_token(app, store=store)
    clock[0] = 20.0

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(token),
    )

    assert response.status_code == 302
    assert urlsplit(response.location).path == f"/emails/{MESSAGE_ID}"
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 0


def test_database_commit_failure_rolls_back_and_keeps_retry_form(
    client,
    app,
    monkeypatch,
):
    token = issue_token(app)

    def fail_commit():
        raise SQLAlchemyError("private database detail")

    monkeypatch.setattr(db.session, "commit", fail_commit)
    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/apply",
        data=application_post_data(token),
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "応募先へ反映できませんでした" in html
    assert "private database detail" not in html
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 0


def test_apply_form_requires_csrf_when_enabled():
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "email-application-apply-csrf"

    csrf_app = create_app(CsrfConfig)
    with csrf_app.app_context():
        db.create_all()
        token = issue_token(csrf_app)
        client = csrf_app.test_client()
        get_response = client.get(
            f"/emails/{MESSAGE_ID}/analysis/apply",
            query_string={"token": token},
        )
        csrf_match = re.search(
            r'name="csrf_token"[^>]+value="([^"]+)"',
            get_response.get_data(as_text=True),
        )
        assert csrf_match is not None

        rejected = client.post(
            f"/emails/{MESSAGE_ID}/analysis/apply",
            data=application_post_data(token),
        )
        assert rejected.status_code == 400
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 0

        accepted_data = application_post_data(token)
        accepted_data["csrf_token"] = csrf_match.group(1)
        accepted = client.post(
            f"/emails/{MESSAGE_ID}/analysis/apply",
            data=accepted_data,
        )
        assert accepted.status_code == 302
        assert db.session.scalar(db.select(db.func.count(Application.id))) == 1
        db.session.remove()
        db.drop_all()
