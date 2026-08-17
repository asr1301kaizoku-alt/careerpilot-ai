from flask import current_app, render_template
from flask_wtf.csrf import CSRFError

from .extensions import db


def register_error_handlers(app):
    def log_exception_safely(exc_info):
        error = exc_info[1]
        app.logger.error(
            "Unhandled application error operation=request "
            "stage=dispatch exception=%s success=false",
            type(error).__name__,
        )

    app.log_exception = log_exception_safely

    @app.errorhandler(CSRFError)
    def csrf_error(_error):
        current_app.logger.warning(
            "Request rejected operation=request_validation "
            "stage=csrf classification=invalid_or_expired success=false"
        )
        return render_template("400.html", csrf_failed=True), 400

    @app.errorhandler(400)
    def bad_request(_error):
        return render_template("400.html", csrf_failed=False), 400

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def internal_server_error(_error):
        db.session.rollback()
        return render_template("500.html"), 500
