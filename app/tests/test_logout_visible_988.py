"""#988 item 8: the shared nav's Logout button was white text on light grey."""
import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parents[1] / "templates" / "base.html"
CSS = pathlib.Path(__file__).resolve().parents[1] / "static" / "css" / "pdhc.css"


def _logout_tag():
    m = re.search(r"<a [^>]*auth\.logout[^>]*>", BASE.read_text())
    assert m, "no logout link in base.html"
    return m.group(0)


def test_the_logout_link_sets_no_inline_colour():
    assert "style=" not in _logout_tag()


def test_btn_secondary_is_dark_text_on_a_light_background():
    rule = re.search(r"\.btn-secondary\s*\{([^}]*)\}", CSS.read_text()).group(1)
    assert "var(--text)" in rule and "#e2e8f0" in rule


def test_the_diagnosis_heading_names_its_real_source_984():
    """Since #984 the chips are the EPS problems section; the heading said
    'ICD-11-register' and no script replaced it (found writing docs/dashboard.md)."""
    page = (BASE.parent / "dashboard.html").read_text()
    assert "ICD-11-register" not in page
    assert 'data-role="dx-source">från patientsammanfattningen (EPS)<' in page
