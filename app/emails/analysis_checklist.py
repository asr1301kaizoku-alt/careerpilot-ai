import re
import unicodedata

from sqlalchemy import func

from app.extensions import db
from app.models import ChecklistItem

from .analysis_application import ai_datetime_to_jst_naive


_ACTION_SUFFIX = r"(?:する|してください|して下さい|すること)?"
_ES_SUBMISSION_PATTERN = re.compile(
    rf"(?:es|エントリーシート)(?:を)?(?:提出|送信){_ACTION_SUFFIX}"
)
_WEB_TEST_PATTERN = re.compile(
    rf"(?:webテスト|ウェブテスト|適性検査|spi)(?:を)?(?:受験|受検){_ACTION_SUFFIX}"
)


def build_checklist_candidate_data(result):
    return [
        {
            "selected": True,
            "title": title,
            "due_at": checklist_due_candidate(title, result),
        }
        for title in result.action_items
    ]


def checklist_due_candidate(title, result):
    normalized = _normalize_action_title(title)
    if _ES_SUBMISSION_PATTERN.fullmatch(normalized):
        return ai_datetime_to_jst_naive(result.es_deadline)
    if _WEB_TEST_PATTERN.fullmatch(normalized):
        return ai_datetime_to_jst_naive(result.web_test_deadline)
    return None


def duplicate_candidate_flags(form, application_id):
    existing = incomplete_title_keys(application_id)
    return [
        bool(candidate.title.data)
        and _title_key(candidate.title.data) in existing
        for candidate in form.candidates.entries
    ]


def incomplete_title_keys(application_id):
    titles = db.session.scalars(
        db.select(ChecklistItem.title).where(
            ChecklistItem.application_id == application_id,
            ChecklistItem.is_completed.is_(False),
        )
    ).all()
    return {_title_key(title) for title in titles}


def build_selected_checklist_items(form, application_id):
    current_max = db.session.scalar(
        db.select(func.max(ChecklistItem.sort_order)).where(
            ChecklistItem.application_id == application_id
        )
    )
    next_sort_order = 0 if current_max is None else current_max + 1
    items = []
    for candidate in form.candidates.entries:
        if not candidate.selected.data:
            continue
        items.append(
            ChecklistItem(
                application_id=application_id,
                title=candidate.title.data,
                due_at=candidate.due_at.data,
                is_completed=False,
                sort_order=next_sort_order + len(items),
            )
        )
    return items


def _normalize_action_title(title):
    normalized = unicodedata.normalize("NFKC", str(title or "")).casefold()
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.rstrip("。.!！")


def _title_key(title):
    return unicodedata.normalize("NFKC", str(title or "").strip()).casefold()
