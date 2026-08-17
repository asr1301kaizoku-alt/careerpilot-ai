from flask import Flask

from config import Config

from .extensions import csrf, db, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    from .errors import register_error_handlers

    register_error_handlers(app)

    from .applications import bp as applications_bp
    from .checklists import bp as checklists_bp
    from .emails.analysis_apply_store import EmailAnalysisApplyStore
    from .emails.analysis_session_store import EmailAnalysisSessionStore
    from .emails.cache import GmailListCache
    from .emails import bp as emails_bp
    from .integrations import bp as integrations_bp
    from .integrations.oauth_attempt_store import OAuthAttemptStore
    from .integrations.oauth_transport import configure_oauthlib_transport
    from .main import bp as main_bp

    configure_oauthlib_transport(app.config)
    app.extensions["google_oauth_attempt_store"] = OAuthAttemptStore()
    app.extensions["gmail_list_cache"] = GmailListCache(
        ttl_seconds=app.config["GMAIL_LIST_CACHE_TTL_SECONDS"],
        max_entries=app.config["GMAIL_LIST_CACHE_MAX_ENTRIES"],
    )
    app.extensions["email_analysis_apply_store"] = EmailAnalysisApplyStore()
    app.extensions["email_analysis_checklist_store"] = EmailAnalysisApplyStore()
    app.extensions["email_analysis_calendar_store"] = EmailAnalysisApplyStore()
    app.extensions["email_analysis_session_store"] = EmailAnalysisSessionStore(
        ttl_seconds=app.config["EMAIL_ANALYSIS_SESSION_TTL_SECONDS"],
        max_entries=app.config["EMAIL_ANALYSIS_SESSION_MAX_ENTRIES"],
        max_payload_bytes=app.config[
            "EMAIL_ANALYSIS_SESSION_MAX_PAYLOAD_BYTES"
        ],
    )

    app.register_blueprint(main_bp)
    app.register_blueprint(applications_bp)
    app.register_blueprint(checklists_bp)
    app.register_blueprint(emails_bp)
    app.register_blueprint(integrations_bp)

    if app.config.get("AUTO_CREATE_DATABASE", True):
        with app.app_context():
            db.create_all()

    return app
