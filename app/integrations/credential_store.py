import json
from datetime import timezone

from google.oauth2.credentials import Credentials

from app.extensions import db
from app.models import GoogleCredential, JST


GOOGLE_PROVIDER = GoogleCredential.PROVIDER_GOOGLE
GOOGLE_CONNECTION_CALENDAR = GoogleCredential.CONNECTION_CALENDAR
GOOGLE_CONNECTION_GMAIL = GoogleCredential.CONNECTION_GMAIL
GOOGLE_CONNECTION_TYPES = GoogleCredential.CONNECTION_TYPES
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class CredentialStorageError(RuntimeError):
    def __init__(self, stage, original_error):
        super().__init__("Google credential storage failed.")
        self.stage = stage
        self.original_error = original_error


class PlaintextTokenProtector:
    """Development-only token protector.

    Tokens are intentionally isolated behind this interface so production can
    replace it with authenticated encryption or a managed secret store.
    """

    def protect(self, value):
        return value

    def unprotect(self, value):
        return value


def _to_jst_naive(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JST).replace(tzinfo=None)


def _to_utc_naive(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _scope_list(credentials):
    scopes = getattr(credentials, "granted_scopes", None) or getattr(
        credentials,
        "scopes",
        None,
    )
    if isinstance(scopes, str):
        return scopes.split()
    return list(scopes or [])


class GoogleCredentialStore:
    def __init__(self, owner_key, protector=None):
        self.owner_key = owner_key
        self.protector = protector or PlaintextTokenProtector()

    @staticmethod
    def _validate_connection_type(connection_type):
        if connection_type not in GOOGLE_CONNECTION_TYPES:
            raise ValueError("Unsupported Google credential connection type.")
        return connection_type

    def get(self, connection_type):
        connection_type = self._validate_connection_type(connection_type)
        return db.session.scalar(
            db.select(GoogleCredential).where(
                GoogleCredential.owner_key == self.owner_key,
                GoogleCredential.provider == GOOGLE_PROVIDER,
                GoogleCredential.connection_type == connection_type,
            )
        )

    def get_calendar_credential(self):
        return self.get(GOOGLE_CONNECTION_CALENDAR)

    def get_gmail_credential(self):
        return self.get(GOOGLE_CONNECTION_GMAIL)

    def save(self, credentials, connection_type, email=None):
        connection_type = self._validate_connection_type(connection_type)
        if not credentials.token:
            raise CredentialStorageError(
                "credential_validation",
                ValueError("Google access token is missing."),
            )

        try:
            record = self.get(connection_type)
            if record is None:
                record = GoogleCredential(
                    owner_key=self.owner_key,
                    provider=GOOGLE_PROVIDER,
                    connection_type=connection_type,
                    access_token="",
                    token_uri=GOOGLE_TOKEN_URI,
                    scopes="[]",
                )
                db.session.add(record)

            record.google_account_email = email or record.google_account_email
            record.access_token = self.protector.protect(credentials.token)
            if credentials.refresh_token:
                record.refresh_token = self.protector.protect(
                    credentials.refresh_token
                )
            record.token_uri = credentials.token_uri or GOOGLE_TOKEN_URI
            record.scopes = json.dumps(
                _scope_list(credentials),
                ensure_ascii=False,
            )
            record.expires_at = _to_jst_naive(credentials.expiry)
        except Exception as error:
            db.session.rollback()
            raise CredentialStorageError(
                "credential_upsert",
                error,
            ) from error

        try:
            db.session.commit()
        except Exception as error:
            db.session.rollback()
            raise CredentialStorageError(
                "db_commit",
                error,
            ) from error
        return record

    def save_calendar_credential(self, credentials, email=None):
        return self.save(
            credentials,
            GOOGLE_CONNECTION_CALENDAR,
            email=email,
        )

    def save_gmail_credential(self, credentials, email=None):
        return self.save(
            credentials,
            GOOGLE_CONNECTION_GMAIL,
            email=email,
        )

    def delete(self, connection_type):
        record = self.get(connection_type)
        if record is None:
            return False
        db.session.delete(record)
        db.session.commit()
        return True

    def delete_calendar_credential(self):
        return self.delete(GOOGLE_CONNECTION_CALENDAR)

    def delete_gmail_credential(self):
        return self.delete(GOOGLE_CONNECTION_GMAIL)

    def to_google_credentials(self, record, client_id, client_secret):
        credentials = Credentials(
            token=self.protector.unprotect(record.access_token),
            refresh_token=(
                self.protector.unprotect(record.refresh_token)
                if record.refresh_token
                else None
            ),
            token_uri=record.token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=json.loads(record.scopes or "[]"),
        )
        credentials.expiry = _to_utc_naive(record.expires_at)
        return credentials
