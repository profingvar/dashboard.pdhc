"""#1049 — card versions in "EPS att granska" (contract s7.4/s7.5).

Same harness as #1044: the real routes, AUTH_MODE=sso, and ONLY the outbound
HTTP boundary (`requests.get` / `requests.post`) patched. The fake below adds
cdr1's s7.4 card API to #1044's fake of sso + the per-row review API. No
client of ours is patched, so a wrong URL, header or body is a failing test.

Synthetic data only. Controls for each guard: progress.md (2026-11-22, #1049).
"""
from __future__ import annotations

import copy
import re

import pytest
import requests

from app.tests.test_eps_review_1044 import (  # noqa: F401  (fixture `app`)
    CDR, P1, P2, R1, TOKEN, FakeUpstreams, _HTML_404, _Resp, _audit_rows,
    _admin, _items, _nurse, app,
)

CV_ALLERGY = "11111111-0000-4000-8000-000000000001"
CV_MED = "11111111-0000-4000-8000-000000000002"
CV_HIST = "11111111-0000-4000-8000-000000000003"
CV_GONE = "11111111-0000-4000-8000-0000000000ff"

# proposed rows (guids are the ORIGINALS in cdr1)
A_SUB0, A_REA0, A_SEV0 = ("a0000000-0000-4000-8000-00000000000%d" % i for i in (1, 2, 3))
A_SUB1, A_REA1, A_SEV1 = ("a0000000-0000-4000-8000-00000000001%d" % i for i in (1, 2, 3))
A_STATUS = "a0000000-0000-4000-8000-000000000021"
M_COUNT = "b0000000-0000-4000-8000-000000000001"
M_NOTE = "b0000000-0000-4000-8000-000000000002"
H_SURG = "c0000000-0000-4000-8000-000000000001"
H_SMOKE = "c0000000-0000-4000-8000-000000000002"

REMOVED_ALLERGY = "Jordnötter"
ADDED_ALLERGY = "Hasselnötter"
REASON = "Stämmer inte med journalen, ring mottagningen"
NOTE = "Bekräftat vid telefonsamtal"


def _row(guid, link_id, label, value, display=None, group=None):
    return {"guid": guid, "link_id": link_id, "group": group, "label": label,
            "value": value, "display": display}


def _cards():
    """Three pending versions: P1 allergy (instances), P1 medication (single
    rows), P2 history (nothing endorsed yet)."""
    return [
        {"card_version": CV_ALLERGY, "patient_guid": P1, "section": "allergy",
         "submitted_at": "2026-11-21T08:00:00+00:00",
         "rows": [
             _row(A_SUB0, "allergy.substance", "Ämne", "Penicillin", group="rcpt2/0"),
             _row(A_REA0, "allergy.reaction", "Reaktion", "Utslag", group="rcpt2/0"),
             _row(A_SEV0, "allergy.severity", "Svårighetsgrad", "severe", "Svår", group="rcpt2/0"),
             _row(A_SUB1, "allergy.substance", "Ämne", ADDED_ALLERGY, group="rcpt2/1"),
             _row(A_REA1, "allergy.reaction", "Reaktion", "Svullnad", group="rcpt2/1"),
             _row(A_SEV1, "allergy.severity", "Svårighetsgrad", "mild", "Lindrig", group="rcpt2/1"),
             _row(A_STATUS, "allergy.status", "Har du allergier?", "has", "Ja, jag har allergier"),
         ],
         "endorsed": {"card_version": "11111111-0000-4000-8000-0000000000e1",
                      "reviewed_at": "2026-11-10T12:00:00+00:00",
                      "rows": [
             _row("e1", "allergy.substance", "Ämne", "Penicillin", group="rcpt1/0"),
             _row("e2", "allergy.reaction", "Reaktion", "Utslag", group="rcpt1/0"),
             _row("e3", "allergy.severity", "Svårighetsgrad", "severe", "Svår", group="rcpt1/0"),
             _row("e4", "allergy.substance", "Ämne", REMOVED_ALLERGY, group="rcpt1/1"),
             _row("e5", "allergy.reaction", "Reaktion", "Klåda", group="rcpt1/1"),
             _row("e6", "allergy.severity", "Svårighetsgrad", "mild", "Lindrig", group="rcpt1/1"),
             _row("e7", "allergy.status", "Har du allergier?", "has", "Ja, jag har allergier"),
         ]}},
        {"card_version": CV_MED, "patient_guid": P1, "section": "medication",
         "submitted_at": "2026-11-21T08:05:00+00:00",
         "rows": [
             _row(M_COUNT, "medication.count", "Antal läkemedel", 2),
             _row(M_NOTE, "medication.note", "Övrigt", "Tar Waran"),
         ],
         "endorsed": {"card_version": "11111111-0000-4000-8000-0000000000e2",
                      "reviewed_at": "2026-11-10T12:00:00+00:00",
                      "rows": [
             _row("f1", "medication.count", "Antal läkemedel", 3),
             _row("f2", "medication.other", "Annat läkemedel", "Alvedon"),
         ]}},
        {"card_version": CV_HIST, "patient_guid": P2, "section": "history",
         "submitted_at": "2026-11-21T09:00:00+00:00",
         "rows": [
             _row(H_SURG, "history.surgery", "Tidigare operationer", "Blindtarm 1998"),
             _row(H_SMOKE, "history.smoker", "Röker du?", True),
         ],
         "endorsed": None},
    ]


class CardUpstreams(FakeUpstreams):
    """#1044's fake + cdr1's s7.4 card routes."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cards = _cards()
        self.card_decided: dict[str, str] = {}
        self.superseded: set[str] = set()
        self.card_list_response = None
        self.card_decision_response = None
        self.card_gets: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        if url == f"{CDR}/api/v1/reviews/cards":
            headers = headers or {}
            self.card_gets.append({"headers": dict(headers), "params": list(params or [])})
            refusal = self._credential_refusal(headers)
            if refusal:
                return refusal
            if isinstance(self.card_list_response, Exception):
                raise self.card_list_response
            if self.card_list_response is not None:
                return self.card_list_response
            pending = [c for c in self.cards if c["card_version"] not in self.card_decided
                       and c["card_version"] not in self.superseded]
            return _Resp(200, {"items": copy.deepcopy(pending)})
        return super().get(url, headers=headers, params=params, timeout=timeout, **kw)

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        m = re.fullmatch(re.escape(CDR) + r"/api/v1/reviews/cards/([^/]+)/(approve|reject)", url)
        if not m:
            return super().post(url, headers=headers, json=json, timeout=timeout, **kw)
        headers = headers or {}
        cv, verb = m.groups()
        self.cdr_posts.append({"card_version": cv, "verb": verb, "json": json,
                               "headers": dict(headers)})
        refusal = self._credential_refusal(headers)
        if refusal:
            return refusal
        if isinstance(self.card_decision_response, Exception):
            raise self.card_decision_response
        if self.card_decision_response is not None:
            return self.card_decision_response
        card = next((c for c in self.cards if c["card_version"] == cv), None)
        if card is None:
            return _Resp(403, {"error": "forbidden"})
        if cv in self.superseded:
            return _Resp(409, {"error": "superseded"})
        if cv in self.card_decided:
            return _Resp(409, {"error": "already_decided"})
        if verb == "reject":
            if not str((json or {}).get("reason") or "").strip():
                return _Resp(422, {"error": "reason_required"})
            self.card_decided[cv] = "rejected"
            return _Resp(200, {"review_status": "rejected"})
        amend = (json or {}).get("amend") or {}
        own = {r["guid"] for r in card["rows"]}
        if set(amend) - own:
            return _Resp(422, {"error": "validation", "code": "unknown_row"})
        self.card_decided[cv] = "endorsed"
        return _Resp(201, {"endorsed": len(card["rows"]), "amended": len(amend)})


@pytest.fixture
def up(monkeypatch):
    fake = CardUpstreams()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def c(app, up):
    client = app.test_client()
    with client.session_transaction() as s:
        s["sso_token"] = TOKEN
    return client


def _page(client):
    r = client.get("/eps-granskning")
    return r, r.get_data(as_text=True)


def _csrf(client) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', _page(client)[1])
    assert m, "the queue page carries no CSRF token"
    return m.group(1)


def _confirm(client, cv, **form):
    form.setdefault("csrf_token", _csrf(client))
    return client.post(f"/eps-granskning/kort/{cv}/bekrafta", data=form)


def _decide(client, cv, **form):
    form.setdefault("csrf_token", _csrf(client))
    form.setdefault("confirmed", "ja")
    return client.post(f"/eps-granskning/kort/{cv}/beslut", data=form)


def _card_html(html, cv):
    m = re.search(r'<div class="cd-card" data-card="%s">(.*?)<div class="rv-actions">' % cv,
                  html, re.S)
    assert m, f"card {cv} not rendered"
    return m.group(1)


def _marks(card_html):
    """(mark, proposed-row guid or None, the line's HTML) per data line."""
    return [(m.group(1), m.group(3), m.group(4)) for m in re.finditer(
        r'<div class="cd-row" role="row" data-mark="(\w+)"( data-row="([^"]+)")?>(.*?)</div>',
        card_html, re.S)]


def _flashes(client):
    with client.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def _card_posts(up):
    return [p for p in up.cdr_posts if "card_version" in p]


# --------------------------------------------------------------------------
# the list
# --------------------------------------------------------------------------

def test_cards_are_grouped_per_patient_then_section_in_cdrs_order(c, up):
    r, html = _page(c)
    assert r.status_code == 200
    groups = re.findall(r'<section class="card rv-cgroup" data-patient="([^"]+)"(.*?)</section>',
                        html, re.S)
    assert [g[0] for g in groups] == [P1, P2]
    assert re.findall(r'data-card="([^"]+)"', groups[0][1]) == [CV_ALLERGY, CV_MED]
    assert re.findall(r'data-card="([^"]+)"', groups[1][1]) == [CV_HIST]
    assert re.findall(r"<h3>([^<]+)</h3>", groups[0][1]) == ["Allergier", "Läkemedel"]
    assert "Testsson, Anna" in groups[0][1]
    assert 'id="cd-empty"' not in html and 'id="cd-problem"' not in html
    assert "3 versioner väntar" in html


def test_instances_are_matched_whole_never_paired_by_position(c, up):
    """The patient kept Penicillin, dropped Jordnötter and added Hasselnötter.
    Pairing by position would call Hasselnötter a CHANGED Jordnötter."""
    card = _card_html(_page(c)[1], CV_ALLERGY)
    marks = _marks(card)
    assert [m for m, _, _ in marks] == ["same"] * 3 + ["added"] * 3 + ["same"] + ["removed"] * 3
    assert [g for _, g, _ in marks[:7]] == [A_SUB0, A_REA0, A_SEV0, A_SUB1, A_REA1, A_SEV1, A_STATUS]
    added_sub = marks[3][2]
    assert ADDED_ALLERGY in added_sub and REMOVED_ALLERGY not in added_sub
    assert REMOVED_ALLERGY in marks[7][2]
    assert "changed" not in [m for m, _, _ in marks]


def test_a_coded_answer_is_shown_by_its_display(c, up):
    card = _card_html(_page(c)[1], CV_ALLERGY)
    assert "Svår" in card and "Ja, jag har allergier" in card
    assert ">severe<" not in card and ">has<" not in card


def test_single_rows_marked_changed_added_removed_with_both_values(c, up):
    marks = _marks(_card_html(_page(c)[1], CV_MED))
    assert [(m, g) for m, g, _ in marks] == [("changed", M_COUNT), ("added", M_NOTE),
                                              ("removed", None)]
    changed = marks[0][2]
    assert re.search(r'class="cd-p".*?2</span>', changed, re.S)
    assert re.search(r'class="cd-e".*?3</span>', changed, re.S)
    assert "Alvedon" in marks[2][2]
    for mark, label in (("changed", "Ändrad"), ("added", "Ny"), ("removed", "Borttagen")):
        line = next(h for m, _, h in marks if m == mark)
        assert f'<span class="cd-mark">{label}</span>' in line


def test_same_group_value_is_diffed_field_by_field(c, up):
    """When cdr1 carries a stable group value across versions, an edited field
    of one instance is CHANGED, not the whole instance removed and added."""
    allergy = up.cards[0]
    for r in allergy["endorsed"]["rows"]:
        r["group"] = (r["group"] or "").replace("rcpt1", "rcpt2") or None
    allergy["endorsed"]["rows"][4]["value"] = "Svullnad"     # instance 1: reaction same
    marks = _marks(_card_html(_page(c)[1], CV_ALLERGY))
    assert [m for m, _, _ in marks] == ["same"] * 3 + ["changed", "same", "same", "same"]
    assert 'data-mark="changed"><div class="cd-insthead">Post: Ändrad' in \
        _card_html(_page(c)[1], CV_ALLERGY)


def test_nothing_endorsed_marks_everything_added(c, up):
    card = _card_html(_page(c)[1], CV_HIST)
    assert [m for m, _, _ in _marks(card)] == ["added", "added"]
    assert "ingen tidigare godkänd version" in card


def test_every_data_cell_carries_its_own_label_for_the_stacked_layout(c, up):
    """iPad portrait and phones stack the four columns; each cell then says
    which column it is, so a value is never read against the wrong one."""
    for cv in (CV_ALLERGY, CV_MED, CV_HIST):
        for _, _, line in _marks(_card_html(_page(c)[1], cv)):
            for k in ("Föreslaget", "Nu godkänt", "Markering"):
                assert f'<span class="cd-k">{k}</span>' in line


def test_cards_are_read_with_the_operators_bearer_and_no_service_key(c, up):
    _page(c)
    assert up.card_gets, "the card list was not asked"
    h = up.card_gets[-1]["headers"]
    assert h["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Service-Key" not in h and "X-Source-Service" not in h
    assert ("status", "pending") in up.card_gets[-1]["params"]


def test_no_cards_says_so_and_the_v1_queue_is_still_shown(c, up):
    up.cards = []
    r, html = _page(c)
    assert r.status_code == 200
    assert 'id="cd-empty"' in html and 'id="cd-problem"' not in html
    assert "Det finns inga patientkort att granska för dina vårdenheter just nu." in html
    assert 'class="card rv-group"' in html          # #1044's rows


def test_the_v1_queue_leaves_out_rows_that_belong_to_a_card(c, up):
    """cdr1 may list a card's rows on the per-row route too. They are shown
    once, in their card, and cannot be decided one by one."""
    extra = dict(_items()[0], guid=A_SUB0)                      # in a card's rows
    tagged = dict(_items()[0], guid="bbbbbbbb-0000-4000-8000-0000000000aa",
                  card_version=CV_MED)                          # says so itself
    up.items = _items() + [extra, tagged]
    html = _page(c)[1]
    v1 = html.split('id="rv-part"', 1)[1]
    assert f'data-review="{A_SUB0}"' not in v1
    assert 'data-review="bbbbbbbb-0000-4000-8000-0000000000aa"' not in v1
    assert c.get("/eps-granskning/antal").get_json() == {"count": 4 + 3}
    # and the per-row route will not decide one
    tok = _csrf(c)
    r = c.post("/eps-granskning/bbbbbbbb-0000-4000-8000-0000000000aa/beslut",
               data={"csrf_token": tok, "confirmed": "ja", "action": "approve"})
    assert r.status_code == 303 and up.cdr_posts == []


def test_no_action_is_preselected_or_styled_as_recommended(c, up):
    html = _page(c)[1]
    assert " open>" not in html and "autofocus" not in html and " checked" not in html
    assert "btn-primary" not in html and "btn-success" not in html and "btn-danger" not in html
    # amend inputs start empty: nothing is amended unless the professional types
    assert re.findall(r'name="amend-[^"]+" maxlength="2000" value="([^"]*)"', html) \
        and set(re.findall(r'name="amend-[^"]+" maxlength="2000" value="([^"]*)"', html)) == {""}


def test_coded_rows_offer_no_amend_field(c, up):
    html = _page(c)[1]
    assert f'name="amend-{A_SEV0}"' not in html and f'name="amend-{A_STATUS}"' not in html
    assert f'name="amend-{A_SUB0}"' in html
    assert f'<select id="a-{H_SMOKE}" name="amend-{H_SMOKE}">' in html


# --------------------------------------------------------------------------
# refusal is not an empty list
# --------------------------------------------------------------------------

@pytest.mark.parametrize("response,code,status", [
    (_HTML_404, "card_api_missing", 502),
    (_Resp(403, {"error": "forbidden"}), "forbidden", 403),
    (_Resp(500, {"error": "boom"}), "unreachable", 502),
    (requests.ConnectionError("down"), "unreachable", 502),
    (_Resp(200, {"cards": []}), "unreachable", 502),
    (_Resp(200, {"items": [{"card_version": CV_MED, "rows": "x"}]}), "unreachable", 502),
    (_Resp(200, {"items": [{"card_version": CV_MED, "rows": [], "endorsed": {"rows": None}}]}),
     "unreachable", 502),
])
def test_a_failed_card_read_is_a_named_problem_and_v1_still_shows(app, c, up, response, code, status):
    app.config["PROPAGATE_EXCEPTIONS"] = False   # a crash is a 500 to assert on, not a raise
    up.card_list_response = response
    r, html = _page(c)
    assert r.status_code == status
    assert f'id="cd-problem" data-problem="{code}"' in html
    assert 'id="cd-empty"' not in html
    assert "inga patientkort att granska" not in html
    assert 'class="card rv-group"' in html          # the v1 queue is not lost


def test_the_nav_count_is_unknown_when_the_card_list_fails(app, c, up):
    app.config["PROPAGATE_EXCEPTIONS"] = False   # a crash is a 500 to assert on
    assert c.get("/eps-granskning/antal").get_json() == {"count": 4 + 3}
    up.card_list_response = _Resp(500, {"error": "boom"})
    r = c.get("/eps-granskning/antal")
    assert r.status_code == 502 and r.get_json()["count"] is None


# --------------------------------------------------------------------------
# approve
# --------------------------------------------------------------------------

def test_approve_is_two_steps_and_sends_nothing_at_the_first(c, up):
    r = _confirm(c, CV_MED, action="approve")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert f'data-card="{CV_MED}" data-action="approve"' in html
    assert "Inget har skickats ännu" in html and "rader markerade Borttagen följer inte med" in html
    assert _card_posts(up) == []


def test_approve_without_amend_sends_an_empty_body_in_the_operators_name(c, up):
    r = _decide(c, CV_MED, action="approve", note=NOTE)
    assert r.status_code == 303
    (p,) = _card_posts(up)
    assert p["card_version"] == CV_MED and p["verb"] == "approve"
    assert p["json"] == {"note": NOTE}
    assert p["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Service-Key" not in p["headers"]
    assert any("är godkänd" in f for f in _flashes(c))


def test_approve_with_amend_sends_only_the_amended_rows_in_their_types(c, up):
    form = {"action": "approve", f"amend-{M_COUNT}": "4", f"amend-{M_NOTE}": "Tar Eliquis"}
    r = _confirm(c, CV_MED, **form)
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert f'data-amended="{M_COUNT}"' in html and "Tar Eliquis" in html
    assert f'name="amend-{M_NOTE}" value="Tar Eliquis"' in html
    r = _decide(c, CV_MED, **form)
    assert r.status_code == 303
    assert _card_posts(up)[0]["json"] == {"amend": {M_COUNT: 4, M_NOTE: "Tar Eliquis"}}
    assert any("2 värde(n) ändrades" in f for f in _flashes(c))


def test_amend_of_a_bool_row(c, up):
    _decide(c, CV_HIST, action="approve", **{f"amend-{H_SMOKE}": "nej"})
    assert _card_posts(up)[0]["json"] == {"amend": {H_SMOKE: False}}


def test_a_field_for_a_row_outside_the_version_is_not_read(c, up):
    _decide(c, CV_MED, action="approve", **{f"amend-{H_SURG}": "Gallsten 2001"})
    assert _card_posts(up)[0]["json"] == {}


@pytest.mark.parametrize("field,value,why", [
    (f"amend-{A_SEV0}", "mild", "kan inte ändras här"),         # coded
    (f"amend-{A_SUB0}", "Penicillin", "oförändrat"),             # unchanged
])
def test_an_unusable_amend_is_stopped_before_cdr(c, up, field, value, why):
    for step in (_confirm, _decide):
        r = step(c, CV_ALLERGY, action="approve", **{field: value})
        assert r.status_code == 400
        assert why in r.get_data(as_text=True)
    assert _card_posts(up) == []


def test_a_wrong_typed_number_amend_is_stopped(c, up):
    r = _confirm(c, CV_MED, action="approve", **{f"amend-{M_COUNT}": "två"})
    assert r.status_code == 400 and "Antal läkemedel: Värdet måste vara ett tal" in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# reject
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["", "   "])
def test_reject_without_a_reason_is_stopped(c, up, reason):
    for step in (_confirm, _decide):
        r = step(c, CV_MED, action="reject", reason=reason)
        assert r.status_code == 400 and "Ett skäl krävs" in r.get_data(as_text=True)
    assert _card_posts(up) == []


def test_reject_sends_the_reason_only(c, up):
    r = _confirm(c, CV_MED, action="reject", reason=REASON)
    assert r.status_code == 200 and REASON in r.get_data(as_text=True)
    r = _decide(c, CV_MED, action="reject", reason=REASON, **{f"amend-{M_COUNT}": "9"})
    assert r.status_code == 303
    (p,) = _card_posts(up)
    assert p["verb"] == "reject" and p["json"] == {"reason": REASON}
    assert any("är avvisad" in f for f in _flashes(c))


# --------------------------------------------------------------------------
# 409 and the other answers of cdr1
# --------------------------------------------------------------------------

def test_already_decided_is_told_in_swedish(c, up):
    up.card_decision_response = _Resp(409, {"error": "already_decided"})
    r = _decide(c, CV_MED, action="approve")
    assert r.status_code == 303
    (f,) = [x for x in _flashes(c) if "registrerades inte" in x]
    assert "redan beslutat" in f and "nyare version" not in f


def test_superseded_is_told_apart_from_already_decided(c, up):
    up.card_decision_response = _Resp(409, {"error": "superseded"})
    r = _decide(c, CV_MED, action="reject", reason=REASON)
    assert r.status_code == 303
    (f,) = [x for x in _flashes(c) if "registrerades inte" in x]
    assert "nyare version av avsnittet Läkemedel" in f and "redan beslutat" not in f


def test_a_version_no_longer_pending_is_gone_before_any_post(c, up):
    tok = _csrf(c)
    up.superseded.add(CV_MED)                  # the patient sent a newer one meanwhile
    for step in ("bekrafta", "beslut"):
        r = c.post(f"/eps-granskning/kort/{CV_MED}/{step}",
                   data={"csrf_token": tok, "confirmed": "ja", "action": "approve"})
        assert r.status_code == 303
    assert _card_posts(up) == []
    assert any("nyare version som ersatt den" in f for f in _flashes(c))


def test_cdr_refusing_the_content_is_a_422_with_nothing_recorded(c, up):
    up.card_decision_response = _Resp(422, {"error": "validation", "code": "wrong_type"})
    r = _decide(c, CV_MED, action="approve", **{f"amend-{M_NOTE}": "x"})
    assert r.status_code == 422 and "fel typ" in r.get_data(as_text=True)


def test_an_unanswered_card_decision_is_unknown(c, up):
    up.card_decision_response = requests.Timeout("slow")
    r = _decide(c, CV_MED, action="approve")
    assert r.status_code == 502 and "OKÄNT" in r.get_data(as_text=True)


def test_cdr_403_on_a_card_decision(c, up):
    up.card_decision_response = _Resp(403, {"error": "forbidden"})
    r = _decide(c, CV_MED, action="approve")
    assert r.status_code == 403 and "inget har registrerats" in r.get_data(as_text=True)


# --------------------------------------------------------------------------
# gate, CSRF, can_decide, path
# --------------------------------------------------------------------------

def test_can_decide_false_shows_no_actions_and_posts_nothing(c, up):
    up.cards[1]["can_decide"] = False
    html = _page(c)[1]
    assert f'action="/eps-granskning/kort/{CV_MED}/bekrafta"' not in html
    assert f'action="/eps-granskning/kort/{CV_ALLERGY}/bekrafta"' in html
    r = _decide(c, CV_MED, action="approve")
    assert r.status_code == 403 and _card_posts(up) == []


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
@pytest.mark.parametrize("step", ["bekrafta", "beslut"])
def test_csrf_missing_or_wrong_is_refused(c, up, token, step):
    _csrf(c)
    gets = len(up.card_gets)
    data = {"action": "approve", "confirmed": "ja"}
    if token is not None:
        data["csrf_token"] = token
    r = c.post(f"/eps-granskning/kort/{CV_MED}/{step}", data=data)
    assert r.status_code == 403 and "CSRF" in r.get_data(as_text=True)
    assert _card_posts(up) == [] and len(up.card_gets) == gets


def test_an_unconfirmed_decision_is_not_sent(c, up):
    r = _decide(c, CV_MED, action="approve", confirmed="")
    assert r.status_code == 400 and _card_posts(up) == []


@pytest.mark.parametrize("who", ["patient", "no unit"])
def test_a_user_outside_the_gate_is_refused(c, up, who):
    up.blob = ({"user_guid": "p", "user_type": "patient", "is_su_admin": False,
                "patient_link": {"ips_patient_guid": P1}} if who == "patient"
               else _nurse(affiliations=[]))
    for step in ("bekrafta", "beslut"):
        r = c.post(f"/eps-granskning/kort/{CV_MED}/{step}",
                   data={"action": "approve", "confirmed": "ja"})
        assert r.status_code == 403
    assert up.card_gets == [] and up.cdr_posts == []


def test_an_admin_must_attest_before_a_card_is_looked_up(c, up):
    up.blob = _admin()
    r = c.get("/eps-granskning")
    assert r.status_code == 428 and up.card_gets == []
    with c.session_transaction() as s:
        tok = s.get("_csrf_token") or "x"
        s["_csrf_token"] = tok
    for step in ("bekrafta", "beslut"):
        r = c.post(f"/eps-granskning/kort/{CV_MED}/{step}",
                   data={"csrf_token": tok, "confirmed": "ja", "action": "approve"})
        assert r.status_code == 428
    assert up.card_gets == [] and _card_posts(up) == []


def test_a_card_version_that_is_not_a_uuid_is_404(c, up):
    tok = _csrf(c)
    r = c.post("/eps-granskning/kort/not-a-uuid/beslut",
               data={"csrf_token": tok, "confirmed": "ja", "action": "approve"})
    assert r.status_code == 404 and _card_posts(up) == []


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def test_audit_carries_identifiers_and_counts_never_values(app, c, up):
    _decide(c, CV_MED, action="approve", note=NOTE,
            **{f"amend-{M_NOTE}": "Tar Eliquis"})
    _decide(c, CV_HIST, action="reject", reason=REASON)
    rows = {r["event"]: r for r in _audit_rows(app)
            if r["event"] in ("eps_card_approve", "eps_card_reject")}
    a, j = rows["eps_card_approve"], rows["eps_card_reject"]
    assert a["payload"]["card_version"] == CV_MED and a["payload"]["section"] == "medication"
    assert a["payload"]["n_amended"] == 1 and a["payload"]["outcome"] == "sent"
    assert a["patient"] == P1 and j["patient"] == P2
    blob = repr([a, j])
    for secret in ("Tar Eliquis", "Tar Waran", NOTE, REASON):
        assert secret not in blob
