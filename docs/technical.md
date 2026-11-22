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

## EPS review v2: card versions (#1049)
In v2 the patient sends a whole SECTION of their patient card at a time
(allergy, medication, history, vaccination, contact), as a `card_version`
(contract `plans/patient_app_contract.md` s7.4/s7.5). `/eps-granskning` now
shows those versions first ("Patientkort att granska") and the v1 per-row queue
below them ("Enskilda svar") for rows without a `card_version`.

- **Code**: card routes at the end of `app/routes/eps_review.py`; the display
  diff in `app/services/card_diff.py`; client methods `list_cards`,
  `approve_card`, `reject_card` and the exception `ReviewSuperseded` in
  `app/services/cdr_review_client.py`; templates `_card_diff.html` (macro, the
  four-column table) and `eps_review_card_confirm.html`; CSS `.cd-*` at the end
  of `dashboard.css`.
- **What it calls** (same credential as v1: the operator's own bearer, no
  service key): `GET /api/v1/reviews/cards?status=pending`,
  `POST /api/v1/reviews/cards/<card_version>/approve` (`{"amend": {"<row guid>":
  value}?, "note"?}`), `POST …/reject` (`{"reason"}`).
- **Routes**: `POST /eps-granskning/kort/<card_version>/bekrafta` and
  `…/beslut`, two steps as in v1, both CSRF-checked, `card_version` must be a
  UUID (else 404, nothing sent). Both re-read the card list and act only on a
  version cdr1 lists as pending NOW; otherwise "no longer in your queue" with
  nothing sent (decided, or superseded by a newer version from the patient).
- **Layout**: versions grouped per patient, then per section, in cdr1's order.
  Each line: question, proposed value (the patient's), currently endorsed
  value, mark (Ny / Ändrad / Borttagen / Oförändrad) as TEXT; the left border
  style repeats it. From 768px four columns; narrower, each line stacks and every
  cell carries its own label (`.cd-k`). Touch targets 44px, inputs 16px.
- **The diff is display only** (`card_diff.py` docstring). A row without
  `group` is matched on `link_id`. A repeating-group instance is matched whole:
  same `group` value -> field by field; else identical content -> unchanged;
  else added, and an endorsed instance left over is removed. Instances are
  never paired by position: gateway mints `group` per receipt, so position
  pairing would call a new allergy a "changed" old one. Values are compared as
  shown (`value` + `display`). Nothing is hidden, sorted or highlighted by
  meaning.
- **Approve with amend**: each amendable proposed row has an EMPTY field
  `amend-<row guid>`; empty = not amended. A value is coerced to the original's
  JSON type (text, number, yes/no) as in v1 and an unchanged value is refused.
  Only the version's own rows are read from the form. A CODED row (its
  `display` differs from its value: the value is a code from the form's answer
  list) and a structured value are not amendable here; reject with a reason.
- **409**: `superseded` -> "the patient has sent a newer version of <section>;
  not recorded"; `already_decided` (or any other 409) -> "someone else already
  decided; not recorded". Both return to the queue (303) with a flash.
- **v1 rows and cards**: a row listed by the per-row API that has a
  `card_version`, or whose guid is in a card's rows, is shown only in its card
  and the per-row routes refuse to decide it ("gone", nothing sent).
- **Failure**: the two lists fail separately. A failed card read renders
  `#cd-problem` (`data-problem`, e.g. `card_api_missing` for a cdr1 without the
  s7.4 routes) and the v1 list is still shown; the page answers the failure's
  status. The nav count is `null` if EITHER list failed; otherwise v1 total +
  number of card versions.
- **Audit**: `eps_card_confirm`, `eps_card_approve`, `eps_card_reject`,
  `eps_card_decision` (refused before an action); payload `card_version`,
  `section`, `action`, `outcome`, `n_amended`, `endorsed`, `note_given`,
  `cdr_status`. Never a value, the note or the reason. The queue row adds
  `n_cards`.
- **`can_decide: false`** on a version (as cdr1 does on v1 items) -> no
  actions, and a POST is refused 403 without calling cdr1.

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
  - `routes/picker.py` — `GET /select` clinical patient picker (eps rosters ∪ CDR1; `services/eps_roster.py`).
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
| `/select` | GET | clinical patient picker (eps rosters ∪ CDR1, #1050) |
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
| `/eps-granskning/kort/<card_version>/bekrafta` | POST | card version: confirmation step (CSRF, #1049) |
| `/eps-granskning/kort/<card_version>/beslut` | POST | card version: send the decision to cdr1 (CSRF, #1049) |
| `/admin/audit` | GET | SU-only audit browse (#215) |
| `/admin/audit/export.csv` | GET | SU-only audit CSV export (#215) |

## Patient picker: names and personnummer from eps (#1050, 2026-11-22)

Operator: "why is the patient list in the invite lacking patient names and
personnummer?" The picker listed cdr1's `GET /api/v1/clinical/patients` only.
cdr1 holds no demographics by design (production 2026-11-22: 51 patients with
observations, 1 with a name, none with a personnummer), and lists only
patients WITH observations, so a newly registered patient could not be opened
and therefore not invited.

`GET /select` (`routes/picker.py`) is now the UNION of:

1. **eps rosters** (`services/eps_roster.py`): `GET
   {IPS_BASE_URL}/api/v1/clinics/<care_unit_guid>/patients` for each unit in
   `affiliations[].care_unit_guid` (Rule 24; never `organization_ids`), at most
   `MAX_UNITS` (20) per load. Auth = the professional's own SSO bearer (the
   invite_client rule, reusing its `_get`); no service key. eps admits that
   bearer without a local eps user row and only for units the caller is
   affiliated to (eps `require_auth_or_care_bearer` + 403 `not_affiliated`).
   Name = given + family; personnummer shown as `YYYYMMDD-XXXX` only for the
   personnummer/samordningsnummer OIDs.
2. **cdr1** `list_org_patients`, unchanged (service key, `X-Org-Guids` /
   break-glass `X-Is-Admin`): observation count and last observed. cdr1's
   `name` is not displayed.

Merged by lower-case guid. A cdr1 row not in the rosters reads "ej i
patientregistret" only when every roster was read and the caller is not an
SU; otherwise "namn okänt". eps-only rows read "ingen data än" and link to
`/patient/<guid>/charts` like every row, so they can be opened and invited.

Degrade: every unit failed (network, 5xx) or `IPS_BASE_URL` unset → banner
`#eps-down` "Patientregistret (eps) svarar inte – namn saknas" with the cdr1
list; some units failed → `#eps-partial`; eps 403 on a unit → `#eps-refused`;
more than 20 units → `#eps-truncated`. eps 404 (no clinic for the unit) is an
empty roster, not an outage.

SU: break-glass attestation first (#575, unchanged); then cdr1's whole set plus
the eps rosters of the SU's OWN affiliated units only. Reading every eps clinic
would put the whole register on one page, unbounded, so it is not done.

Personnummer: in the page only (`no-store`, `Pragma: no-cache`); never logged,
never in a URL. The audit row carries counts only:
`payload_snapshot = {eps_roster, eps_units_asked, eps_units_failed,
eps_units_refused, eps_patients, cdr1_patients}`. The filter box searches
`data-search` (name, personnummer with and without dash, guid) client-side.

Tests: `app/tests/test_picker_roster_1050.py` (22), auth ON, only
`requests.get/post` patched; `test_patient_picker.py` updated (cdr1 names no
longer shown).

Depends on eps (ips.pdhc) with the #1050 roster gate deployed FIRST: the old
eps answers 401 to a professional without an eps user row, which the picker
shows as the eps-down banner.

## Bjud in till patientappen (#1050, v2 2026-11-22)

`app/routes/invite.py` (blueprint `invite`, `/bjud-in-patient`) with
`app/services/invite_client.py`. Patients join the patient app only by
invitation. v2 (operator 2026-11-22): the invite is an action on the patient
the professional has OPEN; nothing about the patient is typed. The v1
standalone form (name, personnummer, unit) is removed, for SU too.

| route | does |
|---|---|
| `GET /bjud-in-patient` | 302 to the patient picker (`/select`): a patient is always chosen first |
| `GET /bjud-in-patient/<guid>` | the page for the open patient: name (eps), unit, mobile to confirm |
| `POST /bjud-in-patient/<guid>/bekrafta` | CSRF + validation; the confirmation page. Reads eps, writes nothing |
| `POST /bjud-in-patient/<guid>/skicka` | CSRF + `confirmed=ja`; sso invite; the done page with the `sms:` button |

- **The patient in context.** The dashboard has no session patient: the
  patient is the guid in the URL of the view opened from the picker.
  `charts_page` (`/patient/<guid>/charts`) and `patient_view`
  (`/dashboard/view/<guid>`) call `remember_opened_patient`, which keeps the
  last 20 opened guids in the session (`_opened_patients`). The invite routes
  refuse a guid not opened in this session (403 "Öppna patienten först"), and
  refuse a `patient_guid` / `ips_patient_guid` form field that differs from
  the path (400). Both views show "Bjud in till patientappen" when
  `has_invite_page_access` holds. The base-template nav item is gone.
- **eps reads**, with the user's own bearer: `GET /api/v1/patients/<guid>/state`
  (name.given, name.family, telecom; eps scopes it with
  `patient_export.exportable`, 404 outside the reader's scope) and
  `GET /api/v1/patients/<guid>/clinics` (units the patient is ACTIVELY listed
  at; the same list sso's `_eps_check` reads). An `X-PDHC-Patient-Guid` that
  differs from the guid is refused (`not_platform_guid`). The name always comes
  from eps, never from the form. No full name in eps → 409 refusal.
- **Units.** Non-SU: `affiliations[].care_unit_guid`, narrowed to the units eps
  lists the patient at. One → preselected; several → `required` radios, none
  `checked`; none → 409 "Patienten är inte listad vid …". No affiliation and
  not SU → 403 before eps is asked. SU: the units eps lists. `register-at-unit`
  is NOT called any more: it needs a personnummer, which this flow does not
  have, so a patient not listed at the unit is refused, not attached.
- **Mobile.** Prefilled from eps (first phone that normalises to a Swedish
  mobile, `use=mobile` first; else the first raw value, to be corrected);
  required; normalised to `+467XXXXXXXX` (`normalise_mobile`, a per-repo copy
  of sso's rule). A number different from eps's is "for this message only, not
  saved": eps's only telecom write is the unscoped whole-resource FHIR
  `PUT /fhir/Patient/<id>`, so no save is offered.
- **sso**: `POST {SSO_BASE_URL}/api/patient-links/invites` with
  `ips_patient_guid` (the path guid), `care_unit_guid` and eps's name, bearer
  only, no service key. sso re-checks the affiliation and asks eps that the
  patient is at the unit. Errors are named (`patient_already_linked` → 409
  "Patienten har redan ett konto…").
- **The message.** `invite_done.html`, `Cache-Control: no-store`, shown once:
  a button "Skicka inbjudan i Meddelanden" with
  `href = sms:<+46…>&body=<quote(text, safe="")>` (`sms_href`). The `&body=`
  form is Apple's (iOS 8+, and the form used for macOS Messages); RFC 5724's
  `?body=` is not used. `safe=""` encodes spaces as `%20` and every `& ? = # /
  +` of the link, so the link cannot end the body. Text: "Hej <förnamn>!
  <Vårdenhet> bjuder in dig till Patientappen. Öppna länken för att skapa ditt
  konto: <länk> Länken gäller en gång." Copy buttons for the text and for the
  link. The page says Messages opens with the text filled in and that it
  cannot see whether the message was sent. **Not verified on a real
  iPhone/iPad/Mac.**
- **Audit**: `patient_invite_form` / `_confirm` / `_send` with `outcome`
  (`shown`, `invited`, `not_in_context`, `patient_mismatch`, `not_listed`,
  `eps_<code>`, `sso_<code>`, `csrf_refused`, …), the patient guid and unit;
  never the name, the mobile or the link.
- **Config**: `IPS_BASE_URL` (filled from `EPS_BASE_URL` first, #1019) and
  `SSO_BASE_URL`; unset → named "inte konfigurerad" message.
- Tests: `app/tests/test_invite_1050.py` (35), auth ON, only HTTP patched.

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
