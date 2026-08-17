import logging
from urllib.parse import urlparse


SAFE_ERROR_MESSAGES = {
    "Warning": "OAuth scope response did not match the requested scopes.",
    "InsecureTransportError": "OAuth token endpoint rejected an insecure transport.",
    "InvalidGrantError": "OAuth authorization grant was rejected.",
    "HttpError": "Google API request failed.",
    "RefreshError": "Google OAuth credential refresh failed.",
    "CalendarEventCancelledError": "Google Calendar event is cancelled.",
    "CalendarEventStatusError": "Google Calendar event status is unavailable.",
}

INVALID_GRANT_CLASSIFICATIONS = {
    "code_already_used": ("already used", "already redeemed", "redeemed"),
    "redirect_uri_mismatch_during_exchange": (
        "redirect_uri",
        "redirect uri",
        "redirect mismatch",
    ),
    "pkce_verifier_mismatch": (
        "code_verifier",
        "code verifier",
        "code challenge",
        "pkce",
    ),
    "code_invalid_or_expired": (
        "authorization code",
        "invalid code",
        "malformed auth code",
        "expired",
    ),
}


def get_http_status(error):
    response = getattr(error, "response", None)
    if response is None:
        response = getattr(error, "resp", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(response, "status", None)
    if status_code is None:
        status_code = getattr(error, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def safe_error_details(error):
    oauth_error = (
        "invalid_grant"
        if getattr(error, "error", None) == "invalid_grant"
        else "unknown"
    )
    return {
        "exception_class": type(error).__name__,
        "safe_message": SAFE_ERROR_MESSAGES.get(
            type(error).__name__,
            "Sensitive exception details were omitted.",
        ),
        "http_status": get_http_status(error),
        "oauth_error": oauth_error,
        "classification": classify_invalid_grant(error),
    }


def classify_invalid_grant(error):
    if getattr(error, "error", None) != "invalid_grant":
        return "not_invalid_grant"

    description = str(getattr(error, "description", "") or "").lower()
    for classification, markers in INVALID_GRANT_CLASSIFICATIONS.items():
        if any(marker in description for marker in markers):
            return classification
    return "unknown_invalid_grant"


def log_oauth_failure(
    logger,
    stage,
    error,
    level=logging.ERROR,
    connection_type="calendar",
):
    details = safe_error_details(error)
    logger.log(
        level,
        "Google OAuth failed connection_type=%s stage=%s exception=%s message=%s "
        "http_status=%s oauth_error=%s classification=%s",
        connection_type,
        stage,
        details["exception_class"],
        details["safe_message"],
        (
            details["http_status"]
            if details["http_status"] is not None
            else "unknown"
        ),
        details["oauth_error"],
        details["classification"],
    )


def classify_google_api_error(error):
    status = get_http_status(error)
    if status == 404:
        return "not_found"
    if status == 410:
        return "gone"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    if status is not None and 500 <= status <= 599:
        return "server_error"
    return "unknown"


def log_calendar_failure(
    logger,
    operation,
    stage,
    error,
    level=logging.ERROR,
    event_type="unknown",
):
    details = safe_error_details(error)
    event_status = (
        "cancelled"
        if getattr(error, "event_status", None) == "cancelled"
        else "unknown"
    )
    logger.log(
        level,
        "Google Calendar failed operation=%s event_type=%s stage=%s exception=%s "
        "message=%s http_status=%s api_error=%s event_status=%s",
        operation,
        event_type,
        stage,
        details["exception_class"],
        details["safe_message"],
        (
            details["http_status"]
            if details["http_status"] is not None
            else "unknown"
        ),
        classify_google_api_error(error),
        event_status,
    )


def log_gmail_failure(logger, operation, stage, error, level=logging.ERROR):
    details = safe_error_details(error)
    logger.log(
        level,
        "Gmail API failed operation=%s stage=%s exception=%s message=%s "
        "http_status=%s api_error=%s",
        operation,
        stage,
        details["exception_class"],
        details["safe_message"],
        (
            details["http_status"]
            if details["http_status"] is not None
            else "unknown"
        ),
        classify_google_api_error(error),
    )


def redirect_uri_details(uri):
    parsed = urlparse(uri)
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1"}:
        host_kind = "loopback"
    elif hostname:
        host_kind = "external"
    else:
        host_kind = "missing"
    try:
        port = parsed.port
    except ValueError:
        port = "invalid"
    return {
        "scheme": parsed.scheme.lower() or "missing",
        "host_kind": host_kind,
        "port": port if port is not None else "default",
        "path": parsed.path,
    }


def log_redirect_uri_check(
    logger,
    configured_uri,
    authorization_uri,
    exchange_uri,
    connection_type="calendar",
):
    configured = redirect_uri_details(configured_uri)
    authorization = redirect_uri_details(authorization_uri)
    exchange = redirect_uri_details(exchange_uri)
    exact_match = configured_uri == authorization_uri == exchange_uri
    path_match = (
        configured["path"]
        == authorization["path"]
        == exchange["path"]
    )
    logger.info(
        "Google OAuth redirect check connection_type=%s scheme=%s "
        "host_kind=%s port=%s "
        "authorization_match=%s exchange_match=%s path_match=%s",
        connection_type,
        configured["scheme"],
        configured["host_kind"],
        configured["port"],
        configured_uri == authorization_uri,
        authorization_uri == exchange_uri,
        path_match,
    )
    return exact_match


def log_callback_request_check(
    logger,
    configured_uri,
    callback_base_url,
    connection_type="calendar",
):
    configured = redirect_uri_details(configured_uri)
    callback = redirect_uri_details(callback_base_url)
    logger.info(
        "Google OAuth callback check connection_type=%s scheme=%s "
        "host_kind=%s port=%s "
        "scheme_match=%s host_kind_match=%s port_match=%s path_match=%s",
        connection_type,
        callback["scheme"],
        callback["host_kind"],
        callback["port"],
        callback["scheme"] == configured["scheme"],
        callback["host_kind"] == configured["host_kind"],
        callback["port"] == configured["port"],
        callback["path"] == configured["path"],
    )
