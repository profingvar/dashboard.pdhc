# Legal / DPO review request — spärr indispensable-care *lift* filtering on the CDR1 clinical dashboard

**Status:** ✅ APPROVED 2026-08-07 — Q1–Q4 answered; Q1 parse acceptable as-is, Q2/Q3 confirmed, Q4 special audit required (spec in §9). Build gated. · **Owner ticket:** #472 (legal question) · **Implements:** #471 item 4 · **Related:** #437 (legal gate), #462 (dashboard rebuild), #241/#242 (spärr sign-offs)
**Prepared:** 2026-08-06 (engineering) · **For:** Data Protection Officer / legal counsel

---

## 1. What we are asking you to decide

The rebuilt clinical dashboard reads patient observation data from CDR1 and enforces patient **spärr** (blocks) on its own side. We want to refine that enforcement so it applies the **indispensable-care lift** (*nödvändig vård*) exception — i.e. it would **show** a small set of otherwise-blocked data points when an active lift covers them.

Because this change moves in the **data-exposing** direction (revealing data a block would otherwise hide), we will **not** build it until you confirm it is lawful and specify any conditions. Four specific questions are in §5. Until then the dashboard keeps today's safe, over-hiding behaviour.

This is **not a new policy.** The exposure rule below was legally confirmed on **2026-06-04** and is already live on the platform's legacy read path. This request is about applying **the same confirmed rule** to the new CDR1 read path, and about one genuinely new implementation detail (Q1).

---

## 2. Legal framework (for your confirmation)

- **Spärr / block** — under the Patient Data Act (Patientdatalagen, PDL); the platform's code cites **PDL 4 kap. 4 §** as the basis. A patient may block a care provider/unit from accessing their records within cohesive record-keeping (*sammanhållen journalföring*).
- **Indispensable-care lift** (*nödvändig vård*) — the exception under which specifically-scoped data may still be accessed despite a block when it is indispensable for the patient's care.
- **GDPR** — the exposure and its audit trail must remain compliant (lawful basis, data minimisation, logging).

Please confirm the statutory basis and that the mechanism in §4 sits within the *nödvändig vård* exception as applied to this clinical read.

---

## 3. Current behaviour (the safe baseline)

If a patient has an **active block** on a care unit, the dashboard drops **every** data point sourced from that unit. This **over-hides**: it also denies clinicians the specific data an indispensable-care lift is meant to expose. It is safe (nothing over-shared) but clinically incomplete.

---

## 4. Proposed mechanism (please confirm acceptable)

A blocked point is **exposed** (shown despite the block) **only if both** of these hold:

1. its clinical concept is listed in an **active** indispensable-care lift's `lift_concept_guids`, **and**
2. its date falls within that lift's validity window `[lift_from_date, lift_until_date]`.

This is **identical** to the rule already implemented and running on the legacy path (`ips_client.filter_blocked_rows` / `_row_passes_any_lift`).

**How the point's concept is identified (the new detail):** the CDR1 observation row does **not** carry an authoritative `concept_guid` column. Instead, the concept identity is **derived by parsing** it out of the row's `code_canonical` field, whose production format is `urn:pdhc:concept/<concept-guid>` (verified 2026-07-15: 7064 of 7065 rows conform).

**Fail-safe fallback:** any point whose `code_canonical` is **not** a parseable concept GUID (e.g. an external terminology URI or a legacy code) produces **no** concept match and therefore **stays hidden** under the block. Ambiguity always resolves toward hiding (the safe, under-exposing direction).

**Activation:** if approved, the exposing behaviour will be built **gated** so it only activates deliberately (not silently on by default).

---

## 5. Questions for decision

> **Q1 — Concept identity by parsing.**
> Is deriving the concept identity by **parsing `code_canonical`** (`urn:pdhc:concept/<guid>`) an acceptable basis for this legally-mandated lift decision — or must a lift decision use an **authoritative stored `concept_guid`**?
> *Engineering note:* parsing is technically reliable (7064/7065 rows conform, non-conforming rows fail safe to hidden). Requiring an authoritative column is possible but is a larger change (making `concept_guid` a trusted field on the observation read path first). This is the **only new** question.

> **Q2 — Rule unchanged.**
> Confirm the exposure rule in §4 (concept ∈ active lift's `lift_concept_guids` **and** date ∈ `[lift_from, lift_until]`) is **unchanged** from the 2026-06-04 confirmation.

> **Q3 — Safe fallback.**
> Is the fail-safe fallback acceptable — a point with a non-parseable `code_canonical` **stays hidden** under a block (under-exposes, never over-exposes)?

> **Q4 — Audit of exposures.**
> Should a read where a lift **exposes** otherwise-blocked data be **specially audited** (distinct event type / justification), beyond the normal read audit? If yes, please specify what must be recorded (e.g. event type, lift reference, retention, whether patient notification applies).

---

## 6. Safeguards already in place / committed

- Every dashboard read is already logged via `@audit_read` (X1 extended access log: purpose + access_basis + role).
- The change is **fail-safe by construction** (any doubt → hidden; §4 fallback).
- It will ship **gated**, so exposing behaviour is a deliberate, revertible activation.
- No change to *who* can access — only to *which* points are filtered for an already-authorised clinical reader; the exposure is limited to lift-covered concepts within the lift window.

---

## 7. On approval

On a yes to Q1–Q4 (with any Q4 audit conditions), this becomes a small, low-risk port of an already-proven rule to the CDR1 path — implementable in a focused session, behind the activation gate. On a no to Q1 (authoritative identity required), we will first scope making `concept_guid` a reliable column before building.

---

## 8. Sign-off

| Reviewer | Role | Date | Decision |
|---|---|---|---|
| _(countersignature)_ | DPO / legal counsel | 2026-08-07 | Q1: ☑ **parse acceptable as-is** · Q2: ☑ **confirmed** · Q3: ☑ **acceptable** · Q4: ☑ **special audit required** — record spec in §9 |

**Conditions / notes:** Approved as specified. Build **gated** (exposing behaviour activates deliberately). Q4 imposes the distinct audit record in §9 on every lift exposure. Decisions recorded 2026-08-07; a named counsel countersignature line is retained above for the formal file copy.

---

## 9. Q4 — required audit record for a lift exposure (approved)

Per Q4, **every** read where an indispensable-care lift **exposes** otherwise-blocked data must be written as a **distinct** audit event (separate from the ordinary `@audit_read` event), recording the following fields:

| Required field | What must be recorded for each lift exposure |
|---|---|
| **Event type** | A distinct audit event type (e.g. `sparr_lift_exposure`) marking that data otherwise hidden by a spärr block was exposed under an indispensable-care lift — separable in queries from ordinary read events. |
| **Lift reference** | The active lift that authorised it: the lift identifier, the matched `lift_concept_guid`(s), and the validity window `[lift_from_date, lift_until_date]`; together with the blocked care-unit, the patient, and the reading clinician + role. |
| **Retention** | Held under the platform access-log retention (weekly append to T9 + rotation, #321). **DPO to confirm** whether spärr-override records require a longer statutory minimum than the standard access log. |
| **Patient notification** | Whether and when the patient is informed that lifted data was exposed despite their block, and via which channel (e.g. patient portal). **DPO to specify** the trigger and channel. |

Two cells above are marked **DPO to confirm/specify** — the retention duration and the notification trigger/channel are the only values still needed to make §9 fully executable; the event type and lift-reference fields are ready to implement as-is.

---

*References: tickets #472 (this question), #471 (dashboard cutover), #437 (admin off-org read legal gate), #241 (clinical-lead runbook sign-off), #242 (patient-facing spärr copy legal sign-off), #321 (access-log retention); the 2026-06-04 indispensable-care lift confirmation; `plans/sparr_implementation_plan.md`; legacy implementation `ips_client._row_passes_any_lift`.*
