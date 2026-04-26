import os
import logging
from datetime import datetime, timezone
from flask import Flask, jsonify
from flask_migrate import Migrate
from app.models import db


def _results_dir():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ_results")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results", ts))
    os.makedirs(root, exist_ok=True)
    return root


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        AUTH_MODE=os.environ.get("AUTH_MODE", "off"),
        GATEWAY_BASE_URL=os.environ.get("GATEWAY_BASE_URL", ""),
    )
    if config:
        app.config.update(config)

    app.config["SQLALCHEMY_DATABASE_URI"] = app.config.get("DATABASE_URL") or "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config.setdefault("SSO_BASE_URL", os.environ.get("SSO_BASE_URL", ""))
    app.config.setdefault("SSO_CLIENT_ID", os.environ.get("SSO_CLIENT_ID", ""))
    app.config.setdefault("SSO_CLIENT_SECRET", os.environ.get("SSO_CLIENT_SECRET", ""))
    app.config.setdefault("SSO_CALLBACK_URL", os.environ.get("SSO_CALLBACK_URL", ""))
    db.init_app(app)
    Migrate(app, db, directory=os.path.join(os.path.dirname(__file__), "migrations"))

    from app.auth import register_cli, install_request_loader
    register_cli(app)
    install_request_loader(app)

    from app.routes.views import bp as views_bp
    from app.routes.api import bp as api_bp, register_metadata
    from app.routes.auth import bp as auth_bp
    from app.routes.nurse import bp as nurse_bp
    from app.routes.researcher import bp as researcher_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(nurse_bp)
    app.register_blueprint(researcher_bp)
    register_metadata(app)

    log_dir = _results_dir()
    handler = logging.FileHandler(os.path.join(log_dir, "app.log"))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

    @app.get("/healthz")
    def healthz():
        # CORS: allow www.pdhc.se/services.html to read this JSON cross-origin
        # so it can drive real status dots instead of no-cors opaque guesses
        # (ticket #70 / CLAUDE.md §10). Specific origin + Vary: Origin (not "*")
        # so any future Allow-Credentials stays spec-compliant.
        # Ticket #71: add real DB probe so the services.html DB dot reflects
        # reality. Canonical shape per CLAUDE.md §10: database:"connected"|
        # "unavailable" + HTTP 503 on degraded.
        try:
            db.session.execute(db.text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
        status = "ok" if db_ok else "degraded"
        resp = jsonify(
            status=status,
            service="dashboard.pdhc",
            database="connected" if db_ok else "unavailable",
            auth_mode=app.config["AUTH_MODE"],
        )
        resp.headers["Access-Control-Allow-Origin"] = "https://www.pdhc.se"
        resp.headers["Access-Control-Allow-Methods"] = "GET"
        resp.headers["Vary"] = "Origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp, 200 if db_ok else 503

    return app
