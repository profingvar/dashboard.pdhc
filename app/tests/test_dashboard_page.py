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
    # As a WORD: "pinch" (zoom) and "spinner" are not a pin feature.
    pin = re.compile(r"\bpin(?:s|ned|ning)?\b|\bunpin")
    js = _js().lower()
    html = _page().get_data(as_text=True).lower()
    assert not pin.search(js)
    assert not pin.search(html)
    assert pin.search("const pinned = 1")      # the matcher can see one


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


# --------------------------------------------------------------------------
# the series panel (#978)
# --------------------------------------------------------------------------

def test_the_chart_is_driven_by_SELECTION_not_hover():
    js = _js()
    assert "events.onSelect(show)" in js
    show = _fn(js, "show")
    assert "mouse" not in show


def test_a_gap_over_3x_the_median_BREAKS_the_line():
    gaps = _fn(_js(), "withGaps")
    assert "3 * median" in gaps
    assert "y: null" in gaps
    assert "spanGaps: false" in _js()


def test_a_vertical_drag_still_scrolls_the_page():
    """A chart that swallows vertical scroll makes the page unusable on touch."""
    js = _js()
    assert "canvas.style.touchAction = 'pan-y'" in js
    # set AFTER the chart (and hammer) attach, or hammer overrides it
    draw = _fn(js, "draw")
    assert draw.index("chart = new Chart(") < draw.index("canvas.style.touchAction = 'pan-y'")


def test_the_mouse_wheel_does_not_hijack_page_scroll():
    assert "wheel: { enabled: true, modifierKey: 'ctrl' }" in _js()


def test_colour_is_never_the_only_signal():
    draw = _fn(_js(), "draw")
    assert "'rectRot'" in draw and "'triangle'" in draw   # selected / out of range
    marks = _fn(_js(), "markers")
    dashes = set(re.findall(r"dash: (\[[^\]]*\])", marks))
    assert len(dashes) >= 3, dashes                         # each kind its own dash
    assert "label:" in marks


def test_the_zoom_plugin_and_hammer_load_before_the_page_script():
    html = _page().get_data(as_text=True)
    h, z, d = (html.index(x) for x in ("vendor/hammer.min.js",
                                         "vendor/chartjs-plugin-zoom.umd.min.js",
                                         "js/dashboard.js"))
    assert h < z < d


def test_a_tick_shows_the_time_when_not_at_midnight():
    tick = _fn(_js(), "fmtTick")
    assert "t === '00:00' ? day : day + ' ' + t" in tick


# --------------------------------------------------------------------------
# filters (#977 part B) and zoom refinement
# --------------------------------------------------------------------------

def test_the_filter_bar_has_every_filter():
    html = _page().get_data(as_text=True)
    for name in ("q", "code", "from", "to", "plan"):
        assert f'name="{name}"' in html, name
    assert 'role="search"' in html


def test_filters_live_in_the_URL():
    js = _js()
    assert "history.replaceState(" in _fn(js, "writeUrl")
    assert "new URLSearchParams(window.location.search)" in _fn(js, "readUrl")


def test_text_that_matches_nothing_shows_NOTHING_not_everything():
    apply_ = _fn(_js(), "apply")
    assert re.search(r"if \(!hits\.length\) \{ noMatch = true; note = 'Inga av patientens begrepp matchar", apply_)


def test_text_search_needs_the_concept_list_and_says_so():
    apply_ = _fn(_js(), "apply")
    assert "if (!conceptsKnown)" in apply_


def test_a_stale_page_cannot_land_in_a_new_filter():
    js = _js()
    assert "if (mine !== gen) { return; }" in _fn(js, "loadMore")
    assert "gen += 1;" in _fn(js, "reload")


def test_zoom_refines_to_full_resolution_and_never_claims_all_when_downsampled():
    refine = _fn(_js(), "refine")
    assert "if (!chart || !s || !s.downsampledFrom) { return; }" in refine
    assert "w.downsampledFrom" in refine
    assert "refineCtl.abort()" in refine


def test_an_invalid_URL_filter_is_REFUSED_not_silently_dropped():
    """Found in the browser: ?from=2026-13-40 — the date input refused the
    value silently, the URL was rewritten without it, and the whole history
    was shown as if filtered."""
    js = _js()
    read = _fn(js, "readUrl")
    assert "if (form.elements[k].value !== q.get(k)) { rejected.push(" in read
    apply_ = _fn(js, "apply")
    assert apply_.index("if (rejected.length)") < apply_.index("writeUrl();")
    assert "events.reload('', true, true);" in apply_


def test_the_no_match_note_is_not_overwritten_afterwards():
    """Found in the browser: a later line reset the note to '' on no hits."""
    apply_ = _fn(_js(), "apply")
    assert ": '';" not in apply_
    assert "if (hits.length) { note = 'Söker i '" in apply_


# --------------------------------------------------------------------------
# limits on the page (MDR class IIa) — rendered, never computed
# --------------------------------------------------------------------------

def test_the_page_NEVER_compares_a_value_to_a_limit():
    js = _js()
    oor = _fn(js, "outOfRange")
    assert "pt.ls === 'above' || pt.ls === 'below'" in oor
    assert "<" not in oor.replace("pt.ls", "") and ">" not in oor
    assert "referenceLow" not in _fn(js, "draw")


def test_an_outside_row_is_marked_by_SYMBOL_and_WORD_not_colour_alone():
    row = _fn(_js(), "rowNode")
    assert "LIMIT_SYMBOL[lim.status]" in row
    assert "'över' : 'under'" in row


def test_the_detail_shows_the_limit_FIRST_with_its_order():
    js = _js()
    detail = _fn(js, "renderDetail")
    assert detail.index("renderLimit(") < detail.index("block('Värde'")
    lim = _fn(js, "renderLimit")
    assert "'Gränsen kommer från patientens beställning ' + l.orderGuid" in lim
    # the observation's own order is labelled as such, so the two cannot be confused
    assert "'Beställning registrerad på mätvärdet'" in _fn(js, 'renderDetail')
    assert "l.reason && !judged" in lim


def test_every_not_judged_state_has_words():
    js = _js()
    for st in ("no_limit", "unit_mismatch", "conflict", "invalid_limit", "not_numeric", "unknown"):
        assert re.search(rf"\b{st}: 'ej bedömd|\b{st}: 'ingen gräns'", js), st


def test_the_outside_filter_is_in_the_bar_and_the_URL():
    html = _page().get_data(as_text=True)
    assert 'name="outside"' in html
    js = _js()
    assert "if (form.elements.outside.checked) { q.set('outside', '1'); }" in _fn(js, "writeUrl")
    assert "parts.push('outside=1')" in _fn(js, "apply")
