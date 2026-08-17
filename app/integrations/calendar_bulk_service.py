from dataclasses import dataclass

from app.models import CalendarSync

from .calendar_service import (
    APPLICATION_EVENT_SPECS,
    CalendarEventNotFoundError,
    CalendarServiceError,
)
from .calendar_sync_service import CalendarSyncStorageError


BULK_APPLICATION_EVENT_TYPES = (
    CalendarSync.EVENT_INTERVIEW,
    CalendarSync.EVENT_ES_DEADLINE,
    CalendarSync.EVENT_WEB_TEST_DEADLINE,
)


def is_bulk_create_candidate(scheduled_at, sync):
    return scheduled_at is not None and sync is None


def is_bulk_update_candidate(scheduled_at, sync):
    return scheduled_at is not None and sync is not None


def is_bulk_delete_candidate(scheduled_at, sync):
    return sync is not None


def _partition_application_events(application, existing_syncs, predicate):
    targets = []
    skipped = []
    for event_type in BULK_APPLICATION_EVENT_TYPES:
        spec = APPLICATION_EVENT_SPECS[event_type]
        scheduled_at = getattr(application, spec.datetime_attribute)
        sync = existing_syncs.get(event_type)
        if predicate(scheduled_at, sync):
            targets.append((scheduled_at, event_type, sync))
        else:
            skipped.append(event_type)
    targets.sort(key=lambda target: (target[0] is None, target[0], target[1]))
    return targets, skipped


@dataclass(frozen=True)
class CalendarBulkCreateFailure:
    event_type: str
    error: CalendarServiceError | CalendarSyncStorageError


@dataclass(frozen=True)
class CalendarBulkCreateResult:
    created_event_types: tuple[str, ...]
    skipped_event_types: tuple[str, ...]
    failures: tuple[CalendarBulkCreateFailure, ...]

    @property
    def created_count(self):
        return len(self.created_event_types)

    @property
    def skipped_count(self):
        return len(self.skipped_event_types)

    @property
    def failed_count(self):
        return len(self.failures)

    @property
    def target_count(self):
        return self.created_count + self.failed_count


@dataclass(frozen=True)
class CalendarBulkUpdateFailure:
    event_type: str
    error: CalendarServiceError | CalendarSyncStorageError


@dataclass(frozen=True)
class CalendarBulkSyncCleared:
    event_type: str
    error: CalendarEventNotFoundError


@dataclass(frozen=True)
class CalendarBulkUpdateResult:
    updated_event_types: tuple[str, ...]
    skipped_event_types: tuple[str, ...]
    sync_cleared: tuple[CalendarBulkSyncCleared, ...]
    failures: tuple[CalendarBulkUpdateFailure, ...]

    @property
    def updated_count(self):
        return len(self.updated_event_types)

    @property
    def skipped_count(self):
        return len(self.skipped_event_types)

    @property
    def sync_cleared_count(self):
        return len(self.sync_cleared)

    @property
    def failed_count(self):
        return len(self.failures)

    @property
    def target_count(self):
        return self.updated_count + self.sync_cleared_count + self.failed_count


@dataclass(frozen=True)
class CalendarBulkDeleteFailure:
    event_type: str
    error: CalendarServiceError | CalendarSyncStorageError


@dataclass(frozen=True)
class CalendarBulkAlreadyDeleted:
    event_type: str
    error: CalendarEventNotFoundError


@dataclass(frozen=True)
class CalendarBulkDeleteResult:
    deleted_event_types: tuple[str, ...]
    already_deleted: tuple[CalendarBulkAlreadyDeleted, ...]
    failures: tuple[CalendarBulkDeleteFailure, ...]

    @property
    def deleted_count(self):
        return len(self.deleted_event_types)

    @property
    def already_deleted_count(self):
        return len(self.already_deleted)

    @property
    def failed_count(self):
        return len(self.failures)

    @property
    def target_count(self):
        return self.deleted_count + self.already_deleted_count + self.failed_count


class CalendarBulkCreateService:
    """Create eligible Application events without rolling back prior successes."""

    def __init__(self, calendar_service, sync_service):
        self.calendar_service = calendar_service
        self.sync_service = sync_service

    def create_application_events(self, application):
        existing_syncs = self.sync_service.get_application_syncs(
            application.id,
            BULK_APPLICATION_EVENT_TYPES,
        )
        targets, skipped = _partition_application_events(
            application,
            existing_syncs,
            is_bulk_create_candidate,
        )

        created = []
        failures = []
        for _, event_type, _ in targets:
            try:
                event_id = self.calendar_service.create_calendar_event(
                    application,
                    event_type,
                )
                self.sync_service.create_application(
                    application.id,
                    event_type,
                    event_id,
                )
            except (CalendarServiceError, CalendarSyncStorageError) as error:
                failures.append(CalendarBulkCreateFailure(event_type, error))
                continue
            created.append(event_type)

        return CalendarBulkCreateResult(
            created_event_types=tuple(created),
            skipped_event_types=tuple(skipped),
            failures=tuple(failures),
        )


class CalendarBulkUpdateService:
    """Update eligible events independently and clear missing remote syncs."""

    def __init__(self, calendar_service, sync_service):
        self.calendar_service = calendar_service
        self.sync_service = sync_service

    def update_application_events(self, application):
        existing_syncs = self.sync_service.get_application_syncs(
            application.id,
            BULK_APPLICATION_EVENT_TYPES,
        )
        targets, skipped = _partition_application_events(
            application,
            existing_syncs,
            is_bulk_update_candidate,
        )

        updated = []
        sync_cleared = []
        failures = []
        for _, event_type, sync in targets:
            try:
                self.calendar_service.update_calendar_event(
                    application,
                    event_type,
                    sync.external_event_id,
                )
            except CalendarEventNotFoundError as error:
                try:
                    self.sync_service.delete(sync)
                except CalendarSyncStorageError as storage_error:
                    failures.append(
                        CalendarBulkUpdateFailure(event_type, storage_error)
                    )
                    continue
                sync_cleared.append(CalendarBulkSyncCleared(event_type, error))
            except CalendarServiceError as error:
                failures.append(CalendarBulkUpdateFailure(event_type, error))
            else:
                updated.append(event_type)

        return CalendarBulkUpdateResult(
            updated_event_types=tuple(updated),
            skipped_event_types=tuple(skipped),
            sync_cleared=tuple(sync_cleared),
            failures=tuple(failures),
        )


class CalendarBulkDeleteService:
    """Delete synced events independently without changing Application data."""

    def __init__(self, calendar_service, sync_service):
        self.calendar_service = calendar_service
        self.sync_service = sync_service

    def delete_application_events(self, application):
        existing_syncs = self.sync_service.get_application_syncs(
            application.id,
            BULK_APPLICATION_EVENT_TYPES,
        )
        targets, _ = _partition_application_events(
            application,
            existing_syncs,
            is_bulk_delete_candidate,
        )

        deleted = []
        already_deleted = []
        failures = []
        for _, event_type, sync in targets:
            missing_error = None
            try:
                self.calendar_service.delete_calendar_event(
                    sync.external_event_id
                )
            except CalendarEventNotFoundError as error:
                missing_error = error
            except CalendarServiceError as error:
                failures.append(CalendarBulkDeleteFailure(event_type, error))
                continue

            try:
                self.sync_service.delete(sync)
            except CalendarSyncStorageError as error:
                failures.append(CalendarBulkDeleteFailure(event_type, error))
                continue

            if missing_error is None:
                deleted.append(event_type)
            else:
                already_deleted.append(
                    CalendarBulkAlreadyDeleted(event_type, missing_error)
                )

        return CalendarBulkDeleteResult(
            deleted_event_types=tuple(deleted),
            already_deleted=tuple(already_deleted),
            failures=tuple(failures),
        )
