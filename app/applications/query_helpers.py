from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import case, func, or_
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import Application, STATUS_CHOICES


DEADLINE_CHOICES = {
    "overdue": "期限切れあり",
    "3days": "3日以内",
    "7days": "7日以内",
    "14days": "14日以内",
    "none": "締切なし",
}

SORT_CHOICES = {
    "updated_desc": "更新日時が新しい順",
    "updated_asc": "更新日時が古い順",
    "company_asc": "会社名の昇順",
    "company_desc": "会社名の降順",
    "priority_desc": "志望度が高い順",
    "priority_asc": "志望度が低い順",
    "deadline_asc": "締切が近い順",
    "interview_asc": "面接日時が近い順",
}

DEFAULT_SORT = "updated_desc"
VALID_PRIORITIES = {"1", "2", "3", "4", "5"}


@dataclass(frozen=True)
class ApplicationSearchCriteria:
    q: str = ""
    status: str = ""
    priority: str = ""
    deadline: str = ""
    sort: str = DEFAULT_SORT

    @classmethod
    def from_args(cls, args):
        keyword = args.get("q", "").strip()
        status = args.get("status", "")
        priority = args.get("priority", "")
        deadline = args.get("deadline", "")
        sort = args.get("sort", DEFAULT_SORT)
        return cls(
            q=keyword,
            status=status if status in STATUS_CHOICES else "",
            priority=priority if priority in VALID_PRIORITIES else "",
            deadline=deadline if deadline in DEADLINE_CHOICES else "",
            sort=sort if sort in SORT_CHOICES else DEFAULT_SORT,
        )

    @property
    def has_filters(self):
        return any((self.q, self.status, self.priority, self.deadline))

    def as_form_data(self):
        return {
            "q": self.q,
            "status": self.status,
            "priority": self.priority,
            "deadline": self.deadline,
            "sort": self.sort,
        }


def relevant_deadline_expression(now):
    es_deadline = Application.es_deadline
    web_deadline = Application.web_test_deadline
    both_upcoming = (es_deadline >= now) & (web_deadline >= now)
    both_set = es_deadline.is_not(None) & web_deadline.is_not(None)

    return case(
        (
            both_upcoming,
            case((es_deadline <= web_deadline, es_deadline), else_=web_deadline),
        ),
        (es_deadline >= now, es_deadline),
        (web_deadline >= now, web_deadline),
        (
            both_set,
            case((es_deadline >= web_deadline, es_deadline), else_=web_deadline),
        ),
        (es_deadline.is_not(None), es_deadline),
        else_=web_deadline,
    )


def relevant_deadline(application, now):
    deadlines = [
        deadline
        for deadline in (application.es_deadline, application.web_test_deadline)
        if deadline is not None
    ]
    if not deadlines:
        return None
    upcoming = [deadline for deadline in deadlines if deadline >= now]
    return min(upcoming) if upcoming else max(deadlines)


def deadline_css_class(deadline, now):
    if deadline is None:
        return ""
    remaining = deadline - now
    if remaining.total_seconds() < 0 or remaining <= timedelta(days=3):
        return "text-danger fw-bold"
    if remaining <= timedelta(days=7):
        return "text-warning-emphasis fw-bold"
    return ""


def _escaped_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_applications_query(criteria, now):
    statement = db.select(Application).options(
        selectinload(Application.checklist_items)
    )

    if criteria.q:
        pattern = f"%{_escaped_like(criteria.q)}%"
        statement = statement.where(
            or_(
                Application.company_name.ilike(pattern, escape="\\"),
                Application.position_name.ilike(pattern, escape="\\"),
            )
        )
    if criteria.status:
        statement = statement.where(Application.status == criteria.status)
    if criteria.priority:
        statement = statement.where(Application.priority >= int(criteria.priority))

    deadline = relevant_deadline_expression(now)
    if criteria.deadline == "overdue":
        statement = statement.where(deadline < now)
    elif criteria.deadline in {"3days", "7days", "14days"}:
        days = int(criteria.deadline.removesuffix("days"))
        statement = statement.where(
            deadline >= now,
            deadline <= now + timedelta(days=days),
        )
    elif criteria.deadline == "none":
        statement = statement.where(
            Application.es_deadline.is_(None),
            Application.web_test_deadline.is_(None),
        )

    null_deadline_last = case((deadline.is_(None), 1), else_=0)
    null_interview_last = case((Application.interview_at.is_(None), 1), else_=0)
    company_name = func.lower(Application.company_name)
    orderings = {
        "updated_desc": (Application.updated_at.desc(), Application.id.asc()),
        "updated_asc": (Application.updated_at.asc(), Application.id.asc()),
        "company_asc": (company_name.asc(), Application.id.asc()),
        "company_desc": (company_name.desc(), Application.id.asc()),
        "priority_desc": (Application.priority.desc(), Application.id.asc()),
        "priority_asc": (Application.priority.asc(), Application.id.asc()),
        "deadline_asc": (
            null_deadline_last.asc(),
            deadline.asc(),
            Application.id.asc(),
        ),
        "interview_asc": (
            null_interview_last.asc(),
            Application.interview_at.asc(),
            Application.id.asc(),
        ),
    }
    return statement.order_by(*orderings[criteria.sort])
