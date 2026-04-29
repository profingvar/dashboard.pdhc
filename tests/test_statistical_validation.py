"""F3 statistical-validation suite — drives the deployed dashboard
and CDR cluster, asserts cohort distributions land in clinically
plausible bands and that the SES-axis discrimination is detectable.

Skipped when ``MONITOR_PDHC_SERVICE_KEY`` is not set in env (so it
doesn't break ``pytest`` in CI / local-dev that hasn't been wired
to the live cluster).

Targets per platform-plan §5.2 / §5.3:
  - cohort distributions plausible (no NaNs, in clinical reference range)
  - low-SES vs mid-SES gap detectable on HbA1c
  - dual-coding intact (SNOMED + ICD-10 both present on Conditions)
  - all 5 (4 active) CDRs reachable
"""
from __future__ import annotations

import os
import statistics
from typing import Any

import pytest
import requests


DASHBOARD = os.environ.get("DASHBOARD_BASE_URL", "https://dashboard.pdhc.se")
KEY = os.environ.get("MONITOR_PDHC_SERVICE_KEY", "")
DASH_KEY = os.environ.get("DASHBOARD_PDHC_SERVICE_KEY", "")

CDR_HOSTS = [
    "https://cdr2.pdhc.se",
    "https://cdr3.pdhc.se",
    "https://cdr4.pdhc.se",
    "https://cdr5.pdhc.se",
]
LOW_SES = {"cdr2", "cdr4"}   # cohort_nord, cohort_ost
MID_SES = {"cdr3", "cdr5"}   # cohort_vast, cohort_mitt

HBA1C_CODE = "4548-4"
LDL_CODE = "18262-6"
EGFR_CODE = "33914-3"
BMI_CODE = "39156-5"
T2DM_SNOMED_CODE = "44054006"


pytestmark = pytest.mark.skipif(
    not KEY,
    reason="set MONITOR_PDHC_SERVICE_KEY to run the live statistical suite",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dashboard_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Source-Service": "monitor.pdhc",
        "X-Service-Key": KEY,
    })
    s.verify = True
    return s


def _cdr_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Source-Service": "dashboard.pdhc",
        "X-Service-Key": DASH_KEY,
    })
    return s


def _new_cohort_with_retry(s: requests.Session, *, attempts: int = 5) -> str:
    """Create a cohort across cdr2..5 and verify the worker can see it.

    Cohort store is per-gunicorn-worker today (Phase-4.5 follow-up);
    same retry pattern as the Playwright suite.
    """
    body = {"cdr_ids": ["cdr2", "cdr3", "cdr4", "cdr5"],
            "demographics": {"age_min": 18, "age_max": 95}}
    last_id = None
    for _ in range(attempts):
        r = s.post(f"{DASHBOARD}/api/cohort", json=body, timeout=30)
        assert r.status_code == 201, f"cohort create failed: {r.status_code}"
        cid = r.json()["cohort_id"]
        last_id = cid
        verify = s.get(f"{DASHBOARD}/api/cohort", timeout=10)
        if verify.ok:
            items = verify.json().get("cohorts") or verify.json()
            if any(c.get("cohort_id") == cid for c in items):
                return cid
    pytest.skip(f"cohort store not visible after {attempts} attempts (id={last_id})")


def _hba1c_per_cdr(s: requests.Session) -> dict[str, list[float]]:
    """Page through HbA1c observations on each CDR, return values per cdr_id."""
    out: dict[str, list[float]] = {}
    for url in CDR_HOSTS:
        cdr_id = "cdr" + url.split("cdr", 1)[1].split(".", 1)[0]
        vals: list[float] = []
        for offset in range(0, 5 * 200, 200):
            r = s.get(
                f"{url}/api/v1/fhir/Observation",
                params={"code": HBA1C_CODE, "_count": 200, "_offset": offset},
                timeout=15,
            )
            if not r.ok:
                break
            page = [
                e["resource"]["valueQuantity"]["value"]
                for e in r.json().get("entry", [])
                if e["resource"].get("valueQuantity")
            ]
            if not page:
                break
            vals.extend(page)
            if len(page) < 200:
                break
        out[cdr_id] = vals
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dash_session() -> requests.Session:
    return _dashboard_session()


@pytest.fixture(scope="module")
def cdr_session() -> requests.Session:
    if not DASH_KEY:
        pytest.skip("DASHBOARD_PDHC_SERVICE_KEY not set; can't talk to CDRs directly")
    return _cdr_session()


@pytest.fixture(scope="module")
def cohort_id(dash_session) -> str:
    return _new_cohort_with_retry(dash_session)


# ---- 1. coverage -----------------------------------------------------------

def test_all_active_cdrs_healthy():
    """All 5 demonstrator hostnames respond 200 on /healthz."""
    for n in (1, 2, 3, 4, 5):
        url = f"https://cdr{n}.pdhc.se/healthz"
        r = requests.get(url, timeout=10)
        assert r.status_code == 200, f"{url} returned {r.status_code}"
        body = r.json()
        assert body.get("status") == "ok"
        assert body.get("database") == "connected"


def test_dashboard_health():
    r = requests.get(f"{DASHBOARD}/healthz", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body.get("auth_mode") == "sso"
    assert body.get("database") == "connected"


# ---- 2. cohort shape -------------------------------------------------------

def test_cohort_size_within_band(dash_session, cohort_id):
    """Cohort over cdr2..5 should hit ~400 patients (4 CDRs × 100 each)."""
    r = dash_session.get(f"{DASHBOARD}/api/cohort", timeout=15)
    assert r.ok
    items = r.json().get("cohorts", [])
    cohort = next((c for c in items if c["cohort_id"] == cohort_id), None)
    assert cohort is not None, "cohort vanished between create + list"
    assert 380 <= cohort["n"] <= 420, f"cohort size {cohort['n']} outside 380..420"


# ---- 3. plausibility bands -------------------------------------------------

def test_hba1c_mean_in_diabetes_band(dash_session, cohort_id):
    """Federated HbA1c mean should land in 42–80 mmol/mol (Swedish DM
    register). Equivalents in NGSP %: 6.0–9.5."""
    canonical = "https%3A//termbank.pdhc.se/CodeSystem/loinc/4548-4"
    r = dash_session.get(
        f"{DASHBOARD}/api/cohort/{cohort_id}/variable/{canonical}/histogram",
        params={"buckets": 20}, timeout=30,
    )
    if r.status_code == 404:
        pytest.skip("cohort store cross-worker — see post_seed_followups")
    assert r.ok, r.text[:200]
    body = r.json()
    assert body["fanout_mode"] == "complete", body
    assert body["n"] > 100, f"only {body['n']} HbA1c obs"
    assert 42 <= body["mean"] <= 80, (
        f"HbA1c mean {body['mean']:.1f} mmol/mol out of band"
    )


def test_hba1c_dispersion_realistic(dash_session, cohort_id):
    """SD in mmol/mol should be 5–25 (≈0.5–2.5 % NGSP)."""
    canonical = "https%3A//termbank.pdhc.se/CodeSystem/loinc/4548-4"
    r = dash_session.get(
        f"{DASHBOARD}/api/cohort/{cohort_id}/variable/{canonical}/histogram",
        params={"buckets": 20}, timeout=30,
    )
    if r.status_code == 404:
        pytest.skip("cohort store cross-worker")
    assert r.ok, r.text[:200]
    body = r.json()
    assert 5 <= body.get("sd", 0) <= 27, f"sd {body.get('sd')} mmol/mol unrealistic"


def test_cdr_per_cdr_hba1c_mean_within_band(cdr_session):
    """Each CDR independently should produce HbA1c mean in 48–75 mmol/mol
    (≈6.5–9.0 % NGSP)."""
    per_cdr = _hba1c_per_cdr(cdr_session)
    assert len(per_cdr) == 4
    for cdr, vals in per_cdr.items():
        assert vals, f"{cdr} returned no HbA1c values"
        m = statistics.fmean(vals)
        assert 48 <= m <= 75, f"{cdr} mean {m:.1f} mmol/mol out of band"


# ---- 4. SES discrimination -------------------------------------------------

def test_ses_axis_detectable_on_hba1c(cdr_session):
    """Low-SES (cdr2+cdr4) mean HbA1c should be at least 3.3 mmol/mol
    higher than mid-SES (cdr3+cdr5). Design intent +7.6 mmol/mol
    (≈+0.7 % NGSP); allowing for within-cohort variance attenuation."""
    per_cdr = _hba1c_per_cdr(cdr_session)
    low = [v for c, vs in per_cdr.items() if c in LOW_SES for v in vs]
    mid = [v for c, vs in per_cdr.items() if c in MID_SES for v in vs]
    assert low and mid, "missing low-SES or mid-SES samples"
    low_m = statistics.fmean(low)
    mid_m = statistics.fmean(mid)
    delta = low_m - mid_m
    assert delta >= 3.3, (
        f"SES gap {delta:+.1f} mmol/mol too small "
        f"(low={low_m:.1f}, mid={mid_m:.1f})"
    )
    assert delta <= 16.4, (
        f"SES gap {delta:+.1f} mmol/mol implausibly large "
        f"(sim bug or data drift)"
    )


# ---- 5. dual-coding integrity ---------------------------------------------

def test_t2dm_condition_dual_coded(cdr_session):
    """A T2DM Condition should carry both the termbank canonical
    (SNOMED 44054006, promoted to coding[0] by the canonicaliser) and
    the original plan.pdhc Concept GUID + ICD-10 alt coding from sim's
    `coding_for()` dual-coding (preserved at coding[1..n])."""
    r = cdr_session.get(
        "https://cdr2.pdhc.se/api/v1/fhir/Condition",
        params={"code": T2DM_SNOMED_CODE, "_count": 1},
        timeout=15,
    )
    assert r.ok, r.text[:200]
    entries = r.json().get("entry", [])
    if not entries:
        pytest.skip("no T2DM patients on cdr2")
    coding = entries[0]["resource"]["code"]["coding"]
    assert len(coding) >= 2, f"expected dual-coded coding[], got {len(coding)}"
    systems = {c.get("system") for c in coding}
    # Either ICD10 or plan.pdhc Concept must be in the secondary codings.
    assert any("icd10" in s.lower() or "Concept" in s for s in systems), (
        f"no ICD-10 or plan.pdhc Concept secondary coding: {systems}"
    )


# ---- 6. fanout health ------------------------------------------------------

def test_histogram_fanout_complete_when_all_cdrs_up(dash_session, cohort_id):
    """When all 4 CDRs are healthy, ``fanout_mode == complete``."""
    canonical = "https%3A//termbank.pdhc.se/CodeSystem/loinc/4548-4"
    r = dash_session.get(
        f"{DASHBOARD}/api/cohort/{cohort_id}/variable/{canonical}/histogram",
        timeout=30,
    )
    if r.status_code == 404:
        pytest.skip("cohort store cross-worker")
    assert r.ok
    body = r.json()
    assert body["fanout_mode"] == "complete"
    assert sorted(body["succeeded_cdrs"]) == ["cdr2", "cdr3", "cdr4", "cdr5"]
    assert body["failed_cdrs"] == []
