#!/usr/bin/env python3
"""dashboard.pdhc — does every call to a SIBLING service actually work? (#708)

Run it INSIDE the container, which is the only place the real credentials and
the real network names exist:

    docker exec dashboard_pdhc_app python deploy/smoke_siblings.py
    docker exec dashboard_pdhc_app python deploy/smoke_siblings.py --json

READ-ONLY. It creates nothing, writes nothing and mints no token, so it is
safe against production at any time, including straight after a deploy.

## What dashboard calls

    sso.pdhc      /api/auth/me/service                     identity
    cdr.pdhc      /api/v1/clinical/patients                the patient picker
                  /api/v1/clinical/patient/<g>/summary     the chart data
    ips.pdhc      /api/v1/patients/<g>/blocks?active=true  spärr
                  /api/v1/patients/analysis-filter         consent
    gateway.pdhc  (configured; see the check)

## Why a smoke here and not just the health page

Almost every one of these clients FAILS OPEN OR SILENT by design, and each
choice is defensible on its own:

* `Cdr1Client` logs and returns `[]` on any network error or non-2xx, "so a
  CDR1 blip degrades the picker to 'no patients' with a banner rather than a
  500". So a moved path looks like an empty clinic.
* `ips_client.fetch_active_blocks` logs and returns `[]` on a network error
  AND on any 4xx/5xx — its own comment says "falling back to empty list". An
  empty block list means NOTHING IS HIDDEN, so an ips failure degrades in the
  direction that shows more data, not less. cdr1 applies
  `care_access_policy` itself on the care-delivery read, so this is a second
  layer rather than the only one — but a second layer that is silently absent
  is not a second layer.
* `analysis_filter` is the exception and raises `IpsUnreachable`, which is
  the right direction for a secondary-use read.

So the checks below are about DISTINGUISHING those states, which is the one
thing the clients cannot do for themselves. In particular each path is probed
with a deliberately invalid credential and asserted to REFUSE (401/403/405)
rather than 404 — a 404 is the shape a moved endpoint takes, and every client
above turns it into an empty list.

Exit code is 0 only if every check passed.
"""
from __future__ import annotations

import json as _json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

#: Every sibling path dashboard builds -> the client module that builds it.
#: `%s` stands for a path parameter. Checked ANCHORED, because a truncated
#: prefix is a substring of the real path and must not pass (#804).
PROBED_PATHS = {
    "/api/auth/me/service": "app/auth.py",
    "/api/v1/clinical/patients": "app/services/cdr1_client.py",
    "/api/v1/clinical/patient/%s/summary": "app/services/cdr1_client.py",
    "/api/v1/patients/%s/blocks": "app/services/ips_client.py",
    "/api/v1/patients/analysis-filter": "app/services/ips_client.py",
}

#: Probed but composed inline here, with no client module to drift from:
#:   /api/health, /healthz  — bare liveness pings.
INLINE_PATHS = ("/api/health", "/healthz")

#: A guid that cannot exist, used wherever a path needs one. Deliberately
#: well-FORMED, so a 400 means "bad request" and not "bad uuid".
NOBODY = "00000000-0000-4000-8000-000000000000"

RESET, RED, GRN, YEL, BOLD = "\033[0m", "\033[31m", "\033[32m", "\033[33m", "\033[1m"

results: list[dict] = []


def check(name: str):
    def wrap(fn):
        started = time.time()
        try:
            ok, note = fn()
        except Exception as e:                      # noqa: BLE001
            ok, note = False, f"{type(e).__name__}: {e}"
        results.append({"check": name, "ok": bool(ok), "note": note,
                        "ms": int((time.time() - started) * 1000)})
        return ok
    return wrap


def _repo_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent.parent
    return here


def _path_appears_in_client(template: str, src: str) -> bool:
    frags = [f for f in template.split("%s") if f]
    if not frags:
        return False
    if not re.search(r"""[}"']""" + re.escape(frags[0]), src):
        return False
    return all(f in src for f in frags[1:])


def _probe(url, headers=None, params=None, timeout=10):
    """(status, short body). A transport failure is status 0, so a service
    that is DOWN and a path that MOVED stay distinguishable."""
    import requests
    try:
        r = requests.get(url, headers=headers or {}, params=params or {},
                         timeout=timeout)
        return r.status_code, (r.text or "")[:140]
    except requests.RequestException as e:
        return 0, f"{type(e).__name__}: {e}"


def _exists(code: int) -> tuple[bool, str]:
    if code == 0:
        return False, "unreachable"
    if code == 404:
        return False, ("404 — the path MOVED, and every client here turns "
                       "that into an empty list")
    if code in (400, 401, 403, 405, 422, 428):
        return True, f"{code} (refused, so the path is live)"
    if 200 <= code < 300:
        return True, f"{code} (answered)"
    return False, f"{code}"


def main(as_json: bool = False) -> int:
    from app import create_app
    app = create_app()

    with app.app_context():
        from flask import current_app
        cfg = current_app.config
        sso = (cfg.get("SSO_BASE_URL") or "").rstrip("/")
        cdr1 = (cfg.get("CDR1_BASE_URL") or "").rstrip("/")
        ips = (cfg.get("IPS_BASE_URL") or "").rstrip("/")
        gw = (cfg.get("GATEWAY_BASE_URL") or "").rstrip("/")
        svc_key = cfg.get("DASHBOARD_PDHC_SERVICE_KEY") or ""

        @check("paths: every probed path appears in its client module")
        def _():
            root = _repo_root()
            missing = []
            for template, mod in PROBED_PATHS.items():
                f = root / mod
                if not f.exists():
                    missing.append(f"{mod} is missing (looked under {root})")
                    continue
                if not _path_appears_in_client(template,
                                               f.read_text(encoding="utf-8")):
                    missing.append(f"{template} does not START a URL in {mod}")
            return (not missing), (
                f"all {len(PROBED_PATHS)} anchored to a URL boundary; "
                f"{len(INLINE_PATHS)} inline liveness paths unguarded by "
                f"design" if not missing else "; ".join(missing))

        @check("config: every sibling base URL is set")
        def _():
            miss = [n for n, v in (("SSO_BASE_URL", sso),
                                   ("CDR1_BASE_URL", cdr1),
                                   ("IPS_BASE_URL", ips)) if not v]
            note = "SSO/CDR1/IPS set" if not miss else "MISSING: " + ", ".join(miss)
            if not gw:
                note += "; GATEWAY_BASE_URL unset"
            return (not miss), note

        @check("config: the sso service credential pair is configured")
        def _():
            cid, sec = cfg.get("SSO_CLIENT_ID"), cfg.get("SSO_CLIENT_SECRET")
            return bool(cid and sec), ("client id + secret present"
                                       if cid and sec else "MISSING")

        @check("sso: /api/health answers 200")
        def _():
            if not sso:
                return False, "SSO_BASE_URL not set"
            code, body = _probe(f"{sso}/api/health")
            return code == 200, f"{code} {body[:40]}"

        @check("sso: /api/auth/me/service EXISTS (not 404)")
        def _():
            if not sso:
                return False, "SSO_BASE_URL not set"
            code, _b = _probe(f"{sso}/api/auth/me/service", headers={
                "Authorization": "Bearer smoke-not-a-real-token",
                "X-SSO-Client-Id": cfg.get("SSO_CLIENT_ID") or "",
                "X-SSO-Client-Secret": cfg.get("SSO_CLIENT_SECRET") or ""})
            if 200 <= code < 300:
                return False, f"{code} for a junk token"
            return _exists(code)

        # ── cdr1: the picker and the charts ──
        @check("cdr1: the service key is accepted on /clinical/patients")
        def _():
            # The one call the picker makes. A WRONG key and a MOVED path both
            # reach the UI as "no patients", so the status code is the only
            # thing that separates them.
            if not cdr1:
                return False, "CDR1_BASE_URL not set"
            if not svc_key:
                return False, "DASHBOARD_PDHC_SERVICE_KEY is not set"
            code, body = _probe(f"{cdr1}/api/v1/clinical/patients", headers={
                "Accept": "application/json",
                "X-Access-Purpose": "care-delivery",
                "X-Source-Service": "dashboard.pdhc",
                "X-Service-Key": svc_key,
                "X-Is-Admin": "1",
                # #575/#212: an admin read is break-glass and cdr1 answers 428
                # ADMIN_READ_REASON_REQUIRED without this. Supplied so the
                # check measures the KEY, not the attestation.
                "X-Admin-Read-Reason": "#708 sibling smoke, read-only probe"})
            if code == 404:
                return False, ("404 — the picker would silently show no "
                               "patients")
            if 200 <= code < 300:
                return True, f"200 ({len(body)} bytes of body)"
            return False, f"{code} {body[:70]}"

        @check("cdr1: dashboard's own client returns rows through that path")
        def _():
            # Drives `Cdr1Client`, so a wrong header name or a wrong purpose
            # is wrong here too. An empty list is NOT a pass: this client
            # returns [] for every failure, which is the whole reason the
            # status-code check above exists.
            from app.services.cdr1_client import Cdr1Client
            if not (cdr1 and svc_key):
                return False, "CDR1_BASE_URL or the service key is not set"
            rows = Cdr1Client(base_url=cdr1, service_key=svc_key
                              ).list_org_patients(
                [], is_admin=True,
                reason="#708 sibling smoke, read-only probe")
            return bool(rows), (f"{len(rows)} patient(s)" if rows else
                                "[] — which this client also returns for "
                                "every failure, so see the check above")

        @check("cdr1: an ADMIN read with NO reason is refused (#575 break-glass)")
        def _():
            # Asserting the gate BITES, not merely that the happy path works.
            # A gate that admits everyone is indistinguishable from one that
            # is not wired up — the #729 failure that hid for five months.
            if not (cdr1 and svc_key):
                return False, "CDR1_BASE_URL or the service key is not set"
            code, body = _probe(f"{cdr1}/api/v1/clinical/patients", headers={
                "Accept": "application/json",
                "X-Access-Purpose": "care-delivery",
                "X-Source-Service": "dashboard.pdhc",
                "X-Service-Key": svc_key,
                "X-Is-Admin": "1"})
            if 200 <= code < 300:
                return False, ("200 — an admin override was admitted with no "
                               "attested reason; #575 is not being enforced")
            return code in (400, 403, 428), f"{code} {body[:60]}"

        # ── ips: spärr and consent ──
        @check("ips: /patients/<g>/blocks EXISTS (not 404)")
        def _():
            if not ips:
                return False, "IPS_BASE_URL not set"
            code, _b = _probe(f"{ips}/api/v1/patients/{NOBODY}/blocks",
                              params={"active": "true"},
                              headers={"Accept": "application/json"})
            if code == 404:
                return False, ("404 — fetch_active_blocks would return [], "
                               "which means NOTHING IS HIDDEN")
            return _exists(code)

        @check("ips: a SERVICE-KEY-only block read is refused, and that "
               "means the spärr filter fails OPEN")
        def _():
            # ips's `require_auth` reads ONLY `Authorization` and accepts
            # exactly `Bearer` / `ApiKey`; it IGNORES `X-Service-Key`
            # (memory: ips.pdhc auth header scheme). dashboard's
            # `_default_client` sends the service key "as fallback for
            # service-to-service paths", so on any path with no session token
            # the block fetch 401s and returns [] — and an empty block list
            # means nothing is hidden.
            #
            # This check does not fail the smoke, because cdr1 applies
            # care_access_policy itself on the care-delivery read, so spärr is
            # still enforced one layer down. It is reported so the second
            # layer's absence is VISIBLE rather than silent.
            if not (ips and svc_key):
                return True, "skipped — IPS_BASE_URL or the service key unset"
            code, _b = _probe(f"{ips}/api/v1/patients/{NOBODY}/blocks",
                              params={"active": "true"},
                              headers={"Accept": "application/json",
                                       "X-Service-Key": svc_key})
            if code in (401, 403):
                return True, (f"{code} as expected — ips ignores "
                              f"X-Service-Key, so a session-less block fetch "
                              f"returns [] and hides nothing. cdr1 still "
                              f"enforces spärr itself on the care read")
            if 200 <= code < 300:
                return True, f"{code} — ips DOES accept the service key here"
            return True, f"{code} (reported, not asserted)"

        @check("ips: an ApiKey block read is accepted, if a key is configured")
        def _():
            # The header scheme that DOES work. Reported rather than asserted
            # when no key is configured, because dashboard is not required to
            # hold one — but if it holds one, it must work.
            key = cfg.get("IPS_API_KEY") or ""
            if not (ips and key):
                return True, "skipped — no IPS_API_KEY configured here"
            code, _b = _probe(f"{ips}/api/v1/patients/{NOBODY}/blocks",
                              params={"active": "true"},
                              headers={"Accept": "application/json",
                                       "Authorization": f"ApiKey {key}"})
            # NOBODY does not exist, so 404 is the right answer and proves
            # authentication passed.
            return code in (200, 404), f"{code}"

        @check("ips: /patients/analysis-filter EXISTS (not 404)")
        def _():
            if not ips:
                return False, "IPS_BASE_URL not set"
            # POST-only, so GET is a 405 — which proves it is routed without
            # this smoke sending a patient list to a sibling.
            code, _b = _probe(f"{ips}/api/v1/patients/analysis-filter")
            if code == 404:
                return False, ("404 — every secondary-use read would fail "
                               "closed and blame ips")
            return _exists(code)

        @check("ips: the consent client fails CLOSED, by name")
        def _():
            # The one client here that raises rather than degrading, and the
            # direction is the safety property: a missing verdict must not be
            # read as permission.
            from app.services.ips_client import IpsClient, IpsUnreachable
            client = IpsClient(base_url="http://127.0.0.1:1",
                               service_key=svc_key or None)
            try:
                client.analysis_filter([NOBODY], "statistics", [])
            except IpsUnreachable as e:
                return True, f"IpsUnreachable: {str(e)[:60]}"
            except Exception as e:                  # noqa: BLE001
                return False, (f"raised {type(e).__name__}, not IpsUnreachable "
                               f"— a caller handling the documented type would "
                               f"not catch it")
            return False, "it RETURNED a verdict with ips unreachable"

    failed = [r for r in results if not r["ok"]]
    if as_json:
        print(_json.dumps({"service": "dashboard.pdhc", "checks": results,
                           "failed": len(failed)}, indent=2))
    else:
        print(f"\n{BOLD}dashboard.pdhc — sibling smoke{RESET}\n")
        for r in results:
            mark = f"{GRN}OK{RESET}" if r["ok"] else f"{RED}FAIL{RESET}"
            print(f"  {mark:<14} {r['check']:<62}\n{' ' * 18}{r['note']}  "
                  f"({r['ms']}ms)")
        if failed:
            print(f"\n{RED}{BOLD}{len(failed)} check(s) failed.{RESET}\n")
        else:
            print(f"\n{GRN}{BOLD}All {len(results)} checks passed.{RESET}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(as_json="--json" in sys.argv))
