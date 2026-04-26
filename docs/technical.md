# dashboard.pdhc — Technical Description

## Purpose
Read-only visualisation dashboard over observations delivered to gateway.pdhc
from upstream providers. Serves an eligible-patient list, cohort time-activity
curves, and a per-patient dashboard. Part of the PDHC microservice suite.

## Architecture
```
[provider.pdhc] ── push ──► [gateway.pdhc] ◄── GET Observation ── [dashboard.pdhc]
                                                                        │
                                                                 Postgres (9026)
                                                                        │
                                                                   Flask (9027)
```

- **Rule 6**: dashboard relies on gateway.pdhc for all observation data.
- **Rule 5 / 10**: own Postgres on localhost (`dashboard_pdhc_db`, port 9026).
- **Rule 16**: ports 9026–9029 reserved. 9026=DB, 9027=Flask app.
- **Rule 18**: every internal reference is by GUID.
- **Rule 24**: admins see all orgs; members restricted to their OrgMembership rows.

## Components
- `app/__init__.py` — Flask app factory, config, logging to `results/<ts>/app.log`.
- `app/models/__init__.py` — SQLAlchemy models: `User`, `OrgMembership`,
  `ObservationCache`, `RefreshLog`.
- `app/auth.py` — `AUTH_MODE=off` bypass (dev SU), SSO stub, `scope_to_user_orgs`
  helper, `flask create-su` CLI (Rule 23).
- `app/services/gateway_client.py` — `GatewayClient.fetch_observations()` calls
  `GET {GATEWAY_BASE_URL}/fhir/Observation?organization=<org>`; `normalise()`
  maps FHIR Observation → `ObservationCache` shape; `refresh_org()` is the
  clear+repopulate routine logged in `RefreshLog`.
- `app/routes/views.py` — HTML routes: `/`, `/patient/<guid>`, POST `/refresh`.
- `app/routes/api.py` — JSON API: `/api/v1/series`, plus `/metadata`
  CapabilityStatement (Rule 15, FHIR R5).
- `app/templates/` — `base.html` (PDHC styling, 12px), `landing.html`,
  `patient.html` (Chart.js time scatter).
- `app/migrations/` — Alembic migrations (`flask db upgrade`).

## Data flow
1. User clicks "Refresh from gateway" → POST `/refresh`.
2. Server iterates `OrgMembership` rows for current user, calls
   `refresh_org(user, org)` for each.
3. `refresh_org` fetches bundle → normalises → replaces `ObservationCache`
   rows for that org → writes `RefreshLog` with count + status.
4. Landing page reads cache, filtered by `scope_to_user_orgs`.

## Environment variables (.env)
- `DATABASE_URL` — postgresql connection string
- `APP_PORT` — Flask port (default 9027)
- `AUTH_MODE` — `off` (dev) or `sso` (prod)
- `GATEWAY_BASE_URL` — e.g. `https://gateway.pdhc.se`
- `GATEWAY_TOKEN` — bearer token (keep in `.env` only; see API keys)
- `SECRET_KEY` — Flask session secret

## API keys (Rule 8)
- **Storage**: `.env` only, never committed. `.gitignore` excludes `.env`.
- **Rotation**: generate a new token via the gateway admin UI, update
  `GATEWAY_TOKEN` in `.env`, restart with `./start.sh`.
- **Expiry**: tokens expire at 90 days; set a calendar reminder at 75 days.
- **Revocation**: revoke in the gateway admin UI immediately on compromise;
  rotate per above.

## Endpoints
| Path | Method | Purpose |
|------|--------|---------|
| `/healthz` | GET | liveness + current AUTH_MODE |
| `/metadata` | GET | FHIR R5 CapabilityStatement |
| `/` | GET | landing page (eligible patients + optional cohort curves) |
| `/patient/<guid>` | GET | patient dashboard |
| `/refresh` | POST | pull observations from gateway for user's orgs |
| `/api/v1/series?patient&concept` | GET | FHIR Bundle of Observations |

## Running locally
```
./start.sh
```
- Kills anything on 9026–9029
- Ensures Docker Desktop is up
- Starts Postgres via docker-compose
- Runs Alembic migrations
- Starts Flask on 9027
- Ctrl+C → `docker compose down` + venv deactivate

## Tests
```
app/.venv/bin/python -m pytest app/tests
./scripts/test_api.sh   # exercises live endpoints against a running instance
```
