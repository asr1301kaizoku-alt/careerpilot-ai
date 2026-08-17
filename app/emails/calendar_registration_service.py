import hashlib
from dataclasses import dataclass, field

from app.extensions import db
from app.integrations.calendar_service import (
    CalendarEventNotFoundError,
    CalendarServiceError,
)
from app.models import EmailCalendarRegistration, GoogleCredential


class EmailCalendarRegistrationStorageError(RuntimeError):
    def __init__(self, stage, original_error):
        super().__init__("Email calendar registration storage failed.")
        self.stage = stage
        self.original_error = original_error


@dataclass(frozen=True)
class EmailCalendarRegistrationFailure:
    event_type: str
    error: Exception


@dataclass
class EmailCalendarRegistrationStatusResult:
    active_event_types: list[str] = field(default_factory=list)
    cleared_event_types: list[str] = field(default_factory=list)
    cleared_failures: list[EmailCalendarRegistrationFailure] = field(
        default_factory=list
    )
    failures: list[EmailCalendarRegistrationFailure] = field(
        default_factory=list
    )
    storage_failures: list[EmailCalendarRegistrationFailure] = field(
        default_factory=list
    )


class EmailCalendarRegistrationService:
    """Persist reviewed Gmail-to-Calendar registrations without raw Gmail IDs.

    The digest is only a pseudonymous lookup key. It keeps the Gmail message ID
    and Google account identifier out of this table and out of diagnostics.
    """

    def __init__(self, owner_key):
        self.owner_key = str(owner_key or "").strip()

    def get(self, message_id, event_type, credential):
        return db.session.scalar(
            db.select(EmailCalendarRegistration).where(
                EmailCalendarRegistration.owner_key == self.owner_key,
                EmailCalendarRegistration.provider
                == EmailCalendarRegistration.PROVIDER_GOOGLE,
                EmailCalendarRegistration.connection_key
                == self.connection_key(credential),
                EmailCalendarRegistration.message_key
                == self.message_key(message_id),
                EmailCalendarRegistration.event_type == event_type,
            )
        )

    def get_for_event_types(self, message_id, event_types, credential):
        normalized_types = tuple(dict.fromkeys(event_types))
        if not normalized_types:
            return {}
        records = db.session.scalars(
            db.select(EmailCalendarRegistration).where(
                EmailCalendarRegistration.owner_key == self.owner_key,
                EmailCalendarRegistration.provider
                == EmailCalendarRegistration.PROVIDER_GOOGLE,
                EmailCalendarRegistration.connection_key
                == self.connection_key(credential),
                EmailCalendarRegistration.message_key
                == self.message_key(message_id),
                EmailCalendarRegistration.event_type.in_(normalized_types),
            )
        ).all()
        return {record.event_type: record for record in records}

    def create(self, message_id, event_type, credential, external_event_id):
        existing = self.get(message_id, event_type, credential)
        if existing is not None:
            return existing, False
        record = EmailCalendarRegistration(
            owner_key=self.owner_key,
            provider=EmailCalendarRegistration.PROVIDER_GOOGLE,
            connection_key=self.connection_key(credential),
            message_key=self.message_key(message_id),
            event_type=event_type,
            calendar_id=EmailCalendarRegistration.DEFAULT_CALENDAR_ID,
            external_event_id=external_event_id,
        )
        try:
            db.session.add(record)
            db.session.commit()
        except Exception as error:
            db.session.rollback()
            raise EmailCalendarRegistrationStorageError(
                "db_commit",
                error,
            ) from error
        return record, True

    def delete(self, record):
        try:
            db.session.delete(record)
            db.session.commit()
        except Exception as error:
            db.session.rollback()
            raise EmailCalendarRegistrationStorageError(
                "db_commit",
                error,
            ) from error

    def reconcile_remote(
        self,
        message_id,
        event_types,
        credential,
        calendar_service,
    ):
        result = EmailCalendarRegistrationStatusResult()
        records = self.get_for_event_types(
            message_id,
            event_types,
            credential,
        )
        for event_type, record in records.items():
            try:
                calendar_service.get_calendar_event(record.external_event_id)
            except CalendarEventNotFoundError as error:
                result.cleared_failures.append(
                    EmailCalendarRegistrationFailure(event_type, error)
                )
                try:
                    self.delete(record)
                except EmailCalendarRegistrationStorageError as error:
                    result.storage_failures.append(
                        EmailCalendarRegistrationFailure(event_type, error)
                    )
                else:
                    result.cleared_event_types.append(event_type)
            except CalendarServiceError as error:
                result.failures.append(
                    EmailCalendarRegistrationFailure(event_type, error)
                )
            else:
                result.active_event_types.append(event_type)
        return result

    @staticmethod
    def message_key(message_id):
        return _digest(f"gmail-message:{str(message_id or '').strip()}")

    def connection_key(self, credential):
        account = str(
            getattr(credential, "google_account_email", "") or ""
        ).strip().casefold()
        if not account:
            account = "calendar-account-unavailable"
        return _digest(
            "|".join(
                (
                    self.owner_key,
                    GoogleCredential.PROVIDER_GOOGLE,
                    GoogleCredential.CONNECTION_CALENDAR,
                    account,
                )
            )
        )


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
