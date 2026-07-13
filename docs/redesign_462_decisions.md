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
- **Q6 ObservationCache** = OPEN (legal has priority; decide after Q1). Default
  lean: live CDR1 reads, cache only if latency demands it.
- **Q7 parameter identity** = dropdown keyed by **plan.pdhc Concept.guid**;
  y-axis label/unit from **concept.unit** (unit lives on the concept, not the
  transaction). Confirmed.
- **Q8 diagrams** = **1 by default, hard cap 3**. Each diagram is **independent**,
  including its **own time window** (the scaler is PER-diagram, not global).

## Impact on the build tickets

- #463 D1 — this note; the analyse engine relocation is part of the split.
- #464 D2 — read CDR1 **directly**; blocked on Q1.
- #465 D3 — patient picker, org-affiliation scoped (unblocked).
- #466 D4 — dual-axis mirror; per-diagram continuous time slider + y-slider +
  zero/autoscale toggle; 1 default / 3 cap; Concept.guid + concept.unit; vendor
  Chart.js.
- #467 D5 — SavedDesign = user-private reusable template, no patient binding.
- #468 D6 — CDR1 per-patient concept-count summary; blocked on Q1 (auth basis).
