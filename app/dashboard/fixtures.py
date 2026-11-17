"""Synthetic fixture layer for the patient dashboard (#974, Phase 6 step 22).

WHY THIS EXISTS
===============
Every later check of the dashboard — the pytest panel tests, the Playwright
e2e run, the resilience test and a developer's local run — needs patients with
realistic timelines, and none of them may touch production data or any live
service. This module is that dataset plus five in-memory upstreams.

WHY THE FAKES SIT AT THE TRANSPORT, NOT AT THE METHOD
=====================================================
Each fake SUBCLASSES the real client and replaces only the one method that
opens a socket (`Cdr1Client._get_json_strict` / `_get_json`,
`RequestClient._get`, `PlanClient._get_json`, `Icd11Client._get`; ips has no
transport method, so its two reads are replaced). Everything above that line
is the real code: cdr1's #983 projection check, `plan_summaries`' assembly of
CarePlans + ServiceRequests + matches, the concept-index cache, the problem
list parser. So the fixtures exercise the same code production runs, and a
"source down" is the SAME exception the real client raises on a network error
— `Cdr1Unreachable`, `RequestUnreachable`, `PlanUnreachable`,
`Icd11Unreachable`, `IpsUnreachable` — because it is raised from the same
place, not chosen by hand. ips's block read fails OPEN to `[]`, exactly as the
real `fetch_active_blocks` does.

THE DATA IS SYNTHETIC AND SAYS SO
=================================
* Every guid starts `fa4e0974-` ("fake", #974). They are valid GUIDs, because
  `ips_client.concept_guid_from_canonical` requires one.
* Names are "Fixtur ..." and every personnummer carries an INVALID Luhn check
  digit (#405), so none can be mistaken for, or collide with, a real person.
  `app/tests/test_dashboard_fixtures_974.py` asserts that.
* Dates are relative to an ANCHOR (default: today 00:00 UTC; override with
  `DASHBOARD_FIXTURES_ANCHOR`), so the data always looks live and a run on a
  given anchor is byte-for-byte deterministic. Values come from a seeded RNG.
  Planned/overdue is judged by request_client against the REAL clock, so with
  a fixed anchor the "planned" order (anchor + 21 d) turns overdue three weeks
  later; the default anchor never does.

DELIBERATELY UNTIDY
===================
Most observations carry NO `transaction_guid` and NO `care_plan_guid` (the
keys are present and null, as on cdr1 since #983). On 2026-11-12 0 of 1 949
cdr1 rows had a care plan; data shaped otherwise would hide the "not
recorded" path the detail panel exists to show. Do not tidy it.

THE SWITCH
==========
`install(app)` runs from `create_app` ONLY when `DASHBOARD_FIXTURES` is truthy,
and REFUSES (raises `FixturesRefused`) unless `AUTH_MODE=off`: fixture data
must never be servable behind a real login. With it installed, every upstream
base URL is overwritten with `fixture://<source>` — a scheme `requests`
refuses before any DNS lookup — so a path this module does not cover fails
instead of reaching a live service.

`DASHBOARD_FIXTURES_FAIL=cdr1,ips,...` (or `set_failing(app, ...)` /
`with failing(app, ...)` in a test) takes sources down.
"""
from __future__ import annotations

import contextlib
import math
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from flask import current_app

from app.services import cdr1_client, icd11_client, ips_client, plan_client, request_client

EXTENSION_KEY = "dashboard_fixtures"
SOURCES = ("cdr1", "ips", "request", "plan", "icd11")


class FixturesRefused(RuntimeError):
    """DASHBOARD_FIXTURES was asked for where it must never run."""


def _truthy(v: Any) -> bool:
    return v is True or str(v or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# identities
# ---------------------------------------------------------------------------

def _g(kind: int, n: int) -> str:
    """A clearly synthetic, syntactically valid GUID: `fa4e0974-<kind>-...`."""
    return f"fa4e0974-{kind:04x}-4000-8000-{n:012x}"


K_PATIENT, K_SR, K_CONCEPT, K_TX, K_CAREPLAN, K_ORG = 0xA, 0xB, 0xC, 0xD, 0xE, 0xF
K_OBS, K_INGEST, K_PD, K_ACT, K_BLOCK, K_CONTRACT = 0x1, 0x2, 0x3, 0x4, 0x5, 0x6

PATIENT_T2D = _g(K_PATIENT, 1)
PATIENT_HF = _g(K_PATIENT, 2)
PATIENT_CGM = _g(K_PATIENT, 3)
PATIENTS = (PATIENT_T2D, PATIENT_HF, PATIENT_CGM)

ORG_VC = _g(K_ORG, 1)          # Fixturvårdcentralen
ORG_HEART = _g(K_ORG, 2)       # Fixturkliniken, hjärtmottagningen
ORG_BLOCKED = _g(K_ORG, 3)     # a clinic the HF patient has blocked (spärr)

#: concept key -> (guid, name, unit)
CONCEPTS = {
    "hba1c": (_g(K_CONCEPT, 1), "HbA1c", "mmol/mol"),
    "weight": (_g(K_CONCEPT, 2), "Kroppsvikt", "kg"),
    "sys": (_g(K_CONCEPT, 3), "Systoliskt blodtryck", "mmHg"),
    "dia": (_g(K_CONCEPT, 4), "Diastoliskt blodtryck", "mmHg"),
    "eqvas": (_g(K_CONCEPT, 5), "Självskattad hälsa (EQ VAS)", "{score}"),
    "wellbeing": (_g(K_CONCEPT, 6), "Hur mår du idag? (fritext)", None),
    "ntprobnp": (_g(K_CONCEPT, 7), "NT-proBNP", "ng/L"),
    "glucose": (_g(K_CONCEPT, 8), "Glukos (CGM)", "mmol/L"),
    "waist": (_g(K_CONCEPT, 9), "Midjeomfång", "cm"),
    "lvef": (_g(K_CONCEPT, 10), "Ejektionsfraktion (LVEF)", "%"),
}


def code_of(concept_key: str) -> str:
    """cdr1's `code_canonical` for a fixture concept."""
    return f"urn:pdhc:concept/{CONCEPTS[concept_key][0]}"


TX = {k: _g(K_TX, i) for i, k in enumerate(
    ("hba1c", "weight", "sys", "dia", "eqvas", "waist",
     "hf_weight", "ntprobnp", "glucose", "lvef"), start=1)}

CP_T2D, CP_HF, CP_CGM = _g(K_CAREPLAN, 1), _g(K_CAREPLAN, 2), _g(K_CAREPLAN, 3)
PD_T2D, PD_HF, PD_CGM, PD_ECHO, PD_OLD = (_g(K_PD, i) for i in range(1, 6))

#: ICD-11 MMS labels — the ONE source of display text: icd11's fake serves
#: them, and the EPS problems section is built from the same table (since
#: #984 the card reads the EPS, which in production is derived from the CDR
#: icd11 writes to). `XT8W` (chronic) is an illustrative extension code.
ICD11_RELEASE = "2024-01"
ICD11_LABELS = {
    "5A11": "Diabetes mellitus typ 2",
    "BA00": "Essentiell hypertoni",
    "BD10": "Kongestiv hjärtsvikt",
    "5A11&XT8W": "Diabetes mellitus typ 2 & Kronisk",
}


def luhn_valid(digits: str) -> bool:
    """Swedish personnummer check: Luhn over the 10-digit YYMMDDNNNC."""
    ds = [int(c) for c in digits if c.isdigit()]
    ds = ds[-10:]
    if len(ds) != 10:
        return False
    total = 0
    for i, d in enumerate(ds[:9]):
        p = d * (2 if i % 2 == 0 else 1)
        total += p - 9 if p > 9 else p
    return (10 - total % 10) % 10 == ds[9]


def _invalid_pnr(stem9: str) -> str:
    """YYMMDD-NNN + a check digit that is deliberately WRONG (#405)."""
    for c in range(10):
        if not luhn_valid(stem9 + str(c)):
            # Take the first wrong digit: the right one is excluded by test.
            return f"19{stem9[:6]}-{stem9[6:]}{c}"
    raise AssertionError("unreachable")


PATIENT_INFO = {
    PATIENT_T2D: {"name": "Fixtur Typtvå Testsson", "birth_date": "1958-03-14",
                  "sex": "female", "personnummer": _invalid_pnr("580314902"),
                  "org": ORG_VC, "exercises": "type 2 diabetes plan"},
    PATIENT_HF: {"name": "Fixtur Hjärta Provsson", "birth_date": "1947-09-02",
                 "sex": "male", "personnummer": _invalid_pnr("470902903"),
                 "org": ORG_HEART, "exercises": "heart-failure plan + spärr"},
    PATIENT_CGM: {"name": "Fixtur Sensor Exempelsson", "birth_date": "1989-12-24",
                  "sex": "female", "personnummer": _invalid_pnr("891224904"),
                  "org": ORG_VC, "exercises": "dense CGM series (LTTB)"},
}


# ---------------------------------------------------------------------------
# the dataset
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def default_anchor() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def parse_anchor(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    raw = str(raw or "").strip()
    if not raw:
        return default_anchor()
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _snapshot(pd_guid: str, title: str, activities: list[tuple[str, list[dict]]]) -> dict:
    """A frozen PlanDefinition snapshot, request.pdhc's shape."""
    return {"guid": pd_guid, "title": title, "name": title, "version": "1.0",
            "activities": [{"guid": act, "transactions": txs} for act, txs in activities]}


def _tx(tx_key: str, concept_key: str, range_min=None, range_max=None) -> dict:
    cg, name, unit = CONCEPTS[concept_key]
    return {"guid": TX[tx_key], "concept_guid": cg, "concept_name": name,
            "unit": unit, "range_min": range_min, "range_max": range_max,
            "requirement_type": "required"}


@dataclass
class Dataset:
    anchor: datetime
    observations: list[dict] = field(default_factory=list)
    care_plans: list[dict] = field(default_factory=list)
    service_requests: list[dict] = field(default_factory=list)
    matches: dict[str, list[dict]] = field(default_factory=dict)
    plan_definitions: dict[str, dict] = field(default_factory=dict)
    blocks: dict[str, list[dict]] = field(default_factory=dict)
    eps: dict[str, dict] = field(default_factory=dict)
    diagnoses: dict[str, list[dict]] = field(default_factory=dict)

    def patient_observations(self, patient_guid: str) -> list[dict]:
        return [o for o in self.observations if o["patient_guid"] == patient_guid]

    def observation(self, guid: str) -> dict | None:
        return self._by_guid.get(guid)

    def index(self) -> "Dataset":
        self._by_guid = {o["observation_guid"]: o for o in self.observations}
        return self


def build_dataset(anchor: datetime | None = None) -> Dataset:
    """The whole synthetic world, deterministic for a given anchor."""
    a = anchor or default_anchor()
    rng = random.Random(974)
    ds = Dataset(anchor=a)
    counter = [0]

    def obs(patient, concept_key, at, value=None, *, unit="__concept__",
            value_string=None, org=None, tx=None, care_plan=None, sr=None,
            pd=None, act=None, raw_extra=None):
        counter[0] += 1
        n = counter[0]
        cg, name, cunit = CONCEPTS[concept_key]
        u = cunit if unit == "__concept__" else unit
        org = org or PATIENT_INFO[patient]["org"]
        raw = {"resourceType": "Observation", "id": _g(K_OBS, n), "status": "final",
               "code": {"text": name, "coding": [{"system": "urn:pdhc:concept",
                                                   "code": cg, "display": name}]},
               "effectiveDateTime": _iso(at)}
        if value is not None:
            raw["valueQuantity"] = {"value": value, "unit": u}
        else:
            raw["valueString"] = value_string
        raw.update(raw_extra or {})
        ds.observations.append({
            "observation_guid": _g(K_OBS, n), "patient_guid": patient,
            "code": code_of(concept_key), "at": _iso(at),
            "received_at": _iso(at + timedelta(minutes=5)),
            "value": value, "unit": u if value is not None else None,
            "value_string": value_string, "org_guid": org,
            "transaction_guid": TX[tx] if tx else None,
            "care_plan_guid": care_plan,
            "ingest_raw_guid": _g(K_INGEST, n),
            "service_request_guid": sr, "plan_definition_guid": pd,
            "plan_activity_guid": act,
            "contract_guid": _g(K_CONTRACT, 1) if tx else None,
            "provider_org_guid": org if tx else None,
            "raw": raw,
        })

    lab_ref = lambda lo, hi: {"referenceRange": [{"low": {"value": lo}, "high": {"value": hi}}],  # noqa: E731
                              "method": {"text": "Fixturlabbet, venöst prov"}}

    # ---- patient 1: type 2 diabetes ------------------------------------
    p = PATIENT_T2D
    act_q, act_bp, act_prom = _g(K_ACT, 1), _g(K_ACT, 2), _g(K_ACT, 3)
    sr_done, sr_overdue, sr_planned = _g(K_SR, 1), _g(K_SR, 2), _g(K_SR, 3)
    # HbA1c quarterly for two years, drifting ABOVE the patient's goal (<= 52).
    hba1c = [49, 51, 50, 54, 57, 55, 59, 53]
    for i, v in enumerate(hba1c):
        at = a - timedelta(days=91 * (len(hba1c) - i)) + timedelta(hours=8)
        tagged = i >= 5      # the older ones predate the order: untagged
        obs(p, "hba1c", at, float(v), tx="hba1c" if tagged else None,
            care_plan=CP_T2D if i == len(hba1c) - 1 else None,
            sr=sr_done if tagged else None, pd=PD_T2D if tagged else None,
            act=act_q if tagged else None, raw_extra=lab_ref(27, 42))
    # Weight monthly; no goal and no range anywhere -> `no_limit`.
    for i in range(24):
        at = a - timedelta(days=30 * (24 - i)) + timedelta(hours=7, minutes=30)
        v = round(92.0 - i * 0.25 + rng.uniform(-0.6, 0.6), 1)
        obs(p, "weight", at, v, tx="weight" if i % 3 == 0 else None)
    # BP every two weeks; the goal 100–135 is exceeded now and then.
    for i in range(26):
        at = a - timedelta(days=14 * (26 - i)) + timedelta(hours=9)
        s = round(128 + 10 * math.sin(i / 3) + rng.uniform(-4, 4))
        d = round(80 + 5 * math.sin(i / 3) + rng.uniform(-3, 3))
        tag = "sys" if i % 4 == 0 else None
        obs(p, "sys", at, float(s), tx=tag, care_plan=CP_T2D if i == 25 else None)
        obs(p, "dia", at + timedelta(seconds=1), float(d), tx="dia" if tag else None)
    # Patient-reported form answers (via the app): never tagged.
    for i in range(12):
        at = a - timedelta(days=7 * (12 - i)) + timedelta(hours=19)
        obs(p, "eqvas", at, float(60 + rng.randint(-10, 20)))
        obs(p, "wellbeing", at + timedelta(seconds=30), None,
            value_string=rng.choice(["Mår bra", "Lite trött", "Ont i fötterna", "Bra vecka"]))
    # Waist from an OLD plan nobody ordered for this patient -> `unordered`.
    for i in range(3):
        obs(p, "waist", a - timedelta(days=200 - 60 * i), float(104 - i), tx="waist")

    t2d_snapshot = _snapshot(PD_T2D, "Typ 2-diabetes, årskontroll (fixtur)", [
        (act_q, [_tx("hba1c", "hba1c", 20, 48), _tx("weight", "weight")]),
        (act_bp, [_tx("sys", "sys", 90, 140), _tx("dia", "dia", 60, 90)]),
        (act_prom, [_tx("eqvas", "eqvas")]),
    ])
    ds.care_plans.append({
        "care_plan_guid": CP_T2D, "guid": CP_T2D, "patient_guid": p,
        "status": "active", "title": "Vårdplan typ 2-diabetes (fixtur)",
        "period_start": _iso(a - timedelta(days=400)), "period_end": None,
        "plan_definition_guid": PD_T2D, "plan_definition_snapshot": t2d_snapshot,
        "goals": [
            # ONE-SIDED action goal: HbA1c <= 52 (overrides the PD range).
            {"concept_guid": CONCEPTS["hba1c"][0], "target_type": "quantity",
             "target_operator": "<=", "target_quantity": 52, "target_unit": "mmol/mol",
             "description": "HbA1c högst 52", "limit_source": "requester"},
            # Two-sided action goal on systolic BP.
            {"concept_guid": CONCEPTS["sys"][0], "target_type": "range",
             "target_range_low": 100, "target_range_high": 135, "target_unit": "mmHg",
             "description": "Systoliskt 100–135", "limit_source": "plan_recommended"},
        ],
    })
    for sr, status, start, act in (
            (sr_done, "archived", a - timedelta(days=120), act_q),
            (sr_overdue, "active", a - timedelta(days=14), act_bp),
            (sr_planned, "active", a + timedelta(days=21), act_prom)):
        acts = [x for x in t2d_snapshot["activities"] if x["guid"] == act]
        ds.service_requests.append({
            "guid": sr, "patient_guid": p, "status": status,
            "period_start": _iso(start), "period_end": None,
            "created_at": _iso(start - timedelta(days=30)),
            "plan_definition_guid": PD_T2D,
            "plan_definition_snapshot": dict(t2d_snapshot, activities=acts)})
    ds.matches[sr_done] = [{"status": "completed",
                            "response_at": _iso(a - timedelta(days=100))}]

    # ---- patient 2: heart failure --------------------------------------
    p = PATIENT_HF
    act_w, act_lab, act_echo = _g(K_ACT, 4), _g(K_ACT, 5), _g(K_ACT, 6)
    sr_hf, sr_echo = _g(K_SR, 4), _g(K_SR, 5)
    for i in range(90):
        at = a - timedelta(days=90 - i) + timedelta(hours=6, minutes=45)
        v = round(81.0 + 2.5 * math.sin(i / 9) + (2.0 if 60 <= i <= 66 else 0)
                  + rng.uniform(-0.4, 0.4), 1)
        # Device-reported weight is tagged; manual entries (every 5th) are not.
        obs(p, "weight", at, v, tx="hf_weight" if i % 5 else None,
            raw_extra={"device": {"display": "Fixturvåg BT-1"}})
    for i, v in enumerate([310, 420, 380, 650, 520, 360]):
        at = a - timedelta(days=30 * (6 - i)) + timedelta(hours=10)
        # Two results come from a clinic the patient has blocked (spärr).
        org = ORG_BLOCKED if i in (1, 3) else None
        obs(p, "ntprobnp", at, float(v), org=org, tx="ntprobnp" if i % 2 == 0 else None,
            raw_extra=lab_ref(0, 300))
    hf_snapshot = _snapshot(PD_HF, "Hjärtsvikt, egenmonitorering (fixtur)", [
        (act_w, [_tx("hf_weight", "weight")]),
        (act_lab, [_tx("ntprobnp", "ntprobnp", None, 400)]),   # one-sided RANGE
    ])
    ds.care_plans.append({
        "care_plan_guid": CP_HF, "guid": CP_HF, "patient_guid": p,
        "status": "active", "title": "Vårdplan hjärtsvikt (fixtur)",
        "period_start": _iso(a - timedelta(days=95)), "period_end": None,
        "plan_definition_guid": PD_HF, "plan_definition_snapshot": hf_snapshot,
        "goals": [{"concept_guid": CONCEPTS["weight"][0], "target_type": "quantity",
                   "target_operator": "<=", "target_quantity": 84, "target_unit": "kg",
                   "description": "Kontakta mottagningen över 84 kg",
                   "limit_source": "requester"}],
    })
    ds.service_requests.append({
        "guid": sr_hf, "patient_guid": p, "status": "active", "period_start": None,
        "period_end": None, "created_at": _iso(a - timedelta(days=95)),
        "plan_definition_guid": PD_HF, "plan_definition_snapshot": hf_snapshot})
    # An order no CarePlan claims (production's usual shape), withdrawn.
    ds.service_requests.append({
        "guid": sr_echo, "patient_guid": p, "status": "revoked",
        "period_start": _iso(a - timedelta(days=40)), "period_end": None,
        "created_at": _iso(a - timedelta(days=60)), "plan_definition_guid": PD_ECHO,
        "plan_definition_snapshot": _snapshot(PD_ECHO, "Ekokardiografi (fixtur)",
                                              [(act_echo, [_tx("lvef", "lvef")])])})
    ds.blocks[p] = [{"guid": _g(K_BLOCK, 1), "patient_guid": p,
                     "source_scope_type": "clinic", "source_scope_id": ORG_BLOCKED,
                     "is_active": True, "lift_kind": None, "lift_concept_guids": None,
                     "lift_from_date": None, "lift_until_date": None}]

    # ---- patient 3: CGM -------------------------------------------------
    p = PATIENT_CGM
    act_cgm, sr_cgm = _g(K_ACT, 7), _g(K_SR, 6)
    start = a - timedelta(days=14)
    for i in range(14 * 288):                       # 5-minute CGM, 4 032 points
        at = start + timedelta(minutes=5 * i)
        hour = (at.hour + at.minute / 60.0)
        meal = sum(3.2 * math.exp(-((hour - m) ** 2) / 1.2) for m in (8, 12.5, 18.5))
        v = 6.2 + 0.8 * math.sin(i / 288 * 2 * math.pi) + meal + rng.uniform(-0.3, 0.3)
        if i % 997 == 500:
            v = 3.1                                 # a few hypos: below 3.9
        obs(p, "glucose", at, round(v, 1), tx="glucose",
            raw_extra={"device": {"display": "Fixtur-CGM sensor G0"}})
    cgm_snapshot = _snapshot(PD_CGM, "Kontinuerlig glukosmätning (fixtur)",
                             [(act_cgm, [_tx("glucose", "glucose")])])
    ds.care_plans.append({
        "care_plan_guid": CP_CGM, "guid": CP_CGM, "patient_guid": p,
        "status": "active", "title": "Vårdplan CGM (fixtur)",
        "period_start": _iso(start), "period_end": None,
        "plan_definition_guid": PD_CGM, "plan_definition_snapshot": cgm_snapshot,
        "goals": [{"concept_guid": CONCEPTS["glucose"][0], "target_type": "range",
                   "target_range_low": 3.9, "target_range_high": 10.0,
                   "target_unit": "mmol/L", "description": "Målområde 3,9–10,0",
                   "limit_source": "requester"}],
    })
    ds.service_requests.append({
        "guid": sr_cgm, "patient_guid": p, "status": "archived",
        "period_start": _iso(start), "period_end": _iso(a),
        "created_at": _iso(start - timedelta(days=3)), "plan_definition_guid": PD_CGM,
        "plan_definition_snapshot": cgm_snapshot})
    ds.matches[sr_cgm] = [{"status": "completed", "response_at": _iso(a - timedelta(hours=2))}]

    for pd_guid, title in ((PD_T2D, "Typ 2-diabetes, årskontroll (fixtur)"),
                           (PD_HF, "Hjärtsvikt, egenmonitorering (fixtur)"),
                           (PD_CGM, "Kontinuerlig glukosmätning (fixtur)"),
                           (PD_ECHO, "Ekokardiografi (fixtur)"),
                           (PD_OLD, "Viktprogram, avslutat (fixtur)")):
        ds.plan_definitions[pd_guid] = {"resourceType": "PlanDefinition", "id": pd_guid,
                                        "name": title, "title": title,
                                        "version": "1.0", "status": "active"}

    # ---- diagnoses: icd11 problem list, and the EPS derived from it ------
    ds.diagnoses = {
        PATIENT_T2D: [{"code": "5A11", "status": "active", "onset": "2015-04-01", "cdr_backed": True},
                      {"code": "BA00", "status": "active", "onset": "2012-10-15", "cdr_backed": True}],
        PATIENT_HF: [{"code": "BD10", "status": "active", "onset": "2023-02-20", "cdr_backed": True},
                     {"code": "BA00", "status": "resolved", "onset": "2009-06-01", "cdr_backed": True},
                     # Held by the EPS alone, not yet in any CDR.
                     {"code": "5A11", "status": "active", "onset": None, "cdr_backed": False}],
        PATIENT_CGM: [{"code": "5A11&XT8W", "status": "active", "onset": "2019-11-11",
                       "cdr_backed": True}],
    }
    for pg in PATIENTS:
        ds.eps[pg] = _eps_summary(pg, ds.diagnoses[pg])
    return ds.index()


_MMS = icd11_client.MMS_SYSTEM


def _eps_summary(patient_guid: str, diagnoses: list[dict]) -> dict:
    info = PATIENT_INFO[patient_guid]
    problems = [{"display": ICD11_LABELS[d["code"]], "code": d["code"], "system": _MMS,
                 "date": d["onset"], "onset": d["onset"], "status": d["status"],
                 "source": "ips", "cdr_backed": d["cdr_backed"],
                 "cdr": {"name": "cdr2", "guid": _g(K_ORG, 99)} if d["cdr_backed"] else None}
                for d in diagnoses]
    meds = {PATIENT_T2D: [("A10BA02", "Metformin 500 mg")],
            PATIENT_CGM: [("A10AB05", "Insulin aspart"), ("A10AE04", "Insulin glargin")]}
    med_entries = [{"display": d, "code": c, "system": "http://www.whocc.no/atc",
                    "date": None, "status": "active", "source": "ips"}
                   for c, d in meds.get(patient_guid, [])]
    return {
        "source": "ips",
        # Identity as ips holds it. The personnummer is INVALID by design (#405).
        "patient": {"guid": patient_guid, "name": info["name"],
                    "birth_date": info["birth_date"], "personnummer": info["personnummer"]},
        "sections": [
            {"key": "allergies", "title": "Allergier", "loinc": "48765-2",
             "status": "EXPLICITLY_ABSENT", "entries": [],
             "absent": {"code": "no-known-allergies", "narrative_text": "Inga kända allergier"}},
            {"key": "problems", "title": "Problem", "loinc": "11450-4",
             "status": "PRESENT", "absent": None, "entries": problems},
            {"key": "medications", "title": "Läkemedel", "loinc": "10160-0",
             "status": "PRESENT" if med_entries else "MISSING",
             "absent": None, "entries": med_entries},
        ],
    }


# ---------------------------------------------------------------------------
# state, and the failure knob
# ---------------------------------------------------------------------------

@dataclass
class FixtureState:
    dataset: Dataset
    failing: set = field(default_factory=set)

    def is_down(self, source: str) -> bool:
        return source in self.failing


def _parse_sources(raw: Any) -> set[str]:
    if isinstance(raw, str):
        items = [s.strip().lower() for s in raw.split(",")]
    else:
        items = [str(s).strip().lower() for s in (raw or ())]
    out = {s for s in items if s}
    unknown = out - set(SOURCES)
    if unknown:
        # A typo must not silently test nothing.
        raise ValueError(f"unknown fixture source(s) {sorted(unknown)}; "
                         f"known: {', '.join(SOURCES)}")
    return out


def _reset_caches() -> None:
    """Process-wide caches would otherwise answer for a source that is down
    (the concept index for 300 s, spärr blocks for 30 s) — and would leak
    fixture names into the next test's app."""
    plan_client.invalidate_concepts()
    ips_client.invalidate()


def state_of(app) -> FixtureState | None:
    return app.extensions.get(EXTENSION_KEY)


def set_failing(app, *sources: str) -> None:
    """Take exactly these sources down (none = all up)."""
    st = state_of(app)
    if st is None:
        raise RuntimeError("fixtures are not installed on this app")
    st.failing = _parse_sources(sources)
    _reset_caches()


@contextlib.contextmanager
def failing(app, *sources: str):
    st = state_of(app)
    before = set(st.failing) if st else set()
    set_failing(app, *sources)
    try:
        yield
    finally:
        set_failing(app, *before)


def _active_state() -> FixtureState | None:
    try:
        return current_app.extensions.get(EXTENSION_KEY)
    except RuntimeError:          # no app context
        return None


# ---------------------------------------------------------------------------
# the fakes — real clients with the socket replaced
# ---------------------------------------------------------------------------

def _multi(params) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    items = params.items() if isinstance(params, dict) else (params or [])
    for k, v in items:
        out.setdefault(k, []).append(str(v))
    return out


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


_POINT_FIELDS = ("code", "at", "value", "unit", "value_string", "org_guid",
                 "observation_guid", "transaction_guid", "ingest_raw_guid",
                 "care_plan_guid")


class FakeCdr1Client(cdr1_client.Cdr1Client):
    def __init__(self, state: FixtureState):
        super().__init__(base_url="fixture://cdr1")
        self._st = state

    @property
    def _ds(self) -> Dataset:
        return self._st.dataset

    def _scoped(self, rows: Iterable[dict], org_guids, is_admin) -> list[dict]:
        if is_admin:
            return list(rows)
        allowed = set(org_guids or ())
        return [r for r in rows if r["org_guid"] in allowed
                or PATIENT_INFO[r["patient_guid"]]["org"] in allowed]

    def _answer(self, path: str, org_guids, is_admin, params) -> tuple[int, Any]:
        q = _multi(params)
        parts = path.strip("/").split("/")          # api v1 clinical ...
        if parts[3:] == ["patients"]:
            rows = []
            for pg in PATIENTS:
                if not is_admin and PATIENT_INFO[pg]["org"] not in set(org_guids or ()):
                    continue
                obs = self._ds.patient_observations(pg)
                rows.append({"patient_guid": pg, "name": PATIENT_INFO[pg]["name"],
                             "birth_date": PATIENT_INFO[pg]["birth_date"],
                             "observation_count": len(obs),
                             "last_observed_at": max(o["at"] for o in obs)})
            return 200, {"patients": rows}
        if parts[3] == "observation":
            o = self._ds.observation(parts[4])
            if o is None or not self._scoped([o], org_guids, is_admin):
                return 404, {"error": "not_found"}
            tagged = bool(o["transaction_guid"])
            ctx = {"plan_definition_guid": o["plan_definition_guid"],
                   "contract_guid": o["contract_guid"],
                   "provider_org_guid": o["provider_org_guid"],
                   "service_request_guid": o["service_request_guid"],
                   "care_plan_guid": o["care_plan_guid"],
                   "transaction_guid": o["transaction_guid"],
                   "plan_activity_guid": o["plan_activity_guid"],
                   "author_org_guid": None, "requesting_org_guid": None}
            return 200, {**{k: o[k] for k in _POINT_FIELDS},
                         "patient_guid": o["patient_guid"],
                         "received_at": o["received_at"],
                         "context": ctx, "context_found": tagged,
                         "context_filled_from_raw": [], "raw_fhir": o["raw"]}
        patient = parts[4]
        rows = self._scoped(self._ds.patient_observations(patient), org_guids, is_admin)
        if parts[5] == "summary":
            by: dict[str, dict] = {}
            for o in rows:
                s = by.setdefault(o["code"], {"code": o["code"], "unit": o["unit"], "count": 0,
                                              "first_observed_at": o["at"],
                                              "last_observed_at": o["at"]})
                s["count"] += 1
                s["first_observed_at"] = min(s["first_observed_at"], o["at"])
                s["last_observed_at"] = max(s["last_observed_at"], o["at"])
            return 200, {"parameters": sorted(by.values(), key=lambda s: -s["count"])}
        # series — the strict cursor read when `order` is present, else legacy
        codes = set(q.get("code", []))
        txs = set(q.get("transaction", []))
        frm = _dt(q["from"][0]) if q.get("from") else None
        to = _dt(q["to"][0]) if q.get("to") else None
        sel = [o for o in rows
               if (not codes or o["code"] in codes)
               and (not txs or o["transaction_guid"] in txs)
               and (frm is None or _dt(o["at"]) >= frm)
               and (to is None or _dt(o["at"]) <= to)]
        pts = lambda os_: [{k: o[k] for k in _POINT_FIELDS} for o in os_]  # noqa: E731
        if "order" not in q:
            return 200, {"points": pts(sorted(sel, key=lambda o: o["at"]))}
        sel.sort(key=lambda o: (o["at"], o["observation_guid"]), reverse=True)
        limit = int(q.get("limit", ["100"])[0])
        cursor = (q.get("cursor") or [None])[0]
        off = 0
        if cursor:
            if not cursor.startswith("fx1:") or not cursor[4:].isdigit():
                return 400, {"error": "invalid_cursor"}
            off = int(cursor[4:])
        page = sel[off:off + limit]
        nxt = f"fx1:{off + limit}" if off + limit < len(sel) else None
        body = {"points": pts(page), "next_cursor": nxt, "truncated": False,
                "limit": limit, "undated_count": 0}
        if txs:
            body["transaction_filter"] = True
        return 200, body

    def _get_json_strict(self, path, org_guids, is_admin, *, params=None,
                         reason=None, allow_not_found=False):
        if self._st.is_down("cdr1"):
            raise cdr1_client.Cdr1Unreachable("cdr1 network error: ConnectionError")
        status, body = self._answer(path, org_guids, is_admin, params)
        if status == 400:
            raise cdr1_client.Cdr1BadCursor(f"cdr1 refused the request: {body['error']}")
        if status == 404:
            if allow_not_found:
                return None
            raise cdr1_client.Cdr1Unreachable("cdr1 answered 404", status_code=404)
        return body

    def _get_json(self, path, org_guids, is_admin, *, what, params=None, reason=None):
        if self._st.is_down("cdr1"):
            current_app.logger.warning("CDR1 %s failed (network) [fixture]", what)
            return None
        status, body = self._answer(path, org_guids, is_admin, params)
        return body if status == 200 else None


class FakeIpsClient(ips_client.IpsClient):
    def __init__(self, state: FixtureState):
        super().__init__(base_url="fixture://ips")
        self._st = state

    def fetch_active_blocks(self, patient_guid, *, strict=False):
        if self._st.is_down("ips"):
            # As the real client: strict (the dashboard) raises; the older
            # views fail OPEN on a network error, by design.
            if strict:
                raise ips_client.IpsUnreachable("ips blocks: ConnectionError [fixture]")
            current_app.logger.warning("ips block fetch failed (network) [fixture]")
            return []
        return [ips_client.Block.from_dict(b)
                for b in self._st.dataset.blocks.get(patient_guid, [])]

    def euips_summary(self, patient_guid):
        if self._st.is_down("ips"):
            raise ips_client.IpsUnreachable("euips-summary network error: ConnectionError")
        return self._st.dataset.eps.get(patient_guid)       # None = ips's 404


class FakeRequestClient(request_client.RequestClient):
    def __init__(self, state: FixtureState):
        super().__init__(base_url="fixture://request")
        self._st = state

    def _get(self, path, *, what, params=None, item=False):
        if self._st.is_down("request"):
            raise request_client.RequestUnreachable(
                f"request.pdhc {what} failed (network): ConnectionError")
        ds = self._st.dataset
        p = path.split("?")[0].strip("/").split("/")[2:]     # after api/v1
        q = dict(params or {})
        if p == ["careplans"]:
            return {"careplans": [c for c in ds.care_plans
                                  if c["patient_guid"] == q.get("patient_guid")]}
        if p[0] == "careplans":
            cp = next((c for c in ds.care_plans if c["care_plan_guid"] == p[1]), None)
            if cp is None:
                return None
            if len(p) == 2:
                return cp
            return {"transactions": [
                {"activity_guid": act["guid"], "transaction_guid": t["guid"],
                 "concept_guid": t["concept_guid"], "concept_name": t["concept_name"],
                 "range_min": t["range_min"], "range_max": t["range_max"],
                 "requirement_type": t["requirement_type"]}
                for act in cp["plan_definition_snapshot"]["activities"]
                for t in act["transactions"]]}
        if p == ["ServiceRequest"]:
            return {"items": ds.service_requests, "total": len(ds.service_requests)}
        if p[0] == "ServiceRequest":
            sr = next((s for s in ds.service_requests if s["guid"] == p[1]), None)
            if sr is None:
                return None
            return sr if len(p) == 2 else {"matches": ds.matches.get(p[1], [])}
        raise request_client.RequestUnreachable(f"request.pdhc {what} returned 404",
                                                status_code=404)


class FakePlanClient(plan_client.PlanClient):
    def __init__(self, state: FixtureState):
        super().__init__(base_url="fixture://plan")
        self._st = state

    def _get_json(self, path, *, what, params=None, none_on_404=False):
        if self._st.is_down("plan"):
            raise plan_client.PlanUnreachable(f"plan.pdhc {what} network error: ConnectionError")
        p = path.strip("/").split("/")[2:]
        rows = [{"guid": g, "concept_guid": g, "concept_name": n, "concept_display_text": n}
                for g, n, _u in CONCEPTS.values()]
        if p == ["concepts"]:
            return {"items": rows, "total": len(rows)}
        if p[0] == "concepts":
            return next((r for r in rows if r["guid"] == p[1]), None)
        if p[0] == "PlanDefinition" and len(p) == 2:
            return self._st.dataset.plan_definitions.get(p[1])
        if p == ["PlanDefinition"]:
            return {"resourceType": "Bundle", "entry": [
                {"resource": r} for r in self._st.dataset.plan_definitions.values()]}
        if none_on_404:
            return None
        raise plan_client.PlanUnreachable(f"plan.pdhc {what} returned 404")


class FakeIcd11Client(icd11_client.Icd11Client):
    def __init__(self, state: FixtureState):
        super().__init__(base_url="fixture://icd11")
        self._st = state

    def _get(self, path, *, what, params=None, item=False):
        if self._st.is_down("icd11"):
            raise icd11_client.Icd11Unreachable(
                f"icd11.pdhc {what} failed (network): ConnectionError")
        p = path.strip("/").split("/")[2:]
        if p[0] == "patients" and p[-1] == "diagnosis-summary":
            include_inactive = (params or {}).get("include_inactive") == "true"
            entries = []
            for i, d in enumerate(self._st.dataset.diagnoses.get(p[1], [])):
                if d["status"] in ("resolved", "inactive") and not include_inactive:
                    continue
                entries.append({"resource": {
                    "resourceType": "Condition", "id": _g(K_CONTRACT, 100 + i),
                    "clinicalStatus": {"coding": [{"system": icd11_client.CLINICAL_STATUS_CS,
                                                   "code": d["status"]}]},
                    "verificationStatus": {"coding": [{"system": icd11_client.VERIFICATION_STATUS_CS,
                                                       "code": "confirmed"}]},
                    "code": {"text": ICD11_LABELS[d["code"]],
                             "coding": [{"system": _MMS, "code": d["code"],
                                         "display": ICD11_LABELS[d["code"]],
                                         "version": ICD11_RELEASE}]},
                    "subject": {"reference": f"Patient/{p[1]}"},
                    "onsetDateTime": d["onset"]}})
            return {"resourceType": "Bundle", "type": "collection",
                    "meta": {"tag": [
                        {"system": icd11_client.AS_OF_TAG_SYSTEM,
                         "code": _iso(self._st.dataset.anchor)},
                        {"system": icd11_client.SCOPE_TAG_SYSTEM,
                         "code": icd11_client.SCOPE_UNRESTRICTED}]},
                    "entry": entries}
        if item:
            return None
        raise icd11_client.Icd11Unreachable(f"icd11.pdhc {what} returned 404", status_code=404)


_FAKES = {"cdr1": FakeCdr1Client, "ips": FakeIpsClient, "request": FakeRequestClient,
          "plan": FakePlanClient, "icd11": FakeIcd11Client}


def fake_client(source: str, state: FixtureState | None = None):
    st = state or _active_state()
    if st is None:
        raise RuntimeError("fixtures are not installed on the current app")
    return _FAKES[source](st)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

#: (module, attribute, source). picker/charts bind cdr1's `build_client` by
#: name at import, so their copies are wrapped too (the picker then lists the
#: three fixture patients locally).
def _factory_sites():
    from app.routes import charts, picker
    return ((cdr1_client, "build_client", "cdr1"), (picker, "build_client", "cdr1"),
            (charts, "build_client", "cdr1"), (ips_client, "_default_client", "ips"),
            (request_client, "build_client", "request"),
            (plan_client, "build_client", "plan"), (icd11_client, "build_client", "icd11"))


def _dispatching(original, source):
    """The original factory, unless the CURRENT app has fixtures installed.

    Dispatching per app (rather than replacing the factory outright) keeps an
    app WITHOUT fixtures — the rest of the suite, in the same process — on the
    real clients, and lets a test's own monkeypatch override this as before.
    """
    def factory(*a, **k):
        st = _active_state()
        if st is None:
            return original(*a, **k)
        return _FAKES[source](st)
    factory._dashboard_fixture_dispatch = True
    factory.__wrapped__ = original
    factory.__doc__ = original.__doc__
    return factory


def install(app) -> FixtureState:
    """Wire the fakes into `app`. Refuses unless AUTH_MODE=off."""
    mode = str(app.config.get("AUTH_MODE", "off") or "off").strip().lower()
    if mode != "off":
        raise FixturesRefused(
            f"DASHBOARD_FIXTURES is set with AUTH_MODE={mode!r}. Synthetic "
            "fixture data must never be servable behind a real login; the "
            "fixtures run only with AUTH_MODE=off (local development).")
    state = FixtureState(dataset=build_dataset(
        parse_anchor(app.config.get("DASHBOARD_FIXTURES_ANCHOR")
                     or os.environ.get("DASHBOARD_FIXTURES_ANCHOR"))))
    state.failing = _parse_sources(app.config.get("DASHBOARD_FIXTURES_FAIL")
                                   or os.environ.get("DASHBOARD_FIXTURES_FAIL", ""))
    app.extensions[EXTENSION_KEY] = state
    # No live upstream may be reachable from a fixture app: a scheme requests
    # refuses outright, so an uncovered path fails instead of calling out.
    for key, source in (("CDR1_BASE_URL", "cdr1"), ("IPS_BASE_URL", "ips"),
                        ("REQUEST_BASE_URL", "request"), ("PLAN_BASE_URL", "plan"),
                        ("ICD11_BASE_URL", "icd11"), ("GATEWAY_BASE_URL", "gateway"),
                        ("SSO_BASE_URL", "sso")):
        app.config[key] = f"fixture://{source}"
    app.config["CDR_ENDPOINTS"] = []          # the CDR2-6 analyse federation
    for module, attr, source in _factory_sites():
        current = getattr(module, attr)
        if not getattr(current, "_dashboard_fixture_dispatch", False):
            setattr(module, attr, _dispatching(current, source))
    _reset_caches()
    app.logger.warning("DASHBOARD_FIXTURES installed: SYNTHETIC data, AUTH_MODE=off, "
                       "failing=%s", sorted(state.failing) or "none")
    return state


def maybe_install(app) -> FixtureState | None:
    """The `create_app` hook: install iff DASHBOARD_FIXTURES is truthy."""
    if not _truthy(app.config.get("DASHBOARD_FIXTURES")):
        return None
    return install(app)


# ---------------------------------------------------------------------------
# local runner:  python -m app.dashboard.fixtures   (scripts/run_fixtures.sh)
# ---------------------------------------------------------------------------

def main() -> None:
    import socket
    import tempfile

    host = "127.0.0.1"
    # Dashboard's port block is 9026-9029 (Rule 16/22); 9027 is the app.
    port = int(os.environ.get("FIXTURES_PORT", "9029"))
    with socket.socket() as s:
        try:
            s.bind((host, port))
        except OSError:
            raise SystemExit(f"{host}:{port} is in use; set FIXTURES_PORT to a free port "
                             "in dashboard's block (9026-9029)")
    os.environ["AUTH_MODE"] = "off"
    os.environ["DASHBOARD_FIXTURES"] = "1"
    os.environ["SESSION_COOKIE_SECURE"] = "0"
    db_dir = tempfile.mkdtemp(prefix="dashboard_fixtures_")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_dir}/fixtures.db"

    from app import create_app
    from app.models import db
    app = create_app()
    with app.app_context():
        db.create_all()
    print("dashboard.pdhc FIXTURES (synthetic data, AUTH_MODE=off) on "
          f"http://{host}:{port}")
    failing_now = sorted(state_of(app).failing)
    if failing_now:
        print(f"  failing sources: {', '.join(failing_now)}")
    for pg in PATIENTS:
        print(f"  {PATIENT_INFO[pg]['exercises']:<32} http://{host}:{port}/dashboard/view/{pg}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
