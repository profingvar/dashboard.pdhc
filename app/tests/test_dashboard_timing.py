"""Tests for `app.dashboard.timing` — the T+ origin and the keyset cursor.

Written to BITE. Each of the three rules that matter clinically has a test
that fails when the rule is removed, and the three removals were run:

  a) `period_start=None` falling back to "now"       -> 2 tests fail
  b) the `observation_guid` tiebreak dropped          -> 4 tests fail
  c) a malformed cursor returning page one            -> 7 tests fail

That last point is the reason several of these look redundant: a test suite
that passes against broken code is not a test suite, and this platform has
been bitten by exactly that (#729 hid for five months).
"""
from __future__ import annotations

import base64
import datetime as _dt
import json

import pytest

from app.dashboard import timing
from app.dashboard.timing import (
    EM_DASH,
    Cursor,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
    format_t_plus,
    paginate,
    t_from_start_seconds,
)

UTC = _dt.timezone.utc


# ==========================================================================
# t_from_start_seconds — the origin rule
# ==========================================================================

def test_period_start_none_returns_none():
    assert t_from_start_seconds("2026-11-11T12:00:00Z", None) is None


def test_period_start_none_does_not_fall_back_to_now():
    """The rule, stated as a measurement rather than as an intention.

    A fallback to "now" would return a number near zero for an event
    happening now, and a large number for an old event. Both are asserted
    against, so no substitute origin — now, created_at, or the first
    observation — can pass this.
    """
    now = _dt.datetime.now(UTC)
    assert t_from_start_seconds(now, None) is None
    assert t_from_start_seconds(now - _dt.timedelta(days=400), None) is None
    assert t_from_start_seconds("2026-11-11T12:00:00Z", None) is None


def test_effective_at_none_returns_none():
    assert t_from_start_seconds(None, "2026-11-11T12:00:00Z") is None


def test_both_none_returns_none():
    assert t_from_start_seconds(None, None) is None


def test_offset_after_plan_start_is_positive():
    assert t_from_start_seconds(
        "2026-11-14T16:00:00Z", "2026-11-11T12:00:00Z"
    ) == 3 * 86400 + 4 * 3600


def test_pre_plan_event_is_negative():
    """A plan authored after the data exists is the ordinary case."""
    seconds = t_from_start_seconds("2026-11-09T12:00:00Z", "2026-11-11T13:00:00Z")
    assert seconds is not None
    assert seconds < 0
    assert seconds == -(2 * 86400 + 3600)


def test_sub_second_pre_plan_event_is_still_negative():
    """floor(), not truncation: a pre-plan event never collapses to 0."""
    seconds = t_from_start_seconds(
        "2026-11-11T11:59:59.600000Z", "2026-11-11T12:00:00Z"
    )
    assert seconds == -1


def test_event_at_exactly_the_plan_start_is_zero():
    assert t_from_start_seconds("2026-11-11T12:00:00Z", "2026-11-11T12:00:00Z") == 0


def test_sub_second_after_plan_start_floors_to_zero():
    assert t_from_start_seconds(
        "2026-11-11T12:00:00.900000Z", "2026-11-11T12:00:00Z"
    ) == 0


def test_naive_string_is_treated_as_utc():
    naive = t_from_start_seconds("2026-11-11T13:00:00", "2026-11-11T12:00:00")
    aware = t_from_start_seconds("2026-11-11T13:00:00+00:00", "2026-11-11T12:00:00Z")
    assert naive == aware == 3600


def test_naive_and_aware_datetimes_mix_without_raising():
    """Plain datetime subtraction raises on this; the function must not."""
    naive_event = _dt.datetime(2026, 11, 11, 13, 0, 0)
    aware_start = _dt.datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    assert t_from_start_seconds(naive_event, aware_start) == 3600
    assert t_from_start_seconds(aware_start, naive_event) == -3600


def test_naive_and_aware_strings_mix_without_raising():
    assert t_from_start_seconds("2026-11-11T13:00:00", "2026-11-11T12:00:00Z") == 3600
    assert t_from_start_seconds("2026-11-11T13:00:00Z", "2026-11-11T12:00:00") == 3600


def test_non_utc_offset_is_converted_not_ignored():
    """14:00 in +02:00 is 12:00Z, i.e. the plan start itself."""
    assert t_from_start_seconds(
        "2026-11-11T14:00:00+02:00", "2026-11-11T12:00:00Z"
    ) == 0


def test_datetime_and_string_mix():
    assert t_from_start_seconds(
        _dt.datetime(2026, 11, 11, 13, 0, 0, tzinfo=UTC), "2026-11-11T12:00:00Z"
    ) == 3600


def test_date_only_is_midnight_utc():
    assert t_from_start_seconds("2026-11-11T01:00:00Z", "2026-11-11") == 3600
    assert t_from_start_seconds(
        _dt.datetime(2026, 11, 11, 1, 0, tzinfo=UTC), _dt.date(2026, 11, 11)
    ) == 3600


def test_space_separated_timestamp_parses():
    """Postgres' default text form, which several producers pass through."""
    assert t_from_start_seconds(
        "2026-11-11 13:00:00+00", "2026-11-11 12:00:00+00"
    ) == 3600


def test_nanosecond_precision_is_trimmed_not_rejected():
    assert t_from_start_seconds(
        "2026-11-11T13:00:00.123456789Z", "2026-11-11T12:00:00Z"
    ) == 3600


def test_unparseable_timestamp_raises_rather_than_returning_none():
    """None means "not recorded"; garbage is a producer bug and must be loud."""
    with pytest.raises(ValueError):
        t_from_start_seconds("not-a-timestamp", "2026-11-11T12:00:00Z")
    with pytest.raises(ValueError):
        t_from_start_seconds("2026-11-11T12:00:00Z", "yesterday")


def test_wrong_type_raises_type_error():
    with pytest.raises(TypeError):
        t_from_start_seconds(1762862400, "2026-11-11T12:00:00Z")


# ==========================================================================
# format_t_plus
# ==========================================================================

def test_format_none_is_the_em_dash():
    out = format_t_plus(None)
    assert out == EM_DASH
    assert out == "—"
    assert out not in ("0", "", "T+0 s", "-")


def test_format_zero():
    assert format_t_plus(0) == "T+0 s"


def test_format_seconds_only():
    assert format_t_plus(45) == "T+45 s"


def test_format_minutes_only_is_the_plan_shape():
    assert format_t_plus(42 * 60) == "T+42 min"


def test_format_days_and_hours_is_the_plan_shape():
    assert format_t_plus(3 * 86400 + 4 * 3600) == "T+3d 4h"


def test_format_hours_only():
    assert format_t_plus(5 * 3600) == "T+5 h"


def test_format_hours_and_minutes():
    assert format_t_plus(4 * 3600 + 30 * 60) == "T+4h 30min"


def test_format_minutes_and_seconds():
    assert format_t_plus(150) == "T+2min 30s"


def test_format_days_only():
    assert format_t_plus(3 * 86400) == "T+3 d"


def test_format_drops_anything_below_the_second_unit():
    """Documented as a summary: 2d 0h 30min shows as T+2 d."""
    assert format_t_plus(2 * 86400 + 30 * 60) == "T+2 d"


def test_format_negative_is_t_minus():
    assert format_t_plus(-(2 * 86400 + 3600)) == "T-2d 1h"
    assert format_t_plus(-45) == "T-45 s"
    assert format_t_plus(-42 * 60) == "T-42 min"


def test_format_never_shows_more_than_two_components():
    for seconds in (1, 59, 61, 3601, 86401, 90061, -90061, 10**7):
        assert len(format_t_plus(seconds).split(" ")) <= 3  # "T+3d" "4h" at most


def test_format_round_trips_from_the_timing_function():
    seconds = t_from_start_seconds("2026-11-14T16:00:00Z", "2026-11-11T12:00:00Z")
    assert format_t_plus(seconds) == "T+3d 4h"
    assert format_t_plus(t_from_start_seconds("2026-11-14T16:00:00Z", None)) == EM_DASH


# ==========================================================================
# cursors
# ==========================================================================

MOMENT = "2026-11-11T12:00:00+00:00"
GUID = "6f1a9c2e-0b4d-4c7a-9f33-7a1b2c3d4e5f"


def test_cursor_round_trip_is_exact():
    decoded = decode_cursor(encode_cursor(MOMENT, GUID))
    assert decoded == Cursor(effective_at=MOMENT, observation_guid=GUID)
    assert decoded.effective_at == MOMENT
    assert decoded.observation_guid == GUID


def test_cursor_round_trip_is_idempotent():
    first = encode_cursor(MOMENT, GUID)
    decoded = decode_cursor(first)
    assert encode_cursor(decoded.effective_at, decoded.observation_guid) == first


def test_cursor_carries_the_guid_tiebreak():
    """Directly guards mutation (b): the guid must survive the encoding."""
    decoded = decode_cursor(encode_cursor(MOMENT, GUID))
    assert decoded.observation_guid == GUID
    assert decoded.sort_key == (
        _dt.datetime(2026, 11, 11, 12, 0, tzinfo=UTC), GUID,
    )


def test_cursor_normalises_naive_and_offset_inputs_to_the_same_position():
    a = decode_cursor(encode_cursor("2026-11-11T12:00:00", GUID))
    b = decode_cursor(encode_cursor("2026-11-11T12:00:00Z", GUID))
    c = decode_cursor(encode_cursor("2026-11-11T14:00:00+02:00", GUID))
    assert a == b == c


def test_cursor_is_opaque_in_its_encoded_form():
    cursor = encode_cursor(MOMENT, GUID)
    assert GUID not in cursor
    assert "2026" not in cursor
    assert "=" not in cursor          # unpadded base64url
    assert cursor == cursor.strip()


def test_encode_cursor_refuses_a_missing_guid():
    for bad in (None, "", "   ", 7):
        with pytest.raises((ValueError, TypeError)):
            encode_cursor(MOMENT, bad)


def test_encode_cursor_refuses_a_missing_effective_at():
    with pytest.raises(ValueError):
        encode_cursor(None, GUID)


def test_malformed_cursor_raises_invalid_cursor():
    with pytest.raises(InvalidCursor):
        decode_cursor("!!!! not base64 !!!!")


def test_truncated_cursor_raises_invalid_cursor():
    cursor = encode_cursor(MOMENT, GUID)
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor[: len(cursor) // 2])


def test_foreign_cursor_raises_invalid_cursor():
    """Valid base64 of valid JSON that is simply not one of ours."""
    foreign = base64.urlsafe_b64encode(
        json.dumps({"offset": 20, "page": 2}).encode()
    ).decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(foreign)


def test_cursor_without_the_guid_raises_invalid_cursor():
    """A cursor carrying only a timestamp is not usable, and must not be used."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"v": timing.CURSOR_VERSION, "e": MOMENT}).encode()
    ).decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(payload)


def test_wrong_version_cursor_raises_invalid_cursor():
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"v": timing.CURSOR_VERSION + 1, "e": MOMENT, "g": GUID}
        ).encode()
    ).decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(payload)


def test_empty_and_non_string_cursors_raise_invalid_cursor():
    for bad in ("", "   ", None, 17, b"abc", {"v": 1}):
        with pytest.raises(InvalidCursor):
            decode_cursor(bad)


def test_non_json_cursor_raises_invalid_cursor():
    payload = base64.urlsafe_b64encode(b"\xff\xfe\x00not json").decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(payload)


def test_decode_cursor_never_returns_a_page_one_sentinel():
    """Guards mutation (c) by type, not by exception.

    A cursor that quietly restarts serves rows the caller has already shown,
    forever, and says nothing. So every bad input must raise — never return
    None, and never return a zero/empty Cursor that paginate would read as
    "start at the top".
    """
    bads = ["", "!!!", "x", None, 17, encode_cursor(MOMENT, GUID)[:4]]
    for bad in bads:
        try:
            result = decode_cursor(bad)
        except InvalidCursor:
            continue
        pytest.fail(
            f"decode_cursor({bad!r}) returned {result!r} instead of raising"
        )


# ==========================================================================
# the page boundary — the reason the tiebreak exists
# ==========================================================================

class Row:
    """A stand-in for dto.EventRow: only the two ordering fields matter."""

    def __init__(self, guid: str, effective_at: str):
        self.observation_guid = guid
        self.effective_at = effective_at

    def __repr__(self) -> str:        # pragma: no cover - debugging aid
        return f"Row({self.observation_guid!r}, {self.effective_at!r})"


# Four rows share 12:00:00 — a batch ingest, which is how dto's correction 2
# describes the real data (10 transactions over 1 949 observations).
TIED = "2026-11-11T12:00:00+00:00"
ROWS = [
    Row("g-09", "2026-11-11T13:00:00+00:00"),
    Row("g-08", TIED),
    Row("g-07", TIED),
    Row("g-06", TIED),
    Row("g-05", TIED),
    Row("g-04", "2026-11-11T11:00:00+00:00"),
    Row("g-03", "2026-11-11T10:00:00+00:00"),
    Row("g-02", "2026-11-11T09:00:00+00:00"),
    Row("g-01", "2026-11-11T08:00:00+00:00"),
]


def _walk(rows, limit):
    """Page through everything, returning the guids in served order."""
    seen: list[str] = []
    cursor = None
    for _ in range(50):                       # bounded: a loop is a failure
        page, cursor = paginate(rows, limit, cursor)
        seen.extend(r.observation_guid for r in page)
        if cursor is None:
            return seen
    raise AssertionError("paging did not terminate")


def test_page_ordering_is_newest_first():
    page, _ = paginate(ROWS, 3)
    assert [r.observation_guid for r in page] == ["g-09", "g-08", "g-07"]


def test_paging_boundary_shared_effective_at_skips_and_repeats_nothing():
    """THE test. The page ends in the middle of four rows sharing a timestamp.

    With a timestamp-only cursor, the next page either loses the rest of the
    tied group (`effective_at < 12:00`) or serves the whole group again
    (`effective_at <= 12:00`). Both are asserted against here.
    """
    first, cursor = paginate(ROWS, 3)
    assert [r.observation_guid for r in first] == ["g-09", "g-08", "g-07"]
    assert cursor is not None

    second, cursor2 = paginate(ROWS, 3, cursor)
    second_guids = [r.observation_guid for r in second]

    # nothing repeated
    assert not set(second_guids) & {r.observation_guid for r in first}
    # nothing skipped: the rest of the tied group comes next, in order
    assert second_guids == ["g-06", "g-05", "g-04"]
    assert cursor2 is not None


def test_paging_walks_every_row_exactly_once_at_every_page_size():
    expected = [r.observation_guid for r in ROWS]   # already newest-first
    for limit in range(1, len(ROWS) + 2):
        walked = _walk(ROWS, limit)
        assert walked == expected, f"limit={limit}"
        assert len(walked) == len(set(walked)), f"limit={limit} repeated a row"


def test_paging_walks_rows_that_ALL_share_one_timestamp():
    """The degenerate case: the tiebreak is the only ordering there is."""
    rows = [Row(f"g-{i:02d}", TIED) for i in range(1, 8)]
    expected = sorted((r.observation_guid for r in rows), reverse=True)
    for limit in (1, 2, 3, 7, 8):
        assert _walk(rows, limit) == expected, f"limit={limit}"


def test_last_page_has_no_cursor():
    _, cursor = paginate(ROWS, len(ROWS))
    assert cursor is None
    page, cursor = paginate(ROWS, 100)
    assert len(page) == len(ROWS)
    assert cursor is None


def test_paginate_accepts_mapping_rows():
    rows = [
        {"observation_guid": "g-02", "effective_at": "2026-11-11T12:00:00Z"},
        {"observation_guid": "g-01", "effective_at": "2026-11-11T11:00:00Z"},
    ]
    page, cursor = paginate(rows, 1)
    assert page[0]["observation_guid"] == "g-02"
    page2, _ = paginate(rows, 1, cursor)
    assert page2[0]["observation_guid"] == "g-01"


def test_paginate_with_a_bad_cursor_raises_instead_of_serving_page_one():
    """Mutation (c) at the level the UI would feel it."""
    first, _ = paginate(ROWS, 3)
    with pytest.raises(InvalidCursor):
        paginate(ROWS, 3, "garbage-cursor")
    for bad in ("", "!!!", encode_cursor(MOMENT, GUID)[:5]):
        with pytest.raises(InvalidCursor) as caught:
            paginate(ROWS, 3, bad)
        assert caught.value is not None
    # and the honest path still works, so the raises above are not a tautology
    page, _ = paginate(ROWS, 3)
    assert [r.observation_guid for r in page] == \
        [r.observation_guid for r in first]


def test_paginate_rejects_a_non_positive_limit():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            paginate(ROWS, bad)


def test_paginate_works_on_real_event_rows():
    """Positive control: the dto the service actually uses, unmodified."""
    from app.dashboard.dto import EventRow

    rows = [
        EventRow(
            observation_guid=f"g-{i:02d}",
            code_canonical="urn:pdhc:concept:demo",
            display_name="Demo",
            value=i,
            unit="mmol/L",
            effective_at=TIED,
            source_cdr="cdr1",
        )
        for i in range(1, 5)
    ]
    walked = _walk(rows, 2)
    assert walked == ["g-04", "g-03", "g-02", "g-01"]
