"""Loggkontroll (#1045): one patient's access log, merged across services.

Patientdatalagen 4 kap. 3 §: the care provider checks, systematically and
repeatedly, who has accessed a patient's records. The log is spread over the
services that hold the patient's data, so this module asks each of them for
ITS log of ONE patient and merges the answers into one chronological list.

    dashboard.pdhc   its own `dashboard_audit` (this database)
    cdr.pdhc (cdr1)  GET {CDR1_BASE_URL}/api/v1/access-log/patients/<guid>
    eps.pdhc         GET {IPS_BASE_URL}/api/v1/audit/patients/<guid>
    request.pdhc     GET {REQUEST_BASE_URL}/api/v1/patients/<guid>/access-log

AUTH IS THE PROFESSIONAL'S OWN SSO BEARER on all three, and nothing else
(the cdr_review_client / invite_client rule): each service decides for itself
whether this person may read this patient's log (SU, or a professional
affiliated to the patient's care unit) and logs that read. No service key is
sent: a key is organisation-blind, and a log of who saw a patient is not
something a machine reads on its own behalf. request.pdhc takes the bearer
as `X-API-Key` (its documented door, see request_client).

NEVER SILENT. A service that could not be asked, or that refused, becomes a
`SourceResult` with `ok=False` and a Swedish sentence saying so ("Kunde inte
hämtas från ..."). An empty list from here always means that service answered
and has nothing in the period. "Nobody looked" and "we could not ask" must
never read the same, or the check certifies a gap.

Every service answers in one shape (keys below, `ITEM_KEYS`); each repo
documents its own mapping next to its endpoint.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import requests
from flask import current_app, session

STOCKHOLM = ZoneInfo("Europe/Stockholm")
DEFAULT_TIMEOUT = 10.0
#: per service; the page says when a service had more.
LIMIT = 2000

#: The keys every service's access-log item carries (#1045).
ITEM_KEYS = ("id", "at", "user_guid", "user_label", "actor_type", "action",
             "outcome", "route", "resource_type", "session_id", "actor_units",
             "basis", "detail")

#: Working hours for "utanför arbetstid": weekdays, [start, end) local time.
#: A unit with other hours sets LOGGKONTROLL_WORKDAY="06-22" (operator).
DEFAULT_WORKDAY = (7, 18)

#: The flags of the systematic check, in display order, with their Swedish
#: names. Each is True, False or None (= the row cannot tell).
FLAGS = (
    ("nodoppning", "Nödöppning / särskild åtkomst"),
    ("utanfor_enhet", "Användaren är inte knuten till patientens vårdenhet"),
    ("nekad", "Åtkomsten nekades"),
    ("su_admin", "Systemadministratör"),
    ("utanfor_arbetstid", "Utanför arbetstid"),
)

SERVICE_LABELS = {
    "dashboard.pdhc": "Dashboard",
    "cdr.pdhc": "Journaldatabasen (cdr1)",
    "eps.pdhc": "Patientregistret (eps)",
    "request.pdhc": "Beställningar (request)",
}


@dataclass
class SourceResult:
    service: str
    ok: bool
    items: list = field(default_factory=list)
    truncated: bool = False
    #: Swedish, shown to the reviewer when ok is False.
    error: str | None = None

    @property
    def label(self) -> str:
        return SERVICE_LABELS.get(self.service, self.service)


# --------------------------------------------------------------------------
# period
# --------------------------------------------------------------------------

def period_utc(day_from, day_to) -> tuple[datetime, datetime]:
    """Swedish calendar days [from, to] inclusive -> UTC [start, end)."""
    start = datetime.combine(day_from, time(0, 0), tzinfo=STOCKHOLM)
    end = datetime.combine(day_to + timedelta(days=1), time(0, 0), tzinfo=STOCKHOLM)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_at(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# the three neighbours
# --------------------------------------------------------------------------

def _bearer() -> str | None:
    try:
        return session.get("sso_token")
    except RuntimeError:
        return None


def _session_headers() -> dict:
    try:
        from app.services.session_headers import outbound_session_headers
        return outbound_session_headers()
    except Exception:  # noqa: BLE001 — best effort, as in every other client
        return {}


def _fetch(service: str, base_key: str, path: str, auth_header: str,
           start: datetime, end: datetime) -> SourceResult:
    name = SERVICE_LABELS.get(service, service)
    base = (current_app.config.get(base_key) or "").strip().rstrip("/")
    if not base:
        return SourceResult(service, False, error=(
            f"Kunde inte hämtas från {name}: tjänsten är inte konfigurerad i "
            f"dashboard ({base_key} saknas)."))
    token = _bearer()
    if not token:
        return SourceResult(service, False, error=(
            f"Kunde inte hämtas från {name}: din inloggning saknas. "
            "Logga ut och in igen."))
    headers = {"Accept": "application/json", **_session_headers()}
    headers[auth_header] = f"Bearer {token}" if auth_header == "Authorization" else token
    try:
        r = requests.get(f"{base}{path}", headers=headers, timeout=DEFAULT_TIMEOUT,
                         params={"from": _z(start), "to": _z(end), "limit": LIMIT},
                         allow_redirects=False)
    except requests.RequestException:
        return SourceResult(service, False, error=(
            f"Kunde inte hämtas från {name}: tjänsten svarade inte."))
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code == 200 and isinstance(body, dict) and isinstance(body.get("items"), list):
        items = [_clean(service, i) for i in body["items"] if isinstance(i, dict)]
        return SourceResult(service, True, items=items,
                            truncated=bool(body.get("truncated")))
    if r.status_code in (403, 404):
        why = ("tjänsten ger dig inte tillgång till patientens logg där "
               "(du är inte knuten till patientens vårdenhet, eller tjänsten "
               "känner inte patienten)")
    elif r.status_code == 401:
        why = "tjänsten godtog inte din inloggning"
    elif r.status_code == 502:
        why = "tjänsten kunde inte kontrollera patientens vårdenhet"
    elif r.status_code == 200:
        why = "svaret hade fel form"
    else:
        why = f"tjänsten svarade med fel ({r.status_code})"
    return SourceResult(service, False, error=f"Kunde inte hämtas från {name}: {why}.")


def _clean(service: str, item: dict) -> dict:
    out = {k: item.get(k) for k in ITEM_KEYS}
    out["service"] = service
    out["via_service"] = item.get("via_service")
    if not isinstance(out["detail"], dict):
        out["detail"] = {}
    if not isinstance(out["actor_units"], list):
        out["actor_units"] = None
    return out


def from_cdr1(patient_guid: str, start, end) -> SourceResult:
    return _fetch("cdr.pdhc", "CDR1_BASE_URL",
                  f"/api/v1/access-log/patients/{patient_guid}", "Authorization",
                  start, end)


def from_eps(patient_guid: str, start, end) -> SourceResult:
    return _fetch("eps.pdhc", "IPS_BASE_URL",
                  f"/api/v1/audit/patients/{patient_guid}", "Authorization", start, end)


def from_request(patient_guid: str, start, end) -> SourceResult:
    return _fetch("request.pdhc", "REQUEST_BASE_URL",
                  f"/api/v1/patients/{patient_guid}/access-log", "X-API-Key", start, end)


def from_dashboard(patient_guid: str, start, end) -> SourceResult:
    """This service's own log. Read from the database, so it fails only when
    the database does, and then says so like any other source."""
    from app.models import DashboardAudit
    try:
        q = (DashboardAudit.query
             .filter(DashboardAudit.patient_guid == patient_guid)
             .filter(DashboardAudit.timestamp >= start)
             .filter(DashboardAudit.timestamp < end)
             .order_by(DashboardAudit.timestamp.asc()))
        rows = q.limit(LIMIT + 1).all()
    except Exception:  # noqa: BLE001 — named, never an empty list
        return SourceResult("dashboard.pdhc", False, error=(
            "Kunde inte hämtas från Dashboard: databasen svarade inte."))
    items = []
    for r in rows[:LIMIT]:
        payload = r.payload_snapshot if isinstance(r.payload_snapshot, dict) else {}
        basis = payload.get("access_basis")
        if r.event_type == "admin_override":
            basis = "break_glass"
        at = r.timestamp
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        items.append({
            "id": f"dashboard:{r.guid}",
            "at": at.isoformat() if at else None,
            "user_guid": r.user_guid,
            "user_label": None,
            "actor_type": "service" if (r.user_guid or "").find("-service-") > 0 else "user",
            "action": r.event_type or "read",
            "outcome": "denied" if (r.response_status or 0) >= 400 else "served",
            "route": r.route,
            "resource_type": None,
            "session_id": r.session_id,
            "actor_units": list(r.user_org_guids or []),
            "basis": basis,
            "detail": ({"reason": r.admin_justification} if r.admin_justification else {}),
            "service": "dashboard.pdhc",
            "via_service": None,
        })
    return SourceResult("dashboard.pdhc", True, items=items, truncated=len(rows) > LIMIT)


#: The sources, in the order their failures are listed.
SOURCES: tuple[Callable, ...] = (from_dashboard, from_cdr1, from_eps, from_request)


# --------------------------------------------------------------------------
# merge, flags, sample
# --------------------------------------------------------------------------

def workday() -> tuple[int, int]:
    raw = str(current_app.config.get("LOGGKONTROLL_WORKDAY") or "").strip()
    try:
        a, b = (int(x) for x in raw.split("-"))
        if 0 <= a < b <= 24:
            return a, b
    except (ValueError, TypeError):
        pass
    return DEFAULT_WORKDAY


def flags_for(item: dict, patient_units: set[str] | None,
              hours: tuple[int, int] = DEFAULT_WORKDAY) -> dict:
    """The systematic check's questions about one row. None = cannot tell."""
    at = parse_at(item.get("at"))
    after = None
    if at is not None:
        local = at.astimezone(STOCKHOLM)
        after = local.weekday() >= 5 or not (hours[0] <= local.hour < hours[1])
    units = item.get("actor_units")
    outside = None
    if isinstance(units, list) and patient_units is not None and item.get("actor_type") == "user":
        outside = not ({str(u).lower() for u in units} & patient_units)
    basis = item.get("basis")
    return {
        "nodoppning": basis == "break_glass" or item.get("outcome") == "break_glass",
        "utanfor_enhet": outside,
        "nekad": item.get("outcome") == "denied",
        "su_admin": basis == "su_admin",
        "utanfor_arbetstid": after,
    }


def is_unusual(flags: dict) -> bool:
    return any(v is True for v in flags.values())


def label_users(items: Iterable[dict]) -> None:
    """Fill a missing `user_label` from dashboard's own users table (every
    person who has logged in here has a row: guid = sso user guid)."""
    from app.models import User
    wanted = {i["user_guid"] for i in items if i.get("user_guid") and not i.get("user_label")}
    if not wanted:
        return
    try:
        names = {u.guid: u.username for u in User.query.filter(User.guid.in_(list(wanted))).all()}
    except Exception:  # noqa: BLE001 — a label is a convenience
        return
    for i in items:
        if not i.get("user_label") and i.get("user_guid") in names:
            i["user_label"] = names[i["user_guid"]]


def collect(patient_guid: str, start, end, patient_units: set[str] | None,
            sources: Iterable[Callable] = SOURCES) -> tuple[list[dict], list[SourceResult]]:
    results = [src(patient_guid, start, end) for src in sources]
    items: list[dict] = []
    for res in results:
        items.extend(res.items)
    hours = workday()
    for i in items:
        i["flags"] = flags_for(i, patient_units, hours)
        i["unusual"] = is_unusual(i["flags"])
        at = parse_at(i.get("at"))
        i["at_local"] = at.astimezone(STOCKHOLM).strftime("%Y-%m-%d %H:%M:%S") if at else ""
    label_users(items)
    items.sort(key=lambda i: (parse_at(i.get("at")) or datetime.min.replace(tzinfo=timezone.utc),
                              i.get("service") or "", str(i.get("id"))))
    return items, results


def sample(items: list[dict], n: int, seed: int) -> list[dict]:
    """A reproducible random sample: the same seed gives the same rows, so a
    reviewer can show which rows were checked. Chronological order kept."""
    if n >= len(items):
        return list(items)
    picked = set(random.Random(seed).sample(range(len(items)), n))
    return [it for k, it in enumerate(items) if k in picked]
