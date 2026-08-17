import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace

from app.services.email_ai_service import EmailAnalysisResult


@dataclass(frozen=True)
class EmailAnalysisApplyEntry:
    message_id: str
    result: EmailAnalysisResult
    return_to: str
    expires_at: float
    application_id: int | None = None
    analysis_session_token: str | None = None


class EmailAnalysisApplyStore:
    """Bounded, process-local storage for one-time AI review candidates."""

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
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def save(
        self,
        message_id,
        result,
        return_to,
        analysis_session_token=None,
    ):
        normalized_message_id = str(message_id or "").strip()
        normalized_return_to = str(return_to or "").strip()
        if not normalized_message_id or not normalized_return_to:
            raise ValueError("Analysis review context is incomplete.")
        if not isinstance(result, EmailAnalysisResult):
            raise ValueError("Analysis review result is invalid.")
        if analysis_session_token is not None:
            analysis_session_token = self._normalize_token(
                analysis_session_token
            )
            if analysis_session_token is None:
                raise ValueError("Analysis session token is invalid.")
        payload = {
            "message_id": normalized_message_id,
            "return_to": normalized_return_to,
            "result": asdict(result),
            "analysis_session_token": analysis_session_token,
        }
        payload_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if payload_size > self.max_payload_bytes:
            raise ValueError("Analysis review context is too large.")

        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            token = self._new_token()
            self._entries[token] = EmailAnalysisApplyEntry(
                message_id=normalized_message_id,
                result=result,
                return_to=normalized_return_to,
                expires_at=now + self.ttl_seconds,
                analysis_session_token=analysis_session_token,
            )
            self._entries.move_to_end(token)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return token

    def bind_application(self, token, message_id, application_id):
        normalized_token = self._normalize_token(token)
        if normalized_token is None or not isinstance(application_id, int):
            return None
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(normalized_token)
            if entry is None or not self._matches_message(entry, message_id):
                return None
            bound = replace(entry, application_id=application_id)
            self._entries[normalized_token] = bound
            self._entries.move_to_end(normalized_token)
            return bound

    def get(self, token, message_id=None):
        normalized_token = self._normalize_token(token)
        if normalized_token is None:
            return None
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(normalized_token)
            if entry is None or not self._matches_message(entry, message_id):
                return None
            self._entries.move_to_end(normalized_token)
            return entry

    def consume(self, token, message_id=None):
        normalized_token = self._normalize_token(token)
        if normalized_token is None:
            return None
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(normalized_token)
            if entry is None or not self._matches_message(entry, message_id):
                return None
            self._entries.pop(normalized_token, None)
            return entry

    def __len__(self):
        with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)

    def _new_token(self):
        for _ in range(5):
            token = self._normalize_token(self._token_factory())
            if token and token not in self._entries:
                return token
        raise RuntimeError("Could not allocate an analysis review token.")

    @staticmethod
    def _normalize_token(token):
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        return token

    @staticmethod
    def _matches_message(entry, message_id):
        return message_id is None or entry.message_id == str(message_id)

    def _purge_expired(self, now):
        expired = [
            token
            for token, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for token in expired:
            self._entries.pop(token, None)
