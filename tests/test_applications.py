from datetime import timedelta

from app.extensions import db
from app.models import Application, now_jst_naive


def application_data(**overrides):
    data = {
        "company_name": "テスト株式会社",
        "position_name": "エンジニア職",
        "application_url": "https://example.com/apply",
        "application_source": "企業サイト",
        "status": "応募済み",
        "priority": "4",
        "memo": "テストメモ",
    }
    data.update(overrides)
    return data


def test_top_page_is_displayed(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "就活ダッシュボード".encode() in response.data


def test_create_application(client, app):
    response = client.post(
        "/applications/new", data=application_data(), follow_redirects=True
    )
    assert response.status_code == 200
    assert "応募先を登録しました。".encode() in response.data
    with app.app_context():
        assert Application.query.count() == 1
        assert Application.query.first().company_name == "テスト株式会社"


def test_create_requires_company_name(client, app):
    response = client.post(
        "/applications/new",
        data=application_data(company_name=""),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "会社名を入力してください。".encode() in response.data
    with app.app_context():
        assert Application.query.count() == 0


def test_index_displays_application(client):
    client.post("/applications/new", data=application_data())
    response = client.get("/applications/")
    assert response.status_code == 200
    assert "テスト株式会社".encode() in response.data
    assert "応募済み".encode() in response.data


def test_edit_application(client, app):
    client.post("/applications/new", data=application_data())
    response = client.post(
        "/applications/1/edit",
        data=application_data(company_name="更新後株式会社", status="面接"),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "応募先を更新しました。".encode() in response.data
    with app.app_context():
        application = db.session.get(Application, 1)
        assert application.company_name == "更新後株式会社"
        assert application.status == "面接"


def test_delete_application(client, app):
    client.post("/applications/new", data=application_data())
    response = client.post("/applications/1/delete", follow_redirects=True)
    assert response.status_code == 200
    assert "応募先を削除しました。".encode() in response.data
    with app.app_context():
        assert Application.query.count() == 0


def test_delete_does_not_accept_get(client):
    client.post("/applications/new", data=application_data())
    response = client.get("/applications/1/delete")
    assert response.status_code == 405


def test_unknown_application_returns_404(client):
    response = client.get("/applications/999")
    assert response.status_code == 404
    assert "ページが見つかりません".encode() in response.data


def test_dashboard_counts_are_correct(client, app):
    now = now_jst_naive()
    with app.app_context():
        db.session.add_all(
            [
                Application(company_name="A社", status="ES作成中", priority=3),
                Application(
                    company_name="B社",
                    status="面接",
                    priority=5,
                    interview_at=now + timedelta(days=2),
                ),
                Application(company_name="C社", status="内定", priority=4),
                Application(company_name="D社", status="不合格", priority=2),
            ]
        )
        db.session.commit()

    response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-stat="total">4</div>' in html
    assert 'data-stat="es-pending">1</div>' in html
    assert 'data-stat="in-progress">2</div>' in html
    assert 'data-stat="interviews">1</div>' in html
    assert 'data-stat="offers">1</div>' in html
