"""#729 — the service-key path is closed, and gated if it ever reopens.

Two defects, one file:

1. ``KNOWN_SERVICES`` held ``gateway.pdhc`` long after #540 repointed its
   ``/api/v1/observations`` proxy to analyse.pdhc. Verified against the live
   gateway container on 2026-10-01: ``ANALYSE_BASE_URL`` is :9110 and there is
   no ``DASHBOARD*`` variable at all. The dict is now empty.

2. The service-key branch of ``install_request_loader`` returned **before**
   ``_dashboard_access_allowed``, so a service-key caller skipped the
   care-delivery / analysis-phase gate and could address every clinical path an
   SSO caller without a care relationship is 403'd out of.

Why (2) was invisible: ``test_analysis_consent`` already had
``test_service_blob_denied_on_clinical_routes``, whose comment said "the
app-level gate keeps it out" — but it asserted
``has_care_delivery_access(blob) is False``, the *predicate*, on a path where
the predicate was never consulted. These tests go through the real loader
instead, so they would have failed.
"""
import sqlalchemy
from app import create_app
from app.auth import KNOWN_SERVICES

CLINICAL_PATHS = ["/", "/workspace", "/refresh"]


def _app(**over):
    cfg = {
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        # sso, so the dev-SU blob cannot mask what the service path does
        "AUTH_MODE": "sso",
        "SSO_BASE_URL": "https://sso.pdhc.se",
        "SSO_CLIENT_ID": "cid",
        "SSO_CLIENT_SECRET": "secret",
        "SSO_CALLBACK_URL": "https://dashboard.pdhc.se/auth/callback",
    }
    cfg.update(over)
    from app.models import db
    app = create_app(cfg)
    with app.app_context():
        db.create_all()
    return app


def _hdr(service="gateway.pdhc", key="k"):
    return {"X-Source-Service": service, "X-Service-Key": key}


# --- 1. the dict is empty ---------------------------------------------------

def test_known_services_is_empty():
    assert KNOWN_SERVICES == {}


def test_every_service_identity_is_refused_now():
    """An empty dict makes _service_key_outcome return False for ANY source,
    so the attempt is 403 before a route is reached."""
    c = _app().test_client()
    for service in ("gateway.pdhc", "monitor.pdhc", "anything.pdhc"):
        r = c.get("/api/nurse/patients", headers=_hdr(service))
        assert r.status_code == 403, f"{service} was not refused"
        assert r.get_json() == {"error": "Invalid service credentials"}


def test_a_service_key_attempt_never_reaches_a_route():
    # 403 from the loader, not 404 from routing — i.e. refused before dispatch.
    r = _app().test_client().get("/no/such/route", headers=_hdr())
    assert r.status_code == 403


# --- 2. the gate now applies if an identity is ever re-added ----------------

def test_readded_identity_is_still_gated_off_clinical_paths(monkeypatch):
    """The real regression test for #729.

    Re-admit an identity exactly as a future change would, then prove the
    loader still refuses it on the clinical paths. Before #729 this returned
    200/302 — the gate was never consulted on this branch.
    """
    monkeypatch.setitem(KNOWN_SERVICES, "probe.pdhc", "PROBE_KEY")
    c = _app(PROBE_KEY="right").test_client()
    for path in CLINICAL_PATHS:
        r = c.get(path, headers=_hdr("probe.pdhc", "right"))
        assert r.status_code == 403, (
            f"{path} admitted a service blob with no care relationship"
        )


def test_readded_identity_with_a_wrong_key_is_still_403(monkeypatch):
    monkeypatch.setitem(KNOWN_SERVICES, "probe.pdhc", "PROBE_KEY")
    c = _app(PROBE_KEY="right").test_client()
    r = c.get("/", headers=_hdr("probe.pdhc", "wrong"))
    assert r.status_code == 403


def test_the_gate_call_is_present_on_the_service_branch():
    """Guards against the fix being undone by a later refactor that restores
    the early return. The source check is deliberate: the behavioural tests
    above only bite while an identity exists to exercise them, and the dict is
    empty by design.

    Comments are stripped first. The branch carries a long #729 comment that
    names the gate function, and the first version of this test passed against
    a deliberately reverted fix because of it — a source assertion that matches
    its own explanation is worth nothing.
    """
    import inspect
    from app import auth
    src = inspect.getsource(auth.install_request_loader)
    _, _, tail = src.partition("if sk is True:")
    assert tail, "service-key branch not found"
    branch, _, _ = tail.partition("if sk is False:")
    code = "\n".join(
        line for line in branch.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "_dashboard_access_allowed" in code, (
        "the service-key branch returns without applying the access gate (#729)"
    )


# --- 3. the public paths are untouched -------------------------------------

def test_healthz_still_open():
    assert _app().test_client().get("/healthz").status_code == 200
