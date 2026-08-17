import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import create_app
from app.checklists.services import build_default_checklist
from app.emails.analysis_apply_store import EmailAnalysisApplyStore
from app.emails.analysis_checklist import checklist_due_candidate
from app.extensions import db
from app.models import Application, CalendarSync, ChecklistItem
from app.services.email_ai_service import validate_analysis_payload
from config import TestConfig


MESSAGE_ID = "message-checklist"
RETURN_TO = "/emails/?q=選考&page_token=page-3"


def analysis_payload(**overrides):
    payload = {
        "company_name": "株式会社チェックリスト",
        "mail_category": "es",
        "es_deadline": "2026-08-20T23:59:00+09:00",
        "web_test_deadline": "2026-08-22T18:00:00+09:00",
        "interview_datetime": "2026-08-25T13:00:00+09:00",
        "event_datetime": None,
        "es_deadline_text": None,
        "web_test_deadline_text": None,
        "interview_datetime_text": None,
        "event_datetime_text": None,
        "action_items": [
            "ESを提出する",
            "Webテストを受験する",
            "面接日程を予約する",
        ],
        "important_notes": ["提出後の修正はできません"],
        "summary": "ESとWebテストに対応する必要があります。",
        "confidence": "high",
        "evidence": {
            "company_name": "株式会社チェックリスト",
            "es_deadline": "8月20日23時59分まで",
            "web_test_deadline": "8月22日18時まで",
            "interview_datetime": "8月25日13時",
            "event_datetime": None,
        },
    }
    payload.update(overrides)
    return payload


def analysis_result(**overrides):
    return validate_analysis_payload(analysis_payload(**overrides))


def create_application(company_name="登録先株式会社", checklist_items=()):
    application = Application(
        company_name=company_name,
        position_name="総合職",
        status="応募済み",
        priority=3,
    )
    application.checklist_items.extend(checklist_items)
    db.session.add(application)
    db.session.commit()
    return application


def issue_token(app, result=None, store=None):
    with app.app_context():
        target_store = store or app.extensions[
            "email_analysis_checklist_store"
        ]
        return target_store.save(
            MESSAGE_ID,
            result or analysis_result(),
            RETURN_TO,
        )


def select_application(client, token, application_id):
    return client.get(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        query_string={
            "token": token,
            "return_to": RETURN_TO,
            "application_id": application_id,
        },
    )


def checklist_post_data(token, application_id, candidates):
    data = {
        "token": token,
        "return_to": RETURN_TO,
        "application_id": str(application_id),
    }
    for index, candidate in enumerate(candidates):
        if candidate.get("selected", True):
            data[f"candidates-{index}-selected"] = "y"
        data[f"candidates-{index}-title"] = candidate.get("title", "")
        data[f"candidates-{index}-due_at"] = candidate.get("due_at", "")
    return data


def default_candidates():
    return [
        {
            "title": "ESを提出する",
            "due_at": "2026-08-20T23:59",
        },
        {
            "title": "Webテストを受験する",
            "due_at": "2026-08-22T18:00",
        },
        {"title": "面接日程を予約する", "due_at": ""},
    ]


def test_adds_one_action_item_to_selected_application(client, app):
    result = analysis_result(
        action_items=["ESを提出する"],
        web_test_deadline=None,
    )
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app, result=result)
    select_application(client, token, application_id)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(
            token,
            application_id,
            [{"title": "ESを提出する", "due_at": "2026-08-20T23:59"}],
        ),
    )

    assert response.status_code == 302
    assert response.location.endswith(f"/applications/{application_id}#checklist")
    with app.app_context():
        item = db.session.scalar(db.select(ChecklistItem))
        assert item.application_id == application_id
        assert item.title == "ESを提出する"
        assert item.due_at == datetime(2026, 8, 20, 23, 59)
        assert item.is_completed is False
        assert item.completed_at is None
        assert db.session.scalar(db.select(db.func.count(CalendarSync.id))) == 0


def test_multiple_items_follow_existing_sort_order_and_do_not_touch_other_app(
    client,
    app,
):
    with app.app_context():
        target = create_application(
            checklist_items=[
                ChecklistItem(title="既存1", sort_order=2),
                ChecklistItem(title="既存2", sort_order=5),
            ]
        )
        target_id = target.id
        other_id = create_application(company_name="別会社").id
    token = issue_token(app)
    select_application(client, token, target_id)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, target_id, default_candidates()),
    )

    assert response.status_code == 302
    with app.app_context():
        target_items = db.session.scalars(
            db.select(ChecklistItem)
            .where(ChecklistItem.application_id == target_id)
            .order_by(ChecklistItem.sort_order)
        ).all()
        assert [item.sort_order for item in target_items] == [2, 5, 6, 7, 8]
        assert [item.title for item in target_items[-3:]] == [
            "ESを提出する",
            "Webテストを受験する",
            "面接日程を予約する",
        ]
        assert db.session.scalar(
            db.select(db.func.count(ChecklistItem.id)).where(
                ChecklistItem.application_id == other_id
            )
        ) == 0


def test_user_can_edit_titles_deadlines_and_unselect_candidates(client, app):
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app)
    select_application(client, token, application_id)
    candidates = default_candidates()
    candidates[0] = {
        "title": "修正したES提出",
        "due_at": "2026-08-21T10:30",
    }
    candidates[1]["selected"] = False

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, candidates),
    )

    assert response.status_code == 302
    with app.app_context():
        items = db.session.scalars(
            db.select(ChecklistItem).order_by(ChecklistItem.sort_order)
        ).all()
        assert [item.title for item in items] == [
            "修正したES提出",
            "面接日程を予約する",
        ]
        assert items[0].due_at == datetime(2026, 8, 21, 10, 30)


@pytest.mark.parametrize(
    ("title", "field", "value", "expected"),
    [
        (
            "ESを提出する",
            "es_deadline",
            "2026-08-20T23:59:00+09:00",
            datetime(2026, 8, 20, 23, 59),
        ),
        (
            "Webテストを受験する",
            "web_test_deadline",
            "2026-08-22T09:00:00+00:00",
            datetime(2026, 8, 22, 18, 0),
        ),
        (
            "面接日程を予約する",
            "interview_datetime",
            "2026-08-25T13:00:00+09:00",
            None,
        ),
        ("ESを提出する", "es_deadline", None, None),
        ("ES提出について確認する", "es_deadline", "2026-08-20T23:59:00+09:00", None),
    ],
)
def test_safe_due_candidate_rules(title, field, value, expected):
    result = replace(analysis_result(), **{field: value})
    assert checklist_due_candidate(title, result) == expected


def test_text_only_deadline_is_not_prefilled(client, app):
    text_only = analysis_result(
        action_items=["ESを提出する"],
        es_deadline=None,
        es_deadline_text="8月20日まで",
    )
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app, result=text_only)

    response = select_application(client, token, application_id)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    due_input = re.search(r'<input[^>]+id="candidates-0-due_at"[^>]*>', html)
    assert due_input is not None
    assert "23:59" not in due_input.group(0)


def test_invalid_iso_deadline_is_not_prefilled(client, app):
    invalid = replace(
        analysis_result(action_items=["ESを提出する"]),
        es_deadline="invalid-private-value",
    )
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app, result=invalid)

    response = select_application(client, token, application_id)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    due_input = re.search(r'<input[^>]+id="candidates-0-due_at"[^>]*>', html)
    assert due_input is not None
    assert "23:59" not in due_input.group(0)
    assert "invalid-private-value" not in html


def test_application_and_checklist_review_tokens_are_independent(app):
    with app.app_context():
        application_store = app.extensions["email_analysis_apply_store"]
        checklist_store = app.extensions["email_analysis_checklist_store"]
        result = analysis_result()
        application_token = application_store.save(
            MESSAGE_ID, result, RETURN_TO
        )
        checklist_token = checklist_store.save(
            MESSAGE_ID, result, RETURN_TO
        )

        assert application_token != checklist_token
        assert application_store.consume(
            application_token, MESSAGE_ID
        ) is not None
        assert application_store.get(application_token, MESSAGE_ID) is None
        assert checklist_store.get(checklist_token, MESSAGE_ID) is not None


def test_all_candidates_unselected_is_rejected_without_saving(client, app):
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app)
    select_application(client, token, application_id)
    candidates = default_candidates()
    for candidate in candidates:
        candidate["selected"] = False

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, candidates),
    )

    assert response.status_code == 200
    assert "追加する項目を選択してください" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_existing_incomplete_duplicate_warns_but_can_be_added(client, app):
    result = analysis_result(action_items=["ESを提出する"])
    with app.app_context():
        application_id = create_application(
            checklist_items=[
                ChecklistItem(
                    title="ESを提出する",
                    sort_order=0,
                    is_completed=False,
                )
            ]
        ).id
    token = issue_token(app, result=result)

    preview = select_application(client, token, application_id)
    assert "既存タスクあり" in preview.get_data(as_text=True)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(
            token,
            application_id,
            [{"title": "ESを提出する", "due_at": "2026-08-20T23:59"}],
        ),
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "既に未完了タスクとして存在します" in html
    assert "チェックリストに1件追加しました" in html
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 2


def test_standard_checklist_duplicate_is_shown(client, app):
    result = analysis_result(action_items=["Webテストを受験する"])
    with app.app_context():
        application_id = create_application(
            checklist_items=build_default_checklist()
        ).id
    token = issue_token(app, result=result)

    response = select_application(client, token, application_id)

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "既存タスクあり" in html
    assert "Webテストを受験する" in html


@pytest.mark.parametrize(
    ("candidate_overrides", "expected_error"),
    [
        ({"title": ""}, "タイトルを入力してください"),
        ({"title": "A" * 151}, "作業名は150文字以内"),
        ({"due_at": "invalid"}, "Not a valid datetime value"),
    ],
)
def test_one_invalid_candidate_prevents_all_saves(
    client,
    app,
    candidate_overrides,
    expected_error,
):
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app)
    select_application(client, token, application_id)
    candidates = default_candidates()
    candidates[1].update(candidate_overrides)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, candidates),
    )

    assert response.status_code == 200
    assert expected_error in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_application_id_tampering_is_rejected(client, app):
    with app.app_context():
        first_id = create_application(company_name="第一会社").id
        second_id = create_application(company_name="第二会社").id
    token = issue_token(app)
    select_application(client, token, first_id)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, second_id, default_candidates()),
    )

    assert response.status_code == 200
    assert "登録先の応募先を確認できませんでした" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_application_selection_is_required(client, app):
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app)

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, default_candidates()),
    )

    assert response.status_code == 200
    assert "登録先の応募先を上の選択欄から選んでください" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_tampered_and_reused_tokens_do_not_create_items(client, app):
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app)
    select_application(client, token, application_id)
    first = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, default_candidates()),
    )
    reused = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, default_candidates()),
    )
    tampered = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(
            "tampered-token",
            application_id,
            default_candidates(),
        ),
    )

    assert first.status_code == reused.status_code == tampered.status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 3


def test_expired_token_does_not_create_items(client, app):
    clock = [100.0]
    store = EmailAnalysisApplyStore(ttl_seconds=10, clock=lambda: clock[0])
    app.extensions["email_analysis_checklist_store"] = store
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app, store=store)
    select_application(client, token, application_id)
    clock[0] = 110.0

    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, default_candidates()),
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_database_commit_failure_rolls_back_every_item(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app)
    select_application(client, token, application_id)

    def fail_commit():
        raise SQLAlchemyError("private database failure")

    monkeypatch.setattr(db.session, "commit", fail_commit)
    response = client.post(
        f"/emails/{MESSAGE_ID}/analysis/checklist",
        data=checklist_post_data(token, application_id, default_candidates()),
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "チェックリストへ追加できませんでした" in html
    assert "private database failure" not in html
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0


def test_confirmation_ui_contains_editable_candidates_and_reference(client, app):
    with app.app_context():
        application_id = create_application().id
    token = issue_token(app)

    response = select_application(client, token, application_id)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "登録先の応募先" in html
    assert "株式会社チェックリスト" in html
    assert "信頼度" in html
    assert "提出後の修正はできません" in html
    assert "8月20日23時59分まで" in html
    assert 'name="candidates-0-selected"' in html
    assert 'name="candidates-0-title"' in html
    assert 'name="candidates-0-due_at"' in html
    assert "キャンセル" in html
    assert "メール詳細へ戻る" in html


def test_checklist_confirmation_mobile_css_is_scoped():
    css = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "css"
        / "style.css"
    ).read_text(encoding="utf-8")

    assert ".ai-checklist-candidate" in css
    assert ".ai-checklist-apply .card-body { padding: 1rem !important; }" in css
    assert ".ai-analysis-actions .btn { width: 100%; }" in css


def test_checklist_apply_form_requires_csrf_when_enabled():
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "email-checklist-apply-csrf"

    csrf_app = create_app(CsrfConfig)
    with csrf_app.app_context():
        db.create_all()
        application_id = create_application().id
        token = issue_token(csrf_app)
        client = csrf_app.test_client()
        get_response = select_application(client, token, application_id)
        csrf_match = re.search(
            r'name="csrf_token"[^>]+value="([^"]+)"',
            get_response.get_data(as_text=True),
        )
        assert csrf_match is not None

        rejected = client.post(
            f"/emails/{MESSAGE_ID}/analysis/checklist",
            data=checklist_post_data(token, application_id, default_candidates()),
        )
        assert rejected.status_code == 400
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 0

        accepted_data = checklist_post_data(token, application_id, default_candidates())
        accepted_data["csrf_token"] = csrf_match.group(1)
        accepted = client.post(
            f"/emails/{MESSAGE_ID}/analysis/checklist",
            data=accepted_data,
        )
        assert accepted.status_code == 302
        assert db.session.scalar(db.select(db.func.count(ChecklistItem.id))) == 3
        db.session.remove()
        db.drop_all()
