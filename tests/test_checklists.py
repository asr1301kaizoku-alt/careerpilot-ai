import re
from datetime import timedelta

import pytest

from app import create_app
from app.checklists.services import DEFAULT_CHECKLIST_TITLES
from app.extensions import db
from app.models import Application, ChecklistItem, now_jst_naive
from config import TestConfig


def application_data(**overrides):
    data = {
        "company_name": "チェックリスト株式会社",
        "status": "応募予定",
        "priority": "3",
    }
    data.update(overrides)
    return data


def create_application(client, with_default=False):
    data = application_data()
    if with_default:
        data["create_default_checklist"] = "y"
    return client.post("/applications/new", data=data)


def add_item(client, application_id=1, **overrides):
    data = {
        "title": "企業研究を深める",
        "due_at": "2026-08-05T18:00",
        "sort_order": "0",
    }
    data.update(overrides)
    return client.post(
        f"/applications/{application_id}/checklist/new",
        data=data,
        follow_redirects=True,
    )


def test_default_checklist_is_created(client, app):
    create_application(client, with_default=True)
    with app.app_context():
        items = ChecklistItem.query.order_by(ChecklistItem.sort_order).all()
        assert [item.title for item in items] == DEFAULT_CHECKLIST_TITLES
        assert all(item.is_completed is False for item in items)


def test_default_checklist_can_be_disabled(client, app):
    create_application(client, with_default=False)
    with app.app_context():
        assert Application.query.count() == 1
        assert ChecklistItem.query.count() == 0


def test_create_form_checks_default_checklist(client):
    response = client.get("/applications/new")
    html = response.get_data(as_text=True)
    assert 'name="create_default_checklist"' in html
    assert "checked" in html


def test_add_checklist_item(client, app):
    create_application(client)
    response = add_item(client)
    assert response.status_code == 200
    assert "チェック項目を追加しました。" in response.get_data(as_text=True)
    with app.app_context():
        item = ChecklistItem.query.one()
        assert item.title == "企業研究を深める"
        assert item.sort_order == 0


@pytest.mark.parametrize("title", ["", "   "])
def test_add_rejects_empty_or_whitespace_title(client, app, title):
    create_application(client)
    response = add_item(client, title=title)
    assert response.status_code == 400
    assert "作業名を入力してください。" in response.get_data(as_text=True)
    with app.app_context():
        assert ChecklistItem.query.count() == 0


def test_add_rejects_invalid_datetime(client, app):
    create_application(client)
    response = add_item(client, due_at="invalid-date")
    assert response.status_code == 400
    with app.app_context():
        assert ChecklistItem.query.count() == 0


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"title": "あ" * 151}, "作業名は150文字以内で入力してください。"),
        ({"sort_order": "-1"}, "表示順は0以上で入力してください。"),
    ],
)
def test_add_rejects_invalid_title_length_and_sort_order(
    client, app, overrides, message
):
    create_application(client)
    response = add_item(client, **overrides)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    with app.app_context():
        assert ChecklistItem.query.count() == 0


def test_edit_checklist_item(client, app):
    create_application(client)
    add_item(client)
    response = client.post(
        "/checklist/1/edit",
        data={
            "title": "更新した作業",
            "due_at": "2026-08-10T10:30",
            "sort_order": "2",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "チェック項目を更新しました。" in response.get_data(as_text=True)
    with app.app_context():
        item = db.session.get(ChecklistItem, 1)
        assert item.title == "更新した作業"
        assert item.sort_order == 2


def test_toggle_sets_and_clears_completed_at(client, app):
    create_application(client)
    add_item(client)

    response = client.post("/checklist/1/toggle", follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        item = db.session.get(ChecklistItem, 1)
        assert item.is_completed is True
        assert item.completed_at is not None

    client.post("/checklist/1/toggle")
    with app.app_context():
        item = db.session.get(ChecklistItem, 1)
        assert item.is_completed is False
        assert item.completed_at is None


def test_delete_checklist_item(client, app):
    create_application(client)
    add_item(client)
    response = client.post("/checklist/1/delete", follow_redirects=True)
    assert response.status_code == 200
    assert "チェック項目を削除しました。" in response.get_data(as_text=True)
    with app.app_context():
        assert ChecklistItem.query.count() == 0


@pytest.mark.parametrize(
    "path",
    ["/checklist/1/toggle", "/checklist/1/delete"],
)
def test_checklist_mutations_do_not_accept_get(client, path):
    create_application(client)
    add_item(client)
    assert client.get(path).status_code == 405


def test_deleting_application_cascades_checklist_items(client, app):
    create_application(client, with_default=True)
    with app.app_context():
        assert ChecklistItem.query.count() == 7
    client.post("/applications/1/delete")
    with app.app_context():
        assert Application.query.count() == 0
        assert ChecklistItem.query.count() == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/applications/999/checklist/new"),
        ("post", "/checklist/999/toggle"),
        ("get", "/checklist/999/edit"),
        ("post", "/checklist/999/delete"),
    ],
)
def test_unknown_checklist_ids_return_404(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 404


def test_dashboard_displays_incomplete_task(client, app):
    now = now_jst_naive()
    with app.app_context():
        application = Application(
            company_name="ダッシュボード株式会社",
            status="応募予定",
            priority=3,
        )
        application.checklist_items.append(
            ChecklistItem(title="提出資料を確認", due_at=now + timedelta(days=1))
        )
        db.session.add(application)
        db.session.commit()

    response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "直近の未完了タスク" in html
    assert "提出資料を確認" in html
    assert "ダッシュボード株式会社" in html
    assert 'data-stat="incomplete-tasks">1</div>' in html


def test_deadline_state_classification(app):
    now = now_jst_naive()
    with app.app_context():
        assert ChecklistItem(due_at=now - timedelta(minutes=1)).deadline_state(now) == "overdue"
        assert ChecklistItem(due_at=now + timedelta(days=2)).deadline_state(now) == "urgent"
        assert ChecklistItem(due_at=now + timedelta(days=5)).deadline_state(now) == "soon"
        assert ChecklistItem(due_at=now + timedelta(days=8)).deadline_state(now) == "later"
        assert ChecklistItem().deadline_state(now) == "none"


def test_progress_percentage(app):
    with app.app_context():
        application = Application(
            company_name="進捗株式会社",
            status="応募予定",
            priority=3,
        )
        application.checklist_items = [
            ChecklistItem(title=f"タスク{index}", is_completed=index < 3)
            for index in range(7)
        ]
        db.session.add(application)
        db.session.commit()
        assert application.checklist_completed == 3
        assert application.checklist_total == 7
        assert application.checklist_progress == 43


def test_empty_checklist_progress_is_zero(app):
    with app.app_context():
        application = Application(
            company_name="空株式会社",
            status="応募予定",
            priority=3,
        )
        db.session.add(application)
        db.session.commit()
        assert application.checklist_progress == 0


def extract_csrf_token(html):
    match = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_forms_work_with_csrf_enabled():
    class CSRFTestConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "csrf-test-secret"

    test_app = create_app(CSRFTestConfig)
    client = test_app.test_client()
    with test_app.app_context():
        db.create_all()

        response = client.post("/applications/new", data=application_data())
        assert response.status_code == 400

        token = extract_csrf_token(
            client.get("/applications/new").get_data(as_text=True)
        )
        response = client.post(
            "/applications/new",
            data=application_data(csrf_token=token),
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert Application.query.count() == 1

        token = extract_csrf_token(
            client.get("/applications/1").get_data(as_text=True)
        )
        response = client.post(
            "/applications/1/checklist/new",
            data={
                "csrf_token": token,
                "title": "CSRF確認",
                "sort_order": "0",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert ChecklistItem.query.count() == 1

        db.session.remove()
        db.drop_all()
