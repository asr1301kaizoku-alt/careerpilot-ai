from datetime import timedelta

from app.extensions import db
from app.models import Application, ChecklistItem, now_jst_naive


def seed_applications(app):
    now = now_jst_naive()
    with app.app_context():
        applications = [
            Application(
                company_name="Fictional Zenith Labs",
                position_name="AI Engineer",
                status="応募予定",
                priority=5,
                es_deadline=now + timedelta(days=2),
                web_test_deadline=now + timedelta(days=10),
                interview_at=now + timedelta(days=5),
                updated_at=now - timedelta(days=1),
            ),
            Application(
                company_name="Beta株式会社",
                position_name="総合職",
                status="面接",
                priority=3,
                es_deadline=now - timedelta(days=1),
                interview_at=now + timedelta(days=1),
                updated_at=now - timedelta(days=3),
            ),
            Application(
                company_name="Cyber Works",
                position_name="データサイエンティスト",
                status="内定",
                priority=4,
                es_deadline=now + timedelta(days=5),
                web_test_deadline=now + timedelta(days=8),
                updated_at=now - timedelta(days=2),
            ),
            Application(
                company_name="Delta商事",
                position_name="営業職",
                status="不合格",
                priority=1,
                interview_at=now + timedelta(days=3),
                updated_at=now - timedelta(days=4),
            ),
            Application(
                company_name="Epsilon",
                position_name="AI Engineer",
                status="応募予定",
                priority=2,
                es_deadline=now + timedelta(days=10),
                updated_at=now - timedelta(days=5),
            ),
        ]
        applications[0].checklist_items = [
            ChecklistItem(title="企業研究", is_completed=True),
            ChecklistItem(title="ES提出"),
        ]
        db.session.add_all(applications)
        db.session.commit()
    return now


def response_html(client, **query):
    return client.get("/applications", query_string=query).get_data(as_text=True)


def assert_company_order(html, expected):
    positions = [html.index(company_name) for company_name in expected]
    assert positions == sorted(positions)


def test_no_conditions_displays_all_applications(client, app):
    seed_applications(app)
    response = client.get("/applications")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    for company_name in (
        "Fictional Zenith Labs",
        "Beta株式会社",
        "Cyber Works",
        "Delta商事",
        "Epsilon",
    ):
        assert company_name in html
    assert "検索結果 <strong>5</strong>件 / 全5件" in html


def test_company_name_partial_search_is_case_insensitive(client, app):
    seed_applications(app)
    html = response_html(client, q="zenith")
    assert "Fictional Zenith Labs" in html
    assert "Beta株式会社" not in html


def test_position_name_partial_search(client, app):
    seed_applications(app)
    html = response_html(client, q="Engineer")
    assert "Fictional Zenith Labs" in html
    assert "Epsilon" in html
    assert "Cyber Works" not in html


def test_keyword_trims_surrounding_spaces(client, app):
    seed_applications(app)
    html = response_html(client, q="  Zenith  ")
    assert "Fictional Zenith Labs" in html
    assert "Beta株式会社" not in html
    assert 'value="Zenith"' in html


def test_whitespace_only_keyword_displays_all(client, app):
    seed_applications(app)
    html = response_html(client, q="   ")
    assert "検索結果 <strong>5</strong>件 / 全5件" in html


def test_status_filter(client, app):
    seed_applications(app)
    html = response_html(client, status="応募予定")
    assert "Fictional Zenith Labs" in html
    assert "Epsilon" in html
    assert "Beta株式会社" not in html


def test_priority_minimum_filter(client, app):
    seed_applications(app)
    html = response_html(client, priority="4")
    assert "Fictional Zenith Labs" in html
    assert "Cyber Works" in html
    assert "Beta株式会社" not in html


def test_overdue_deadline_filter(client, app):
    seed_applications(app)
    html = response_html(client, deadline="overdue")
    assert "Beta株式会社" in html
    assert "Fictional Zenith Labs" not in html


def test_three_day_deadline_filter(client, app):
    seed_applications(app)
    html = response_html(client, deadline="3days")
    assert "Fictional Zenith Labs" in html
    assert "Cyber Works" not in html


def test_seven_day_deadline_filter(client, app):
    seed_applications(app)
    html = response_html(client, deadline="7days")
    assert "Fictional Zenith Labs" in html
    assert "Cyber Works" in html
    assert "Epsilon" not in html


def test_fourteen_day_deadline_filter(client, app):
    seed_applications(app)
    html = response_html(client, deadline="14days")
    assert "Fictional Zenith Labs" in html
    assert "Cyber Works" in html
    assert "Epsilon" in html
    assert "Beta株式会社" not in html


def test_no_deadline_filter(client, app):
    seed_applications(app)
    html = response_html(client, deadline="none")
    assert "Delta商事" in html
    assert "Fictional Zenith Labs" not in html


def test_upcoming_deadline_takes_priority_over_expired_deadline(client, app):
    now = now_jst_naive()
    with app.app_context():
        db.session.add(
            Application(
                company_name="Mixed Deadline",
                status="応募予定",
                priority=3,
                es_deadline=now - timedelta(days=1),
                web_test_deadline=now + timedelta(days=2),
            )
        )
        db.session.commit()
    assert "Mixed Deadline" in response_html(client, deadline="3days")
    assert "Mixed Deadline" not in response_html(client, deadline="overdue")


def test_combined_filters(client, app):
    seed_applications(app)
    html = response_html(
        client,
        q="AI",
        status="応募予定",
        priority="3",
        deadline="7days",
    )
    assert "Fictional Zenith Labs" in html
    assert "Epsilon" not in html
    assert "検索結果 <strong>1</strong>件 / 全5件" in html


def test_updated_at_sorting(client, app):
    seed_applications(app)
    newest = response_html(client, sort="updated_desc")
    assert_company_order(
        newest,
        ["Fictional Zenith Labs", "Cyber Works", "Beta株式会社", "Delta商事", "Epsilon"],
    )
    oldest = response_html(client, sort="updated_asc")
    assert_company_order(
        oldest,
        ["Epsilon", "Delta商事", "Beta株式会社", "Cyber Works", "Fictional Zenith Labs"],
    )


def test_company_name_sorting(client, app):
    seed_applications(app)
    ascending = response_html(client, sort="company_asc")
    assert_company_order(
        ascending,
        ["Beta株式会社", "Cyber Works", "Delta商事", "Epsilon", "Fictional Zenith Labs"],
    )
    descending = response_html(client, sort="company_desc")
    assert_company_order(
        descending,
        ["Fictional Zenith Labs", "Epsilon", "Delta商事", "Cyber Works", "Beta株式会社"],
    )


def test_priority_sorting(client, app):
    seed_applications(app)
    high_first = response_html(client, sort="priority_desc")
    assert_company_order(
        high_first,
        ["Fictional Zenith Labs", "Cyber Works", "Beta株式会社", "Epsilon", "Delta商事"],
    )
    low_first = response_html(client, sort="priority_asc")
    assert_company_order(
        low_first,
        ["Delta商事", "Epsilon", "Beta株式会社", "Cyber Works", "Fictional Zenith Labs"],
    )


def test_deadline_sort_places_missing_deadline_last(client, app):
    seed_applications(app)
    html = response_html(client, sort="deadline_asc")
    assert_company_order(
        html,
        ["Beta株式会社", "Fictional Zenith Labs", "Cyber Works", "Epsilon", "Delta商事"],
    )


def test_interview_sort_places_missing_interview_last(client, app):
    seed_applications(app)
    html = response_html(client, sort="interview_asc")
    assert_company_order(
        html,
        ["Beta株式会社", "Delta商事", "Fictional Zenith Labs", "Cyber Works", "Epsilon"],
    )


def test_invalid_query_values_fall_back_without_error(client, app):
    seed_applications(app)
    response = client.get(
        "/applications",
        query_string={
            "status": "invalid",
            "priority": "99",
            "deadline": "invalid",
            "sort": "invalid",
        },
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "検索結果 <strong>5</strong>件 / 全5件" in html
    assert_company_order(
        html,
        ["Fictional Zenith Labs", "Cyber Works", "Beta株式会社", "Delta商事", "Epsilon"],
    )


def test_search_conditions_are_retained(client, app):
    seed_applications(app)
    html = response_html(
        client,
        q="Zenith",
        status="応募予定",
        priority="4",
        deadline="7days",
        sort="deadline_asc",
    )
    assert 'value="Zenith"' in html
    assert '<option selected value="応募予定">応募予定</option>' in html
    assert '<option selected value="4">4以上</option>' in html
    assert '<option selected value="7days">7日以内</option>' in html
    assert '<option selected value="deadline_asc">締切が近い順</option>' in html


def test_zero_results_message_and_reset_link(client, app):
    seed_applications(app)
    html = response_html(client, q="存在しない企業")
    assert "検索結果 <strong>0</strong>件 / 全5件" in html
    assert "条件に一致する応募先はありません" in html
    assert "条件をリセット" in html


def test_reset_url_displays_all_applications(client, app):
    seed_applications(app)
    assert "検索結果 <strong>1</strong>件 / 全5件" in response_html(
        client, q="Zenith"
    )
    reset_html = response_html(client)
    assert "検索結果 <strong>5</strong>件 / 全5件" in reset_html


def test_checklist_progress_is_displayed_without_n_plus_one(client, app):
    seed_applications(app)
    html = response_html(client, q="Zenith")
    assert "1 / 2（50%）" in html


def test_existing_detail_edit_and_delete_still_work(client, app):
    with app.app_context():
        db.session.add(
            Application(
                company_name="CRUD確認株式会社",
                status="応募予定",
                priority=3,
            )
        )
        db.session.commit()

    assert client.get("/applications/1").status_code == 200
    edit_response = client.post(
        "/applications/1/edit",
        data={
            "company_name": "CRUD更新株式会社",
            "status": "面接",
            "priority": "4",
        },
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    assert "CRUD更新株式会社" in edit_response.get_data(as_text=True)
    assert client.post("/applications/1/delete").status_code == 302
    with app.app_context():
        assert Application.query.count() == 0
