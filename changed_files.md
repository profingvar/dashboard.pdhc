# changed_files.md

All edited files (full path) from now on (Rule 17).

## Ticket #211 — dashboard_audit table + decorator (2026-06-04)

- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/models/__init__.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/migrations/versions/2026_06_04_dashboard_audit.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/audit.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/views.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/nurse.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/researcher.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/api.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_audit_read.py

- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/readme.md
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/progress.md
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/changed_files.md
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/newtask.txt
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/wish_answers.md
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/.gitignore
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/.env.example
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/.env
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/requirements.txt
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/docker-compose.yml
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/start.sh
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/scripts/test_api.sh
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/__init__.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/auth.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/models/__init__.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/__init__.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/gateway_client.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/__init__.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/views.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/api.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/auth.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/base.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/landing.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/patient.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_scaffold.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_healthz.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_models.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_gateway_client.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_auth.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_views_api.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/migrations/ (alembic init + initial revision)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/docs/technical.md
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/docs/user_manual.md

# 2026-04-11 — dashboard.pdhc.se robust restart + services.html diagnosis
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/start.sh (gunicorn daemon rewrite, shared/ pid+logs, pinned COMPOSE_PROJECT_NAME)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/progress.md (§18 rewritten with real state, venv rebuild log, services.html diagnosis, TODOs)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/newtask.txt (replaced stale step-1 note with current open items)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/auth.py (Ticket #51 — before_request loader redirects to {SSO_BASE_URL}/change-password when the freshly validated blob has must_change_password=True, gated after session cache refresh and before the analysis-access check. Public paths — /auth/*, /healthz, /metadata, /static/* — still skip the loader entirely. Deployed to /usr/local/www/dashboard.pdhc/current/app/auth.py; backup .bak-2026-04-15T18-20-01Z on server. `kill -HUP 1673` reloaded gunicorn; https://dashboard.pdhc.se/healthz returns 200.)
| `app/__init__.py` | Ticket #70 / CLAUDE.md §10: added `Access-Control-Allow-Origin: https://www.pdhc.se`, `Access-Control-Allow-Methods: GET`, `Vary: Origin` on `/healthz` so www.pdhc.se/services.html can read the JSON cross-origin (drives real status + DB dots instead of no-cors opaque false-greens). Specific origin (not `*`) + `Vary: Origin` keep any future Allow-Credentials spec-compliant. Verified via `curl -I -H 'Origin: https://www.pdhc.se'`: all three headers emitted, body unchanged, OBJC preserved on new master PID 89649. Server backup at `/tmp/dashboard_app_init.py.bak.20260416T164245Z`. |

# 2026-04-16 — ticket #71 (dashboard DB probe)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/__init__.py (ticket #71 / CLAUDE.md §10: replaced the bare `{status:"ok"}` payload with a real `SELECT 1` DB probe. Now returns canonical `{status, service, database, auth_mode}` with HTTP 503 when the DB is unavailable. `Cache-Control: no-store` added. services.html DB dot can now read `data.database === 'connected'` correctly — was previously always showing red because the field was missing. Deployed + SIGHUP'd master pid 89649; verified `curl https://dashboard.pdhc.se/healthz` returns `{"auth_mode":"sso","database":"connected","service":"dashboard.pdhc","status":"ok"}`. Server backup at `/tmp/dashboard_init.server.bak.20260416T201302Z.py`.)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_healthz.py (ticket #71: added assertions for `service == "dashboard.pdhc"` and `database in ("connected","unavailable")` — passed via local pytest with in-memory sqlite.)

# 2026-04-26 — Phase 4 dashboard upgrade (platform-plan execution §4.1–§4.3, §4.6, §4.7)

- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/.gitignore (added dist/, backups/, .venv/, .pytest_cache/ — dist/ was holding build tarballs that shouldn't be in source)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/federation.py (NEW — §4.1: CdrRegistry + fanout with per-CDR timeout and complete/degraded/error mode classification, merge_histograms with parallel-variance combine for cross-cohort mean+sd, concat_series with cdr_id tagging, lttb_downsample for ≤2k chart points, agp_hourly_bands computing TIR/TBR/TAR + 5/25/50/75/95 hourly percentiles + hypo-event count from a CGM stream)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/cohort.py (NEW — CohortFilter dataclass, to_predicate_searches that turns the filter object into a list of (resource_type, FHIR-search-params) for fan-out, intersect_patient_sets for AND-ing per-predicate result sets)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/role_guards.py (NEW — §4.7: nurse_required / researcher_required / admin_required decorators reading roles from g.access_blob; admin satisfies both clinical roles)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/nurse.py (NEW — §4.2: /api/nurse/patient/<guid>, /agp?window=, /variable/<canonical>, /events; LTTB-downsample on variable series; AGP from federated CGM observations; finds owning CDR by first-ok Patient read)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/researcher.py (NEW — §4.3 + §4.6: POST /api/cohort, GET /api/cohort, GET /api/cohort/<id>/variable/<canonical>/{histogram,boxplot}, /scatter, /trend, streaming /export with CSV + audit log row at end)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/__init__.py (registered nurse_bp + researcher_bp)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_federation.py (NEW — 16 tests: cohort_predicate_builder + intersect, fanout complete/degraded/error + auth header forwarding, histogram_merge total/skip-failed/weighted-mean, concat_series cdr_id tagging, LTTB short-passthrough/extrema-preserved/target-size, AGP flat-trace-reference + hypo count)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_role_guards.py (NEW — 6 tests: nurse blocked for researcher-only, allowed for nurse, researcher blocked for nurse-only, allowed for researcher, admin satisfies both, anonymous blocked)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_researcher_flow.py (NEW — 4 integration-shaped tests against fully-mocked federation: define_cohort intersection, histogram_merges_two_cdrs, csv_export_filters_to_members + correct_header, scatter_truncates_above_cap)

Phase 4 surface is backend-only this commit. The frontend (§4.4 nurse
workspace UI / §4.5 researcher workspace UI) and Playwright E2E
suite are deferred — they need an actual running 5-CDR cluster to
exercise meaningfully, and that's gated on the §3.2 / §3.3 operator
actions on miserver.

26/26 new Phase 4 tests pass. 38/49 total in the dashboard test suite;
the 11 pre-existing failures stem from the legacy ObservationCache
model being JSONB+UUID-typed, which SQLite's create_all() can't
reproduce — not a Phase 4 regression.

# 2026-04-26 — Phase 4 frontend (§4.4 nurse + §4.5 researcher workspaces)

- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/workspace.py (NEW — workspace selector + nurse + researcher HTML routes; route guards via the same role-guard helpers as the JSON API)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/__init__.py (registered workspace_bp)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/workspace_selector.html (NEW — chooser; sole-role users redirected, dual-role/admin shown both cards)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/nurse_workspace.html (NEW — patient-GUID search + recent strip, AGP card with hourly bands chart and TIR/TBR/TAR/mean/CV/hypo summary, variable-switcher chart with LTTB display, latest-values table, events feed; Chart.js + fetch against /api/nurse/*)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/researcher_workspace.html (NEW — left rail filters: 5 CDR multi-select, age min/max + sex, condition pills, medication pills; cohort builder POSTs /api/cohort; chart pane with Histogram / Box-Violin / Scatter (auto-disabled until 2 vars) / Trend tabs; right rail summary stats + per-CDR breakdown + CSV export download via hidden iframe; partial-result banner driven by fanout_mode)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_workspace.py (NEW — 9 tests: selector redirects single-role / shows chooser dual-role / admin / blocks no-clinical-role; nurse + researcher pages render their key markers + canonical URIs; cross-role page guards return 403)

35/35 Phase 4 tests now pass (16 federation + 6 role guards + 4
researcher flow + 9 workspace). 47/58 total (47 = 12 pre-existing
green + 35 Phase 4); same 11 pre-existing JSONB-on-SQLite failures.

## 2026-04-28 — Workspace nav links + F2 GUID rebase
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/base.html — added top-nav links for Workspace / Nurse / Researcher so users land on the chooser (or their default workspace) without typing the URL. Deployed via `start.sh` restart on miserver 2026-04-28T22:14Z.
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/e2e/specs/nurse_flow.spec.ts — `CGM_PATIENT` default rebased from the stale `04bbc029-…` (purged in the SI conversion) to `e57da193-bfe9-5015-8062-c4d5fd8bf5f6` (the seed's CGM patient on cdr2, ~26k CGM-raw obs). Will only round-trip cleanly after the cdr.pdhc writer fix + reseed land — see the parallel cdr.pdhc/changed_files.md entry.

## 2026-04-29 — CGM LOINC code mismatch surfaced by termbank load (followup)

Termbank.pdhc loaded LOINC 2.82 today. Cross-referencing the codes
PDHC's CGM pipeline uses:

- 97506-0 → "Glucose management indicator" (GMI) — CORRECT
- 97507-8 → "Average glucose [Mass/volume] in Interstitial fluid during Reporting Period" — CORRECT
- 97509-4 → **LOINC says "Cancer disease progression"** — sim/dashboard treat it as a CGM time-in-range / time-above-range marker. **Wrong code.**
- 97511-0 → **LOINC says "Fungal Ab panel - Specimen by Immune diffusion (ID)"** — sim/dashboard treat it as another CGM range marker. **Wrong code.**

Real CGM Time-In-Range codes from the LOINC family typically used:
- 97509-4 is wrong. Correct CGM TIR (% of time 70–180 mg/dL or
  3.9–10.0 mmol/L) is **97510-2** "Time in target range".
- TBR (time below range): **97509-4** is wrong; the right codes are
  **97511-0** is also wrong. Real ones: **TBR Level 1** (54-69
  mg/dL) is `97515-1`, **TBR Level 2** (<54) is `97514-4`.
- TAR (time above): `97508-6` (level 1, 181-250) and `97506-0` is GMI
  not TAR — TAR codes are `97507-8`-adjacent.

Action: review every reference to 97509-4, 97511-0 in
sim.pdhc/sim/variables/cgm.py, dashboard.pdhc/app/services/federation.py,
the seeded test fixtures, and replace with the real CGM TIR/TBR/TAR
codes. NOT done in this commit — surfacing the bug only.

Discovery via: termbank.pdhc deploy + LOINC 2.82 import on 2026-04-29.

## 2026-04-30 — CGM LOINC fix landed

Replaced the wrong CGM codes with verified LOINC 2.82 codes (each
verified by `curl http://127.0.0.1:9012/CodeSystem/loinc/<code>` on
miserver against the loaded termbank). Mapping:

- TIR (CGM): `97509-4` → **`97510-2`** ("Glucose measurements in range
  out of Total glucose measurements during reporting period")
- Hypoglycemia / severe hypo events: `97511-0` → **`104642-4`**
  ("Time below range, very low" — <3.0 mmol/L; matches sim.pdhc's
  `cgm_hypo_count` concept)
- CGM mean (researcher workspace): `97506-0` → **`97507-8`**
  ("Average glucose [Mass/volume] in Interstitial fluid during
  Reporting Period"); `97506-0` (GMI) split out as its own variable.

Files:

- `app/templates/nurse_workspace.html` — TIR canonical
- `app/templates/researcher_workspace.html` — TIR canonical, CGM mean
  canonical, added GMI as separate variable
- `app/routes/nurse.py` — `hypo_canonical` now `104642-4` with a
  comment explaining what the code means and how it lines up with
  `cgm_hypo_count` in sim.pdhc

Deployed via `scp` of the three files into
`/usr/local/www/dashboard.pdhc/current/` and a manual gunicorn
restart (start.sh failed at the docker-context check because the
mac-side colima socket forward is broken right now; the dashboard
app itself talks to its DB on `localhost:9026`, no docker call
needed). Verified `https://dashboard.pdhc.se/healthz` 200.

- 2026-06-09 (#212 Dashboard PDL #2 — admin off-org bypass becomes audited lift):
  - app/migrations/versions/2026_06_09_dashboard_audit_admin_override.py (NEW, rev `a8bc21200001` ← `e21404aa01`):
    adds `event_type VARCHAR(32) NOT NULL DEFAULT 'read'` + index +
    `admin_justification TEXT NULL` to `dashboard_audit`.
  - app/models/__init__.py: DashboardAudit gains `event_type` and `admin_justification` columns.
  - app/services/audit.py: `_write_audit_row` reads `g._audit_event_type` /
    `g._audit_admin_justification` (set by the patient view); defaults to `read` / NULL.
  - app/routes/views.py: `/patient/<guid>` detects off-org admin reads
    (admin's `organization_ids` does not overlap the patient's distinct
    `org_guid` set in ObservationCache), gates them behind a written
    justification, and emits a distinct audit-row shape:
      * `event_type='admin_override_required'` when admin off-org with no
        justification (form rendered, no data leaked, n_rows=0)
      * `event_type='admin_override'` + `admin_justification=<verbatim>`
        when admin proceeds.
    Non-admin off-org reads continue to hit the existing 404/empty path.
  - app/templates/admin_override_required.html (NEW): Swedish-first
    confirmation form (PDL Ch 4 § 1 / Lag 2022:913 § 2 framing).
  - app/tests/test_admin_override_audit.py (NEW, 10 tests): decorator
    column propagation + off-org detection + form rendering + 'admin_override'
    audit row + whitespace-only-justification handling + non-admin path
    + partial overlap (no lift) + no-data case.

- 2026-06-09 (#213 Dashboard PDL #3 — ObservationCache retention + admin scrub):
  - app/services/cache_retention.py (NEW): `sweep_expired_observations(ttl_hours)`
    drops rows by `fetched_at`; `scrub_observations(patient_guid?, org_guid?)`
    requires at least one filter, raises `ValueError` otherwise.
  - app/routes/admin.py (NEW): `POST /admin/cache/scrub` (SU-only, 403 otherwise,
    400 when no filter). Writes a `dashboard_audit` row with
    `event_type='cache_scrub'`, `admin_justification=<reason>`,
    `n_rows_returned=<deleted_count>`, `payload_snapshot={patient_guid, org_guid,
    reason, deleted_count}`. Also registers the `flask cache-sweep
    [--ttl-hours N] [--dry-run]` CLI.
  - app/services/audit.py: `_write_audit_row` now also honours
    `g._audit_payload_snapshot` (JSONB blob override) and
    `g._audit_patient_guid` (so body-driven routes like the scrub
    can override the URL-derived patient).
  - app/__init__.py: new config `OBSERVATION_CACHE_TTL_HOURS` (default 48
    hours, env-overridable); registers admin blueprint + CLI.
  - docs/technical.md: new "ObservationCache retention" section
    (cron snippet + endpoint shape), new endpoint row, new env var row.
  - readme.md: §15.a cross-link to the retention section.
  - app/tests/test_cache_retention.py (NEW, 14 tests): sweep TTL-clamp +
    rollback; scrub filter requirement + patient/org/both predicates +
    rollback; admin route 403/400/200/500 + audit-row shape (event_type,
    patient_guid override, payload_snapshot, n_rows, justification);
    whitespace-only filters treated as missing.
