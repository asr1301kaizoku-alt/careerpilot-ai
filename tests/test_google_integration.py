import json
import logging
import os
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from oauthlib.oauth2 import InvalidGrantError, WebApplicationClient
from requests_oauthlib import OAuth2Session

from app import create_app
from app.extensions import db
from app.integrations import google_service, routes
from app.integrations.credential_store import (
    CredentialStorageError,
    GoogleCredentialStore,
)
from app.integrations.diagnostics import get_http_status
from app.integrations.google_service import (
    GoogleOAuthError,
    GoogleOAuthService,
    GoogleOAuthSettings,
)
from app.integrations.oauth_transport import (
    OAUTHLIB_INSECURE_TRANSPORT,
    OAuthTransportConfigurationError,
    configure_oauthlib_transport,
)
from app.models import GoogleCredential
from config import TestConfig


TEST_REDIRECT_URI = (
    "http://127.0.0.1:5000/integrations/google/callback"
)
TEST_CODE_VERIFIER = "v" * 64


def prepare_oauth_callback(client, state="expected-state"):
    with client.session_transaction() as session:
        session[routes.GOOGLE_OAUTH_STATE_KEY] = state
    client.application.extensions["google_oauth_attempt_store"].save(
        state,
        TEST_CODE_VERIFIER,
        TEST_REDIRECT_URI,
    )


def fake_credentials(
    token="access-token",
    refresh_token="refresh-token",
    granted_scopes=None,
):
    return SimpleNamespace(
        token=token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[
            "openid",
            google_service.GOOGLE_USERINFO_EMAIL_SCOPE,
            "https://www.googleapis.com/auth/calendar.events",
        ],
        granted_scopes=granted_scopes,
        expiry=datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
    )


def save_connected_credential():
    return GoogleCredentialStore("test-user").save_calendar_credential(
        fake_credentials(),
        email="student@example.com",
    )


def test_integrations_settings_page_is_displayed(client):
    response = client.get("/settings/integrations")
    assert response.status_code == 200
    assert "Googleカレンダー連携" in response.get_data(as_text=True)


def test_missing_environment_variables_do_not_cause_500(client):
    response = client.get("/settings/integrations")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Google OAuthの設定が必要です" in html
    assert "GOOGLE_CLIENT_ID" in html
    assert "GOOGLE_CLIENT_SECRET" in html

    connect_response = client.get(
        "/integrations/google/connect",
        follow_redirects=True,
    )
    assert connect_response.status_code == 200
    assert "Google連携に必要な環境変数が設定されていません。" in (
        connect_response.get_data(as_text=True)
    )


def test_unconnected_state_is_displayed(client):
    html = client.get("/settings/integrations").get_data(as_text=True)
    assert "未連携" in html


def test_connected_state_and_email_are_displayed(client, app):
    with app.app_context():
        save_connected_credential()

    html = client.get("/settings/integrations").get_data(as_text=True)
    assert "連携済み" in html
    assert "student@example.com" in html
    assert "access-token" not in html
    assert "refresh-token" not in html


def test_google_connect_redirects_and_stores_state(client, monkeypatch):
    class FakeGoogleOAuthService:
        def authorization_url(self):
            return google_service.GoogleAuthorizationRequest(
                authorization_url=(
                    "https://accounts.google.com/o/oauth2/v2/auth?client_id=test"
                ),
                state="generated-state",
                code_verifier=TEST_CODE_VERIFIER,
                redirect_uri=TEST_REDIRECT_URI,
            )

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FakeGoogleOAuthService(),
    )

    response = client.get("/integrations/google/connect")
    assert response.status_code == 302
    assert response.location.startswith("https://accounts.google.com/")
    with client.session_transaction() as session:
        assert session[routes.GOOGLE_OAUTH_STATE_KEY] == "generated-state"
        assert TEST_CODE_VERIFIER not in str(dict(session))
    attempt = client.application.extensions[
        "google_oauth_attempt_store"
    ].consume("generated-state")
    assert attempt.code_verifier == TEST_CODE_VERIFIER
    assert attempt.authorization_redirect_uri == TEST_REDIRECT_URI


def test_authorization_url_requests_offline_access_and_consent(monkeypatch):
    captured = {}

    class FakeFlow:
        redirect_uri = None
        code_verifier = None

        def authorization_url(self, **kwargs):
            captured.update(kwargs)
            self.code_verifier = TEST_CODE_VERIFIER
            return "https://accounts.google.com/auth", "secure-state"

    fake_flow = FakeFlow()

    def fake_from_client_config(client_config, scopes, state=None, **kwargs):
        captured["client_config"] = client_config
        captured["scopes"] = scopes
        captured["state"] = state
        captured.update(kwargs)
        return fake_flow

    monkeypatch.setattr(
        google_service.Flow,
        "from_client_config",
        fake_from_client_config,
    )
    settings = GoogleOAuthSettings(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="http://127.0.0.1:5000/integrations/google/callback",
        scopes=google_service.DEFAULT_GOOGLE_SCOPES,
    )
    authorization_request = GoogleOAuthService(settings).authorization_url()

    assert authorization_request.authorization_url == (
        "https://accounts.google.com/auth"
    )
    assert authorization_request.state == "secure-state"
    assert authorization_request.code_verifier == TEST_CODE_VERIFIER
    assert authorization_request.redirect_uri == settings.redirect_uri
    assert captured["access_type"] == "offline"
    assert captured["prompt"] == "consent"
    assert captured["include_granted_scopes"] == "true"
    assert captured["scopes"] == list(google_service.DEFAULT_GOOGLE_SCOPES)
    assert captured["autogenerate_code_verifier"] is True
    assert captured["code_verifier"] is None
    assert "email" not in captured["scopes"]
    assert google_service.GOOGLE_USERINFO_EMAIL_SCOPE in captured["scopes"]
    assert fake_flow.redirect_uri == settings.redirect_uri


@pytest.mark.parametrize("hostname", ["127.0.0.1", "localhost"])
def test_development_loopback_with_explicit_permission_enables_http(hostname):
    environ = {}

    enabled = configure_oauthlib_transport(
        {
            "APP_ENV": "development",
            "ALLOW_INSECURE_OAUTH": "true",
            "GOOGLE_REDIRECT_URI": (
                f"http://{hostname}:5000/integrations/google/callback"
            ),
        },
        environ=environ,
    )

    assert enabled is True
    assert environ[OAUTHLIB_INSECURE_TRANSPORT] == "1"


def test_application_factory_applies_local_oauth_transport(monkeypatch):
    class LocalOAuthConfig(TestConfig):
        APP_ENV = "development"
        ALLOW_INSECURE_OAUTH = True
        GOOGLE_REDIRECT_URI = (
            "http://127.0.0.1:5000/integrations/google/callback"
        )

    monkeypatch.delenv(OAUTHLIB_INSECURE_TRANSPORT, raising=False)

    test_app = create_app(LocalOAuthConfig)

    assert test_app.config["APP_ENV"] == "development"
    assert os.environ[OAUTHLIB_INSECURE_TRANSPORT] == "1"


def test_development_without_explicit_permission_keeps_http_disabled():
    environ = {OAUTHLIB_INSECURE_TRANSPORT: "1"}

    enabled = configure_oauthlib_transport(
        {
            "APP_ENV": "development",
            "ALLOW_INSECURE_OAUTH": "false",
            "GOOGLE_REDIRECT_URI": (
                "http://127.0.0.1:5000/integrations/google/callback"
            ),
        },
        environ=environ,
    )

    assert enabled is False
    assert OAUTHLIB_INSECURE_TRANSPORT not in environ


def test_production_never_enables_insecure_transport():
    environ = {OAUTHLIB_INSECURE_TRANSPORT: "1"}

    enabled = configure_oauthlib_transport(
        {
            "APP_ENV": "production",
            "ALLOW_INSECURE_OAUTH": "true",
            "GOOGLE_REDIRECT_URI": (
                "https://careerpilot.example/integrations/google/callback"
            ),
        },
        environ=environ,
    )

    assert enabled is False
    assert OAUTHLIB_INSECURE_TRANSPORT not in environ


def test_production_http_redirect_is_a_configuration_error():
    environ = {OAUTHLIB_INSECURE_TRANSPORT: "1"}

    with pytest.raises(
        OAuthTransportConfigurationError,
        match="must use HTTPS",
    ):
        configure_oauthlib_transport(
            {
                "APP_ENV": "production",
                "ALLOW_INSECURE_OAUTH": "true",
                "GOOGLE_REDIRECT_URI": (
                    "http://127.0.0.1:5000/integrations/google/callback"
                ),
            },
            environ=environ,
        )

    assert OAUTHLIB_INSECURE_TRANSPORT not in environ


def test_external_http_host_never_enables_insecure_transport():
    environ = {OAUTHLIB_INSECURE_TRANSPORT: "1"}

    enabled = configure_oauthlib_transport(
        {
            "APP_ENV": "development",
            "ALLOW_INSECURE_OAUTH": "true",
            "GOOGLE_REDIRECT_URI": (
                "http://careerpilot.example/integrations/google/callback"
            ),
        },
        environ=environ,
    )

    assert enabled is False
    assert OAUTHLIB_INSECURE_TRANSPORT not in environ


def test_email_scope_alias_is_normalized_to_userinfo_email():
    settings = GoogleOAuthSettings.from_config(
        {
            "GOOGLE_CLIENT_ID": "client-id",
            "GOOGLE_CLIENT_SECRET": "client-secret",
            "GOOGLE_REDIRECT_URI": (
                "http://127.0.0.1:5000/integrations/google/callback"
            ),
            "GOOGLE_OAUTH_SCOPES": (
                "openid email "
                "https://www.googleapis.com/auth/calendar.events"
            ),
        }
    )

    assert settings.scopes == google_service.DEFAULT_GOOGLE_SCOPES


def test_fetch_token_accepts_explicit_userinfo_email_scope(monkeypatch):
    oauth_session = OAuth2Session(
        client=WebApplicationClient("client-id"),
        scope=list(google_service.DEFAULT_GOOGLE_SCOPES),
        redirect_uri="http://127.0.0.1:5000/integrations/google/callback",
    )
    token_response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/json"},
        text=json.dumps(
            {
                "access_token": "mock-access-token",
                "refresh_token": "mock-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(google_service.DEFAULT_GOOGLE_SCOPES),
            }
        ),
        request=SimpleNamespace(
            url=google_service.GOOGLE_TOKEN_URI,
            headers={},
            body="redacted-test-request",
        ),
    )
    monkeypatch.setattr(
        oauth_session,
        "request",
        lambda *args, **kwargs: token_response,
    )

    token = oauth_session.fetch_token(
        google_service.GOOGLE_TOKEN_URI,
        code="mock-authorization-code",
        client_secret="mock-client-secret",
    )

    assert token["access_token"] == "mock-access-token"
    assert google_service.GOOGLE_USERINFO_EMAIL_SCOPE in token["scope"]


def test_exchange_callback_returns_credentials_and_email(monkeypatch):
    credentials = fake_credentials()
    fetch_token_calls = []
    flow_arguments = {}

    class FakeFlow:
        redirect_uri = TEST_REDIRECT_URI

        def fetch_token(self, authorization_response):
            assert authorization_response == "redacted-callback-for-test"
            fetch_token_calls.append(True)

        @property
        def credentials(self):
            return credentials

    service = GoogleOAuthService(
        GoogleOAuthSettings(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri=(
                "http://127.0.0.1:5000/integrations/google/callback"
            ),
            scopes=google_service.DEFAULT_GOOGLE_SCOPES,
        )
    )
    def fake_flow(**kwargs):
        flow_arguments.update(kwargs)
        return FakeFlow()

    monkeypatch.setattr(service, "_flow", fake_flow)
    monkeypatch.setattr(
        service,
        "_fetch_email",
        lambda value: "student@example.com",
    )

    result, email = service.exchange_callback(
        "redacted-callback-for-test",
        "expected-state",
        TEST_CODE_VERIFIER,
        TEST_REDIRECT_URI,
    )

    assert result is credentials
    assert email == "student@example.com"
    assert fetch_token_calls == [True]
    assert flow_arguments == {
        "state": "expected-state",
        "code_verifier": TEST_CODE_VERIFIER,
        "autogenerate_code_verifier": False,
    }


def test_authorization_response_uses_configured_redirect_uri():
    authorization_response = google_service.build_authorization_response(
        TEST_REDIRECT_URI,
        b"code=mock-code&state=mock-state",
    )

    assert authorization_response == (
        TEST_REDIRECT_URI + "?code=mock-code&state=mock-state"
    )


def test_redirect_uri_change_is_rejected_before_fetch_token(
    monkeypatch,
    caplog,
):
    fetch_token_calls = []

    class FakeFlow:
        redirect_uri = TEST_REDIRECT_URI

        def fetch_token(self, authorization_response):
            fetch_token_calls.append(True)

    service = GoogleOAuthService(
        GoogleOAuthSettings(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri=TEST_REDIRECT_URI,
            scopes=google_service.DEFAULT_GOOGLE_SCOPES,
        )
    )
    monkeypatch.setattr(service, "_flow", lambda **kwargs: FakeFlow())
    caplog.set_level(logging.INFO, logger=google_service.__name__)

    with pytest.raises(GoogleOAuthError) as error_info:
        service.exchange_callback(
            "redacted-callback-for-test",
            "expected-state",
            TEST_CODE_VERIFIER,
            "http://localhost:5000/integrations/google/callback/",
        )

    assert error_info.value.stage == "redirect_uri_validation"
    assert fetch_token_calls == []
    assert "authorization_match=False" in caplog.text
    assert "path_match=False" in caplog.text
    assert TEST_REDIRECT_URI not in caplog.text


def test_flow_reconstruction_uses_same_oauth_configuration(
    monkeypatch,
):
    flow_constructions = []
    credentials = fake_credentials()

    class FakeFlow:
        def __init__(self, code_verifier, autogenerate_code_verifier):
            self.redirect_uri = None
            self.code_verifier = code_verifier
            self.autogenerate_code_verifier = autogenerate_code_verifier

        def authorization_url(self, **kwargs):
            if self.autogenerate_code_verifier:
                self.code_verifier = TEST_CODE_VERIFIER
            return "https://accounts.google.com/auth", "expected-state"

        def fetch_token(self, authorization_response):
            return None

        @property
        def credentials(self):
            return credentials

    def fake_from_client_config(client_config, scopes, **kwargs):
        flow_constructions.append(
            {
                "client_config": client_config,
                "scopes": scopes,
                "state": kwargs.get("state"),
                "code_verifier": kwargs.get("code_verifier"),
                "autogenerate_code_verifier": kwargs.get(
                    "autogenerate_code_verifier"
                ),
            }
        )
        return FakeFlow(
            kwargs.get("code_verifier"),
            kwargs.get("autogenerate_code_verifier"),
        )

    monkeypatch.setattr(
        google_service.Flow,
        "from_client_config",
        fake_from_client_config,
    )
    service = GoogleOAuthService(
        GoogleOAuthSettings(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri=TEST_REDIRECT_URI,
            scopes=google_service.DEFAULT_GOOGLE_SCOPES,
        )
    )
    monkeypatch.setattr(
        service,
        "_fetch_email",
        lambda value: "student@example.com",
    )

    authorization_request = service.authorization_url()
    service.exchange_callback(
        TEST_REDIRECT_URI + "?code=mock&state=expected-state",
        authorization_request.state,
        authorization_request.code_verifier,
        authorization_request.redirect_uri,
    )

    assert len(flow_constructions) == 2
    assert flow_constructions[0]["client_config"] == (
        flow_constructions[1]["client_config"]
    )
    assert flow_constructions[0]["scopes"] == flow_constructions[1]["scopes"]
    assert flow_constructions[0]["autogenerate_code_verifier"] is True
    assert flow_constructions[1]["autogenerate_code_verifier"] is False
    assert flow_constructions[1]["code_verifier"] == TEST_CODE_VERIFIER


def test_state_mismatch_rejects_callback(client, app, monkeypatch):
    called = False

    class FakeGoogleOAuthService:
        def exchange_callback(self, authorization_response, state):
            nonlocal called
            called = True

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FakeGoogleOAuthService(),
    )
    with client.session_transaction() as session:
        session[routes.GOOGLE_OAUTH_STATE_KEY] = "expected-state"

    response = client.get(
        "/integrations/google/callback",
        query_string={"code": "code", "state": "wrong-state"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Google連携を確認できませんでした。" in response.get_data(
        as_text=True
    )
    assert called is False
    with app.app_context():
        assert GoogleCredential.query.count() == 0


def test_successful_callback_saves_credentials(client, app, monkeypatch):
    class FakeGoogleOAuthService:
        def exchange_callback(
            self,
            authorization_response,
            state,
            code_verifier,
            authorization_redirect_uri,
        ):
            assert authorization_response.startswith(TEST_REDIRECT_URI + "?")
            assert "code=authorization-code" in authorization_response
            assert state == "expected-state"
            assert code_verifier == TEST_CODE_VERIFIER
            assert authorization_redirect_uri == TEST_REDIRECT_URI
            return fake_credentials(), "student@example.com"

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FakeGoogleOAuthService(),
    )
    prepare_oauth_callback(client)

    response = client.get(
        "/integrations/google/callback",
        query_string={
            "code": "authorization-code",
            "state": "expected-state",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Googleカレンダーと連携しました。" in response.get_data(
        as_text=True
    )
    with app.app_context():
        credential = GoogleCredential.query.one()
        assert credential.google_account_email == "student@example.com"
        assert credential.access_token == "access-token"
        assert credential.refresh_token == "refresh-token"
        assert credential.owner_key == "test-user"
        assert credential.connection_type == "calendar"


def test_callback_saves_credentials_when_email_lookup_failed(
    client,
    app,
    monkeypatch,
    ):
    class FakeGoogleOAuthService:
        def exchange_callback(
            self,
            authorization_response,
            state,
            code_verifier,
            authorization_redirect_uri,
        ):
            return fake_credentials(), None

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FakeGoogleOAuthService(),
    )
    prepare_oauth_callback(client)

    response = client.get(
        "/integrations/google/callback",
        query_string={
            "code": "mock-code",
            "state": "expected-state",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Googleカレンダーと連携しました。" in response.get_data(
        as_text=True
    )
    with app.app_context():
        credential = GoogleCredential.query.one()
        assert credential.google_account_email is None
        assert credential.access_token == "access-token"


def test_callback_reload_does_not_exchange_code_twice(
    client,
    app,
    monkeypatch,
):
    exchange_calls = []

    class FakeGoogleOAuthService:
        def exchange_callback(
            self,
            authorization_response,
            state,
            code_verifier,
            authorization_redirect_uri,
        ):
            exchange_calls.append(True)
            return fake_credentials(), "student@example.com"

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FakeGoogleOAuthService(),
    )
    prepare_oauth_callback(client)
    callback_path = (
        "/integrations/google/callback"
        "?code=one-time-code&state=expected-state"
    )

    first_response = client.get(callback_path)
    second_response = client.get(callback_path, follow_redirects=True)

    assert first_response.status_code == 302
    assert second_response.status_code == 200
    assert exchange_calls == [True]
    assert "Google連携を確認できませんでした。" in (
        second_response.get_data(as_text=True)
    )
    with app.app_context():
        assert GoogleCredential.query.count() == 1


def test_oauth_attempt_state_can_only_be_consumed_once(app):
    store = app.extensions["google_oauth_attempt_store"]
    store.save(
        "one-time-state",
        TEST_CODE_VERIFIER,
        TEST_REDIRECT_URI,
    )

    assert store.consume("one-time-state") is not None
    assert store.consume("one-time-state") is None


def test_cancelled_callback_does_not_return_500(client):
    with client.session_transaction() as session:
        session[routes.GOOGLE_OAUTH_STATE_KEY] = "expected-state"
    response = client.get(
        "/integrations/google/callback",
        query_string={"error": "access_denied", "state": "expected-state"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Google連携がキャンセルされました。" in response.get_data(
        as_text=True
    )


def test_failed_callback_does_not_return_500(client, app, monkeypatch):
    class FailingGoogleOAuthService:
        def exchange_callback(
            self,
            authorization_response,
            state,
            code_verifier,
            authorization_redirect_uri,
        ):
            raise GoogleOAuthError(
                "token_exchange",
                RuntimeError("mocked failure"),
            )

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FailingGoogleOAuthService(),
    )
    prepare_oauth_callback(client)

    response = client.get(
        "/integrations/google/callback",
        query_string={"code": "bad-code", "state": "expected-state"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Google連携を完了できませんでした。" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert GoogleCredential.query.count() == 0


def test_invalid_grant_is_classified_without_exposing_description(
    client,
    monkeypatch,
    caplog,
):
    secret_fragments = (
        "secret-authorization-code",
        "secret-access-token",
        "secret-client-secret",
    )

    class FailingGoogleOAuthService:
        def exchange_callback(
            self,
            authorization_response,
            state,
            code_verifier,
            authorization_redirect_uri,
        ):
            raise GoogleOAuthError(
                "token_exchange",
                InvalidGrantError(
                    description=(
                        "PKCE code verifier mismatch "
                        + " ".join(secret_fragments)
                    )
                ),
            )

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FailingGoogleOAuthService(),
    )
    prepare_oauth_callback(client)
    caplog.set_level(logging.ERROR)

    response = client.get(
        "/integrations/google/callback",
        query_string={
            "code": secret_fragments[0],
            "state": "expected-state",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "oauth_error=invalid_grant" in caplog.text
    assert "classification=pkce_verifier_mismatch" in caplog.text
    assert "http_status=400" in caplog.text
    for secret in secret_fragments:
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("description", "classification"),
    [
        ("Authorization code expired", "code_invalid_or_expired"),
        ("Authorization code already redeemed", "code_already_used"),
        (
            "redirect_uri does not match the original request",
            "redirect_uri_mismatch_during_exchange",
        ),
        ("Code verifier did not match PKCE challenge", "pkce_verifier_mismatch"),
        ("Bad Request", "unknown_invalid_grant"),
    ],
)
def test_invalid_grant_description_is_safely_classified(
    description,
    classification,
):
    from app.integrations.diagnostics import classify_invalid_grant

    error = InvalidGrantError(description=description)

    assert classify_invalid_grant(error) == classification


def test_missing_new_refresh_token_preserves_existing_value(app):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        store.save_calendar_credential(
            fake_credentials(refresh_token="original-refresh")
        )
        store.save_calendar_credential(
            fake_credentials(
                token="new-access",
                refresh_token=None,
            )
        )
        credential = store.get_calendar_credential()
        assert credential.access_token == "new-access"
        assert credential.refresh_token == "original-refresh"


def test_first_save_without_refresh_token_is_supported(app):
    with app.app_context():
        store = GoogleCredentialStore("test-user")
        store.save_calendar_credential(fake_credentials(refresh_token=None))

        credential = store.get_calendar_credential()
        assert credential.access_token == "access-token"
        assert credential.refresh_token is None


def test_granted_scopes_are_saved_as_json(app):
    granted_scopes = [
        "openid",
        google_service.GOOGLE_USERINFO_EMAIL_SCOPE,
        "https://www.googleapis.com/auth/calendar.events",
    ]
    with app.app_context():
        credential = GoogleCredentialStore(
            "test-user"
        ).save_calendar_credential(
            fake_credentials(granted_scopes=granted_scopes)
        )

        assert json.loads(credential.scopes) == granted_scopes
        assert credential.expires_at.tzinfo is None


def test_space_delimited_granted_scopes_are_saved_as_json(app):
    credentials = fake_credentials()
    credentials.granted_scopes = " ".join(
        google_service.DEFAULT_GOOGLE_SCOPES
    )
    with app.app_context():
        credential = GoogleCredentialStore(
            "test-user"
        ).save_calendar_credential(credentials)

        assert json.loads(credential.scopes) == list(
            google_service.DEFAULT_GOOGLE_SCOPES
        )


def test_safe_diagnostics_extracts_available_http_status():
    error = RuntimeError("sensitive response body")
    error.resp = SimpleNamespace(status=400)

    assert get_http_status(error) == 400


def test_commit_failure_rolls_back_without_partial_record(app, monkeypatch):
    with app.app_context():
        rollback_calls = []
        original_commit = db.session.commit
        original_rollback = db.session.rollback

        def fail_commit():
            raise RuntimeError("database commit failed")

        def track_rollback():
            rollback_calls.append(True)
            return original_rollback()

        monkeypatch.setattr(db.session, "commit", fail_commit)
        monkeypatch.setattr(db.session, "rollback", track_rollback)

        with pytest.raises(CredentialStorageError) as error_info:
            GoogleCredentialStore(
                "test-user"
            ).save_calendar_credential(fake_credentials())

        assert error_info.value.stage == "db_commit"
        assert rollback_calls == [True]

        monkeypatch.setattr(db.session, "commit", original_commit)
        monkeypatch.setattr(db.session, "rollback", original_rollback)
        assert GoogleCredential.query.count() == 0


def test_disconnect_is_post_only(client, app):
    with app.app_context():
        save_connected_credential()
    assert client.get("/integrations/google/disconnect").status_code == 405


def test_disconnect_deletes_saved_credentials(client, app):
    with app.app_context():
        save_connected_credential()
    response = client.post(
        "/integrations/google/disconnect",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Googleカレンダー連携を解除しました。" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert GoogleCredential.query.count() == 0


def extract_csrf_token(html):
    match = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_disconnect_form_works_with_csrf_enabled():
    class CSRFIntegrationConfig(TestConfig):
        WTF_CSRF_ENABLED = True
        SECRET_KEY = "integration-csrf-secret"

    test_app = create_app(CSRFIntegrationConfig)
    client = test_app.test_client()
    with test_app.app_context():
        db.create_all()
        save_connected_credential()

        assert client.post("/integrations/google/disconnect").status_code == 400

        html = client.get("/settings/integrations").get_data(as_text=True)
        token = extract_csrf_token(html)
        response = client.post(
            "/integrations/google/disconnect",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert GoogleCredential.query.count() == 0

        db.session.remove()
        db.drop_all()


def test_email_lookup_is_mocked(monkeypatch):
    class FakeRequest:
        def execute(self):
            return {"email": "mocked@example.com"}

    class FakeUserInfo:
        def get(self):
            return FakeRequest()

    class FakeOAuth2Service:
        def userinfo(self):
            return FakeUserInfo()

    monkeypatch.setattr(
        google_service,
        "build",
        lambda *args, **kwargs: FakeOAuth2Service(),
    )
    assert (
        GoogleOAuthService(SimpleNamespace())._fetch_email(fake_credentials())
        == "mocked@example.com"
    )


def test_email_lookup_failure_is_logged_and_does_not_abort(
    monkeypatch,
    caplog,
):
    secret_fragments = (
        "secret-access-token",
        "secret-authorization-code",
    )

    def fail_build(*args, **kwargs):
        raise RuntimeError(" ".join(secret_fragments))

    monkeypatch.setattr(google_service, "build", fail_build)
    caplog.set_level(logging.WARNING, logger=google_service.__name__)

    email = GoogleOAuthService(SimpleNamespace())._fetch_email(
        fake_credentials(token=secret_fragments[0])
    )

    assert email is None
    assert "stage=account_email" in caplog.text
    assert "exception=RuntimeError" in caplog.text
    for secret in secret_fragments:
        assert secret not in caplog.text


def test_callback_failure_log_does_not_expose_secrets(
    client,
    monkeypatch,
    caplog,
):
    secret_fragments = (
        "secret-access-token",
        "secret-authorization-code",
        "secret-client-secret",
    )

    class FailingGoogleOAuthService:
        def exchange_callback(
            self,
            authorization_response,
            state,
            code_verifier,
            authorization_redirect_uri,
        ):
            raise GoogleOAuthError(
                "token_exchange",
                RuntimeError(" ".join(secret_fragments)),
            )

    monkeypatch.setattr(
        routes,
        "get_google_oauth_service",
        lambda connection_type="calendar": FailingGoogleOAuthService(),
    )
    prepare_oauth_callback(client)
    caplog.set_level(logging.ERROR)

    response = client.get(
        "/integrations/google/callback",
        query_string={
            "code": secret_fragments[1],
            "state": "expected-state",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "stage=token_exchange" in caplog.text
    assert "exception=RuntimeError" in caplog.text
    for secret in secret_fragments:
        assert secret not in caplog.text


def test_existing_features_still_load(client):
    assert client.get("/").status_code == 200
    assert client.get("/applications").status_code == 200
