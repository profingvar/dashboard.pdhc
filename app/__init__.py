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
    # Service-key bypass: monitor.pdhc → benchmarks / smoke / future CI.
    app.config.setdefault(
        "MONITOR_PDHC_SERVICE_KEY",
        os.environ.get("MONITOR_PDHC_SERVICE_KEY", ""),
    )
    # Ticket #291 — gateway.pdhc calls dashboard's /api/v1/observations
    # search via X-Source-Service: gateway.pdhc + X-Service-Key. The
    # operator copies the matching value from gateway's .env.
    app.config.setdefault(
        "GATEWAY_PDHC_SERVICE_KEY",
        os.environ.get("GATEWAY_PDHC_SERVICE_KEY", ""),
    )
    # CDR endpoints + outbound key (used by analyse/federation.py).
    # Env shape is a comma-separated URL list (e.g. "https://cdr2.pdhc.se,
    # https://cdr3.pdhc.se,..."). The federation expects dicts with
    # cdr_id + base_url, so parse the cdr<N> shortname out of each
    # hostname here once at app boot.
    import re as _re
    _eps: list[dict] = []
    for raw in (os.environ.get("CDR_ENDPOINTS", "") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        # Accept two forms:
        #   1. plain URL — e.g. "https://cdr2.pdhc.se" — cdr_id pulled
        #      from the cdrN.pdhc.se hostname.
        #   2. "<id>=<url>" — e.g. "cdr2=http://127.0.0.1:9146" —
        #      explicit id. Used for loopback / internal URLs that
        #      don't have a cdrN.pdhc.se hostname (avoids hairpin NAT
        #      and the resulting fanout 499s when dashboard's
        #      gunicorn worker calls the public hostnames back into
        #      itself).
        if "=" in raw and not raw.startswith("http"):
            cdr_id, _, url = raw.partition("=")
            cdr_id = cdr_id.strip()
            url = url.strip().rstrip("/")
        else:
            url = raw.rstrip("/")
            m = _re.search(r"cdr(\d+)\.pdhc\.se", url)
            cdr_id = f"cdr{m.group(1)}" if m else url
        if not url or not cdr_id:
            continue
        _eps.append({"cdr_id": cdr_id, "base_url": url})
    app.config.setdefault("CDR_ENDPOINTS", _eps)
    app.config.setdefault(
        "DASHBOARD_PDHC_SERVICE_KEY",
        os.environ.get("DASHBOARD_PDHC_SERVICE_KEY", ""),
    )
    # Ticket #213 — ObservationCache retention. Rows whose `fetched_at`
    # is older than this many hours are dropped by the `flask cache-sweep`
    # CLI (run from cron). Default 48h matches the upper end of the
    # ticket's 24-48h band.
    app.config.setdefault(
        "OBSERVATION_CACHE_TTL_HOURS",
        int(os.environ.get("OBSERVATION_CACHE_TTL_HOURS", "48")),
    )
    db.init_app(app)
    Migrate(app, db, directory=os.path.join(os.path.dirname(__file__), "migrations"))

    from app.auth import register_cli, install_request_loader
    register_cli(app)
    install_request_loader(app)

    from app.routes.views import bp as views_bp
    from app.routes.api import bp as api_bp, register_metadata
    from app.routes.auth import bp as auth_bp
    from app.routes.nurse import bp as nurse_bp
    from app.routes.researcher import (
        bp as researcher_bp,
        register_export_audit_cli,
    )
    from app.routes.workspace import bp as workspace_bp
    # #291 — analyse-layer observations search (mirrors cdr1's removed
    # /api/v1/observations endpoint). Gateway's proxy lands here.
    from app.analyse.observations_search import bp as observations_search_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(nurse_bp)
    app.register_blueprint(researcher_bp)
    app.register_blueprint(workspace_bp)
    app.register_blueprint(observations_search_bp)
    register_metadata(app)
    register_export_audit_cli(app)

    # Ticket #213. POST /admin/cache/scrub + `flask cache-sweep` CLI.
    from app.routes.admin import bp as admin_bp, register_cache_sweep_cli
    app.register_blueprint(admin_bp)
    register_cache_sweep_cli(app)

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
