# progress.md

Tracks progress against readme.md. Same numbering. Each step records tests deployed and results.

## 1. Repo scaffolding — done
- 1.a–1.c directories, seed files, git init ✓
- 1.d test_scaffold.py: 2/2 passed

## 2. venv + dependencies — done
- 2.a–2.c app/.venv created, requirements.txt installed
- 2.d import check ok (via pytest run)

## 3. Postgres on 9026 — done
- 3.a docker-compose.yml (postgres:16, named volume)
- 3.b .env from .env.example
- 3.c container up, pg_isready ok, psycopg2 connect ok

## 4. Flask app skeleton — done
- 4.a create_app() reading env
- 4.b /healthz → {"status":"ok"}
- 4.c logging to results/<ts>/app.log
- 4.d test_healthz.py: 1/1 passed
- Full suite: 3/3 passed

## 5. Data model + migrations — done
- 5.a models: User, OrgMembership, ObservationCache, RefreshLog (GUID PKs, Rule 18)
- 5.b alembic init + initial migration applied to dashboard_pdhc_db
- 5.c test_models.py CRUD cycle passed
- Full suite: 4/4 passed

## 6. Gateway client — done
- 6.a GatewayClient.fetch_observations (FHIR Observation bundle)
- 6.b GUID-only normalise (Rule 18); drops malformed entries
- 6.c refresh_org: clear + repopulate cache, RefreshLog row with status/count
- 6.d test_gateway_client.py: 2/2 passed (normalise + refresh with mock)
- Full suite: 6/6 passed

## 7. Auth + bootstrap SU — done
- 7.a AUTH_MODE=off loads dev SU user automatically
- 7.b `flask create-su` CLI command (Rule 23)
- 7.c SSO stub (reads X-PDHC-User, 401/403 as appropriate)
- 7.d scope_to_user_orgs middleware (Rule 24) — admin sees all, members restricted
- 7.e test_auth.py: 3/3 passed (off mode, scoping, CLI)
- Full suite: 9/9 passed

## 8. Landing page — done
- `/` lists patients in user's org with counts per concept, latest observation
- PDHC-styled base.html (12px, accent #2a6ebb)

## 9. Concept selection + cohort curves — done
- checkbox multi-select, max 2 enforced server-side
- Chart.js time scatter with line overlay

## 10. Patient dashboard — done
- `/patient/<guid>` latest-values table + per-concept time curves
- 403 if outside user orgs, 404 if no data

## 11. Refresh button — done
- POST `/refresh` → refresh_org for each user org, logged in RefreshLog
- prominent button in header of every page

## 12. API + FHIR metadata — done
- GET `/api/v1/series?patient&concept` returns FHIR Bundle of Observations
- GET `/metadata` returns FHIR R5 CapabilityStatement
- test_views_api.py: 5/5 passed (landing, patient view, series, metadata, 400)
- Full suite: 14/14 passed

## 13. API test script — done
- `scripts/test_api.sh` walks endpoints, writes `results/<ts>/api_test.json`
- Expected-code scoring: pass=5 fail=0

## 14. start.sh — done
- kills 9026–9029, ensures Docker, `docker compose up -d db`, waits pg_isready
- activates venv, `flask db upgrade`, starts Flask on APP_PORT
- Ctrl+C trap → `docker compose down` + deactivate

## 15. Documentation (Rule 25) — done
- `docs/technical.md` (architecture, models, endpoints, env, running)
- `docs/user_manual.md` (login, landing, patient view, refresh, API)

## 16. API key handling — documented in docs/technical.md
- storage: .env only (gitignored)
- rotation: regenerate + restart
- expiry: 90d reminder at 75d
- revocation: gateway admin revoke + rotate

## 17. Local acceptance — green
- pytest: 14/14 passed
- scripts/test_api.sh: 5/5 passed (live)
- Manual walkthrough: landing, patient view, /metadata, /healthz all 200

## 18. Server deploy — app serving on dashboard.pdhc.se (2026-04-11)
- 18.a pre-flight: pytest 14/14 → after SSO refactor 22/22 ✓; tarball built
- 18.b remote pre-checks: target dir created (operator), Colima up on macmini, ports 9026–9029 free
- 18.c transfer: two tarballs extracted to releases/2026-04-09T10-36-42Z and releases/2026-04-09T14-40-47Z; `current` → 14-40-47Z
- 18.d server .env seeded from .env.example, mode 600; operator filled secrets
- 18.i-bis SSO integration (in-place, before reverse proxy):
  - app/auth.py rewritten: validate via sso.pdhc /api/auth/me/service (mirrors gateway)
  - new app/routes/auth.py blueprint: /auth/login, /auth/callback, /auth/logout
  - phase gate: 'analysis' in effective_phases OR is_su_admin
  - org scope now reads blob['organization_ids'] (Rule 24)
  - AUTH_MODE=off retained for local dev (Rule 23)
  - tests rewritten (test_auth.py): 22/22 passed locally
- 18.e venv on server — done (rebuilt from scratch 2026-04-11; see "Venv rebuild" below)
- 18.f migrate — done. alembic head 146cc611c12e, all 5 tables present
- 18.g create-su — done. martin@ingvar.com (is_su=true, created 2026-04-09 18:13 UTC)
- 18.h start — done. gunicorn daemon on 127.0.0.1:9027, pid under shared/, logs under shared/logs/
- 18.i smoke — done. Internal and external probes both pass:
  - /healthz → 200 `{"auth_mode":"sso","status":"ok"}`
  - /metadata → 200 (FHIR R5 CapabilityStatement)
  - GET / → 302 → /auth/login (SSO redirect, per auth.py before_request)
  - /auth/login → 302 (handoff to sso.pdhc)
- 18.j reverse proxy — operator already live (dashboard.pdhc.se → 127.0.0.1:9027). Not touched.

### 2026-11-20 — the service is called dashboard again (cd-assist removed)
- Page titles (base, select, nurse workspace, logged out), the h1, code comments, tests' comments and both manuals say "dashboard". The host, ports and containers were already dashboard.
- Suite 881 passed. Deployed 2026-11-20 (overlay, rollback tag :pre_rename).

### start.sh rewrite (2026-04-11)
Replaced the foreground `flask run` pattern with a robust daemonized setup
matching gateway.pdhc / 1177 / cgm conventions:
- gunicorn `--daemon` bound to 127.0.0.1:9027 (reverse proxy fronts it)
- pid file + access/error logs under `/usr/local/www/dashboard.pdhc/shared/`
  so they survive release swaps
- graceful stop of previous gunicorn via pid file; belt-and-braces kill of
  any leftover :9027 listener
- COMPOSE_PROJECT_NAME pinned (suppresses the "using name current from
  symlink" warning and prevents accidental parallel compose projects)
- hard-bounded waits on Postgres readiness and /healthz so the script fails
  loudly instead of hanging

### Venv rebuild (2026-04-11)
Found that releases/2026-04-09T14-40-47Z/app/.venv was a copy of the old
release's venv — gunicorn and other scripts still had shebangs pointing at
`releases/2026-04-09T10-36-42Z/app/.venv/bin/python3.14`, and pyvenv.cfg's
`command` field still referenced the old release path. That meant the new
release was functionally glued to the old one: pruning 10-36-42Z would
have broken 14-40-47Z immediately. Fixed by:
  rm -rf releases/2026-04-09T14-40-47Z/app/.venv
  /opt/homebrew/opt/python@3.14/bin/python3.14 -m venv releases/2026-04-09T14-40-47Z/app/.venv
  .../app/.venv/bin/pip install -r requirements.txt
gunicorn shebang now correctly points at 14-40-47Z's own python.

### services.html diagnosis — why broken services show green
Reviewed www.pdhc.se/services.html client-side polling JS. Two independent
bugs, plus a third class of silent failures:
1. `fetch(url, { mode: 'no-cors' })` returns an *opaque* response for ANY
   network-level success — including HTTP 5xx, 4xx, redirects, TLS, etc.
   The dot goes green as long as the reverse proxy's TLS handshake works,
   regardless of whether the upstream Flask/gunicorn returned 200, 503, or
   nginx's 502. This is the *primary* false-green bug.
2. The "DB" dot is hard-coded to mirror the API dot (`if (dbDot) setDot(dbDot,
   'up', 'DB ok')`). There is no separate DB probe at all.
3. Several services' /health endpoints don't report DB status themselves —
   contract `/health` → `{"status":"ok"}` only, plan `/api/health` → same,
   dashboard `/healthz` and rosetta `/healthz` → `{auth_mode,status}` only.
   Even a CORS-enabled JSON parser couldn't tell DB state from these.

Ground-truth probe (internal, 2026-04-11 07:52 UTC):
  sso        :9000 /api/health       200  database=connected
  request    :9060 /api/health       503  database=unavailable  ← actually broken
  ips        :9040 /api/v1/health    200  database=connected
  contract   :9021 /health           200  (no db field)
  provider1  :9070 /api/v1/health    200  database=connected
  1177       :9036 /api/health       200  database=connected
  cgm        :9080 /api/v1/health    200  database=connected
  gateway    :9050 /api/v1/health    200  database=connected
  dashboard  :9027 /healthz          200  (no db field)
  rosetta    :9092 /healthz          200  (no db field)
  cdr        :9046 /healthz          503  db=false              ← actually broken
  plan       :9030 /api/health       200  (no db field)

Currently broken (but green on services.html):
  * request.pdhc.se — container `.env` creds drifted from its DB container;
    psycopg2 `FATAL: password authentication failed for user "request_admin"`.
    `docker ps` marks request_pdhc_app as `(unhealthy)` for 28+ min.
  * cdr.pdhc.se — Postgres closes the connection mid-query during
    `SELECT count(*) FROM ingest_raw`. Likely OOM/pool config in cdr.pdhc,
    not a lost DB.

Fix plan for services.html (follow-up, separate service):
  a) Switch fetch to `mode: 'cors'` and add `Access-Control-Allow-Origin:
     https://www.pdhc.se` on each service's /health* endpoint. Then use
     `response.ok` to distinguish 2xx from 5xx.
  b) Standardise /healthz payloads to `{status, database, service, version}`,
     parse JSON in the JS, and drive the DB dot from the real `database` field.
  c) Normalise the path names (`/healthz` vs `/health` vs `/api/v1/health`
     vs `/api/health`) — one canonical path per service.

### Follow-up TODOs (not blocking but noted)
- dashboard /healthz does not ping the DB. Add a minimal SELECT 1 health check
  (and apply the standardised JSON shape in (b) above).
- Compose project name on the macmini is `current` (accidental, from the
  symlinked dir). Plan a maintenance window to dump → rename to
  `dashboard_pdhc` → restore. Non-urgent.
- Orphan volume `dashboard_pdhc_dashboard_pdhc_pgdata` exists on the macmini
  from a prior failed attempt. Verify empty and `docker volume rm`.
- Prune `releases/2026-04-09T10-36-42Z` eventually — safe NOW that 14-40-47Z
  has an independent venv, but leave until after one or two more
  smoke-verified days to keep a rollback target.

---

## Platform-plan Phase 4 (CDR-federation dashboard upgrade) — overlay

Per `../plans/CDR_sim_dashboard_execution_plan.md` §4.

### §4.1 Federation backend — DONE (2026-04-26)
- [x] `app/services/federation.py` — `CdrRegistry` from
  `app.config["CDR_ENDPOINTS"]`, `discover()` boot-time ping.
- [x] `fanout(...)` with per-CDR timeout, ThreadPoolExecutor concurrency,
  bearer/org/admin header forwarding (§4.1.b–c).
- [x] `merge_histograms(...)` parallel-variance combine (§4.1.d).
- [x] `concat_series(...)` with cdr_id tagging (§4.1.e).
- [x] `lttb_downsample` + `agp_hourly_bands` helpers used by §4.2.

### §4.2 Nurse workspace backend — DONE
- [x] `GET /api/nurse/patient/<guid>` — finds owning CDR, rolls up
  conditions / regimen / latest-values.
- [x] `GET /api/nurse/patient/<guid>/agp?window=14d|90d`.
- [x] `GET /api/nurse/patient/<guid>/variable/<canonical>` — LTTB to ≤2k.
- [x] `GET /api/nurse/patient/<guid>/events`.

### §4.3 Researcher workspace backend — DONE
- [x] `POST /api/cohort` (define) + `GET /api/cohort` (list).
- [x] `GET /api/cohort/<id>/variable/<canonical>/histogram` — federated
  $stats merge.
- [x] `GET /api/cohort/<id>/variable/<canonical>/boxplot?group_by=`.
- [x] `GET /api/cohort/<id>/scatter?x=&y=&max=` — truncate flag.
- [x] `GET /api/cohort/<id>/trend?canonical=&window=`.
- [x] `GET /api/cohort/<id>/export?format=csv&variables=` (§4.6).

### §4.4–§4.5 Frontend — DONE (2026-04-26, backend-callable HTML/JS)
- [x] `/workspace` selector — sole-role redirect, dual-role chooser,
  admin sees both, no-clinical-role 403.
- [x] `/nurse` (§4.4) — patient-GUID search + recent strip, AGP card
  with hourly bands and TIR/TBR/TAR/mean/CV/hypo summary, variable
  picker (HbA1c, weight, BMI, BP sys/dia, TIR), event feed.
- [x] `/researcher` (§4.5) — left rail (5 CDR multi-select +
  demographics + condition + medication filters), variable picker,
  chart pane with Histogram / Box-Violin / Scatter / Trend tabs
  (scatter auto-disabled until 2 vars), right rail summary +
  per-CDR breakdown + CSV export via hidden iframe.
- [x] Partial-result banner driven by `fanout_mode` from the
  federation layer.

The pages are pure HTML+vanilla-JS+Chart.js (no framework / build
step). They work the moment the 5 CDRs are reachable; until then the
"build cohort" button can be pressed but will return a 0-member
cohort because no CDRs respond.

### §4.6 CSV export — DONE (audit log to ``results/export_audit.log``)
DB-backed `dashboard_audit` table is a follow-up — needs a migration
that the existing JSONB+UUID model issue must be sorted first.

### §4.7 Permissions / role guards — DONE
`nurse_required`, `researcher_required`, `admin_required` decorators
in `app/services/role_guards.py`. Admin satisfies any clinical role.

### §4.8 Backend tests — DONE
26 new tests:
- `test_federation.py` — 16 tests covering cohort_predicate_builder,
  fanout (complete / degraded / error / header-forwarding),
  histogram_merge (preserves total / skips-failed / weighted-mean),
  concat_series tagging, LTTB (short-passthrough / extrema-preserved /
  target-size), AGP (flat-trace / hypo-count).
- `test_role_guards.py` — 6 tests for role enforcement.
- `test_researcher_flow.py` — 4 integration-shaped tests with a
  fully-mocked federation: cohort intersection, histogram merge across
  2 CDRs, CSV export filters to members, scatter truncation flag.

38/49 total in dashboard's test suite (38 = 12 pre-existing green +
26 new). The 11 pre-existing failures stem from the legacy
ObservationCache model being JSONB+UUID-typed, which SQLite's
create_all() can't reproduce — not Phase 4 regressions.

Frontend (§4.4 / §4.5) and Playwright E2E (§4.8 e2e block) deferred
until the 5 CDRs exist and a real environment can drive them.

---

## #462 clinical dashboard redesign — D3 + D5 (2026-07-13)

Decomposition of #462 into build tickets #463–#469 (see
`docs/redesign_462_decisions.md` for the locked operator answers to the
#469 open questions). Started the two Q1-independent tickets.

### D5 — Saved designs (#467) — DONE (local, tested; not deployed)
- `SavedDesign` model (`app/models/__init__.py`): user-private reusable
  template. `owner_user_guid` (String128) + `name` + opaque JSONB `spec`.
  NOT patient-bound (operator #469 Q3).
- Migration `2026_07_13_saved_design.py` (rev `sd071322aa01`, down
  `a8bc21200001`; id ≤32 chars). `flask db heads` → single head.
- `app/routes/designs.py`: CRUD under `/api/v1/designs`, every op scoped
  to `owner_user_guid`; foreign designs read back 404 (no existence leak).
- Tests `test_saved_design.py`: 3 (CRUD happy-path, validation, owner
  isolation). All green.

### D3 — Clinical patient picker (#465) — DONE (dashboard side; live wiring pends #468)
- `app/services/cdr1_client.py`: `Cdr1Client` reads CDR1 under a
  care-delivery basis — declares `X-Access-Purpose: care-delivery` so
  CDR1 selects care_access_policy/spärr over #422 analysis-consent
  (bypass is CDR-side, #468). Org scope via `X-Org-Guids`; service-key
  auth; fail-open to `[]` on error. `parse_patient_bundle` pure helper.
- `app/routes/picker.py` + `templates/select.html`: `GET /select` lists
  org-affiliation-scoped patients from CDR1, client-side name/id filter,
  single-select → `/patient/<guid>`. Banner when `CDR1_BASE_URL` unset.
- Config `CDR1_BASE_URL` (+ `.env.example`).
- Tests `test_patient_picker.py`: 5 (bundle parse, no-base-url empty,
  non-admin-no-orgs empty, /select lists+scopes, unconfigured banner).

Full suite: **211 passed**. NOT yet deployed. Live smoke deferred until
the SSH tunnel is up AND #468 (CDR1 care-delivery read + per-org
patient-index) lands. Blocked/next: #463 (D1 split + auth re-home),
#464 (D2 per-patient CDR1 reads), #466 (D4 charts), #468 (D6 CDR side).

## #463 / #462 D1 — auth re-home (2026-07-13)

The clinical dashboard's front door moves off the analysis-phase gate onto a
CARE-DELIVERY gate; the analyse engine keeps analysis-phase. Implemented as a
route-aware SSO gate (app/auth._dashboard_access_allowed) so NO analyse route
file changes and the whole test suite (AUTH_MODE=off, no gate) is unaffected —
only the production SSO path changes.
  - has_care_delivery_access = SU admin OR professional with a care-unit scope
    (scope_org_guids: affiliations[].care_unit_guid, dual-read organization_ids).
  - _is_clinical_path: /, /refresh, /select, /patient/*, /api/v1/designs.
  - Existing test_auth.py SSO tests still pass (blobs carry org scope).
Boundary inventory (what stays vs relocates to analyse.pdhc) in
docs/redesign_462_decisions.md §D1. PHYSICAL relocation deferred to #470
(blocked until analyse.pdhc exists — deleting live analyse code now would drop
nurse/researcher + gateway's analyse pull with nowhere to land). CLAUDE.md §11
to be updated when this deploys. Tests: test_care_access_auth.py 10/10; full
suite 222 passed. NOT deployed.

## #464 D2 + #466 D4 — CDR1-backed per-patient charts (2026-07-13)

Built the new clinical patient view WITHOUT disturbing the legacy
ObservationCache /patient view (retirement + #212 re-home tracked in #471).

#464 (data): cdr1_client.patient_series() → CDR1 /clinical/patient/<guid>/series.
New app/routes/charts.py JSON proxies the browser calls (browser has no service
key): /api/v1/patient/<guid>/parameters (sorted concepts) + /series (points,
spärr-filtered by org_guid via ips_client, coarse — lift refinement in #471).
Both care-delivery gated (/api/v1/patient/ added to clinical paths).

#466 (charts): new page /patient/<guid>/charts (charts.html). Chart.js.
Per operator #469 answers: parameter dropdown sorted by data count (Q7 label =
code+unit); MIRROR a 2nd parameter on an independent right y-axis (Q2 dual-axis);
1 diagram default, hard cap 3, each independent incl. its own time window (Q8);
CONTINUOUS time slider mapping exp 1 day..5 years (Q4) + zero-based/autoscale
toggle + y-max slider (Q4); SAVE/LOAD reusable design templates via
/api/v1/designs (#467), applied to any patient (Q3). /select now links here.

Deferred (→#471): retire legacy /patient + ObservationCache; re-home #212;
vendor Chart.js (still CDN); spärr lift refinement; plan.pdhc display names.

Tests: test_patient_charts.py 5/5; full suite 227 passed. NOT deployed.

## #471 item 3 — vendor Chart.js (2026-07-13)
Chart.js 4.4.6 UMD + chartjs-adapter-date-fns v3.0.0 vendored into
app/static/vendor/; base.html now loads them locally instead of cdn.jsdelivr.
Self-contained (survives an offline/locked-down deploy), no CSP/external dep.
Served + referenced verified; full suite 227 passed. Remaining #471 items
(retire legacy /patient+ObservationCache [blocked on #469 Q6], re-home #212
[needs legal #437], spärr lift refinement + plan.pdhc display names [need a
code_canonical↔Concept.guid mapping]) stay open.

## DEPLOYED to prod — 2026-07-16
Both PRs #1 merged to main; deployed per docs/deploy_462_runbook.md.
- CDR1 (cdr.pdhc): git pull --ff-only (5350a73→a920c14) + docker-compose up -d
  --build app. No schema change. clinical_read live; /api/v1/clinical/patients
  smoke HTTP 200 count=10.
- dashboard: new release releases/2026-07-16T11-19-18Z (from tarball 3281af3),
  cp .env forward, docker-compose up -d --build app, flask db upgrade
  (a8bc21200001→sd071322aa01, single head), current flipped.
- EDIT A done: dashboard .env CDR1_BASE_URL=http://host.docker.internal:9046
  (loopback). EDIT B (PLAN_BASE_URL) deferred → raw concept codes for now.
- Verified end-to-end: dashboard container→CDR1 (app env) HTTP 200 count=10;
  host.docker.internal resolves; external /healthz 200 both services; DBs
  untouched. NOT yet browser/SSO-smoked (operator: /select→/charts).
- Gotcha: `docker compose` v2 threw 'unknown flag -d' → used docker-compose v1
  (§8.3 probe). CDR1 compose project-name unpinned warning (data intact).
- Rollback: dashboard ln -snf releases/2026-07-10T06-54-37Z-x1 current + rebuild;
  CDR1 git checkout 5350a73 + rebuild.
- Next: operator browser smoke; then #471 item 1 (retire legacy ObservationCache
  surface, Q6=CDR1-only); legal on #472/#437; optional PLAN_BASE_URL for names.

## #471 item 1 — retire legacy ObservationCache surface (STAGED 2026-07-16)
Branch feat/471-retire-legacy-cache (PR, NOT merged/deployed — lands after the
browser smoke). Q6=CDR1-only, so the gateway→cache clinical surface is retired:
- '/'->redirect /select; '/patient/<guid>'->redirect /patient/<guid>/charts.
- Removed: /api/v1/series, GatewayClient, cache_retention (sweep+scrub),
  /admin/cache/scrub, cache-sweep CLI, refresh button, OBSERVATION_CACHE_TTL,
  dead templates (landing/patient/admin_override_required).
- KEPT (not destructive): ObservationCache + RefreshLog MODELS + prod tables
  (dropping the tables is a separate confirm-required data step); ips_client;
  /admin/audit viewer.
- #212 admin off-org justification flow REMOVED (lived only on legacy /patient;
  the live /charts path already lacked it). Care-delivery admin control = item 2
  (legal #437). #212 logic preserved in git history.
Tests: 194 passed (33 obsolete removed: gateway_client/cache_retention/
admin_override_audit + route tests). app boots, /select present, series gone.

## #471 item 1 — DEPLOYED 2026-07-19
PR #2 merged (main 0ea948f); code-only dashboard redeploy (no migration).
New release releases/2026-07-19T19-14-02Z, rebuild, current flipped. Verified in
running image: gateway_client absent; /select + /patient/<guid>/charts present;
/api/v1/series absent. External /metadata advertises only 'healthz' (no series);
/healthz 200. Legacy ObservationCache clinical surface is now retired in prod
(models + tables KEPT — dropping tables is a separate confirm-required step).
Rollback: ln -snf releases/2026-07-16T11-19-18Z current + rebuild.

## #471 — drop the retired ObservationCache + RefreshLog tables (STAGED 2026-07-19)
Branch feat/471-drop-cache-tables (PR, NOT merged/deployed). The tables were NOT
empty (observation_cache=7019, refresh_log=185 stale rows from before the
retirement). Backed up first: pg_dump both to
~/backups/predeploy/dashboard/observation_cache_refresh_log_<ts>.sql (21.7 MB,
7019+185 rows verified). Then: removed the models + unused scope_to_user_orgs;
migration drop0719cache01 (down_revision sd071322aa01, single head) drops both
tables (downgrade recreates the empty schemas; data restorable from the dump).
Tests 192 passed (2 org-scoping tests removed). Deploy = flask db upgrade in the
dashboard container (like the last deploy).

## #471 drop cache tables — DEPLOYED 2026-07-19
PR #3 merged (main 88c9a23); deployed release releases/2026-07-19T19-25-58Z,
flask db upgrade ran drop0719cache01 (sd071322aa01->drop0719cache01).
Verified: pg_tables query for observation_cache/refresh_log returns empty (both
dropped); models absent in running image; /healthz 200. Backup retained at
~/backups/predeploy/dashboard/observation_cache_refresh_log_20260719T192036Z.sql
(21.7 MB, 7019+185 rows). Rollback: flask db downgrade + restore from that dump.

## #546 nurse-fold into cd-assist — LOCAL, verified 2026-08-07
Pivot #545: cd-assist = this (dashboard) renamed; the nurse single-patient
views are folded into the care-delivery surface (salvaged from the discarded
greenfield cd-assist.pdhc). Changes: routes/nurse.py re-gated analysis-phase →
care-delivery + per-patient spärr (coarse default; #471.4 indispensable-care
lift under SPARR_LIFT_ENABLED with the sparr_lift_exposure audit event), same
shape as charts.py::series; auth.py _is_clinical_path now covers /api/nurse/ so
the app-level gate enforces has_care_delivery_access (nurse dropped its
per-route @nurse_required — matches /charts which has no per-route guard).
Tests: full suite 211 passed (new test_nurse_sparr.py = 9; role_guards/
analysis_consent/care_access_auth updated to the app-level-gate model).
Next: rebrand → cd-assist (#547, low-friction: keep host, rename internally);
discard the greenfield cd-assist.pdhc (salvage complete).

## #543 remove group/population analyse-engine — LOCAL, verified 2026-08-08
Refocus of dashboard.pdhc into "cd-assist" (individual/point-of-care). The
group/population half was extracted into the separate new service analyse.pdhc,
so it is removed here.

Removed (source): app/routes/researcher.py (owned /api/cohort* +
register_export_audit_cli), app/analyse/{cohort,observations_search,stats,
canonical,openehr}.py. In app/__init__.py the 5 group blueprints
(researcher_bp, observations_search_bp, analyse_stats_bp, analyse_canonical_bp,
analyse_openehr_bp) + the register_export_audit_cli import/call are gone.
role_guards.researcher_required removed (nurse_required + admin_required kept).
workspace.py trimmed to nurse-only: the /researcher shell and the role-selector
branching are gone; /workspace now redirects straight to /nurse (nurse is the
sole remaining clinical shell). The orphan workspace_selector.html /
researcher_workspace.html templates are left in place (harmless, unrendered).

Deliberately KEPT (back the #546 nurse single-patient fold): app/analyse/
federation.py + aggregations.py (+ __init__.py) — the CDR2-5 federated reads
under the dashboard.pdhc service identity — and their config in app/__init__.py
(CDR_ENDPOINTS, DASHBOARD_PDHC_SERVICE_KEY, CDR1_BASE_URL). app/services/* kept
intact (audit.py still classifies the /api/cohort route-string — harmless).
app/models/* and app/migrations/* were NOT touched: the now-orphan Cohort /
CohortExportAudit tables remain (dropping them is a separate data decision);
single alembic head unchanged = drop0719cache01.

Tests: deleted the 4 removed-module-only suites (test_researcher_flow,
test_observations_search, test_analyse_aux, test_export_audit_to_db); trimmed
the mixed suites (test_role_guards, test_workspace, test_care_access_auth,
test_x1_tuple, test_analysis_consent, and test_federation for the cohort-
predicate import) to drop only the group/researcher/api-cohort cases. The
_apply_research_consent tests were removed because that fn lived in the deleted
researcher.py; the role-derivation route assertions were converted to direct
role_guards._roles() unit checks (same guarantee, no removed route). Full suite:
167 passed, 0 failures. py_compile clean across app/**/*.py. App boots with
create_app; registered blueprints = admin, auth, charts, designs, nurse_api,
picker, views, workspace (the 5 group blueprints gone).

Next: analyse.pdhc owns the extracted engine (separate repo/service); rebrand
dashboard→cd-assist (#547) still pending.

## 2026-09-03 — #578/#579 (dashboard=individual) choose-patient list + nav DEPLOYED
Operator clarified 578/579 target dashboard.pdhc (cd-assist, individual), NOT
analyse.pdhc (group) — an earlier analyse.pdhc build was the wrong surface
(left as-is; "reder ut analyse senare").

Applied the 578 design to cd-assist (template-only, reusing existing picker /
per-patient charts / spärr / admin audit):
- base.html nav: PDHC/services · choose patient (→picker.select) · Dashboard
  (was Nurse) · Spärrlogg (admin → existing /admin/audit).
- select.html "Choose patient": Namn · Födelseår · Patient-GUID (full guid on
  mouseover) · Datapunkter · Senaste data (last_observed_at) + Uppdatera; row →
  charts (Dashboard).
- nurse_workspace.html "Nurse workspace" → "Dashboard".
Commit 081da0e (origin/main). 167 tests pass.

DEPLOY (release 2026-09-03T07-13-40Z): safest path — copied the current release,
overwrote ONLY the 3 template files (checksum-verified vs local reform),
preserved .env, flipped symlink, docker-compose up -d --build (project
dashboard_pdhc, volume reused). Verified: /healthz 200 local+public; running
image contains the new nav markers; routes 302; no boot errors. Old release
2026-08-08T10-34-25Z retained as rollback; predeploy tar in
~/backups/predeploy/dashboard.pdhc/.

Deferred (with analyse, later): CDR1-5 multi-CDR selector; per-row spärr badge.

## 2026-10-01 — #727: monitor.pdhc removed from dashboard's KNOWN_SERVICES

Done under explicit operator instruction, which overrides the standing "do not
touch other services" rule for this change only.

`monitor.pdhc` was never a service: a synthetic service-key identity created
2026-04-28 so **this repo's** Playwright (F2), perf, stats (F3) and chaos (F4)
suites could bypass SSO (`plans/*_2026-04-28.md`). analyse.pdhc removed it the
same day; this was the last place it still authenticated.

`MONITOR_PDHC_SERVICE_KEY` is no longer read into config, so the variable is
inert wherever it is still set.

### What this breaks, deliberately
`e2e/` authenticates as `monitor.pdhc` (`e2e/playwright.config.ts`), as do
`app/tests/test_chaos_isolation.py` and `test_statistical_validation.py`. They
will now get 403 at the loader. The suite has one commit touching it
(2026-04-29, the day after it was written), has never produced a
`playwright-report` anywhere in this repo, and this service is what #462
supersedes. A harness that needs to come back should get its OWN key under its
own name — not reuse a shared one.

### A finding that is NOT fixed here (#727 notes it; needs its own ticket)
The service-key branch of `install_request_loader` returns **before**
`_dashboard_access_allowed` is applied:

```python
sk = _service_key_outcome(app)
if sk is True:
    ...
    return None          # <-- gate never runs
```

So a service-key caller skips the care-delivery / analysis-phase gate entirely
and can reach clinical paths (`/patient/…`, `/api/v1/patient/…`, `/api/nurse/…`,
`/workspace`) that an SSO caller would be 403'd out of. The service blob carries
no affiliations and no orgs, so org scoping should empty the result — that
"should" is exactly what wants verifying rather than assuming. Removing
`monitor.pdhc` removes one of the two identities that could do it; `gateway.pdhc`
remains, and its one endpoint here (`/api/v1/observations`) moved to
analyse.pdhc in #540, so that entry may be dead too.

167 tests pass.

## 2026-10-01 — #729: the service-key path is closed, and gated if it reopens

Two separate defects, both fixed. Done under explicit operator instruction
(this repo is normally out of scope).

### 1. `KNOWN_SERVICES` is now empty
`gateway.pdhc` was still listed. It joined for #291 so its
`/api/v1/observations` proxy could land here instead of cdr1; **#540 repointed
that proxy to analyse.pdhc** and nobody removed the entry. Verified against the
live gateway container 2026-10-01: `ANALYSE_BASE_URL` is
`http://host.docker.internal:9110` (analyse) and there is **no `DASHBOARD*`
variable at all**. Nothing calls dashboard service-to-service.

An empty dict makes `_service_key_outcome` return `False` for any
`X-Source-Service` header, so every service-key attempt is 403 before a route is
reached. `GATEWAY_PDHC_SERVICE_KEY` is no longer read into config.

### 2. The gate now applies to the service-key branch
`install_request_loader` returned from the service-key branch **before**
`_dashboard_access_allowed`, so a service-key caller skipped the care-delivery /
analysis-phase gate and could address every clinical path (`/`, `/workspace`,
`/refresh`, `/patient/…`, `/api/v1/patient/…`, `/api/v1/designs`, `/api/nurse/…`)
that an SSO caller without a care relationship is 403'd out of.

The service blob carries no affiliations and no orgs, so org scoping (Rule 24)
*should* have emptied each read. That is the second line of defence doing the
first line's job, and ips's block filter failed open once already (e7e81a1), so
it is not a defence to rely on. An empty `KNOWN_SERVICES` is a fact about today;
the gate is what holds when someone adds an identity tomorrow.

### Why this was invisible for five months
`test_analysis_consent.py::test_service_blob_denied_on_clinical_routes` already
existed, and its comment said *"so the app-level gate keeps it out"*. But it
asserted `has_care_delivery_access(blob) is False` — the **predicate**, on a path
where the predicate was never consulted. The ingredient was tested; the dish was
never cooked. That comment is now corrected and points at the real test.

### Tests
174 pass (was 167). `tests/test_service_key_gate.py` is new, 7 tests, and goes
through the **real loader** rather than calling the helper:
- `KNOWN_SERVICES` is empty; every identity, known or invented, gets 403
  `Invalid service credentials`; the attempt never reaches routing (403, not 404).
- The regression test re-admits an identity exactly as a future change would,
  then asserts the loader still refuses it on the clinical paths.
- A source check that the gate call is still on that branch.

**Both of those were verified to fail against a deliberately reverted fix** —
without the gate, `/` returns 302 instead of 403. The source check needed
hardening to get there: its first version passed on the reverted fix, because
the branch's own #729 comment names the gate function. A source assertion that
matches its own explanation is worth nothing.

---

## #827 — spärr was never asked about at all (2026-10-15)

Found by `deploy/smoke_siblings.py` on its first run (#708): `IPS_BASE_URL` was
**unset in production**. Every spärr lookup this service ever made returned
`[]` without asking ips, which means no point was dropped by `_apply_sparr`
and the PDL Ch 4 §4 ¶3 banner never rendered.

### What was NOT exposed
cdr1 applies `care_access_policy` on the care-delivery read itself and
dashboard declares `X-Access-Purpose: care-delivery` (#468), so the rows
arriving here were already spärr-filtered. The smoke confirms cdr1's
break-glass gate is live (428 `ADMIN_READ_REASON_REQUIRED` on a reasonless
admin read). This was a **missing second layer and a missing banner**, not
demonstrated exposure. The banner is the part cdr1 cannot cover.

### Three defects, not one

1. **The variable.** Now `IPS_BASE_URL=https://ips.pdhc.se`. The `.env` also
   moved from release-local to `shared/.env` with `current/.env` a symlink
   into it, as analyse got in #758 — release-local means the file dies with
   its release and each deploy carries a *copy* forward, which is how this
   drifted out in the first place.

2. **`is_configured()` could never have answered yes.** It read
   `current_app.config["IPS_BASE_URL"]`; `IpsClient.__init__` read
   `os.environ`; and `create_app` loaded every other sibling URL into the
   config but not that one. Setting the variable would have fixed the filter
   and left the banner silent — with nothing to blame. This is the cdr_6
   defect from the same #708 sweep, one service over: **two resolution paths
   for one deployment fact.** `resolve_base_url()` is now the only path.

   No test caught it because all four `is_configured()` tests passed the URL
   as a **config override**. Production sets it in the **environment**. Five
   new tests go through `create_app` and the real variable; four fail against
   the previous commit.

3. **The banner read the wrong question.** `has_blocked_sources` is False both
   when there is no block and when ips was never asked. `sparr_checked` now
   travels beside it at all six render/jsonify sites, and `charts.html` carries
   a second notice for the case the first cannot express — styled as a warning,
   because not knowing is not a neutral state. The JSON half matters equally:
   the nurse views are driven by JS, which cannot read a Jinja conditional.

### The open question answered rather than fixed
The ticket asked: give dashboard an `IPS_API_KEY` and send
`Authorization: ApiKey` as analyse does, or state that the paths are
session-only. **There is no path to give a credential to.** Every caller of
`get_active_blocks` is an HTTP route in `routes/nurse.py` or `routes/charts.py`;
the only CLI command is `create-su`; there is no scheduler; and
`KNOWN_SERVICES` is empty (#729), so no service identity can reach dashboard at
all. Session-only by construction. `_default_client`'s docstring claimed a
"fallback for service-to-service paths (sim refresh, monitor probes)" and all
three parts were wrong — ips ignores `X-Service-Key` (#783), `monitor.pdhc` was
retired (#727), and there is no sim refresh here. Corrected in place.

### Deploy
Release dir source was a month stale and the running container was *ahead* of
it (files `docker cp`'d in during the #708 session), so a `--build` would have
silently reverted last session's work. All 41 tracked `app/` files were hashed
against local git first: exactly one differed, nothing on the server was
unique. Then sync → `.env` → `docker-compose up -d --build` (not `docker
restart`, which keeps create-time env). Dependencies are pinned (#785) so the
rebuild did not move them.

Rollback: image `dashboard_pdhc-app:pre827`; source+`.env`
`miserver:~/backups/predeploy/dashboard.pdhc/pre827_20261015T080254Z.tgz`.

### Verified on production
`printenv IPS_BASE_URL` = `https://ips.pdhc.se`; `resolve_base_url()`,
`is_configured()`, the config key and `_default_client().base_url` all agree;
`/healthz` `status: ok` / `database: connected`; `smoke_siblings.py` **13/13**
(was 10 pass, 3 fail).

### Tests
196 pass in `app/tests` (was 183), none skipped. 13 new across
`test_sparr_unconfigured.py` and `test_sparr_banner_distinguishes.py`; 10 of
the 13 were verified to fail against the reverted fix, and the 3 that pass are
negative cases that pass trivially when nothing renders.

## #931 — dashboard reads plan with a key (2026-11-16)
PlanClient._headers adds dashboard's read_only plan key (config-then-env, the
resolve_base_url path). pytest: 772 passed.

## #942 (2026-11-16): the canonical URI format is owned by termbank.pdhc (app/canonical_uri.py + conformance/canonical_uri_cases.json); the concept URN by plan.pdhc (planp/app/concept_urn.py + conformance/concept_urn_cases.json). Consumers keep byte-identical corpus copies in their tests; tools/check_canonical_uri_conformance.py in the workspace fails on drift or on a literal that breaks the grammar.

## #984/#985 (2026-11-16): operator decision — the CDR is the record for diagnoses (and observations); icd11 codes into the CDR; the ips EPS is a summary derived from the CDR (`derive-conditions-from-cdr`); the dashboard chips read the EPS. EPS Conditions were backfilled into their home CDR (`import-eps-conditions`); those with no home CDR stay, marked not CDR-backed.

## 2026-11-18 — Nurse view reads observations from every CDR (9ba70b2)
- Summary (`/api/nurse/patient/<guid>`): one Observation search across all CDRs; 404 only with neither a Patient resource nor a visible observation. New fields `patient_record_found`, `observation_cdrs`.
- AGP: fans out per CGM code (`NURSE_CGM_CODES`, default LOINC 41653-7 + plan concept CGM 22d0f6c6) and merges per CDR.
- Why: the 40 Hackathon patients' simulated CGM data sits in cdr2 with no Patient resource there (sim writes none); 39 of 40 answered "not found".
- Tests: +6 (app/tests/test_nurse_cross_cdr.py; 5 fail on the old code), 879 pass. Deployed as an overlay image, rollback tag `dashboard_pdhc-app:pre_nurse` + `*.pre_nurse` files in current/. Verified live on klin1 (owner cdr1, values cdr1+cdr2, AGP n=86) and klin2 (no Patient anywhere, values cdr2, AGP n=78).
- Display names (859176f, deployed same day, rollback tag `:pre_names`): values with no coding display are named from plan.pdhc's concept index. Verified live: "B-glucos", "Kontinuerlig glucosmätning CGM".

### 2026-11-21 — #1014 patient-generated data shown to the professional
- Event list "Patient" badge; detail "Ursprung" row (never "ej registrerat": unmarked = healthcare by policy) plus a note; chart hollow points with a legend line and an aria-label count; CSV `data_origin` as the LAST column. Fixtures mark EQ VAS, the free-text well-being answers and the HF manual weights.
- 5 unit tests (fail with the change reverted) + e2e `fixtures-specs/origin.spec.ts` (badge, note, legend, axe wcag2a/aa with the marked row selected; it caught a selected-row badge contrast of 2.83, fixed). Suite 886; fixtures e2e 16/16.
- Deployed 2026-11-21 (overlay, image tag `dashboard_pdhc-app:pre_origin2`, pg_dump in ~/backups/predeploy/origin2_dash/). Health 200, hashes ok, the served JS/CSS carry the markers, smoke --all green before and after.

## #1019 — EPS rename phase 3 (2026-11-21, local commit, NOT deployed)
dashboard reads EPS_BASE_URL first, IPS_BASE_URL only when EPS_BASE_URL is
blank (app/eps_env.py), in both create_app and ips_client.resolve_base_url.
Both unset = "" as before (is_configured() False, SPARR_UNAVAILABLE logged).
dashboard reads no IPS_API_KEY from the environment (the smoke's
cfg.get("IPS_API_KEY") reads a config key nothing loads), so only
EPS_BASE_URL is needed. pytest: 886 -> 898 passed (+11 eps, +1 guard).
Mutation proof: reverted helper fails 4 new-name tests, EPS-only 4 old-name
tests, invented default 3 unset tests.
Baseline: .env.example and app/tests/test_blocks_filter.py in the container
are OLDER than git (#931 / #942 not shipped into the image); all touched code
files match. Operator, at deploy: add EPS_BASE_URL=https://eps.pdhc.se to
shared/.env (env_file), then docker-compose up -d.

## 2026-11-22 — #1019 eps rename code DEPLOYED

- Overlay image on the running one, baseline-hashed first; rollback tag `:pre_eps`, replaced release files kept as `*.pre_eps.<ts>`.
- Health 200 and file hashes verified in the container; sibling smoke green before (pre-rename smoke) and after.
- No behaviour change until `EPS_*` is set in .env: operator script `/tmp/epsdep/eps_env.sh <group>` (refuses until https://eps.pdhc.se answers as eps.pdhc).


## 2026-11-22 — #1044 (epic #1039) queue "EPS att granska" (local commit, NOT deployed)
Built against `plans/patient_app_contract.md` s4.2/s5 and then checked against
cdr.pdhc's `app/api/reviews.py` as it stood uncommitted in the working tree the
same day. Detail in docs/technical.md ("EPS review queue").

- Routes: `GET /eps-granskning`, `GET /eps-granskning/antal`,
  `POST /eps-granskning/<review_guid>/bekrafta`, `POST …/beslut`. Care-delivery
  gate (registered in `_is_clinical_path`); SU admin must attest first (428).
- cdr1 is called with the operator's own bearer only, no service key.
- Refusal vs empty: named exceptions in the client; the page renders
  `#rv-problem` with 403/502/503, and `#rv-empty` only on a 200 with an empty
  `items` list. Nav count: `count: null` + status on failure, never 0.
- CSRF: none existed in this service. Added `app/services/csrf.py` for these
  two POST routes only.
- Audit: `eps_review_queue|confirm|approve|amend|reject|decision`; identifiers
  and outcome only, no answer/value/note/reason text.
- As-built differences from the contract text that the view handles: cdr1
  answers a flat 403 (not 404) for an unknown or out-of-scope original; the
  list carries `can_decide`, `limit`, `truncated` (default limit 200).

### Tests
pytest `app/tests tests`: 898 passed, 13 skipped -> **961 passed, 13 skipped**
(+63, `app/tests/test_eps_review_1044.py`). The 13 skips are
`tests/test_chaos_isolation.py` + `tests/test_statistical_validation.py`,
which need `MONITOR_PDHC_SERVICE_KEY` (the retired monitor.pdhc identity);
identical with this change stashed. e2e/ not run.

All through the real routes, AUTH_MODE=sso, only `requests.get/post` patched
(one fake = sso token validation + cdr1 review API).

Controls (mutate, run, restore; every failure an assertion):
| mutation | tests that fail |
|---|---|
| gate always allows | 3 `test_a_user_outside_the_gate_is_refused[*]` |
| path not registered as clinical | 57 (a nurse without the analysis phase is 403'd) |
| `csrf_valid` always True | 6 `test_csrf_missing_or_wrong_is_refused[*]` + audit |
| client reads 403 as an empty list | 4, incl. `test_cdr_403_renders_a_refusal_not_an_empty_queue` |
| client swallows every list failure | 10 |
| a 200 without `items` read as empty | 1 |
| route renders the problem as an empty page | 8 |
| count answers 0 on failure | 1 |
| reason not required | 3 `test_reject_without_a_reason_is_stopped[*]` |
| confirmation not required | 1 |
| service key sent with the bearer | 52 |
| audit stores note/reason/value | 3 |
| must_change_password not bounced | 1 |
| admin attestation not required | 1 |
| `can_decide` ignored | 1 |

### Not verified
- No browser pass at all: the local Playwright browsers do not match the
  installed Playwright version and nothing was downloaded. Layout, touch
  targets (44px in CSS), the `<details>` disclosure and the nav count script
  are untested in a real browser; no iPad pass, no axe run.
- Not run against a real cdr1 or sso; the fake follows the contract and the
  cdr code read on 2026-11-22.

### Deploy (operator decides when; cdr.pdhc's review API must be deployed first)
No migration, no new dependency, no new `.env` key (`CDR1_BASE_URL` and the
SSO pair are already set). Code only: baseline-hash, then rebuild or overlay
as for the previous code-only deploys. Until cdr1 has the API the queue shows "CDR saknar
granskningsfunktionen" and the nav count shows "?".

## 2026-11-22 — #1049 (v2) card versions in "EPS att granska" (local commit, NOT deployed)
Built against `plans/patient_app_contract.md` s7.4/s7.5 while cdr.pdhc's card
API was being built in parallel: coded to the contract shapes, cdr1 faked at the
HTTP boundary. Detail in docs/technical.md ("EPS review v2: card versions").
Deviations recorded in the contract as `[v2 dashboard]`.

- `/eps-granskning` lists card versions first, per patient then section, each
  proposed row beside the endorsed one with Ny / Ändrad / Borttagen /
  Oförändrad as text (`app/services/card_diff.py`, a display diff). The v1
  per-row queue follows for rows without `card_version`.
- `POST /eps-granskning/kort/<card_version>/bekrafta|beslut`: approve (optional
  per-row amend, `amend-<row guid>`, empty = unchanged) or reject (reason
  required). CSRF and the two-step confirmation as v1; operator bearer only.
- 409 `superseded` and 409 `already_decided` each have their own Swedish
  message (`ReviewSuperseded` subclasses `ReviewAlreadyDecided`).
- Instances are matched whole (same group / identical content), never by
  position, because gateway's `group` is per receipt. Coded rows not amendable.
- Card-list failure: `#cd-problem` with the v1 list still shown; nav count is
  null if either list fails.

### Tests
pytest `app/tests tests`: 961 passed, 13 skipped -> **1011 passed, 13 skipped**
(+50, `app/tests/test_eps_review_cards_1049.py`; `test_eps_review_1044.py`'s fake
also answers the card list, empty). Re-run from clean source (no __pycache__,
PYTHONDONTWRITEBYTECODE=1) after another agent may have run a mutation script
against `card_diff.py`; all 37 mutation sites verified restored first.

Controls: 37 mutations, every one caught by at least one #1049 test, every
failure an assertion (two tests set PROPAGATE_EXCEPTIONS=False so a crash is a
500 to assert on, not a raise):
| mutation | #1049 tests failing |
|---|---|
| instances paired by position | 1 |
| value comparison always equal | 3 |
| removed rows not shown | 2 |
| same-group instances not diffed field by field | 1 |
| superseded read as already_decided | 1 |
| card rows / endorsed not shape-checked | 1 / 1 |
| card list failure read as empty | 8 |
| v1 keeps card rows / per-row decide accepts a card row | 1 / 1 |
| count ignores a card failure | 1 |
| coded rows amendable / coded shown by code | 2 / 1 |
| amend kept as raw text / read from any posted field | 3 / 1 |
| reject reason not required | 2 |
| CSRF not checked (confirm / decide) | 3 / 3 |
| confirmation not required | 1 |
| can_decide ignored (post / template) | 1 / 1 |
| card_version not checked as uuid | 1 |
| admin attest skipped on card lookup | 1 |
| audit stores the note | 1 |
| gone not detected | 8 |
| service key sent with the bearer | 47 |
| cell labels dropped / empty text / nothing-endorsed text | 1 / 1 / 1 |
| confirm step posts / reject sends amends | 3 / 1 |
| already-decided message / 422 / unknown / 403 statuses | 1 / 1 / 1 / 1 |
| card paths not clinical | 26 |
| cards grouped per version, not patient | 1 |

### Browser (static, no server)
Rendered HTML of the queue and the confirmation page opened from file in
Chrome (Playwright from `e2e/` driving the installed Chrome) at 820x1180,
1180x820 and 390x844: no horizontal overflow, no `.rv-btn` under 44px, four
columns at both iPad sizes, one stacked column on the phone. WebKit (Safari)
NOT run: its Playwright build is not installed. No real iPad, no axe run,
nothing interactive beyond opening the amend disclosure.

### Not verified
- Not run against a real cdr1: the card API did not exist in cdr.pdhc when
  this was written. The fake follows s7.4 plus the `[v2 dashboard]` readings.

### Deploy (operator decides; cdr.pdhc's s7.4 must be deployed first or together)
Code only, no migration, no new dependency, no `.env` key. Until cdr1 has s7.4
the card section shows "CDR saknar granskning av patientkort", the v1 queue
works, and the nav count shows "?".

## 2026-11-22 — #1050 step 2: "Bjud in patient" (LOCAL, not deployed)

- `/bjud-in-patient` (nav link "Bjud in patient", professional or SU): form → confirmation (calls nothing) → send. CSRF on both POSTs, `confirmed=ja` required.
- Units from `affiliations[].care_unit_guid` only: one preselected; several = required radios, no default; none and not SU = 403 page "Du har ingen affiliering till någon vårdenhet". The callback now lets a professional without unit/phase sign in ONLY when going to this page, so the message can be shown.
- Calls eps `register-at-unit` then sso `POST /api/patient-links/invites` (with the name), both with the user's own bearer, no service key. Named Swedish errors; eps ok + sso failed = half-done page with retry. The link is shown once (`no-store`) with a copy button and a patient text; sms later.
- Audit: `patient_invite_form` / `_confirm` / `_send` with `outcome`, never the number, name or link.
- Tests: `app/tests/test_invite_1050.py` (22), auth ON, HTTP boundary patched. Suite 1011 -> 1033 passed. 7 mutation controls (D1-D6 + bearer-only), each fails on an assertion.
- Loopback e2e (sso + eps + dashboard + patientapp, real apps): 18/18.
- Deploy: code only, no migration, no new dependency. Uses the config key `IPS_BASE_URL`, which create_app fills from `EPS_BASE_URL` first (#1019), and `SSO_BASE_URL`; both are already set for the dashboard's other features. No `.env` change.
