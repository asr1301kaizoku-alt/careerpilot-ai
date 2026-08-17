import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_int(name, default, minimum=0, maximum=86_400):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


class Config:
    APP_ENV = os.getenv("FLASK_ENV", "development")
    ALLOW_INSECURE_OAUTH = os.getenv("ALLOW_INSECURE_OAUTH", "false")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'career_pilot.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_CREATE_DATABASE = False
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:5000/integrations/google/callback",
    )
    GOOGLE_OAUTH_SCOPES = os.getenv(
        "GOOGLE_OAUTH_SCOPES",
        "openid https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/calendar.events",
    )
    GOOGLE_GMAIL_REDIRECT_URI = os.getenv(
        "GOOGLE_GMAIL_REDIRECT_URI",
        "http://127.0.0.1:5000/integrations/google/gmail/callback",
    )
    GOOGLE_GMAIL_OAUTH_SCOPES = os.getenv(
        "GOOGLE_GMAIL_OAUTH_SCOPES",
        "openid https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    GMAIL_LIST_CACHE_TTL_SECONDS = _env_int(
        "GMAIL_LIST_CACHE_TTL_SECONDS", 60
    )
    GMAIL_LIST_CACHE_MAX_ENTRIES = _env_int(
        "GMAIL_LIST_CACHE_MAX_ENTRIES", 128, minimum=1, maximum=10_000
    )
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    GEMINI_TIMEOUT_SECONDS = _env_int(
        "GEMINI_TIMEOUT_SECONDS", 30, minimum=5, maximum=120
    )
    EMAIL_ANALYSIS_SESSION_TTL_SECONDS = _env_int(
        "EMAIL_ANALYSIS_SESSION_TTL_SECONDS",
        600,
        minimum=60,
        maximum=3_600,
    )
    EMAIL_ANALYSIS_SESSION_MAX_ENTRIES = _env_int(
        "EMAIL_ANALYSIS_SESSION_MAX_ENTRIES",
        128,
        minimum=1,
        maximum=10_000,
    )
    EMAIL_ANALYSIS_SESSION_MAX_PAYLOAD_BYTES = _env_int(
        "EMAIL_ANALYSIS_SESSION_MAX_PAYLOAD_BYTES",
        32_768,
        minimum=1_024,
        maximum=1_048_576,
    )
    OAUTH_OWNER_KEY = os.getenv("OAUTH_OWNER_KEY", "local")


class TestConfig(Config):
    APP_ENV = "testing"
    ALLOW_INSECURE_OAUTH = False
    TESTING = True
    WTF_CSRF_ENABLED = False
    AUTO_CREATE_DATABASE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    GOOGLE_CLIENT_ID = ""
    GOOGLE_CLIENT_SECRET = ""
    GOOGLE_REDIRECT_URI = "http://127.0.0.1:5000/integrations/google/callback"
    GOOGLE_OAUTH_SCOPES = (
        "openid https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/calendar.events"
    )
    GOOGLE_GMAIL_REDIRECT_URI = (
        "http://127.0.0.1:5000/integrations/google/gmail/callback"
    )
    GOOGLE_GMAIL_OAUTH_SCOPES = (
        "openid https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/gmail.readonly"
    )
    GMAIL_LIST_CACHE_TTL_SECONDS = 60
    GMAIL_LIST_CACHE_MAX_ENTRIES = 128
    GEMINI_API_KEY = ""
    GEMINI_MODEL = "gemini-3.6-flash"
    GEMINI_TIMEOUT_SECONDS = 30
    EMAIL_ANALYSIS_SESSION_TTL_SECONDS = 600
    EMAIL_ANALYSIS_SESSION_MAX_ENTRIES = 128
    EMAIL_ANALYSIS_SESSION_MAX_PAYLOAD_BYTES = 32_768
    OAUTH_OWNER_KEY = "test-user"
