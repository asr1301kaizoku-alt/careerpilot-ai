import logging
from datetime import datetime

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.integrations.calendar_service import GoogleCalendarService
from app.integrations.calendar_sync_service import CalendarSyncService
from app.integrations.credential_store import GoogleCredentialStore
from app.integrations.diagnostics import (
    get_http_status,
    log_calendar_failure,
    log_gmail_failure,
)
from app.integrations.gmail_service import GmailServiceError, GoogleGmailService
from app.models import Application, JST
from app.services.email_ai_service import (
    EmailAIService,
    EmailAIServiceError,
    log_email_ai_failure,
    log_email_ai_success,
)

from . import bp
from .analysis_application import (
    ai_datetime_reference,
    build_ai_candidate_form_data,
    build_application_choices,
    build_existing_application_form_data,
    create_application_from_ai_form,
    duplicate_company_exists,
    populate_application_from_ai_form,
)
from .analysis_session_store import gmail_connection_key
from .analysis_calendar import (
    AI_CALENDAR_CANDIDATE_SPECS,
    EmailAnalysisCalendarApplyService,
    application_sync_label,
    build_calendar_candidate_data,
    build_datetime_text_references,
    build_reviewed_calendar_candidates,
    calendar_candidate_labels,
    calendar_candidate_types,
    candidate_ai_datetime_display,
    candidate_evidence,
    candidate_sync_states,
)
from .analysis_checklist import (
    build_checklist_candidate_data,
    build_selected_checklist_items,
    duplicate_candidate_flags,
)
from .cache import make_gmail_list_cache_key
from .calendar_registration_service import (
    EmailCalendarRegistrationService,
)
from .forms import (
    AnalyzeEmailForm,
    EmailAnalysisCalendarForm,
    EmailAnalysisApplyForm,
    EmailAnalysisChecklistForm,
    EmailCalendarStatusForm,
)
from .navigation import (
    EMAIL_LIST_PATH,
    build_email_list_url,
    safe_email_list_return_url,
)
from .pagination import decode_page_history, encode_page_history


MAX_SEARCH_QUERY_CHARS = 500


def get_credential_store():
    return GoogleCredentialStore(current_app.config["OAUTH_OWNER_KEY"])


def get_gmail_service():
    return GoogleGmailService(
        get_credential_store(),
        current_app.config["GOOGLE_CLIENT_ID"],
        current_app.config["GOOGLE_CLIENT_SECRET"],
    )


def get_gmail_list_cache():
    return current_app.extensions["gmail_list_cache"]


def get_email_analysis_apply_store():
    return current_app.extensions["email_analysis_apply_store"]


def get_email_analysis_checklist_store():
    return current_app.extensions["email_analysis_checklist_store"]


def get_email_analysis_calendar_store():
    return current_app.extensions["email_analysis_calendar_store"]


def get_email_analysis_session_store():
    return current_app.extensions["email_analysis_session_store"]


def get_gmail_connection_key(credential):
    return gmail_connection_key(
        current_app.config["OAUTH_OWNER_KEY"],
        credential,
    )


def get_google_calendar_service():
    return GoogleCalendarService(
        get_credential_store(),
        current_app.config["GOOGLE_CLIENT_ID"],
        current_app.config["GOOGLE_CLIENT_SECRET"],
    )


def get_calendar_sync_service():
    return CalendarSyncService()


def get_email_calendar_registration_service():
    return EmailCalendarRegistrationService(
        current_app.config["OAUTH_OWNER_KEY"]
    )


def get_email_ai_service():
    return EmailAIService(
        current_app.config.get("GEMINI_API_KEY", ""),
        current_app.config.get("GEMINI_MODEL", "gemini-3.6-flash"),
        current_app.config.get("GEMINI_TIMEOUT_SECONDS", 30),
    )


def is_email_ai_configured():
    return bool(
        str(current_app.config.get("GEMINI_API_KEY", "")).strip()
        and str(current_app.config.get("GEMINI_MODEL", "")).strip()
    )


def _safe_query(value):
    return str(value or "").strip()[:MAX_SEARCH_QUERY_CHARS]


def _error_context(error):
    status = get_http_status(error.original_error)
    if status in {401, 403} or error.stage in {
        "gmail_authentication",
        "gmail_credential_refresh",
        "credential_refresh",
    }:
        return (
            "Gmailの再認証または権限の確認が必要です。外部連携設定から再度連携してください。",
            True,
        )
    if status == 404:
        return ("指定されたメールがGmail上で見つかりませんでした。", False)
    if status == 429:
        return (
            "Gmail APIの利用が一時的に制限されています。時間をおいて再度お試しください。",
            False,
        )
    if status is not None and 500 <= status <= 599:
        return (
            "Gmail側で一時的な問題が発生しています。時間をおいて再度お試しください。",
            False,
        )
    if error.stage in {
        "gmail_page_token_validation",
        "gmail_message_id_validation",
    }:
        return ("指定されたページまたはメール情報が正しくありません。", False)
    return ("就活メールを取得できませんでした。もう一度お試しください。", False)


def _ai_error_message(error):
    if error.classification == "configuration_missing":
        return "AI解析を利用するにはGemini APIキーの設定が必要です。"
    if error.classification == "rate_limited":
        return "AI解析の利用が一時的に制限されています。時間をおいて再度お試しください。"
    if error.classification == "timeout":
        return "AI解析がタイムアウトしました。時間をおいて再度お試しください。"
    if error.classification == "authentication_or_permission":
        return "Gemini APIの設定または権限を確認してください。"
    if error.classification == "model_not_found_or_unsupported":
        return "指定したGeminiモデルを利用できません。モデル設定を確認してください。"
    if error.classification in {
        "endpoint_mismatch",
        "unsupported_request",
        "unknown_not_found",
    }:
        return "Gemini APIの接続先またはリクエスト設定を確認してください。"
    return "AI解析に失敗しました。もう一度お試しください。"


@bp.get("")
@bp.get("/")
def index():
    credential = get_credential_store().get_gmail_credential()
    query = _safe_query(request.args.get("q"))
    if credential is None:
        return render_template(
            "emails/index.html",
            gmail_credential=None,
            emails=(),
            query=query,
            error_message=None,
            requires_reauthentication=False,
            next_url=None,
            previous_url=None,
            refresh_url=None,
            last_fetched_at=None,
            list_return_url=EMAIL_LIST_PATH,
        )

    page_token = request.args.get("page_token") or None
    history = decode_page_history(request.args.get("history"))
    list_return_url = build_email_list_url(query, page_token, history)
    force_refresh = request.args.get("refresh") == "1"
    cache_key = make_gmail_list_cache_key(
        current_app.config["OAUTH_OWNER_KEY"],
        credential.google_account_email,
        query,
        page_token,
        credential_id=credential.id,
    )
    cache = get_gmail_list_cache()
    cached = None if force_refresh else cache.get(cache_key)
    current_app.logger.info(
        "Gmail list cache operation=list stage=cache_lookup cache_hit=%s",
        cached is not None,
    )

    if cached is not None:
        page = cached.page
        last_fetched_at = cached.fetched_at
    else:
        try:
            page = get_gmail_service().list_messages(
                query=query,
                page_token=page_token,
            )
        except GmailServiceError as error:
            log_gmail_failure(
                current_app.logger,
                "list",
                error.stage,
                error.original_error,
            )
            error_message, requires_reauthentication = _error_context(error)
            return render_template(
                "emails/index.html",
                gmail_credential=credential,
                emails=(),
                query=query,
                error_message=error_message,
                requires_reauthentication=requires_reauthentication,
                next_url=None,
                previous_url=None,
                refresh_url=_build_refresh_url(query, page_token, history),
                last_fetched_at=None,
                list_return_url=list_return_url,
            )
        last_fetched_at = datetime.now(JST)
        cached = cache.set(
            cache_key,
            page,
            fetched_at=last_fetched_at,
        )
        if cached is not None:
            last_fetched_at = cached.fetched_at
        current_app.logger.info(
            "Gmail API completed operation=list stage=completed count=%s",
            len(page.messages),
        )

    common_parameters = {"q": query} if query else {}
    next_url = None
    if page.next_page_token:
        next_history = encode_page_history([*history, page_token or ""])
        next_url = url_for(
            "emails.index",
            **common_parameters,
            page_token=page.next_page_token,
            history=next_history,
        )

    previous_url = None
    if history:
        previous_token = history[-1] or None
        previous_url = url_for(
            "emails.index",
            **common_parameters,
            page_token=previous_token,
            history=encode_page_history(history[:-1]),
        )

    return render_template(
        "emails/index.html",
        gmail_credential=credential,
        emails=page.messages,
        query=query,
        error_message=None,
        requires_reauthentication=False,
        next_url=next_url,
        previous_url=previous_url,
        refresh_url=_build_refresh_url(query, page_token, history),
        last_fetched_at=last_fetched_at,
        list_return_url=list_return_url,
    )


def _build_refresh_url(query, page_token, history):
    parameters = {"refresh": "1"}
    if query:
        parameters["q"] = query
    if page_token:
        parameters["page_token"] = page_token
    if history:
        parameters["history"] = encode_page_history(history)
    return url_for("emails.index", **parameters)


def _analysis_session_entry(token, message_id, gmail_credential):
    if not token or gmail_credential is None:
        return None
    return get_email_analysis_session_store().get(
        token,
        message_id,
        get_gmail_connection_key(gmail_credential),
    )


def _issue_analysis_review_tokens(message_id, session_token, session_entry):
    tokens = {
        "application": None,
        "checklist": None,
        "calendar": None,
    }
    stores = (
        ("application", get_email_analysis_apply_store()),
        ("checklist", get_email_analysis_checklist_store()),
    )
    for purpose, store in stores:
        try:
            token = store.save(
                message_id,
                session_entry.result,
                session_entry.return_to,
                analysis_session_token=session_token,
            )
            if purpose == "checklist" and session_entry.state.application_id:
                store.bind_application(
                    token,
                    message_id,
                    session_entry.state.application_id,
                )
            tokens[purpose] = token
        except (RuntimeError, ValueError):
            current_app.logger.warning(
                "Email AI derived context rejected operation=%s "
                "stage=context_store classification=context_unavailable "
                "success=false",
                purpose,
            )

    if build_calendar_candidate_data(session_entry.result):
        try:
            calendar_store = get_email_analysis_calendar_store()
            calendar_token = calendar_store.save(
                message_id,
                session_entry.result,
                session_entry.return_to,
                analysis_session_token=session_token,
            )
            if session_entry.state.application_id:
                calendar_store.bind_application(
                    calendar_token,
                    message_id,
                    session_entry.state.application_id,
                )
            tokens["calendar"] = calendar_token
        except (RuntimeError, ValueError):
            current_app.logger.warning(
                "Email AI Calendar derived context rejected "
                "operation=calendar stage=context_store "
                "classification=context_unavailable success=false"
            )
    return tokens


def _analysis_session_detail_url(message_id, session_token, return_to):
    parameters = {
        "message_id": message_id,
        "return_to": str(return_to or EMAIL_LIST_PATH),
    }
    if session_token:
        parameters["analysis_session"] = session_token
    return url_for("emails.detail", **parameters)


def _analysis_session_expired_log():
    current_app.logger.info(
        "Email AI analysis session unavailable "
        "operation=email_ai_analysis_session stage=expired "
        "classification=expired_or_invalid success=false"
    )


def _validate_review_analysis_session(entry, message_id):
    session_token = entry.analysis_session_token
    if not session_token:
        return None, None, False
    credential = get_credential_store().get_gmail_credential()
    session_entry = _analysis_session_entry(
        session_token,
        message_id,
        credential,
    )
    return session_entry, credential, True


def _expired_analysis_session_redirect(message_id, return_to):
    _analysis_session_expired_log()
    flash(
        "AI解析結果の有効期限が切れました。もう一度解析してください。",
        "warning",
    )
    return redirect(
        _analysis_session_detail_url(message_id, None, return_to)
    )


@bp.get("/<message_id>")
def detail(message_id):
    return_to = safe_email_list_return_url(request.args.get("return_to"))
    back_link_label = (
        "検索結果へ戻る"
        if return_to != EMAIL_LIST_PATH
        else "就活メール一覧へ戻る"
    )
    credential = get_credential_store().get_gmail_credential()
    analysis_session_token = request.args.get("analysis_session")
    if credential is None:
        flash("就活メールを表示するにはGmail連携が必要です。", "warning")
        return redirect(url_for("integrations.settings"))

    try:
        email = get_gmail_service().get_message(message_id)
    except GmailServiceError as error:
        log_gmail_failure(
            current_app.logger,
            "detail",
            error.stage,
            error.original_error,
        )
        error_message, requires_reauthentication = _error_context(error)
        return render_template(
            "emails/detail.html",
            email=None,
            error_message=error_message,
            requires_reauthentication=requires_reauthentication,
            return_to=return_to,
            back_link_label=back_link_label,
            analyze_form=None,
            ai_available=False,
            ai_result=None,
            ai_error_message=None,
            ai_apply_token=None,
            ai_checklist_token=None,
            ai_calendar_token=None,
            analysis_session_token=None,
            analysis_session_state=None,
        )

    current_app.logger.info(
        "Gmail API completed operation=detail stage=completed count=1"
    )
    ai_result = None
    ai_apply_token = None
    ai_checklist_token = None
    ai_calendar_token = None
    analysis_session_state = None
    if analysis_session_token:
        session_entry = _analysis_session_entry(
            analysis_session_token,
            message_id,
            credential,
        )
        if session_entry is None:
            _analysis_session_expired_log()
            flash(
                "AI解析結果の有効期限が切れました。"
                "もう一度解析してください。",
                "warning",
            )
            analysis_session_token = None
        else:
            tokens = _issue_analysis_review_tokens(
                message_id,
                analysis_session_token,
                session_entry,
            )
            ai_result = session_entry.result
            analysis_session_state = session_entry.state
            ai_apply_token = tokens["application"]
            ai_checklist_token = tokens["checklist"]
            ai_calendar_token = tokens["calendar"]
            current_app.logger.info(
                "Email AI analysis session reused "
                "operation=email_ai_analysis_session stage=reused "
                "success=true"
            )
    analyze_form = AnalyzeEmailForm(formdata=None, return_to=return_to)
    if ai_result is not None:
        analyze_form.submit.label.text = "AIで再解析"
    return render_template(
        "emails/detail.html",
        email=email,
        error_message=None,
        requires_reauthentication=False,
        return_to=return_to,
        back_link_label=back_link_label,
        analyze_form=analyze_form,
        ai_available=is_email_ai_configured(),
        ai_result=ai_result,
        ai_error_message=None,
        ai_apply_token=ai_apply_token,
        ai_checklist_token=ai_checklist_token,
        ai_calendar_token=ai_calendar_token,
        analysis_session_token=analysis_session_token,
        analysis_session_state=analysis_session_state,
    )


@bp.post("/<message_id>/analyze")
def analyze(message_id):
    form = AnalyzeEmailForm()
    if not form.validate_on_submit():
        abort(400)

    return_to = safe_email_list_return_url(form.return_to.data)
    back_link_label = (
        "検索結果へ戻る"
        if return_to != EMAIL_LIST_PATH
        else "就活メール一覧へ戻る"
    )
    credential = get_credential_store().get_gmail_credential()
    if credential is None:
        flash("AI解析を実行するにはGmail連携が必要です。", "warning")
        return redirect(url_for("integrations.settings"))

    try:
        email = get_gmail_service().get_message(message_id)
    except GmailServiceError as error:
        log_gmail_failure(
            current_app.logger,
            "analyze",
            error.stage,
            error.original_error,
        )
        error_message, requires_reauthentication = _error_context(error)
        return render_template(
            "emails/detail.html",
            email=None,
            error_message=error_message,
            requires_reauthentication=requires_reauthentication,
            return_to=return_to,
            back_link_label=back_link_label,
            analyze_form=None,
            ai_available=False,
            ai_result=None,
            ai_error_message=None,
            ai_apply_token=None,
            ai_checklist_token=None,
            ai_calendar_token=None,
        )

    ai_service = get_email_ai_service()
    ai_result = None
    ai_error_message = None
    ai_apply_token = None
    ai_checklist_token = None
    ai_calendar_token = None
    analysis_session_token = None
    analysis_session_state = None
    try:
        ai_result, input_char_count = ai_service.analyze(
            subject=email.subject,
            sender=email.sender,
            received_at=email.received_at,
            body_text=email.body_text,
        )
    except EmailAIServiceError as error:
        log_email_ai_failure(current_app.logger, error)
        ai_error_message = _ai_error_message(error)
    else:
        log_email_ai_success(
            current_app.logger,
            input_char_count,
            ai_result.output_item_count,
        )
        try:
            analysis_session_token = get_email_analysis_session_store().save(
                message_id,
                get_gmail_connection_key(credential),
                ai_result,
                return_to,
            )
            session_entry = _analysis_session_entry(
                analysis_session_token,
                message_id,
                credential,
            )
        except (RuntimeError, ValueError):
            session_entry = None
            current_app.logger.warning(
                "Email AI analysis session rejected "
                "operation=email_ai_analysis_session stage=context_store "
                "classification=context_unavailable success=false"
            )
        if session_entry is not None:
            tokens = _issue_analysis_review_tokens(
                message_id,
                analysis_session_token,
                session_entry,
            )
            ai_apply_token = tokens["application"]
            ai_checklist_token = tokens["checklist"]
            ai_calendar_token = tokens["calendar"]
            analysis_session_state = session_entry.state
            current_app.logger.info(
                "Email AI analysis session created "
                "operation=email_ai_analysis_session stage=created "
                "success=true"
            )

    analyze_form = AnalyzeEmailForm(formdata=None, return_to=return_to)
    if ai_result is not None:
        analyze_form.submit.label.text = "AIで再解析"
    return render_template(
        "emails/detail.html",
        email=email,
        error_message=None,
        requires_reauthentication=False,
        return_to=return_to,
        back_link_label=back_link_label,
        analyze_form=analyze_form,
        ai_available=ai_service.is_configured,
        ai_result=ai_result,
        ai_error_message=ai_error_message,
        ai_apply_token=ai_apply_token,
        ai_checklist_token=ai_checklist_token,
        ai_calendar_token=ai_calendar_token,
        analysis_session_token=analysis_session_token,
        analysis_session_state=analysis_session_state,
    )


def _render_analysis_apply(
    message_id,
    entry,
    form,
    application_choices,
    selected_application,
):
    return render_template(
        "emails/analysis_apply.html",
        message_id=message_id,
        ai_result=entry.result,
        ai_datetime_reference=ai_datetime_reference,
        form=form,
        application_choices=application_choices,
        selected_application=selected_application,
        return_to=entry.return_to,
        cancel_url=_analysis_session_detail_url(
            message_id,
            entry.analysis_session_token,
            entry.return_to,
        ),
    )


def _expired_analysis_apply_redirect(message_id, return_to):
    flash(
        "AI解析結果の有効期限が切れたか、すでに使用されています。"
        "もう一度AI解析を実行してください。",
        "warning",
    )
    return redirect(
        url_for(
            "emails.detail",
            message_id=message_id,
            return_to=safe_email_list_return_url(return_to),
        )
    )


@bp.route("/<message_id>/analysis/apply", methods=["GET", "POST"])
def apply_analysis(message_id):
    store = get_email_analysis_apply_store()
    token = (
        request.form.get("token")
        if request.method == "POST"
        else request.args.get("token")
    )
    fallback_return_to = safe_email_list_return_url(
        request.values.get("return_to")
    )
    entry = store.get(token, message_id)
    if entry is None:
        return _expired_analysis_apply_redirect(message_id, fallback_return_to)
    analysis_session_entry, gmail_credential, has_analysis_session = (
        _validate_review_analysis_session(entry, message_id)
    )
    if has_analysis_session and analysis_session_entry is None:
        return _expired_analysis_session_redirect(
            message_id,
            entry.return_to,
        )

    application_choices = build_application_choices()
    selected_application = None

    if request.method == "GET":
        selected_id = request.args.get("application_id", type=int)
        if selected_id is None:
            selected_id = entry.application_id
        if selected_id is not None and selected_id > 0:
            selected_application = db.session.get(Application, selected_id)
            if selected_application is None:
                flash("選択した応募先が見つかりませんでした。", "warning")
            else:
                entry = store.bind_application(token, message_id, selected_id)
                if entry is None:
                    return _expired_analysis_apply_redirect(
                        message_id,
                        fallback_return_to,
                    )
        form_data = (
            build_existing_application_form_data(selected_application)
            if selected_application is not None
            else build_ai_candidate_form_data(entry.result)
        )
        form_data.update(token=token, return_to=entry.return_to)
        form = EmailAnalysisApplyForm(data=form_data)
        form.application_id.choices = application_choices
        return _render_analysis_apply(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    form = EmailAnalysisApplyForm()
    form.application_id.choices = application_choices
    selected_id = form.application_id.data
    if isinstance(selected_id, int) and selected_id > 0:
        selected_application = db.session.get(Application, selected_id)

    form_is_valid = form.validate_on_submit()
    if form.apply_mode.data == "existing":
        if entry.application_id is None:
            form.application_id.errors.append(
                "上の選択欄から既存の応募先を読み込んでください。"
            )
            form_is_valid = False
        elif selected_id != entry.application_id:
            form.application_id.errors.append(
                "反映先の応募先を確認できませんでした。"
            )
            form_is_valid = False
        elif selected_application is None:
            form.application_id.errors.append(
                "選択した応募先が見つかりませんでした。"
            )
            form_is_valid = False

    if not form_is_valid:
        return _render_analysis_apply(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    consumed_entry = store.consume(token, message_id)
    if consumed_entry is None:
        return _expired_analysis_apply_redirect(message_id, entry.return_to)

    is_new = form.apply_mode.data == "new"
    duplicate_exists = False
    try:
        if is_new:
            duplicate_exists = duplicate_company_exists(form.company_name.data)
            application = create_application_from_ai_form(form)
        else:
            application = populate_application_from_ai_form(
                form,
                selected_application,
            )
        db.session.commit()
    except SQLAlchemyError as error:
        db.session.rollback()
        current_app.logger.error(
            "Email AI application apply failed operation=email_ai_apply "
            "stage=db_commit exception=%s success=false",
            type(error).__name__,
        )
        replacement_token = store.save(
            message_id,
            consumed_entry.result,
            consumed_entry.return_to,
            analysis_session_token=consumed_entry.analysis_session_token,
        )
        if consumed_entry.application_id is not None:
            entry = store.bind_application(
                replacement_token,
                message_id,
                consumed_entry.application_id,
            )
        else:
            entry = store.get(replacement_token, message_id)
        form.token.data = replacement_token
        flash(
            "応募先へ反映できませんでした。入力内容を確認してもう一度お試しください。",
            "danger",
        )
        return _render_analysis_apply(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    if duplicate_exists:
        flash("同じ会社名の応募先が既に登録されています。", "warning")
    if is_new:
        flash("AI解析結果を確認して応募先を登録しました。", "success")
    else:
        flash("AI解析結果を確認して応募先情報を更新しました。", "success")
    current_app.logger.info(
        "Email AI application apply completed operation=email_ai_apply "
        "stage=completed mode=%s success=true",
        form.apply_mode.data,
    )
    if consumed_entry.analysis_session_token:
        get_email_analysis_session_store().mark_application(
            consumed_entry.analysis_session_token,
            message_id,
            get_gmail_connection_key(gmail_credential),
            application.id,
        )
        return redirect(
            _analysis_session_detail_url(
                message_id,
                consumed_entry.analysis_session_token,
                consumed_entry.return_to,
            )
            + "#ai-analysis-result"
        )
    return redirect(
        url_for("applications.detail", application_id=application.id)
    )


def _render_analysis_checklist(
    message_id,
    entry,
    form,
    application_choices,
    selected_application,
):
    duplicate_flags = (
        duplicate_candidate_flags(form, selected_application.id)
        if selected_application is not None
        else [False] * len(form.candidates.entries)
    )
    return render_template(
        "emails/analysis_checklist.html",
        message_id=message_id,
        ai_result=entry.result,
        form=form,
        application_choices=application_choices,
        selected_application=selected_application,
        duplicate_flags=duplicate_flags,
        return_to=entry.return_to,
        cancel_url=_analysis_session_detail_url(
            message_id,
            entry.analysis_session_token,
            entry.return_to,
        ),
    )


@bp.route("/<message_id>/analysis/checklist", methods=["GET", "POST"])
def apply_analysis_checklist(message_id):
    store = get_email_analysis_checklist_store()
    token = (
        request.form.get("token")
        if request.method == "POST"
        else request.args.get("token")
    )
    fallback_return_to = safe_email_list_return_url(
        request.values.get("return_to")
    )
    entry = store.get(token, message_id)
    if entry is None:
        return _expired_analysis_apply_redirect(message_id, fallback_return_to)
    analysis_session_entry, gmail_credential, has_analysis_session = (
        _validate_review_analysis_session(entry, message_id)
    )
    if has_analysis_session and analysis_session_entry is None:
        return _expired_analysis_session_redirect(
            message_id,
            entry.return_to,
        )

    application_choices = build_application_choices()
    selected_application = None

    if request.method == "GET":
        selected_id = request.args.get("application_id", type=int)
        if selected_id is None:
            selected_id = entry.application_id
        if selected_id is not None and selected_id > 0:
            selected_application = db.session.get(Application, selected_id)
            if selected_application is None:
                flash("選択した応募先が見つかりませんでした。", "warning")
            else:
                entry = store.bind_application(token, message_id, selected_id)
                if entry is None:
                    return _expired_analysis_apply_redirect(
                        message_id,
                        fallback_return_to,
                    )
        form = EmailAnalysisChecklistForm(
            data={
                "application_id": (
                    selected_application.id
                    if selected_application is not None
                    else -1
                ),
                "candidates": build_checklist_candidate_data(entry.result),
                "token": token,
                "return_to": entry.return_to,
            }
        )
        form.application_id.choices = application_choices
        return _render_analysis_checklist(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    form = EmailAnalysisChecklistForm()
    form.application_id.choices = application_choices
    selected_id = form.application_id.data
    if isinstance(selected_id, int) and selected_id > 0:
        selected_application = db.session.get(Application, selected_id)

    form_is_valid = form.validate_on_submit()
    if len(form.candidates.entries) != len(entry.result.action_items):
        form.candidates.errors.append("AI候補の件数を確認できませんでした。")
        form_is_valid = False
    if entry.application_id is None:
        form.application_id.errors.append(
            "登録先の応募先を上の選択欄から選んでください。"
        )
        form_is_valid = False
    elif selected_id != entry.application_id:
        form.application_id.errors.append(
            "登録先の応募先を確認できませんでした。"
        )
        form_is_valid = False
    elif selected_application is None:
        form.application_id.errors.append(
            "選択した応募先が見つかりませんでした。"
        )
        form_is_valid = False

    selected_count = sum(
        candidate.selected.data for candidate in form.candidates.entries
    )
    if selected_count == 0:
        form.candidates.errors.append("追加する項目を選択してください。")
        form_is_valid = False

    if not form_is_valid:
        return _render_analysis_checklist(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    duplicate_flags = duplicate_candidate_flags(form, selected_application.id)
    consumed_entry = store.consume(token, message_id)
    if consumed_entry is None:
        return _expired_analysis_apply_redirect(message_id, entry.return_to)

    try:
        items = build_selected_checklist_items(form, selected_application.id)
        db.session.add_all(items)
        db.session.commit()
    except SQLAlchemyError as error:
        db.session.rollback()
        current_app.logger.error(
            "Email AI checklist apply failed operation=email_ai_checklist "
            "stage=db_commit exception=%s success=false",
            type(error).__name__,
        )
        replacement_token = store.save(
            message_id,
            consumed_entry.result,
            consumed_entry.return_to,
            analysis_session_token=consumed_entry.analysis_session_token,
        )
        entry = store.bind_application(
            replacement_token,
            message_id,
            consumed_entry.application_id,
        )
        form.token.data = replacement_token
        flash(
            "チェックリストへ追加できませんでした。"
            "入力内容を確認してもう一度お試しください。",
            "danger",
        )
        return _render_analysis_checklist(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    warned_titles = set()
    for candidate, is_duplicate in zip(
        form.candidates.entries,
        duplicate_flags,
        strict=True,
    ):
        if not candidate.selected.data or not is_duplicate:
            continue
        title = candidate.title.data
        title_key = title.casefold()
        if title_key in warned_titles:
            continue
        warned_titles.add(title_key)
        flash(
            f"「{title}」は既に未完了タスクとして存在します。",
            "warning",
        )
    flash(
        f"AI解析結果を確認してチェックリストに{len(items)}件追加しました。",
        "success",
    )
    current_app.logger.info(
        "Email AI checklist apply completed operation=email_ai_checklist "
        "stage=completed count=%s success=true",
        len(items),
    )
    if consumed_entry.analysis_session_token:
        get_email_analysis_session_store().mark_checklist(
            consumed_entry.analysis_session_token,
            message_id,
            get_gmail_connection_key(gmail_credential),
            selected_application.id,
            len(items),
        )
        return redirect(
            _analysis_session_detail_url(
                message_id,
                consumed_entry.analysis_session_token,
                consumed_entry.return_to,
            )
            + "#ai-analysis-result"
        )
    return redirect(
        url_for(
            "applications.detail",
            application_id=selected_application.id,
        )
        + "#checklist"
    )


def _calendar_application_choices():
    choices = build_application_choices()
    choices[0] = (-1, "応募先に紐付けず登録")
    return choices


def _render_analysis_calendar(
    message_id,
    entry,
    form,
    application_choices,
    selected_application,
):
    sync_service = get_calendar_sync_service()
    credential = get_credential_store().get_calendar_credential()
    registration_service = get_email_calendar_registration_service()
    registrations = {}
    if credential is not None:
        registrations = registration_service.get_for_event_types(
            message_id,
            (
                candidate.event_type.data
                for candidate in form.candidates.entries
            ),
            credential,
        )
        for candidate in form.candidates.entries:
            if candidate.event_type.data in registrations:
                candidate.selected.data = False
    status_form = EmailCalendarStatusForm(
        data={"token": form.token.data, "return_to": entry.return_to}
    )
    return render_template(
        "emails/analysis_calendar.html",
        message_id=message_id,
        ai_result=entry.result,
        form=form,
        application_choices=application_choices,
        selected_application=selected_application,
        calendar_connected=credential is not None,
        candidate_labels=calendar_candidate_labels(),
        candidate_evidence=lambda event_type: candidate_evidence(
            entry.result,
            event_type,
        ),
        candidate_ai_datetime=lambda event_type: candidate_ai_datetime_display(
            entry.result,
            event_type,
        ),
        candidate_states=candidate_sync_states(
            form,
            selected_application,
            sync_service,
            registrations,
        ),
        status_form=status_form,
        has_registered_candidates=bool(registrations),
        datetime_text_references=build_datetime_text_references(entry.result),
        return_to=entry.return_to,
        cancel_url=_analysis_session_detail_url(
            message_id,
            entry.analysis_session_token,
            entry.return_to,
        ),
    )


def _calendar_apply_flash(result):
    for _event_type in result.registered_event_types:
        flash(
            "このメールから抽出した予定はすでにGoogle Calendarへ"
            "登録されています。",
            "warning",
        )
    for event_type in result.duplicate_event_types:
        label = application_sync_label(event_type)
        flash(
            f"この応募先の{label}はすでにGoogle Calendarへ同期されています。",
            "warning",
        )

    messages = []
    if result.created_count:
        messages.append(f"{result.created_count}件登録しました。")
    if result.failed_count:
        messages.append(f"{result.failed_count}件は登録できませんでした。")
    if result.sync_failure_count:
        messages.append(
            f"{result.sync_failure_count}件は予定を登録しましたが、"
            "同期情報を保存できませんでした。"
        )
    if result.tracking_failure_count:
        messages.append(
            f"{result.tracking_failure_count}件はGoogle側に予定が作成された"
            "可能性がありますが、登録済み情報を保存できませんでした。"
            "再操作する前にGoogle Calendarを確認してください。"
        )
    if messages:
        category = "success"
        if (
            result.failed_count
            or result.sync_failure_count
            or result.tracking_failure_count
        ):
            category = "warning" if result.created_count else "danger"
        flash("".join(messages), category)
    elif not result.duplicate_count:
        flash("登録できる予定がありませんでした。", "warning")


@bp.route("/<message_id>/analysis/calendar", methods=["GET", "POST"])
def apply_analysis_calendar(message_id):
    store = get_email_analysis_calendar_store()
    token = (
        request.form.get("token")
        if request.method == "POST"
        else request.args.get("token")
    )
    fallback_return_to = safe_email_list_return_url(
        request.values.get("return_to")
    )
    entry = store.get(token, message_id)
    if entry is None:
        return _expired_analysis_apply_redirect(message_id, fallback_return_to)
    analysis_session_entry, gmail_credential, has_analysis_session = (
        _validate_review_analysis_session(entry, message_id)
    )
    if has_analysis_session and analysis_session_entry is None:
        return _expired_analysis_session_redirect(
            message_id,
            entry.return_to,
        )

    application_choices = _calendar_application_choices()
    selected_application = None

    if request.method == "GET":
        selected_id = request.args.get("application_id", type=int)
        if selected_id is None:
            selected_id = entry.application_id
        if selected_id is not None and selected_id > 0:
            selected_application = db.session.get(Application, selected_id)
            if selected_application is None:
                flash("選択した応募先が見つかりませんでした。", "warning")
            else:
                entry = store.bind_application(token, message_id, selected_id)
                if entry is None:
                    return _expired_analysis_apply_redirect(
                        message_id,
                        fallback_return_to,
                    )
        form = EmailAnalysisCalendarForm(
            data={
                "application_id": (
                    selected_application.id
                    if selected_application is not None
                    else -1
                ),
                "candidates": build_calendar_candidate_data(entry.result),
                "token": token,
                "return_to": entry.return_to,
            }
        )
        form.application_id.choices = application_choices
        return _render_analysis_calendar(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    current_app.logger.info(
        "Google Calendar route started operation=ai_calendar_apply "
        "stage=route_start"
    )
    form = EmailAnalysisCalendarForm()
    form.application_id.choices = application_choices
    selected_id = form.application_id.data
    if isinstance(selected_id, int) and selected_id > 0:
        selected_application = db.session.get(Application, selected_id)

    form_is_valid = form.validate_on_submit()
    expected_types = calendar_candidate_types(entry.result)
    submitted_types = tuple(
        candidate.event_type.data for candidate in form.candidates.entries
    )
    if submitted_types != expected_types:
        form.candidates.errors.append("AI候補の内容を確認できませんでした。")
        form_is_valid = False

    if selected_id is None or selected_id < -1 or selected_id == 0:
        form.application_id.errors.append("紐付け先を確認できませんでした。")
        form_is_valid = False
    elif selected_id > 0:
        if entry.application_id is None:
            form.application_id.errors.append(
                "上の選択欄から応募先を読み込んでください。"
            )
            form_is_valid = False
        elif selected_id != entry.application_id:
            form.application_id.errors.append(
                "紐付け先の応募先を確認できませんでした。"
            )
            form_is_valid = False
        elif selected_application is None:
            form.application_id.errors.append(
                "選択した応募先が見つかりませんでした。"
            )
            form_is_valid = False

    selected_count = sum(
        candidate.selected.data for candidate in form.candidates.entries
    )
    if selected_count == 0:
        form.candidates.errors.append("登録する予定を選択してください。")
        form_is_valid = False

    if not form_is_valid:
        return _render_analysis_calendar(
            message_id,
            entry,
            form,
            application_choices,
            selected_application,
        )

    calendar_credential = get_credential_store().get_calendar_credential()
    if calendar_credential is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    consumed_entry = store.consume(token, message_id)
    if consumed_entry is None:
        return _expired_analysis_apply_redirect(message_id, entry.return_to)

    candidates = build_reviewed_calendar_candidates(form)
    result = EmailAnalysisCalendarApplyService(
        get_google_calendar_service(),
        get_calendar_sync_service(),
        get_email_calendar_registration_service(),
        message_id,
        calendar_credential,
    ).apply(
        candidates,
        consumed_entry.result,
        selected_application,
    )

    for failure in result.failures:
        error = failure.error
        log_calendar_failure(
            current_app.logger,
            "ai_calendar_apply",
            error.stage,
            error.original_error,
            event_type=failure.event_type,
        )
    for failure in result.sync_failures:
        error = failure.error
        log_calendar_failure(
            current_app.logger,
            "ai_calendar_apply",
            error.stage,
            error.original_error,
            level=logging.ERROR,
            event_type=failure.event_type,
        )
    for failure in result.tracking_failures:
        error = failure.error
        log_calendar_failure(
            current_app.logger,
            "ai_calendar_apply",
            error.stage,
            error.original_error,
            level=logging.ERROR,
            event_type=failure.event_type,
        )
    current_app.logger.info(
        "Google Calendar completed operation=ai_calendar_apply "
        "stage=completed success_count=%s failed_count=%s "
        "duplicate_count=%s sync_failure_count=%s tracking_failure_count=%s",
        result.created_count,
        result.failed_count,
        result.duplicate_count,
        result.sync_failure_count,
        result.tracking_failure_count,
    )
    _calendar_apply_flash(result)
    if consumed_entry.analysis_session_token:
        get_email_analysis_session_store().mark_calendar(
            consumed_entry.analysis_session_token,
            message_id,
            get_gmail_connection_key(gmail_credential),
            result.created_count,
            (
                result.failed_count
                + result.sync_failure_count
                + result.tracking_failure_count
            ),
        )
        return redirect(
            _analysis_session_detail_url(
                message_id,
                consumed_entry.analysis_session_token,
                consumed_entry.return_to,
            )
            + "#ai-analysis-result"
        )
    return redirect(
        url_for(
            "emails.detail",
            message_id=message_id,
            return_to=consumed_entry.return_to,
        )
    )


@bp.post("/<message_id>/analysis/calendar/status")
def check_analysis_calendar_status(message_id):
    current_app.logger.info(
        "Google Calendar route started operation=ai_calendar_status "
        "stage=route_start"
    )
    form = EmailCalendarStatusForm()
    fallback_return_to = safe_email_list_return_url(
        request.form.get("return_to")
    )
    if not form.validate_on_submit():
        abort(400)

    store = get_email_analysis_calendar_store()
    entry = store.get(form.token.data, message_id)
    if entry is None:
        return _expired_analysis_apply_redirect(
            message_id,
            fallback_return_to,
        )
    analysis_session_entry, _gmail_credential, has_analysis_session = (
        _validate_review_analysis_session(entry, message_id)
    )
    if has_analysis_session and analysis_session_entry is None:
        return _expired_analysis_session_redirect(
            message_id,
            entry.return_to,
        )

    calendar_credential = get_credential_store().get_calendar_credential()
    if calendar_credential is None:
        flash("先にGoogleカレンダーと連携してください。", "warning")
        return redirect(url_for("integrations.settings"))

    result = get_email_calendar_registration_service().reconcile_remote(
        message_id,
        calendar_candidate_types(entry.result),
        calendar_credential,
        get_google_calendar_service(),
    )
    for failure in result.cleared_failures:
        error = failure.error
        log_calendar_failure(
            current_app.logger,
            "ai_calendar_status",
            error.stage,
            error.original_error,
            level=logging.WARNING,
            event_type=failure.event_type,
        )
    for failure in result.failures:
        error = failure.error
        log_calendar_failure(
            current_app.logger,
            "ai_calendar_status",
            error.stage,
            error.original_error,
            event_type=failure.event_type,
        )
    for failure in result.storage_failures:
        error = failure.error
        log_calendar_failure(
            current_app.logger,
            "ai_calendar_status",
            error.stage,
            error.original_error,
            level=logging.ERROR,
            event_type=failure.event_type,
        )

    if result.cleared_event_types:
        flash(
            "Google Calendar上の予定が見つからなかったため、登録済み状態を"
            "解除しました。再度登録してください。",
            "warning",
        )
    if result.active_event_types:
        flash(
            f"Google Calendar上の登録済み予定を"
            f"{len(result.active_event_types)}件確認しました。",
            "success",
        )
    if result.failures:
        flash(
            "Google Calendar上の登録状態を確認できない予定がありました。"
            "登録済み状態は維持しています。",
            "warning",
        )
    if result.storage_failures:
        flash(
            "Google Calendar上では予定が見つかりませんでしたが、登録済み"
            "状態を解除できませんでした。もう一度お試しください。",
            "danger",
        )
    if not (
        result.cleared_event_types
        or result.active_event_types
        or result.failures
        or result.storage_failures
    ):
        flash("確認できる登録済み予定はありません。", "info")

    current_app.logger.info(
        "Google Calendar completed operation=ai_calendar_status "
        "stage=completed active_count=%s cleared_count=%s failed_count=%s "
        "storage_failure_count=%s",
        len(result.active_event_types),
        len(result.cleared_event_types),
        len(result.failures),
        len(result.storage_failures),
    )
    query = {
        "token": form.token.data,
        "return_to": entry.return_to,
    }
    if entry.application_id:
        query["application_id"] = entry.application_id
    return redirect(
        url_for(
            "emails.apply_analysis_calendar",
            message_id=message_id,
            **query,
        )
    )
