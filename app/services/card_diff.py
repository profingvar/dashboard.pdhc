"""Display diff between a proposed card version and the endorsed one (#1049).

Contract `plans/patient_app_contract.md` s7.5: proposed rows are shown next
to the currently endorsed rows, with added / removed / changed marked.

THIS IS A DISPLAY DIFF AND NOTHING ELSE (MDR IIa: the dashboard shows data and
records a decision, it computes nothing clinical). It answers one question:
"does what is shown on this line differ from what is shown on that line?". It
does not judge which answer is right, does not sort by meaning, does not hide
an unchanged row and proposes no decision. Rows keep the order cdr1 gave.

Matching, in this order:

1. A row with no `group` is matched on `link_id` (the nth occurrence of a
   link_id to the nth, so a duplicate cannot swallow another row).
2. A repeating-group INSTANCE (rows sharing a `group` value, e.g. one allergy =
   substance + reaction + severity) is matched as a whole:
   a. to the endorsed instance with the SAME `group` value, and then field by
      field on `link_id` (same / changed / added / removed per field);
   b. else to an endorsed instance with IDENTICAL content (every field's value
      and display), which is marked same;
   c. else it is ADDED, and an endorsed instance left over is REMOVED.

   Why 2b/2c and not "pair the first with the first": gateway mints `group` as
   `<receipt guid>/<n>` (contract Deviations, [gateway]), so a new card version
   never carries the endorsed version's group values. Pairing by position would
   mark an allergy as "changed" into a DIFFERENT allergy whenever the patient
   removed one above it. With no stable key, an edited instance is shown as one
   removed and one added: never a claim that two entries are the same entry
   when nothing says so.

Values are compared as shown: `value` and `display` together, JSON-canonical.
"""
from __future__ import annotations

import json
from typing import Callable

SAME, CHANGED, ADDED, REMOVED = "same", "changed", "added", "removed"


def _sig(row: dict) -> str:
    return json.dumps([row.get("value"), row.get("display")],
                      sort_keys=True, ensure_ascii=False, default=str)


def _keyed(rows: list[dict]) -> list[tuple[tuple[str, int], dict]]:
    """(link_id, nth occurrence) for each row, in order."""
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        lid = str(r.get("link_id") or "")
        n = seen.get(lid, 0)
        seen[lid] = n + 1
        out.append(((lid, n), r))
    return out


def _units(rows: list[dict]) -> list[tuple[str, object, list[dict]]]:
    """Split one version into display units, in order of first appearance:
    ("single", (link_id, n), [row]) or ("instance", group, [rows])."""
    units: list = []
    inst: dict[str, list] = {}
    seen: dict[str, int] = {}
    for r in rows:
        grp = r.get("group")
        if grp is None or grp == "":
            lid = str(r.get("link_id") or "")
            n = seen.get(lid, 0)
            seen[lid] = n + 1
            units.append(("single", (lid, n), [r]))
            continue
        grp = str(grp)
        if grp not in inst:
            inst[grp] = []
            units.append(("instance", grp, inst[grp]))
        inst[grp].append(r)
    return units


def _content(rows: list[dict]) -> str:
    return json.dumps(sorted([str(r.get("link_id") or ""), _sig(r)] for r in rows),
                      ensure_ascii=False)


def _line(mark, proposed, endorsed, cell: Callable[[dict], dict]) -> dict:
    return {"mark": mark,
            "proposed": cell(proposed) if proposed is not None else None,
            "endorsed": cell(endorsed) if endorsed is not None else None}


def _field_diff(p_rows, e_rows, cell) -> list[dict]:
    e_by = dict(_keyed(e_rows))
    used = set()
    lines = []
    for key, p in _keyed(p_rows):
        e = e_by.get(key)
        if e is None:
            lines.append(_line(ADDED, p, None, cell))
        else:
            used.add(key)
            lines.append(_line(SAME if _sig(p) == _sig(e) else CHANGED, p, e, cell))
    for key, e in _keyed(e_rows):
        if key not in used:
            lines.append(_line(REMOVED, None, e, cell))
    return lines


def diff(proposed: list[dict], endorsed: list[dict] | None,
         cell: Callable[[dict], dict]) -> list[dict]:
    """Display blocks for one card version.

    Each block is `{"kind": "single"|"instance", "mark", "lines": [...]}`;
    each line is `{"mark", "proposed": cell(row)|None, "endorsed": cell(row)|None}`.
    An instance's mark is `same`, `added`, `removed` or `changed` (when any of
    its lines is not `same`). `cell` turns a raw row into what the template
    shows; it is given every row exactly once.
    """
    endorsed = endorsed or []
    e_units = _units(endorsed)
    e_single = {key: rows[0] for kind, key, rows in e_units if kind == "single"}
    e_inst = {key: rows for kind, key, rows in e_units if kind == "instance"}
    used_single: set = set()
    used_inst: set = set()

    blocks: list[dict] = []
    pending_content: list[tuple[int, list[dict]]] = []   # (block index, rows)
    for kind, key, rows in _units(proposed):
        if kind == "single":
            e = e_single.get(key)
            if e is None:
                line = _line(ADDED, rows[0], None, cell)
            else:
                used_single.add(key)
                line = _line(SAME if _sig(rows[0]) == _sig(e) else CHANGED,
                             rows[0], e, cell)
            blocks.append({"kind": "single", "mark": line["mark"], "lines": [line]})
            continue
        if key in e_inst:                                  # 2a: same group value
            used_inst.add(key)
            lines = _field_diff(rows, e_inst[key], cell)
            mark = SAME if all(ln["mark"] == SAME for ln in lines) else CHANGED
            blocks.append({"kind": "instance", "mark": mark, "lines": lines})
            continue
        blocks.append(None)                                # resolved below
        pending_content.append((len(blocks) - 1, rows))

    for idx, rows in pending_content:                      # 2b: identical content
        want = _content(rows)
        match = next((k for k, er in e_inst.items()
                      if k not in used_inst and _content(er) == want), None)
        if match is not None:
            used_inst.add(match)
            lines = [_line(SAME, p, e, cell) for p, e in
                     zip(rows, _order_like(rows, e_inst[match]))]
            blocks[idx] = {"kind": "instance", "mark": SAME, "lines": lines}
        else:                                              # 2c: added
            blocks[idx] = {"kind": "instance", "mark": ADDED,
                           "lines": [_line(ADDED, p, None, cell) for p in rows]}

    for kind, key, rows in e_units:                        # left over: removed
        if kind == "single" and key not in used_single:
            blocks.append({"kind": "single", "mark": REMOVED,
                           "lines": [_line(REMOVED, None, rows[0], cell)]})
        elif kind == "instance" and key not in used_inst:
            blocks.append({"kind": "instance", "mark": REMOVED,
                           "lines": [_line(REMOVED, None, e, cell) for e in rows]})
    return blocks


def _order_like(p_rows: list[dict], e_rows: list[dict]) -> list[dict]:
    """The endorsed rows of an identical instance, in the proposed rows' order,
    so each line pairs a field with its own counterpart."""
    pool = list(e_rows)
    out = []
    for p in p_rows:
        want = (str(p.get("link_id") or ""), _sig(p))
        for i, e in enumerate(pool):
            if (str(e.get("link_id") or ""), _sig(e)) == want:
                out.append(pool.pop(i))
                break
    return out


def counts(blocks: list[dict]) -> dict:
    """How many LINES carry each mark. For the summary sentence only."""
    out = {SAME: 0, CHANGED: 0, ADDED: 0, REMOVED: 0}
    for b in blocks:
        for ln in b["lines"]:
            out[ln["mark"]] += 1
    return out
