from flask_wtf import FlaskForm
from wtforms import DateTimeLocalField, IntegerField, StringField, SubmitField
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional


CHECKLIST_TITLE_MAX_LENGTH = 150
CHECKLIST_TITLE_TOO_LONG_MESSAGE = "作業名は150文字以内で入力してください。"


def strip_text(value):
    return value.strip() if isinstance(value, str) else value


class ChecklistItemForm(FlaskForm):
    title = StringField(
        "作業名",
        filters=[strip_text],
        validators=[
            DataRequired(message="作業名を入力してください。"),
            Length(
                max=CHECKLIST_TITLE_MAX_LENGTH,
                message=CHECKLIST_TITLE_TOO_LONG_MESSAGE,
            ),
        ],
    )
    due_at = DateTimeLocalField(
        "期限",
        format="%Y-%m-%dT%H:%M",
        validators=[Optional()],
    )
    sort_order = IntegerField(
        "表示順",
        default=0,
        validators=[
            InputRequired(message="表示順を入力してください。"),
            NumberRange(min=0, message="表示順は0以上で入力してください。"),
        ],
    )
    submit = SubmitField("保存する")


class ChecklistActionForm(FlaskForm):
    submit = SubmitField()
