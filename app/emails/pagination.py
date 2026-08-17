import base64
import json


MAX_HISTORY_ITEMS = 20
MAX_ENCODED_HISTORY_CHARS = 8_192
MAX_TOKEN_CHARS = 2_048


def encode_page_history(tokens):
    safe_tokens = [
        token
        for token in tokens[-MAX_HISTORY_ITEMS:]
        if isinstance(token, str) and len(token) <= MAX_TOKEN_CHARS
    ]
    payload = json.dumps(safe_tokens, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_page_history(value):
    if not value:
        return []
    if not isinstance(value, str) or len(value) > MAX_ENCODED_HISTORY_CHARS:
        return []
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        tokens = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(tokens, list):
        return []
    return [
        token
        for token in tokens[-MAX_HISTORY_ITEMS:]
        if isinstance(token, str) and len(token) <= MAX_TOKEN_CHARS
    ]
