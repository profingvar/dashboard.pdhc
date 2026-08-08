# cd-assist — Technical Description

## Purpose
cd-assist is the **individual / point-of-care** half of the old dashboard,
kept after the analyse-engine split (#533 / #543). It serves a treating
clinician a **single-patient** clinical-decision-assist surface: a patient
picker, a CDR1-backed per-patient charts view, and the nurse single-patient
views (summary / AGP / variable / events) federated across the regional CDRs.

It is a **low-friction rename** of dashboard.pdhc: it keeps the host
`dashboard.pdhc.se`, the port block **9026–9029**, and the container names
`dashboard_pdhc_app` / `dashboard_pdhc_db` (compose project `dashboard_pdhc`).

> The **group / population** features (researcher cohort engine, cohort export,
> federated observation-search / stats / canonical / openEHR) were **removed**
> (#543) and now live in the separate **analyse.pdhc** service. This service is
> care-delivery only.

## Access model (care-delivery, not analysis)
The clinical routes are gated on a **care relationship**, not the analysis
phase (`app/auth.py`):

- `has_care_delivery_access(blob)` = `is_su_admin` OR (`user_type ==
  "professional"` AND at least one care-unit scope, i.e. a non-empty
  `scope_org_guids(blob)` = `affiliations[].care_unit_guid`, dual-read to legacy
  `organization_ids`).
- The `before_request` loader is **route-aware**: `_is_clinical_path()` (`/`,
  `/select`, `/patient/*`, `/api/v1/patient/*`, `/api/v1/designs`, `/api/nurse/*`,
  `/workspace`, `/nurse`) → `has_care_delivery_access`. `has_analysis_access`
  remains defined for legacy/compat but the live surface is care-delivery.
- **Rule 24 org scoping**: the caller's care-unit GUIDs are forwarded to each
  CDR as `X-Org-Guids` / `X-Is-Admin`; the CDR enforces the scope on its side.
  Admins send `X-Is-Admin` and no org restriction.
- **Roles** (`app/services/role_guards.py`): the nurse workspace **page**
  requires the `nurse` role (or admin). Roles derive from
  `affiliations[].role`, dual-read to legacy `roles[]`.
- **SSO re-validation (Rule 11)**: the bearer is re-validated with sso.pdhc
  `/api/auth/me/service` on every request; the blob is never cached.
- **Service-key auth**: trusted siblings in `KNOWN_SERVICES` (monitor.pdhc,
  gateway.pdhc) may present `X-Source-Service` + `X-Service-Key`. Service
  callers get a machine blob with **no** clinical roles or admin bit, so they
  cannot reach any of the clinical UI/data routes above.

## Spärr (per-patient blocks)
Both the charts series read (`app/routes/charts.py::series`) and every nurse
read (`app/routes/nurse.py::_apply_sparr`) apply spärr identically per patient:

1. Fetch the patient's active blocks from ips.pdhc (`app/services/ips_client.py`).
2. **Default (coarse)** — drop every point whose `org_guid` is a blocked clinic.
3. **`SPARR_LIFT_ENABLED` (#471.4, DPO-approved #472)** — apply the
   *indispensable-care* lift: a blocked point is exposed only if an active
   `indispensable_care` lift on its clinic covers its concept **and** its date;
   each exposure raises a `sparr_lift_exposure` audit event.

Every response carries `has_blocked_sources` so the UI can show the
metadata-only banner (PDL Ch 4 §4).

## Architecture
```
                       ┌── CDR1 (cdr.pdhc.se) ── charts + picker (care-delivery reads)
browser ──SSO──▶ cd-assist ─┤
 (dashboard.pdhc.se)   └── CDR2..CDR5 fan-out ── nurse summary/AGP/variable/events
                            │
                   ips.pdhc (spärr blocks)   Postgres 9026 / Flask(gunicorn) 9027
```

- **CDR1 client** (`app/services/cdr1_client.py`) — the patient picker
  (`list_org_patients`) and the charts view (`patient_summary` / `patient_series`)
  read the production CDR (`CDR1_BASE_URL`) under a care-delivery basis, using
  `DASHBOARD_PDHC_SERVICE_KEY` (`X-Source-Service: dashboard.pdhc`).
- **CDR fan-out** (`app/analyse/federation.py` + `aggregations.py`) — the nurse
  views fan out concurrently across CDR2–5 (`CdrRegistry.from_config`, reads
  `CDR_ENDPOINTS`), tolerating partial results; aggregators include AGP band
  merge and LTTB downsampling. (This read core is shared with analyse.pdhc; keep
  fixes mirrored — decision D5.)

## Components
- `app/__init__.py` — Flask app factory, config, logging.
- `app/auth.py` — care-delivery gate, route-aware loader, org scoping,
  service-key auth, `flask create-su` CLI (Rule 23).
- `app/services/role_guards.py` — `nurse_required` / admin role checks.
- `app/services/cdr1_client.py` — CDR1 care-delivery client (picker + charts).
- `app/services/ips_client.py` — spärr blocks + lift filtering.
- `app/services/audit.py` — `@audit_read` decorator writing `dashboard_audit`.
- `app/analyse/federation.py`, `aggregations.py` — CDR fan-out + aggregations
  (shared with analyse.pdhc).
- **Routes**
  - `routes/views.py` — legacy redirects: `/` → `/select`,
    `/patient/<guid>` → `/patient/<guid>/charts`.
  - `routes/picker.py` — `GET /select` clinical patient picker (CDR1).
  - `routes/charts.py` — `GET /patient/<guid>/charts` (page) +
    `GET /api/v1/patient/<guid>/parameters` + `.../series` (CDR1, spärr).
  - `routes/nurse.py` — `GET /api/nurse/patient/<guid>{,/agp,/variable/<c>,/events}`
    (CDR2–5 fan-out, spärr).
  - `routes/workspace.py` — `/workspace` (→ nurse) and `/nurse` HTML shell.
  - `routes/designs.py` — `/api/v1/designs` CRUD (user-private SavedDesign).
  - `routes/admin.py` — SU-only `/admin/audit` + `/admin/audit/export.csv`.
  - `routes/auth.py` — SSO `/auth/login|callback|logout`.
- `app/templates/` — `select.html`, `charts.html`, `nurse_workspace.html`,
  `admin_audit.html`, `base.html`.
- `app/migrations/` — Alembic migrations (`flask db upgrade`). The old
  `ObservationCache` / `RefreshLog` surface was dropped (`drop0719cache01`);
  live clinical reads go straight to the CDRs (operator #469 Q6).

## Data model (`app/models/__init__.py`)
- `User` — local mirror of the SSO caller (+ `create-su` bootstrap).
- `OrgMembership` — legacy org linkage.
- `DashboardAudit` — read-side PDL Ch 4 §3 kontroller log; one row per
  patient-touching read (incl. denials and `sparr_lift_exposure`).
- `SavedDesign` — user-private reusable view config (owner = SSO `user_guid`).
- `Cohort` — legacy cohort rows; the cohort engine itself moved to analyse.pdhc.

## Environment variables (.env)
- `APP_PORT` (9027), `DB_HOST`/`DB_PORT` (9026)/`DB_NAME`/`DB_USER`/
  `DB_PASSWORD`/`DATABASE_URL` — own Postgres.
- `AUTH_MODE` — `off` (dev) or `sso` (prod).
- `SSO_BASE_URL`, `SSO_CLIENT_ID`, `SSO_CLIENT_SECRET`, `SSO_CALLBACK_URL`.
- `CDR1_BASE_URL` — production CDR for picker + charts (care-delivery basis).
- `DASHBOARD_PDHC_SERVICE_KEY` — outbound key presented to the CDRs.
- `CDR_ENDPOINTS` — comma-separated CDR2–5 endpoints for the nurse fan-out.
- `SPARR_LIFT_ENABLED` — enable the indispensable-care lift (#471.4/#472).
- `SECRET_KEY` — Flask session secret.

## Endpoints
| Path | Method | Purpose |
|------|--------|---------|
| `/healthz` | GET | liveness + AUTH_MODE |
| `/metadata` | GET | FHIR R5 CapabilityStatement |
| `/` | GET | → `/select` (picker) |
| `/select` | GET | clinical patient picker (CDR1) |
| `/patient/<guid>` | GET | → `/patient/<guid>/charts` |
| `/patient/<guid>/charts` | GET | per-patient charts page |
| `/api/v1/patient/<guid>/parameters` | GET | concept list for the dropdown |
| `/api/v1/patient/<guid>/series` | GET | series points (CDR1, spärr) |
| `/api/nurse/patient/<guid>` | GET | summary (demographics/conditions/regimen/latest) |
| `/api/nurse/patient/<guid>/agp` | GET | AGP bands + summary (`?window=14d\|90d`) |
| `/api/nurse/patient/<guid>/variable/<canonical>` | GET | single-variable series (downsampled) |
| `/api/nurse/patient/<guid>/events` | GET | encounter + hypo markers |
| `/workspace`, `/nurse` | GET | nurse workspace (nurse role/admin) |
| `/api/v1/designs` | GET/POST/PUT/DELETE | user-private saved designs |
| `/admin/audit` | GET | SU-only audit browse (#215) |
| `/admin/audit/export.csv` | GET | SU-only audit CSV export (#215) |

## Running locally
```
./start.sh
```
Containerised (#170 / Option C #157): app + DB both run as Docker containers
in compose project `dashboard_pdhc` (`dashboard_pdhc_app` / `_db`). Health path
is `/healthz`. The script kills only its own 9026–9029 block and never touches
the Colima DB port destructively.

## Tests
```
app/.venv/bin/python -m pytest app/tests
```
