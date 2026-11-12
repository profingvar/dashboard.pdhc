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
    js = _js()
    assert "mouseenter" in js
    assert "mouseleave" not in js and "mouseout" not in js
    assert re.search(r"mouseenter'[^}]*cap\.hasHover[^}]*setEps\(true", js, re.S)


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
