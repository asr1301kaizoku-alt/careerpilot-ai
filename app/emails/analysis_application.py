from datetime import datetime

from sqlalchemy import func

from app.checklists.services import build_default_checklist
from app.extensions import db
from app.models import Application, JST


AI_APPLICATION_FIELDS = (
    "company_name",
    "position_name",
    "status",
    "es_deadline",
    "web_test_deadline",
    "interview_at",
    "priority",
    "memo",
)


def build_application_choices():
    rows = db.session.execute(
        db.select(
            Application.id,
            Application.company_name,
            Application.position_name,
        ).order_by(Application.company_name.asc(), Application.id.asc())
    ).all()
    choices = [(-1, "応募先を選択してください")]
    for application_id, company_name, position_name in rows:
        label = company_name
        if position_name:
            label = f"{label}（{position_name}）"
        choices.append((application_id, label))
    return choices


def build_ai_candidate_form_data(result):
    return {
        "apply_mode": "new",
        "application_id": -1,
        "company_name": result.company_name or "",
        "position_name": "",
        "status": "応募予定",
        "es_deadline": ai_datetime_to_jst_naive(result.es_deadline),
        "web_test_deadline": ai_datetime_to_jst_naive(
            result.web_test_deadline
        ),
        "interview_at": ai_datetime_to_jst_naive(
            result.interview_datetime
        ),
        "priority": 3,
        "memo": build_ai_memo_candidate(result),
        "create_default_checklist": True,
    }


def build_existing_application_form_data(application):
    return {
        "apply_mode": "existing",
        "application_id": application.id,
        "company_name": application.company_name,
        "position_name": application.position_name or "",
        "status": application.status,
        "es_deadline": application.es_deadline,
        "web_test_deadline": application.web_test_deadline,
        "interview_at": application.interview_at,
        "priority": application.priority,
        "memo": application.memo or "",
        "create_default_checklist": False,
    }


def ai_datetime_to_jst_naive(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(JST).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return None


def ai_datetime_reference(result, field):
    parsed = ai_datetime_to_jst_naive(getattr(result, field, None))
    if parsed is not None:
        return parsed.strftime("%Y/%m/%d %H:%M")
    return getattr(result, f"{field}_text", None) or "なし"


def build_ai_memo_candidate(result):
    sections = [result.summary.strip()]
    if result.important_notes:
        notes = "\n".join(f"- {note}" for note in result.important_notes)
        sections.append(f"重要事項\n{notes}")
    return "\n\n".join(section for section in sections if section)


def populate_application_from_ai_form(form, application):
    for field_name in AI_APPLICATION_FIELDS:
        value = getattr(form, field_name).data
        if field_name in {"company_name", "position_name"}:
            value = str(value or "").strip() or None
        setattr(application, field_name, value)
    return application


def create_application_from_ai_form(form):
    application = populate_application_from_ai_form(form, Application())
    db.session.add(application)
    if form.create_default_checklist.data:
        application.checklist_items.extend(build_default_checklist())
    return application


def duplicate_company_exists(company_name):
    normalized = str(company_name or "").strip()
    if not normalized:
        return False
    statement = db.select(Application.id).where(
        func.trim(func.lower(Application.company_name)) == normalized.lower()
    )
    return db.session.scalar(statement) is not None
