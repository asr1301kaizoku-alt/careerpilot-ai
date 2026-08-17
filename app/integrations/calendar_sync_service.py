from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import CalendarSync


class CalendarSyncStorageError(RuntimeError):
    def __init__(self, stage, original_error):
        super().__init__("Calendar sync storage operation failed.")
        self.stage = stage
        self.original_error = original_error


class CalendarSyncAlreadyExistsError(CalendarSyncStorageError):
    pass


class CalendarSyncService:
    """Persist provider event identifiers separately from domain models."""

    @staticmethod
    def get(owner_type, owner_id, event_type, provider):
        owner_column = CalendarSyncService._owner_column(owner_type)
        return db.session.scalar(
            db.select(CalendarSync).where(
                owner_column == owner_id,
                CalendarSync.event_type == event_type,
                CalendarSync.provider == provider,
            )
        )

    def get_application(self, application_id, event_type):
        return self.get(
            CalendarSync.OWNER_APPLICATION,
            application_id,
            event_type,
            CalendarSync.PROVIDER_GOOGLE,
        )

    def get_application_interview(self, application_id):
        return self.get_application(
            application_id,
            CalendarSync.EVENT_INTERVIEW,
        )

    def get_checklist_item(
        self,
        checklist_item_id,
        event_type=CalendarSync.EVENT_CHECKLIST_DUE,
    ):
        return self.get(
            CalendarSync.OWNER_CHECKLIST_ITEM,
            checklist_item_id,
            event_type,
            CalendarSync.PROVIDER_GOOGLE,
        )

    @staticmethod
    def get_application_syncs(application_id, event_types=None):
        statement = db.select(CalendarSync).where(
            CalendarSync.application_id == application_id,
            CalendarSync.provider == CalendarSync.PROVIDER_GOOGLE,
        )
        if event_types:
            statement = statement.where(CalendarSync.event_type.in_(event_types))
        return {
            sync.event_type: sync
            for sync in db.session.scalars(statement).all()
        }

    @staticmethod
    def get_checklist_item_syncs(
        checklist_item_ids,
        event_type=CalendarSync.EVENT_CHECKLIST_DUE,
    ):
        if not checklist_item_ids:
            return {}
        statement = db.select(CalendarSync).where(
            CalendarSync.checklist_item_id.in_(checklist_item_ids),
            CalendarSync.event_type == event_type,
            CalendarSync.provider == CalendarSync.PROVIDER_GOOGLE,
        )
        return {
            sync.checklist_item_id: sync
            for sync in db.session.scalars(statement).all()
        }

    def create(
        self,
        owner_type,
        owner_id,
        event_type,
        provider,
        calendar_id,
        external_event_id,
    ):
        if self.get(owner_type, owner_id, event_type, provider) is not None:
            raise CalendarSyncAlreadyExistsError(
                "calendar_sync_duplicate",
                ValueError("A matching calendar sync already exists."),
            )

        owner_values = self._owner_values(owner_type, owner_id)
        sync = CalendarSync(
            **owner_values,
            event_type=event_type,
            provider=provider,
            calendar_id=calendar_id,
            external_event_id=external_event_id,
        )
        db.session.add(sync)
        try:
            db.session.commit()
        except SQLAlchemyError as error:
            db.session.rollback()
            raise CalendarSyncStorageError("calendar_sync_save", error) from error
        return sync

    def create_application(
        self,
        application_id,
        event_type,
        external_event_id,
        calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
    ):
        return self.create(
            CalendarSync.OWNER_APPLICATION,
            application_id,
            event_type,
            CalendarSync.PROVIDER_GOOGLE,
            calendar_id,
            external_event_id,
        )

    def create_application_interview(
        self,
        application_id,
        external_event_id,
        calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
    ):
        return self.create_application(
            application_id,
            CalendarSync.EVENT_INTERVIEW,
            external_event_id,
            calendar_id,
        )

    def create_checklist_item(
        self,
        checklist_item_id,
        external_event_id,
        event_type=CalendarSync.EVENT_CHECKLIST_DUE,
        calendar_id=CalendarSync.DEFAULT_CALENDAR_ID,
    ):
        return self.create(
            CalendarSync.OWNER_CHECKLIST_ITEM,
            checklist_item_id,
            event_type,
            CalendarSync.PROVIDER_GOOGLE,
            calendar_id,
            external_event_id,
        )

    @staticmethod
    def delete(sync):
        db.session.delete(sync)
        try:
            db.session.commit()
        except SQLAlchemyError as error:
            db.session.rollback()
            raise CalendarSyncStorageError("calendar_sync_delete", error) from error

    @staticmethod
    def _owner_column(owner_type):
        if owner_type == CalendarSync.OWNER_APPLICATION:
            return CalendarSync.application_id
        if owner_type == CalendarSync.OWNER_CHECKLIST_ITEM:
            return CalendarSync.checklist_item_id
        raise ValueError("Unsupported calendar sync owner type.")

    @staticmethod
    def _owner_values(owner_type, owner_id):
        if owner_type == CalendarSync.OWNER_APPLICATION:
            return {"application_id": owner_id}
        if owner_type == CalendarSync.OWNER_CHECKLIST_ITEM:
            return {"checklist_item_id": owner_id}
        raise ValueError("Unsupported calendar sync owner type.")
