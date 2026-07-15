"""Per-patient CDR1 charts proxies + spärr (#464 D2).

The browser calls these dashboard endpoints; they read CDR1 via Cdr1Client
(mocked here) and apply spärr on this side. The page shell is also checked.
"""
import sqlalchemy

from app import create_app
from app.models import db
from app.services.ips_client import Block
import app.routes.charts as charts


def _app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "off",
    })
    with app.app_context():
        db.create_all()
    return app


class _FakeClient:
    base_url = "https://cdr.pdhc.se"

    def __init__(self, summary=None, series=None):
        self._summary = summary or []
        self._series = series or []
        self.calls = []

    def patient_summary(self, guid, orgs, *, is_admin=False):
        self.calls.append(("summary", guid, tuple(orgs), is_admin))
        return self._summary

    def patient_series(self, guid, codes, frm, to, orgs, *, is_admin=False):
        self.calls.append(("series", guid, tuple(codes), frm, to, is_admin))
        return self._series


def _block(scope_id):
    return Block.from_dict({
        "guid": "b1", "patient_guid": "p", "source_scope_type": "clinic",
        "source_scope_id": scope_id, "is_active": True,
    })


def test_parameters_proxied(monkeypatch):
    app = _app()
    fake = _FakeClient(summary=[
        {"code": "sys|weight", "unit": "kg", "count": 9},
        {"code": "sys|glucose", "unit": "mmol/L", "count": 3},
    ])
    monkeypatch.setattr(charts, "build_client", lambda: fake)
    r = app.test_client().get("/api/v1/patient/pat-1/parameters")
    assert r.status_code == 200
    body = r.get_json()
    assert body["patient_guid"] == "pat-1"
    assert [p["code"] for p in body["parameters"]] == ["sys|weight", "sys|glucose"]


def test_series_proxied_and_code_window_forwarded(monkeypatch):
    app = _app()
    fake = _FakeClient(series=[
        {"code": "sys|weight", "at": "2026-04-05T10:00:00+00:00",
         "value": 80.0, "unit": "kg", "org_guid": "org-a"},
    ])
    monkeypatch.setattr(charts, "build_client", lambda: fake)
    monkeypatch.setattr(charts, "get_active_blocks", lambda g: [])
    r = app.test_client().get(
        "/api/v1/patient/pat-1/series?code=sys|weight&from=2026-01-01T00:00:00Z")
    assert r.status_code == 200
    assert len(r.get_json()["points"]) == 1
    # the code + window were forwarded to the client
    call = fake.calls[0]
    assert call[0] == "series" and call[2] == ("sys|weight",)
    assert call[3] == "2026-01-01T00:00:00Z"


def test_series_sparr_drops_blocked_clinic(monkeypatch):
    app = _app()
    fake = _FakeClient(series=[
        {"code": "c", "at": "t1", "value": 1, "org_guid": "org-ok"},
        {"code": "c", "at": "t2", "value": 2, "org_guid": "org-blocked"},
    ])
    monkeypatch.setattr(charts, "build_client", lambda: fake)
    monkeypatch.setattr(charts, "get_active_blocks",
                        lambda g: [_block("org-blocked")])
    r = app.test_client().get("/api/v1/patient/pat-1/series")
    body = r.get_json()
    assert [p["org_guid"] for p in body["points"]] == ["org-ok"]
    assert body["has_blocked_sources"] is True


def test_charts_page_renders(monkeypatch):
    app = _app()
    monkeypatch.setattr(charts, "build_client", lambda: _FakeClient())
    monkeypatch.setattr(charts, "get_active_blocks", lambda g: [])
    r = app.test_client().get("/patient/pat-1/charts")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "pat-1" in html
    # the page wires the data endpoints
    assert "/api/v1/patient/" in html


def test_charts_page_sparr_banner(monkeypatch):
    app = _app()
    monkeypatch.setattr(charts, "build_client", lambda: _FakeClient())
    monkeypatch.setattr(charts, "get_active_blocks",
                        lambda g: [_block("org-x")])
    r = app.test_client().get("/patient/pat-1/charts")
    assert "spärr" in r.get_data(as_text=True).lower()
