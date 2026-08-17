from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    FieldList,
    Form,
    FormField,
    HiddenField,
    RadioField,
    SelectField,
    StringField,
    SubmitField,
)
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from app.applications.forms import ApplicationForm
from app.checklists.forms import (
    CHECKLIST_TITLE_MAX_LENGTH,
    CHECKLIST_TITLE_TOO_LONG_MESSAGE,
    strip_text,
)

from .analysis_calendar import (
    AI_CALENDAR_CANDIDATE_SPECS,
    MAX_CALENDAR_TITLE_LENGTH,
)


class AnalyzeEmailForm(FlaskForm):
    return_to = HiddenField()
    submit = SubmitField("AIでメールを解析")


class EmailAnalysisApplyForm(ApplicationForm):
    apply_mode = RadioField(
        "反映方法",
        choices=[
            ("new", "新しい応募先として登録"),
            ("existing", "既存の応募先へ反映"),
        ],
        validators=[DataRequired(message="反映方法を選択してください。")],
        default="new",
    )
    application_id = SelectField(
        "反映先の応募先",
        coerce=int,
        choices=[(-1, "未選択")],
        default=-1,
    )
    token = HiddenField(
        validators=[DataRequired(message="AI解析結果を確認できませんでした。")]
    )
    return_to = HiddenField()
    create_default_checklist = BooleanField(
        "標準チェックリストを作成する", default=True
    )
    submit = SubmitField("応募先へ反映")


class EmailChecklistCandidateForm(Form):
    selected = BooleanField("追加する", default=True)
    title = StringField(
        "タイトル",
        filters=[strip_text],
        validators=[
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

    def validate_title(self, field):
        if self.selected.data and not field.data:
            raise ValidationError("タイトルを入力してください。")


class EmailAnalysisChecklistForm(FlaskForm):
    application_id = SelectField(
        "登録先応募先",
        coerce=int,
        choices=[(-1, "応募先を選択してください")],
        default=-1,
    )
    candidates = FieldList(
        FormField(EmailChecklistCandidateForm),
        min_entries=0,
        max_entries=10,
    )
    token = HiddenField(
        validators=[DataRequired(message="AI解析結果を確認できませんでした。")]
    )
    return_to = HiddenField()
    submit = SubmitField("選択した項目を追加")


class EmailCalendarCandidateForm(Form):
    selected = BooleanField("登録する", default=True)
    event_type = HiddenField()
    title = StringField(
        "タイトル",
        filters=[strip_text],
        validators=[
            Length(
                max=MAX_CALENDAR_TITLE_LENGTH,
                message=(
                    f"タイトルは{MAX_CALENDAR_TITLE_LENGTH}文字以内で"
                    "入力してください。"
                ),
            ),
        ],
    )
    start_at = DateTimeLocalField(
        "開始",
        format="%Y-%m-%dT%H:%M",
        validators=[Optional()],
    )
    end_at = DateTimeLocalField(
        "終了",
        format="%Y-%m-%dT%H:%M",
        validators=[Optional()],
    )

    def validate_event_type(self, field):
        if field.data not in AI_CALENDAR_CANDIDATE_SPECS:
            raise ValidationError("予定の種類を確認できませんでした。")

    def validate_title(self, field):
        if self.selected.data and not field.data:
            raise ValidationError("タイトルを入力してください。")

    def validate_start_at(self, field):
        if self.selected.data and field.data is None:
            raise ValidationError("開始日時を入力してください。")

    def validate_end_at(self, field):
        if not self.selected.data:
            return
        if field.data is None:
            raise ValidationError("終了日時を入力してください。")
        if self.start_at.data is not None and field.data <= self.start_at.data:
            raise ValidationError("終了日時は開始日時より後にしてください。")


class EmailAnalysisCalendarForm(FlaskForm):
    application_id = SelectField(
        "紐付け先の応募先（任意）",
        coerce=int,
        choices=[(-1, "応募先に紐付けず登録")],
        default=-1,
    )
    candidates = FieldList(
        FormField(EmailCalendarCandidateForm),
        min_entries=0,
        max_entries=4,
    )
    token = HiddenField(
        validators=[DataRequired(message="AI解析結果を確認できませんでした。")]
    )
    return_to = HiddenField()
    submit = SubmitField("選択した予定をGoogleカレンダーへ登録")


class EmailCalendarStatusForm(FlaskForm):
    token = HiddenField(
        validators=[DataRequired(message="AI解析結果を確認できませんでした。")]
    )
    return_to = HiddenField()
    submit = SubmitField("Google側の登録状態を確認")
