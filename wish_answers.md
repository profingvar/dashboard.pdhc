# wish_answers.md — clarifications before bring-up (dashboard.pdhc)

Answer inline under each `**A:**`. Leave blank to defer.

---

## On top_rules.md

### 1. Rule 6 — "relies on gateway.pdhc". Confirm dashboard reads observations via gateway.pdhc HTTP API (not directly from observation.pdhc DB). Which gateway base URL for local dev? 
**A:** Observations are delivered to gateway.pdhc.se from the different providers. It is those observations that should be used. 

### 2. Rule 5 + Rule 10 — own Postgres on localhost. Suggest DB name `dashboard_pdhc_db` on port 9026 per Rule 16. OK?
**A:** ok

### 3. Rule 16 reserves 9026–9029. Proposed: 9026=Postgres, 9027=Flask app, 9028/9029=reserved. OK?
**A:** ok

### 4. Rule 11 — fresh `./results/` (new repo, nothing to archive). Correct?
**A:** ok

### 5. Rule 23 — local SU bootstrap for first boot, then SSO afterwards (dual mode), like other PDHC repos?
**A:** like the others

### 6. Rule 24 — patient↔org membership lookup: query gateway, query SSO, or query observation.pdhc?
**A:** preferrably only gateway, the others are lookup if needed

### 7. Rule 15 FHIR R5 — dashboard is read-only. Does compliance apply to (a) data consumed from gateway, (b) any internal API exposed, or (c) both? CapabilityStatement required for read-only UI?
**A:** The graåhs may be re-exposed elsewhere so they should be addressed via API.

### 8. Rule 12 — bring-up local-Mac-only first; no ssh/scp until you say so. Confirm.
**A:** correct

---

## On Wish.md

### 9. "Eligible patients" — eligibility = all patients in user's org with ≥1 observation? Or filtered by active SR? Or other?
**A:** first suggestion

### 10. Concept names source — gateway response, plan.pdhc concept registry, or local cache?
**A:** gateway response, and if needed for confimattion plan.pdhc

### 11. "Date of latest observation" — per concept per patient, or one latest-overall per patient row? (Drives landing-page table shape.)
**A:** Latest one overall

### 12. "Select up to two concepts for time activity curves" — on the landing (cohort overlay) page, or only inside the patient dashboard?
**A:** I want up to two time activity curves. you decide. The rest of data listed per concept in this first instance

### 13. Patient dashboard panels — beyond curves, what to show? Demographics from IPS? Active SRs? Recent values table? Range bands from PlanDef?
**A:** Present the patient in a readable format. 

### 14. Chart library — Chart.js, Plotly, or vanilla SVG? Match an existing PDHC look (request.pdhc / contract.pdhc style)?
**A:** WHatever is practicle

### 15. SSO listing — register in sso.pdhc registry same as other repos. Display name + slug (suggest `dashboard` / "Dashboard")?
**A:**

### 16. "Leave open for development without login before web deploy" — confirm local dev = no auth, server = SSO + Rule 24 org-scoping. Single env flag (e.g. `AUTH_MODE=off|sso`)?
**A:** yes

### 17. Data freshness — live pull on each request, or cache observations in own DB? If cached, refresh trigger (poll interval, webhook, manual)?
**A:** Single well exposed button for refresh (for simplicity)

### 18. Observation source — does dashboard need to support multiple observation services later, or just observation.pdhc via gateway for now?
**A:** for now just gateway

---

When done, say "wish answered" and I'll draft `readme.md` (numbered deployment plan), seed `progress.md` and `changed_files.md`, and lay out the project skeleton.
