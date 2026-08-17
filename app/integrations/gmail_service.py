import base64
import binascii
import codecs
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.models import JST

from .credential_store import CredentialStorageError


GMAIL_USER_ID = "me"
GMAIL_MAX_RESULTS = 50
GMAIL_RECENT_QUERY = "newer_than:90d"
MAX_SNIPPET_CHARS = 240
MAX_BODY_CHARS = 20_000
MAX_DECODED_BODY_BYTES = 80_000
MAX_PAGE_TOKEN_CHARS = 2_048
MAX_MESSAGE_ID_CHARS = 256
MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
BODY_DECODE_ERROR_MESSAGE = "本文を正しく表示できませんでした。"
FALLBACK_CHARSETS = ("utf-8", "iso2022_jp", "cp932", "euc_jp")
CHARSET_ALIASES = {
    "utf8": "utf-8",
    "utf_8": "utf-8",
    "iso-2022-jp": "iso2022_jp",
    "iso2022-jp": "iso2022_jp",
    "iso2022jp": "iso2022_jp",
    "shift-jis": "shift_jis",
    "shift_jis": "shift_jis",
    "sjis": "shift_jis",
    "windows-31j": "cp932",
    "windows31j": "cp932",
    "ms932": "cp932",
    "cp932": "cp932",
    "euc-jp": "euc_jp",
    "euc_jp": "euc_jp",
    "eucjp": "euc_jp",
}


class GmailServiceError(RuntimeError):
    def __init__(self, stage, original_error):
        super().__init__("Gmail API operation failed.")
        self.stage = stage
        self.original_error = original_error


@dataclass(frozen=True)
class GmailMessageSummary:
    message_id: str
    subject: str
    sender: str
    received_at: datetime | None
    date_header: str
    snippet: str


@dataclass(frozen=True)
class GmailMessageDetail:
    message_id: str
    subject: str
    sender: str
    recipient: str
    received_at: datetime | None
    date_header: str
    snippet: str
    body_text: str


@dataclass(frozen=True)
class GmailMessagePage:
    messages: tuple[GmailMessageSummary, ...]
    next_page_token: str | None


class _SafeHTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "p",
        "section",
        "table",
        "tr",
    }
    IGNORED_TAGS = {"head", "script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []
        self.ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        normalized = tag.lower()
        if normalized in self.IGNORED_TAGS:
            self.ignored_depth += 1
        elif not self.ignored_depth and normalized in self.BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_endtag(self, tag):
        normalized = tag.lower()
        if normalized in self.IGNORED_TAGS:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif not self.ignored_depth and normalized in self.BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data):
        if not self.ignored_depth:
            self.fragments.append(data)

    def text(self):
        return "".join(self.fragments)


class GoogleGmailService:
    def __init__(self, credential_store, client_id, client_secret):
        self.credential_store = credential_store
        self.client_id = client_id
        self.client_secret = client_secret

    def list_messages(self, query="", page_token=None):
        if page_token and (
            not isinstance(page_token, str)
            or len(page_token) > MAX_PAGE_TOKEN_CHARS
        ):
            raise GmailServiceError(
                "gmail_page_token_validation",
                ValueError("Gmail page token is invalid."),
            )

        gmail = self._gmail_client()
        parameters = {
            "userId": GMAIL_USER_ID,
            "maxResults": GMAIL_MAX_RESULTS,
            "q": build_gmail_search_query(query),
            "fields": "messages/id,nextPageToken",
        }
        if page_token:
            parameters["pageToken"] = page_token

        try:
            response = gmail.users().messages().list(**parameters).execute()
        except Exception as error:
            raise GmailServiceError("gmail_messages_list", error) from error

        references = response.get("messages", []) if response else []
        messages = []
        for reference in references[:GMAIL_MAX_RESULTS]:
            message_id = (
                reference.get("id")
                if isinstance(reference, dict)
                else None
            )
            if not _is_valid_message_id(message_id):
                continue
            try:
                resource = (
                    gmail.users()
                    .messages()
                    .get(
                        userId=GMAIL_USER_ID,
                        id=message_id,
                        format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                        fields="id,internalDate,snippet,payload/headers",
                    )
                    .execute()
                )
            except Exception as error:
                raise GmailServiceError(
                    "gmail_message_metadata",
                    error,
                ) from error
            messages.append(parse_message_summary(resource))

        next_page_token = response.get("nextPageToken") if response else None
        return GmailMessagePage(
            messages=tuple(messages),
            next_page_token=next_page_token,
        )

    def get_message(self, message_id):
        if not _is_valid_message_id(message_id):
            raise GmailServiceError(
                "gmail_message_id_validation",
                ValueError("Gmail message ID is invalid."),
            )

        gmail = self._gmail_client()
        try:
            resource = (
                gmail.users()
                .messages()
                .get(
                    userId=GMAIL_USER_ID,
                    id=message_id,
                    format="full",
                )
                .execute()
            )
        except Exception as error:
            raise GmailServiceError("gmail_message_get", error) from error
        return parse_message_detail(resource)

    def _gmail_client(self):
        credentials = self._current_credentials()
        try:
            return build(
                "gmail",
                "v1",
                credentials=credentials,
                cache_discovery=False,
            )
        except Exception as error:
            raise GmailServiceError("gmail_client_build", error) from error

    def _current_credentials(self):
        record = self.credential_store.get_gmail_credential()
        if record is None:
            raise GmailServiceError(
                "gmail_authentication",
                RuntimeError("Gmail credential is not connected."),
            )

        try:
            credentials = self.credential_store.to_google_credentials(
                record,
                self.client_id,
                self.client_secret,
            )
            if credentials.expired:
                if not credentials.refresh_token:
                    raise RuntimeError("Google refresh token is unavailable.")
                credentials.refresh(Request())
                self.credential_store.save_gmail_credential(
                    credentials,
                    email=record.google_account_email,
                )
        except CredentialStorageError as error:
            raise GmailServiceError(error.stage, error.original_error) from error
        except Exception as error:
            raise GmailServiceError("gmail_credential_refresh", error) from error
        return credentials


def build_gmail_search_query(query):
    query = str(query or "").strip()
    if not query:
        return GMAIL_RECENT_QUERY
    return f"{GMAIL_RECENT_QUERY} ({query})"


def parse_message_summary(resource):
    resource = resource if isinstance(resource, dict) else {}
    headers = _headers(resource.get("payload"))
    return GmailMessageSummary(
        message_id=str(resource.get("id") or ""),
        subject=_header_value(headers, "Subject") or "（件名なし）",
        sender=_header_value(headers, "From") or "送信者不明",
        received_at=_received_at(resource.get("internalDate")),
        date_header=_header_value(headers, "Date"),
        snippet=normalize_snippet(resource.get("snippet")),
    )


def parse_message_detail(resource):
    resource = resource if isinstance(resource, dict) else {}
    payload = resource.get("payload") or {}
    headers = _headers(payload)
    return GmailMessageDetail(
        message_id=str(resource.get("id") or ""),
        subject=_header_value(headers, "Subject") or "（件名なし）",
        sender=_header_value(headers, "From") or "送信者不明",
        recipient=_header_value(headers, "To") or "宛先不明",
        received_at=_received_at(resource.get("internalDate")),
        date_header=_header_value(headers, "Date"),
        snippet=normalize_snippet(resource.get("snippet")),
        body_text=extract_body_text(payload),
    )


def extract_body_text(payload):
    plain_parts = []
    html_parts = []
    _collect_body_parts(payload or {}, plain_parts, html_parts)
    if plain_parts:
        body = "\n".join(plain_parts)
    elif html_parts:
        extractor = _SafeHTMLTextExtractor()
        extractor.feed("\n".join(html_parts))
        extractor.close()
        body = extractor.text()
    else:
        body = "本文を取得できませんでした。"
    return _truncate(_normalize_body(body), MAX_BODY_CHARS)


def normalize_snippet(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return _truncate(text, MAX_SNIPPET_CHARS)


def _collect_body_parts(part, plain_parts, html_parts):
    if not isinstance(part, dict):
        return
    body = part.get("body") or {}
    if part.get("filename") or body.get("attachmentId"):
        return

    mime_type = str(part.get("mimeType") or "").lower()
    base_mime_type = mime_type.split(";", 1)[0].strip()
    data = body.get("data")
    if data and base_mime_type == "text/plain":
        decoded = _decode_base64url(data, _part_charset(part))
        if decoded:
            plain_parts.append(decoded)
    elif data and base_mime_type == "text/html":
        decoded = _decode_base64url(
            data,
            _part_charset(part),
            is_html=True,
        )
        if decoded:
            html_parts.append(decoded)

    for child in part.get("parts") or []:
        _collect_body_parts(child, plain_parts, html_parts)


def _decode_base64url(value, charset=None, is_html=False):
    if not isinstance(value, str):
        return ""
    try:
        encoded_limit = ((MAX_DECODED_BODY_BYTES + 2) // 3) * 4
        value = value[:encoded_limit]
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error):
        return ""
    # Gmail API body.data is already the MIME part bytes wrapped in base64url.
    # Content-Transfer-Encoding must not be decoded a second time here.
    decoded = decoded[:MAX_DECODED_BODY_BYTES]
    return _decode_body_bytes(decoded, charset, is_html=is_html)


def _part_charset(part):
    content_type = _header_value(_headers(part), "Content-Type")
    if not content_type:
        content_type = str(part.get("mimeType") or "")
    return _extract_charset(content_type)


def _extract_charset(content_type):
    match = re.search(
        r"charset\s*=\s*[\"']?([^;\s\"']+)",
        str(content_type or ""),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _normalize_charset(charset):
    if not charset:
        return None
    normalized = str(charset).strip().strip("\"'").lower()
    normalized = CHARSET_ALIASES.get(normalized, normalized)
    try:
        return codecs.lookup(normalized).name
    except LookupError:
        return None


def _html_meta_charset(raw_body):
    head = raw_body[:8_192]
    match = re.search(
        rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([a-z0-9._:-]+)",
        head,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).decode("ascii", errors="ignore") or None


def _decode_body_bytes(raw_body, declared_charset=None, is_html=False):
    if not raw_body:
        return ""

    candidates = []
    declared = _normalize_charset(declared_charset)
    if declared:
        candidates.append(declared)
    elif is_html:
        html_charset = _normalize_charset(_html_meta_charset(raw_body))
        if html_charset:
            candidates.append(html_charset)

    fallback_charsets = list(FALLBACK_CHARSETS)
    if re.search(rb"\x1b(?:\$[@B]|\([BJ])", raw_body):
        fallback_charsets.remove("iso2022_jp")
        fallback_charsets.insert(0, "iso2022_jp")

    for charset in fallback_charsets:
        normalized = _normalize_charset(charset)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    for charset in candidates:
        try:
            decoder = codecs.getincrementaldecoder(charset)(errors="strict")
            return decoder.decode(raw_body, final=False)
        except (LookupError, UnicodeDecodeError):
            continue

    replacement_charset = candidates[0] if candidates else "utf-8"
    try:
        decoded = raw_body.decode(replacement_charset, errors="replace")
    except LookupError:
        decoded = raw_body.decode("utf-8", errors="replace")
    if not decoded or decoded.count("\ufffd") * 5 >= len(decoded):
        return BODY_DECODE_ERROR_MESSAGE
    return decoded


def _headers(payload):
    if not isinstance(payload, dict):
        return []
    headers = payload.get("headers") or []
    return [header for header in headers if isinstance(header, dict)]


def _header_value(headers, name):
    target = name.casefold()
    for header in headers:
        if str(header.get("name") or "").casefold() == target:
            return str(header.get("value") or "").strip()
    return ""


def _received_at(internal_date):
    try:
        milliseconds = int(internal_date)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(
        milliseconds / 1000,
        tz=timezone.utc,
    ).astimezone(JST)


def _normalize_body(value):
    value = html.unescape(str(value or "")).replace("\r\n", "\n")
    value = value.replace("\r", "\n")
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in value.split("\n")
    ]
    value = "\n".join(lines)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _truncate(value, limit):
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _is_valid_message_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_MESSAGE_ID_CHARS
        and MESSAGE_ID_PATTERN.fullmatch(value) is not None
    )
