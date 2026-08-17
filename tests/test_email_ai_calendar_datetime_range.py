from dataclasses import replace
from datetime import datetime

import pytest

from app.emails.analysis_calendar import build_calendar_candidate_data
from app.services.email_ai_service import (
    EMAIL_ANALYSIS_SCHEMA,
    SYSTEM_INSTRUCTION,
    EmailAIValidationError,
    validate_analysis_payload,
)


def payload(**overrides):
    values = {
        "company_name": "株式会社レンジ",
        "mail_category": "event",
        "es_deadline": None,
        "web_test_deadline": None,
        "interview_datetime": None,
        "event_datetime": "2026-08-22T10:00:00+09:00",
        "event_start_datetime": "2026-08-22T10:00:00+09:00",
        "event_end_datetime": "2026-08-22T17:00:00+09:00",
        "es_deadline_text": None,
        "web_test_deadline_text": None,
        "interview_datetime_text": None,
        "event_datetime_text": None,
        "action_items": [],
        "important_notes": [],
        "summary": "イベント案内です。",
        "confidence": "high",
        "evidence": {
            "company_name": "株式会社レンジ",
            "es_deadline": None,
            "web_test_deadline": None,
            "interview_datetime": None,
            "event_datetime": "2026年8月22日10:00～17:00",
        },
    }
    values.update(overrides)
    return values


def general_candidate(**overrides):
    result = validate_analysis_payload(payload(**overrides))
    return build_calendar_candidate_data(result)[0]


def test_explicit_10_to_17_range_is_used_for_calendar_candidate():
    candidate = general_candidate()

    assert candidate["start_at"] == datetime(2026, 8, 22, 10, 0)
    assert candidate["end_at"] == datetime(2026, 8, 22, 17, 0)


def test_explicit_1330_to_1500_range_is_used_for_calendar_candidate():
    candidate = general_candidate(
        event_datetime="2026-08-22T13:30:00+09:00",
        event_start_datetime="2026-08-22T13:30:00+09:00",
        event_end_datetime="2026-08-22T15:00:00+09:00",
    )

    assert candidate["start_at"] == datetime(2026, 8, 22, 13, 30)
    assert candidate["end_at"] == datetime(2026, 8, 22, 15, 0)


def test_missing_explicit_end_uses_general_event_default_only_in_candidate():
    result = validate_analysis_payload(payload(event_end_datetime=None))
    candidate = build_calendar_candidate_data(result)[0]

    assert result.event_end_datetime is None
    assert candidate["end_at"] == datetime(2026, 8, 22, 11, 0)


def test_interview_without_end_keeps_existing_sixty_minute_default():
    result = validate_analysis_payload(
        payload(
            interview_datetime="2026-08-22T14:00:00+09:00",
            event_datetime=None,
            event_start_datetime=None,
            event_end_datetime=None,
        )
    )
    candidate = build_calendar_candidate_data(result)[0]

    assert candidate["event_type"] == "interview_datetime"
    assert candidate["end_at"] - candidate["start_at"].replace() == (
        datetime(2026, 8, 22, 15, 0) - datetime(2026, 8, 22, 14, 0)
    )


def test_event_range_can_cross_midnight():
    candidate = general_candidate(
        event_datetime="2026-08-22T22:00:00+09:00",
        event_start_datetime="2026-08-22T22:00:00+09:00",
        event_end_datetime="2026-08-23T01:00:00+09:00",
    )

    assert candidate["start_at"] == datetime(2026, 8, 22, 22, 0)
    assert candidate["end_at"] == datetime(2026, 8, 23, 1, 0)


def test_schema_and_instruction_require_null_instead_of_inferred_end():
    assert "event_start_datetime" in EMAIL_ANALYSIS_SCHEMA["required"]
    assert "event_end_datetime" in EMAIL_ANALYSIS_SCHEMA["required"]
    assert "event_end_datetimeは必ずnull" in SYSTEM_INSTRUCTION
    assert "1時間後等を\n推測しない" in SYSTEM_INSTRUCTION


@pytest.mark.parametrize(
    "end_value",
    ["not-an-iso-datetime", "2026-08-22T17:00:00"],
)
def test_invalid_or_timezone_naive_explicit_end_is_rejected(end_value):
    with pytest.raises(EmailAIValidationError):
        validate_analysis_payload(payload(event_end_datetime=end_value))


def test_end_not_after_start_is_rejected_before_calendar_registration():
    with pytest.raises(EmailAIValidationError):
        validate_analysis_payload(
            payload(event_end_datetime="2026-08-22T10:00:00+09:00")
        )


def test_evidence_text_is_never_used_as_a_calendar_datetime():
    result = validate_analysis_payload(
        payload(
            event_datetime=None,
            event_start_datetime=None,
            event_end_datetime=None,
            event_datetime_text="2026年8月22日10:00～17:00",
        )
    )

    assert build_calendar_candidate_data(result) == []


def test_malformed_explicit_end_on_internal_result_is_not_defaulted():
    result = validate_analysis_payload(payload())
    malformed = replace(result, event_end_datetime="2026-08-22T17:00:00")

    assert build_calendar_candidate_data(malformed) == []


def test_event_range_display_omits_nonexistent_end():
    result = validate_analysis_payload(payload(event_end_datetime=None))

    assert result.event_datetime_range_display() == "2026/08/22 10:00"
