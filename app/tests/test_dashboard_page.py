"""The patient dashboard PAGE (#976): shell, layout contract, card demographics.

The page's behaviour in a browser (disclosure by keyboard/tap/hover, real iPad
widths) is Gate 2's Playwright work in Phase 6; these tests pin what can be
pinned without a browser: the shell carries no patient data, the regions
exist, and the layout rules Gate 0 corrected are actually in the stylesheet.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.tests.test_dashboard_card import _app, _card, _summary, _wire

STATIC = Path(__file__).resolve().parents[1] / "static"


def _css() -> str:
    text = (STATIC / "css" / "dashboard.css").read_text()
    # Comments explain WHY 1024 is wrong and so quote it; assert on rules only.
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _page(guid="p1"):
    app = _app()
    with app.test_client() as c:
        return c.get(f"/dashboard/view/{guid}")


def test_the_page_renders_the_four_regions_and_its_scripts():
    res = _page()
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    for region in ("panel-card", "panel-plan", "panel-events", "panel-series"):
        assert f'id="{region}"' in html
    assert 'data-patient-guid="p1"' in html
    assert "js/capability.js" in html and "js/dashboard.js" in html
    assert "css/dashboard.css" in html


def test_the_page_is_a_shell_and_reads_no_upstream(monkeypatch):
    """Every datum comes through the guarded JSON routes. If the page itself
    called a service it would read patient data without the audit."""
    import app.routes.dashboard as dash

    def boom(*a, **k):
        raise AssertionError("the page shell must not call an upstream")
    monkeypatch.setattr(dash.cdr1_client, "build_client", boom)
    monkeypatch.setattr(dash.ips_client, "_default_client", boom)
    assert _page().status_code == 200


def test_a_hostile_guid_is_escaped():
    res = _page('x"><script>alert(1)</script>')
    html = res.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html


def test_the_eps_detail_is_a_disclosure_not_a_popover():
    html = _page().get_data(as_text=True)
    assert re.search(r'<button[^>]*id="eps-toggle"[^>]*aria-expanded="false"', html, re.S)
    assert 'aria-controls="eps-detail"' in html
    assert re.search(r'id="eps-detail"[^>]*hidden', html, re.S)


# --------------------------------------------------------------------------
# the layout contract (Gate 0, Measure 4)
# --------------------------------------------------------------------------

def _media_min_widths(css: str) -> list[int]:
    return [int(w) for w in re.findall(r"@media\s*\(\s*min-width:\s*(\d+)px\s*\)", css)]


def test_two_columns_from_1000px_and_NOT_on_an_ipad_width():
    widths = _media_min_widths(_css())
    assert widths == [1000]
    # Common iPad CSS widths, portrait and landscape. A breakpoint ON one of
    # these flips layout for the commonest devices on rotation-adjacent sizes.
    ipad = {744, 768, 810, 820, 834, 1024, 1080, 1112, 1133, 1180, 1194, 1366}
    assert not set(widths) & ipad


def test_every_ipad_landscape_gets_two_columns_and_portrait_one():
    bp = _media_min_widths(_css())[0]
    assert all(w >= bp for w in (1024, 1080, 1112, 1133, 1180, 1194, 1366))
    assert all(w < bp for w in (744, 768, 810, 820, 834))


def test_panels_contain_overscroll_and_targets_are_44px():
    css = _css()
    panel = re.search(r"\.dash-panel\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"overscroll-behavior:\s*contain", panel)
    assert re.search(r"overflow:\s*auto", panel)
    target = re.search(r"\.dash-chip, \.dash-toggle, \.dash-close\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"min-height:\s*44px", target)
    assert re.search(r"min-width:\s*44px", target)


def test_the_plan_column_is_designed_for_320px():
    css = _css()
    assert re.search(r"grid-template-columns:\s*minmax\(320px,\s*1fr\)\s*minmax\(0,\s*2fr\)", css)


# --------------------------------------------------------------------------
# the script
# --------------------------------------------------------------------------

def _js() -> str:
    text = (STATIC / "js" / "dashboard.js").read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_the_script_never_writes_server_text_as_html():
    assert "innerHTML" not in _js()
    assert "insertAdjacentHTML" not in _js()


def test_hover_only_OPENS_and_only_with_real_hover():
    """The EPS disclosure: hover opens, never closes. (The event list and the
    plan link use mouseleave deliberately, to end a PREVIEW; scope this to
    the disclosure.)"""
    eps = _fn(_js(), "wireEps")
    assert "mouseenter" in eps
    assert "mouseleave" not in eps and "mouseout" not in eps
    assert re.search(r"mouseenter'[^}]*cap\.hasHover[^}]*setEps\(true", eps, re.S)


def test_escape_closes():
    assert re.search(r"ev\.key\s*!==\s*'Escape'", _js())


# --------------------------------------------------------------------------
# the card's demographics
# --------------------------------------------------------------------------

class _Cdr1Named:
    base_url = "https://cdr.example"

    def list_org_patients(self, orgs, *, is_admin=False, reason=None):
        return [{"guid": "p0", "name": "Not this one", "birth_date": "1990-01-01"},
                {"guid": "p1", "name": "Testa Testsson", "birth_date": "1951-03-04"}]


def test_name_and_birth_date_come_from_the_patient_index(monkeypatch):
    app = _app()
    _wire(monkeypatch, _summary())
    import app.routes.dashboard as dash
    monkeypatch.setattr(dash.cdr1_client, "build_client", lambda: _Cdr1Named())
    card = _card(app)["patientCard"]
    assert card["name"] == "Testa Testsson"
    assert card["birthDate"] == "1951-03-04"


def test_fields_with_no_source_stay_None_not_empty_string(monkeypatch):
    """None renders as "—" = not known here. An empty string would be a value."""
    app = _app()
    _wire(monkeypatch, _summary())
    card = _card(app)["patientCard"]
    assert card["name"] is None and card["birthDate"] is None
    assert card["sex"] is None and card["personnummer"] is None
    assert card["organisation"] is None


def test_the_stale_events_placeholder_is_gone(monkeypatch):
    app = _app()
    _wire(monkeypatch, _summary())
    assert "eventsPage" not in _card(app)


def test_the_card_no_longer_reads_request(monkeypatch):
    """The plan panel has its own route; the card must not pay for a list
    nothing displays."""
    app = _app()
    _wire(monkeypatch, _summary())
    import app.services.request_client as rc

    def boom():
        raise AssertionError("the card must not call request.pdhc")
    monkeypatch.setattr(rc, "build_client", boom)
    body = _card(app)
    assert "plans" not in body
    assert not any(u.startswith("request:") for u in body["unavailable"])


# --------------------------------------------------------------------------
# the event list (#977)
# --------------------------------------------------------------------------

def _fn(js: str, name: str) -> str:
    """The body of `function name(...) { ... }`, brace-matched."""
    start = js.index(f"function {name}(")
    i = js.index("{", start)
    depth = 0
    for j in range(i, len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[i:j + 1]
    raise AssertionError(name)


def test_the_list_is_a_listbox_with_a_persistent_detail_region():
    html = _page().get_data(as_text=True)
    assert re.search(r'data-role="ev-list"[^>]*role="listbox"[^>]*tabindex="0"', html, re.S)
    assert re.search(r'data-role="ev-detail"[^>]*aria-live="polite"', html, re.S)


def test_there_is_NO_pin():
    js = _js().lower()
    html = _page().get_data(as_text=True).lower()
    assert "pin" not in re.sub(r"spinner|pinned_at", "", js)
    assert "pin" not in re.sub(r"spinner", "", html)


def test_arrow_keys_drive_the_SAME_path_as_a_click():
    js = _js()
    on_key = _fn(js, "onKey")
    assert "ArrowDown" in on_key and "ArrowUp" in on_key
    assert "select(i, true)" in on_key
    row = _fn(js, "rowNode")
    assert "select(i, true)" in row


def test_hover_PREVIEWS_after_300ms_and_only_with_real_hover():
    row = _fn(_js(), "rowNode")
    assert re.search(r"if \(!cap\.hasHover\) \{ return; \}", row)
    assert re.search(r"setTimeout\(function \(\) \{ preview\(i\); \}, 300\)", row)


def test_a_newer_selection_CANCELS_the_detail_request_in_flight():
    show = _fn(_js(), "showDetail")
    assert "controller.abort()" in show
    assert "signal:" in show
    assert "AbortError" in show


def test_absent_provenance_is_said_BY_NAME():
    js = _js()
    assert "var MISSING = 'ej registrerat';" in js
    block = _fn(js, "block")
    assert "empty ? MISSING : v" in block
    # every DTO provenance field is listed, even when it is None
    detail = _fn(js, "renderDetail")
    for field in ("planActivityGuid", "serviceRequestGuid", "carePlanGuid",
                  "contractGuid", "patientOrgGuid", "providerOrgGuid",
                  "ingestedAt", "ingestRawGuid", "interpretation", "method", "device"):
        assert field in detail, field


def test_row_height_comes_from_capability_and_follows_changes():
    js = _js()
    assert "cap.rowHeight()" in _fn(js, "rowH")
    assert "cap.subscribe(" in js
    css = _css()
    assert re.search(r"\.ev-row\s*\{[^}]*height:\s*var\(--ev-row-h", css, re.S)
    assert re.search(r"\.ev-list\s*\{[^}]*overscroll-behavior:\s*contain", css, re.S)


def test_only_the_visible_window_is_rendered():
    render = _fn(_js(), "render")
    assert "Math.floor(top / h) - OVERSCAN" in render
    assert "for (var i = first; i <= last; i++)" in render


def test_a_bounded_scan_never_says_NONE():
    """Found by screenshot: a done item showed "≥0" and "no observations
    exist" while the scan had stopped at its bound. Unknown is not zero."""
    js = _js()
    count = _fn(js, "countText")
    assert "if (!data.observationCountsComplete)" in count
    assert "'inga bland genomsökta'" in count
    plan = _fn(js, "renderPlan")
    assert re.search(r"data\.observationCountsComplete\s*\?\s*'Klar enligt beställningen, men inga mätvärden finns\.'", plan)


def test_a_lower_bound_is_never_printed_as_a_total():
    """Found by benchmark: "Markerar 2500 av 1250"."""
    link = _fn(_js(), "describeLink")
    assert "linked.complete ? ' av ' + total" in link
