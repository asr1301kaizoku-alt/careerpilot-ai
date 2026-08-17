import logging
import re

from flask import abort

from app import create_app
from app.extensions import db
from app.models import Application, ChecklistItem
from config import TestConfig


def test_navigation_marks_current_page_and_exposes_skip_link(client):
    response = client.get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'href="#main-content"' in html
    assert 'id="main-content"' in html
    assert 'aria-label="メインナビゲーション"' in html
    assert 'aria-controls="mainNav"' in html
    assert re.search(
        r'<a class="nav-link active" aria-current="page" href="/">'
        r"ダッシュボード</a>",
        html,
    )


def test_dashboard_links_to_both_primary_workflows(client):
    html = client.get("/").get_data(as_text=True)

    assert "就活メールを確認" in html
    assert 'href="/emails/"' in html
    assert "応募先を登録する" in html
    assert 'href="/applications/new"' in html


def test_long_running_actions_have_progressive_loading_hints(client, app):
    app.config.update(
        GOOGLE_CLIENT_ID="configured-client-id",
        GOOGLE_CLIENT_SECRET="configured-client-secret",
    )
    html = client.get("/settings/integrations").get_data(as_text=True)
    javascript = client.get("/static/js/app.js").get_data(as_text=True)

    assert 'data-loading-text="Googleへ移動中…"' in html
    assert "data-loading-text" in javascript
    assert 'form.setAttribute("aria-busy", "true")' in javascript
    assert 'link.setAttribute("aria-disabled", "true")' in javascript


def test_generic_bad_request_uses_safe_error_page():
    error_app = create_app(TestConfig)

    @error_app.get("/_test/bad-request")
    def bad_request_route():
        abort(400)

    response = error_app.test_client().get("/_test/bad-request")
    html = response.get_data(as_text=True)

    assert response.status_code == 400
    assert "リクエストを確認できませんでした" in html
    assert "ダッシュボードへ戻る" in html


def test_csrf_failure_uses_safe_guidance():
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "portfolio-polish-csrf-secret"

    csrf_app = create_app(CsrfConfig)
    response = csrf_app.test_client().post(
        "/applications/new",
        data={"company_name": "CSRF確認株式会社"},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 400
    assert "画面の有効期限が切れた可能性があります" in html
    assert "CSRF token" not in html


def test_internal_error_rolls_back_and_hides_exception_details(caplog):
    class ProductionLikeConfig(TestConfig):
        TESTING = False
        PROPAGATE_EXCEPTIONS = False

    error_app = create_app(ProductionLikeConfig)
    private_message = "private-runtime-detail-must-not-be-shown"

    @error_app.get("/_test/internal-error")
    def internal_error_route():
        raise RuntimeError(private_message)

    with caplog.at_level(logging.ERROR):
        response = error_app.test_client().get("/_test/internal-error")
    html = response.get_data(as_text=True)

    assert response.status_code == 500
    assert "処理を完了できませんでした" in html
    assert private_message not in html
    assert private_message not in caplog.text
    assert "exception=RuntimeError" in caplog.text


def test_application_detail_has_unique_ids_with_csrf_enabled():
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "portfolio-polish-id-secret"

    csrf_app = create_app(CsrfConfig)
    with csrf_app.app_context():
        db.create_all()
        application = Application(
            company_name="アクセシビリティ確認株式会社",
            status="応募予定",
            priority=3,
        )
        application.checklist_items.extend(
            (
                ChecklistItem(title="企業研究", sort_order=0),
                ChecklistItem(title="面接準備", sort_order=1),
            )
        )
        db.session.add(application)
        db.session.commit()
        application_id = application.id

    html = csrf_app.test_client().get(
        f"/applications/{application_id}"
    ).get_data(as_text=True)
    ids = re.findall(r'\sid="([^"]+)"', html)

    assert len(ids) == len(set(ids))
    assert "createCalendarCsrf-interview" in ids
    assert "toggleChecklistCsrf-1" in ids

    with csrf_app.app_context():
        db.session.remove()
        db.drop_all()
