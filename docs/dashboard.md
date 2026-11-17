# Patient dashboard — data sources, rules and known gaps

Ticket #974, Phase 6, step 25. Derived from the code as of 2026-11-17. Where a
plan or a ticket says something else, the code is described here and the
difference is listed in [section 7](#7-where-the-code-and-the-plans-disagree).

The dashboard is the page `GET /dashboard/view/<guid>`. It is a shell: it
carries only the patient guid. Every datum arrives through JSON routes, each
of which runs the access gate, spärr and the PDL audit. One failing source
degrades its own panel. It never blanks the page.

Code map:

| file | role |
|---|---|
| [`app/routes/dashboard.py`](../app/routes/dashboard.py) | the BFF: the page and its JSON routes |
| [`app/dashboard/dto.py`](../app/dashboard/dto.py) | wire DTOs (dataclasses, sent as camelCase) |
| [`app/dashboard/access.py`](../app/dashboard/access.py) | the per-patient access gate and spärr state |
| [`app/dashboard/timing.py`](../app/dashboard/timing.py) | T+ arithmetic, keyset cursors |
| [`app/dashboard/units.py`](../app/dashboard/units.py) | series grouping and unit conversion |
| [`app/dashboard/hba1c.py`](../app/dashboard/hba1c.py) | the HbA1c % ↔ mmol/mol affine bridge |
| [`app/dashboard/limits.py`](../app/dashboard/limits.py) | limit verdicts (MDR class IIa) |
| [`app/templates/dashboard.html`](../app/templates/dashboard.html), [`app/static/js/dashboard.js`](../app/static/js/dashboard.js), [`app/static/css/dashboard.css`](../app/static/css/dashboard.css) | the page |
| `app/services/{cdr1,ips,request,plan}_client.py` | the upstream clients |

---

## 1. Routes

| route | panel | upstreams |
|---|---|---|
| `GET /dashboard/view/<guid>` | the page shell | none |
| `GET /dashboard/patients/<guid>` | patient card | cdr1 (patient index), ips (EPS, spärr) |
| `GET /dashboard/patients/<guid>/plan` | plan panel | request, cdr1, ips (spärr), plan (names) |
| `GET /dashboard/patients/<guid>/concepts` | event filter bar | cdr1, plan (names) |
| `GET /dashboard/patients/<guid>/series/<code>` | time series (`?format=csv` for export, `?from=&to=` for a zoom window) | cdr1, ips (spärr), plan (name), request (limits) |
| `GET /dashboard/patients/<guid>/events` | event list (`cursor`, `limit` ≤ 500, `code`, `from`, `to`, `transaction`, `outside=1`) | cdr1, ips (spärr), plan (names), request (limits, CarePlan start) |
| `GET /dashboard/events/<observation_guid>` | event detail | cdr1, ips (spärr), plan (PlanDefinition), request (limits) |

Every per-patient route is wrapped by `dashboard_patient_read`
(`access.py:381`): audit outermost, so refusals are logged too, then
`assert_patient_open`. The event detail route has no patient guid in its URL,
so it fetches first (scoped to the caller's organisations) and then gates on
the observation's own patient (`dashboard.py:1003`).

Refusals are named: `no_basis`, `wrong_role`, `no_care_unit`,
`other_organisation`, `unassigned_patient`, `undetermined` (503 — an
authority could not be reached; not a denial). The organisation check asks
cdr1's patient index (`_patient_orgs`, `dashboard.py:67`), not ips.

Every response that shows patient data carries the #827 pair
`hasBlockedSources` and `sparrChecked`. The filter bar's concept list is
built from the VISIBLE points when the patient has an active block, so a
count, a date range or a concept name never discloses blocked data. A source
that failed is named in
`unavailable` (e.g. `plan:PlanUnreachable`, `limits:RequestUnreachable`); a
field that is `null` means "not recorded", never "could not fetch".

---

## 2. Data source per field

### 2.1 Patient card

`GET /dashboard/patients/<guid>` (`dashboard.py:174`).

| panel | field | source service — endpoint / client method | notes |
|---|---|---|---|
| card | Namn | cdr1 — `GET /api/v1/clinical/patients` via `Cdr1Client.list_org_patients` | Reused from the organisation check (`g._dashboard_patient_row`), not a second call. "—" when the patient is not in the index. |
| card | Ålder | computed in the browser from `birthDate` (same cdr1 row) | "—" when no birth date. |
| card | Kön | **no source** | `PatientCard.sex` is never set. Always "—". |
| card | Personnummer | **no source** | Always "—". |
| card | Organisation | **no source** | Always "—". |
| card | Diagnoser (chips) | ips — `GET /api/v1/patients/<guid>/euips-summary` via `IpsClient.euips_summary`, the section with `key == "problems"` | Since #984 the only source. `code`, `display`, `status`, `onset`, `abatement`, `date`, `system`, `cdr_backed`, `cdr` are read from each entry. `verificationStatus` is always `null`. A chip with `cdrBacked: false` is marked "ej förankrad i CDR". icd11.pdhc is **not** called. |
| card | diagnosis note | derived from the problems section | Three states: `diagnosesDefinitivelyEmpty` `null` = ips failed (unknown), `true` = section is `EXPLICITLY_ABSENT`, `false` with no chips = section does not say. |
| card | Patientsammanfattning (EPS) | ips — same `euips-summary` body, all sections | Each section → `EpsSection` (`_eps_section`, `dashboard.py:107`). An explicitly-absent section shows its absent-assertion text. |
| card | spärr banner | ips — `GET /api/v1/patients/<guid>/blocks?active=true` via `get_active_blocks(strict=True)` (`sparr_state`, `access.py`) | `sparrChecked: false` when `IPS_BASE_URL` is unset; ips configured but unreachable → 503 `undetermined` on every patient read (#974). |
| card | unavailable banners | `unavailable` list | `eps:unreachable:…`, `eps:patient-not-in-ips-scope` (ips 404). |

### 2.2 Plan panel

`GET /dashboard/patients/<guid>/plan` (`dashboard.py:248`).

| panel | field | source service — endpoint / client method | notes |
|---|---|---|---|
| plan | plans | request — `RequestClient.plan_summaries`: `GET /api/v1/careplans?patient_guid=`, `GET /api/v1/careplans/<guid>/context`, `GET /api/v1/ServiceRequest` (page 1, ≤ 200 rows, filtered to the patient client-side), `GET /api/v1/ServiceRequest/<guid>/matches` | A ServiceRequest no CarePlan claims becomes its own plan (`source: "service_request"`, `build_order_summary`). On production that is every plan (0 CarePlans). Failure → `plansKnown: false`, banner "okända, inte tomma". |
| plan | name / version | the frozen `plan_definition_snapshot` (`title` or `name`, `version`) on the CarePlan or ServiceRequest | Not read from plan.pdhc. |
| plan | status | CarePlan or ServiceRequest `status` | SR statuses: `draft`, `active`, `archived` (= done), `revoked`. |
| plan | beställd | `ServiceRequest.created_at` | A fact about the order. Never a T+ origin. |
| plan | start (T0) | `CarePlan.period_start`, or `ServiceRequest.period_start` for an order | "inget startdatum — ingen T+" when null. |
| plan | item state (Klara / Försenade / Planerade / Avbrutna) | `request_client.derive_item_state` (`request_client.py:235`) | From the order's status and its `period_start` only. `overdue` needs a scheduled time in the past and a non-terminal status. Observations are never consulted. |
| plan | item `utförd` | latest `response_at` of a `completed`/`accepted` contract match | "—" when none. |
| plan | observation count per item | cdr1 — `GET /api/v1/clinical/patient/<guid>/series` via `Cdr1Client.patient_events`, up to 10 pages × 500 | Counted per `transaction_guid`, after spärr. Past the bound `observationCountsComplete: false` and counts show as "≥ n". |
| plan | Mätningar utan beställning | same cdr1 scan; transactions no order covers | Display name from plan — `concept_display_names` (`GET /api/v1/concepts`, cached), else the code. |
| plan | observations without a transaction | same scan | `observationsWithoutTransaction`. |

### 2.3 Event list and filter bar

`GET /dashboard/patients/<guid>/events` (`dashboard.py:774`) and
`GET /dashboard/patients/<guid>/concepts` (`dashboard.py:378`).

| panel | field | source service — endpoint / client method | notes |
|---|---|---|---|
| events | Mätning (name) | plan — `concept_display_names` over `/api/v1/concepts`, keyed by the guid in `code_canonical` (`urn:pdhc:concept/<guid>`) | Falls back to `code_canonical`; plan failure → `plan:…` in `unavailable`. |
| events | Värde, enhet | cdr1 — `patient_events` (`value`, else `value_string`; `unit`) | Newest first, cdr1's own cursor passed through. |
| events | Tid | cdr1 — `at` | Shown in Europe/Stockholm. "odaterad" when absent. |
| events | T+ | see [section 3](#3-the-t-definition) | "—" for every production row today. |
| events | ×n badge | cdr1 — `GET /api/v1/clinical/patient/<guid>/summary` via `patient_summary` (per-concept count over the whole history) | Badge when count ≥ 2. Summary failure with points present → `cdr1:summary`. |
| events | limit flag (▲ över / ▼ under) | `limits.evaluate` against the patient's CarePlans from request — `GET /api/v1/careplans?patient_guid=` via `fetch_care_plans` | See [section 5](#5-limits). |
| events | `sourceCdr` | constant `"cdr1"` | The BFF reads only cdr1. |
| filter bar | concept list | cdr1 — `/summary` via `patient_concepts` (strict); names from plan | cdr1 failure → 503 `cdr1_unreachable`, not an empty list. |
| filter bar | Från / Till | validated in the BFF (`_bound`, `dashboard.py`) | A bare date is a Stockholm calendar day. A bad value is a 400, never a silently dropped filter. |
| filter bar | Plan | the transaction guids of the selected plan, sent to cdr1 as `transaction=` | cdr1 must echo `transaction_filter: true`, else 503 `upstream_filter_unsupported`. |
| filter bar | Utanför gräns | `outside=1`: cdr1 is asked only for transactions with a usable limit, every page scanned (20 × 500), judged after spärr | CarePlans unreadable → 503 `limits_unknown`. No limits → `noLimits: true`. |

### 2.4 Event detail

`GET /dashboard/events/<observation_guid>` (`dashboard.py:1003`). Every field
is listed; an empty one renders "ej registrerat".

| panel | field | source service — endpoint / client method | notes |
|---|---|---|---|
| detail | Värde, Tid, Kod, Observation | cdr1 — `GET /api/v1/clinical/observation/<guid>` via `observation_detail` | 404 for "not in scope", "does not exist" and "blocked by spärr" alike. |
| detail | Referensintervall (labb), Tolkning, Metod, Utrustning | cdr1 — `raw_fhir` (`referenceRange`, `interpretation`, `method`, `device`) | 0 of 1 949 cdr1 rows carry a reference range or interpretation (measured 2026-11-12). |
| detail | T+ | not computed by this route | Always "—" in the detail panel. |
| detail | PlanDefinition, version | plan — `GET /api/v1/PlanDefinition/<id>` via `PlanClient.get_plan_definition`, only when the context has `plan_definition_guid` | |
| detail | Vårdplan | cdr1 context `care_plan_guid` | ~0 % populated. |
| detail | Beställning registrerad på mätvärdet | cdr1 context `service_request_guid` | 7 of 1 949 rows. |
| detail | Besök (plan activity) | cdr1 context `plan_activity_guid` | 1 942 of 1 949 rows. |
| detail | Transaktion | cdr1 context `transaction_guid` | |
| detail | Kontrakt | cdr1 context `contract_guid` | |
| detail | Patientens organisation | **no source** — set to `null` on purpose (`dashboard.py:1112`) | Not recorded per datapoint (#774). The provider organisation is not substituted. |
| detail | Utförande organisation | cdr1 context `provider_org_guid` | |
| detail | Mottagen, Ingest-id | cdr1 `received_at`, `ingest_raw_guid` | |
| detail | Begrepp | derived from `code_canonical` (`concept_guid_from_canonical`) | |
| detail | limit block | `limits.evaluate` | Same verdict as the row and the chart point. |
| detail | notes | cdr1 `context_found`, `context_filled_from_raw` | "Ingen kontextrad hittades" when cdr1 found none. |
| detail | Rå FHIR Observation | cdr1 `raw_fhir` | |

### 2.5 Time series

`GET /dashboard/patients/<guid>/series/<code>` (`dashboard.py:473`). Drawn for
the selected event's concept.

| panel | field | source service — endpoint / client method | notes |
|---|---|---|---|
| series | points | cdr1 — `patient_events` with `code=`, up to 20 pages × 500 | Numeric points only; the rest are counted in `nonNumericCount`. `complete: false` past the bound. |
| series | display name | plan — `concept_display_names` | |
| series | unit split | `units.group_series` | See [section 4](#4-the-series-grouping-rule). |
| series | point marker (triangle) | `limits.evaluate` per point, in the series unit | |
| series | limit band | `_band` (`dashboard.py:621`) | Only when exactly one limit governs every judged point and converts to the series unit. |
| series | downsampling | LTTB above 2 000 points, server side | Every point outside its limit is kept on top of the LTTB selection. The panel says "Visar N av M". |
| series | T0 marker, T+ axis | the plan panel's `periodStart` | Drawn only when exactly one plan has a `periodStart` (`t0()`, `dashboard.js:1271`). |
| series | done markers | plan items with state `done` and a `performedAt` | |
| series | diagnosis onset / abatement markers | the card's diagnoses (EPS) | |

---

## 3. The T+ definition

T+ is the time from a **CarePlan's `period.start`** to the observation's
effective time. Nothing else is an origin.

- `timing.t_from_start_seconds(effective_at, period_start)`
  (`timing.py:142`) returns whole seconds, `floor()` of the exact offset.
- `None` when either input is `None`. The UI shows "—". It never substitutes
  now, the first observation, `created_at` or any other date.
- An event before the plan start is negative (`T−…`). `floor()` keeps a
  pre-start event from rounding to 0.
- A present but unparseable timestamp raises; it is a producer bug, not "not
  recorded".

Where it is computed:

- **Event list:** only for a point that carries `care_plan_guid`. The BFF
  then reads `GET /api/v1/careplans/<guid>` (`fetch_care_plan`) and uses its
  `period_start` (`dashboard.py:903`). A point without `care_plan_guid` gets
  `null`.
- **Event detail:** not computed; always "—".
- **Series axis:** T0 is the plan panel's `periodStart`. For an order that is
  `ServiceRequest.period_start`. The axis shows whole days from T0 and is
  drawn only when exactly one plan has a start.

Display: the browser formats seconds itself (`tPlus`, `dashboard.js:624`):
`T+3d 4h`, `T+4h 30m`, `T+42m`. `timing.format_t_plus` (`T+42 min`,
`T+45 s`) exists and is tested but is not used by the page.

## 4. The series grouping rule

Two points belong to one series when their `code_canonical` is equal **and**
their units are convertible. Otherwise the concept splits into one series per
unit class, and the panel says why (`units.group_series`, `units.py:831`).

- The identity is `code_canonical`. There is no `concept_guid` column on a CDR
  observation (`dto.py`, correction 1).
- Conversion uses a small closed table, not a UCUM library. An unknown unit
  refuses rather than guessing; a missing unit and an unknown unit never merge.
- **Refused:** mmol/L ↔ mg/dL unless the analyte is known; any affine relation
  (`Cel` ↔ `[degF]`); calendar units (`mo`, `a`) ↔ durations.
- **Molar-mass bridge** (mmol/L ↔ mg/dL): only for glucose and creatinine, and
  only when the substance is known from a LOINC code in `_LOINC_SUBSTANCE`
  (`units.py:420`).
- **HbA1c bridge** (% ↔ mmol/mol): joined through the NGSP master equation in
  `hba1c.py`, not a factor. Charted in mmol/mol. Same LOINC condition.
- The series unit is the unit most points arrived in, then the class's
  preferred unit, then alphabetical.
- Points sort by `(effective_at, observation_guid)`; series by
  `(code_canonical, unit)`.

**On cdr1 neither bridge opens.** cdr1's `code_canonical` is
`urn:pdhc:concept/<guid>`, which names no substance, and the route calls
`group_series(rows)` without a `substance_resolver` (`dashboard.py:547`). So a
concept recorded in mmol/L and mg/dL shows as two series. This is the
deliberate deferral in [`plans/dashboard_sources.md`](../plans/dashboard_sources.md)
("Deferred, deliberately: the cdr1 `substance_resolver`").

## 5. Limits

Limits come only from the patient's CarePlans in request.pdhc: the CarePlan's
goal first, else the range in its frozen PlanDefinition. Orders are not a
source. Every verdict is made in `limits.py`; the browser never compares a
value to a limit.

Requirements, traceability, data dependencies and risk notes are in
[`docs/limits_requirements_and_risks.md`](limits_requirements_and_risks.md).
They are not repeated here.

---

## 6. Known context gaps

These are facts about the data and the wiring today, not defects in the page.
Each is visible in the UI rather than hidden.

| gap | effect on the page | reference |
|---|---|---|
| Observation context is mostly empty. The cdr1 `observation` ↔ `clinical_context` join is 1:1 on `ingest_raw_guid`, but `care_plan_guid` is populated on 0 of 1 949 rows and `service_request_guid` on 7 (measured 2026-11-11). | Event detail shows "ej registrerat" for Vårdplan and Beställning on almost every row. Event T+ is "—" for every row. | [`plans/dashboard_sources.md`](../plans/dashboard_sources.md) §7; `dto.py` corrections 2–3 |
| The patient's organisation is not recorded per datapoint. | "Patientens organisation" is always "ej registrerat". | #774 |
| Card demographics: sex, personnummer and organisation have no source the dashboard reads. | Always "—". | `dashboard.py:209` |
| No ServiceRequest has `period_start` (0 of 28, measured 2026-11-12). | No plan item is ever "Försenad", no T0 marker and no T+ axis appear, and every plan reads "inget startdatum — ingen T+". | `request_client.build_order_summary` |
| Production has 0 CarePlans, and limits come only from CarePlans. | Every verdict is `no_limit`; "Utanför gräns" always answers "inga gränser". | [`docs/limits_requirements_and_risks.md`](limits_requirements_and_risks.md) §3 |
| The BFF reads cdr1 only. cdr2 and cdr3 are not read. The Gate 0 report says they have no repo on this machine (#944). | Data held only in cdr2/cdr3 does not appear; `sourceCdr` is always `cdr1`. A diagnosis in cdr2 reaches the card only through the EPS. | #944 |
| The organisation check uses cdr1's patient index, which lists only patients **with observations**. | A care-basis user opening a patient of their own unit who has no observations is refused `other_organisation`. `unassigned_patient` is not reachable through these routes. | `dashboard.py:67–97` |
| The ServiceRequest list is read as one page (≤ 200 rows), filtered to the patient client-side; request.pdhc has no `patient_guid` filter. | A patient's orders beyond the first page would be missing. | `request_client.fetch_service_requests` |
| request.pdhc's CarePlan routes carry no org scope and no spärr. | The dashboard's own gate is the only scope on those reads. | #981 |
| No dark theme. | `dashboard.css` and `pdhc.css` define no `prefers-color-scheme` rules; the page is light only. | — |
| The iPad pass, including the limit markings, is open. | — | #988 |

---

## 7. Where the code and the plans disagree

The code wins. Each item is what the code does.

1. **Diagnoses come from the EPS, not icd11.pdhc.** Gate 0 decision 6 and
   `app/dashboard/__init__.py` say the ICD-11 display text comes from
   icd11.pdhc. Since #984 the card reads only the EPS problems section from
   ips.pdhc. `icd11_client` is not imported by any route. The `Diagnosis`
   docstring in `dto.py` still says `system` is fixed to the WHO MMS URI; it
   now carries whatever system the EPS entry has (SNOMED CT or ICD-11).
2. **The template still labels the chips "ICD-11-register"**
   (`dashboard.html:27`), and no script replaces the label. The source is the
   EPS.
3. **The context join does not "join 0 rows".** The coding plan (and the
   ticket's gap list) say so. Gate 0 measured 446 439 rows on
   `transaction_guid` (a fan-out) and exactly 1:1 on `ingest_raw_guid`. What
   is empty is the context's fields, chiefly `care_plan_guid`.
4. **Plan items are matched to observations by `transaction_guid`.**
   `dto.py` correction 4 and Gate 0 §7 say `plan_activity_guid` is the usable
   key. The plan panel's counts and the event list's highlighting still group
   by `transaction_guid`, which is shared by every patient on a plan; the
   patient half of the key is the patient-scoped cdr1 read.
5. **The organisation check uses cdr1, not ips.** Gate 0 §9 says the
   patient's organisation is resolved from ips's clinic assignment. The code
   asks cdr1's patient index (see section 6).
6. **T+ format.** The page's `tPlus` differs from `timing.format_t_plus`
   (`T+42m` versus `T+42 min`). The Python formatter is unused.

---

## 8. Running locally with fixtures

The fixture layer (`app/dashboard/fixtures.py`) holds three SYNTHETIC patients
and a fake for each upstream client. Each fake subclasses the real client and
replaces only the method that opens a socket, so the real assembly code runs.
Every upstream base URL becomes `fixture://<source>`, which `requests`
refuses, so nothing reaches the network.

| setting | meaning |
|---|---|
| `DASHBOARD_FIXTURES=1` | install the fixture clients |
| `AUTH_MODE=off` | **required.** With auth on, `create_app` raises `FixturesRefused`, so fixture data can never answer a real session |
| `DASHBOARD_FIXTURES_FAIL=<list>` | comma list of `cdr1`, `ips`, `request`, `plan`, `icd11`; each named source raises the exception its real client raises when unreachable. An unknown name is an error |
| `DASHBOARD_FIXTURES_ANCHOR=<ISO>` | pins the dates (default: today 00:00 UTC). Planned/overdue is judged against the real clock |
| `FIXTURES_PORT` | runner port, default `9029` (dashboard's block is 9026–9029) |

Run:

```
scripts/run_fixtures.sh            # 127.0.0.1:9029, throwaway sqlite, prints the patient URLs
DASHBOARD_FIXTURES_FAIL=ips,plan scripts/run_fixtures.sh
```

| patient guid | exercises |
|---|---|
| `fa4e0974-000a-4000-8000-000000000001` | type 2 diabetes: HbA1c goal ≤52 exceeded, systolic 100–135, weight with no limit, free text (`not_numeric`), done / overdue / planned plan items, unordered measurements, most rows WITHOUT transaction or care-plan context |
| `fa4e0974-000a-4000-8000-000000000002` | heart failure: daily weight, NT-proBNP with a PlanDefinition range; spärr hides two results; one EPS-only diagnosis not backed by any CDR |
| `fa4e0974-000a-4000-8000-000000000003` | CGM: 4 032 glucose points, downsampled by LTTB with every outside value kept; postcoordinated cluster `5A11&XT8W` (the extension is illustrative) |

Names start with "Fixtur" and every personnummer has an INVALID check digit
(#405, tested).

What failing a source does (pinned by tests): `plan` or `request` fail →
that panel's `unavailable` entry, the rest renders; `ips` fails → spärr
cannot be verified, so every patient read answers a named 503 `undetermined`
(fail CLOSED; the older chart/nurse views still fail open); `cdr1` fails → the
event list and series answer a named 503 `cdr1_unreachable` by design, and
the card names `cdr1:patient-index`; `icd11` → no effect, because no route
reads icd11 since #984. A scan that runs past `DASHBOARD_SCAN_BUDGET_S`
(default 60 s, below gunicorn's 120 s) stops and reports itself incomplete.

In pytest: the fixtures `fixtures_app` / `fixtures_client`
(`app/tests/conftest.py`); fail a source in one test with
`fixtures.set_failing(fixtures_app, "ips")` or
`with fixtures.failing(fixtures_app, "plan", "request"):`.

## 9. Tests

```
app/.venv/bin/python -m pytest -q app/tests
```

The dashboard modules are `app/tests/test_dashboard_*.py` (access, card,
detail, events, filters, hba1c, limit routes, limits, page, plan, routes,
series, timing, units), plus the #974 fixture-layer modules
(`test_dashboard_fixtures_974`, `_panels_974`, `_resilience_974`,
`test_concepts_sparr_974`, `test_scan_budget_974`, `test_outside_filter_goal_974`).

Browser tests against the fixture runner (Playwright + axe, desktop Chromium
and iPad portrait/landscape in WebKit):

```
cd e2e && npm run test:fixtures
```

Use the npm script, not a bare `npx playwright test`: it sets
`PLAYWRIGHT_BROWSERS_PATH` to `~/Library/Caches/ms-playwright-user` (the
default cache is root-owned and holds older builds). The config starts
`scripts/run_fixtures.sh` on 127.0.0.1:9029 and refuses any other base URL;
the resilience spec starts its failing-source runners on 9028. The anchor is
today 00:00 UTC unless `DASHBOARD_FIXTURES_ANCHOR` is set. Screenshots and
axe JSON land in `results/phase6/` (git-ignored).

Not covered by a headless run (operator's real-iPad pass): Safari's first-tap
hover, a hover device attached at runtime, two-finger pinch/pan, real
vertical drag over the chart, momentum scroll, native date pickers,
VoiceOver, and contrast inside the chart canvas.

The older `e2e/playwright.config.ts` + `specs/` default to
`https://dashboard.pdhc.se` and authenticate as the retired `monitor.pdhc`
(#727); they are not the fixture suite and will be refused.
