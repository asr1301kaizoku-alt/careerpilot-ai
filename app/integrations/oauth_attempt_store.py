from dataclasses import dataclass
from threading import Lock
from time import monotonic


@dataclass(frozen=True)
class OAuthAttempt:
    code_verifier: str
    authorization_redirect_uri: str
    connection_type: str
    expires_at: float


class OAuthAttemptStore:
    """Process-local, one-time storage for short-lived OAuth attempts."""

    def __init__(self, ttl_seconds=600):
        self.ttl_seconds = ttl_seconds
        self._attempts = {}
        self._lock = Lock()

    def save(
        self,
        state,
        code_verifier,
        authorization_redirect_uri,
        connection_type="calendar",
    ):
        if not state or not code_verifier:
            raise ValueError("OAuth attempt data is incomplete.")
        with self._lock:
            self._remove_expired()
            self._attempts[state] = OAuthAttempt(
                code_verifier=code_verifier,
                authorization_redirect_uri=authorization_redirect_uri,
                connection_type=connection_type,
                expires_at=monotonic() + self.ttl_seconds,
            )

    def consume(self, state):
        if not state:
            return None
        with self._lock:
            self._remove_expired()
            return self._attempts.pop(state, None)

    def discard(self, state):
        if not state:
            return
        with self._lock:
            self._attempts.pop(state, None)

    def _remove_expired(self):
        now = monotonic()
        expired_states = [
            state
            for state, attempt in self._attempts.items()
            if attempt.expires_at <= now
        ]
        for state in expired_states:
            self._attempts.pop(state, None)
