import copy
import time

import pytest

from calendar_notification_monitor.model import (
    MonitorError, canonical, compare, digest, parse_calendar, validate_snapshot,
)
from conftest import calendar, event


def test_master_and_overrides_have_distinct_stable_keys():
    master = event(extra="RRULE:FREQ=WEEKLY\n")
    first = event(extra="RECURRENCE-ID:20261005T170000Z\n")
    second = event(extra="RECURRENCE-ID:20261012T170000Z\n")
    parsed = parse_calendar(calendar(master, first, second))
    assert len({item["key"] for item in parsed["events"]}) == 3
    assert sum("recurrence_id" in item for item in parsed["events"]) == 2
    validate_snapshot(parsed)


def test_order_folding_and_export_clock_are_not_changes(sample):
    parsed = parse_calendar(sample)
    reordered = sample.replace(
        b"RRULE:FREQ=WEEKLY;BYDAY=MO,WE",
        b"RRULE:BYDAY=WE,MO;FREQ=WEEKLY",
    ).replace(b"DTSTAMP:20261001T000000Z", b"DTSTAMP:20261005T000000Z")
    reordered = reordered.replace(b"SUMMARY:Public working group", b"SUMMARY:Public working\r\n  group")
    lines = reordered.decode().split("\r\n")
    begin, finish = lines.index("BEGIN:VEVENT"), lines.index("END:VEVENT")
    # Keep folded lines together while moving complete properties.
    props = "\r\n".join(lines[begin + 1:finish]).replace("working\r\n  group", "working group").split("\r\n")
    lines[begin + 1:finish] = reversed(props)
    assert digest(parsed) == digest(parse_calendar("\r\n".join(lines).encode()))
    assert compare(parsed, parsed)["updated"] == []


def test_recurring_instance_order_and_recurrence_date_order():
    a = event(extra="RRULE:FREQ=WEEKLY;BYDAY=MO,WE\nEXDATE:20261012T170000Z,20261019T170000Z\n")
    b = event(uid="other@example.invalid")
    alternative = a.replace("MO,WE", "WE,MO").replace("20261012T170000Z,20261019T170000Z", "20261019T170000Z,20261012T170000Z")
    assert parse_calendar(calendar(a, b)) == parse_calendar(calendar(b, alternative))


def test_new_series_same_times_is_only_a_candidate():
    before = parse_calendar(calendar(event(uid="original@example.invalid", extra="RRULE:FREQ=WEEKLY\n")))
    after = parse_calendar(calendar(event(uid="replacement_R20261005@example.invalid", extra="RRULE:FREQ=WEEKLY\n")))
    changes = compare(before, after)
    assert len(changes["added"]) == len(changes["not_observed"]) == 1
    assert changes["same_schedule_series_candidates"] == [{
        "previous_uid": "original@example.invalid",
        "new_uid": "replacement_R20261005@example.invalid",
        "previous_still_observed": False,
    }]
    assert "deleted" not in canonical(changes).decode()


@pytest.mark.parametrize(("change", "category"), [
    (("20261005T180000Z", "20261005T190000Z"), "time_changed"),
    (("FREQ=WEEKLY", "FREQ=DAILY"), "recurrence_changed"),
    (("STATUS:CONFIRMED", "STATUS:CANCELLED"), "status_changed"),
    (("SEQUENCE:1", "SEQUENCE:2"), "metadata_only"),
    (("LAST-MODIFIED:20261001T000000Z", "LAST-MODIFIED:20261002T000000Z"), "metadata_only"),
    (("Public working group", "Public contributor meeting"), "metadata_only"),
])
def test_change_categories(change, category):
    data = calendar(event(extra="RRULE:FREQ=WEEKLY\nSTATUS:CONFIRMED\nSEQUENCE:1\nLAST-MODIFIED:20261001T000000Z\n"))
    changes = compare(parse_calendar(data), parse_calendar(data.replace(change[0].encode(), change[1].encode())))
    assert changes["updated"][0]["categories"] == [category]


def test_date_only_is_not_utc_midnight():
    payload = calendar(event()).replace(
        b"DTSTART:20261005T170000Z", b"DTSTART;VALUE=DATE:20261005",
    ).replace(b"DTEND:20261005T180000Z", b"DTEND;VALUE=DATE:20261006")
    parsed = parse_calendar(payload)
    assert parsed["events"][0]["dtstart"] == {"kind": "date", "value": "2026-10-05"}
    altered = payload.replace(b"VALUE=DATE:20261006", b"VALUE=DATE:20261007")
    assert compare(parsed, parse_calendar(altered))["updated"][0]["categories"] == ["time_changed"]


def test_timezone_dst_floating_and_zone_change():
    def zoned(day, zone="America/New_York"):
        return calendar(event()).replace(
            b"DTSTART:20261005T170000Z", f"DTSTART;TZID={zone}:{day}T100000".encode(),
        ).replace(b"DTEND:20261005T180000Z", f"DTEND;TZID={zone}:{day}T110000".encode())
    summer = parse_calendar(zoned("20261005"))["events"][0]["dtstart"]
    winter = parse_calendar(zoned("20261205"))["events"][0]["dtstart"]
    assert summer["utc"] == "2026-10-05T14:00:00+00:00"
    assert winter["utc"] == "2026-12-05T15:00:00+00:00"
    assert summer["tzid"] == "America/New_York"
    changed = compare(parse_calendar(zoned("20261005")), parse_calendar(zoned("20261005", "America/Chicago")))
    assert changed["updated"][0]["categories"] == ["time_changed"]
    floating = parse_calendar(calendar(event()).replace(b"T170000Z", b"T170000").replace(b"T180000Z", b"T180000"))
    assert floating["events"][0]["dtstart"]["floating"] is True


def timezone_definition(offset="+0100"):
    return (
        "BEGIN:VTIMEZONE\nTZID:Synthetic/Local\n"
        "BEGIN:STANDARD\nDTSTART:19700101T000000\n"
        f"TZOFFSETFROM:{offset}\nTZOFFSETTO:{offset}\n"
        "END:STANDARD\nEND:VTIMEZONE\n"
    )


def test_custom_zone_context_changes_and_does_not_reuse_old_parser_cache():
    def payload(offset):
        return calendar(event(), zones=timezone_definition(offset)).replace(
            b"DTSTART:20261005T170000Z", b"DTSTART;TZID=Synthetic/Local:20261005T170000",
        ).replace(b"DTEND:20261005T180000Z", b"DTEND;TZID=Synthetic/Local:20261005T180000")
    before, after = parse_calendar(payload("+0100")), parse_calendar(payload("+0200"))
    assert before["events"][0]["dtstart"]["utc"] == "2026-10-05T16:00:00+00:00"
    assert after["events"][0]["dtstart"]["utc"] == "2026-10-05T15:00:00+00:00"
    assert compare(before, after)["time_zone_context_changed"] is True
    assert parse_calendar(payload("+0100")) == before
    validate_snapshot(after)


def test_custom_zone_can_follow_events():
    payload = calendar(event()).replace(
        b"DTSTART:20261005T170000Z", b"DTSTART;TZID=Synthetic/Local:20261005T170000",
    ).replace(b"DTEND:20261005T180000Z", b"DTEND;TZID=Synthetic/Local:20261005T180000")
    payload = payload.replace(b"END:VCALENDAR", timezone_definition().replace("\n", "\r\n").encode() + b"END:VCALENDAR")
    assert parse_calendar(payload)["events"][0]["dtstart"]["utc"] == "2026-10-05T16:00:00+00:00"


@pytest.mark.parametrize("value", ["20261101T013000", "20260308T023000"])
def test_ambiguous_or_nonexistent_dst_wall_times_fail(value):
    payload = calendar(event()).replace(
        b"DTSTART:20261005T170000Z", f"DTSTART;TZID=America/New_York:{value}".encode(),
    )
    with pytest.raises(MonitorError):
        parse_calendar(payload)


def test_duration_period_and_recurrence_range():
    payload = calendar(event(extra="RDATE;VALUE=PERIOD:20261012T170000Z/PT1H\nRECURRENCE-ID;RANGE=THISANDFUTURE:20261005T170000Z\n"))
    payload = payload.replace(b"DTEND:20261005T180000Z", b"DURATION:PT1H")
    parsed = parse_calendar(payload)
    assert parsed["events"][0]["duration_seconds"] == 3600
    assert parsed["events"][0]["recurrence_range"] == "THISANDFUTURE"
    assert parsed["events"][0]["rdate"][0]["period"]["duration_seconds"] == 3600
    validate_snapshot(parsed)


@pytest.mark.parametrize("extra", [
    "UID:duplicate@example.invalid\n",
    "DTSTART:not-a-date\n",
    "RRULE:FREQ=INVALID\n",
    "RRULE:FREQ=DAILY;COUNT=0\n",
    "RRULE:FREQ=DAILY;COUNT=3;UNTIL=20261010T000000Z\n",
    "RRULE:FREQ=DAILY;BYHOUR=50\n",
    "RRULE:FREQ=MONTHLY;BYDAY=99MO\n",
    "RRULE:FREQ=DAILY;SECRET=hidden\n",
    "SEQUENCE:-1\n",
    "LAST-MODIFIED:20261001\n",
    "STATUS:INVALID\n",
])
def test_invalid_properties_fail_whole_capture(extra):
    with pytest.raises(MonitorError):
        parse_calendar(calendar(event(extra=extra)))


@pytest.mark.parametrize("payload", [
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
    b"<html>private response body</html>",
    calendar(event())[:-20],
    calendar(event()) + calendar(event()),
    calendar(event(), event()),
    calendar(event()).replace(b"END:VEVENT", b"END:VTODO"),
    calendar(event()).replace(b"20261005T180000Z", b"20261005T160000Z"),
    calendar(event()).replace(b"DTSTART:20261005T170000Z", b"DTSTART;TZID=Unknown/Private:20261005T170000"),
    calendar(event()).replace(b"VERSION:2.0", b"VERSION:1.0"),
])
def test_malformed_or_ambiguous_calendar(payload):
    with pytest.raises(MonitorError):
        parse_calendar(payload)


def test_baseline_allowlist_rejects_extra_private_fields(sample):
    parsed = parse_calendar(sample)
    for field in ("description", "location", "attendee", "organizer", "url"):
        altered = copy.deepcopy(parsed)
        altered["events"][0][field] = "private-marker"
        with pytest.raises(MonitorError):
            validate_snapshot(altered)


def test_real_sized_feed_feasibility():
    events = []
    for index in range(6211):
        extra = "DESCRIPTION:" + "Excluded synthetic content " * 50 + "\n"
        if index < 4662:
            extra += f"RECURRENCE-ID:20261005T{index // 3600:02}{index // 60 % 60:02}{index % 60:02}Z\n"
            uid = f"series-{index % 1522}@example.invalid"
        else:
            uid = f"series-{index - 4662}@example.invalid"
            if index < 4662 + 1522:
                extra += "RRULE:FREQ=WEEKLY\n"
        events.append(event(uid=uid, extra=extra))
    payload = calendar(*events)
    assert len(payload) > 7_543_961
    started = time.monotonic()
    parsed = parse_calendar(payload)
    assert len(parsed["events"]) == 6211
    assert sum("recurrence_id" in item for item in parsed["events"]) == 4662
    assert sum("rrule" in item for item in parsed["events"]) == 1522
    assert len(canonical(parsed)) < 4_000_000
    assert time.monotonic() - started < 60
