# changed_files.md

All edited files (full path) from now on (Rule 17).

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
