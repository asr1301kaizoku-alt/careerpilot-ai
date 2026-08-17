import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace

from app.models import GoogleCredential
from app.services.email_ai_service import EmailAnalysisResult


@dataclass(frozen=True)
class EmailAnalysisSessionState:
    application_completed: bool = False
    application_id: int | None = None
    checklist_completed: bool = False
    checklist_application_id: int | None = None
    checklist_count: int = 0
    calendar_attempted: bool = False
    calendar_created_count: int = 0
    calendar_failed_count: int = 0


@dataclass(frozen=True)
class EmailAnalysisSessionEntry:
    message_key: str
    connection_key: str
    result: EmailAnalysisResult
    return_to: str
    expires_at: float
    state: EmailAnalysisSessionState = field(
        default_factory=EmailAnalysisSessionState
    )


class EmailAnalysisSessionStore:
    """Bounded, reusable, process-local storage for structured AI results."""

    def __init__(
        self,
        ttl_seconds=600,
        max_entries=128,
        max_payload_bytes=32_768,
        clock=None,
        token_factory=None,
    ):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.max_payload_bytes = max(1, int(max_payload_bytes))
        self._clock = clock or time.monotonic
        self._token_factory = token_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def save(self, message_id, connection_key, result, return_to):
        message_key = self.message_key(message_id)
        normalized_connection_key = self._normalize_connection_key(
            connection_key
        )
        normalized_return_to = str(return_to or "").strip()
        if not message_key or not normalized_connection_key or not normalized_return_to:
            raise ValueError("Analysis session context is incomplete.")
        if not isinstance(result, EmailAnalysisResult):
            raise ValueError("Analysis session result is invalid.")
        payload = {
            "message_key": message_key,
            "connection_key": normalized_connection_key,
            "return_to": normalized_return_to,
            "result": asdict(result),
        }
        payload_size = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if payload_size > self.max_payload_bytes:
            raise ValueError("Analysis session context is too large.")

        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            token = self._new_token()
            self._entries[token] = EmailAnalysisSessionEntry(
                message_key=message_key,
                connection_key=normalized_connection_key,
                result=result,
                return_to=normalized_return_to,
                expires_at=now + self.ttl_seconds,
            )
            self._entries.move_to_end(token)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return token

    def get(self, token, message_id, connection_key):
        normalized_token = self._normalize_token(token)
        normalized_connection_key = self._normalize_connection_key(
            connection_key
        )
        if normalized_token is None or normalized_connection_key is None:
            return None
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(normalized_token)
            if entry is None:
                return None
            if entry.message_key != self.message_key(message_id):
                return None
            if entry.connection_key != normalized_connection_key:
                return None
            self._entries.move_to_end(normalized_token)
            return entry

    def mark_application(self, token, message_id, connection_key, application_id):
        return self._replace_state(
            token,
            message_id,
            connection_key,
            application_completed=True,
            application_id=application_id,
        )

    def mark_checklist(
        self,
        token,
        message_id,
        connection_key,
        application_id,
        count,
    ):
        return self._replace_state(
            token,
            message_id,
            connection_key,
            checklist_completed=True,
            checklist_application_id=application_id,
            checklist_count=max(0, int(count)),
        )

    def mark_calendar(
        self,
        token,
        message_id,
        connection_key,
        created_count,
        failed_count,
    ):
        normalized_token = self._normalize_token(token)
        if normalized_token is None:
            return None
        with self._lock:
            entry = self.get(
                normalized_token,
                message_id,
                connection_key,
            )
            if entry is None:
                return None
            updated = replace(
                entry,
                state=replace(
                    entry.state,
                    calendar_attempted=True,
                    calendar_created_count=max(
                        entry.state.calendar_created_count,
                        max(0, int(created_count)),
                    ),
                    calendar_failed_count=max(0, int(failed_count)),
                ),
            )
            self._entries[normalized_token] = updated
            self._entries.move_to_end(normalized_token)
            return updated

    def __len__(self):
        with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)

    def _replace_state(self, token, message_id, connection_key, **changes):
        normalized_token = self._normalize_token(token)
        if normalized_token is None:
            return None
        with self._lock:
            entry = self.get(
                normalized_token,
                message_id,
                connection_key,
            )
            if entry is None:
                return None
            updated = replace(
                entry,
                state=replace(entry.state, **changes),
            )
            self._entries[normalized_token] = updated
            self._entries.move_to_end(normalized_token)
            return updated

    def _new_token(self):
        for _ in range(5):
            token = self._normalize_token(self._token_factory())
            if token and token not in self._entries:
                return token
        raise RuntimeError("Could not allocate an analysis session token.")

    @staticmethod
    def message_key(message_id):
        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        return _digest(f"gmail-message:{normalized}")

    @staticmethod
    def _normalize_connection_key(connection_key):
        if not isinstance(connection_key, str):
            return None
        normalized = connection_key.strip()
        if len(normalized) != 64:
            return None
        return normalized

    @staticmethod
    def _normalize_token(token):
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        return token

    def _purge_expired(self, now):
        expired = [
            token
            for token, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for token in expired:
            self._entries.pop(token, None)


def gmail_connection_key(owner_key, credential):
    account = str(
        getattr(credential, "google_account_email", "") or ""
    ).strip().casefold()
    if not account:
        account = "gmail-account-unavailable"
    return _digest(
        "|".join(
            (
                str(owner_key or "").strip(),
                GoogleCredential.PROVIDER_GOOGLE,
                GoogleCredential.CONNECTION_GMAIL,
                account,
            )
        )
    )


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
