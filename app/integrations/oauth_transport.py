import os
from urllib.parse import urlparse


OAUTHLIB_INSECURE_TRANSPORT = "OAUTHLIB_INSECURE_TRANSPORT"
LOCAL_OAUTH_HOSTS = {"127.0.0.1", "localhost"}
TRUE_VALUES = {"1", "true", "yes", "on"}


class OAuthTransportConfigurationError(RuntimeError):
    pass


def _is_enabled(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUE_VALUES


def configure_oauthlib_transport(config, environ=None):
    """Allow HTTP OAuth only for an explicitly enabled loopback setup."""
    target_environ = os.environ if environ is None else environ
    environment = str(config.get("APP_ENV", "development")).strip().lower()
    redirect_uris = [
        str(config.get("GOOGLE_REDIRECT_URI", "")).strip(),
        str(config.get("GOOGLE_GMAIL_REDIRECT_URI", "")).strip(),
    ]
    parsed_redirects = [
        urlparse(uri) for uri in redirect_uris if uri
    ]
    has_http = any(
        parsed.scheme.lower() == "http" for parsed in parsed_redirects
    )
    all_https = bool(parsed_redirects) and all(
        parsed.scheme.lower() == "https" for parsed in parsed_redirects
    )
    all_secure_or_local = bool(parsed_redirects) and all(
        parsed.scheme.lower() == "https"
        or (
            parsed.scheme.lower() == "http"
            and (parsed.hostname or "").lower() in LOCAL_OAUTH_HOSTS
        )
        for parsed in parsed_redirects
    )

    if environment == "production" and not all_https:
        target_environ.pop(OAUTHLIB_INSECURE_TRANSPORT, None)
        raise OAuthTransportConfigurationError(
            "Production Google OAuth redirect URI must use HTTPS."
        )

    allow_insecure = (
        environment == "development"
        and _is_enabled(config.get("ALLOW_INSECURE_OAUTH", False))
        and has_http
        and all_secure_or_local
    )

    if allow_insecure:
        target_environ[OAUTHLIB_INSECURE_TRANSPORT] = "1"
    else:
        target_environ.pop(OAUTHLIB_INSECURE_TRANSPORT, None)

    return allow_insecure
