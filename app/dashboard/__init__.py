"""The patient dashboard (the operator's coding plan, Gate 0 decided 2026-11-11).

Gate 0's decisions, because they are load-bearing for everything here:

* Stack is Jinja + vanilla JS + Chart.js. No framework, no build step.
* SELECTION is the interaction mechanism; hover is an accelerator only where
  `(hover: hover) and (pointer: fine)` holds. The plan's "pin" is deleted —
  it existed only because hover is transient.
* The new view REPLACES `/patient/<guid>/charts` and `/nurse/patient/<guid>`,
  but `/agp` and saved designs (#467) survive as features.
* EPS is composed LIVE, not read from a frozen snapshot (ips.pdhc work).
* ICD-11 display text comes from icd11.pdhc, not termbank.
"""
