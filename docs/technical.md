# dashboard — Technical Description

## Purpose
dashboard is the **individual / point-of-care** half of the platform's clinical
tooling, kept after the analyse-engine split (#533 / #543). It serves a treating
clinician a **single-patient** clinical-decision-assist surface: a patient
picker, a CDR1-backed per-patient charts view, and the nurse single-patient
views (summary / AGP / variable / events) federated across the regional CDRs.

It runs on the host `dashboard.pdhc.se`, port block **9026–9029**, with the container names
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
  `/workspace`, `/nurse`, `/eps-granskning*`) → `has_care_delivery_access`. `has_analysis_access`
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

## EPS review queue, "EPS att granska" (#1044, epic #1039)
Patients answer the EPS form in the patient app. Each answer is stored in cdr1
as a patient-generated ORIGINAL with `review_status = pending`. The queue lets a
professional record one decision per original. Contract:
`plans/patient_app_contract.md` s4.2 and s5 (workspace repo).

- **Code**: `app/routes/eps_review.py` (routes), `app/services/cdr_review_client.py`
  (cdr1 client), `app/services/csrf.py`, templates `eps_review.html`,
  `eps_review_confirm.html`, `eps_review_message.html`, CSS at the end of
  `static/css/dashboard.css` (`.rv-*`), nav link + count script in `base.html`.
- **What it calls**: `GET /api/v1/reviews?status=pending`,
  `POST /api/v1/reviews/<guid>/approve` (`{"value"?, "note"?}`),
  `POST /api/v1/reviews/<guid>/reject` (`{"reason"}`) on `CDR1_BASE_URL`.
- **Credential**: the logged-in professional's own SSO bearer
  (`session["sso_token"]`) and nothing else. The dashboard service key is
  deliberately NOT sent on these calls: the decision must be the person's, and
  cdr1 decides who may endorse (doctor or nurse affiliated to the patient's
  care unit; an admin may list and not decide). The dashboard does not
  re-implement that rule; it shows no action buttons on an item cdr1 marks
  `can_decide: false` and renders cdr1's 403 as a refusal.
- **Gate**: `/eps-granskning*` is a clinical path, so the request loader
  applies the care-delivery gate (SU admin, or professional with a care-unit
  scope), SSO re-validation per request and the `must_change_password` bounce
  before any route runs. An SU admin must first give the session's break-glass
  attestation reason (the same `POST /admin-read-attest` as the picker),
  because an admin's list spans every unit; the page answers 428 until then.
- **CSRF**: the dashboard had no CSRF mechanism before this. `app/services/csrf.py`
  adds a per-session random token, required on both POST routes as form field
  `csrf_token` or header `X-CSRF-Token`, compared in constant time. Only these
  routes use it; `/admin-read-attest` and `/api/v1/designs` still rely on the
  cookie's `SameSite=Lax`.
- **Two steps**: `POST …/bekrafta` validates the form and renders what will be
  sent (nothing goes to cdr1); `POST …/beslut` requires `confirmed=ja`,
  re-reads the item from cdr1, re-validates and sends. Reject needs a reason at
  both steps. An amended value is sent in the original's JSON type (text,
  number, yes/no); cdr1 validates it again.
- **Refusal is never an empty queue.** Every client failure is a named
  exception (`ReviewForbidden`, `ReviewUnreachable`, `ReviewApiMissing`,
  `ReviewNotConfigured`, `ReviewNoBearer`, `ReviewAlreadyDecided`,
  `ReviewNotFound`, `ReviewInvalid`). The page renders a problem panel
  (`#rv-problem`, `data-problem="<code>"`) with HTTP 403 / 502 / 503; the
  "nothing to review" text (`#rv-empty`) is rendered only when cdr1 answered
  200 with an empty `items` list. A 200 without an `items` list is a failure.
  The nav count (`GET /eps-granskning/antal`) answers `count: null` with the
  failing status, and the link then shows `?` (nothing on a 403), never 0.
  The count is asked at most once per two minutes per browser tab
  (`sessionStorage`), because each read is a cdr1 call and a read-audit row
  there; a failure is never remembered.
- **Outcomes of a decision**: 201/200 → flash + back to the queue (the item is
  gone); 409 → "someone else already decided"; 403 → refusal page; 422 →
  cdr1's code in Swedish; network/5xx → "unknown whether it was recorded".
- **Audit** (`dashboard_audit`, via `@audit_read`): `event_type`
  `eps_review_queue`, `eps_review_confirm`, `eps_review_approve|amend|reject`,
  `eps_review_decision` (refused before an action was known). The row has the
  user, care units, patient guid, and in `payload_snapshot` the `review_guid`,
  `action`, `outcome`, `cdr_status`, `endorsement_guid`, `amended`,
  `note_given`. It never holds the answer, the amended value, the note or the
  reason; those are in cdr1. The count endpoint is not audited (a number, no
  patient data). The route argument is `review_guid`, not `guid`, because
  `audit_read` reads `guid` as the patient.
- **MDR**: the view displays and records. Items keep cdr1's order, grouped per
  patient by first appearance; nothing is sorted, scored or highlighted by
  content; the three actions are styled alike and none is preselected.
- **AUTH_MODE=off** has no bearer, so the queue shows "Ingen SSO-inloggning"
  rather than data. The fixture layer does not cover this view.
- **Times** are shown in Europe/Stockholm; without tzdata in the image they
  fall back to UTC, labelled.

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
browser ──SSO──▶ dashboard ─┤
 (dashboard.pdhc.se)   └── CDR2..CDR3 fan-out ── nurse summary/AGP/variable/events
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
| `/eps-granskning` | GET | EPS review queue (#1044) |
| `/eps-granskning/antal` | GET | pending count for the nav link, JSON |
| `/eps-granskning/<review_guid>/bekrafta` | POST | confirmation step (CSRF) |
| `/eps-granskning/<review_guid>/beslut` | POST | send the decision to cdr1 (CSRF) |
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

## Port Allocation

All ports bind to `127.0.0.1` (loopback only); external traffic arrives
via the reverse proxy.

| Port | Service |
|------|---------|
| 9027 | Flask application (Gunicorn) |
| 9026 | PostgreSQL database |
| 9028–9029 | Reserved |
