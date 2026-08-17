import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.models import GoogleCredential

from .diagnostics import log_oauth_failure, log_redirect_uri_check


GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_EMAIL_SCOPE = (
    "https://www.googleapis.com/auth/userinfo.email"
)
DEFAULT_GOOGLE_SCOPES = (
    "openid",
    GOOGLE_USERINFO_EMAIL_SCOPE,
    "https://www.googleapis.com/auth/calendar.events",
)
GOOGLE_GMAIL_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/gmail.readonly"
)
DEFAULT_GMAIL_SCOPES = (
    "openid",
    GOOGLE_USERINFO_EMAIL_SCOPE,
    GOOGLE_GMAIL_READONLY_SCOPE,
)

logger = logging.getLogger(__name__)


class GoogleConfigurationError(RuntimeError):
    pass


class GoogleOAuthError(RuntimeError):
    def __init__(self, stage, original_error):
        super().__init__("Google OAuth processing failed.")
        self.stage = stage
        self.original_error = original_error


@dataclass(frozen=True)
class GoogleAuthorizationRequest:
    authorization_url: str
    state: str
    code_verifier: str
    redirect_uri: str


def normalize_google_scopes(scopes):
    normalized = []
    for scope in scopes:
        canonical_scope = (
            GOOGLE_USERINFO_EMAIL_SCOPE if scope == "email" else scope
        )
        if canonical_scope not in normalized:
            normalized.append(canonical_scope)
    return tuple(normalized)


def build_authorization_response(redirect_uri, query_string):
    parsed_redirect = urlsplit(redirect_uri)
    callback_query = query_string.decode("ascii")
    combined_query = "&".join(
        value
        for value in (parsed_redirect.query, callback_query)
        if value
    )
    return urlunsplit(
        (
            parsed_redirect.scheme,
            parsed_redirect.netloc,
            parsed_redirect.path,
            combined_query,
            "",
        )
    )


@dataclass(frozen=True)
class GoogleOAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]
    redirect_uri_variable: str = "GOOGLE_REDIRECT_URI"

    @classmethod
    def from_config(
        cls,
        config,
        connection_type=GoogleCredential.CONNECTION_CALENDAR,
    ):
        if connection_type == GoogleCredential.CONNECTION_GMAIL:
            redirect_uri_variable = "GOOGLE_GMAIL_REDIRECT_URI"
            scope_variable = "GOOGLE_GMAIL_OAUTH_SCOPES"
            default_scopes = DEFAULT_GMAIL_SCOPES
        else:
            redirect_uri_variable = "GOOGLE_REDIRECT_URI"
            scope_variable = "GOOGLE_OAUTH_SCOPES"
            default_scopes = DEFAULT_GOOGLE_SCOPES

        raw_scopes = config.get(scope_variable, "")
        scopes = normalize_google_scopes(
            scope
            for scope in raw_scopes.replace(",", " ").split()
            if scope
        )
        if connection_type == GoogleCredential.CONNECTION_GMAIL:
            scopes = tuple(
                scope for scope in scopes if scope in DEFAULT_GMAIL_SCOPES
            )
            scopes = tuple(
                dict.fromkeys((*scopes, *DEFAULT_GMAIL_SCOPES))
            )

        return cls(
            client_id=config.get("GOOGLE_CLIENT_ID", "").strip(),
            client_secret=config.get("GOOGLE_CLIENT_SECRET", "").strip(),
            redirect_uri=config.get(redirect_uri_variable, "").strip(),
            scopes=scopes or default_scopes,
            redirect_uri_variable=redirect_uri_variable,
        )

    @property
    def missing_variables(self):
        missing = []
        if not self.client_id:
            missing.append("GOOGLE_CLIENT_ID")
        if not self.client_secret:
            missing.append("GOOGLE_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append(self.redirect_uri_variable)
        return missing

    @property
    def is_configured(self):
        return not self.missing_variables


class GoogleOAuthService:
    def __init__(
        self,
        settings,
        connection_type=GoogleCredential.CONNECTION_CALENDAR,
    ):
        if connection_type not in GoogleCredential.CONNECTION_TYPES:
            raise ValueError("Unsupported Google OAuth connection type.")
        self.settings = settings
        self.connection_type = connection_type

    def _flow(
        self,
        state=None,
        code_verifier=None,
        autogenerate_code_verifier=False,
    ):
        if not self.settings.is_configured:
            raise GoogleConfigurationError(
                "Google OAuth environment variables are missing."
            )
        client_config = {
            "web": {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
                "redirect_uris": [self.settings.redirect_uri],
            }
        }
        flow = Flow.from_client_config(
            client_config,
            scopes=list(self.settings.scopes),
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=autogenerate_code_verifier,
        )
        flow.redirect_uri = self.settings.redirect_uri
        return flow

    def authorization_url(self):
        try:
            flow = self._flow(autogenerate_code_verifier=True)
            authorization_url, state = flow.authorization_url(
                access_type="offline",
                include_granted_scopes=(
                    "false"
                    if self.connection_type
                    == GoogleCredential.CONNECTION_GMAIL
                    else "true"
                ),
                prompt=(
                    "select_account consent"
                    if self.connection_type
                    == GoogleCredential.CONNECTION_GMAIL
                    else "consent"
                ),
            )
            if not flow.code_verifier:
                raise ValueError("PKCE code verifier was not generated.")
            return GoogleAuthorizationRequest(
                authorization_url=authorization_url,
                state=state,
                code_verifier=flow.code_verifier,
                redirect_uri=flow.redirect_uri,
            )
        except GoogleConfigurationError:
            raise
        except Exception as error:
            raise GoogleOAuthError("authorization_start", error) from error

    def exchange_callback(
        self,
        authorization_response,
        state,
        code_verifier,
        authorization_redirect_uri,
    ):
        if not code_verifier:
            raise GoogleOAuthError(
                "pkce_verifier_validation",
                ValueError("PKCE code verifier is missing."),
            )

        try:
            flow = self._flow(
                state=state,
                code_verifier=code_verifier,
                autogenerate_code_verifier=False,
            )
        except GoogleConfigurationError:
            raise
        except Exception as error:
            raise GoogleOAuthError("flow_reconstruction", error) from error

        redirect_uri_matches = log_redirect_uri_check(
            logger,
            self.settings.redirect_uri,
            authorization_redirect_uri,
            flow.redirect_uri,
            connection_type=self.connection_type,
        )
        if not redirect_uri_matches:
            raise GoogleOAuthError(
                "redirect_uri_validation",
                ValueError("OAuth redirect URI changed between requests."),
            )

        try:
            flow.fetch_token(authorization_response=authorization_response)
        except Exception as error:
            raise GoogleOAuthError("token_exchange", error) from error

        try:
            credentials = flow.credentials
            if not credentials or not credentials.token:
                raise ValueError("OAuth credentials are incomplete.")
        except Exception as error:
            raise GoogleOAuthError("credentials_retrieval", error) from error

        email = self._fetch_email(credentials)
        return credentials, email

    def _fetch_email(self, credentials):
        try:
            oauth2_service = build(
                "oauth2",
                "v2",
                credentials=credentials,
                cache_discovery=False,
            )
            account = oauth2_service.userinfo().get().execute()
            return account.get("email")
        except Exception as error:
            # Email is helpful for display but must not make token storage fail.
            log_oauth_failure(
                logger,
                "account_email",
                error,
                level=logging.WARNING,
                connection_type=self.connection_type,
            )
            return None
