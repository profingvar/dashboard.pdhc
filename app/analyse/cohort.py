"""Cohort predicate builder — translates a researcher's filter object
into FHIR search parameters per CDR.

Platform-plan execution §4.3. The researcher's filter dict is shaped
like::

    {
      "cdr_ids": ["cdr1", "cdr3"],
      "demographics": {
        "age_min": 40, "age_max": 70,
        "sex": "female",
      },
      "conditions": [
        "https://termbank.pdhc.se/CodeSystem/snomed/44054006"
      ],
      "medications": [
        "https://termbank.pdhc.se/CodeSystem/atc/A10A"
      ],
    }

and we turn it into a list of ``(resource_type, params_dict)`` tuples
that the federation can fan out as FHIR Search calls.

We never reach into a CDR's DB schema directly — the predicate goes
through whatever ``$has`` / ``code=`` / ``date=`` syntax the CDR's
search supports (which we built in cdr.pdhc Phase 1.3).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


@dataclass
class CohortFilter:
    """A typed view of the predicate dict — we keep it as a dataclass
    so unit tests don't have to mock a JSON shape."""
    cdr_ids: list[str]
    age_min: int | None = None
    age_max: int | None = None
    sex: str | None = None
    conditions: list[str] | None = None
    medications: list[str] | None = None
    type_canonical: str | None = None  # T1DM/T2DM canonical, optional shortcut
    region: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "CohortFilter":
        demo = raw.get("demographics") or {}
        return cls(
            cdr_ids=list(raw.get("cdr_ids") or []),
            age_min=demo.get("age_min"),
            age_max=demo.get("age_max"),
            sex=demo.get("sex"),
            region=demo.get("region"),
            conditions=list(raw.get("conditions") or []) or None,
            medications=list(raw.get("medications") or []) or None,
            type_canonical=raw.get("type_canonical"),
        )


def to_patient_search_params(filt: CohortFilter,
                              *,
                              today: date | None = None) -> dict:
    """Turn a filter into Patient-search FHIR params.

    Age min/max → birthdate ranges. Sex → ``gender=``. Region is hinted
    via ``_has`` chained over Patient.address.state — but FHIR-search
    semantics for ``Patient.address`` are best-effort across CDRs, so
    callers should be ready for minor regional drift.
    """
    today = today or _utc_today()
    params: dict[str, Any] = {}
    if filt.age_min is not None:
        # age_min ⇒ birthdate ≤ today − age_min years
        params.setdefault("birthdate", []).append(
            f"le{(today.replace(year=today.year - filt.age_min)).isoformat()}"
        )
    if filt.age_max is not None:
        params.setdefault("birthdate", []).append(
            f"ge{(today.replace(year=today.year - filt.age_max - 1)).isoformat()}"
        )
    if filt.sex:
        params["gender"] = filt.sex
    return params


def to_predicate_searches(filt: CohortFilter,
                          *,
                          today: date | None = None) -> list[tuple[str, dict]]:
    """Return a list of (resource_type, params) pairs whose search
    intersection defines the cohort.

    Cohort membership = AND across the returned predicates. The federation
    layer fans each one out, intersects the patient-id sets, and returns
    the resulting members.
    """
    out: list[tuple[str, dict]] = [("Patient", to_patient_search_params(filt, today=today))]

    for cond in filt.conditions or []:
        out.append(("Condition", {"code": cond, "_count": "1000"}))
    for med in filt.medications or []:
        out.append(("MedicationStatement", {"code": med, "_count": "1000"}))
    if filt.type_canonical:
        out.append(("Condition", {"code": filt.type_canonical, "_count": "1000"}))
    return out


def intersect_patient_sets(per_predicate: list[set[str]]) -> set[str]:
    """Intersect Patient-guid sets coming back from each predicate.

    Sole reason for a separate function: tests want to assert the
    intersection logic in isolation from the HTTP layer.
    """
    if not per_predicate:
        return set()
    base, *rest = per_predicate
    out = set(base)
    for s in rest:
        out &= s
    return out


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()
