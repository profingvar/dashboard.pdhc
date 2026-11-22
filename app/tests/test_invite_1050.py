"""#1050 v2 — "Bjud in till patientappen" for the patient OPEN in the dashboard.

Operator 2026-11-22: the patient is chosen on entry; the invite only confirms
the contact way and forms a message (sms: link) carrying the invite.

Every test goes through the real routes with AUTH_MODE=sso, so the request
loader, the gate and the CSRF check all run. The ONLY thing patched is the
outbound HTTP boundary (`requests.get` / `requests.post`): one fake answers as
sso.pdhc (`/api/auth/me/service`, `/api/patient-links/invites`) and as eps
(`/patients/<guid>/state`, `/clinics`, `/blocks`), each refusing what the real
service refuses (a service key; sso: a unit the bearer is not affiliated to,
or a patient eps does not list there). No client of ours is patched.

A patient is "opened" the way a professional opens one: GET of the real
charts page.

Synthetic data only: no personnummer appears anywhere in this flow.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import uuid
from urllib.parse import quote, unquote

import pytest
import requests
import sqlalchemy

from app import create_app
from app.models import DashboardAudit, db

SSO = "https://sso.test"
EPS = "https://eps.test"
TOKEN = "tok-operator"
UNIT_A = "cccccccc-1050-4000-8000-00000000000a"
UNIT_B = "cccccccc-1050-4000-8000-00000000000b"
P1 = "aaaaaaaa-1050-4000-8000-000000000001"
P2 = "aaaaaaaa-1050-4000-8000-000000000002"
INVITE_URL = "https://sso.test/patient-invite/ONE-TIME_tok-xyz"
NAMES = {UNIT_A: "Vårdcentralen Norr", UNIT_B: "Vårdcentralen Söder"}


def _blob(units=(UNIT_A,), **kw):
    b = {"user_guid": "dddddddd-1050-4000-8000-000000000001",
         "email": "ssk@example.test", "user_type": "professional",
         "is_su_admin": False, "session_phases": [],
         "affiliations": [{"affiliation_guid": f"a{i}", "role": "nurse",
                           "care_unit_guid": u, "care_unit_name": NAMES.get(u, u)}
                          for i, u in enumerate(units)]}
    b.update(kw)
    return b


class _Resp:
    def __init__(self, status, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _state(given=("Synt",), family="Inbjudensson", phones=(("070-000 00 01", "mobile"),)):
    facts = []
    if given is not None:
        facts.append({"fact_type": "name.given", "value": {"value": list(given)}})
    if family is not None:
        facts.append({"fact_type": "name.family", "value": {"value": family}})
    for v, use in phones:
        facts.append({"fact_type": "telecom",
                      "value": {"system": "phone", "value": v, "use": use}})
    facts.append({"fact_type": "telecom",
                  "value": {"system": "email", "value": "x@example.test", "use": None}})
    return {"patient_guid": P1, "facts": facts, "count": len(facts)}


class Fake:
    def __init__(self):
        self.blob = _blob()
        self.calls = []             # (method, url-path, headers, json)
        self.state = {P1: _state(), P2: _state(given=("Annan",), family="Patient")}
        self.listed = {P1: [UNIT_A], P2: [UNIT_A]}
        self.platform_header = {}   # guid -> header value override
        self.eps_answer = None      # _Resp or Exception for /state
        self.sso_answer = None

    def _who(self, headers):
        if "X-Service-Key" in headers or "X-Source-Service" in headers:
            return "service"
        return "ok" if headers.get("Authorization") == f"Bearer {TOKEN}" else None

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        headers = headers or {}
        if url == f"{SSO}/api/auth/me/service":
            if headers.get("Authorization") != f"Bearer {TOKEN}":
                return _Resp(401, {"error": "invalid"})
            return _Resp(200, self.blob)
        m = re.fullmatch(rf"{EPS}/api/v1/patients/([^/]+)/(state|clinics|blocks)", url)
        if m:
            guid, what = m.groups()
            if what == "blocks":
                return _Resp(200, {"blocks": []})
            self.calls.append(("GET", what, dict(headers), guid))
            # eps require_auth reads Authorization only: the service key the
            # ips client attaches elsewhere must NOT be what authenticates.
            if self._who(headers) != "ok" and "Authorization" not in headers:
                return _Resp(401, {"error": "Missing Authorization header"})
            if what == "state":
                if isinstance(self.eps_answer, Exception):
                    raise self.eps_answer
                if self.eps_answer is not None:
                    return self.eps_answer
                if guid not in self.state:
                    return _Resp(404, {"error": "patient not found"})
                return _Resp(200, self.state[guid])
            if guid not in self.listed:
                return _Resp(404, {"error": "Patient not found"})
            return _Resp(200, [{"guid": str(uuid.uuid4()), "care_unit_guid": u,
                                "organisation_guid": u, "name": NAMES.get(u, u) + " (eps)"}
                               for u in self.listed[guid]],
                         headers={"X-PDHC-Patient-Guid": self.platform_header.get(guid, guid)})
        return _Resp(404, None, "<html>404</html>")

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        headers = headers or {}
        if url == f"{SSO}/api/patient-links/invites":
            self.calls.append(("POST", "invites", dict(headers), dict(json)))
            if self._who(headers) != "ok":
                return _Resp(401, {"error": "unauthorized"})
            if isinstance(self.sso_answer, Exception):
                raise self.sso_answer
            if self.sso_answer is not None:
                return self.sso_answer
            units = {a["care_unit_guid"] for a in self.blob.get("affiliations") or []}
            if not self.blob.get("is_su_admin") and json["care_unit_guid"] not in units:
                return _Resp(403, {"error": "not_affiliated"})
            if json["care_unit_guid"] not in self.listed.get(json["ips_patient_guid"], []):
                return _Resp(403, {"error": "patient_not_at_unit"})
            return _Resp(201, {"invite_guid": str(uuid.uuid4()),
                               "ips_patient_guid": json["ips_patient_guid"],
                               "care_unit_guid": json["care_unit_guid"],
                               "given_name": json.get("given_name"),
                               "family_name": json.get("family_name"),
                               "expires_at": "2026-11-29T12:00:00+00:00",
                               "invite_url": INVITE_URL})
        return _Resp(404, None, "<html>404</html>")

    def sso_invites(self):
        return [c for c in self.calls if c[1] == "invites"]

    def eps_calls(self):
        return [c for c in self.calls if c[1] in ("state", "clinics")]


@pytest.fixture
def up(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True, "SECRET_KEY": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False},
                                      "poolclass": sqlalchemy.pool.StaticPool},
        "AUTH_MODE": "sso", "SSO_BASE_URL": SSO, "SSO_CLIENT_ID": "cid",
        "SSO_CLIENT_SECRET": "secret",
        "SSO_CALLBACK_URL": "https://dashboard.test/auth/callback",
        "IPS_BASE_URL": EPS,
        # Set on purpose: the invite calls must NOT send it.
        "DASHBOARD_PDHC_SERVICE_KEY": "dashboard-service-key",
    })
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture
def c(app, up):
    client = app.test_client()
    with client.session_transaction() as s:
        s["sso_token"] = TOKEN
    return client


def _open(client, guid=P1):
    r = client.get(f"/patient/{guid}/charts")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r


def _csrf(client, guid=P1):
    r = client.get(f"/bjud-in-patient/{guid}")
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True))
    assert m, r.get_data(as_text=True)[:800]
    return m.group(1)


def _post(client, step, guid=P1, csrf=True, **data):
    if csrf:
        data.setdefault("csrf_token", _csrf(client, guid))
    if step == "skicka":
        data.setdefault("confirmed", "ja")
    data.setdefault("mobile", "070-000 00 01")
    return client.post(f"/bjud-in-patient/{guid}/{step}",
                       data={k: v for k, v in data.items() if v is not None})


def _audit(app):
    with app.app_context():
        return [{"event": r.event_type, "patient": r.patient_guid,
                 "status": r.response_status, "payload": r.payload_snapshot or {}}
                for r in DashboardAudit.query.order_by(DashboardAudit.timestamp).all()]


def _sms_href(html):
    m = re.search(r'id="iv-sms" href="([^"]+)"', html)
    assert m, html[:1500]
    return htmllib.unescape(m.group(1))


def _expected_text(url=INVITE_URL, given="Synt", unit="Vårdcentralen Norr"):
    return (f"Hej {given}! {unit} bjuder in dig till Patientappen. Öppna länken "
            f"för att skapa ditt konto: {url} Länken gäller en gång.")


# --------------------------------------------------------------------------
# entry: the patient is chosen first
# --------------------------------------------------------------------------

def test_the_open_patient_carries_the_action_and_the_nav_item_is_gone(c, up):
    html = _open(c).get_data(as_text=True)
    assert f'href="/bjud-in-patient/{P1}"' in html
    assert "Bjud in till patientappen" in html
    assert 'id="invite-nav"' not in html


def test_the_patient_overview_page_also_opens_the_patient_and_carries_the_action(c, up):
    r = c.get(f"/dashboard/view/{P2}")
    assert r.status_code == 200
    assert f'href="/bjud-in-patient/{P2}"' in r.get_data(as_text=True)
    assert c.get(f"/bjud-in-patient/{P2}").status_code == 200


def test_no_patient_redirects_to_the_patient_picker(c, up):
    r = c.get("/bjud-in-patient")
    assert r.status_code == 302 and r.headers["Location"].endswith("/select")


def test_a_patient_not_opened_in_this_session_is_refused_before_eps_is_asked(c, up):
    _open(c, P1)
    r = c.get(f"/bjud-in-patient/{P2}")
    assert r.status_code == 403
    assert "Öppna patienten först" in r.get_data(as_text=True)
    assert [x for x in up.eps_calls() if x[3] == P2] == []


def test_a_malformed_guid_is_a_404(c, up):
    assert c.get("/bjud-in-patient/not-a-guid").status_code == 404


# --------------------------------------------------------------------------
# the page: name from eps, no identity entry, mobile for confirmation
# --------------------------------------------------------------------------

def test_the_page_shows_eps_name_and_prefills_the_normalised_mobile(c, up):
    _open(c)
    r = c.get(f"/bjud-in-patient/{P1}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'id="iv-patient">Synt Inbjudensson<' in html
    assert 'name="mobile"' in html and 'value="+46700000001"' in html
    for gone in ('name="personnummer"', 'name="given_name"', 'name="family_name"',
                 "Personnummer"):
        assert gone not in html
    assert "Vårdenhet:</strong> Vårdcentralen Norr" in html
    assert f'type="hidden" name="care_unit_guid" value="{UNIT_A}"' in html
    # eps read with the professional's bearer; Authorization is the bearer.
    for _, what, headers, _ in up.eps_calls():
        assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_no_mobile_in_eps_means_an_empty_required_field(c, up):
    up.state[P1] = _state(phones=())
    _open(c)
    html = c.get(f"/bjud-in-patient/{P1}").get_data(as_text=True)
    assert re.search(r'name="mobile" required[^>]*value=""', html)
    assert "inget mobilnummer" in html
    r = _post(c, "bekrafta", mobile="")
    assert r.status_code == 400
    assert "Ange patientens mobilnummer" in r.get_data(as_text=True)
    assert up.sso_invites() == []


def test_a_landline_only_in_eps_is_prefilled_raw_and_must_be_corrected(c, up):
    up.state[P1] = _state(phones=(("08-123 456", "home"),))
    _open(c)
    html = c.get(f"/bjud-in-patient/{P1}").get_data(as_text=True)
    assert 'value="08-123 456"' in html
    r = _post(c, "bekrafta", mobile="08-123 456")
    assert r.status_code == 400
    assert "svenskt mobilnummer" in r.get_data(as_text=True)


@pytest.mark.parametrize("typed,norm", [("070-000 00 01", "+46700000001"),
                                        ("+46 70 000 00 01", "+46700000001"),
                                        ("0046700000001", "+46700000001"),
                                        ("(073) 000-0009", "+46730000009")])
def test_the_mobile_is_normalised_to_plus_46(c, up, typed, norm):
    _open(c)
    r = _post(c, "bekrafta", mobile=typed)
    assert r.status_code == 200
    assert f'id="iv-c-mobile">{norm}<' in r.get_data(as_text=True)


def test_a_changed_mobile_is_said_to_be_for_this_message_only(c, up):
    _open(c)
    same = _post(c, "bekrafta", mobile="+46700000001").get_data(as_text=True)
    assert 'id="iv-not-saved"' not in same
    other = _post(c, "bekrafta", mobile="070-000 00 02").get_data(as_text=True)
    assert 'id="iv-not-saved"' in other
    assert "används bara för det här meddelandet och sparas inte" in other
    assert up.sso_invites() == []


# --------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------

def test_several_listed_units_are_a_required_choice_with_no_default(c, up):
    up.blob = _blob(units=(UNIT_A, UNIT_B))
    up.listed[P1] = [UNIT_A, UNIT_B]
    _open(c)
    html = c.get(f"/bjud-in-patient/{P1}").get_data(as_text=True)
    assert html.count('type="radio" name="care_unit_guid"') == 2
    assert "checked" not in html.split('id="iv-units"')[1].split("</fieldset>")[0]
    r = _post(c, "bekrafta")
    assert r.status_code == 400 and "Välj vårdenhet." in r.get_data(as_text=True)
    r = _post(c, "bekrafta", care_unit_guid=UNIT_B)
    assert r.status_code == 200 and 'id="iv-c-unit">Vårdcentralen Söder<' in r.get_data(as_text=True)


def test_only_units_the_patient_is_listed_at_can_be_chosen(c, up):
    up.blob = _blob(units=(UNIT_A, UNIT_B))
    up.listed[P1] = [UNIT_B]
    _open(c)
    html = c.get(f"/bjud-in-patient/{P1}").get_data(as_text=True)
    assert f'type="hidden" name="care_unit_guid" value="{UNIT_B}"' in html
    assert 'id="iv-not-listed"' in html and "Vårdcentralen Norr" in html
    r = _post(c, "skicka", care_unit_guid=UNIT_A)
    assert r.status_code == 400
    assert "inte listad vid den valda vårdenheten" in r.get_data(as_text=True)
    assert up.sso_invites() == []


def test_a_patient_not_listed_at_any_of_my_units_is_refused_by_name(app, c, up):
    up.listed[P1] = [UNIT_B]
    _open(c)
    r = c.get(f"/bjud-in-patient/{P1}")
    assert r.status_code == 409
    text = r.get_data(as_text=True)
    assert "Patienten är inte listad vid Vårdcentralen Norr" in text
    assert "Ingenting har gjorts" in text
    r = _post(c, "skicka", csrf=False, csrf_token="x")
    assert up.sso_invites() == []
    assert any(a["payload"].get("outcome") == "not_listed" for a in _audit(app))


def test_a_unit_i_am_not_affiliated_to_is_refused(c, up):
    _open(c)
    r = _post(c, "skicka", care_unit_guid=UNIT_B)
    assert r.status_code == 400
    assert "inte affilierad" in r.get_data(as_text=True)
    assert up.sso_invites() == []


def test_no_affiliation_and_not_su_is_refused_before_eps(c, up):
    up.blob = _blob(units=())
    # Such a professional cannot open the picker/charts (care door); go direct.
    with c.session_transaction() as s:
        s["_opened_patients"] = [P1]
    r = c.get(f"/bjud-in-patient/{P1}")
    assert r.status_code == 403
    assert "ingen affiliering" in r.get_data(as_text=True)
    assert up.eps_calls() == []


def test_su_chooses_among_the_units_eps_lists(c, up):
    up.blob = _blob(units=(), is_su_admin=True)
    up.listed[P1] = [UNIT_A, UNIT_B]
    _open(c)
    html = c.get(f"/bjud-in-patient/{P1}").get_data(as_text=True)
    assert html.count('type="radio" name="care_unit_guid"') == 2
    r = _post(c, "skicka", care_unit_guid=UNIT_B)
    assert r.status_code == 200
    assert up.sso_invites()[-1][3]["care_unit_guid"] == UNIT_B


def test_a_non_professional_cannot_reach_the_page(c, up):
    up.blob = _blob(user_type="patient")
    assert c.get(f"/bjud-in-patient/{P1}").status_code in (302, 403)
    assert up.eps_calls() == []


# --------------------------------------------------------------------------
# no IDOR: the patient is the one in context, never the form's
# --------------------------------------------------------------------------

def test_a_different_patient_guid_in_the_form_is_refused(c, up):
    _open(c, P1)
    _open(c, P2)
    for key in ("patient_guid", "ips_patient_guid"):
        r = _post(c, "skicka", P1, **{key: P2})
        assert r.status_code == 400, key
        assert "annan patient" in r.get_data(as_text=True)
    assert up.sso_invites() == []


def test_names_in_the_form_are_ignored_and_eps_names_are_sent(c, up):
    _open(c)
    r = _post(c, "skicka", given_name="Mallory", family_name="Annan",
              patient_guid=P1)
    assert r.status_code == 200
    (_, _, headers, body), = up.sso_invites()
    assert body == {"ips_patient_guid": P1, "care_unit_guid": UNIT_A,
                    "given_name": "Synt", "family_name": "Inbjudensson"}
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Service-Key" not in headers
    assert "Mallory" not in r.get_data(as_text=True)


def test_a_guid_eps_knows_under_another_platform_guid_is_refused(c, up):
    up.platform_header[P1] = "ffffffff-0000-4000-8000-000000000000"
    _open(c)
    r = c.get(f"/bjud-in-patient/{P1}")
    assert r.status_code == 502
    assert "annat id" in r.get_data(as_text=True)


def test_eps_out_of_scope_404_and_eps_down_503(c, up):
    _open(c)
    up.eps_answer = _Resp(404, {"error": "patient not found"})
    r = c.get(f"/bjud-in-patient/{P1}")
    assert r.status_code == 404 and "utanför" in r.get_data(as_text=True)
    up.eps_answer = requests.ConnectionError("down")
    r = c.get(f"/bjud-in-patient/{P1}")
    assert r.status_code == 503 and "svarade inte" in r.get_data(as_text=True)


def test_eps_without_a_full_name_is_refused(c, up):
    up.state[P1] = _state(family=None)
    _open(c)
    r = c.get(f"/bjud-in-patient/{P1}")
    assert r.status_code == 409 and "inget fullständigt namn" in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# the two steps, CSRF, sso errors
# --------------------------------------------------------------------------

def test_csrf_is_required_on_both_posts(c, up):
    _open(c)
    for step in ("bekrafta", "skicka"):
        r = _post(c, step, csrf=False)
        assert r.status_code == 403, step
        r = _post(c, step, csrf=False, csrf_token="wrong")
        assert r.status_code == 403, step
    assert up.sso_invites() == []


def test_send_needs_confirmed_ja(c, up):
    _open(c)
    r = _post(c, "skicka", confirmed="")
    assert r.status_code == 400 and "bekräftats" in r.get_data(as_text=True)
    assert up.sso_invites() == []


def test_confirm_calls_no_sso_and_carries_unit_and_mobile_forward(c, up):
    _open(c)
    html = _post(c, "bekrafta").get_data(as_text=True)
    assert up.sso_invites() == []
    assert f'action="/bjud-in-patient/{P1}/skicka"' in html
    assert 'name="confirmed" value="ja"' in html
    assert 'name="mobile" value="+46700000001"' in html


def test_sso_patient_already_linked(c, up):
    up.sso_answer = _Resp(409, {"error": "patient_already_linked"})
    _open(c)
    r = _post(c, "skicka")
    assert r.status_code == 409
    assert "Patienten har redan ett konto" in r.get_data(as_text=True)
    assert "iv-sms" not in r.get_data(as_text=True)


@pytest.mark.parametrize("answer,status,text", [
    (requests.ConnectionError("down"), 503, "Inloggningstjänsten (sso) svarade inte"),
    (_Resp(403, {"error": "patient_not_at_unit"}), 403, "kunde inte se patienten"),
    (_Resp(500, None, "<html>boom</html>"), 503, "svarade inte"),
])
def test_sso_failures_are_named(c, up, answer, status, text):
    up.sso_answer = answer
    _open(c)
    r = _post(c, "skicka")
    assert r.status_code == status
    assert text in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# the done page: the sms: link, once, no-store
# --------------------------------------------------------------------------

def test_the_done_page_forms_the_message_exactly(app, c, up):
    _open(c)
    r = _post(c, "skicka")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    href = _sms_href(html)
    text = _expected_text()
    assert href == "sms:+46700000001&body=" + quote(text, safe="")
    recipient, body = href[len("sms:"):].split("&body=", 1)
    assert recipient == "+46700000001"
    assert "&" not in body and " " not in body and "+" not in body
    assert unquote(body) == text
    assert unquote(body).count(INVITE_URL) == 1
    assert "Skicka inbjudan i Meddelanden" in html
    assert "Meddelanden öppnas med texten ifylld – tryck skicka" in html
    assert "skickat" not in html.lower().replace("skickades", "")
    assert 'id="iv-copy-text"' in html and 'id="iv-copy-link"' in html
    # Nothing identifying in the audit rows.
    rows = _audit(app)
    dump = json.dumps(rows, ensure_ascii=False)
    for secret in (INVITE_URL, "ONE-TIME", "Synt", "Inbjudensson", "700000001"):
        assert secret not in dump
    sent = [a for a in rows if a["event"] == "patient_invite_send"]
    assert sent[-1]["payload"]["outcome"] == "invited"
    assert sent[-1]["patient"] == P1


def test_an_invite_link_with_reserved_characters_is_encoded_in_the_body(c, up):
    odd = "https://sso.test/patient-invite/a&b=c?d#e+f"
    up.sso_answer = _Resp(201, {"invite_guid": "g", "invite_url": odd,
                                "expires_at": "2026-11-29T12:00:00+00:00"})
    _open(c)
    href = _sms_href(_post(c, "skicka").get_data(as_text=True))
    body = href.split("&body=", 1)[1]
    assert href.count("&") == 1 and "#" not in href and "?" not in href
    assert unquote(body) == _expected_text(url=odd)


def test_the_sms_uses_the_confirmed_number_and_the_chosen_unit(c, up):
    up.blob = _blob(units=(UNIT_A, UNIT_B))
    up.listed[P1] = [UNIT_A, UNIT_B]
    _open(c)
    html = _post(c, "skicka", care_unit_guid=UNIT_B, mobile="073-000 00 09").get_data(as_text=True)
    href = _sms_href(html)
    assert href.startswith("sms:+46730000009&body=")
    assert unquote(href.split("&body=", 1)[1]) == _expected_text(unit="Vårdcentralen Söder")
    assert "sparas inte" in html
