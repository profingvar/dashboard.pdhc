# Limits on values — requirements, traceability and risk notes

**Status:** working draft, 2026-11-12. Input to the MDR technical file, **not**
a substitute for it. Owner of the clinical content: the operator / responsible
clinician. Owner of this document: whoever changes `app/dashboard/limits.py`.

## 1. Context and decision

Operator decision 2026-11-12: the patient dashboard (`/dashboard/view/<guid>`)
**shall indicate limits** on a patient's values, and dashboard.pdhc will be
marked **MDR class IIa** (Rule 11: software that applies thresholds to an
individual patient's data to support clinical decisions).

Second operator decision, same day: **the limit is the one set in the
patient's CarePlan** — the CarePlan's **goal** for the measurement first (goals
are inherited from the PlanDefinition and editable per patient), and only where
there is no usable goal, the **range in the CarePlan's frozen PlanDefinition**.
Limits frozen into ServiceRequest (order) snapshots are **not** a source. Every
limit shown says which of the two it is.

This makes dashboard.pdhc a regulated product for this function. The rules in
this document apply to every change that touches limit logic, its wiring, or
how a verdict is displayed.

### Intended use of the limit function (draft — to be confirmed by the operator)

> Shows a healthcare professional, for one patient, whether each recorded
> numeric value lies inside or outside the limit set for that measurement in
> the patient's care plan, together with where that limit came from.
> It does not raise alerts, does not notify anyone, and does not recommend
> treatment. The professional remains responsible for interpretation.

Out of scope of this function: alerting (none exists on the platform; see
the plan/request split), laboratory reference ranges (none are recorded —
section 3), and any automatic action.

## 2. Requirements and traceability

Every requirement maps to code and to at least one test. A test listed here was
**mutation-checked**: the rule was deliberately broken and the test turned red
(2026-11-12). Two tests failed that check in their first version and were
rewritten (now `REQ-LIM-14`, `REQ-LIM-15`) — a green test is not evidence until it
has been shown to fail.

| ID | Requirement | Code | Tests |
|---|---|---|---|
| REQ-LIM-01 | A value is compared to a limit **only** in `app/dashboard/limits.py`. The browser renders the verdict and never compares. | `limits.evaluate`; JS `outOfRange` reads `pt.ls` only | `test_the_page_NEVER_compares_a_value_to_a_limit` |
| REQ-LIM-02 | Limits come from **this patient's CarePlans** only. Another patient's CarePlan never supplies one; ServiceRequest (order) snapshots are not a source. | `limits_from_care_plans`; `_patient_limits` reads `fetch_care_plans` | `test_another_patients_care_plan_never_supplies_a_limit`, `test_ORDERS_are_not_a_source` |
| REQ-LIM-03 | Only CarePlans **in force** apply (`active`, `on-hold`, `completed`); `draft`, `revoked`, `entered-in-error` do not. | `APPLICABLE_CARE_PLAN_STATUSES` | `test_a_care_plan_not_in_force_supplies_no_limit`, `test_care_plans_in_force_supply_limits` |
| REQ-LIM-04 | The CarePlan's **goal** for the concept takes precedence over the PlanDefinition range; the range applies only when the CarePlan has no usable goal for it. The source is stated. | `PatientLimits.candidates` | `test_a_GOAL_overrides_the_PlanDefinition_range`, `test_a_patients_GOAL_drives_the_row_verdict`, `test_the_limit_names_its_CarePlan_and_PlanDefinition` |
| REQ-LIM-05 | A goal becomes a limit only from `range` or from a `>=`/`>`/`<=`/`<` operator; `=` and categorical goals are **targets**, not limits. Two one-sided goals combine. | `_goal_limit` | `test_an_EQUALS_or_categorical_goal_is_a_target_not_a_limit_and_the_range_applies`, `test_two_one_sided_goals_combine_into_a_range` |
| REQ-LIM-06 | Range bounds are **inclusive**; a goal's `<`/`>` is **exclusive** and `<=`/`>=` inclusive, exactly as written. | `evaluate` | `test_range_bounds_are_INCLUSIVE`, `test_operators_and_INCLUSIVITY` |
| REQ-LIM-07 | The goal is found through **both** the CarePlan's mapping of the transaction and the concept in the value's code; two different goals reached that way are a conflict, never a choice. | `PatientLimits.candidates` | `test_the_goal_is_matched_through_the_transactions_concept`, `test_a_goal_is_found_even_when_the_codes_concept_differs_from_the_plans`, `test_two_different_goals_reached_by_the_two_concept_ids_are_a_conflict` |
| REQ-LIM-08 | A value is compared **in the limit's unit**, only through the units module's safe conversion; a refused conversion or a missing value unit is **not judged** (`unit_mismatch`). A goal without its own unit uses the concept's unit. | `evaluate` → `units.convert`; `_goal_limit` | `test_the_goals_own_unit_is_used_and_converted`, `test_the_goal_unit_falls_back_to_the_concepts_unit`, `test_an_unconvertible_unit_is_NOT_judged`, `test_a_value_with_no_unit_is_NOT_judged` |
| REQ-LIM-09 | An unusable limit (non-numeric, low > high, no unit, contradictory units, two different goals for one bound) is **not used** (`invalid_limit`) and the reason is stated. | `_goal_limit`, `_range_limit` | `test_an_unusable_limit_is_NOT_judged_and_says_why` (9 cases) |
| REQ-LIM-10 | Two CarePlans setting **different** limits are a `conflict`: not judged, both reported. Identical limits agree. | `evaluate` (`distinct`) | `test_two_care_plans_with_DIFFERENT_limits_are_a_conflict`, `test_two_care_plans_with_the_SAME_limit_agree`, `test_a_goal_in_one_plan_and_the_range_in_another_DIFFERENT_is_a_conflict` |
| REQ-LIM-11 | CarePlans that **could not be read** make every verdict `unknown` — never `no_limit`; the "outside" filter then answers 503, never an empty list. | `evaluate(limits=None)`; events route | `test_care_plans_that_could_not_be_read_are_UNKNOWN_not_no_limit`, `test_unreadable_orders_make_every_row_UNKNOWN_not_no_limit`, `test_outside_filter_with_unreadable_orders_is_503_not_empty` |
| REQ-LIM-12 | Every verdict that is not `inside`/`below`/`above` carries a reason, stated in words on the page. | `Verdict.reason`; JS `LIMIT_TEXT` | `test_no_limit_is_NOT_inside`, `test_text_is_NOT_judged`, `test_every_not_judged_state_has_words` |
| REQ-LIM-13 | Every limit shown names its source (goal vs PlanDefinition range), CarePlan and PlanDefinition version, labelled apart from the order registered on the value. | `Limit`; `wire`; JS `limitSource`, `renderLimit` | `test_wire_carries_everything_the_page_shows`, `test_the_detail_shows_the_limit_FIRST_with_its_order` |
| REQ-LIM-14 | Downsampling (LTTB) **never** removes a value outside its limit. | series route (`keep |= ...OUTSIDE`) | `test_downsampling_NEVER_hides_an_outside_value` |
| REQ-LIM-15 | A shaded band is drawn **only** when exactly one limit governs every judged point; otherwise none, with the reason. | `_band` | `test_two_DIFFERENT_judged_limits_in_one_series_give_NO_band`, `test_conflicting_limits_give_NO_band` |
| REQ-LIM-16 | An outside value is marked by **symbol and word** (▲ över / ▼ under; triangle in the chart), never colour alone. | JS `rowNode`, `draw` | `test_an_outside_row_is_marked_by_SYMBOL_and_WORD_not_colour_alone`, `test_colour_is_never_the_only_signal` |
| REQ-LIM-17 | "Utanför gräns" returns only outside values, judged **after** spärr, over the whole history (bounded, stated), asking cdr1 only for limited transactions (ranges and goal concepts) through the echoed filter. | events route (`outside_only`); `limited_transactions` | `test_outside_filter_returns_ONLY_outside_values_and_asks_for_ranged_tx`, `test_outside_filter_with_NO_limits_says_so`, `test_limited_transactions_cover_ranges_and_goal_concepts` |
| REQ-LIM-18 | Rows, the detail and series points carry the same verdict for the same value. | `wire` in all three routes | `test_every_row_carries_its_verdict`, `test_detail_carries_the_verdict`, `test_series_points_carry_verdicts_and_one_limit_gives_a_band` |

## 3. Data the function depends on (measured 2026-11-12, production, read-only)

| Fact | Value |
|---|---|
| cdr1 observations with `referenceRange` or `interpretation` in raw FHIR | **0 of 1 949** |
| ServiceRequest transactions with a limit | 27 entries, 5 distinct transactions |
| … with a unit (`unit` or `concept_unit_name`) | 27 of 27 (`unit` itself only on 3) |
| … numeric | 27 of 27 |
| cdr1 observations on a transaction that has a limit | **0** |
| **CarePlans in production** (the only limit source) | **0** |

The ServiceRequest rows above are kept as the measurement that led to the
decision; orders are no longer a source. With no CarePlans, production **today
shows no verdict other than `no_limit`**.
It is verified on synthetic data only, until orders with limits and
observations on them coexist.

## 4. Risk notes (ISO 14971-style, draft)

| # | Hazardous situation | Cause | Control in this release | Verified by |
|---|---|---|---|---|
| R1 | A value outside its limit is shown as normal | a "not judged" state rendered as inside | every non-judged state is distinct and worded; only `below`/`above` mark | REQ-LIM-11, -12 |
| R2 | A value compared in the wrong unit | e.g. mg/dL against a mmol/L limit | conversion only via `units.convert`, refusals not judged | REQ-LIM-08 |
| R3 | Another patient's or a withdrawn plan's limit applied | transaction ids are shared across patients | patient + CarePlan status filter | REQ-LIM-02, -03 |
| R4 | One of two conflicting limits silently chosen | several CarePlans, or a goal reached by two concept ids | `conflict`, both shown, no band | REQ-LIM-07, -10, -15 |
| R5 | An outside value hidden in a dense chart | LTTB keeps shape, not meaning | outside points always kept | REQ-LIM-14 |
| R6 | "Nothing outside" shown when the care plans could not be read | request.pdhc outage | `unknown` + 503 on the filter | REQ-LIM-11 |
| R7 | A spärr-blocked value revealed by the outside filter or a count | judging before spärr | spärr applied before judging and counting | REQ-LIM-17 |
| R8 | The limit is read as a lab reference range, or a template range as the patient's own goal | unlabelled band / source | source stated (goal vs PlanDefinition range), CarePlan and version | REQ-LIM-13 |
| R9 | Colour-blind or angled-tablet reader misses an outside value | colour-only marking | symbol + word + shape | REQ-LIM-16 |
| R11 | A patient's tightened goal is ignored in favour of the template range | goal not matched (concept id mismatch) | goal looked up by both concept ids; found 2026-11-12 and fixed | REQ-LIM-04, -07 |
| R12 | A target value (`=`) treated as a limit | goal semantics | only range / `>=` `>` `<=` `<` make a limit | REQ-LIM-05 |
| **R10** | **The limit itself is clinically wrong** | authoring error in the PlanDefinition / order | **NOT controlled by this software.** Shown with provenance so it can be traced. See open item O1 | — |

## 5. Open items before clinical use

- **O1 — the `bp_diastolic` template range.** 80–120 mmHg in the live orders
  (#987, plan.pdhc). Operator decision 2026-11-12: the limit is the one set in
  the patient's CarePlan — so the orders' value no longer drives the dashboard.
  It still matters: a CarePlan created from that PlanDefinition inherits it as
  the fallback range wherever the clinician sets no goal, so the template
  value should be reviewed before CarePlans are created from it.
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
