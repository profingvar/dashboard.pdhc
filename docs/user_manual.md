# dashboard.pdhc — User Manual

## 1. Logging in
- **Development**: `AUTH_MODE=off` — you are logged in automatically as a dev
  super-user. No credentials required.
- **Production**: `AUTH_MODE=sso` — log in via the PDHC SSO portal; the
  dashboard trusts the `X-PDHC-User` header set by the SSO reverse proxy.
- Admin users see all organisations. Regular users see only patients from
  organisations they are a member of (Rule 24).

## 2. Landing page — Eligible patients
After login you land on `/`:
- **Patient list** — one row per patient in your organisation with at least
  one observation. Columns: Patient GUID (clickable), total observation
  count, count per concept (names only), date of the latest observation
  overall.
- **Concept picker** — check up to two concept boxes and click
  **"Show curves (max 2)"** to overlay time-activity curves for the cohort.
- **Refresh from gateway** (top-right button) — pulls the latest observations
  from gateway.pdhc for your organisations. This is the single canonical way
  to update the data; there is no background polling.

## 3. Patient dashboard
Click a patient GUID on the landing page to open `/patient/<guid>`:
- **Latest values table** — one row per concept with the most recent value,
  unit, and timestamp.
- **Time-activity chart** — every concept the patient has data for is shown
  as its own line on the chart.
- **Back** link returns to the landing page.

## 4. Refreshing data
The **Refresh from gateway** button is visible on every page. Clicking it:
1. Clears the local observation cache for each organisation you belong to.
2. Pulls the current Observation bundle from gateway.pdhc.
3. Repopulates the cache and writes an entry to the refresh log.

If the refresh fails, the error is stored in the refresh log and the existing
cache remains untouched.

## 5. Reading the charts
- X axis: time (day-grouped).
- Y axis: numeric value of the observation.
- On the landing cohort chart, each point is one observation for one patient
  in the cohort; hover to see value and patient GUID.
- On the patient chart, each line is one concept; use the legend to toggle.

## 6. API for downstream tools
The dashboard exposes its curve data as FHIR R5 so it can be re-consumed
elsewhere:
- `GET /api/v1/series?patient=<guid>&concept=<guid>` — returns a FHIR Bundle
  of `Observation` resources.
- `GET /metadata` — CapabilityStatement describing all endpoints.

## 7. Getting help
- Runtime logs: `results/<timestamp>_results/app.log`
- Technical details: `docs/technical.md`
- Deployment plan + progress: `readme.md`, `progress.md`
