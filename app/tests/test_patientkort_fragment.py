"""The shared patient card in dashboard (operator 2026-11-25).

`GET /dashboard/patients/<guid>/patientkort` renders eps's `_patientkort.html`
(a byte-identical copy, pinned below) from eps's view-model, AFTER dashboard's
own access gate, and answers a NAMED fragment whenever eps cannot give it.
eps is mocked with the contract shape; synthetic data only.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import requests
from flask import Flask, g

import app.routes.dashboard as dash
import app.services.ips_client as ipsmod
from app.services.ips_client import (IpsClient, IpsNotConfigured, IpsRefused,
                                     IpsUnreachable)
from app.tests.test_dashboard_card import _app

APP_DIR = Path(__file__).resolve().parents[1]

#: sha256 of eps.pdhc's files at the copy (ips.pdhc a479362). A change to the
#: shared card is made in eps and re-copied; this makes drift visible.
SHARED = {
    "templates/_patientkort.html":
        "02a04d59b60bde2c84e68ac443b4646585d2617aadabece6acfd8d328559fecf",
    "static/css/patientkort.css":
        "279b3cc19a52ac14b3b80e3eb337216cf9d37e2292f389fa16b4e00acda6e73a",
}


def _pop(n, title):
    return {"id": f"pk-p-{n}", "title": title,
            "stored": [{"where": "eps.pdhc · FHIR Patient", "id": "pat-1"}],
            "origin": [], "history": [], "history_more": 0}


def pk_fixture():
    """A pk in the contract's shape (patientkort_view.build's output)."""
    return {
        "name": "Fragmentsson, Frida", "name_prov": _pop(1, "Namn"),
        "age_text": "67 år", "sex_text": "Kvinna", "birth_date": "1958-04-11",
        "personnummer": {"text": None, "synthetic": False},
        "fields": [{"label": "Ålder", "value": "67 år", "sub": "född 1958-04-11",
                    "mono": False, "tag": None, "prov": _pop(2, "Ålder")}],
        "units": [{"name": "VC Fragment", "caregiver_name": "Region Fragment",
                   "is_own_caregiver": False, "since": "2026-01-02",
                   "prov": _pop(3, "Vårdenhet")}],
        "banners": [{"kind": "block", "level": "warning", "text": "Spärr aktiv",
                     "prov": None}],
        "diagnoses": [{"code": "38341003", "display": "Hypertoni",
                       "system_label": "SNOMED CT", "status_text": "aktiv",
                       "active": True, "cdr_backed": False, "cdr_name": None,
                       "date": "2019-03-02", "prov": _pop(4, "Diagnos")}],
        "diagnoses_state": "ok", "diagnosis_note": None,
        "sections": [{"key": "allergies", "title": "Allergier", "count": 0,
                      "state": "absent", "priority": True, "entries": [],
                      "absent_text": "Inga kända allergier"}],
        "sections_state": "ok", "empty_sections": [], "entries_shown": 5,
        "ids": {"guid": "p1", "fhir_id": "pat-1"},
    }


class _Cdr1:
    base_url = "https://cdr.example"

    def __init__(self, patients=None):
        self._p = patients if patients is not None else [{"guid": "p1"}]

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return self._p


def _wire(monkeypatch, *, pk=None, raises=None, calls=None):
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _Cdr1())
    monkeypatch.setattr(dash.ips_client, "is_configured", lambda: True)
    monkeypatch.setattr(dash.ips_client, "get_active_blocks", lambda *a, **k: [])

    class _Ips:
        def patientkort(self, guid):
            if calls is not None:
                calls.append(guid)
            if raises:
                raise raises
            return pk
    monkeypatch.setattr(dash.ips_client, "_default_client", lambda: _Ips())


def _get(app):
    with app.test_client() as c:
        return c.get("/dashboard/patients/p1/patientkort")


# ── the fragment ────────────────────────────────────────────────────────


def test_the_card_renders_from_eps_view_model(monkeypatch):
    _wire(monkeypatch, pk=pk_fixture())
    r = _get(_app())
    assert r.status_code == 200 and r.mimetype == "text/html"
    html = r.get_data(as_text=True)
    assert '<section class="pk"' in html and "data-pk" in html
    for text in ("Fragmentsson, Frida", "VC Fragment", "Hypertoni",
                 "ej förankrad i CDR", "Spärr aktiv", "Inga kända allergier"):
        assert text in html, text


def test_the_fragment_IS_the_shared_partial_rendered(monkeypatch):
    """No dashboard wrapper or rewrite: the bytes are the partial's output."""
    app = _app()
    _wire(monkeypatch, pk=pk_fixture())
    html = _get(app).get_data(as_text=True)
    from flask import render_template
    with app.test_request_context():
        assert html == render_template("_patientkort.html", pk=pk_fixture())


@pytest.mark.parametrize("raises,status,state,says", [
    (IpsNotConfigured("x"), 503, "not_configured", "inte konfigurerad"),
    (IpsUnreachable("x"), 502, "unreachable", "svarade inte"),
    (IpsRefused(403), 403, "refused", "nekade"),
    (None, 404, "not_found", "inom din behörighet"),
])
def test_every_eps_failure_is_NAMED_never_an_empty_card(monkeypatch, raises, status,
                                                        state, says):
    _wire(monkeypatch, pk=None, raises=raises)
    r = _get(_app())
    html = r.get_data(as_text=True)
    assert r.status_code == status and r.mimetype == "text/html"
    assert f'data-pk-state="{state}"' in html and says in html
    assert "Patientkortet kunde inte visas" in html
    assert '<section class="pk"' not in html


# ── dashboard's own gate runs BEFORE eps is asked ───────────────────────


def _non_admin():
    return {"user_guid": "u1", "user_type": "professional",
            "affiliations": [{"affiliation_guid": "a1", "role": "physician",
                              "care_unit_guid": "org-A"}]}


def test_dashboards_gate_refuses_before_eps_is_asked(monkeypatch):
    calls = []
    app = _app()
    _wire(monkeypatch, pk=pk_fixture(), calls=calls)
    monkeypatch.setattr(dash.cdr1_client, "build_client",
                        lambda: _Cdr1(patients=[{"guid": "someone-else"}]))
    with app.test_request_context("/dashboard/patients/p1/patientkort"):
        g.access_blob = _non_admin()
        resp, status = dash.patient_patientkort.__wrapped__(guid="p1")
    assert status == 403 and resp.get_json()["error"] == "other_organisation"
    assert calls == [], "eps was asked for a patient dashboard refused"


def test_the_same_reader_IN_scope_reaches_eps(monkeypatch):
    """Control for the refusal above: the gate, not the wiring, stopped it."""
    calls = []
    app = _app()
    _wire(monkeypatch, pk=pk_fixture(), calls=calls)
    with app.test_request_context("/dashboard/patients/p1/patientkort"):
        g.access_blob = _non_admin()
        out = dash.patient_patientkort.__wrapped__(guid="p1")
    assert calls == ["p1"]
    assert "Fragmentsson, Frida" in (out if isinstance(out, str) else out[0])


# ── the client ──────────────────────────────────────────────────────────


class _R:
    def __init__(self, status, body=None, raw=False):
        self.status_code, self._b, self._raw = status, body, raw

    def json(self):
        if self._raw:
            raise ValueError
        return self._b


@pytest.fixture
def ctx():
    with Flask(__name__).test_request_context("/"):
        yield


def _client():
    return IpsClient(base_url="https://eps.example", token="t")


def test_client_sends_the_persons_bearer_to_the_contract_path(ctx, monkeypatch):
    seen = {}

    def fake(url, headers=None, timeout=None):
        seen.update(url=url, h=headers)
        return _R(200, {"patientkort": pk_fixture(), "version": 1})
    monkeypatch.setattr(ipsmod.requests, "get", fake)
    assert _client().patientkort("p1")["name"] == "Fragmentsson, Frida"
    assert seen["url"] == "https://eps.example/api/v1/patients/p1/patientkort"
    assert seen["h"]["Authorization"] == "Bearer t"


@pytest.mark.parametrize("resp,exc", [
    (_R(401, {}), IpsRefused), (_R(403, {}), IpsRefused),
    (_R(503, {"error": "scope_undetermined"}), IpsUnreachable),
    (_R(200, None, raw=True), IpsUnreachable),
    (_R(200, {"sections": []}), IpsUnreachable),
    (_R(200, {"patientkort": {}, "version": 2}), IpsUnreachable),
])
def test_client_failures_are_typed(ctx, monkeypatch, resp, exc):
    monkeypatch.setattr(ipsmod.requests, "get", lambda *a, **k: resp)
    with pytest.raises(exc):
        _client().patientkort("p1")


def test_client_404_is_an_answer_and_unconfigured_and_network_raise(ctx, monkeypatch):
    monkeypatch.setattr(ipsmod.requests, "get", lambda *a, **k: _R(404, {"error": "x"}))
    assert _client().patientkort("p1") is None
    with pytest.raises(IpsNotConfigured):
        IpsClient(base_url="", token="t").patientkort("p1")

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(ipsmod.requests, "get", boom)
    with pytest.raises(IpsUnreachable):
        _client().patientkort("p1")


# ── the shared files stay byte-identical to eps's ───────────────────────


@pytest.mark.parametrize("rel,digest", sorted(SHARED.items()))
def test_shared_card_files_are_the_recorded_copy(rel, digest):
    assert hashlib.sha256((APP_DIR / rel).read_bytes()).hexdigest() == digest, (
        f"{rel} drifted from eps.pdhc's copy: change it in eps, re-copy it, "
        "and update SHARED")
