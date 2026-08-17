import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from app import create_app
from app.extensions import db
from app.integrations import google_service, routes
from app.integrations.credential_store import GoogleCredentialStore
from app.integrations.google_service import (
    GoogleAuthorizationRequest,
    GoogleOAuthError,
    GoogleOAuthService,
    GoogleOAuthSettings,
)
from app.integrations.oauth_transport import (
    OAUTHLIB_INSECURE_TRANSPORT,
    configure_oauthlib_transport,
)
from app.models import GoogleCredential
from config import TestConfig


GMAIL_REDIRECT_URI = (
    "http://127.0.0.1:5000/integrations/google/gmail/callback"
)
CALENDAR_REDIRECT_URI = (
    "http://127.0.0.1:5000/integrations/google/callback"
)
GMAIL_CODE_VERIFIER = "g" * 64


def make_credentials(
    token="gmail-access",
    refresh_token="gmail-refresh",
    scopes=None,
):
    return SimpleNamespace(
        token=token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=list(scopes or google_service.DEFAULT_GMAIL_SCOPES),
        granted_scopes=None,
        expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


def prepare_gmail_callback(client, state="gmail-expected-state"):
    with client.session_transaction() as session:
        session[routes.GOOGLE_GMAIL_OAUTH_STATE_KEY] = state
    client.application.extensions["google_oauth_attempt_store"].save(
        state,
        GMAIL_CODE_VERIFIER,
        GMAIL_REDIRECT_URI,
        connection_type=GoogleCredential.CONNECTION_GMAIL,
    )


def save_calendar_credential():
    return GoogleCredentialStore("test-user").save_calendar_credential(
        make_credentials(
            token="calendar-access",
            refresh_token="calendar-refresh",
            scopes=google_service.DEFAULT_GOOGLE_SCOPES,
        ),
        email="daily@example.com",
    )


def test_gmail_settings_use_readonly_scope_and_dedicated_redirect_uri():
    settings = GoogleOAuthSettings.from_config(
        {
            "GOOGLE_CLIENT_ID": "client-id",
            "GOOGLE_CLIENT_SECRET": "client-secret",
            "GOOGLE_GMAIL_REDIRECT_URI": GMAIL_REDIRECT_URI,
            "GOOGLE_GMAIL_OAUTH_SCOPES": (
                "openid email "
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/calendar.events "
                "https://www.googleapis.com/auth/gmail.modify"
            ),
        },
        connection_type=GoogleCredential.CONNECTION_GMAIL,
    )

    assert settings.redirect_uri == GMAIL_REDIRECT_URI
    assert settings.scopes == google_service.DEFAULT_GMAIL_SCOPES
    assert google_service.GOOGLE_GMAIL_READONLY_SCOPE in settings.scopes
    assert "https://www.googleapis.com/auth/calendar.events" not in (
        settings.scopes
    )
    assert "https://www.googleapis.com/auth/gmail.modify" not in (
        settings.scopes
    )


def test_gmail_authorization_uses_pkce_offline_and_account_selection(
    monkeypatch,
):
    captured = {}

    class FakeFlow:
        redirect_uri = None
        code_verifier = None

        def authorization_url(self, **kwargs):
            captured["authorization_kwargs"] = kwargs
            self.code_verifier = GMAIL_CODE_VERIFIER
            return "https://accounts.google.com/auth", "gmail-state"

    fake_flow = FakeFlow()

    def fake_from_client_config(client_config, scopes, **kwargs):
        captured["client_config"] = client_config
        captured["scopes"] = scopes
        captured["flow_kwargs"] = kwargs
        return fake_flow

    monkeypatch.setattr(
        google_service.Flow,
        "from_client_config",
        fake_from_client_config,
    )
    service = GoogleOAuthService(
        GoogleOAuthSettings(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri=GMAIL_REDIRECT_URI,
            scopes=google_service.DEFAULT_GMAIL_SCOPES,
            redirect_uri_variable="GOOGLE_GMAIL_REDIRECT_URI",
        ),
        connection_type=GoogleCredential.CONNECTION_GMAIL,
    )

    authorization = service.authorization_url()

    assert authorization.code_verifier == GMAIL_CODE_VERIFIER
    assert authorization.redirect_uri == GMAIL_REDIRECT_URI
    assert captured["scopes"] == list(google_service.DEFAULT_GMAIL_SCOPES)
    assert captured["client_config"]["web"]["redirect_uris"] == [
        GMAIL_REDIRECT_URI
    ]
    assert captured["flow_kwargs"]["autogenerate_code_verifier"] is True
    assert captured["authorization_kwargs"] == {
        "access_type": "offline",
        "include_granted_scopes": "false",
        "prompt": "select_account consent",
    }


def test_external_http_gmail_redirect_never_enables_insecure_transport():
    environ = {OAUTHLIB_INSECURE_TRANSPORT: "1"}

    enabled = configure_oauthlib_transport(
        {
            "APP_ENV": "development",
            "ALLOW_INSECURE_OAUTH": "true",
            "GOOGLE_REDIRECT_URI": CALENDAR_REDIRECT_URI,
            "GOOGLE_GMAIL_REDIRECT_URI": (
                "http://example.com/integrations/google/gmail/callback"
            ),
        },
        environ=environ,
    )

    assert enabled is False
    assert OAUTHLIB_INSECURE_TRANSPORT not in environ


def test_gmail_connect_redirects_and_stores_separate_state(client, monkeypatch):
    captured_types = []

    class FakeService:
        def authorization_url(self):
            return GoogleAuthorizationRequest(
                authorization_url="https://accounts.google.com/gmail-auth",
                state="gmail-generated-state",
                code_verifier=GMAIL_CODE_VERIFIER,
                redirect_uri=GMAIL_REDIRECT_URI,
            )

    def fake_service(connection_type):
        captured_types.append(connection_type)
        return FakeService()

    monkeypatch.setattr(routes, "get_google_oauth_service", fake_service)
    with client.session_transaction() as session:
        session[routes.GOOGLE_OAUTH_STATE_KEY] = "calendar-pending-state"

    response = client.get("/integrations/google/gmail/connect")

    assert response.status_code == 302
    assert response.location == "https://accounts.google.com/gmail-auth"
    assert captured_types == [GoogleCredential.CONNECTION_GMAIL]
    with client.session_transaction() as session:
        assert session[routes.GOOGLE_GMAIL_OAUTH_STATE_KEY] == (
            "gmail-generated-state"
        )
        assert session[routes.GOOGLE_OAUTH_STATE_KEY] == (
            "calendar-pending-state"
        )
        assert GMAIL_CODE_VERIFIER not in str(dict(session))
    attempt = client.application.extensions[
        "google_oauth_attempt_store"
    ].consume("gmail-generated-state")
    assert attempt.connection_type == GoogleCredential.CONNECTION_GMAIL
    assert attempt.authorization_redirect_uri == GMAIL_REDIRECT_URI


def test_calendar_and_gmail_oauth_attempts_do_not_collide(client, monkeypatch):
    class FakeService:
        def __init__(self, connection_type):
            self.connection_type = connection_type

        def authorization_url(self):
            is_gmail = self.connection_type == "gmail"
            return GoogleAuthorizationRequest(
                authorization_url=(
                    "https://accounts.google.com/gmail"
                    if is_gmail
                    else "https://accounts.google.com/calendar"
                ),
                state="gmail-state" if is_gmail else "calendar-state",
                code_verifier=("g" if is_gmail else "c") * 64,
                redirect_uri=(
                    GMAIL_REDIRECT_URI if is_gmail else CALENDAR_REDIRECT_URI
                ),
            )

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type: FakeService(connection_type),
    )

    assert client.get("/integrations/google/connect").status_code == 302
    assert client.get("/integrations/google/gmail/connect").status_code == 302

    with client.session_transaction() as session:
        assert session[routes.GOOGLE_OAUTH_STATE_KEY] == "calendar-state"
        assert session[routes.GOOGLE_GMAIL_OAUTH_STATE_KEY] == "gmail-state"
    store = client.application.extensions["google_oauth_attempt_store"]
    assert store.consume("calendar-state").connection_type == "calendar"
    assert store.consume("gmail-state").connection_type == "gmail"


def test_gmail_callback_saves_gmail_and_preserves_calendar(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        save_calendar_credential()

    class FakeService:
        def exchange_callback(
            self,
            authorization_response,
            state,
            code_verifier,
            authorization_redirect_uri,
        ):
            assert authorization_response.startswith(GMAIL_REDIRECT_URI + "?")
            assert state == "gmail-expected-state"
            assert code_verifier == GMAIL_CODE_VERIFIER
            assert authorization_redirect_uri == GMAIL_REDIRECT_URI
            return make_credentials(), "jobs@example.com"

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type: FakeService(),
    )
    prepare_gmail_callback(client)

    response = client.get(
        "/integrations/google/gmail/callback",
        query_string={
            "code": "gmail-authorization-code",
            "state": "gmail-expected-state",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Gmailと連携しました。" in response.get_data(as_text=True)
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        calendar = store.get_calendar_credential()
        gmail = store.get_gmail_credential()
        assert GoogleCredential.query.count() == 2
        assert calendar.access_token == "calendar-access"
        assert calendar.refresh_token == "calendar-refresh"
        assert gmail.connection_type == "gmail"
        assert gmail.google_account_email == "jobs@example.com"
        assert gmail.access_token == "gmail-access"
        assert gmail.refresh_token == "gmail-refresh"


def test_gmail_reauthentication_preserves_refresh_and_calendar(
    client,
    app,
    monkeypatch,
):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        save_calendar_credential()
        store.save_gmail_credential(
            make_credentials(
                token="old-gmail-access",
                refresh_token="existing-gmail-refresh",
            ),
            email="jobs@example.com",
        )

    class FakeService:
        def exchange_callback(self, *args):
            return (
                make_credentials(
                    token="new-gmail-access",
                    refresh_token=None,
                ),
                "jobs@example.com",
            )

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type: FakeService(),
    )
    prepare_gmail_callback(client, state="gmail-reauth-state")

    response = client.get(
        "/integrations/google/gmail/callback",
        query_string={"code": "new-code", "state": "gmail-reauth-state"},
    )

    assert response.status_code == 302
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        assert store.get_calendar_credential().access_token == (
            "calendar-access"
        )
        assert store.get_gmail_credential().access_token == (
            "new-gmail-access"
        )
        assert store.get_gmail_credential().refresh_token == (
            "existing-gmail-refresh"
        )


def test_gmail_state_mismatch_is_rejected(client, app, monkeypatch):
    exchange_calls = []

    class FakeService:
        def exchange_callback(self, *args):
            exchange_calls.append(True)

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type: FakeService(),
    )
    prepare_gmail_callback(client)

    response = client.get(
        "/integrations/google/gmail/callback",
        query_string={"code": "code", "state": "wrong-state"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Gmail連携を確認できませんでした。" in response.get_data(
        as_text=True
    )
    assert exchange_calls == []
    with app.app_context():
        assert GoogleCredential.query.count() == 0


def test_gmail_attempt_connection_type_mismatch_is_rejected(
    client,
    app,
    monkeypatch,
):
    with client.session_transaction() as session:
        session[routes.GOOGLE_GMAIL_OAUTH_STATE_KEY] = "mismatched-state"
    client.application.extensions["google_oauth_attempt_store"].save(
        "mismatched-state",
        GMAIL_CODE_VERIFIER,
        GMAIL_REDIRECT_URI,
        connection_type=GoogleCredential.CONNECTION_CALENDAR,
    )
    exchange_calls = []

    class FakeService:
        def exchange_callback(self, *args):
            exchange_calls.append(True)

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type: FakeService(),
    )

    response = client.get(
        "/integrations/google/gmail/callback",
        query_string={"code": "code", "state": "mismatched-state"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Gmail連携を確認できませんでした。" in response.get_data(
        as_text=True
    )
    assert exchange_calls == []
    with app.app_context():
        assert GoogleCredential.query.count() == 0


def test_gmail_callback_can_only_be_used_once(client, app, monkeypatch):
    exchange_calls = []

    class FakeService:
        def exchange_callback(self, *args):
            exchange_calls.append(True)
            return make_credentials(), "jobs@example.com"

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type: FakeService(),
    )
    prepare_gmail_callback(client)
    callback_path = (
        "/integrations/google/gmail/callback"
        "?code=one-time-code&state=gmail-expected-state"
    )

    first = client.get(callback_path)
    second = client.get(callback_path, follow_redirects=True)

    assert first.status_code == 302
    assert second.status_code == 200
    assert exchange_calls == [True]
    assert "Gmail連携を確認できませんでした。" in second.get_data(
        as_text=True
    )
    with app.app_context():
        assert GoogleCredential.query.count() == 1


def test_gmail_callback_failure_is_safe_and_preserves_calendar(
    client,
    app,
    monkeypatch,
    caplog,
):
    secret_values = (
        "secret-gmail-code",
        "secret-gmail-access",
        "secret-client-secret",
    )
    with app.app_context():
        save_calendar_credential()

    class FailingService:
        def exchange_callback(self, *args):
            raise GoogleOAuthError(
                "token_exchange",
                RuntimeError(" ".join(secret_values)),
            )

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type: FailingService(),
    )
    prepare_gmail_callback(client)
    caplog.set_level(logging.ERROR)

    response = client.get(
        "/integrations/google/gmail/callback",
        query_string={
            "code": secret_values[0],
            "state": "gmail-expected-state",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Gmail連携を完了できませんでした。" in response.get_data(
        as_text=True
    )
    assert "connection_type=gmail" in caplog.text
    for secret in secret_values:
        assert secret not in caplog.text
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        assert store.get_calendar_credential() is not None
        assert store.get_gmail_credential() is None


def test_gmail_settings_show_independent_accounts_and_actions(client, app):
    with app.app_context():
        save_calendar_credential()
        GoogleCredentialStore("test-user").save_gmail_credential(
            make_credentials(),
            email="jobs@example.com",
        )

    html = client.get("/settings/integrations").get_data(as_text=True)

    assert "daily@example.com" in html
    assert "jobs@example.com" in html
    assert "Gmail連携を解除する" in html
    assert "/integrations/google/gmail/disconnect" in html
    assert "Googleカレンダーと別のGoogleアカウントを利用できます。" in html
    assert "gmail-access" not in html
    assert "calendar-access" not in html


def test_gmail_unconnected_settings_show_connect_action(client):
    class ConfiguredGmailConfig(TestConfig):
        GOOGLE_CLIENT_ID = "client-id"
        GOOGLE_CLIENT_SECRET = "client-secret"

    test_app = create_app(ConfiguredGmailConfig)
    test_client = test_app.test_client()
    with test_app.app_context():
        db.create_all()
        html = test_client.get("/settings/integrations").get_data(as_text=True)
        assert "Gmailと連携する" in html
        assert "/integrations/google/gmail/connect" in html
        assert "未連携" in html
        db.session.remove()
        db.drop_all()


def test_gmail_disconnect_is_post_only_and_preserves_calendar(client, app):
    with app.app_context():
        save_calendar_credential()
        GoogleCredentialStore("test-user").save_gmail_credential(
            make_credentials(),
            email="jobs@example.com",
        )

    assert client.get("/integrations/google/gmail/disconnect").status_code == 405
    response = client.post(
        "/integrations/google/gmail/disconnect",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Gmail連携を解除しました。" in response.get_data(as_text=True)
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        assert store.get_gmail_credential() is None
        assert store.get_calendar_credential() is not None


def test_gmail_disconnect_form_requires_valid_csrf_and_preserves_calendar():
    class CSRFConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "gmail-csrf-test-secret"

    test_app = create_app(CSRFConfig)
    test_client = test_app.test_client()
    with test_app.app_context():
        db.create_all()
        save_calendar_credential()
        GoogleCredentialStore("test-user").save_gmail_credential(
            make_credentials(),
            email="jobs@example.com",
        )

        assert (
            test_client.post("/integrations/google/gmail/disconnect").status_code
            == 400
        )
        html = test_client.get("/settings/integrations").get_data(as_text=True)
        form_match = re.search(
            r'<form id="disconnectGmailForm".*?</form>',
            html,
            flags=re.DOTALL,
        )
        assert form_match is not None
        token_match = re.search(
            r'name="csrf_token" type="hidden" value="([^"]+)"',
            form_match.group(0),
        )
        assert token_match is not None

        response = test_client.post(
            "/integrations/google/gmail/disconnect",
            data={"csrf_token": token_match.group(1)},
            follow_redirects=True,
        )

        assert response.status_code == 200
        store = GoogleCredentialStore("test-user")
        assert store.get_gmail_credential() is None
        assert store.get_calendar_credential() is not None
        db.session.remove()
        db.drop_all()
