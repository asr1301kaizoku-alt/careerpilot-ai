from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional, URL

from app.models import STATUS_CHOICES
from app.applications.query_helpers import (
    DEADLINE_CHOICES,
    DEFAULT_SORT,
    SORT_CHOICES,
)


class ApplicationForm(FlaskForm):
    company_name = StringField(
        "会社名", validators=[DataRequired(message="会社名を入力してください。"), Length(max=100)]
    )
    position_name = StringField(
        "職種・コース名", validators=[Optional(), Length(max=150)]
    )
    application_url = StringField(
        "応募ページURL",
        validators=[
            Optional(),
            Length(max=500),
            URL(require_tld=False, message="正しいURLを入力してください。"),
        ],
    )
    application_source = StringField(
        "応募経路", validators=[Optional(), Length(max=100)]
    )
    status = SelectField(
        "応募ステータス",
        choices=[(status, status) for status in STATUS_CHOICES],
        validators=[DataRequired()],
    )
    es_deadline = DateTimeLocalField(
        "ES締切", format="%Y-%m-%dT%H:%M", validators=[Optional()]
    )
    web_test_deadline = DateTimeLocalField(
        "Webテスト期限", format="%Y-%m-%dT%H:%M", validators=[Optional()]
    )
    interview_at = DateTimeLocalField(
        "面接日時", format="%Y-%m-%dT%H:%M", validators=[Optional()]
    )
    interview_format = StringField(
        "面接形式", validators=[Optional(), Length(max=50)]
    )
    priority = IntegerField(
        "志望度",
        default=3,
        validators=[
            DataRequired(message="志望度を入力してください。"),
            NumberRange(min=1, max=5, message="志望度は1〜5で入力してください。"),
        ],
    )
    memo = TextAreaField("メモ", validators=[Optional()])
    submit = SubmitField("保存する")


class ApplicationCreateForm(ApplicationForm):
    create_default_checklist = BooleanField(
        "標準チェックリストを作成する", default=True
    )


class ApplicationSearchForm(FlaskForm):
    class Meta:
        csrf = False

    q = StringField("キーワード")
    status = SelectField(
        "ステータス",
        choices=[("", "すべて")] + [(status, status) for status in STATUS_CHOICES],
    )
    priority = SelectField(
        "志望度",
        choices=[
            ("", "すべて"),
            ("1", "1以上"),
            ("2", "2以上"),
            ("3", "3以上"),
            ("4", "4以上"),
            ("5", "5"),
        ],
    )
    deadline = SelectField(
        "締切状態",
        choices=[("", "すべて")] + list(DEADLINE_CHOICES.items()),
    )
    sort = SelectField(
        "並び替え",
        choices=list(SORT_CHOICES.items()),
        default=DEFAULT_SORT,
    )
    submit = SubmitField("検索する")


class DeleteForm(FlaskForm):
    submit = SubmitField("削除する")
