# Limits on values — requirements, traceability and risk notes

**Status:** working draft, 2026-11-12. Input to the MDR technical file, **not**
a substitute for it. Owner of the clinical content: the operator / responsible
clinician. Owner of this document: whoever changes `app/dashboard/limits.py`.

## 1. Context and decision

Operator decision 2026-11-12: the patient dashboard (`/dashboard/view/<guid>`)
**shall indicate limits** on a patient's values, and dashboard.pdhc will be
marked **MDR class IIa** (Rule 11: software that applies thresholds to an
individual patient's data to support clinical decisions).

This makes dashboard.pdhc a regulated product for this function. The rules in
this document apply to every change that touches limit logic, its wiring, or
how a verdict is displayed.

### Intended use of the limit function (draft — to be confirmed by the operator)

> Shows a healthcare professional, for one patient, whether each recorded
> numeric value lies inside or outside the limit set for that measurement in
> the patient's own issued order, together with where that limit came from.
> It does not raise alerts, does not notify anyone, and does not recommend
> treatment. The professional remains responsible for interpretation.

Out of scope of this function: alerting (none exists on the platform; see
the plan/request split), laboratory reference ranges (none are recorded —
section 3), and any automatic action.

## 2. Requirements and traceability

Every requirement maps to code and to at least one test. A test listed here was
**mutation-checked**: the rule was deliberately broken and the test turned red
(2026-11-12). Two tests failed that check in their first version and were
rewritten (`REQ-LIM-11`, `REQ-LIM-12`) — a green test is not evidence until it
has been shown to fail.

| ID | Requirement | Code | Tests |
|---|---|---|---|
| REQ-LIM-01 | A value is compared to a limit **only** in `app/dashboard/limits.py`. The browser renders the verdict and never compares. | `limits.evaluate`; JS `outOfRange` reads `pt.ls` only | `test_the_page_NEVER_compares_a_value_to_a_limit` |
| REQ-LIM-02 | The limit is the one frozen in **this patient's** order. An order for another patient never supplies a limit. | `limits_from_orders` (`patient_guid` check) | `test_another_patients_order_never_supplies_a_limit` |
| REQ-LIM-03 | Only **issued** orders are in force (`active`, `archived`, `completed`); `draft` and `revoked` are not. | `APPLICABLE_ORDER_STATUSES` | `test_a_draft_or_revoked_order_is_not_in_force`, `test_issued_orders_are_in_force` |
| REQ-LIM-04 | Bounds are **inclusive**: `low <= v <= high` is inside. A one-sided limit judges one side only. | `evaluate` | `test_bounds_are_INCLUSIVE`, `test_a_one_sided_limit` |
| REQ-LIM-05 | A value in another unit is compared **in the limit's unit**, and only through the units module's safe conversion. A conversion that is refused is **not judged** (`unit_mismatch`). A value without a unit is not judged. | `evaluate` → `units.convert` | `test_a_convertible_unit_is_compared_in_the_LIMITs_unit`, `test_an_unconvertible_unit_is_NOT_judged`, `test_a_value_with_no_unit_is_NOT_judged` |
| REQ-LIM-06 | A limit that is not numeric, has low > high, has no unit, or whose transaction and concept units disagree is **not used** (`invalid_limit`) and the reason is stated. | `_one_limit` | `test_an_unusable_limit_is_NOT_judged_and_says_why` (5 cases) |
| REQ-LIM-07 | Two orders setting **different** limits on the same measurement are a `conflict`: not judged, both limits reported. Identical limits agree. | `evaluate` (`distinct`) | `test_two_orders_with_DIFFERENT_limits_are_a_conflict_not_a_choice`, `test_two_orders_with_the_SAME_limit_agree` |
| REQ-LIM-08 | Orders that **could not be read** make every verdict `unknown` — never `no_limit`. The "outside" filter then answers 503, never an empty list. | `evaluate(limits=None)`; `_patient_limits`; events route | `test_orders_that_could_not_be_read_are_UNKNOWN_not_no_limit`, `test_unreadable_orders_make_every_row_UNKNOWN_not_no_limit`, `test_outside_filter_with_unreadable_orders_is_503_not_empty` |
| REQ-LIM-09 | Every verdict that is not `inside`/`below`/`above` carries a reason, and the page states it in words. | `Verdict.reason`; JS `LIMIT_TEXT` | `test_no_limit_is_NOT_inside`, `test_every_not_judged_state_has_words` |
| REQ-LIM-10 | Every limit shown names its **order** and **PlanDefinition (version)**, and is labelled apart from the order registered on the value itself. | `Limit`; `wire`; JS `renderLimit` | `test_the_limit_carries_its_ORDER_and_PlanDefinition`, `test_the_detail_shows_the_limit_FIRST_with_its_order` |
| REQ-LIM-11 | Downsampling (LTTB) **never** removes a value that is outside its limit. | series route (`keep |= ...OUTSIDE`) | `test_downsampling_NEVER_hides_an_outside_value` |
| REQ-LIM-12 | A shaded band is drawn **only** when exactly one limit governs every judged point of the series; otherwise none, with the reason. | `_band` | `test_two_DIFFERENT_judged_limits_in_one_series_give_NO_band`, `test_conflicting_limits_give_NO_band` |
| REQ-LIM-13 | An outside value is marked by **symbol and word** (▲ över / ▼ under; triangle in the chart), never by colour alone. | JS `rowNode`, `draw` | `test_an_outside_row_is_marked_by_SYMBOL_and_WORD_not_colour_alone`, `test_colour_is_never_the_only_signal` |
| REQ-LIM-14 | The "Utanför gräns" filter returns only outside values, judged **after** spärr, over the whole history (bounded scan, stated when the bound is hit), asking cdr1 only for limited transactions through the echoed filter. | events route (`outside_only`) | `test_outside_filter_returns_ONLY_outside_values_and_asks_for_ranged_tx`, `test_outside_filter_with_NO_limits_says_so` |
| REQ-LIM-15 | Event rows, the detail and series points all carry the same verdict for the same value. | `wire` in all three routes | `test_every_row_carries_its_verdict`, `test_detail_carries_the_verdict`, `test_series_points_carry_verdicts_and_one_limit_gives_a_band` |

## 3. Data the function depends on (measured 2026-11-12, production, read-only)

| Fact | Value |
|---|---|
| cdr1 observations with `referenceRange` or `interpretation` in raw FHIR | **0 of 1 949** |
| ServiceRequest transactions with a limit | 27 entries, 5 distinct transactions |
| … with a unit (`unit` or `concept_unit_name`) | 27 of 27 (`unit` itself only on 3) |
| … numeric | 27 of 27 |
| cdr1 observations on a transaction that has a limit | **0** |

So in production **today the function shows no verdict other than `no_limit`**.
It is verified on synthetic data only, until orders with limits and
observations on them coexist.

## 4. Risk notes (ISO 14971-style, draft)

| # | Hazardous situation | Cause | Control in this release | Verified by |
|---|---|---|---|---|
| R1 | A value outside its limit is shown as normal | a "not judged" state rendered as inside | every non-judged state is distinct and worded; only `below`/`above` mark | REQ-LIM-08, -09 |
| R2 | A value compared in the wrong unit | e.g. mg/dL against a mmol/L limit | conversion only via `units.convert`, refusals not judged | REQ-LIM-05 |
| R3 | Another patient's or a withdrawn order's limit applied | transaction ids are shared across patients | patient + order status filter | REQ-LIM-02, -03 |
| R4 | One of two conflicting limits silently chosen | multiple orders | `conflict`, both shown, no band | REQ-LIM-07, -12 |
| R5 | An outside value hidden in a dense chart | LTTB keeps shape, not meaning | outside points always kept | REQ-LIM-11 |
| R6 | "Nothing outside" shown when the orders could not be read | request.pdhc outage | `unknown` + 503 on the filter | REQ-LIM-08 |
| R7 | A spärr-blocked value revealed by the outside filter or a count | judging before spärr | spärr applied before judging and counting | REQ-LIM-14 |
| R8 | The limit is read as a lab reference range | unlabelled band | labelled as the order's limit, with order + PlanDefinition | REQ-LIM-10 |
| R9 | Colour-blind or angled-tablet reader misses an outside value | colour-only marking | symbol + word + shape | REQ-LIM-13 |
| **R10** | **The limit itself is clinically wrong** | authoring error in the PlanDefinition / order | **NOT controlled by this software.** Shown with provenance so it can be traced. See open item O1 | — |

## 5. Open items before clinical use

- **O1 — clinically suspicious limit in production.** `bp_diastolic` is
  80–120 mmHg in the live orders: 70 mmHg would read "under" and 115 mmHg
  "inside". Probably synthetic or a typo; must be reviewed by the responsible
  clinician before the function is used on real patients. Filed as **#987**
  (plan.pdhc), 2026-11-12.
- **O2 — intended use (section 1)** must be confirmed by the operator and
  carried into labelling and the user manual.
- **O3 — iPad verification** of the marking (ticket #986, item 1).
- **O4 — SOUP list** for the IEC 62304 file: Chart.js 4.4.6,
  chartjs-adapter-date-fns 3.0.0 (date-fns version unestablished),
  chartjs-plugin-zoom 2.2.0, hammer.js 2.0.8 — see
  `app/static/vendor/VERSIONS.md`; Python dependencies in `requirements.txt`.
- **O5 — software safety class** (IEC 62304 A/B/C) to be assigned; the
  controls above assume at least B.
- **O6 — change control:** any change to `limits.py` or its wiring updates
  this table, adds a test, and records the mutation check.
