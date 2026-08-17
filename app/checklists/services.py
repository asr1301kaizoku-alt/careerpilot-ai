from sqlalchemy import case

from app.models import ChecklistItem


DEFAULT_CHECKLIST_TITLES = [
    "企業研究",
    "ESを作成する",
    "ESを提出する",
    "Webテストを受験する",
    "面接日程を確認する",
    "面接準備をする",
    "面接を受ける",
]


def build_default_checklist():
    return [
        ChecklistItem(title=title, sort_order=index)
        for index, title in enumerate(DEFAULT_CHECKLIST_TITLES)
    ]


def checklist_ordering():
    due_is_missing = case((ChecklistItem.due_at.is_(None), 1), else_=0)
    return (
        ChecklistItem.is_completed.asc(),
        ChecklistItem.sort_order.asc(),
        due_is_missing.asc(),
        ChecklistItem.due_at.asc(),
        ChecklistItem.created_at.asc(),
    )
