import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from app.models import JST


@dataclass(frozen=True)
class GmailListCacheEntry:
    page: object
    fetched_at: datetime
    expires_at: float


class GmailListCache:
    """Thread-safe, bounded process-local cache for Gmail list results."""

    def __init__(self, ttl_seconds=60, max_entries=128, clock=None):
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or time.monotonic
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key):
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry

    def set(self, key, page, fetched_at=None):
        if self.ttl_seconds <= 0:
            return None
        now = self._clock()
        entry = GmailListCacheEntry(
            page=page,
            fetched_at=fetched_at or datetime.now(JST),
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._purge_expired(now)
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return entry

    def delete(self, key):
        with self._lock:
            self._entries.pop(key, None)

    def clear(self):
        with self._lock:
            self._entries.clear()

    def __len__(self):
        with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)

    def _purge_expired(self, now):
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)


def make_gmail_list_cache_key(
    owner_key,
    account_email,
    query,
    page_token,
    credential_id=None,
):
    """Return a fixed-size key without exposing account or page token values."""

    components = (
        str(owner_key or ""),
        str(account_email or "").strip().casefold(),
        str(credential_id or ""),
        str(query or "").strip(),
        str(page_token or ""),
    )
    digest = hashlib.sha256(
        "\x00".join(components).encode("utf-8", errors="replace")
    ).hexdigest()
    return f"gmail-list:{digest}"
