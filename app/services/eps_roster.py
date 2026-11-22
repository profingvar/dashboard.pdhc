"""The patient register's roster for the picker (#1050, operator 2026-11-22:
"why is the patient list in the invite lacking patient names and
personnummer?").

cdr1 holds no demographics by design, so the names and personnummer come from
eps (ips.pdhc), the patient register:

    eps.pdhc  GET {IPS_BASE_URL}/api/v1/clinics/<care_unit_guid>/patients
              -> [PatientIndex.to_dict(), ...] for the unit's ACTIVE patients

AUTH IS THE PROFESSIONAL'S OWN SSO BEARER (the invite_client rule): eps
re-validates it with sso and lets a professional read only the rosters of
units they are affiliated to (eps `list_clinic_patients`, #1050 gate). No
service key is sent.

WHICH UNITS: `affiliations[].care_unit_guid` of the caller (Rule 24) — never
`organization_ids`. Bounded by MAX_UNITS so one picker load cannot fan out
without limit.

THE PERSONNUMMER passes through this module only into the returned rows. It
is never logged and never put in an exception, a URL or an audit row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services import invite_client as ic

#: One picker load asks eps at most this many units.
MAX_UNITS = 20

#: The identifier systems whose value is shown as a personnummer.
PNR_SYSTEMS = {
    "urn:oid:1.2.752.129.2.1.3.1",   # personnummer
    "urn:oid:1.2.752.129.2.1.3.3",   # samordningsnummer
}

_TWELVE = re.compile(r"^(\d{8})-?(\d{4})$")


def display_personnummer(system: str | None, value: str | None) -> str:
    """YYYYMMDD-XXXX, or "" when the identifier is not a (12-digit)
    personnummer/samordningsnummer. Never raises, never logs."""
    if not value or (system and system not in PNR_SYSTEMS):
        return ""
    m = _TWELVE.match(str(value).strip())
    return f"{m.group(1)}-{m.group(2)}" if m else ""


@dataclass
class Roster:
    #: guid (lower-case) -> {"guid", "name", "personnummer", "birth_date",
    #: "units": [unit names]}
    patients: dict = field(default_factory=dict)
    #: "ok" | "unavailable" | "not_configured" | "none" (no unit to ask)
    status: str = "none"
    units_asked: int = 0
    units_failed: int = 0
    #: units refused by eps (403): eps disagrees that the caller is affiliated
    units_refused: int = 0
    truncated: bool = False


def caller_units(blob: dict | None) -> list[dict]:
    """[{"guid", "name"}] from affiliations[], de-duplicated, lower-case."""
    out, seen = [], set()
    for a in (blob or {}).get("affiliations") or []:
        if not isinstance(a, dict):
            continue
        g = str(a.get("care_unit_guid") or "").strip().lower()
        if g and g not in seen:
            seen.add(g)
            out.append({"guid": g, "name": a.get("care_unit_name") or g})
    return out


def _name(p: dict) -> str:
    return " ".join(x for x in (str(p.get("given_name") or "").strip(),
                                str(p.get("family_name") or "").strip()) if x)


def read_roster(units: list[dict]) -> Roster:
    """Ask eps for each unit's roster with the caller's bearer."""
    r = Roster()
    if not units:
        return r
    r.truncated = len(units) > MAX_UNITS
    units = units[:MAX_UNITS]
    try:
        base = ic._eps_base()
    except ic.InviteError:
        r.status = "not_configured"
        return r
    for u in units:
        r.units_asked += 1
        try:
            _, data, _ = ic._get("eps", f"{base}/api/v1/clinics/{u['guid']}/patients")
        except ic.InviteError as e:
            if e.status == 403:
                r.units_refused += 1
            elif e.status == 404:
                # eps has no clinic for this unit: an empty roster, not an outage.
                pass
            else:
                r.units_failed += 1
            continue
        if not isinstance(data, list):
            r.units_failed += 1
            continue
        for p in data:
            if not isinstance(p, dict):
                continue
            g = str(p.get("guid") or "").strip().lower()
            if not g:
                continue
            row = r.patients.get(g)
            if row is None:
                row = r.patients[g] = {
                    "guid": g, "name": _name(p),
                    "personnummer": display_personnummer(
                        p.get("identifier_system"), p.get("identifier_value")),
                    "birth_date": p.get("birth_date"),
                    "units": [],
                }
            if u["name"] not in row["units"]:
                row["units"].append(u["name"])
    if r.units_failed == r.units_asked:
        r.status = "unavailable"
    else:
        r.status = "ok"
    return r
