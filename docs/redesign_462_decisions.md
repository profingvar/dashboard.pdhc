# dashboard.pdhc redesign — locked decisions (ticket #462)

Source: operator answers to the #469 open-questions ticket, 2026-07-13.
This note is the D1 (#463) deliverable — the boundary + decisions the build
tickets (#463–#468) implement. Two items (Q1 legal, Q6 cache) are still open.

## THIS SERVICE REPLACES THE CURRENT DASHBOARD (operator, 2026-07-13)

The clinical dashboard we are building under #462 **replaces** the current
dashboard.pdhc — it is not a parallel product kept alongside the existing
one. Consequence: the existing analyse/federation engine (nurse +
researcher workspaces, cohort builder, CDR2-6 fanout, the legacy
gateway→ObservationCache clinical view) is being **retired from this repo**
and its analytical parts relocated to the future analyse.pdhc (#463/D1).
When the new clinical dashboard is ready, it takes over the dashboard.pdhc
service identity, hostname, and deploy slot. Plan every ticket as building
the REPLACEMENT, not an addition.

## The product boundary (Q9 — DECIDED)

Build a **NEW, clean CLINICAL dashboard** as its own product (which then
BECOMES dashboard.pdhc, per the note above):
- Single-patient. Feedback to the healthcare team *treating* the patient.
- Reads production data from **CDR1 directly**.
- The existing **analyse/federation engine** in this app (nurse + researcher
  workspaces, cohort builder, CDR2–6 fanout, population aggregates) **moves out**
  to the future **analyse.pdhc** — it is not part of this clinical product.
- Reconcile with the in-flight cdr1/analyse split (plans/cdr1_analyse_split_plan.md,
  #287–293) rather than duplicating it.

## Access / legal basis (Q1 — OPEN, recommendation recorded)

A clinical dashboard is a **care-delivery** basis (vård: vårdrelation + spärr
inre/yttre + nödöppning, via `ips.pdhc/.../care_access_policy.py`), NOT the
**analysis-consent** basis (`#422 check_patient_allowed`) that CDR1's read path
applies today. Using the analysis gate would hide patients who consented to care
but declined research — from their own treating clinician. Wrong basis.

Recommended resolution (pending operator/legal sign-off):
1. Dashboard declares **purpose = care-delivery** on CDR1 calls → CDR1 SKIPS the
   `#422` analysis-consent gate and returns org-scoped data (`_org_filter`).
2. Spärr enforced by the dashboard's EXISTING `ips_client.fetch_active_blocks`
   (already wired) — ships without a CDR1 change.
3. Defense-in-depth (push spärr into CDR1) deferred.
4. **vårdrelation model** (what proves "this clinician treats this patient") →
   legal (#437). v1 proposal: org affiliation ∩ no active spärr.

Blocks #464 (D2) + #468 (D6) until confirmed.

## Confirmed build decisions

- **Q2 mirror** = two parameters on ONE diagram, independent left/right y-axes
  (dual-axis), shared time x-axis. (Not literal inversion.)
- **Q3 saved design** = a **reusable template** (diagram layout + parameter &
  mirror selections + scaler config), **re-applied to any patient**, **private
  to the user**. NOT patient-bound, NOT org-shared. → SavedDesign has no patient
  FK; owner = user.
- **Q4 time control** = **continuous slider** for the time window, PLUS a **y-axis
  slider** for manual scaling, PLUS a **toggle** for the y baseline: either
  **zero-based (0 = lowest)** or **autoscale to data min/max**.
- **Q5 patient identity** = show ips.pdhc name. Every CDR1 patient is expected to
  have an IPS record (production data + the new sim module generates IPS records
  too), so no "orphan" flow is required — keep only a defensive fallback.
- **Q6 ObservationCache** = DECIDED 2026-07-15: **live CDR1 reads only, no
  read-through cache.** So the legacy ObservationCache surface (landing `/`,
  legacy `/patient/<guid>`, `/api/v1/series`, GatewayClient, `/refresh` +
  auto-refresh, the `ObservationCache` model + retention/cache-sweep) is
  retired — tracked in #471 item 1. SEQUENCING: run that retirement only AFTER
  the new CDR1 `/charts` path is deployed and live-smoked, and after the #212
  admin-control re-home (item 2) is settled. Do NOT delete the working legacy
  view on this branch before the replacement is proven in prod: the deploy
  keeps BOTH paths; a follow-up change removes the legacy one.
- **Q7 parameter identity** = dropdown keyed by **plan.pdhc Concept.guid**;
  y-axis label/unit from **concept.unit** (unit lives on the concept, not the
  transaction). Confirmed.
- **Q8 diagrams** = **1 by default, hard cap 3**. Each diagram is **independent**,
  including its **own time window** (the scaler is PER-diagram, not global).

## D1 (#463) — the split, in detail

### Auth re-home (DONE, #463)
The SSO gate is now route-aware (`app/auth.py`):
- **Clinical routes** (`/`, `/select`, `/patient/*`, `/api/v1/designs`,
  `/refresh`) → `has_care_delivery_access`: SU admin, or a professional with
  a care relationship (a care-unit scope via `scope_org_guids`). A treating
  clinician no longer needs the 'analysis' phase.
- **Everything else** (the analyse engine) → the unchanged `has_analysis_access`
  phase gate, so its security is preserved byte-for-byte until it relocates.
- AUTH_MODE=off has no gate (dev SU), so local dev + the whole test suite are
  unaffected. Only the production SSO path changes.
- Follow-up when this deploys: update CLAUDE.md §11 (which still says the
  dashboard "belongs to the analysis phase").

### What STAYS (this becomes the clinical dashboard product)
- Routes: `views.py` (`/`, `/patient` — legacy cache view, replaced by CDR1
  reads in D2/#464), `picker.py` (`/select`), `designs.py` (`/api/v1/designs`).
- Services: `cdr1_client.py`, `ips_client.py` (spärr), `audit.py`
  (PDL kontroller log).
- Models: `User`, `OrgMembership`, `ObservationCache` (legacy — Q6 pending),
  `SavedDesign`, `DashboardAudit`, `RefreshLog`.
- Templates: `landing.html`, `patient.html`, `select.html`, `base.html`.
- The admin read-audit viewer (`routes/admin.py`) — it audits clinical reads,
  so it stays with the clinical product.

### What RELOCATES to analyse.pdhc (tracked as a follow-up)
- `app/analyse/*` — federation, aggregations, stats, cohort, canonical,
  openehr, observations_search.
- Routes: `nurse.py`, `researcher.py`, `workspace.py` + their templates
  (`nurse_workspace.html`, `researcher_workspace.html`, `workspace_selector.html`).
- Model: `Cohort`.
- The analyse-layer service endpoints gateway.pdhc calls
  (`/api/v1/observations`, `/api/v1/canonical`, `/api/v1/stats`,
  `/api/v1/openehr`) — these are the cdr1/analyse-split surface (#287-293).
  **Gateway's proxy must be repointed to analyse.pdhc when they move**, or
  gateway breaks. This is the one hard cross-service dependency.

### Sequencing (why the physical move is a follow-up, not part of #463)
The analyse engine is LIVE and its only relocation target — analyse.pdhc —
does not exist yet. Deleting it now would drop nurse/researcher + gateway's
analyse pull with nowhere to land. So #463 does the auth re-home + this
boundary; the physical extraction (stand up analyse.pdhc, move the code,
repoint gateway, delete here) is a separate tracked ticket.

## Impact on the build tickets

- #463 D1 — this note; the analyse engine relocation is part of the split.
- #464 D2 — read CDR1 **directly**; blocked on Q1.
- #465 D3 — patient picker, org-affiliation scoped (unblocked).
- #466 D4 — dual-axis mirror; per-diagram continuous time slider + y-slider +
  zero/autoscale toggle; 1 default / 3 cap; Concept.guid + concept.unit; vendor
  Chart.js.
- #467 D5 — SavedDesign = user-private reusable template, no patient binding.
- #468 D6 — CDR1 per-patient concept-count summary; blocked on Q1 (auth basis).
