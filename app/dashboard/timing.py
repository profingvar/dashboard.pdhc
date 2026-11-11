"""Pure time-from-plan-start arithmetic and keyset cursors for the event list.

No HTTP, no database, no Flask, no dependencies outside the standard library.
Everything here is a pure function of its arguments, so it is testable without
a CDR, a CarePlan, or a request context — which is the point: the rules below
are clinical rules, and a rule that can only be exercised through three
services is a rule nobody exercises.

Two subjects live here because the event list needs both and neither touches
I/O: the T+ origin, and the cursor that walks the list.


THE T+ ORIGIN RULE, which is the whole reason the first function exists
----------------------------------------------------------------------
`t_from_start_seconds` measures from a CarePlan's `period.start` and from
NOTHING ELSE. When there is no `period.start` the answer is `None`, and the UI
renders `None` as an em dash. It must never substitute "now", the first
observation's time, `created_at`, or any other date that happens to be at
hand: each of those would draw a clinical timeline that nobody authored, and
it would look exactly as authoritative as a real one.

`dto.EventRow.t_from_start_seconds` documents the same rule from the other
side, and `dto`'s correction 3 records that the field is `None` for every row
on the platform today (`care_plan_guid` is populated on 0 of 1 949
observations). So the `None` path is not an edge case to be tidied away — it
is the normal case, and a fallback would currently falsify EVERY row.

An event BEFORE the plan start is legitimate and returns a NEGATIVE number. A
plan can be authored after the data exists — a clinician enrolling a patient
who already has two years of observations is the ordinary case, not an error
— so `None` and `0` are both wrong answers there.


SIGN AND ROUNDING
-----------------
The result is `floor()` of the exact offset: the greatest whole number of
seconds not exceeding it. One definition, two useful consequences:

* a positive offset reads as completed whole seconds (3.9 s -> 3), and
* an event strictly before the plan start can never collapse to `0`
  (-0.4 s -> -1), so the sign always answers "before or after?" truthfully.

Truncation toward zero would break the second property, which is why it is
not used.


PARSING
-------
Both arguments may be ISO-8601 strings or `datetime`/`date` objects, aware or
naive, in any combination. A naive value is TREATED AS UTC; mixing a naive and
an aware value does not raise (plain `datetime` subtraction would).

A value that is present but unparseable RAISES. `None` means "not recorded"
(`dto`'s own rule) and a garbage timestamp is not that — it is a bug in a
producer, and returning `None` for it would hide the bug behind a legitimate
em dash.
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import math
import operator
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "EM_DASH",
    "CURSOR_VERSION",
    "InvalidCursor",
    "Cursor",
    "t_from_start_seconds",
    "format_t_plus",
    "encode_cursor",
    "decode_cursor",
    "keyset_sort_key",
    "paginate",
]

#: U+2014 EM DASH. What `None` renders as — never "0", never "".
EM_DASH = "—"

UTC = _dt.timezone.utc

# fromisoformat accepts 0-6 fractional digits; some producers emit 7 or 9
# (Postgres via a nanosecond-precision client, Java Instants). Trim rather
# than reject: the sub-microsecond part cannot matter to a T+ offset.
_FRACTION = re.compile(r"(\.\d{6})\d+")


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _to_utc(value: Any, *, label: str) -> _dt.datetime | None:
    """Coerce an ISO-8601 string / date / datetime to an AWARE UTC datetime.

    `None` passes through as `None`. A naive input is interpreted as UTC. A
    present-but-unparseable input raises `ValueError`; a wrong type raises
    `TypeError`.
    """
    if value is None:
        return None

    if isinstance(value, _dt.datetime):
        dt = value
    elif isinstance(value, _dt.date):
        # date (not datetime) -> midnight. Checked second: datetime is a
        # subclass of date, so the order of these two branches matters.
        dt = _dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{label}: empty string is not a timestamp")
        candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            dt = _dt.datetime.fromisoformat(candidate)
        except ValueError:
            trimmed = _FRACTION.sub(r"\1", candidate)
            try:
                dt = _dt.datetime.fromisoformat(trimmed)
            except ValueError as exc:
                raise ValueError(
                    f"{label}: {value!r} is not an ISO-8601 timestamp"
                ) from exc
    else:
        raise TypeError(
            f"{label}: expected an ISO-8601 string, date or datetime, "
            f"got {type(value).__name__}"
        )

    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# --------------------------------------------------------------------------
# T+ origin
# --------------------------------------------------------------------------

def t_from_start_seconds(effective_at: Any, period_start: Any) -> int | None:
    """Whole seconds from a CarePlan's `period.start` to an observation time.

    Returns `None` when EITHER input is `None` — and in the `period_start`
    case that is the rule, not a convenience: there is no other date this
    function may measure from. See the module docstring.

    Negative when the event precedes the plan start, which is legitimate.
    `floor()` of the exact offset, so a pre-plan event is never `0`.
    """
    start = _to_utc(period_start, label="period_start")
    if start is None:
        return None
    event = _to_utc(effective_at, label="effective_at")
    if event is None:
        return None
    return math.floor((event - start).total_seconds())


# --------------------------------------------------------------------------
# human form
# --------------------------------------------------------------------------

_SECOND = 1
_MINUTE = 60
_HOUR = 3600
_DAY = 86400

# coarsest first; (seconds in the unit, suffix used in a compound form,
# suffix used when the unit stands alone)
_UNITS: tuple[tuple[int, str], ...] = (
    (_DAY, "d"),
    (_HOUR, "h"),
    (_MINUTE, "min"),
    (_SECOND, "s"),
)


def format_t_plus(seconds: int | None) -> str:
    """Render a T+ offset for the event list.

    THE RULES, in full, because the coding plan fixed only two shapes
    (`T+3d 4h` and `T+42 min`) and the rest had to be decided:

    1.  `None` renders as the em dash `—` (U+2014). Never "0", never "".
        `None` means "there is no plan origin"; "0" would mean "it happened
        at the plan start", and they are not the same statement.
    2.  Sign is carried by the prefix: `T+` for zero and positive, `T-` for
        negative. The magnitude is formatted from `abs(seconds)`, so a plan
        authored two days and one hour after an event reads `T-2d 1h`.
    3.  The magnitude is decomposed into days / hours / minutes / seconds and
        AT MOST TWO components are shown: the largest non-zero unit and the
        next unit down, in that order.
    4.  If that next unit down is zero, the unit stands ALONE, and a lone unit
        is written with a space before a spelled-out suffix:
        `T+45 s`, `T+42 min`, `T+5 h`, `T+3 d`.
    5.  Two components are written compactly, space-separated:
        `T+3d 4h`, `T+4h 30min`, `T+2min 30s`.
    6.  Zero is `T+0 s` by rule 4.

    Rules 4 and 5 differ deliberately: the plan's two required shapes differ
    that way (`T+42 min` is spaced, `T+3d 4h` is not), and matching them is
    worth more than a tidier scheme the UI would then not match.

    Rule 3 makes the string a SUMMARY, not a representation: `T+2 d` is
    returned for 2 days 0 hours 30 minutes, because hours — the next unit
    down — is zero and anything finer is dropped. Never parse this back; use
    the integer.
    """
    if seconds is None:
        return EM_DASH
    if isinstance(seconds, float):
        seconds = math.floor(seconds)
    try:
        total = operator.index(seconds)
    except TypeError as exc:
        raise TypeError(
            f"format_t_plus: expected int or None, got {type(seconds).__name__}"
        ) from exc

    sign = "-" if total < 0 else "+"
    rest = abs(total)

    parts: list[tuple[int, str]] = []
    for index, (size, suffix) in enumerate(_UNITS):
        count, rest = divmod(rest, size)
        if count:
            parts.append((count, suffix))
            # the next unit down, and nothing beyond it
            if index + 1 < len(_UNITS):
                next_size, next_suffix = _UNITS[index + 1]
                next_count = rest // next_size
                if next_count:
                    parts.append((next_count, next_suffix))
            break

    if not parts:                                    # rule 6
        return f"T{sign}0 s"
    if len(parts) == 1:                              # rule 4
        count, suffix = parts[0]
        return f"T{sign}{count} {suffix}"
    return f"T{sign}" + " ".join(                    # rule 5
        f"{count}{suffix}" for count, suffix in parts
    )


# --------------------------------------------------------------------------
# keyset cursors
# --------------------------------------------------------------------------

#: Bumped whenever the payload shape changes. A cursor minted by an older
#: build then decodes to a NAMED error instead of being misread.
CURSOR_VERSION = 1


class InvalidCursor(ValueError):
    """A cursor was malformed, truncated, foreign, or of an unknown version.

    Named, and raised rather than swallowed, for one reason: a cursor that
    quietly restarts at page one is worse than an error. The caller gets rows
    it has already shown, in a list it is walking backwards through time, and
    nothing anywhere says the walk broke — so an infinite scroll silently
    loops instead of ending. A `ValueError` subclass so a caller that wants to
    map it to a 400 can catch either.
    """


@dataclass(frozen=True)
class Cursor:
    """The decoded keyset position: the LAST row of the page just served.

    `effective_at` is canonical — an aware UTC ISO-8601 string — so
    re-encoding a decoded cursor reproduces it byte for byte.

    `observation_guid` IS NOT OPTIONAL. `effective_at` is not unique (a batch
    ingest gives many observations the same instant), and a keyset cursor on a
    non-unique key either skips the rest of a tied group or serves it twice.
    The tiebreak is the whole mechanism, not a refinement of it.
    """
    effective_at: str
    observation_guid: str

    @property
    def effective_dt(self) -> _dt.datetime:
        """The position as an aware UTC datetime."""
        parsed = _to_utc(self.effective_at, label="cursor.effective_at")
        assert parsed is not None            # a Cursor never holds None
        return parsed

    @property
    def sort_key(self) -> tuple[_dt.datetime, str]:
        return (self.effective_dt, self.observation_guid)


def encode_cursor(effective_at: Any, observation_guid: str) -> str:
    """Mint an opaque cursor for the row at (`effective_at`, `observation_guid`).

    The payload is compact JSON, base64url-encoded without padding: opaque to
    the client, in the sense that it carries no promise about its contents and
    invites no hand-editing. It is NOT a secret and NOT tamper-proof — a
    client that decodes it learns a timestamp and a guid it was just served
    anyway. If that ever matters, sign it; do not pretend base64 did.
    """
    if observation_guid is None or not isinstance(observation_guid, str) \
            or not observation_guid.strip():
        raise ValueError(
            "encode_cursor: observation_guid is required and must be a "
            "non-empty string — it is the tiebreak that makes the cursor "
            "correct over a non-unique effective_at"
        )
    moment = _to_utc(effective_at, label="effective_at")
    if moment is None:
        raise ValueError(
            "encode_cursor: effective_at is required — a row with no "
            "effective time has no position in this ordering"
        )
    payload = {
        "v": CURSOR_VERSION,
        "e": moment.isoformat(),
        "g": observation_guid,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> Cursor:
    """Decode a cursor, or raise `InvalidCursor`.

    Every failure mode lands here: not a string, empty, not base64, not
    UTF-8, not JSON, not an object, missing a key, wrong version, a
    timestamp that will not parse. None of them returns page one.
    """
    if not isinstance(cursor, str):
        raise InvalidCursor(
            f"cursor must be a string, got {type(cursor).__name__}"
        )
    text = cursor.strip()
    if not text:
        raise InvalidCursor("cursor is empty")

    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise InvalidCursor("cursor is not valid base64url") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursor("cursor does not decode to JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidCursor("cursor payload is not an object")
    if payload.get("v") != CURSOR_VERSION:
        raise InvalidCursor(
            f"cursor version {payload.get('v')!r} is not {CURSOR_VERSION}"
        )

    moment = payload.get("e")
    guid = payload.get("g")
    if not isinstance(moment, str) or not moment:
        raise InvalidCursor("cursor is missing its effective_at")
    if not isinstance(guid, str) or not guid:
        raise InvalidCursor("cursor is missing its observation_guid tiebreak")

    try:
        parsed = _to_utc(moment, label="cursor.effective_at")
    except (ValueError, TypeError) as exc:
        raise InvalidCursor("cursor effective_at does not parse") from exc
    assert parsed is not None

    return Cursor(effective_at=parsed.isoformat(), observation_guid=guid)


# --------------------------------------------------------------------------
# the ordering the cursor walks
# --------------------------------------------------------------------------

def _field(row: Any, name: str) -> Any:
    """Read `name` off a dataclass/object row or a mapping row."""
    if isinstance(row, Mapping):
        if name not in row:
            raise KeyError(f"row is missing {name!r}")
        return row[name]
    try:
        return getattr(row, name)
    except AttributeError as exc:
        raise KeyError(f"row is missing {name!r}") from exc


def keyset_sort_key(effective_at: Any, observation_guid: str) -> tuple[_dt.datetime, str]:
    """The ordering key: `(effective_at DESC, observation_guid DESC)`.

    Returned ASCENDING; sort with `reverse=True` for newest-first. The guid is
    part of the key, not a decoration on it — see `Cursor`.
    """
    moment = _to_utc(effective_at, label="effective_at")
    if moment is None:
        raise ValueError("keyset_sort_key: effective_at is required")
    return (moment, observation_guid)


def _row_key(row: Any) -> tuple[_dt.datetime, str]:
    return keyset_sort_key(
        _field(row, "effective_at"), _field(row, "observation_guid")
    )


def paginate(
    rows: Iterable[Any],
    limit: int,
    cursor: str | None = None,
) -> tuple[tuple[Any, ...], str | None]:
    """One newest-first page of `rows`, plus the cursor for the next page.

    Pure: `rows` is whatever the caller already has in memory (anything with
    `effective_at` and `observation_guid`, as attributes or mapping keys).
    This is the same comparison a SQL `WHERE` would make, kept here so the
    ordering can be tested without a database:

        (effective_at, observation_guid) < (cursor.effective_at, cursor.guid)

    strictly, as a TUPLE — which is what keeps a tied `effective_at` from
    skipping the rest of its group or repeating it.

    `next_cursor` is `None` only when the page is the last one. An invalid
    cursor raises `InvalidCursor`; it never starts over.
    """
    if limit is None or operator.index(limit) < 1:
        raise ValueError("paginate: limit must be a positive integer")
    limit = operator.index(limit)

    position = decode_cursor(cursor).sort_key if cursor is not None else None

    ordered = sorted(rows, key=_row_key, reverse=True)
    if position is not None:
        ordered = [row for row in ordered if _row_key(row) < position]

    page = tuple(ordered[:limit])
    if len(ordered) > limit and page:
        last = page[-1]
        next_cursor = encode_cursor(
            _field(last, "effective_at"), _field(last, "observation_guid")
        )
    else:
        next_cursor = None
    return page, next_cursor
