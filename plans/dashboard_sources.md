# Dashboard data sources — Phase 0 / Gate 0 report

Deliverable of the Phase 0 ticket, for the coding plan at
`pdhc/docs/PDHC_Patient_Dashboard_Coding_Plan.md`. Measured 2026-11-11.

Everything below is measured, not inferred. Where a number contradicts the
plan, the measurement is given with the command shape that produced it so it
can be re-run.

---

## 1. The stack — there is no SPA

| | measured |
|---|---|
| `package.json` at repo root | **none** |
| JS framework / build step | **none** |
| Jinja templates | 5 — `base`, `nurse_workspace`, `charts`, `select`, `admin_audit` |
| plain `.js` files | 2 |
| chart library | Chart.js UMD + `chartjs-adapter-date-fns`, vendored (#471) |
| only `package.json` | `e2e/` — Playwright |
| responsive breakpoints today | one: `@media (max-width: 768px)` |

The plan assumes an app shell, a router, data hooks, a windowing library and
component tests, and *also* says "reuse the existing stack — do not introduce
a new framework". Both cannot hold.

**Recommendation: Option A — Jinja + vanilla JS + Chart.js.** The virtualised
list and the detail cache are then hand-written, and "component tests" become
route tests plus Playwright.

**`chartjs-plugin-zoom` is NOT vendored.** It is what provides zoom, pan and
every touch gesture in the chart, so step 20's zoom requirement and
pinch-to-zoom on iPad have an unmet dependency. Vendor it as a third UMD
bundle with its version recorded, or drop the zoom requirement.

## 2. The interaction model — settled: selection, not hover

Hover is wrong as the mechanism and fine as an accelerator. iPadOS Safari
synthesises a hover on first tap (two-tap reveal-then-activate), and a
trackpad attached to the same iPad restores real hover — so the capability is
not a property of the device and changes at runtime. Branch on capability
(`@media (hover: hover) and (pointer: fine)`, `matchMedia`,
`event.pointerType`), never on device. The repo uses none of these today.

Measured across `app/templates/` and `app/static/`: **11 `click`, 1
`mouseenter`, 1 `pointerdown`, 1 `touchstart`, 0 `mouseover`.** The module as
built already chose click. The plan's hover-first design is a departure from
what exists.

Consequence: **step 19's pin is deleted.** It exists only because hover is
transient; once selection persists, pinning is the default.

## 3. iPad layout

- The plan's 1024 px stacking breakpoint is **exactly** iPad landscape on the
  9.7"/10.2" models (1194 on the 11" Pro, 1366 on the 12.9"). Move it to
  1100–1200 px, or design the 1/3 panel for ~340 pt.
- Touch targets ≥ 44×44 pt, while a dense list wants ~28–32 px rows with a
  pointer. **Row height is capability-driven** and the virtualiser must be
  told which mode it is in.
- `overscroll-behavior: contain` on each of the three independently scrolling
  panels, or scrolling chains to the page in iOS Safari.

## 4. EPS / patient summary — BUILT, but it is a GENERATOR

The plan asks how far ips is from an EPS Composition. Much closer than it
assumes, with one important caveat.

| module | what it is |
|---|---|
| `gateway/app/services/euips_required.py` | the three REQUIRED sections (#793) — allergies, problems, medications |
| `gateway/app/services/euips_optional.py` | vital signs, social history (tobacco, alcohol), functional status (#795) |
| `gateway/app/services/euips_sections.py` | section assembly |
| `gateway/app/__init__.py:188` | a CLI reporting euIPS section coverage per patient |

Every resource carries narrative `text`, and an absent required section is
emitted as an explicit `absent-unknown-uv-ips` assertion rather than left
empty — which is the euIPS rule the module's own docstring quotes.

**CAVEAT: `required_sections_for()` takes an `rng` and is called only from
`mock_generator.py`.** It *generates* conformant sections for synthetic
patients; it does not compose a summary from stored clinical facts.

**The read path the dashboard needs is the snapshot:**
`IpsCard` → `IpsSnapshot` → `GET /snapshots/<guid>/bundle`, which is an
audited read (`ips_bundle_read`). So the patient card shows the summary **as
of a frozen snapshot**, not live. The plan does not address that; it is a
design decision for Gate 0.

**Conformance, measured in production 2026-10-07 by #791's own code: of 150
patients, 40 were conformant and 110 had no content in ANY required
section.** #793 fixes that for newly generated patients; older snapshots may
still be non-conformant.

## 5. ICD-11 diagnoses — BUILT, and richer than the plan's DTO

`icd11.pdhc` carries a full register:

- `DiagnosisEpisode` — `patient_guid`, `patient_org_guid` (NOT NULL),
  `diagnosis_code_guid`, `clinical_status`, `verification_status`,
  `onset_at`, `abatement_at`, `opened_at`, `closed_at`, `cdr_condition_guid`
- `DiagnosisAssertion` — append-only history: `seq`, `asserted_at`,
  `valid_from`, `entry_path`, `recorder_kind`/`guid`/`name`, `source_system`

Read endpoints already present:

| endpoint | use |
|---|---|
| `GET /patients/<patient_guid>/diagnosis-summary` | **the patient card's chips** |
| `GET /episodes/<guid>/history` | assertion history |
| `GET /encounters/<ref>/diagnoses` | per-encounter |
| `GET /patients` | patient index |
| `GET /used-concepts` | what termbank's importer consumes (#961) |

`cdr.pdhc` additionally holds Condition resource tables
(`cdr_app/app/models/resources.py`, `resource_writer.py`).

**BLOCKER FOR DISPLAY TEXT: termbank holds no ICD-11 concepts yet (#965).**
So `$lookup` returns nothing for an ICD-11 code and a chip can show a code
with no human-readable text until that load happens.

## 6. Plan panel sources — all behind existing endpoints

`request.pdhc` (`gateway/app/api/`):

| endpoint | use |
|---|---|
| `GET /careplans`, `GET /careplans/<guid>` | the CarePlan header and selector |
| `GET /careplans/<guid>/context` | plan context |
| `GET /ServiceRequest`, `/ServiceRequest/<guid>`, `/<guid>/matches` | the plan items |
| `GET /internal/service-request/<sr_guid>/context` | the plan's step-3 fallback — but **NOT CALLABLE FROM dashboard**, see below |
| `GET /requests`, `/requests/<guid>` | request state |

`plan.pdhc` (`planp/app/api/`): `GET /PlanDefinition`,
`GET /PlanDefinition/<fhir_id>`, `GET /concepts`, `GET /concepts/<guid>`,
`GET /concepts/<guid>/values`.

**TWO CORRECTIONS to this section, measured while building the client.**

**1. The `/internal/*` fallback is unreachable from dashboard.** It is guarded
by `requires_service_key` against `INTERNAL_SERVICE_KEY`, which dashboard does
not hold. A client method that always 401s is worse than an absent one, so it
is deliberately not exposed. The plan names this as its provenance fallback;
it is not available to this consumer without an operator issuing a key.

**2. `ServiceRequest.care_plan_guid` never reaches the wire.** The column
exists (`service_request_models.py:28`, "since #310") but `to_dict()` omits it
and `create_service_request` never sets it. **So there is no API-visible edge
from a ServiceRequest to a CarePlan.** The client joins the only way the wire
permits — `patient_guid` plus the transaction guids inside each SR's own
`plan_definition_snapshot` — and a test pins the absence, so the day
request.pdhc starts emitting it, something says a better edge exists.

Two more shapes that differ from the plan's assumptions: `GET /ServiceRequest`
has **no `patient_guid` filter** (and for a non-SU caller the page is scoped to
the caller's first org AND spärr-filtered, so `total` is not a count of what
you can see); and there is **no `completed` ServiceRequest status** — the
measured set is `draft → active → archived` plus `revoked`, and `archived` IS
the done signal. `performed_at` has no direct source; the nearest measured fact
is `ServiceRequestContractMatch.response_at` on a terminal match.

`CarePlan.period_start` is **nullable** (`care_plan_models.py:54`) — exactly
the step 6 risk. Show "—", never substitute another date.

**dashboard has NO client for either service** — only `cdr1_client.py` and
`ips_client.py`. Two new clients are Phase 1 work. plan.pdhc has no usable
machine-auth door (#931), so those reads are unauthenticated by necessity, as
cdr and request already do.

## 6b. request.pdhc's CarePlan routes are NOT org-scoped (#981)

Verified from source, with a control, before filing.
`care_plans.py::list_care_plans` filters on `patient_guid` and `status` and
nothing else — no organisation filter, no spärr — while the ServiceRequest
routes in the SAME service do both (`caller_org_ids & patient_org_guids`, plus
`ips_client.is_sr_visible`).

So the plan panel must NOT assume it inherits Rule 24 from upstream. Scoping
is the dashboard's own job here, which its gate already does. That is defence
in depth, not a substitute — the routes are reachable by anything else holding
a credential. Tracked as #981 in request.pdhc.

## 7. THE CONTEXT JOIN — the plan's premise is wrong, in the dangerous direction

The plan says `observation` → `clinical_context` "currently joins 0 rows".
Measured on cdr1 (`cdr_user`/`cdr_db`), read-only:

| | |
|---|---|
| observations | **1 949** |
| distinct patients | **4** |
| `clinical_context` rows | **1 949** |
| distinct `transaction_guid` in EITHER table | **10** |

So it does not join 0 rows. **It joins 446 439.**

| join key | context rows matched per observation |
|---|---|
| `transaction_guid` | min 32, max 251, **avg 229.1** |
| `transaction_guid` + `patient_guid` | min 1, max 86, avg 76.0 |
| **`ingest_raw_guid`** | **min 1, max 1, avg 1.0** |

**A transaction is a BATCH, not a datapoint** — 10 transactions carry 1 949
observations. So the key everyone reaches for fans out ~229×, and a naive
JOIN in the events query would silently multiply every row in the event list.
That is worse than joining nothing, because it produces *more* data rather
than none.

**Join on `ingest_raw_guid`.** Both tables carry it and it is exactly 1:1.

### Field coverage, measured

| field | populated |
|---|---|
| `observation.transaction_guid` | **1 949 / 1 949 (100 %)** |
| `observation.care_plan_guid` | **0 / 1 949** |
| `clinical_context.care_plan_guid` | **0 / 1 949** |
| `clinical_context.plan_activity_guid` | **1 942 / 1 949 (99.6 %)** — the VISIT (#820) |
| `clinical_context.service_request_guid` | **7 / 1 949 (0.36 %)** |

`observation` has **no `concept_guid` column** — only `code_canonical`, which
is the Path-B embedded-guid form.

### Three consequences the phase tickets must carry

1. **T+ has no origin for any row today.** `care_plan_guid` is 0 on both
   tables, so step 6's "show —" is not an edge case; it is the only path for
   100 % of rows until the datapoint-context work lands.
2. **Step 15's matching key is wrong.** It says highlight a plan item's
   observations by `transactionGuid` — with 10 transactions over 1 949 rows
   that highlights ~195 rows per item, not "its observations". Use
   `plan_activity_guid` (99.6 % populated) or `ingest_raw_guid`.
3. **The plan's ServiceRequest fallback covers 7 rows of 1 949.** It is
   correct to implement and it is not a substitute for the missing context.

### Fixtures: Gate 1 and Gate 4 have no 10 000-row patient

Total is 1 949 observations across 4 patients on cdr1. The p95 and
scroll-performance gates need generated data; a latency figure from 1 949
rows does not clear them. Phase 6's fixtures must therefore come first, which
that ticket already says.

**cdr2 and cdr3 have no repo on this machine (#944)**, so the "merged across
CDRs" requirement cannot be fully verified from here.

## 8. What already exists in dashboard.pdhc, and must not be lost

| route | what |
|---|---|
| `/select` | org-scoped patient picker (#465) |
| `/patient/<guid>/charts` + `/api/v1/patient/<guid>/parameters`, `/series` | per-patient charts (#464, #466) |
| `/nurse/patient/<guid>`, `/events`, `/variable/<canonical>`, `/agp` | nurse workspace; **AGP = the CGM dense-series answer** |
| `/api/v1/designs` | saved designs, owner-private templates (#467) |
| `/audit`, `/audit/export.csv` | PDL access log with audited export |
| `/admin-read-attest` | break-glass attestation (#575/#212) |

Services: `cdr1_client.py`, `ips_client.py`, `audit.py`, `role_guards.py`,
`session_headers.py` (#408), `analyse/federation.py`, `analyse/aggregations.py`.

**`/agp` and saved designs appear NOWHERE in the plan.** Their destiny is a
Gate 0 decision; dropping them by omission is the failure mode here.

## 9. Authorisation — reuse, and carry #827's distinction

`app/auth.py` (route-aware care-delivery vs analysis, #463),
`role_guards.py`, `audit.py`, `ips_client.get_active_blocks`. Nine test
modules cover it.

**`has_blocked_sources` is False both when there is no block AND when ips was
never asked (#827).** `sparr_checked` must travel beside it on every new JSON
response and template. A new endpoint returning only `has_blocked_sources`
reintroduces the defect and will look correct.

Organisation comparison uses the ips `PatientClinicAssignment` →
`Clinic.organisation_guid` fallback, since `patient_org_guid` is not
populated on observations (#774). 28 of 150 patients have no clinic
assignment and cannot pass the care check — reachable only under SU. Say so
in the UI rather than returning an unexplained 403.

---

## Gate 0 — DECIDED by the operator 2026-11-11

| # | decision |
|---|---|
| 1 | **stack: Option A** — Jinja + vanilla JS + Chart.js. No framework, no build step. |
| 2 | **interaction: SELECTION** is the mechanism, hover an accelerator where `hover: hover` holds. Step 19's pin is deleted. |
| 3 | **REPLACE the existing views, but KEEP `/agp` and saved designs.** The new dashboard takes over `/patient/<guid>/charts` and `/nurse/patient/<guid>`; the CGM ambulatory glucose profile and owner-private saved designs (#467) survive as features and are woven in where they fit. Nothing is dropped. |
| 4 | **namespace:** adopt the plan's `/dashboard/...` for the new BFF; the existing `/api/v1/patient/<guid>/...` proxies are retired with the views they serve. |
| 5 | **EPS: LIVE COMPOSITION**, not the frozen snapshot. This is NEW WORK IN ips.pdhc, outside this repo — `required_sections_for()` is a generator for synthetic patients, so a real composer is needed. Tracked in its own ticket in ips.pdhc. |
| 6 | **ICD-11 display text comes from icd11.pdhc, NOT termbank.** Measured and confirmed below; termbank's ICD-11 load (#965) is therefore OFF the critical path. |
| 7 | **vendor `chartjs-plugin-zoom`** as a third UMD bundle with its version recorded (the #471 pattern). |

### Decision 6, measured — icd11 carries the display text

| | |
|---|---|
| `DiagnosisCode.label` | `String(1024)`, **NOT NULL** |
| `DiagnosisCodePart.label` | `String(1024)`, **NOT NULL** — so a postcoordinated cluster renders in full |
| `DiagnosisEpisode`-side `display` | `episode.py:191`, NOT NULL |

And `GET /patients/<patient_guid>/diagnosis-summary` is already more than a
list of codes: it returns a **FHIR Problem List Bundle** via
`fhir.problem_list_bundle()`, with

- `as_of` time travel — the same question answered the same way before and
  after a code change,
- organisation scoping from the caller's access scope,
- a `record_read` audit entry, and
- a `urn:pdhc:scope` meta tag plus `scopeNote` that distinguishes **"this
  patient has no diagnoses"** from **"this patient has diagnoses you are not
  scoped to see"**.

That last one exists because the two were once indistinguishable and sent a
clinician hunting a bug that was not there (2026-10-23). **The dashboard must
surface that scopeNote, not swallow it** — it is the same class of
distinction as #827's `sparr_checked`, and the same failure mode if dropped.

### Decision 5 has a head start

The problems section of a live EPS composition can be taken from this same
`diagnosis-summary` bundle — it IS an IPS Problem List, and problems is one
of euIPS's three required sections. So the new composer in ips.pdhc needs to
build allergies and medications from stored facts, and can delegate problems.

## Still open, and not blocking

## Gate 0 — what needs a decision (superseded — kept for the record)

| | |
|---|---|
| **1. stack** | Option A (Jinja + Chart.js) recommended |
| **2. interaction** | settled: selection, hover as accelerator, pin deleted |
| **3. one view or two** | **OPEN — operator's.** Does the new dashboard replace `/patient/<guid>/charts` and `/nurse/patient/<guid>`, and what happens to `/agp` and saved designs? |
| **4. endpoint namespace** | extend `/api/v1/patient/<guid>/...` or adopt the plan's `/dashboard/...`; pick one |
| **5. EPS liveness** | frozen snapshot or live composition? |
| **6. ICD-11 display text** | ship code-only chips, or block Phase 2 on the termbank load (#965)? |
| **7. zoom** | vendor `chartjs-plugin-zoom`, or drop the zoom requirement? |

## Deferred, deliberately: the cdr1 `substance_resolver` (2026-11-11)

`units.group_series` can open the mmol/L <-> mg/dL bridge only when it knows
the ANALYTE, and cdr1's `code_canonical` is an opaque
`urn:pdhc:concept/<guid>`. A resolver would map that guid through plan.pdhc's
`Concept.canonical_lib` + `canonical_refnumber` (a code system and a code) to
a substance.

**Not built, because it blocks nothing today.** Measured on cdr1: 10 distinct
concepts and **none recorded in more than one unit**. The units in use are
`/d`, `L/min`, `%`, `L` and unitless — no `mmol/L` and no `mg/dL` at all.
Building it now would put a plan.pdhc dependency in the series path for zero
current benefit.

**Without it**, a concentration recorded in both molar and mass units SPLITS
into two series rather than being joined — the correct, safe default: two
honest charts instead of one with a guessed factor.

**It becomes relevant** the day a CDR receives a concentration in mixed units
— for instance when the plan's Phase 6 fixtures (glucose, HbA1c) are loaded
against a real CDR. Re-measure then with:

    SELECT code_canonical, string_agg(DISTINCT value_unit, ',')
      FROM observation GROUP BY 1 HAVING count(DISTINCT value_unit) > 1;

