from flask import flash, redirect, render_template, url_for

from app.extensions import db
from app.models import Application, ChecklistItem

from . import bp
from .forms import ChecklistActionForm, ChecklistItemForm


@bp.route("/applications/<int:application_id>/checklist/new", methods=["POST"])
def create(application_id):
    application = db.get_or_404(Application, application_id)
    form = ChecklistItemForm()
    if form.validate_on_submit():
        item = ChecklistItem(
            application=application,
            title=form.title.data,
            due_at=form.due_at.data,
            sort_order=form.sort_order.data,
        )
        db.session.add(item)
        db.session.commit()
        flash("チェック項目を追加しました。", "success")
        return redirect(
            url_for("applications.detail", application_id=application.id)
            + "#checklist"
        )

    from app.applications.routes import render_detail

    return render_detail(application, checklist_form=form), 400


@bp.route("/checklist/<int:item_id>/toggle", methods=["POST"])
def toggle(item_id):
    item = db.get_or_404(ChecklistItem, item_id)
    form = ChecklistActionForm()
    if form.validate_on_submit():
        item.toggle()
        db.session.commit()
        flash(
            "チェック項目を完了にしました。"
            if item.is_completed
            else "チェック項目を未完了に戻しました。",
            "success",
        )
    else:
        flash(
            "完了状態を変更できませんでした。画面を再読み込みして、"
            "もう一度お試しください。",
            "danger",
        )
    return redirect(
        url_for("applications.detail", application_id=item.application_id)
        + "#checklist"
    )


@bp.route("/checklist/<int:item_id>/edit", methods=["GET", "POST"])
def edit(item_id):
    item = db.get_or_404(ChecklistItem, item_id)
    form = ChecklistItemForm(obj=item)
    if form.validate_on_submit():
        item.title = form.title.data
        item.due_at = form.due_at.data
        item.sort_order = form.sort_order.data
        db.session.commit()
        flash("チェック項目を更新しました。", "success")
        return redirect(
            url_for("applications.detail", application_id=item.application_id)
            + "#checklist"
        )
    return render_template("checklists/edit.html", item=item, form=form)


@bp.route("/checklist/<int:item_id>/delete", methods=["POST"])
def delete(item_id):
    item = db.get_or_404(ChecklistItem, item_id)
    application_id = item.application_id
    form = ChecklistActionForm()
    if form.validate_on_submit():
        db.session.delete(item)
        db.session.commit()
        flash("チェック項目を削除しました。", "success")
    else:
        flash(
            "チェック項目を削除できませんでした。画面を再読み込みして、"
            "もう一度お試しください。",
            "danger",
        )
    return redirect(
        url_for("applications.detail", application_id=application_id) + "#checklist"
    )
