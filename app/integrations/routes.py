from secrets import compare_digest

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.models import GoogleCredential

from . import bp
from .credential_store import CredentialStorageError, GoogleCredentialStore
from .diagnostics import log_callback_request_check, log_oauth_failure
from .forms import DisconnectGmailForm, DisconnectGoogleForm
from .google_service import (
    GoogleConfigurationError,
    GoogleOAuthError,
    GoogleOAuthService,
    GoogleOAuthSettings,
    build_authorization_response,
)


GOOGLE_OAUTH_STATE_KEY = "google_oauth_state"
GOOGLE_GMAIL_OAUTH_STATE_KEY = "google_gmail_oauth_state"
GOOGLE_OAUTH_STATE_KEYS = {
    GoogleCredential.CONNECTION_CALENDAR: GOOGLE_OAUTH_STATE_KEY,
    GoogleCredential.CONNECTION_GMAIL: GOOGLE_GMAIL_OAUTH_STATE_KEY,
}


def get_google_settings(
    connection_type=GoogleCredential.CONNECTION_CALENDAR,
):
    return GoogleOAuthSettings.from_config(
        current_app.config,
        connection_type=connection_type,
    )


def get_credential_store():
    return GoogleCredentialStore(current_app.config["OAUTH_OWNER_KEY"])


def get_google_oauth_service(
    connection_type=GoogleCredential.CONNECTION_CALENDAR,
):
    return GoogleOAuthService(
        get_google_settings(connection_type),
        connection_type=connection_type,
    )


def get_oauth_attempt_store():
    return current_app.extensions["google_oauth_attempt_store"]


def _state_key(connection_type):
    try:
        return GOOGLE_OAUTH_STATE_KEYS[connection_type]
    except KeyError as error:
        raise ValueError("Unsupported Google OAuth connection type.") from error


def _connection_label(connection_type):
    if connection_type == GoogleCredential.CONNECTION_GMAIL:
        return "Gmail"
    return "Google"


def _save_credential(connection_type, credentials, email):
    store = get_credential_store()
    if connection_type == GoogleCredential.CONNECTION_GMAIL:
        return store.save_gmail_credential(credentials, email=email)
    return store.save_calendar_credential(credentials, email=email)


def _delete_credential(connection_type):
    store = get_credential_store()
    if connection_type == GoogleCredential.CONNECTION_GMAIL:
        return store.delete_gmail_credential()
    return store.delete_calendar_credential()


@bp.route("/settings/integrations")
def settings():
    calendar_settings = get_google_settings(
        GoogleCredential.CONNECTION_CALENDAR
    )
    gmail_settings = get_google_settings(GoogleCredential.CONNECTION_GMAIL)
    credential_store = get_credential_store()
    calendar_credential = credential_store.get_calendar_credential()
    gmail_credential = credential_store.get_gmail_credential()
    return render_template(
        "integrations/settings.html",
        google_settings=calendar_settings,
        gmail_settings=gmail_settings,
        calendar_credential=calendar_credential,
        gmail_credential=gmail_credential,
        disconnect_form=DisconnectGoogleForm(),
        gmail_disconnect_form=DisconnectGmailForm(),
    )


def _start_google_oauth(connection_type):
    label = _connection_label(connection_type)
    state_key = _state_key(connection_type)
    current_app.logger.info(
        "Google OAuth route started connection_type=%s stage=authorization_start",
        connection_type,
    )
    try:
        authorization_request = get_google_oauth_service(
            connection_type
        ).authorization_url()
        attempt_store = get_oauth_attempt_store()
        previous_state = session.pop(state_key, None)
        attempt_store.discard(previous_state)
        attempt_store.save(
            authorization_request.state,
            authorization_request.code_verifier,
            authorization_request.redirect_uri,
            connection_type=connection_type,
        )
    except GoogleConfigurationError as error:
        log_oauth_failure(
            current_app.logger,
            "configuration",
            error,
            connection_type=connection_type,
        )
        flash(
            f"{label}連携に必要な環境変数が設定されていません。",
            "warning",
        )
        return redirect(url_for("integrations.settings"))
    except GoogleOAuthError as error:
        log_oauth_failure(
            current_app.logger,
            error.stage,
            error.original_error,
            connection_type=connection_type,
        )
        flash(f"{label}連携を開始できませんでした。", "danger")
        return redirect(url_for("integrations.settings"))
    except ValueError as error:
        log_oauth_failure(
            current_app.logger,
            "oauth_attempt_storage",
            error,
            connection_type=connection_type,
        )
        flash(f"{label}連携を開始できませんでした。", "danger")
        return redirect(url_for("integrations.settings"))

    session[state_key] = authorization_request.state
    return redirect(authorization_request.authorization_url)


@bp.route("/integrations/google/connect")
def google_connect():
    return _start_google_oauth(GoogleCredential.CONNECTION_CALENDAR)


@bp.route("/integrations/google/gmail/connect")
def google_gmail_connect():
    return _start_google_oauth(GoogleCredential.CONNECTION_GMAIL)


def _complete_google_oauth(connection_type):
    label = _connection_label(connection_type)
    state_key = _state_key(connection_type)
    expected_state = session.pop(state_key, None)
    attempt_store = get_oauth_attempt_store()
    google_settings = get_google_settings(connection_type)
    current_app.logger.info(
        "Google OAuth route started connection_type=%s stage=callback",
        connection_type,
    )
    log_callback_request_check(
        current_app.logger,
        google_settings.redirect_uri,
        request.base_url,
        connection_type=connection_type,
    )

    if request.args.get("error"):
        attempt_store.discard(expected_state)
        flash(f"{label}連携がキャンセルされました。", "warning")
        return redirect(url_for("integrations.settings"))

    received_state = request.args.get("state")
    if (
        not expected_state
        or not received_state
        or not compare_digest(expected_state, received_state)
    ):
        attempt_store.discard(expected_state)
        log_oauth_failure(
            current_app.logger,
            "state_validation",
            ValueError("OAuth state validation failed."),
            connection_type=connection_type,
        )
        flash(
            f"{label}連携を確認できませんでした。もう一度お試しください。",
            "danger",
        )
        return redirect(url_for("integrations.settings"))

    oauth_attempt = attempt_store.consume(expected_state)
    if (
        oauth_attempt is None
        or oauth_attempt.connection_type != connection_type
    ):
        log_oauth_failure(
            current_app.logger,
            "oauth_attempt_validation",
            ValueError(
                "OAuth attempt is missing, expired, already used, or mismatched."
            ),
            connection_type=connection_type,
        )
        flash(
            f"{label}連携を確認できませんでした。もう一度お試しください。",
            "danger",
        )
        return redirect(url_for("integrations.settings"))

    try:
        authorization_response = build_authorization_response(
            google_settings.redirect_uri,
            request.query_string,
        )
        credentials, email = get_google_oauth_service(
            connection_type
        ).exchange_callback(
            authorization_response,
            expected_state,
            oauth_attempt.code_verifier,
            oauth_attempt.authorization_redirect_uri,
        )
        _save_credential(connection_type, credentials, email)
    except GoogleConfigurationError as error:
        log_oauth_failure(
            current_app.logger,
            "configuration",
            error,
            connection_type=connection_type,
        )
        flash(
            f"{label}連携を完了できませんでした。もう一度お試しください。",
            "danger",
        )
        return redirect(url_for("integrations.settings"))
    except GoogleOAuthError as error:
        log_oauth_failure(
            current_app.logger,
            error.stage,
            error.original_error,
            connection_type=connection_type,
        )
        flash(
            f"{label}連携を完了できませんでした。もう一度お試しください。",
            "danger",
        )
        return redirect(url_for("integrations.settings"))
    except CredentialStorageError as error:
        log_oauth_failure(
            current_app.logger,
            error.stage,
            error.original_error,
            connection_type=connection_type,
        )
        flash(
            f"{label}連携を完了できませんでした。もう一度お試しください。",
            "danger",
        )
        return redirect(url_for("integrations.settings"))

    success_message = (
        "Gmailと連携しました。"
        if connection_type == GoogleCredential.CONNECTION_GMAIL
        else "Googleカレンダーと連携しました。"
    )
    flash(success_message, "success")
    return redirect(url_for("integrations.settings"))


@bp.route("/integrations/google/callback")
def google_callback():
    return _complete_google_oauth(GoogleCredential.CONNECTION_CALENDAR)


@bp.route("/integrations/google/gmail/callback")
def google_gmail_callback():
    return _complete_google_oauth(GoogleCredential.CONNECTION_GMAIL)


def _disconnect_google(connection_type, form):
    if form.validate_on_submit():
        _delete_credential(connection_type)
        pending_state = session.pop(_state_key(connection_type), None)
        get_oauth_attempt_store().discard(pending_state)
        message = (
            "Gmail連携を解除しました。"
            if connection_type == GoogleCredential.CONNECTION_GMAIL
            else "Googleカレンダー連携を解除しました。"
        )
        flash(message, "success")
    return redirect(url_for("integrations.settings"))


@bp.route("/integrations/google/disconnect", methods=["POST"])
def google_disconnect():
    return _disconnect_google(
        GoogleCredential.CONNECTION_CALENDAR,
        DisconnectGoogleForm(),
    )


@bp.route("/integrations/google/gmail/disconnect", methods=["POST"])
def google_gmail_disconnect():
    return _disconnect_google(
        GoogleCredential.CONNECTION_GMAIL,
        DisconnectGmailForm(),
    )
