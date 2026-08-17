import re
from datetime import datetime

import pytest

from app.applications.calendar_view import sort_calendar_entries
from app.extensions import db
from app.models import Application, CalendarSync


EVENT_LABELS = {
    CalendarSync.EVENT_INTERVIEW: "面接",
    CalendarSync.EVENT_ES_DEADLINE: "ES締切",
    CalendarSync.EVENT_WEB_TEST_DEADLINE: "Webテスト期限",
}


def add_application(*, interview_at=None, es_deadline=None, web_test_deadline=None):
    application = Application(
        company_name="Calendar Order Test",
        position_name="エンジニア",
        status="面接",
        priority=4,
        interview_at=interview_at,
        es_deadline=es_deadline,
        web_test_deadline=web_test_deadline,
    )
    db.session.add(application)
    db.session.commit()
    return application.id


def add_sync(application_id, event_type):
    db.session.add(
        CalendarSync(
            application_id=application_id,
            event_type=event_type,
            provider=CalendarSync.PROVIDER_GOOGLE,
            calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
            external_event_id=f"{event_type}-event",
        )
    )
    db.session.commit()


def calendar_sections(html):
    return re.findall(
        r'<section class="calendar-sync-entry">(.*?)</section>',
        html,
        re.DOTALL,
    )


def calendar_labels(html):
    labels = []
    for section in calendar_sections(html):
        match = re.search(r'<h3 class="h6 fw-bold mb-1">([^<]+)</h3>', section)
        assert match is not None
        labels.append(match.group(1).strip())
    return labels


@pytest.mark.parametrize(
    ("interview_at", "es_deadline", "web_test_deadline", "expected"),
    [
        pytest.param(
            datetime(2026, 8, 12, 12, 30),
            datetime(2026, 8, 9, 23, 59),
            datetime(2026, 8, 10, 23, 59),
            ["ES締切", "Webテスト期限", "面接"],
            id="deadlines-before-interview",
        ),
        pytest.param(
            datetime(2026, 8, 8, 10, 0),
            datetime(2026, 8, 9, 23, 59),
            datetime(2026, 8, 10, 23, 59),
            ["面接", "ES締切", "Webテスト期限"],
            id="interview-before-deadlines",
        ),
    ],
)
def test_calendar_entries_are_displayed_in_application_datetime_order(
    client,
    app,
    interview_at,
    es_deadline,
    web_test_deadline,
    expected,
):
    with app.app_context():
        application_id = add_application(
            interview_at=interview_at,
            es_deadline=es_deadline,
            web_test_deadline=web_test_deadline,
        )

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert calendar_labels(html) == expected


def test_calendar_order_reflects_edited_application_datetimes(client, app):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 12, 12, 30),
            es_deadline=datetime(2026, 8, 9, 23, 59),
            web_test_deadline=datetime(2026, 8, 10, 23, 59),
        )

    response = client.post(
        f"/applications/{application_id}/edit",
        data={
            "company_name": "Calendar Order Test",
            "position_name": "エンジニア",
            "application_url": "",
            "application_source": "",
            "status": "面接",
            "priority": "4",
            "es_deadline": "2026-08-11T23:59",
            "web_test_deadline": "2026-08-12T23:59",
            "interview_at": "2026-08-08T10:00",
            "interview_format": "",
            "memo": "",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert calendar_labels(response.get_data(as_text=True)) == [
        "面接",
        "ES締切",
        "Webテスト期限",
    ]


def test_calendar_entries_without_datetimes_are_displayed_last(client, app):
    with app.app_context():
        application_id = add_application(
            es_deadline=datetime(2026, 8, 9, 23, 59),
        )

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert calendar_labels(html) == ["ES締切", "面接", "Webテスト期限"]


def test_calendar_entries_with_same_datetime_have_stable_type_order(client, app):
    same_datetime = datetime(2026, 8, 9, 23, 59)
    with app.app_context():
        application_id = add_application(
            interview_at=same_datetime,
            es_deadline=same_datetime,
            web_test_deadline=same_datetime,
        )

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert calendar_labels(html) == ["ES締切", "面接", "Webテスト期限"]


def test_calendar_order_does_not_depend_on_sync_state(client, app):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 12, 12, 30),
            es_deadline=datetime(2026, 8, 9, 23, 59),
            web_test_deadline=datetime(2026, 8, 10, 23, 59),
        )
        add_sync(application_id, CalendarSync.EVENT_INTERVIEW)
        add_sync(application_id, CalendarSync.EVENT_WEB_TEST_DEADLINE)

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert calendar_labels(html) == ["ES締切", "Webテスト期限", "面接"]


def test_past_calendar_datetimes_remain_visible(client, app):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2020, 1, 3, 10, 0),
            es_deadline=datetime(2020, 1, 1, 10, 0),
            web_test_deadline=datetime(2020, 1, 2, 10, 0),
        )

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)

    assert calendar_labels(html) == ["ES締切", "Webテスト期限", "面接"]
    assert len(calendar_sections(html)) == 3


def test_sorted_calendar_entries_keep_their_event_type_forms(client, app):
    with app.app_context():
        application_id = add_application(
            interview_at=datetime(2026, 8, 12, 12, 30),
            es_deadline=datetime(2026, 8, 9, 23, 59),
            web_test_deadline=datetime(2026, 8, 10, 23, 59),
        )
        for event_type in EVENT_LABELS:
            add_sync(application_id, event_type)

    html = client.get(f"/applications/{application_id}").get_data(as_text=True)
    sections_by_label = dict(zip(calendar_labels(html), calendar_sections(html)))

    expected_forms = {
        "面接": (
            "calendar/update",
            "calendar/delete",
            "deleteCalendarEventModal",
        ),
        "ES締切": (
            "calendar/es-deadline/update",
            "calendar/es-deadline/delete",
            "deleteEsDeadlineCalendarEventModal",
        ),
        "Webテスト期限": (
            "calendar/web-test/update",
            "calendar/web-test/delete",
            "deleteWebTestCalendarEventModal",
        ),
    }
    for label, (update_path, delete_path, modal_id) in expected_forms.items():
        section = sections_by_label[label]
        assert f'/applications/{application_id}/{update_path}' in section
        assert f'data-bs-target="#{modal_id}"' in section
        assert f'id="{modal_id}"' in html
        assert f'/applications/{application_id}/{delete_path}' in html


def test_sort_calendar_entries_is_stable_when_two_datetimes_are_missing():
    entries = [
        {"event_type": "web_test_deadline", "scheduled_at": None},
        {"event_type": "interview", "scheduled_at": None},
        {
            "event_type": "es_deadline",
            "scheduled_at": datetime(2026, 8, 9, 23, 59),
        },
    ]

    first = sort_calendar_entries(entries)
    second = sort_calendar_entries(reversed(entries))

    expected_types = ["es_deadline", "interview", "web_test_deadline"]
    assert [entry["event_type"] for entry in first] == expected_types
    assert [entry["event_type"] for entry in second] == expected_types
