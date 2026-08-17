from flask_wtf import FlaskForm
from wtforms import SubmitField


class DisconnectGoogleForm(FlaskForm):
    submit = SubmitField("連携を解除する")


class DisconnectGmailForm(FlaskForm):
    submit = SubmitField("Gmail連携を解除する")


class CreateCalendarEventForm(FlaskForm):
    submit = SubmitField("Googleカレンダーへ登録")


class BulkCreateCalendarEventsForm(FlaskForm):
    submit = SubmitField("Googleカレンダーへ一括登録")


class BulkUpdateCalendarEventsForm(FlaskForm):
    submit = SubmitField("Googleカレンダーへ一括更新")


class BulkDeleteCalendarEventsForm(FlaskForm):
    submit = SubmitField("Googleカレンダーから一括削除")


class UpdateCalendarEventForm(FlaskForm):
    submit = SubmitField("Googleカレンダーを更新")


class DeleteCalendarEventForm(FlaskForm):
    submit = SubmitField("Googleカレンダーから削除")
