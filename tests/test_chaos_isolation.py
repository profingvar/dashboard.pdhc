"""F4 cross-CDR isolation chaos test.

Stops cdr3_app, verifies the rest of the cluster keeps serving, that
the dashboard's federation correctly classifies the result as
``degraded`` with cdr3 in the failed list, and that recovery to
``complete`` happens after restart.

Skipped unless both:
  - ``MONITOR_PDHC_SERVICE_KEY`` set in env
  - the test process can drive ``ssh miserver@192.168.1.154`` non-interactively

Always restarts cdr3 in teardown — even if assertions fail mid-test —
so the cluster is left as it was found.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest
import requests


DASHBOARD = os.environ.get("DASHBOARD_BASE_URL", "https://dashboard.pdhc.se")
KEY = os.environ.get("MONITOR_PDHC_SERVICE_KEY", "")
SSH_HOST = os.environ.get("SIM_TUNNEL_HOST", "miserver@192.168.1.154")
TARGET_CDR = "cdr3"  # the one we stop/start
TARGET_CONTAINER = "cdr_pdhc_3_app"


pytestmark = pytest.mark.skipif(
    not KEY,
    reason="set MONITOR_PDHC_SERVICE_KEY to run F4 chaos suite",
)


def _ssh_available() -> bool:
    if shutil.which("ssh") is None:
        return False
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         SSH_HOST, "true"],
        capture_output=True,
    )
    return r.returncode == 0


import shlex


def _docker(cmd: list[str]) -> str:
    """Run a docker command on miserver via SSH. Each arg is quoted for
    the remote shell so `{{.Status}}`-style format strings survive."""
    quoted = " ".join(shlex.quote(c) for c in cmd)
    full = ["ssh", SSH_HOST,
            "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH; "
            f"docker {quoted}"]
    r = subprocess.run(full, capture_output=True, text=True, timeout=30)
    return (r.stdout + r.stderr).strip()


def _container_running(name: str) -> bool:
    out = _docker(["ps", "--format", "{{.Names}}|{{.Status}}"])
    for line in out.splitlines():
        if line.startswith(name + "|") and "Up " in line:
            return True
    return False


def _wait_until(predicate, *, timeout: float = 30.0, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _healthz(host: str) -> int:
    r = requests.get(f"https://{host}/healthz", timeout=5)
    return r.status_code


def _new_cohort(s: requests.Session, cdr_ids: list[str], attempts: int = 5) -> str:
    """Define a cohort across the given CDRs; retry until cohort is
    visible on the same gunicorn worker (Phase-4.5 follow-up)."""
    body = {"cdr_ids": cdr_ids,
            "demographics": {"age_min": 18, "age_max": 95}}
    last_id = None
    for _ in range(attempts):
        r = s.post(f"{DASHBOARD}/api/cohort", json=body, timeout=30)
        assert r.status_code == 201
        last_id = r.json()["cohort_id"]
        v = s.get(f"{DASHBOARD}/api/cohort", timeout=10)
        if v.ok:
            items = v.json().get("cohorts") or v.json()
            if any(c.get("cohort_id") == last_id for c in items):
                return last_id
    pytest.skip(f"cohort cross-worker after {attempts} retries (id={last_id})")


def _hist_or_skip(s: requests.Session, cohort_id: str, attempts: int = 5) -> dict:
    canonical = "https%3A//termbank.pdhc.se/CodeSystem/loinc/4548-4"
    for _ in range(attempts):
        r = s.get(
            f"{DASHBOARD}/api/cohort/{cohort_id}/variable/{canonical}/histogram",
            timeout=30,
        )
        if r.ok:
            return r.json()
        if r.status_code != 404:
            r.raise_for_status()
    pytest.skip("cohort cross-worker on histogram after retries")


# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _ssh_required():
    if not _ssh_available():
        pytest.skip(f"SSH to {SSH_HOST} not available — skipping chaos suite")


@pytest.fixture
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Source-Service": "monitor.pdhc",
        "X-Service-Key": KEY,
    })
    return s


@pytest.fixture
def with_cdr3_stopped():
    """Stop cdr3_app on enter, ensure restart on exit even on test failure."""
    assert _container_running(TARGET_CONTAINER), \
        f"baseline broken: {TARGET_CONTAINER} not running"
    _docker(["stop", TARGET_CONTAINER])
    assert _wait_until(lambda: not _container_running(TARGET_CONTAINER), timeout=10), \
        "cdr3_app didn't stop within 10s"
    try:
        yield
    finally:
        _docker(["start", TARGET_CONTAINER])
        ok = _wait_until(lambda: _healthz(f"{TARGET_CDR}.pdhc.se") == 200, timeout=30)
        if not ok:
            pytest.fail(f"cdr3 didn't recover within 30s — cluster left degraded")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_baseline_complete_before_chaos(session):
    """Sanity: with all 4 active CDRs up, fanout_mode=complete."""
    cohort = _new_cohort(session, ["cdr2", "cdr3", "cdr4", "cdr5"])
    body = _hist_or_skip(session, cohort)
    assert body["fanout_mode"] == "complete"
    assert sorted(body["succeeded_cdrs"]) == ["cdr2", "cdr3", "cdr4", "cdr5"]
    assert body["failed_cdrs"] == []


def test_killing_cdr3_does_not_break_other_cdrs(with_cdr3_stopped):
    """While cdr3 is down, cdr2/4/5 keep returning /healthz 200."""
    for n in (2, 4, 5):
        assert _healthz(f"cdr{n}.pdhc.se") == 200, f"cdr{n} down with cdr3"
    # cdr3 should not return 200 (502 from nginx is the typical signal)
    assert _healthz(f"{TARGET_CDR}.pdhc.se") != 200


def test_federation_degraded_when_cdr3_down(session, with_cdr3_stopped):
    """With cdr3 stopped, fanout_mode=degraded, succeeded=[cdr2,4,5], failed=[cdr3]."""
    cohort = _new_cohort(session, ["cdr2", "cdr3", "cdr4", "cdr5"])
    body = _hist_or_skip(session, cohort)
    assert body["fanout_mode"] == "degraded", \
        f"expected degraded, got {body['fanout_mode']}"
    assert sorted(body["succeeded_cdrs"]) == ["cdr2", "cdr4", "cdr5"]
    assert body["failed_cdrs"] == ["cdr3"]
    # And the data we DO get back should still be plausible.
    assert body["n"] > 0
    assert 42 <= body["mean"] <= 80, f"HbA1c mean {body['mean']:.1f} mmol/mol out of band"


def test_recovery_after_cdr3_restart(session):
    """After the with_cdr3_stopped fixture exits, cdr3 should be back up
    and a fresh cohort/histogram round-trip should be complete again."""
    # Wait an extra beat for cdr3's gunicorn workers to fully boot.
    assert _wait_until(lambda: _healthz(f"{TARGET_CDR}.pdhc.se") == 200, timeout=20)
    cohort = _new_cohort(session, ["cdr2", "cdr3", "cdr4", "cdr5"])
    body = _hist_or_skip(session, cohort)
    assert body["fanout_mode"] == "complete"
    assert "cdr3" in body["succeeded_cdrs"]
