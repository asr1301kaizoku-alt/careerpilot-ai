from urllib.parse import parse_qsl, urlencode, urlsplit

from .pagination import (
    MAX_ENCODED_HISTORY_CHARS,
    MAX_TOKEN_CHARS,
    decode_page_history,
    encode_page_history,
)


EMAIL_LIST_PATH = "/emails/"
ALLOWED_RETURN_PATHS = {"/emails", EMAIL_LIST_PATH}
ALLOWED_RETURN_PARAMETERS = {"q", "page_token", "history"}
MAX_QUERY_CHARS = 500
MAX_RETURN_URL_CHARS = 16_384


def build_email_list_url(query="", page_token=None, history=None):
    parameters = []
    normalized_query = str(query or "").strip()
    if normalized_query:
        parameters.append(("q", normalized_query[:MAX_QUERY_CHARS]))
    if page_token:
        parameters.append(("page_token", str(page_token)))
    if history:
        parameters.append(("history", encode_page_history(history)))
    query_string = urlencode(parameters)
    return EMAIL_LIST_PATH + (f"?{query_string}" if query_string else "")


def safe_email_list_return_url(value):
    """Accept only a canonical local Gmail list URL, never an external URL."""

    if not isinstance(value, str) or not value:
        return EMAIL_LIST_PATH
    if len(value) > MAX_RETURN_URL_CHARS or _contains_control_or_backslash(value):
        return EMAIL_LIST_PATH

    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or parsed.path not in ALLOWED_RETURN_PATHS
    ):
        return EMAIL_LIST_PATH

    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=len(ALLOWED_RETURN_PARAMETERS),
        )
    except ValueError:
        return EMAIL_LIST_PATH
    if any(key not in ALLOWED_RETURN_PARAMETERS for key, _ in pairs):
        return EMAIL_LIST_PATH
    if len({key for key, _ in pairs}) != len(pairs):
        return EMAIL_LIST_PATH

    parameters = dict(pairs)
    query = parameters.get("q", "").strip()
    page_token = parameters.get("page_token") or None
    encoded_history = parameters.get("history") or None
    if len(query) > MAX_QUERY_CHARS:
        return EMAIL_LIST_PATH
    if page_token and len(page_token) > MAX_TOKEN_CHARS:
        return EMAIL_LIST_PATH
    if encoded_history and len(encoded_history) > MAX_ENCODED_HISTORY_CHARS:
        return EMAIL_LIST_PATH

    history = decode_page_history(encoded_history)
    if encoded_history and not history:
        return EMAIL_LIST_PATH
    return build_email_list_url(query, page_token, history)


def _contains_control_or_backslash(value):
    return "\\" in value or any(ord(character) < 32 for character in value)
