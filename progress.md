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
