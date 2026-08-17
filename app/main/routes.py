from datetime import timedelta

from flask import render_template
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Application, ChecklistItem, now_jst_naive

from . import bp


ES_PENDING_STATUSES = ["応募予定", "応募済み", "ES作成中"]
IN_PROGRESS_STATUSES = [
    "応募済み",
    "ES作成中",
    "ES提出済み",
    "Webテスト",
    "面接",
    "最終面接",
]


@bp.route("/")
def dashboard():
    now = now_jst_naive()
    seven_days_later = now + timedelta(days=7)
    applications = Application.query.all()

    upcoming_deadlines = []
    for application in applications:
        for label, deadline in (
            ("ES締切", application.es_deadline),
            ("Webテスト", application.web_test_deadline),
        ):
            if deadline and now <= deadline <= seven_days_later:
                upcoming_deadlines.append((deadline, label, application))
    upcoming_deadlines.sort(key=lambda item: item[0])

    upcoming_interviews = sorted(
        (
            application
            for application in applications
            if application.interview_at and application.interview_at >= now
        ),
        key=lambda application: application.interview_at,
    )

    upcoming_tasks = db.session.scalars(
        db.select(ChecklistItem)
        .options(joinedload(ChecklistItem.application))
        .where(
            ChecklistItem.is_completed.is_(False),
            ChecklistItem.due_at.is_not(None),
        )
        .order_by(ChecklistItem.due_at.asc(), ChecklistItem.created_at.asc())
        .limit(10)
    ).all()
    incomplete_task_count = (
        db.session.scalar(
            db.select(func.count())
            .select_from(ChecklistItem)
            .where(ChecklistItem.is_completed.is_(False))
        )
        or 0
    )

    stats = {
        "total": len(applications),
        "es_pending": sum(
            application.status in ES_PENDING_STATUSES for application in applications
        ),
        "in_progress": sum(
            application.status in IN_PROGRESS_STATUSES for application in applications
        ),
        "interviews": len(upcoming_interviews),
        "offers": sum(application.status == "内定" for application in applications),
        "incomplete_tasks": incomplete_task_count,
    }
    recently_updated = sorted(
        applications, key=lambda application: application.updated_at, reverse=True
    )[:5]

    return render_template(
        "main/dashboard.html",
        stats=stats,
        upcoming_deadlines=upcoming_deadlines,
        upcoming_interviews=upcoming_interviews[:5],
        recently_updated=recently_updated,
        upcoming_tasks=upcoming_tasks,
        now=now,
    )
