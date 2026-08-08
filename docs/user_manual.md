# cd-assist — User Manual

**cd-assist** is the individual / point-of-care **clinical-decision-assist**
tool of the PDHC platform — the former *dashboard*, refocused. It lets a
treating clinician look at **one patient at a time**: their latest values,
time series, ambulatory glucose profile (AGP), and clinical events. It runs on
`dashboard.pdhc.se` (the host is unchanged from the old dashboard).

> **Population / cohort analysis moved out.** The group features — the
> researcher cohort builder, cohort exports, and federated
> observation-search / statistics — are no longer here. They now live in the
> separate **analyse.pdhc** service (`analyse.pdhc.se`). If you are looking for
> aggregate distributions across many patients, go there.

## 1. Logging in and access
- **Development**: `AUTH_MODE=off` — you are logged in automatically as a dev
  super-user. No credentials required.
- **Production**: `AUTH_MODE=sso` — log in via the PDHC SSO portal. Your session
  is re-validated with SSO on every request, so a logout (or an admin revoking
  your session) takes effect immediately.
- **Access is a care relationship, not an analysis grant.** You reach cd-assist
  if you are an SU admin, or a professional with at least one **care-unit
  scope** (a care relationship). You do **not** need the *analysis* phase — that
  phase gates analyse.pdhc, not this service.
- **Org scoping (Rule 24)**: admins see all organisations; a regular clinician
  sees only patients belonging to their own care units.
- **Spärr (per-patient blocks)**: if a patient has blocked one or more clinics,
  data from those clinics is withheld from every view. By default the block is
  coarse (all of a blocked clinic's data is hidden). Where the DPO-approved
  *indispensable-care* lift is enabled, specifically-covered data may be shown,
  and every such exposure is separately audited. A metadata-only banner tells you
  when a patient has blocked sources so you know the picture may be incomplete.

## 2. Choosing a patient
After login you land on the **patient picker** (`/select`):
- One row per patient your care units are affiliated with, read live from CDR1.
- Patients are listed by name; click a patient to open their charts.
- If no CDR1 is configured (local dev), the page says so rather than looking
  falsely empty.

## 3. Per-patient charts (`/patient/<guid>/charts`)
The charts view is the core single-patient surface, backed by CDR1:
- **Parameter list** — the concepts this patient has data for (the dropdown).
- **Series** — pick one or more concepts and a time window; the chart draws the
  points. Data is read from CDR1 under a care-delivery basis and spärr-filtered
  on this side before it reaches your browser (the browser never talks to CDR1
  directly).
- The banner flags when the patient has blocked sources.

## 4. Nurse single-patient views (`/nurse`)
The **nurse workspace** page (`/nurse`, reachable via `/workspace`) drives four
single-patient data views, federated across the regional CDRs (CDR2–5):
- **Summary** — demographics, active conditions, current regimen, and the most
  recent value per concept over the last 90 days.
- **AGP** — the Ambulatory Glucose Profile (14-day or 90-day window): glucose
  percentile bands and summary statistics.
- **Variable** — a single variable's full time series, automatically
  downsampled for display when very dense.
- **Events** — clinical event markers: encounters and severe-hypoglycaemia
  events.

All four are care-delivery gated and spärr-filtered per patient, exactly like
the charts view. Access to the nurse page itself requires the **nurse** role (or
admin).

## 5. Saved designs
You can save a **design** — a personal, reusable view configuration (which
diagrams/variables to show). Designs are private to you: there is no shared or
admin view of another user's designs. Manage them via `/api/v1/designs`
(list / create / get / update / delete).

## 6. Audit (admins)
SU admins can browse the read-audit log at `/admin/audit` (filter by date,
user, patient, route, event type; paginated) and export the filtered set as CSV
at `/admin/audit/export.csv`. Every patient-touching read — including denials
and spärr-lift exposures — is recorded here.

## 7. Getting help
- Runtime logs: `results/<timestamp>_results/app.log` (local) or
  `shared/logs/gunicorn.*.log` (server).
- Technical details: `docs/technical.md`
- Deployment plan + progress: `readme.md`, `progress.md`
- For population / cohort work, see the **analyse.pdhc** manuals.
