from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.integrations.calendar_service import (
    APPLICATION_EVENT_SPECS,
    CalendarServiceError,
)
from app.integrations.calendar_sync_service import CalendarSyncStorageError
from app.models import CalendarSync

from .analysis_application import ai_datetime_to_jst_naive
from .calendar_registration_service import (
    EmailCalendarRegistrationStorageError,
)


MAX_CALENDAR_TITLE_LENGTH = 200
MAX_CALENDAR_DESCRIPTION_LENGTH = 4_000


@dataclass(frozen=True)
class AICalendarCandidateSpec:
    event_type: str
    label: str
    result_field: str
    company_title_suffix: str
    fallback_title: str
    duration_minutes: int
    calendar_sync_event_type: str | None = None
    application_datetime_attribute: str | None = None


AI_CALENDAR_CANDIDATE_SPECS = {
    "es_deadline": AICalendarCandidateSpec(
        event_type="es_deadline",
        label="ES締切",
        result_field="es_deadline",
        company_title_suffix="ES締切",
        fallback_title="ES締切",
        duration_minutes=30,
        calendar_sync_event_type=CalendarSync.EVENT_ES_DEADLINE,
        application_datetime_attribute="es_deadline",
    ),
    "web_test_deadline": AICalendarCandidateSpec(
        event_type="web_test_deadline",
        label="Webテスト期限",
        result_field="web_test_deadline",
        company_title_suffix="Webテスト期限",
        fallback_title="Webテスト期限",
        duration_minutes=30,
        calendar_sync_event_type=CalendarSync.EVENT_WEB_TEST_DEADLINE,
        application_datetime_attribute="web_test_deadline",
    ),
    "interview_datetime": AICalendarCandidateSpec(
        event_type="interview_datetime",
        label="面接",
        result_field="interview_datetime",
        company_title_suffix="面接",
        fallback_title="面接",
        duration_minutes=60,
        calendar_sync_event_type=CalendarSync.EVENT_INTERVIEW,
        application_datetime_attribute="interview_at",
    ),
    "event_datetime": AICalendarCandidateSpec(
        event_type="event_datetime",
        label="イベント",
        result_field="event_start_datetime",
        company_title_suffix="イベント",
        fallback_title="就活イベント",
        duration_minutes=60,
    ),
}


@dataclass(frozen=True)
class ReviewedCalendarCandidate:
    event_type: str
    title: str
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class CalendarApplyFailure:
    event_type: str
    error: Exception


@dataclass
class CalendarApplyResult:
    created_event_types: list[str] = field(default_factory=list)
    duplicate_event_types: list[str] = field(default_factory=list)
    registered_event_types: list[str] = field(default_factory=list)
    independent_event_types: list[str] = field(default_factory=list)
    synced_event_types: list[str] = field(default_factory=list)
    failures: list[CalendarApplyFailure] = field(default_factory=list)
    sync_failures: list[CalendarApplyFailure] = field(default_factory=list)
    tracking_failures: list[CalendarApplyFailure] = field(default_factory=list)

    @property
    def created_count(self):
        return len(self.created_event_types)

    @property
    def duplicate_count(self):
        return len(self.duplicate_event_types) + len(
            self.registered_event_types
        )

    @property
    def failed_count(self):
        return len(self.failures)

    @property
    def sync_failure_count(self):
        return len(self.sync_failures)

    @property
    def tracking_failure_count(self):
        return len(self.tracking_failures)


class EmailAnalysisCalendarApplyService:
    """Create reviewed AI candidates without persisting the AI result."""

    def __init__(
        self,
        calendar_service,
        calendar_sync_service,
        registration_service=None,
        message_id=None,
        calendar_credential=None,
    ):
        self.calendar_service = calendar_service
        self.calendar_sync_service = calendar_sync_service
        self.registration_service = registration_service
        self.message_id = message_id
        self.calendar_credential = calendar_credential

    def apply(self, candidates, analysis_result, application=None):
        result = CalendarApplyResult()
        description = build_ai_calendar_description(analysis_result)

        for candidate in candidates:
            spec = AI_CALENDAR_CANDIDATE_SPECS[candidate.event_type]
            sync_event_type = spec.calendar_sync_event_type
            if self.registration_service is not None:
                existing_registration = self.registration_service.get(
                    self.message_id,
                    candidate.event_type,
                    self.calendar_credential,
                )
                if existing_registration is not None:
                    result.registered_event_types.append(candidate.event_type)
                    continue
            if application is not None and sync_event_type is not None:
                existing = self.calendar_sync_service.get_application(
                    application.id,
                    sync_event_type,
                )
                if existing is not None:
                    result.duplicate_event_types.append(candidate.event_type)
                    continue

            try:
                event_id = self.calendar_service.create_reviewed_event(
                    candidate.title,
                    candidate.start_at,
                    candidate.end_at,
                    description,
                )
            except CalendarServiceError as error:
                # A failed Google candidate never stops the remaining reviewed
                # candidates.
                result.failures.append(
                    CalendarApplyFailure(candidate.event_type, error)
                )
                continue

            if self.registration_service is not None:
                try:
                    _, was_created = self.registration_service.create(
                        self.message_id,
                        candidate.event_type,
                        self.calendar_credential,
                        event_id,
                    )
                    if not was_created:
                        raise EmailCalendarRegistrationStorageError(
                            "duplicate_after_google_create",
                            RuntimeError(
                                "Registration appeared after duplicate check."
                            ),
                        )
                except EmailCalendarRegistrationStorageError as error:
                    # Google may already contain the event. Never retry or
                    # claim a complete success when durable tracking failed.
                    result.tracking_failures.append(
                        CalendarApplyFailure(candidate.event_type, error)
                    )
                    continue

            result.created_event_types.append(candidate.event_type)
            if not application_calendar_sync_is_safe(
                application,
                candidate,
            ):
                result.independent_event_types.append(candidate.event_type)
                continue

            try:
                self.calendar_sync_service.create_application(
                    application.id,
                    sync_event_type,
                    event_id,
                )
            except CalendarSyncStorageError as error:
                # The Google event already exists. Do not delete it as a
                # rollback; report the storage failure separately.
                result.sync_failures.append(
                    CalendarApplyFailure(candidate.event_type, error)
                )
            else:
                result.synced_event_types.append(candidate.event_type)

        return result


def build_calendar_candidate_data(result):
    candidates = []
    for spec in AI_CALENDAR_CANDIDATE_SPECS.values():
        raw_start = getattr(result, spec.result_field, None)
        if spec.event_type == "event_datetime" and not raw_start:
            raw_start = getattr(result, "event_datetime", None)
        start_at = ai_datetime_to_jst_naive(raw_start)
        if start_at is None:
            continue
        end_at = None
        if spec.event_type == "event_datetime":
            raw_end = getattr(result, "event_end_datetime", None)
            if raw_end:
                end_at = ai_datetime_to_jst_naive(raw_end)
                if end_at is None or end_at <= start_at:
                    # A malformed explicit end must not be silently replaced
                    # by an invented duration.
                    continue
        if end_at is None:
            end_at = start_at + timedelta(minutes=spec.duration_minutes)
        candidates.append(
            {
                "selected": True,
                "event_type": spec.event_type,
                "title": build_calendar_candidate_title(
                    result.company_name,
                    spec,
                ),
                "start_at": start_at,
                "end_at": end_at,
            }
        )
    return candidates


def build_calendar_candidate_title(company_name, spec):
    company = str(company_name or "").strip()
    title = (
        f"{company} {spec.company_title_suffix}"
        if company
        else spec.fallback_title
    )
    if len(title) <= MAX_CALENDAR_TITLE_LENGTH:
        return title
    suffix = f"… {spec.company_title_suffix}"
    return title[: MAX_CALENDAR_TITLE_LENGTH - len(suffix)].rstrip() + suffix


def build_datetime_text_references(result):
    references = []
    for spec in AI_CALENDAR_CANDIDATE_SPECS.values():
        if getattr(result, spec.result_field, None):
            continue
        text_field = (
            "event_datetime_text"
            if spec.event_type == "event_datetime"
            else f"{spec.result_field}_text"
        )
        value = getattr(result, text_field, None)
        if value:
            references.append((spec.label, value))
    return references


def candidate_evidence(result, event_type):
    spec = AI_CALENDAR_CANDIDATE_SPECS.get(event_type)
    if not spec:
        return None
    evidence_field = (
        "event_datetime"
        if event_type == "event_datetime"
        else spec.result_field
    )
    return result.evidence.get(evidence_field)


def build_reviewed_calendar_candidates(form):
    return [
        ReviewedCalendarCandidate(
            event_type=candidate.event_type.data,
            title=candidate.title.data,
            start_at=candidate.start_at.data,
            end_at=candidate.end_at.data,
        )
        for candidate in form.candidates.entries
        if candidate.selected.data
    ]


def application_calendar_sync_is_safe(application, candidate):
    if application is None:
        return False
    spec = AI_CALENDAR_CANDIDATE_SPECS[candidate.event_type]
    if (
        spec.calendar_sync_event_type is None
        or spec.application_datetime_attribute is None
    ):
        return False
    application_datetime = getattr(
        application,
        spec.application_datetime_attribute,
        None,
    )
    return (
        application_datetime is not None
        and application_datetime.replace(second=0, microsecond=0)
        == candidate.start_at.replace(second=0, microsecond=0)
    )


def candidate_sync_states(
    form,
    application,
    calendar_sync_service,
    registrations=None,
):
    registrations = registrations or {}
    if application is None:
        return [
            {
                "duplicate": False,
                "registered": candidate.event_type.data in registrations,
                "sync_eligible": False,
            }
            for candidate in form.candidates.entries
        ]
    syncs = calendar_sync_service.get_application_syncs(application.id)
    states = []
    for candidate in form.candidates.entries:
        spec = AI_CALENDAR_CANDIDATE_SPECS.get(candidate.event_type.data)
        sync_event_type = spec.calendar_sync_event_type if spec else None
        reviewed = None
        if spec and candidate.start_at.data and candidate.end_at.data:
            reviewed = ReviewedCalendarCandidate(
                event_type=spec.event_type,
                title=candidate.title.data or "",
                start_at=candidate.start_at.data,
                end_at=candidate.end_at.data,
            )
        states.append(
            {
                "duplicate": bool(
                    sync_event_type and sync_event_type in syncs
                ),
                "registered": candidate.event_type.data in registrations,
                "sync_eligible": bool(
                    reviewed
                    and application_calendar_sync_is_safe(
                        application,
                        reviewed,
                    )
                ),
            }
        )
    return states


def build_ai_calendar_description(result):
    lines = [
        "CareerPilot AIからAI解析結果を確認して登録",
        "",
        f"メール種別: {result.mail_category_label}",
    ]
    if result.action_items:
        lines.append("必要な対応:")
        lines.extend(f"- {item}" for item in result.action_items)
    if result.important_notes:
        lines.append("重要事項:")
        lines.extend(f"- {note}" for note in result.important_notes)
    return "\n".join(lines)[:MAX_CALENDAR_DESCRIPTION_LENGTH]


def calendar_candidate_labels():
    return {
        event_type: spec.label
        for event_type, spec in AI_CALENDAR_CANDIDATE_SPECS.items()
    }


def calendar_candidate_types(result):
    return tuple(
        candidate["event_type"]
        for candidate in build_calendar_candidate_data(result)
    )


def candidate_ai_datetime_display(result, event_type):
    if event_type != "event_datetime":
        spec = AI_CALENDAR_CANDIDATE_SPECS.get(event_type)
        if spec is None:
            return "確認できません"
        return result.datetime_display(spec.result_field)
    return result.event_datetime_range_display()


def application_sync_label(event_type):
    spec = AI_CALENDAR_CANDIDATE_SPECS[event_type]
    if spec.calendar_sync_event_type is None:
        return None
    return APPLICATION_EVENT_SPECS[spec.calendar_sync_event_type].display_label
