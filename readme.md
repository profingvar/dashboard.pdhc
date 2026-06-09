# dashboard.pdhc — Deployment Plan

Read-only dashboard over observations delivered to gateway.pdhc. Local Mac bring-up first; macmini deploy later.

Ports: 9026 Postgres · 9027 Flask app · 9028/9029 reserved.
DB: `dashboard_pdhc_db`. Auth: `AUTH_MODE=off|sso` (off for local dev).

---

## 1. Repo scaffolding
1.a Create `app/` (routes, services, models, templates, static, tests, migrations), `docs/`, `results/`.
1.b Seed `progress.md`, `changed_files.md`, `newtask.txt`, `requirements.txt`, `.env.example`, `.gitignore`.
1.c Initialise local git (Rule 14 — separate from other repos).
1.d Tests: directory exists, all required files present.

## 2. Python venv + dependencies
2.a Create `app/.venv` (Rule 7, Rule 21).
2.b `requirements.txt`: Flask, SQLAlchemy, Flask-Migrate, psycopg2-binary, requests, python-dotenv, pytest, pytest-flask, gunicorn.
2.c Install and freeze.
2.d Tests: `python -c "import flask, sqlalchemy, requests"` passes inside venv.

## 3. Postgres on 9026 (Docker)
3.a `docker-compose.yml` for `dashboard_pdhc_db` exposing 9026 with named volume (Rule 7 isolation).
3.b `.env` DB_URL, DB credentials, `AUTH_MODE=off`, GATEWAY_BASE_URL.
3.c Tests: container starts, `psql` connect succeeds, DB exists.

## 4. Flask app skeleton
4.a `app/__init__.py` create_app() with config from env.
4.b Health endpoint `/healthz`.
4.c Logging to `results/<ts>/app.log` (Rule 11, Rule 24 full log).
4.d Tests: pytest hits `/healthz` → 200.

## 5. Data model (own DB)
5.a SQLAlchemy models: `User` (SU bootstrap), `OrgMembership`, `RefreshLog`, `ObservationCache` (patient_guid, concept_guid, concept_name, value, unit, observed_at, source_obs_guid).
5.b Alembic init + first migration.
5.c Tests: migration up/down clean; CRUD on each model.

## 6. Gateway client service
6.a `services/gateway_client.py` — `list_observations(org_guid)` against gateway.pdhc.
6.b GUID-only matching (Rule 18).
6.c Refresh routine: clear+repopulate `ObservationCache` for the user's org.
6.d Tests: mocked gateway responses → cache populated correctly.

## 7. Auth + bootstrap SU
7.a `AUTH_MODE=off` bypass for local dev.
7.b SU bootstrap CLI: `flask create-su` (Rule 23).
7.c SSO stub for `AUTH_MODE=sso` (talks to sso.pdhc later).
7.d Org-scoping middleware (Rule 24): non-admin sees only own org's patients.
7.e Tests: off mode passes through; SU create idempotent; org filter enforced.

## 8. Landing page (eligible patients)
8.a Route `/` — list patients in user's org with ≥1 observation.
8.b Columns: patient name/guid, total obs count, per-concept counts (names only), date of latest observation overall.
8.c Template extends a base layout, PDHC look matching request.pdhc/contract.pdhc.
8.d Tests: route renders; org-scoping verified; sort/order stable.

## 9. Concept selection + cohort curves
9.a Multi-select (max 2) concept picker on landing.
9.b Cohort overlay: up to two time-activity curves across selected patients.
9.c Chart library: Chart.js (light, no build step).
9.d Tests: selection persisted in querystring; ≤2 enforced; chart JSON endpoint returns valid series.

## 10. Patient dashboard view
10.a Route `/patient/<guid>` — readable patient summary.
10.b Panels: identity header, per-concept latest values table, time-activity curves for any concept.
10.c Tests: route renders; unauthorized org → 403.

## 11. Refresh button
11.a POST `/refresh` triggers gateway pull for user's org; logged in `RefreshLog`.
11.b UI: single prominent button on landing.
11.c Tests: button triggers refresh; log row written; cache updated.

## 12. Internal API + FHIR exposure
12.a JSON API for chart data: `/api/v1/series?patient=<guid>&concept=<guid>` (FHIR-shaped Observation bundle envelope).
12.b CapabilityStatement endpoint `/metadata` (Rule 15).
12.c Tests: schema validation against FHIR R5 Observation profile.

## 13. API endpoint test script
13.a `scripts/test_api.sh` walks the CapabilityStatement and exercises every endpoint (Rule 9, Rule 20).
13.b Results into `results/<ts>/api_test.json`.

## 14. start.sh
14.a Single bash script (Rule 16): kill 9026–9029, ensure docker running, `docker-compose up -d`, activate venv, run migrations, start Flask on 9027.
14.b Trap Ctrl+C → graceful shutdown, `docker-compose down`, deactivate venv.
14.c Tests: script starts and stops cleanly; ports free after exit.

## 15. Documentation (Rule 25)
15.a `docs/technical.md` — architecture, data flow, models, env vars, API.
   See **ObservationCache retention** for the TTL sweep + admin scrub
   policy (#213, PDL Ch 4 §§ 3-4).
15.b `docs/user_manual.md` — login, refresh, selecting concepts, reading curves.
15.c Both updated at the end of every step where behaviour changes.

## 16. API key handling (Rule 8)
16.a Storage in `.env` only, never committed.
16.b Rotation procedure documented in `docs/technical.md`.
16.c Revocation: invalidate via SSO and rotate gateway client token.

## 17. Final local acceptance
17.a Full pytest run green.
17.b API test script green.
17.c Manual walkthrough against landing → patient view → refresh.
17.d progress.md updated with all results in `results/<ts>/`.

## 18. Server deploy (deferred — wait for explicit go)
18.a Operator-only sudo (Rule 19). Bring-up plan to be drafted when greenlit.
