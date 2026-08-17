from flask import flash, redirect, render_template, request, url_for

from sqlalchemy import func

from app.checklists.forms import ChecklistActionForm, ChecklistItemForm
from app.checklists.services import build_default_checklist, checklist_ordering
from app.extensions import db
from app.integrations.forms import (
    BulkCreateCalendarEventsForm,
    BulkDeleteCalendarEventsForm,
    BulkUpdateCalendarEventsForm,
    CreateCalendarEventForm,
    DeleteCalendarEventForm,
    UpdateCalendarEventForm,
)
from app.integrations.calendar_bulk_service import (
    is_bulk_create_candidate,
    is_bulk_delete_candidate,
    is_bulk_update_candidate,
)
from app.integrations.calendar_service import APPLICATION_EVENT_SPECS
from app.integrations.calendar_sync_service import CalendarSyncService
from app.models import Application, CalendarSync, ChecklistItem, now_jst_naive

from . import bp
from .calendar_view import sort_calendar_entries
from .forms import (
    ApplicationCreateForm,
    ApplicationForm,
    ApplicationSearchForm,
    DeleteForm,
)
from .query_helpers import (
    ApplicationSearchCriteria,
    build_applications_query,
    deadline_css_class,
    relevant_deadline,
)


APPLICATION_FORM_FIELDS = (
    "company_name",
    "position_name",
    "application_url",
    "application_source",
    "status",
    "es_deadline",
    "web_test_deadline",
    "interview_at",
    "interview_format",
    "priority",
    "memo",
)

CALENDAR_EVENT_UI = {
    CalendarSync.EVENT_INTERVIEW: {
        "create_endpoint": "integrations.create_calendar_event",
        "update_endpoint": "integrations.update_calendar_event",
        "delete_endpoint": "integrations.delete_calendar_event",
        "modal_id": "deleteCalendarEventModal",
        "delete_description": (
            "Googleカレンダー上の面接予定を削除します。"
            "CareerPilot AIの応募先情報は削除されません。"
        ),
    },
    CalendarSync.EVENT_ES_DEADLINE: {
        "create_endpoint": "integrations.create_es_deadline_calendar_event",
        "update_endpoint": "integrations.update_es_deadline_calendar_event",
        "delete_endpoint": "integrations.delete_es_deadline_calendar_event",
        "modal_id": "deleteEsDeadlineCalendarEventModal",
        "delete_description": (
            "Googleカレンダー上のES締切予定を削除します。"
            "CareerPilot AIのES締切情報は削除されません。"
        ),
    },
    CalendarSync.EVENT_WEB_TEST_DEADLINE: {
        "create_endpoint": "integrations.create_web_test_calendar_event",
        "update_endpoint": "integrations.update_web_test_calendar_event",
        "delete_endpoint": "integrations.delete_web_test_calendar_event",
        "modal_id": "deleteWebTestCalendarEventModal",
        "delete_description": (
            "Googleカレンダー上のWebテスト期限予定を削除します。"
            "CareerPilot AIのWebテスト期限情報は削除されません。"
        ),
    },
}


def populate_application(form, application):
    for field_name in APPLICATION_FORM_FIELDS:
        setattr(application, field_name, getattr(form, field_name).data)


def build_calendar_entries(application):
    syncs = CalendarSyncService().get_application_syncs(application.id)
    entries = []
    for event_type, spec in APPLICATION_EVENT_SPECS.items():
        entries.append(
            {
                "event_type": event_type,
                "label": spec.display_label,
                "scheduled_at": getattr(application, spec.datetime_attribute),
                "sync": syncs.get(event_type),
                **CALENDAR_EVENT_UI[event_type],
            }
        )
    return sort_calendar_entries(entries), bool(syncs)


def render_detail(application, checklist_form=None):
    checklist_items = db.session.scalars(
        db.select(ChecklistItem)
        .where(ChecklistItem.application_id == application.id)
        .order_by(*checklist_ordering())
    ).all()
    checklist_completed = sum(item.is_completed for item in checklist_items)
    checklist_total = len(checklist_items)
    checklist_progress = (
        round(checklist_completed * 100 / checklist_total) if checklist_total else 0
    )
    checklist_calendar_syncs = CalendarSyncService().get_checklist_item_syncs(
        [item.id for item in checklist_items]
    )
    calendar_entries, has_application_calendar_syncs = build_calendar_entries(
        application
    )
    has_calendar_syncs = has_application_calendar_syncs or bool(
        checklist_calendar_syncs
    )
    has_bulk_create_targets = any(
        is_bulk_create_candidate(entry["scheduled_at"], entry["sync"])
        for entry in calendar_entries
    )
    has_bulk_update_targets = any(
        is_bulk_update_candidate(entry["scheduled_at"], entry["sync"])
        for entry in calendar_entries
    )
    has_bulk_delete_targets = any(
        is_bulk_delete_candidate(entry["scheduled_at"], entry["sync"])
        for entry in calendar_entries
    )
    return render_template(
        "applications/detail.html",
        application=application,
        checklist_items=checklist_items,
        checklist_completed=checklist_completed,
        checklist_total=checklist_total,
        checklist_progress=checklist_progress,
        checklist_calendar_syncs=checklist_calendar_syncs,
        calendar_entries=calendar_entries,
        has_calendar_syncs=has_calendar_syncs,
        has_bulk_create_targets=has_bulk_create_targets,
        has_bulk_update_targets=has_bulk_update_targets,
        has_bulk_delete_targets=has_bulk_delete_targets,
        checklist_form=checklist_form or ChecklistItemForm(),
        checklist_action_form=ChecklistActionForm(),
        calendar_create_form=CreateCalendarEventForm(),
        calendar_bulk_create_form=BulkCreateCalendarEventsForm(),
        calendar_bulk_update_form=BulkUpdateCalendarEventsForm(),
        calendar_bulk_delete_form=BulkDeleteCalendarEventsForm(),
        calendar_update_form=UpdateCalendarEventForm(),
        calendar_delete_form=DeleteCalendarEventForm(),
        delete_form=DeleteForm(),
        now=now_jst_naive(),
    )


@bp.route("")
@bp.route("/")
def index():
    now = now_jst_naive()
    criteria = ApplicationSearchCriteria.from_args(request.args)
    search_form = ApplicationSearchForm(data=criteria.as_form_data())
    statement = build_applications_query(criteria, now)
    applications = db.session.scalars(statement).all()
    total_count = (
        db.session.scalar(db.select(func.count()).select_from(Application)) or 0
    )
    return render_template(
        "applications/index.html",
        applications=applications,
        search_form=search_form,
        criteria=criteria,
        result_count=len(applications),
        total_count=total_count,
        relevant_deadline=relevant_deadline,
        deadline_css_class=deadline_css_class,
        now=now,
    )


@bp.route("/new", methods=["GET", "POST"])
def create():
    form = ApplicationCreateForm()
    if form.validate_on_submit():
        application = Application()
        populate_application(form, application)
        db.session.add(application)
        if form.create_default_checklist.data:
            application.checklist_items.extend(build_default_checklist())
        db.session.commit()
        flash("応募先を登録しました。", "success")
        return redirect(url_for("applications.detail", application_id=application.id))
    return render_template(
        "applications/form.html", form=form, page_title="応募先を登録"
    )


@bp.route("/<int:application_id>")
def detail(application_id):
    application = db.get_or_404(Application, application_id)
    return render_detail(application)


@bp.route("/<int:application_id>/edit", methods=["GET", "POST"])
def edit(application_id):
    application = db.get_or_404(Application, application_id)
    form = ApplicationForm(obj=application)
    if form.validate_on_submit():
        populate_application(form, application)
        db.session.commit()
        flash("応募先を更新しました。", "success")
        return redirect(url_for("applications.detail", application_id=application.id))
    return render_template(
        "applications/form.html",
        form=form,
        page_title="応募先を編集",
        application=application,
    )


@bp.route("/<int:application_id>/delete", methods=["POST"])
def delete(application_id):
    application = db.get_or_404(Application, application_id)
    form = DeleteForm()
    if form.validate_on_submit():
        db.session.delete(application)
        db.session.commit()
        flash("応募先を削除しました。", "success")
    else:
        flash("削除リクエストを確認できませんでした。", "danger")
    return redirect(url_for("applications.index"))
