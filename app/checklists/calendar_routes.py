import logging

from flask import current_app, flash, redirect, url_for

from app.extensions import db
from app.integrations.calendar_routes import get_calendar_service
from app.integrations.calendar_service import (
    CalendarEventNotFoundError,
    CalendarServiceError,
)
from app.integrations.calendar_sync_service import (
    CalendarSyncService,
    CalendarSyncStorageError,
)
from app.integrations.diagnostics import log_calendar_failure
from app.integrations.forms import (
    CreateCalendarEventForm,
    DeleteCalendarEventForm,
    UpdateCalendarEventForm,
)
from app.integrations.routes import get_credential_store
from app.models import CalendarSync, ChecklistItem

from . import bp


def get_calendar_sync_service():
    return CalendarSyncService()


def _checklist_redirect(item):
    return redirect(
        url_for("applications.detail", application_id=item.application_id)
        + "#checklist"
    )


def _log_route_start(operation):
    current_app.logger.warning(
        "Google Calendar route started operation=%s stage=route_start "
        "event_type=%s",
        operation,
        CalendarSync.EVENT_CHECKLIST_DUE,
    )


def _log_failure(operation, error, level=logging.ERROR):
    log_calendar_failure(
        current_app.logger,
        operation,
        error.stage,
        error.original_error,
        level=level,
        event_type=CalendarSync.EVENT_CHECKLIST_DUE,
    )


def _delete_sync(sync, operation):
    try:
        get_calendar_sync_service().delete(sync)
    except CalendarSyncStorageError as error:
        _log_failure(operation, error)
        current_app.logger.error(
            "Google Calendar sync state unchanged operation=%s "
            "event_type=%s stage=calendar_sync_delete "
            "sync_state_cleared=false",
            operation,
            CalendarSync.EVENT_CHECKLIST_DUE,
        )
        flash("Googleカレンダーの同期状態を更新できませんでした。", "danger")
        return False
    current_app.logger.warning(
        "Google Calendar sync state changed operation=%s event_type=%s "
        "stage=calendar_sync_delete sync_state_cleared=true",
        operation,
        CalendarSync.EVENT_CHECKLIST_DUE,
    )
    return True


@bp.route("/checklist/<int:item_id>/calendar/create", methods=["POST"])
def create_calendar_event(item_id):
    item = db.get_or_404(ChecklistItem, item_id)
    _log_route_start("create")
    form = CreateCalendarEventForm()
    if not form.validate_on_submit():
        flash("Googleカレンダー登録リクエストを確認できませんでした。", "danger")
        return _checklist_redirect(item)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))
    if item.due_at is None:
        flash("期限を設定してからGoogleカレンダーへ追加してください。", "warning")
        return _checklist_redirect(item)

    sync_service = get_calendar_sync_service()
    if sync_service.get_checklist_item(item.id) is not None:
        flash("このタスクはすでにGoogleカレンダーへ登録済みです。", "warning")
        return _checklist_redirect(item)

    try:
        event_id = get_calendar_service().create_checklist_due_event(item)
        sync_service.create_checklist_item(item.id, event_id)
    except (CalendarServiceError, CalendarSyncStorageError) as error:
        _log_failure("create", error)
        flash("Googleカレンダー登録に失敗しました。", "danger")
    else:
        current_app.logger.info(
            "Google Calendar completed operation=create stage=completed "
            "event_type=%s sync_state_cleared=false",
            CalendarSync.EVENT_CHECKLIST_DUE,
        )
        flash("タスク期限をGoogleカレンダーへ登録しました。", "success")
    return _checklist_redirect(item)


@bp.route("/checklist/<int:item_id>/calendar/update", methods=["POST"])
def update_calendar_event(item_id):
    item = db.get_or_404(ChecklistItem, item_id)
    _log_route_start("update")
    form = UpdateCalendarEventForm()
    if not form.validate_on_submit():
        flash("Googleカレンダー更新リクエストを確認できませんでした。", "danger")
        return _checklist_redirect(item)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    sync = get_calendar_sync_service().get_checklist_item(item.id)
    if sync is None:
        flash("Googleカレンダーへ未登録です。先に予定を登録してください。", "warning")
        return _checklist_redirect(item)
    if item.due_at is None:
        flash("期限を設定してからGoogleカレンダーを更新してください。", "warning")
        return _checklist_redirect(item)

    try:
        get_calendar_service().update_checklist_due_event(
            item,
            sync.external_event_id,
        )
    except CalendarEventNotFoundError as error:
        _log_failure("update", error, level=logging.WARNING)
        if _delete_sync(sync, "update"):
            flash(
                "Googleカレンダー上のタスク予定が削除されていたため、"
                "同期状態を解除しました。再度登録してください。",
                "warning",
            )
    except CalendarServiceError as error:
        _log_failure("update", error)
        flash("Googleカレンダーのタスク予定を更新できませんでした。", "danger")
    else:
        current_app.logger.info(
            "Google Calendar completed operation=update stage=completed "
            "event_type=%s sync_state_cleared=false",
            CalendarSync.EVENT_CHECKLIST_DUE,
        )
        flash("Googleカレンダーのタスク予定を更新しました。", "success")
    return _checklist_redirect(item)


@bp.route("/checklist/<int:item_id>/calendar/delete", methods=["POST"])
def delete_calendar_event(item_id):
    item = db.get_or_404(ChecklistItem, item_id)
    _log_route_start("delete")
    form = DeleteCalendarEventForm()
    if not form.validate_on_submit():
        flash("Googleカレンダー削除リクエストを確認できませんでした。", "danger")
        return _checklist_redirect(item)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    sync = get_calendar_sync_service().get_checklist_item(item.id)
    if sync is None:
        flash("Googleカレンダーへ登録済みのタスク予定はありません。", "warning")
        return _checklist_redirect(item)

    try:
        get_calendar_service().delete_checklist_due_event(
            sync.external_event_id
        )
    except CalendarEventNotFoundError as error:
        _log_failure("delete", error, level=logging.WARNING)
        if _delete_sync(sync, "delete"):
            flash(
                "Googleカレンダー上のタスク予定はすでに削除されていたため、"
                "同期状態を解除しました。",
                "warning",
            )
    except CalendarServiceError as error:
        _log_failure("delete", error)
        flash("Googleカレンダーのタスク予定を削除できませんでした。", "danger")
    else:
        if _delete_sync(sync, "delete"):
            current_app.logger.info(
                "Google Calendar completed operation=delete stage=completed "
                "event_type=%s sync_state_cleared=true",
                CalendarSync.EVENT_CHECKLIST_DUE,
            )
            flash("Googleカレンダーのタスク予定を削除しました。", "success")
    return _checklist_redirect(item)
