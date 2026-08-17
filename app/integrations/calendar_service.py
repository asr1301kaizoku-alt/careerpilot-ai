from dataclasses import dataclass
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.models import CalendarSync, JST

from .credential_store import CredentialStorageError
from .diagnostics import get_http_status


GOOGLE_CALENDAR_ID = "primary"
GOOGLE_CALENDAR_TIME_ZONE = "Asia/Tokyo"
ACTIVE_EVENT_STATUSES = {"confirmed", "tentative"}


@dataclass(frozen=True)
class ApplicationCalendarEventSpec:
    event_type: str
    display_label: str
    datetime_label: str
    datetime_attribute: str
    summary_suffix: str
    duration_minutes: int


APPLICATION_EVENT_SPECS = {
    CalendarSync.EVENT_INTERVIEW: ApplicationCalendarEventSpec(
        event_type=CalendarSync.EVENT_INTERVIEW,
        display_label="面接",
        datetime_label="面接日時",
        datetime_attribute="interview_at",
        summary_suffix="面接",
        duration_minutes=60,
    ),
    CalendarSync.EVENT_ES_DEADLINE: ApplicationCalendarEventSpec(
        event_type=CalendarSync.EVENT_ES_DEADLINE,
        display_label="ES締切",
        datetime_label="ES締切",
        datetime_attribute="es_deadline",
        summary_suffix="ES締切",
        duration_minutes=30,
    ),
    CalendarSync.EVENT_WEB_TEST_DEADLINE: ApplicationCalendarEventSpec(
        event_type=CalendarSync.EVENT_WEB_TEST_DEADLINE,
        display_label="Webテスト期限",
        datetime_label="Webテスト期限",
        datetime_attribute="web_test_deadline",
        summary_suffix="Webテスト期限",
        duration_minutes=30,
    ),
}


class CalendarServiceError(RuntimeError):
    def __init__(self, stage, original_error):
        super().__init__("Google Calendar operation failed.")
        self.stage = stage
        self.original_error = original_error


class CalendarEventNotFoundError(CalendarServiceError):
    pass


class CalendarEventCancelledError(RuntimeError):
    event_status = "cancelled"

    def __init__(self):
        super().__init__("Google Calendar event is cancelled.")


class CalendarEventStatusError(RuntimeError):
    def __init__(self):
        super().__init__("Google Calendar event status is missing or unsupported.")


class GoogleCalendarService:
    def __init__(self, credential_store, client_id, client_secret):
        self.credential_store = credential_store
        self.client_id = client_id
        self.client_secret = client_secret

    def create_calendar_event(self, application, event_type):
        event_body = build_event_payload(application, event_type)
        return self._create_event(event_body)

    def create_checklist_due_event(self, item):
        return self._create_event(build_checklist_due_event_payload(item))

    def create_reviewed_event(self, title, start, end, description):
        return self._create_event(
            build_reviewed_event_payload(title, start, end, description)
        )

    def _create_event(self, event_body):
        credentials = self._current_credentials()

        try:
            calendar = self._build_calendar(credentials)
            created_event = (
                calendar.events()
                .insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body)
                .execute()
            )
        except Exception as error:
            self._raise_operation_error("create", error)

        event_id = created_event.get("id") if created_event else None
        if not event_id:
            raise CalendarServiceError(
                "calendar_event_response",
                ValueError("Google Calendar response did not contain an event ID."),
            )
        return event_id

    def get_calendar_event(self, event_id):
        credentials = self._current_credentials()
        try:
            calendar = self._build_calendar(credentials)
        except Exception as error:
            self._raise_operation_error("get", error)
        return self._get_active_event(calendar, event_id)

    def update_calendar_event(self, application, event_type, event_id):
        event_body = build_event_payload(application, event_type)
        return self._update_event(event_id, event_body)

    def update_checklist_due_event(self, item, event_id):
        return self._update_event(
            event_id,
            build_checklist_due_event_payload(item),
        )

    def _update_event(self, event_id, event_body):
        credentials = self._current_credentials()
        try:
            calendar = self._build_calendar(credentials)
        except Exception as error:
            self._raise_operation_error("update", error)

        self._get_active_event(calendar, event_id)
        try:
            updated_event = (
                calendar.events()
                .patch(
                    calendarId=GOOGLE_CALENDAR_ID,
                    eventId=event_id,
                    body=event_body,
                )
                .execute()
            )
        except Exception as error:
            self._raise_operation_error("update", error)
        self._validate_event_status(updated_event, require_status=False)
        return event_id

    def delete_calendar_event(self, event_id):
        credentials = self._current_credentials()
        try:
            calendar = self._build_calendar(credentials)
        except Exception as error:
            self._raise_operation_error("delete", error)

        self._get_active_event(calendar, event_id)
        try:
            (
                calendar.events()
                .delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id)
                .execute()
            )
        except Exception as error:
            self._raise_operation_error("delete", error)

    def delete_checklist_due_event(self, event_id):
        return self.delete_calendar_event(event_id)

    def create_interview_event(self, application):
        return self.create_calendar_event(
            application,
            CalendarSync.EVENT_INTERVIEW,
        )

    def get_interview_event(self, event_id):
        return self.get_calendar_event(event_id)

    def update_interview_event(self, application, event_id):
        return self.update_calendar_event(
            application,
            CalendarSync.EVENT_INTERVIEW,
            event_id,
        )

    def delete_interview_event(self, event_id):
        return self.delete_calendar_event(event_id)

    def _get_active_event(self, calendar, event_id):
        try:
            event = (
                calendar.events()
                .get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id)
                .execute()
            )
        except Exception as error:
            self._raise_operation_error("get", error)
        self._validate_event_status(event, require_status=True)
        return event

    @staticmethod
    def _validate_event_status(event, require_status):
        status = event.get("status") if isinstance(event, dict) else None
        if status == "cancelled":
            raise CalendarEventNotFoundError(
                "calendar_event_status_check",
                CalendarEventCancelledError(),
            )
        if status is None and not require_status:
            return
        if status not in ACTIVE_EVENT_STATUSES:
            raise CalendarServiceError(
                "calendar_event_status_check",
                CalendarEventStatusError(),
            )

    @staticmethod
    def _build_calendar(credentials):
        return build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

    @staticmethod
    def _raise_operation_error(operation, error):
        stage = f"calendar_event_{operation}"
        if get_http_status(error) in {404, 410}:
            raise CalendarEventNotFoundError(stage, error) from error
        raise CalendarServiceError(stage, error) from error

    def _current_credentials(self):
        record = self.credential_store.get_calendar_credential()
        if record is None:
            raise CalendarServiceError(
                "calendar_authentication",
                RuntimeError("Google credential is not connected."),
            )

        try:
            credentials = self.credential_store.to_google_credentials(
                record,
                self.client_id,
                self.client_secret,
            )
            if credentials.expired:
                if not credentials.refresh_token:
                    raise RuntimeError("Google refresh token is unavailable.")
                credentials.refresh(Request())
                self.credential_store.save_calendar_credential(
                    credentials,
                    email=record.google_account_email,
                )
        except CredentialStorageError as error:
            raise CalendarServiceError(error.stage, error.original_error) from error
        except Exception as error:
            raise CalendarServiceError("credential_refresh", error) from error

        return credentials


def build_event_payload(application, event_type):
    try:
        spec = APPLICATION_EVENT_SPECS[event_type]
    except KeyError as error:
        raise ValueError("Unsupported application calendar event type.") from error

    start = getattr(application, spec.datetime_attribute)
    if start is None:
        raise ValueError(f"{spec.datetime_attribute} is required.")
    start = _as_jst(start)
    end = start + timedelta(minutes=spec.duration_minutes)

    position = application.position_name or "未設定"
    memo = application.memo or "未設定"
    description_lines = [
        "CareerPilot AIから登録",
        "",
        f"会社名: {application.company_name}",
        f"応募職種: {position}",
        f"現在ステータス: {application.status}",
    ]
    if event_type != CalendarSync.EVENT_INTERVIEW:
        description_lines.append(f"期限種別: {spec.display_label}")
    description_lines.append(f"メモ: {memo}")
    return {
        "summary": f"{application.company_name} {spec.summary_suffix}",
        "description": "\n".join(description_lines),
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
    }


def build_interview_event(application):
    return build_event_payload(application, CalendarSync.EVENT_INTERVIEW)


def build_checklist_due_event_payload(item):
    if item.due_at is None:
        raise ValueError("due_at is required.")

    start = _as_jst(item.due_at)
    end = start + timedelta(minutes=30)
    application = item.application
    position = application.position_name or "未設定"
    completion = "完了" if item.is_completed else "未完了"
    description_lines = [
        "CareerPilot AIから登録",
        "",
        f"会社名: {application.company_name}",
        f"応募職種: {position}",
        f"タスク名: {item.title}",
        f"応募ステータス: {application.status}",
        f"タスク完了状態: {completion}",
        f"期限: {start.strftime('%Y/%m/%d %H:%M')}",
    ]
    return {
        "summary": f"{application.company_name} - {item.title}",
        "description": "\n".join(description_lines),
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
    }


def build_reviewed_event_payload(title, start, end, description):
    normalized_title = str(title or "").strip()
    if not normalized_title:
        raise ValueError("title is required.")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise ValueError("start and end must be datetimes.")
    start = _as_jst(start)
    end = _as_jst(end)
    if end <= start:
        raise ValueError("end must be later than start.")
    return {
        "summary": normalized_title,
        "description": str(description or "").strip(),
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
    }


def _as_jst(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)
