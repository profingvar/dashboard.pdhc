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

- 2026-06-09 (#215 Dashboard PDL #5 — /admin/audit operator view):
  - app/routes/admin.py: added `GET /admin/audit` (SU-only, 403 otherwise)
    with date-range + user_guid + patient_guid + route + event_type
    filters, 100-row pages; and `GET /admin/audit/export.csv` (SU-only,
    50k-row cap, JSON-encodes list/dict cells). Each emits its own
    `dashboard_audit` row with `event_type='admin_audit_view'` /
    `'admin_audit_export'` so audit-of-the-audit is itself searchable.
  - app/templates/admin_audit.html (NEW): filter form + table + pager.
    Coloured badges per event_type, payload_snapshot in <details>,
    Swedish-first kontroll framing.
  - docs/technical.md: two new endpoint rows.
  - app/tests/test_admin_audit_view.py (NEW, 12 tests): SU-auth (view +
    export), filter construction (blank → no predicates, all six →
    six predicates, unparseable date → skipped, whitespace → skipped),
    pagination clamp, row rendering, audit-of-audit row shape, CSV
    header + content + filter-preserved payload + JSON-encoded cells.

## 2026-07-06 — M0 #415 (partial): affiliations[] Zone-1 scope + session_phases
- app/auth.py — scope_org_guids() (affiliations[].care_unit_guid, dual-read
  fallback) wired into _blob_to_user.org_ids; _phases() (session_phases w/
  fallback) into has_analysis_access.
- app/routes/nurse.py — _auth_headers() uses scope_org_guids(blob).
- app/tests/test_reform_scope.py — NEW, 8 tests.
- DEFERRED (ticket stays open): drop synthetic roles[] service-key hack;
  EHDS opt-out + research-consent joins on federated reads (analysis
  enforcement, overlaps #422). Note: test_auth.py has 6 PRE-EXISTING failures.
2026-07-13T18:51:50Z docs/redesign_462_decisions.md — #462 redesign locked decisions (Q1 legal + Q6 cache still open)
2026-07-13T19:00:58Z app/models/__init__.py — add SavedDesign model (#467)
2026-07-13T19:00:58Z app/migrations/versions/2026_07_13_saved_design.py — new saved_design table (#467)
2026-07-13T19:00:58Z app/routes/designs.py — new; SavedDesign CRUD, owner-scoped (#467)
2026-07-13T19:00:58Z app/routes/picker.py — new; clinical patient picker /select (#465)
2026-07-13T19:00:58Z app/services/cdr1_client.py — new; CDR1 care-delivery read client (#465)
2026-07-13T19:00:58Z app/templates/select.html — new; patient picker UI (#465)
2026-07-13T19:00:58Z app/__init__.py — register designs+picker blueprints, CDR1_BASE_URL config (#465/#467)
2026-07-13T19:00:58Z .env.example — add CDR1_BASE_URL + DASHBOARD_PDHC_SERVICE_KEY (#465)
2026-07-13T19:00:58Z app/tests/test_saved_design.py — new; CRUD+owner isolation (#467)
2026-07-13T19:00:58Z app/tests/test_patient_picker.py — new; bundle parse + /select route (#465)
2026-07-13T20:34:56Z app/services/cdr1_client.py — rewire list_org_patients to CDR1 /api/v1/clinical/patients + add patient_summary (#465/#468)
2026-07-13T20:34:56Z app/templates/select.html — add Data points column (#465)
2026-07-13T20:34:56Z app/tests/test_patient_picker.py — add parse_clinical_patients test (#465)
2026-07-13T20:44:50Z app/auth.py — #463 route-aware SSO gate: clinical routes → care-delivery, analyse routes keep analysis phase; add has_care_delivery_access + _is_clinical_path
2026-07-13T20:44:50Z app/tests/test_care_access_auth.py — new; #463 care-delivery gate unit + SSO integration (10 tests)
2026-07-13T20:44:50Z docs/redesign_462_decisions.md — #463 D1 split inventory (stays vs relocates) + auth-change spec
2026-07-13T20:56:03Z app/services/cdr1_client.py — add patient_series() + params support in _get_json (#464)
2026-07-13T20:56:03Z app/routes/charts.py — new; /patient/<guid>/charts page + /api/v1/patient/<guid>/{parameters,series} proxies with spärr (#464/#466)
2026-07-13T20:56:03Z app/templates/charts.html — new; #466 charting UI (≤3 diagrams, dual-axis mirror, continuous time slider, zero/y-max, save/load designs)
2026-07-13T20:56:03Z app/__init__.py — register charts blueprint (#464/#466)
2026-07-13T20:56:03Z app/auth.py — add /api/v1/patient/ to clinical paths (#464)
2026-07-13T20:56:03Z app/templates/select.html — picker links to charts.charts_page (#464)
2026-07-13T20:56:03Z app/tests/test_patient_charts.py — new; proxy endpoints + spärr + page (#464)
2026-07-13T21:03:36Z app/static/vendor/chart.umd.min.js — new; vendored Chart.js 4.4.6 UMD (#471 item 3)
2026-07-13T21:03:36Z app/static/vendor/chartjs-adapter-date-fns.bundle.min.js — new; vendored date-fns time adapter v3.0.0 (#471 item 3)
2026-07-13T21:03:36Z app/templates/base.html — reference vendored Chart.js instead of cdn.jsdelivr (#471 item 3)
2026-07-15T07:13:18Z app/templates/charts.html — use plan.pdhc display names in dropdown + chart legend (#471 item 5)
2026-07-16T11:39:39Z app/routes/views.py — retire ObservationCache landing/patient views; '/'->/select, '/patient/<guid>'->/charts redirects; #212 flow removed (#471.1)
2026-07-16T11:39:39Z app/routes/api.py — remove /api/v1/series; keep minimal /metadata (#471.1)
2026-07-16T11:39:39Z app/routes/admin.py — remove cache-scrub route + cache-sweep CLI; keep /admin/audit (#471.1)
2026-07-16T11:39:39Z app/__init__.py — drop OBSERVATION_CACHE_TTL_HOURS, api blueprint, cache-sweep CLI (#471.1)
2026-07-16T11:39:39Z app/templates/base.html — remove 'Refresh from gateway' button (#471.1)
2026-07-16T11:39:39Z app/services/gateway_client.py — DELETED (#471.1)
2026-07-16T11:39:39Z app/services/cache_retention.py — DELETED (#471.1)
2026-07-16T11:39:39Z app/templates/{landing,patient,admin_override_required}.html — DELETED (#471.1)
2026-07-16T11:39:39Z app/tests/{test_gateway_client,test_cache_retention,test_admin_override_audit}.py — DELETED (#471.1)
2026-07-16T11:39:39Z app/tests/{test_views_api,test_blocks_filter,test_auth,test_session_propagation}.py — updated for retirement (#471.1)
2026-07-19T19:24:33Z app/models/__init__.py — remove ObservationCache + RefreshLog models (#471 table drop)
2026-07-19T19:24:33Z app/migrations/versions/2026_07_19_drop_cache_tables.py — new; drop observation_cache + refresh_log (rev drop0719cache01)
2026-07-19T19:24:33Z app/auth.py — remove unused scope_to_user_orgs; fix stale Rule24 + _upsert_local_user docstrings (#471)
2026-07-19T19:24:33Z app/routes/views.py — docstring: tables now dropped (#471)
2026-07-19T19:24:33Z app/tests/{test_models,test_auth,test_blocks_filter}.py — drop ObservationCache/RefreshLog/scope_to_user_orgs coverage (#471)
2026-08-07T17:50:24Z app/routes/nurse.py — #546 nurse-fold: care-delivery re-gate + per-patient spärr (coarse + #471.4 lift + sparr_lift_exposure audit), mirrors charts.py::series
2026-08-07T17:50:24Z app/auth.py — #546: add /api/nurse/ to _is_clinical_path (nurse views = care-delivery, not analysis-phase)
2026-08-07T17:50:24Z app/tests/test_nurse_sparr.py — new; nurse spärr + care-delivery gate coverage (9 tests)
2026-08-07T17:50:24Z app/tests/{test_role_guards,test_analysis_consent,test_care_access_auth}.py — updated to the #546 model (nurse enforced at app-level care-delivery gate, not per-route role guard)
2026-08-08T09:41:46Z app/routes/researcher.py — DELETED (#543 group/analyse-engine extracted to analyse.pdhc; owned /api/cohort* + register_export_audit_cli)
2026-08-08T09:41:46Z app/analyse/cohort.py — DELETED (#543 cohort predicate builder → analyse.pdhc)
2026-08-08T09:41:46Z app/analyse/observations_search.py — DELETED (#543 → analyse.pdhc)
2026-08-08T09:41:46Z app/analyse/stats.py — DELETED (#543 → analyse.pdhc)
2026-08-08T09:41:46Z app/analyse/canonical.py — DELETED (#543 → analyse.pdhc)
2026-08-08T09:41:46Z app/analyse/openehr.py — DELETED (#543 → analyse.pdhc)
2026-08-08T09:41:46Z app/__init__.py — #543: drop imports + register_blueprint for researcher_bp/observations_search_bp/analyse_stats_bp/analyse_canonical_bp/analyse_openehr_bp and register_export_audit_cli; kept auth/views/nurse/workspace/designs/picker/charts/admin/metadata + all config (CDR_ENDPOINTS, DASHBOARD_PDHC_SERVICE_KEY, CDR1_BASE_URL)
2026-08-08T09:41:46Z app/routes/workspace.py — #543: trim to nurse-only; removed /researcher shell + role-selector branching; /workspace now redirects straight to /nurse
2026-08-08T09:41:46Z app/services/role_guards.py — #543: remove now-unused researcher_required; kept nurse_required + admin_required (+ _roles/_is_admin)
2026-08-08T09:41:46Z app/tests/test_researcher_flow.py — DELETED (#543 covered removed researcher /api/cohort engine only)
2026-08-08T09:41:46Z app/tests/test_observations_search.py — DELETED (#543 covered removed module only)
2026-08-08T09:41:46Z app/tests/test_analyse_aux.py — DELETED (#543 covered removed stats/canonical/openehr only)
2026-08-08T09:41:46Z app/tests/test_export_audit_to_db.py — DELETED (#543 covered removed researcher export-audit only)
2026-08-08T09:41:46Z app/tests/test_role_guards.py — #543: dropped the /api/cohort researcher cases; kept nurse care-delivery + admin coverage
2026-08-08T09:41:46Z app/tests/test_workspace.py — #543: dropped researcher-shell + chooser cases; /workspace now asserts nurse redirect (nurse-only/dual-role/admin all → /nurse)
2026-08-08T09:41:46Z app/tests/test_care_access_auth.py — #543: dropped /api/cohort/build + /api/v1/observations from the _is_clinical_path non-clinical tuple; kept care-delivery/gate coverage
2026-08-08T09:41:46Z app/tests/test_x1_tuple.py — #543: dropped the POST /api/cohort route-class assertion; kept care + admin route-class coverage
2026-08-08T09:41:46Z app/tests/test_analysis_consent.py — #543: dropped /api/cohort role-derivation route calls (converted to direct role_guards._roles() checks) + removed the 3 _apply_research_consent tests (fn lived in deleted researcher.py); kept IpsClient.analysis_filter + research_project_guids + service-blob care-delivery coverage
2026-08-08T09:41:46Z app/tests/test_federation.py — #543: removed the app.analyse.cohort import + 3 cohort-predicate tests; kept all federation (fanout/histogram/lttb/agp) tests
2026-08-08T09:57:27Z app/auth.py — nurse-page gate: /nurse + /workspace are care-delivery clinical paths (a pure care-delivery nurse can open the UI)
2026-08-08T09:57:27Z app/templates/base.html — rebrand user-facing to cd-assist (title/brand/h1); remove dead Researcher nav link (#543)
2026-08-08T09:57:27Z app/tests/test_care_access_auth.py — nurse workspace page now care-delivery; care-only reaches it
2026-08-08T10:34:21Z app/routes/auth.py — SSO callback admits care-delivery OR analysis (care-only nurse can log in #546); logged-out title -> cd-assist
2026-08-08T10:34:21Z app/tests/test_care_access_auth.py — callback admits care-only / rejects no-access
- dashboard.pdhc/docs/technical.md (Port Allocation section)

## 2026-09-03 — #578/#579 (dashboard = individual level): choose-patient list + nav per design
- app/templates/base.html — nav: PDHC/services · choose patient (→picker) · Dashboard (was Nurse) · Spärrlogg (admin → /admin/audit)
- app/templates/select.html — "Choose patient": Namn · Födelseår · Patient-GUID (full guid on mouseover) · Datapunkter · Senaste data (last_observed_at) + Uppdatera button; whole row → charts (Dashboard)
- app/templates/nurse_workspace.html — "Nurse workspace" → "Dashboard"

## 2026-10-01 — #729: empty KNOWN_SERVICES + gate the service-key branch
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/auth.py — KNOWN_SERVICES now empty (gateway.pdhc dead since #540, verified against the live gateway container); install_request_loader applies _dashboard_access_allowed on the service-key branch instead of returning before it.
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/__init__.py — GATEWAY_PDHC_SERVICE_KEY no longer read into config.
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_service_key_gate.py — NEW, 7 tests through the real loader. Two verified to fail against a reverted fix.
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_analysis_consent.py — corrected a comment that claimed the app-level gate kept a service blob off clinical routes; it asserted the predicate on a path where the predicate was never consulted.
- 2026-10-13 app/__init__.py — #816: SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE

## 2026-10-15 — #827 two resolution paths for one deployment fact
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/__init__.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/ips_client.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_sparr_unconfigured.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/charts.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/nurse.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/charts.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_sparr_banner_distinguishes.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/progress.md
/Users/martiningvar/T7_sidewinder/dashboard.pdhc/.env.example  (#830: CDR_ENDPOINTS + SPARR_LIFT_ENABLED were DEPLOYED-ONLY)

## Patient dashboard — Gate 0 and Phase 1 foundations (2026-11-11)

Against the operator's plan at `pdhc/docs/PDHC_Patient_Dashboard_Coding_Plan.md`.
Gate 0 report: `plans/dashboard_sources.md`. Tickets #979 (rollup) and the
per-phase set; #981, #982 filed from findings below.

### THE ANSWER TO "should we rebuild?" — NO, EXTEND

The plan's hardest step is already built here: care/SU basis, roles, spärr and
the PDL log, with nine test modules. The plan's own wording assumes it
("goes through the existing SSO access-blob check"). A rebuild would discard
it and then recreate it from a document that assumes it exists.

### THE INTERACTION MODEL CHANGED: selection, not hover

Hover is wrong as the MECHANISM for a UI that must serve an iPad. iPadOS
synthesises a hover on first tap (two-tap reveal-then-activate) and a
trackpad on the SAME device restores real hover, so the capability is not a
property of the device and changes at runtime. The plan admits the problem
twice without naming it: "hovering away keeps the last series shown", and the
whole PIN feature exists only to work around hover's transience.

Measured: this module already chose click — 11 `click`, 1 `mouseenter`,
**0 `mouseover`**. So the plan's hover-first design was a departure from what
exists. Selection makes the detail persistent, makes keyboard parity free, and
DELETES the pin.

### THE JOIN: the plan's premise was wrong in the dangerous direction

The plan says `observation` -> `clinical_context` "joins 0 rows". Measured on
cdr1: it joins **446 439** from 1 949 observations.

    transaction_guid              min 32, max 251, avg 229.1 context rows each
    transaction_guid + patient    avg 76.0
    ingest_raw_guid               min 1, max 1, avg 1.0

A transaction is a BATCH — 10 of them carry 1 949 observations. The key
everyone reaches for would silently multiply the event list. **Join on
`ingest_raw_guid`.**

Coverage: `care_plan_guid` 0/1949 on BOTH tables (so T+ has no origin for any
row today), `plan_activity_guid` 1942/1949, `service_request_guid` 7/1949.
Consequence the plan missed: step 15 says to match a plan item to its
observations by `transaction_guid`, which would highlight ~195 rows per item.

### WHAT WAS ALREADY BUILT, that the plan thought might not be

euIPS in ips.pdhc (#793/#795) — but `required_sections_for()` takes an `rng`
and is called only from `mock_generator.py`: it GENERATES for synthetic
patients. The read path is the frozen snapshot. The operator chose live
composition, so that is new work in ips.pdhc (#980).

The ICD-11 register in icd11.pdhc is richer than the plan's DTO, and
`GET /patients/<guid>/diagnosis-summary` already returns a FHIR Problem List
bundle with `as_of` time travel and a `urn:pdhc:scope` tag distinguishing "no
diagnoses" from "none you may see". **termbank's ICD-11 load (#965) is OFF the
critical path**: `DiagnosisCode.label` carries the text.

### FOUR DEFECTS IN MY OWN WORK, each caught by verifying an assumption

1. `audit_read` does NOT read `g._audit_detail`. The real hook is
   `g._audit_payload_snapshot`. The access basis would have been written
   nowhere, and every test would still have passed.
2. `g.user` does not exist; the accessor is `g.current_user`, and the
   canonical org source is `scope_org_guids(blob)`.
3. **`access_basis` IS A CLOSED ENUM** (`plans/pdhc_data_shapes.md` §5), four
   services populate it, and a route's values WIN over the X1 defaults.
   Writing the plan's words — "care" / "secondary-use" — would have corrupted
   it. They are MAPPED instead.
4. **One of my own tests was a dud.** The mutation it claimed to guard changed
   nothing, because its blob already had a care unit. Tracing why showed the
   "hole" was not one: `has_care_delivery_access` already requires a non-empty
   org scope for a non-admin, so the care basis IMPLIES orgs. The guard moved
   from a dead branch to the COUPLING, which does bite.

And a real defect found in the audit: **X1's `purpose` is ROUTE-classed**, so
a secondary-use caller reading this dashboard would be logged as a care read.
§5's own derivation maps SU to `administration`; that is now set.

### HbA1c: ONE CHART, via a NAMED AFFINE FUNCTION (operator, 2026-11-11)

`%` and `mmol/mol` are numerically a pure ratio, 1 mmol/mol = 0.1 %. For
HbA1c that ratio is clinically wrong by ~32 % across the whole range:

      5.0 %  ratio 50  truth 31        8.0 %  ratio 80  truth 64
      7.0 %  ratio 70  truth 53        9.0 %  ratio 90  truth 75

`app/dashboard/hba1c.py` holds the NGSP master equation
`NGSP% = 0.09148*IFCC + 2.152`, defined ONCE with the inverse derived, so the
two directions cannot drift — the commonly quoted rounded form (2.15/10.929)
disagrees with itself in the third significant figure.

**`convert()` still refuses the pair and must.** The bridge is a separate
named path that only `group_series` takes. The distinction is not pedantry:
"are these units interconvertible?" is still NO; "for this substance is there
a named function?" is the narrower question. THE LESSON THAT GENERALISES:
knowing the substance is not sufficient — the relation must ALSO be
multiplicative. The molar-mass bridge qualifies; this one does not.

Below 2.152 % is refused BY NAME, not clamped — a clamp turns an impossible
reading into a plausible one. There is deliberately NO upper bound: a chart
that quietly omits the worst point is worse than one that shows it.

### hammer.js WAS NEEDED, AND ITS ABSENCE WOULD HAVE BEEN SILENT

`chartjs-plugin-zoom` reads `Hammer` from the global at script-load time and
calls gesture setup as `n && $(t, a)`. With no global, setup is SKIPPED —
nothing throws, nothing logs. Pinch-to-zoom and touch panning would never
work on an iPad while wheel-zoom and drag-zoom kept working on a desktop.
**A desktop test pass would have reported everything fine.** Load order
matters: hammer before the plugin.

### FINDINGS FILED ELSEWHERE

**#981 (request.pdhc)** — `list_care_plans` filters on `patient_guid` and
`status` only: no organisation scope, no spärr, while the ServiceRequest
routes in the SAME service do both. Verified from source with a control
before filing, because a wrong security finding is worse than none (#844/#845).

**#982** — `create_app` loads four `*_BASE_URL` keys and neither
`PLAN_BASE_URL` nor `REQUEST_BASE_URL`. Found twice, independently.

Also measured: **`ServiceRequest.care_plan_guid` never reaches the wire** (the
column exists since #310; `to_dict()` omits it and nothing writes it), so
there is no API-visible edge from a ServiceRequest to a CarePlan. There is no
`completed` SR status either — `archived` IS the done signal.

### INSTRUMENT FAILURES, because there were many and they are the lesson

| what lied | why |
|---|---|
| `for f in $FILES` deployed nothing | zsh does not word-split an unquoted variable |
| `tar extract exit 0` | it packed an EMPTY directory; real success, no meaning |
| a mutation harness reporting 5/5 "correct" | `pytest "a.py b.py"` → **exit 4**, a usage error. The tests never ran. The BASELINE caught it |
| one mutation reporting a failure | **exit 2**, a collection error — my edit left an `except` with no body |
| `shasum` inside the container | not installed; everything reported ABSENT, including `Dockerfile` |
| a VERSIONS.md hash check reporting MISMATCH | my regex paired names and hashes across a 12-line window; per-row parsing gives 4/4 MATCH |

**Exit 1 is an assertion that failed. Exit 2 and 4 are the instrument not
running.** Only the first is evidence.

### FILES

New: `app/dashboard/{__init__,dto,access,timing,units,hba1c}.py`,
`app/services/{plan_client,request_client}.py`,
`app/static/vendor/{chartjs-plugin-zoom.umd.min.js,hammer.min.js,VERSIONS.md}`,
`app/static/js/capability.js`, `plans/dashboard_sources.md`, and six test
modules. **No existing file was edited** — `app/auth.py` and
`app/services/audit.py` are byte-identical to HEAD.

460 tests pass, was 196.

## The dashboard BFF: two endpoints built, two blocked by measurement (2026-11-11)

### WHAT WAS BUILT

    GET /dashboard/patients/<guid>                   card + plans
    GET /dashboard/patients/<guid>/series/<code>      grouped, spärr-filtered
    GET /dashboard/patients/<guid>/events            501, named, #983
    GET /dashboard/events/<observation_guid>         501, named, #983

The last two are blocked by an UPSTREAM PROJECTION GAP, not by effort. CDR1's
clinical series selects six columns — code, at, value, unit, value_string,
org_guid — and omits the observation's own guid, `transaction_guid` and
`ingest_raw_guid`. The columns EXIST and are populated (`transaction_guid` on
1 949 of 1 949 rows measured on cdr1). Without a row identity there is no
cursor tiebreak, no selection and nothing to look a detail up by; without the
transaction keys there is no path to `clinical_context`, which is the whole
content of the plan's step 18. Filed as #983.

They answer 501 WITH A NAMED REASON rather than an empty list. A half-built
event list that looks like it works until somebody clicks a row is worse than
an honest refusal, and the plan's own principle — show what is missing rather
than omit it — applies to the API as much as to the UI. A mutation that makes
them return `200 {"rows": []}` fails a test.

### THE GATE WAS WIDENED, AND THAT IS A SECURITY EDIT

`app/auth.py` gained `_is_patient_dashboard_path`, and `/dashboard/` now
admits care-delivery OR the analysis phase. Every other clinical path is
care-delivery ONLY; these are not, because the plan allows exactly two bases
and a secondary-use caller would otherwise be refused at the door before
`access.py` ever ran.

The split is: the gate decides "may you knock", `app/dashboard/access.py`
decides "may you enter", refuses by a named code, and records which basis
applied on the PDL row. Widening the door without that layer would be a real
loosening.

**THE CONTROL IS THE POINT.** `test_NO_OTHER_PATH_CHANGED_by_the_widening`
walks eight existing clinical paths and two analyse paths and asserts each
still admits exactly who it admitted before. Mutating the new predicate to
`return True` fails it. A one-branch widening is only safe if it is one
branch, and that is checkable rather than arguable.

### THREE DEFECTS FOUND BY USING AND VERIFYING, NOT BY TESTS

1. **`access.py` could not say "could not ask".** `patient_org_guids=None`
   meant "asked, no assignment"; there was no way to express "the lookup
   failed", so an outage would have reported every patient as unassigned.
   `UNKNOWN_ORGS` now refuses with 503 `undetermined` instead of 403
   `other_organisation`. **This is #827's shape a THIRD time, in code written
   by someone who had just fixed it twice elsewhere** — which is the most
   useful thing to record about it.

2. **The route discarded a required audit detail.** `filter_blocked_points`
   returns `(kept, exposures)` and the second half carries #472's obligation:
   an indispensable-care LIFT that exposed a blocked point must be audited,
   as `charts.py` does under `sparr_lift`. My first version took `[0]` and
   dropped it. Silently losing an obligatory audit detail makes the trail
   UNTRUE, not merely thin.

3. **The wrong client signature.** The route called
   `icd11_client.diagnoses_for(guid)`, which does not exist; the real API is
   `build_client().problem_list(guid).patient_card_fields()`. Caught by
   reading the delivered module rather than by a test, because the route's
   own `except Exception` would have swallowed the AttributeError into
   "icd11 unavailable" and the card would have shown no chips, plausibly,
   forever.

### THE DIAGNOSIS DISTINCTION IS THREE-WAY AND THE BFF IS WHERE IT DIES

An empty chip list means one of three things: the patient has no diagnoses;
none that this reader is scoped to see; or icd11 could not be reached.
icd11 tells the first two apart with a `urn:pdhc:scope` tag and a
`scopeNote`, which exist because they were once indistinguishable and sent a
clinician hunting a bug that was not there (2026-10-23).

The route carries the note verbatim and a `diagnosesDefinitivelyEmpty` that
is **null** when icd11 failed. "Unknown" and "none" are different statements
about a patient, and a card that silently shows nothing because a service was
down asserts the second. Both mutations — dropping the note, and claiming
emptiness after a failure — fail a test.

### WHAT THE icd11 CLIENT MEASURED, that changed the route

- An "empty" bundle is **not an empty `entry` array**: icd11 emits one
  sentinel Condition (`absent-unknown-uv-ips#no-problem-info`, "Inga kända
  diagnoser") because IPS requires absence to be STATED. Two wrong ways to
  handle it — parse it as a diagnosis, or write it off as malformed — and
  both are tested against.
- `recordedDate`, `abatementDateTime`, `encounter_ref` and `foundation_uri`
  are NOT on this endpoint's projection. Not dropped by the client; never
  sent. `encounter_ref` is deliberate — icd11 keeps huvud/bidiagnos on the
  ENCOUNTER, since the same condition can be primary at one visit and
  secondary at another.
- The ICD-11 release travels with every coding (Rule 28 forbids a code
  without one) and `dto.Diagnosis` has no field for it. Parked on
  `ProblemList.releases` rather than dropped at the parser.
- **Three neighbours, three different doors**: icd11 takes
  `Authorization: Bearer` and has no machine door at all; request.pdhc takes
  `X-API-Key` and IGNORES `Authorization`; ips takes `Authorization` and
  ignores `X-API-Key` (#783). Nothing about this is guessable.

### CONFIG: a THIRD key is missing (#982)

`ICD11_BASE_URL` appears **nowhere** in this repo — not in `create_app`, not
in `.env.example` (control: `IPS_BASE_URL` appears twice in `create_app`).
With #982's `PLAN_BASE_URL` and `REQUEST_BASE_URL` that is three clients
whose only production path is the environment leg. Until an operator sets
them the clients refuse BY NAME, which is correct — but the card has no chips
and the plan panel is empty, and nobody is told why unless they read a log.

### TESTS

562 pass, was 460. New: `test_dashboard_routes.py` (13) and
`test_icd11_client.py` (85), plus 3 gate tests in `test_dashboard_access.py`.
Seven route-level mutations, each restored byte-identical; every one failed
the right test on exit code 1 — and two earlier mutation runs were THROWN
AWAY because they reported exit 4 (a pytest usage error from zsh not
word-splitting an unquoted variable) and exit 2 (a collection error from an
`except` block left without a body). **Exit 1 is an assertion that failed;
2 and 4 are the instrument not running.**

## #982 — every base URL a client reads is now loaded AND offered (2026-11-11)

Three keys were missing from `create_app` (PLAN, REQUEST, ICD11) and a
FOURTH was loaded but offered nowhere (IPS). Fixing four instances is not the
point; the point is that nothing noticed, so this closes the class.

THE DEFECT IS WRITTEN DOWN IN THE CODE IT GUARDS. `app/__init__.py`'s own
comment above `IPS_BASE_URL` records #827: the client read `os.environ`,
`create_app` loaded nothing, and `ips_client.is_configured()` — which asks
the CONFIG — answered "not configured" with the variable plainly set. One
resolution path for the client, a different one for the question "can we ask
at all?". Any client whose key is loaded nowhere has that waiting.

### The positive control earned its place immediately

The first collector watched only `os.environ` reads and silently missed
`CDR1_BASE_URL`, because `cdr1_client` resolves from `current_app.config`
with NO environment fallback. Four of five keys collected; both assertions
passed; the repo looked clean. `test_the_keys_we_expect_are_actually_FOUND`
is what reported `4 != 5`.

The lesson is the framing, not the fix: the question is not "where does a
client read from" but "what does it depend on". A key read from config is
exactly as missing from `create_app` as one read from the environment — more
so, because no fallback saves it.

### Asserted on the parse tree, for the usual reason

The comment explaining this guard names the very keys it checks, and
`.env.example` names `IPS_BASE_URL` in prose. A substring sweep would read
both as configuration — §7's shape, which this platform has now hit ten
times, twice inside instruments built to detect it. The collector also
resolves a module-level constant, because `icd11_client` holds its key as
`ENV_BASE_URL = "ICD11_BASE_URL"` and a literal-only matcher would miss it
and pass.

Mutations: removing PLAN from `create_app` fails the loaded test; removing
ICD11 from `.env.example` fails the offered test. Both restored.

### The operator half, done the same day

`/usr/local/www/dashboard.pdhc/shared/.env` gained the three absent keys.
Verified afterwards: all five present EXACTLY ONCE (IPS was already there —
appending the four as first written would have duplicated it), all five on
the public subdomain per #830, mode still 600, `current/.env` still a symlink
to `shared/.env`, and the original 22 keys intact.

Note for the deploy: the container must be RECREATED, not restarted —
`docker restart` keeps create-time env. Proven again the same day when
termbank's SECRET_KEY rotation left the running container reporting the old
value until `up -d`.

568 tests pass, was 562.

## The event list is built — #983 unblocked it (2026-11-11)

### app/services/cdr1_client.py — a STRICT events read, beside the old one

`patient_events()` reads cdr1's series newest-first with a cursor and RAISES
named errors instead of returning []: `Cdr1NotConfigured`, `Cdr1Unreachable`
(with status), `Cdr1BadCursor`, `Cdr1ProjectionTooOld`. "No events" and
"cdr1 did not answer" are different statements about a patient.

The existing `_get_json` collapses network errors, every 4xx, every 5xx and
non-JSON into `None`, and `charts.py` depends on `patient_series` returning
[] on failure. Both are left untouched — `_get_json_strict` sits beside them,
and a test pins that the old read still returns [] on a 500.

`Cdr1ProjectionTooOld` exists because #983 is committed in cdr.pdhc and NOT
deployed: production cdr1 still returns points with no `observation_guid`.
Rows built from that would render and could not be selected or looked up, so
the client refuses by name and the route answers `503
upstream_projection_too_old` citing #983. That is what the list will show
until cdr.pdhc is deployed — on purpose.

### app/routes/dashboard.py — GET /dashboard/patients/<guid>/events

Every failure answered by name; none becomes an empty page. Spärr exposures
AUDITED (#472). Display names in ONE batched call per page. The series badge
counts the patient's WHOLE history via cdr1's summary, not the page — a
page-local count would put "x2" on a concept with 300 points — and an empty
summary beside real points marks the badge unknown rather than zero. T+ is
computed only where a care plan is linked: 0 of 1 949 cdr1 rows today, so no
call is made, and a test proves request.pdhc is not touched. Limit capped at
500. cdr1's cursor passed through untouched, so the two ends never disagree
about its encoding.

### tests

`test_cdr1_events.py` (14) and `test_dashboard_events.py` (12). The old 501
test is narrowed to the detail route, which is still blocked. Six mutations,
each failing on assertions: pre-#983 projection accepted, unreachable as an
empty page, an N+1 name lookup, a page-local badge, exposures discarded, a
bad cursor as an empty page. 594 tests pass, was 568.

## The event detail is built — all four BFF endpoints exist (2026-11-11)

### app/routes/dashboard.py — GET /dashboard/events/<observation_guid>

The route carries no patient guid, so the order of operations IS the
security design, each step pinned by a mutation:

1. the caller's BASIS is checked before cdr1 is asked anything (a test counts
   the cdr1 calls — zero expected);
2. the observation is fetched scoped to the caller's organisations, and cdr1
   answers a flat 404 outside them;
3. the gate runs on the OBSERVATION'S OWN patient, never on anything the
   caller asserted — `g._audit_patient_guid` (#213) records it;
4. spärr: a blocked point gets the SAME 404 body as a missing one, so its
   existence is not disclosed.

Every cdr1 context field is returned verbatim, nulls included, beside the
DTO — the DTO has no slot for requesting/author org, plan activity or
requester user, and the panel's job is to show what was not recorded.
`patient_org_guid` is deliberately None, NOT filled from the provider: they
are different organisations with different meanings (#774).

The dead `_BLOCKED` 501 body and its test are removed; the module header now
says all four endpoints are built.

### app/services/cdr1_client.py — observation_detail, and two kinds of 404

cdr1's OWN "no such observation in your scope" is JSON `error: not_found`, an
answer. A route that does not exist is Flask's default HTML 404 — which is
what a cdr1 predating #983 returns for the detail route. Reading the second as
the first would make every detail view say "not found" until the deploy,
silently. It raises `Cdr1ProjectionTooOld` instead, and the route answers
`503 upstream_projection_too_old` citing #983.

### tests

`test_dashboard_detail.py` (10). Five mutations, each failing on assertions;
for M1 (cdr1 asked before the basis) the INTENDED test was confirmed among
the failures, since two others also fell as side effects. 603 tests pass.

NOT deployed — and in production the event list and the detail answer 503 by
name until cdr.pdhc is deployed.

## The card shows the live EPS (#980) and both diagnosis registers, labelled (#984) — 2026-11-11

### app/services/ips_client.py — IpsClient.euips_summary

Reads ips's new `GET /api/v1/patients/<guid>/euips-summary`. Follows the
module's own convention (`IpsUnreachable`, the operator's bearer — ips reads
only `Authorization`, #783). A 404 is an answer (unknown or out of scope, the
same body by ips's design); ips's `503 scope_undetermined` RAISES, so "could
not check" never reaches the card as "nothing to show". A test proves the
method landed ON `IpsClient` — it was inserted by indentation before the next
class.

### app/routes/dashboard.py — the card

The `eps:live-composition-pending-980` marker is gone; the card carries the
live euIPS sections. EXPLICITLY_ABSENT keeps its assertion text ("no known
allergies") and flag; MISSING has neither — a clinical statement and the
absence of one are different on the card too.

TWO REGISTERS, SIDE BY SIDE, NEVER MERGED (operator decision 2026-11-11). The
chips are icd11's ICD-11 register; the EPS problems section is ips's SNOMED
CT Conditions. Different code systems, populated independently, and where
both exist they disagree for 3 of 4 patients measured (#984). `diagnosisSources`
lists both with label, system, availability and count, and
`diagnosisSourcesReconciled: false` says plainly that nobody has reconciled
them. Each source degrades only itself.

### tests

`test_dashboard_card.py` (12). Six mutations, each failing on assertions:
merge the registers, claim them reconciled, silence an unreachable ips, read
503 as not-found, drop source/system, treat absent as missing. 615 pass.
NOT deployed (ips.pdhc's route is committed as e1d8ac5, also not deployed).

## 2026-11-12 — #976 Phase 2 slice 1: page shell, grid, patient card
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/dashboard.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/base.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/dashboard.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/css/dashboard.css
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/js/dashboard.js
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_page.py

## 2026-11-12 — #971 Phase 3: plan panel from ServiceRequests (production has 0 CarePlans)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/dashboard/dto.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/request_client.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/dashboard.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/dashboard.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/css/dashboard.css
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/js/dashboard.js
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_request_client.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_plan.py

## 2026-11-12 — #977 Phase 4 part A: event list, selection, detail, plan link
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/dashboard.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/dashboard.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/css/dashboard.css
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/js/dashboard.js
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_page.py

## 2026-11-12 — #978 Phase 5: series panel (strict read, LTTB, CSV, chart on selection)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/dashboard.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/dashboard.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/css/dashboard.css
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/js/dashboard.js
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_routes.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_series.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_page.py

## 2026-11-12 — #977 part B: filters in the URL, zoom refinement
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/services/cdr1_client.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/dashboard.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/dashboard.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/css/dashboard.css
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/js/dashboard.js
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_filters.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_page.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_routes.py

## 2026-11-12 — limits on values (operator: MDR class IIa)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/dashboard/limits.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/dashboard/dto.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/dashboard.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/templates/dashboard.html
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/css/dashboard.css
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/js/dashboard.js
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_limits.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_limit_routes.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_page.py

## 2026-11-12 — limits: requirements, traceability and risk notes (MDR IIa)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/docs/limits_requirements_and_risks.md
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_limits_traceability.py

## 2026-11-12 — limits come from the patient's CarePlan (goal, else PlanDefinition range)
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/dashboard/limits.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/routes/dashboard.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/static/js/dashboard.js
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_limits.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_limit_routes.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/app/tests/test_dashboard_page.py
- /Users/martiningvar/T7_sidewinder/dashboard.pdhc/docs/limits_requirements_and_risks.md
