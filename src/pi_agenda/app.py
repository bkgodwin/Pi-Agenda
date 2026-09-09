from __future__ import annotations

import os
import secrets
from datetime import timedelta

from flask import Flask, jsonify, request

from .config import RuntimeConfig
from .db import close_db, migrate
from .security import csrf_token


def _load_or_create_secret(config: RuntimeConfig) -> str:
    config.secret_path.parent.mkdir(parents=True, exist_ok=True)
    if config.secret_path.exists():
        value = config.secret_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    config.secret_path.write_text(value, encoding="utf-8")
    try:
        os.chmod(config.secret_path, 0o600)
    except OSError:
        pass
    return value


def create_app(runtime_config: RuntimeConfig | None = None) -> Flask:
    runtime = runtime_config or RuntimeConfig.from_env()
    runtime.ensure_directories()
    migrate(runtime.db_path)
    app = Flask(__name__, instance_relative_config=False)
    app.config.update(
        RUNTIME_CONFIG=runtime,
        SECRET_KEY=_load_or_create_secret(runtime),
        MAX_CONTENT_LENGTH=runtime.max_upload_bytes,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        TESTING=runtime.testing,
        REMOTE_PLAYER_ENABLED=False,
        PLAYER_TOKEN=os.environ.get("PI_AGENDA_PLAYER_TOKEN", ""),
    )

    from . import auth, routes_admin, routes_api, routes_player

    app.register_blueprint(auth.bp)
    app.register_blueprint(routes_admin.bp)
    app.register_blueprint(routes_api.bp)
    app.register_blueprint(routes_player.bp)
    app.teardown_appcontext(close_db)

    @app.context_processor
    def template_globals():
        return {
            "csrf_token": csrf_token,
            "app_version": __import__("pi_agenda").__version__,
            "day_enabled": lambda mask, day: bool(int(mask) & (1 << int(day))),
        }

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["X-Frame-Options"] = "DENY"
        if request.path in {"/player", "/startup"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
                "frame-src http: https:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-src 'self' http: https:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        if (
            request.path.startswith("/admin")
            or request.path.startswith("/api")
            or request.path in {"/login", "/player/connect"}
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def too_large(_error):
        if request.path.startswith("/api/"):
            return jsonify(
                ok=False,
                data=None,
                error={
                    "code": "too_large",
                    "message": "Request exceeds the upload limit",
                },
            ), 413
        return "Request exceeds the upload limit", 413

    @app.errorhandler(400)
    def bad_request(error):
        if request.path.startswith("/api/"):
            return jsonify(
                ok=False,
                data=None,
                error={
                    "code": "bad_request",
                    "message": getattr(error, "description", "Bad request"),
                },
            ), 400
        return "Bad request", 400

    @app.errorhandler(403)
    def forbidden(_error):
        if request.path.startswith("/api/"):
            return jsonify(
                ok=False, data=None, error={"code": "forbidden", "message": "Forbidden"}
            ), 403
        return "Forbidden", 403

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify(
                ok=False, data=None, error={"code": "not_found", "message": "Not found"}
            ), 404
        return "Not found", 404

    @app.errorhandler(500)
    def internal_error(_error):
        if request.path.startswith("/api/"):
            return jsonify(
                ok=False,
                data=None,
                error={"code": "internal_error", "message": "Internal server error"},
            ), 500
        return "Internal server error", 500

    return app
