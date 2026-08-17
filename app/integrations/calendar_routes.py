import logging

from flask import current_app, flash, redirect, url_for

from app.extensions import db
from app.models import Application, CalendarSync

from . import bp
from .calendar_service import (
    APPLICATION_EVENT_SPECS,
    CalendarEventNotFoundError,
    CalendarServiceError,
    GoogleCalendarService,
)
from .calendar_bulk_service import (
    CalendarBulkCreateService,
    CalendarBulkDeleteService,
    CalendarBulkUpdateService,
)
from .calendar_sync_service import (
    CalendarSyncService,
    CalendarSyncStorageError,
)
from .diagnostics import log_calendar_failure
from .forms import (
    BulkCreateCalendarEventsForm,
    BulkDeleteCalendarEventsForm,
    BulkUpdateCalendarEventsForm,
    CreateCalendarEventForm,
    DeleteCalendarEventForm,
    UpdateCalendarEventForm,
)
from .routes import get_credential_store


def get_calendar_service():
    return GoogleCalendarService(
        get_credential_store(),
        current_app.config["GOOGLE_CLIENT_ID"],
        current_app.config["GOOGLE_CLIENT_SECRET"],
    )


def get_calendar_sync_service():
    return CalendarSyncService()


def get_calendar_bulk_create_service():
    return CalendarBulkCreateService(
        get_calendar_service(),
        get_calendar_sync_service(),
    )


def get_calendar_bulk_update_service():
    return CalendarBulkUpdateService(
        get_calendar_service(),
        get_calendar_sync_service(),
    )


def get_calendar_bulk_delete_service():
    return CalendarBulkDeleteService(
        get_calendar_service(),
        get_calendar_sync_service(),
    )


def _detail_redirect(application):
    return redirect(
        url_for("applications.detail", application_id=application.id)
    )


def _log_route_start(operation, event_type):
    current_app.logger.warning(
        "Google Calendar route started operation=%s stage=route_start "
        "event_type=%s",
        operation,
        event_type,
    )


def _log_failure(operation, event_type, error, level=logging.ERROR):
    log_calendar_failure(
        current_app.logger,
        operation,
        error.stage,
        error.original_error,
        level=level,
        event_type=event_type,
    )


def _delete_calendar_sync(sync, operation, event_type):
    try:
        get_calendar_sync_service().delete(sync)
    except CalendarSyncStorageError as error:
        _log_failure(operation, event_type, error)
        current_app.logger.error(
            "Google Calendar sync state unchanged operation=%s event_type=%s "
            "stage=calendar_sync_delete sync_state_cleared=false",
            operation,
            event_type,
        )
        flash("Googleカレンダーの同期状態を更新できませんでした。", "danger")
        return False
    current_app.logger.warning(
        "Google Calendar sync state changed operation=%s event_type=%s "
        "stage=calendar_sync_delete sync_state_cleared=true",
        operation,
        event_type,
    )
    return True


def _create_google_event(service, application, event_type):
    if event_type == CalendarSync.EVENT_INTERVIEW:
        return service.create_interview_event(application)
    return service.create_calendar_event(application, event_type)


def _update_google_event(service, application, event_type, event_id):
    if event_type == CalendarSync.EVENT_INTERVIEW:
        return service.update_interview_event(application, event_id)
    return service.update_calendar_event(application, event_type, event_id)


def _delete_google_event(service, event_type, event_id):
    if event_type == CalendarSync.EVENT_INTERVIEW:
        return service.delete_interview_event(event_id)
    return service.delete_calendar_event(event_id)


def _bulk_create_flash_message(result):
    if result.target_count == 0:
        return "登録できる未同期の予定はありません。"

    messages = []
    if result.created_count:
        messages.append(
            f"Googleカレンダーへ{result.created_count}件登録しました。"
        )
    if result.failed_count:
        messages.append(
            f"{result.failed_count}件は登録できませんでした。"
        )
    if result.skipped_count:
        messages.append(
            f"{result.skipped_count}件は同期済みまたは日時未設定のため"
            "スキップしました。"
        )
    return "".join(messages)


def _bulk_create_flash_category(result):
    if result.target_count == 0:
        return "warning"
    if result.failed_count and not result.created_count:
        return "danger"
    if result.failed_count:
        return "warning"
    return "success"


def _bulk_update_flash_message(result):
    if result.target_count == 0:
        return "更新できる同期済みの予定はありません。"

    messages = []
    if result.updated_count:
        messages.append(
            f"Googleカレンダーの予定を{result.updated_count}件更新しました。"
        )
    if result.sync_cleared_count:
        messages.append(
            f"{result.sync_cleared_count}件はGoogleカレンダー上で"
            "削除されていたため同期を解除しました。"
        )
    if result.failed_count:
        messages.append(
            f"{result.failed_count}件は更新できませんでした。"
        )
    if result.skipped_count:
        messages.append(
            f"{result.skipped_count}件は未同期または日時未設定のため"
            "スキップしました。"
        )
    return "".join(messages)


def _bulk_update_flash_category(result):
    if result.target_count == 0:
        return "warning"
    if result.failed_count and not (
        result.updated_count or result.sync_cleared_count
    ):
        return "danger"
    if result.failed_count or result.sync_cleared_count:
        return "warning"
    return "success"


def _bulk_delete_flash_message(result):
    if result.target_count == 0:
        return "削除できる同期済みの予定はありません。"

    messages = []
    if result.deleted_count:
        messages.append(
            f"Googleカレンダーの予定を{result.deleted_count}件削除しました。"
        )
    if result.already_deleted_count:
        messages.append(
            f"{result.already_deleted_count}件はGoogleカレンダー上で"
            "すでに削除されていました。"
        )
    if result.failed_count:
        messages.append(
            f"{result.failed_count}件は削除できませんでした。"
        )
    return "".join(messages)


def _bulk_delete_flash_category(result):
    if result.target_count == 0:
        return "warning"
    if result.failed_count and not (
        result.deleted_count or result.already_deleted_count
    ):
        return "danger"
    if result.failed_count or result.already_deleted_count:
        return "warning"
    return "success"


def _bulk_create_application_calendar_events(application_id):
    application = db.get_or_404(Application, application_id)
    _log_route_start("bulk_create", "application_events")
    form = BulkCreateCalendarEventsForm()
    if not form.validate_on_submit():
        flash(
            "Googleカレンダー一括登録リクエストを確認できませんでした。",
            "danger",
        )
        return _detail_redirect(application)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    result = get_calendar_bulk_create_service().create_application_events(
        application
    )
    for failure in result.failures:
        _log_failure(
            "bulk_create",
            failure.event_type,
            failure.error,
        )
    current_app.logger.info(
        "Google Calendar completed operation=bulk_create stage=completed "
        "created_count=%s skipped_count=%s failed_count=%s",
        result.created_count,
        result.skipped_count,
        result.failed_count,
    )
    flash(
        _bulk_create_flash_message(result),
        _bulk_create_flash_category(result),
    )
    return _detail_redirect(application)


def _bulk_update_application_calendar_events(application_id):
    application = db.get_or_404(Application, application_id)
    _log_route_start("bulk_update", "application_events")
    form = BulkUpdateCalendarEventsForm()
    if not form.validate_on_submit():
        flash(
            "Googleカレンダー一括更新リクエストを確認できませんでした。",
            "danger",
        )
        return _detail_redirect(application)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    result = get_calendar_bulk_update_service().update_application_events(
        application
    )
    for cleared in result.sync_cleared:
        _log_failure(
            "bulk_update",
            cleared.event_type,
            cleared.error,
            level=logging.WARNING,
        )
        current_app.logger.warning(
            "Google Calendar sync state changed operation=bulk_update "
            "event_type=%s stage=calendar_sync_delete "
            "sync_state_cleared=true",
            cleared.event_type,
        )
    for failure in result.failures:
        _log_failure(
            "bulk_update",
            failure.event_type,
            failure.error,
        )
    current_app.logger.info(
        "Google Calendar completed operation=bulk_update stage=completed "
        "success_count=%s skipped_count=%s failed_count=%s "
        "sync_cleared_count=%s",
        result.updated_count,
        result.skipped_count,
        result.failed_count,
        result.sync_cleared_count,
    )
    flash(
        _bulk_update_flash_message(result),
        _bulk_update_flash_category(result),
    )
    return _detail_redirect(application)


def _bulk_delete_application_calendar_events(application_id):
    application = db.get_or_404(Application, application_id)
    _log_route_start("bulk_delete", "application_events")
    form = BulkDeleteCalendarEventsForm()
    if not form.validate_on_submit():
        flash(
            "Googleカレンダー一括削除リクエストを確認できませんでした。",
            "danger",
        )
        return _detail_redirect(application)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    result = get_calendar_bulk_delete_service().delete_application_events(
        application
    )
    for already_deleted in result.already_deleted:
        _log_failure(
            "bulk_delete",
            already_deleted.event_type,
            already_deleted.error,
            level=logging.WARNING,
        )
        current_app.logger.warning(
            "Google Calendar sync state changed operation=bulk_delete "
            "event_type=%s stage=calendar_sync_delete "
            "sync_state_cleared=true already_deleted=true",
            already_deleted.event_type,
        )
    for failure in result.failures:
        _log_failure(
            "bulk_delete",
            failure.event_type,
            failure.error,
        )
    current_app.logger.info(
        "Google Calendar completed operation=bulk_delete stage=completed "
        "deleted_count=%s already_deleted_count=%s failed_count=%s",
        result.deleted_count,
        result.already_deleted_count,
        result.failed_count,
    )
    flash(
        _bulk_delete_flash_message(result),
        _bulk_delete_flash_category(result),
    )
    return _detail_redirect(application)


def _create_application_calendar_event(application_id, event_type):
    application = db.get_or_404(Application, application_id)
    spec = APPLICATION_EVENT_SPECS[event_type]
    _log_route_start("create", event_type)
    form = CreateCalendarEventForm()
    if not form.validate_on_submit():
        flash("Googleカレンダー登録リクエストを確認できませんでした。", "danger")
        return _detail_redirect(application)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    if getattr(application, spec.datetime_attribute) is None:
        flash(
            f"{spec.datetime_label}を登録してから"
            "Googleカレンダーへ追加してください。",
            "warning",
        )
        return _detail_redirect(application)

    sync_service = get_calendar_sync_service()
    if sync_service.get_application(application.id, event_type) is not None:
        prefix = "この面接" if event_type == CalendarSync.EVENT_INTERVIEW else spec.display_label
        flash(
            f"{prefix}はすでにGoogleカレンダーへ登録済みです。",
            "warning",
        )
        return _detail_redirect(application)

    try:
        event_id = _create_google_event(
            get_calendar_service(), application, event_type
        )
        sync_service.create_application(application.id, event_type, event_id)
    except CalendarServiceError as error:
        _log_failure("create", event_type, error)
        flash("Googleカレンダー登録に失敗しました。", "danger")
    except CalendarSyncStorageError as error:
        _log_failure("create", event_type, error)
        flash("Googleカレンダー登録に失敗しました。", "danger")
    else:
        current_app.logger.info(
            "Google Calendar completed operation=create stage=completed "
            "event_type=%s sync_state_cleared=false",
            event_type,
        )
        flash("Googleカレンダーへ登録しました。", "success")

    return _detail_redirect(application)


def _update_application_calendar_event(application_id, event_type):
    application = db.get_or_404(Application, application_id)
    spec = APPLICATION_EVENT_SPECS[event_type]
    _log_route_start("update", event_type)
    form = UpdateCalendarEventForm()
    if not form.validate_on_submit():
        flash("Googleカレンダー更新リクエストを確認できませんでした。", "danger")
        return _detail_redirect(application)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    sync = get_calendar_sync_service().get_application(application.id, event_type)
    if sync is None:
        flash("Googleカレンダーへ未登録です。先に予定を登録してください。", "warning")
        return _detail_redirect(application)
    if getattr(application, spec.datetime_attribute) is None:
        flash(
            f"{spec.datetime_label}を登録してから"
            "Googleカレンダーを更新してください。",
            "warning",
        )
        return _detail_redirect(application)

    try:
        _update_google_event(
            get_calendar_service(),
            application,
            event_type,
            sync.external_event_id,
        )
    except CalendarEventNotFoundError as error:
        _log_failure("update", event_type, error, level=logging.WARNING)
        if _delete_calendar_sync(sync, "update", event_type):
            flash(
                "Googleカレンダー上の予定が削除されていたため、"
                "同期状態を解除しました。再度登録してください。",
                "warning",
            )
    except CalendarServiceError as error:
        _log_failure("update", event_type, error)
        flash("Googleカレンダーの予定を更新できませんでした。", "danger")
    else:
        current_app.logger.info(
            "Google Calendar completed operation=update stage=completed "
            "event_type=%s sync_state_cleared=false",
            event_type,
        )
        flash("Googleカレンダーの予定を更新しました。", "success")

    return _detail_redirect(application)


def _delete_application_calendar_event(application_id, event_type):
    application = db.get_or_404(Application, application_id)
    _log_route_start("delete", event_type)
    form = DeleteCalendarEventForm()
    if not form.validate_on_submit():
        flash("Googleカレンダー削除リクエストを確認できませんでした。", "danger")
        return _detail_redirect(application)

    if get_credential_store().get_calendar_credential() is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    sync = get_calendar_sync_service().get_application(application.id, event_type)
    if sync is None:
        flash("Googleカレンダーへ登録済みの予定はありません。", "warning")
        return _detail_redirect(application)

    try:
        _delete_google_event(
            get_calendar_service(),
            event_type,
            sync.external_event_id,
        )
    except CalendarEventNotFoundError as error:
        _log_failure("delete", event_type, error, level=logging.WARNING)
        if _delete_calendar_sync(sync, "delete", event_type):
            flash(
                "Googleカレンダー上の予定はすでに削除されていたため、"
                "同期状態を解除しました。",
                "warning",
            )
    except CalendarServiceError as error:
        _log_failure("delete", event_type, error)
        flash("Googleカレンダーの予定を削除できませんでした。", "danger")
    else:
        if _delete_calendar_sync(sync, "delete", event_type):
            current_app.logger.info(
                "Google Calendar completed operation=delete stage=completed "
                "event_type=%s sync_state_cleared=true",
                event_type,
            )
            flash("Googleカレンダーの予定を削除しました。", "success")

    return _detail_redirect(application)


@bp.route(
    "/applications/<int:application_id>/calendar/bulk-create",
    methods=["POST"],
)
def bulk_create_calendar_events(application_id):
    return _bulk_create_application_calendar_events(application_id)


@bp.route(
    "/applications/<int:application_id>/calendar/bulk-update",
    methods=["POST"],
)
def bulk_update_calendar_events(application_id):
    return _bulk_update_application_calendar_events(application_id)


@bp.route(
    "/applications/<int:application_id>/calendar/bulk-delete",
    methods=["POST"],
)
def bulk_delete_calendar_events(application_id):
    return _bulk_delete_application_calendar_events(application_id)


@bp.route(
    "/applications/<int:application_id>/calendar/create",
    methods=["POST"],
)
def create_calendar_event(application_id):
    return _create_application_calendar_event(
        application_id, CalendarSync.EVENT_INTERVIEW
    )


@bp.route(
    "/applications/<int:application_id>/calendar/update",
    methods=["POST"],
)
def update_calendar_event(application_id):
    return _update_application_calendar_event(
        application_id, CalendarSync.EVENT_INTERVIEW
    )


@bp.route(
    "/applications/<int:application_id>/calendar/delete",
    methods=["POST"],
)
def delete_calendar_event(application_id):
    return _delete_application_calendar_event(
        application_id, CalendarSync.EVENT_INTERVIEW
    )


@bp.route(
    "/applications/<int:application_id>/calendar/es-deadline/create",
    methods=["POST"],
)
def create_es_deadline_calendar_event(application_id):
    return _create_application_calendar_event(
        application_id, CalendarSync.EVENT_ES_DEADLINE
    )


@bp.route(
    "/applications/<int:application_id>/calendar/es-deadline/update",
    methods=["POST"],
)
def update_es_deadline_calendar_event(application_id):
    return _update_application_calendar_event(
        application_id, CalendarSync.EVENT_ES_DEADLINE
    )


@bp.route(
    "/applications/<int:application_id>/calendar/es-deadline/delete",
    methods=["POST"],
)
def delete_es_deadline_calendar_event(application_id):
    return _delete_application_calendar_event(
        application_id, CalendarSync.EVENT_ES_DEADLINE
    )


@bp.route(
    "/applications/<int:application_id>/calendar/web-test/create",
    methods=["POST"],
)
def create_web_test_calendar_event(application_id):
    return _create_application_calendar_event(
        application_id, CalendarSync.EVENT_WEB_TEST_DEADLINE
    )


@bp.route(
    "/applications/<int:application_id>/calendar/web-test/update",
    methods=["POST"],
)
def update_web_test_calendar_event(application_id):
    return _update_application_calendar_event(
        application_id, CalendarSync.EVENT_WEB_TEST_DEADLINE
    )


@bp.route(
    "/applications/<int:application_id>/calendar/web-test/delete",
    methods=["POST"],
)
def delete_web_test_calendar_event(application_id):
    return _delete_application_calendar_event(
        application_id, CalendarSync.EVENT_WEB_TEST_DEADLINE
    )
