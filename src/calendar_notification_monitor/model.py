import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, reset_tzpath

from icalendar import Calendar
from icalendar.timezone import tzp

CALENDAR_ID = (
    "c_2bf73e3b6b530da4babd444e72b76a6ad893a5c3f43cf40467abc7a9a897f977"
    "@group.calendar.google.com"
)
SOURCE_URL = (
    "https://calendar.google.com/calendar/ical/"
    "c_2bf73e3b6b530da4babd444e72b76a6ad893a5c3f43cf40467abc7a9a897f977"
    "%40group.calendar.google.com/public/basic.ics"
)
MAX_BYTES = 32 * 1024 * 1024
SCHEMA = 1


class MonitorError(Exception):
    """An error code safe to persist and print without source content."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


SENSITIVE_TITLE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.|"
    r"\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,63}\b|"
    r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/|:)|"
    r"[\w.+-]+@[\w.-]+|"
    r"\b(?:pass(?:word|code)?|pwd|pin|token|secret|credential|authorization|"
    r"bearer|username|login|api[ _-]?key|access[ _-]?key)\b|"
    r"[A-Za-z0-9_+/=-]{32,}|[\x00-\x1f\x7f])"
)


def safe_title(value):
    text = str(value)
    if len(text) > 512 or SENSITIVE_TITLE.search(text):
        return "[redacted title]"
    return text


def identifier(value, timezone_id=False):
    text = str(value)
    pattern = r"[A-Za-z0-9_.+/\-]{1,128}" if timezone_id else r"[A-Za-z0-9_.@+\-]{1,512}"
    if not re.fullmatch(pattern, text):
        raise MonitorError("unsafe_identifier")
    return text


def single(component, name, required=False):
    value = component.get(name)
    if isinstance(value, list):
        raise MonitorError("duplicate_singleton_property")
    if value is None and required:
        raise MonitorError("missing_required_property")
    return value


def temporal(value, params=None, known_zones=None):
    params = params or {}
    known_zones = known_zones or set()
    if set(params) - {"VALUE", "TZID", "RANGE"}:
        raise MonitorError("unsupported_time_parameter")
    tzid = params.get("TZID")
    if tzid is not None:
        tzid = identifier(tzid, timezone_id=True)
        if tzid not in known_zones:
            try:
                ZoneInfo(tzid)
            except ZoneInfoNotFoundError as exc:
                raise MonitorError("unknown_time_zone") from exc
    if isinstance(value, datetime):
        if str(params.get("VALUE", "DATE-TIME")).upper() != "DATE-TIME":
            raise MonitorError("invalid_time_type")
        if tzid and value.tzinfo is None:
            raise MonitorError("unresolved_time_zone")
        if tzid:
            if value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset():
                raise MonitorError("ambiguous_local_time")
            round_trip = value.astimezone(timezone.utc).astimezone(value.tzinfo)
            if round_trip.replace(tzinfo=None) != value.replace(tzinfo=None):
                raise MonitorError("nonexistent_local_time")
        result = {"kind": "date-time", "value": value.replace(tzinfo=None).isoformat()}
        if tzid:
            result["tzid"] = tzid
        if value.tzinfo is not None:
            result["utc"] = value.astimezone(timezone.utc).isoformat()
        else:
            result["floating"] = True
        return result
    if isinstance(value, date):
        if tzid or str(params.get("VALUE", "DATE")).upper() != "DATE":
            raise MonitorError("invalid_date_type")
        return {"kind": "date", "value": value.isoformat()}
    raise MonitorError("invalid_time_value")


def time_property(component, name, known_zones, required=False):
    prop = single(component, name, required=required)
    return None if prop is None else temporal(prop.dt, prop.params, known_zones)


RULE_KEYS = {
    "FREQ", "UNTIL", "COUNT", "INTERVAL", "BYSECOND", "BYMINUTE", "BYHOUR",
    "BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH", "BYSETPOS", "WKST",
}


def recurrence(prop, known_zones):
    if set(prop) - RULE_KEYS or "FREQ" not in prop:
        raise MonitorError("unsupported_recurrence_rule")
    if "COUNT" in prop and "UNTIL" in prop:
        raise MonitorError("ambiguous_recurrence_rule")
    result = {}
    for name, values in prop.items():
        normalized = []
        for value in values:
            if isinstance(value, date):
                normalized.append(temporal(value, known_zones=known_zones))
            elif isinstance(value, int):
                normalized.append(int(value))
            else:
                text = str(value).upper()
                if not re.fullmatch(r"[A-Z0-9,+\-]{1,32}", text):
                    raise MonitorError("invalid_recurrence_value")
                normalized.append(text)
        result[name.lower()] = sorted(normalized, key=canonical)
    if result["freq"] not in [
        [name] for name in ("SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY")
    ]:
        raise MonitorError("invalid_recurrence_frequency")
    for name in ("count", "interval"):
        if name in result and (
            len(result[name]) != 1 or type(result[name][0]) is not int or result[name][0] < 1
        ):
            raise MonitorError("invalid_recurrence_number")
    if "until" in result and (
        len(result["until"]) != 1 or not isinstance(result["until"][0], dict)
    ):
        raise MonitorError("invalid_recurrence_until")
    if "wkst" in result and result["wkst"] not in [
        [name] for name in ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
    ]:
        raise MonitorError("invalid_recurrence_week_start")
    ranges = {
        "bysecond": (0, 60), "byminute": (0, 59), "byhour": (0, 23),
        "bymonth": (1, 12), "bymonthday": (-31, 31), "byyearday": (-366, 366),
        "byweekno": (-53, 53), "bysetpos": (-366, 366),
    }
    for name, (lower, upper) in ranges.items():
        for value in result.get(name, []):
            if type(value) is not int or not lower <= value <= upper:
                raise MonitorError("invalid_recurrence_number")
            if name in {"bymonthday", "byyearday", "byweekno", "bysetpos"} and value == 0:
                raise MonitorError("invalid_recurrence_number")
    for value in result.get("byday", []):
        if not isinstance(value, str) or not re.fullmatch(r"(?:[+-]?[1-9][0-9]?)?(?:MO|TU|WE|TH|FR|SA|SU)", value):
            raise MonitorError("invalid_recurrence_day")
        ordinal = value[:-2]
        if ordinal and not -53 <= int(ordinal) <= 53:
            raise MonitorError("invalid_recurrence_day")
    return result


def recurrence_dates(component, name, known_zones):
    properties = component.get(name, [])
    if not isinstance(properties, list):
        properties = [properties]
    result = []
    for prop in properties:
        for item in prop.dts:
            if isinstance(item.dt, tuple):
                if name != "RDATE" or str(prop.params.get("VALUE", "")).upper() != "PERIOD":
                    raise MonitorError("invalid_recurrence_period")
                params = dict(prop.params)
                params["VALUE"] = "DATE-TIME"
                start, end = item.dt
                period = {"start": temporal(start, params, known_zones)}
                if isinstance(end, timedelta):
                    if end.total_seconds() <= 0:
                        raise MonitorError("invalid_recurrence_period")
                    period["duration_seconds"] = int(end.total_seconds())
                else:
                    period["end"] = temporal(end, params, known_zones)
                    if end <= start:
                        raise MonitorError("invalid_recurrence_period")
                result.append({"period": period})
            else:
                result.append(temporal(item.dt, prop.params, known_zones))
    return sorted({canonical(item): item for item in result}.values(), key=canonical)


def check_framing(payload):
    if len(payload) > MAX_BYTES:
        raise MonitorError("feed_too_large")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MonitorError("invalid_encoding") from exc
    lines = text.replace("\r\n", "\n").split("\n")
    unfolded = []
    for line in lines:
        if line.startswith((" ", "\t")):
            if not unfolded:
                raise MonitorError("invalid_folding")
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    if not unfolded or unfolded[0] != "BEGIN:VCALENDAR" or unfolded[-1] != "END:VCALENDAR":
        raise MonitorError("incomplete_calendar")
    stack = []
    events = 0
    for line in unfolded:
        if ":" not in line:
            raise MonitorError("invalid_content_line")
        name, value = line.split(":", 1)
        if name == "BEGIN":
            if not stack and value != "VCALENDAR":
                raise MonitorError("invalid_component")
            if value == "VCALENDAR" and (stack or events):
                raise MonitorError("multiple_calendars")
            if value == "VEVENT":
                if stack != ["VCALENDAR"]:
                    raise MonitorError("nested_event")
                events += 1
            stack.append(value)
        elif name == "END":
            if not stack or stack.pop() != value:
                raise MonitorError("unbalanced_calendar")
        elif not stack:
            raise MonitorError("content_outside_calendar")
    if stack or not events:
        raise MonitorError("empty_or_incomplete_calendar")
    return events


def parse_calendar(payload):
    expected_count = check_framing(payload)
    try:
        # Custom VTIMEZONE definitions belong to this capture, not a previous parse.
        reset_tzpath(())
        ZoneInfo.clear_cache()
        tzp.use_zoneinfo()
        calendar = Calendar.from_ical(payload)
        if calendar.name != "VCALENDAR":
            raise MonitorError("invalid_calendar")
        if single(calendar, "VERSION", required=True) != "2.0":
            raise MonitorError("unsupported_calendar_version")
        if any(component.errors for component in calendar.walk()):
            raise MonitorError("invalid_calendar_property")
        zones = []
        known_zones = set()
        for component in calendar.subcomponents:
            if component.name == "VTIMEZONE":
                tzid = identifier(single(component, "TZID", required=True), timezone_id=True)
                if tzid in known_zones:
                    raise MonitorError("duplicate_time_zone")
                known_zones.add(tzid)
                zones.append((tzid, component))
            elif component.name != "VEVENT":
                raise MonitorError("unsupported_calendar_component")
        zone_context = []
        for tzid, component in zones:
            observances = []
            for child in component.subcomponents:
                if child.name not in {"STANDARD", "DAYLIGHT"}:
                    raise MonitorError("unsupported_time_zone_component")
                observance = {
                    "kind": child.name.lower(),
                    "dtstart": time_property(child, "DTSTART", known_zones, required=True),
                    "offset_from_seconds": int(single(child, "TZOFFSETFROM", required=True).td.total_seconds()),
                    "offset_to_seconds": int(single(child, "TZOFFSETTO", required=True).td.total_seconds()),
                }
                rule = single(child, "RRULE")
                if rule is not None:
                    observance["rrule"] = recurrence(rule, known_zones)
                dates = recurrence_dates(child, "RDATE", known_zones)
                if dates:
                    observance["rdate"] = dates
                observances.append(observance)
            if not observances:
                raise MonitorError("empty_time_zone")
            zone_context.append({"tzid": tzid, "observances": sorted(observances, key=canonical)})
        events = {}
        for component in calendar.subcomponents:
            if component.name != "VEVENT":
                continue
            uid = identifier(single(component, "UID", required=True))
            event = {"uid": uid}
            rid = time_property(component, "RECURRENCE-ID", known_zones)
            if rid is not None:
                event["recurrence_id"] = rid
                range_value = component["RECURRENCE-ID"].params.get("RANGE")
                if range_value is not None:
                    if str(range_value).upper() != "THISANDFUTURE":
                        raise MonitorError("unsupported_recurrence_range")
                    event["recurrence_range"] = "THISANDFUTURE"
            event["key"] = digest([uid, rid])
            title = single(component, "SUMMARY")
            if title is not None:
                event["summary"] = safe_title(title)
            for name in ("STATUS", "TRANSP"):
                prop = single(component, name)
                if prop is not None:
                    text = str(prop).upper()
                    choices = {"STATUS": {"TENTATIVE", "CONFIRMED", "CANCELLED"}, "TRANSP": {"OPAQUE", "TRANSPARENT"}}
                    if text not in choices[name]:
                        raise MonitorError("invalid_event_enum")
                    event[name.lower()] = text
            start = time_property(
                component, "DTSTART", known_zones,
                required=event.get("status") != "CANCELLED",
            )
            end = time_property(component, "DTEND", known_zones)
            if start is not None:
                event["dtstart"] = start
            if end is not None:
                event["dtend"] = end
                if start is None or start["kind"] != end["kind"]:
                    raise MonitorError("inconsistent_event_times")
                if start.get("floating", False) != end.get("floating", False):
                    raise MonitorError("inconsistent_event_times")
                start_value = start.get("utc", start["value"])
                end_value = end.get("utc", end["value"])
                if end_value <= start_value:
                    raise MonitorError("invalid_event_end")
            duration = single(component, "DURATION")
            if duration is not None:
                if end is not None or start is None or duration.dt.total_seconds() <= 0:
                    raise MonitorError("invalid_event_duration")
                event["duration_seconds"] = int(duration.dt.total_seconds())
            for name in ("CREATED", "LAST-MODIFIED"):
                prop = time_property(component, name, known_zones)
                if prop is not None:
                    if prop["kind"] != "date-time" or "utc" not in prop:
                        raise MonitorError("invalid_source_timestamp")
                    event[name.lower().replace("-", "_")] = prop
            sequence = single(component, "SEQUENCE")
            if sequence is not None:
                if int(sequence) < 0:
                    raise MonitorError("invalid_sequence")
                event["sequence"] = int(sequence)
            rule = single(component, "RRULE")
            if rule is not None:
                event["rrule"] = recurrence(rule, known_zones)
            for name in ("RDATE", "EXDATE"):
                values = recurrence_dates(component, name, known_zones)
                if values:
                    event[name.lower()] = values
            if rid is not None and start is not None and rid["kind"] != start["kind"]:
                raise MonitorError("inconsistent_recurrence_id")
            if event["key"] in events:
                raise MonitorError("duplicate_instance")
            events[event["key"]] = event
        if len(events) != expected_count:
            raise MonitorError("partial_calendar")
        return {
            "schema_version": SCHEMA,
            "calendar_id": CALENDAR_ID,
            "timezones": sorted(zone_context, key=canonical),
            "events": sorted(events.values(), key=lambda event: event["key"]),
        }
    except MonitorError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        raise MonitorError("invalid_calendar_property") from exc


SCHEDULE_FIELDS = {
    "dtstart", "dtend", "duration_seconds", "rrule", "rdate", "exdate",
    "recurrence_id", "recurrence_range",
}
EVENT_FIELDS = SCHEDULE_FIELDS | {
    "uid", "key", "summary", "created", "last_modified", "sequence", "status", "transp",
}


def validate_snapshot(snapshot):
    """Reject untrusted baseline fields before they can enter any new output."""
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"schema_version", "calendar_id", "events", "timezones"}
        or snapshot["schema_version"] != SCHEMA
        or snapshot["calendar_id"] != CALENDAR_ID
        or not isinstance(snapshot["events"], list)
        or not snapshot["events"]
        or not isinstance(snapshot["timezones"], list)
    ):
        raise MonitorError("invalid_baseline_snapshot")
    temporal_fields = {"kind", "value", "utc", "tzid", "floating"}

    def validate_time(value):
        if not isinstance(value, dict) or set(value) - temporal_fields:
            raise MonitorError("invalid_baseline_snapshot")
        if value.get("kind") not in {"date", "date-time"}:
            raise MonitorError("invalid_baseline_snapshot")
        parsed = date.fromisoformat(value["value"]) if value["kind"] == "date" else datetime.fromisoformat(value["value"])
        params = {"TZID": value["tzid"]} if "tzid" in value else {}
        if "utc" in value:
            instant = datetime.fromisoformat(value["utc"])
            if instant.utcoffset() != timedelta(0):
                raise MonitorError("invalid_baseline_snapshot")
        if "tzid" in value:
            identifier(value["tzid"], timezone_id=True)
        if value["kind"] == "date" and (params or "utc" in value or "floating" in value):
            raise MonitorError("invalid_baseline_snapshot")
        if not isinstance(parsed, date):
            raise MonitorError("invalid_baseline_snapshot")

    def validate_rule(value):
        if not isinstance(value, dict) or set(value) - {name.lower() for name in RULE_KEYS}:
            raise MonitorError("invalid_baseline_snapshot")
        for values in value.values():
            if not isinstance(values, list):
                raise MonitorError("invalid_baseline_snapshot")
            for item in values:
                if isinstance(item, dict):
                    validate_time(item)
                elif type(item) is int:
                    continue
                elif not isinstance(item, str) or not re.fullmatch(r"[A-Z0-9,+\-]{1,32}", item):
                    raise MonitorError("invalid_baseline_snapshot")

    def validate_dates(values):
        if not isinstance(values, list):
            raise MonitorError("invalid_baseline_snapshot")
        for value in values:
            if isinstance(value, dict) and set(value) == {"period"}:
                period = value["period"]
                if not isinstance(period, dict) or set(period) - {"start", "end", "duration_seconds"}:
                    raise MonitorError("invalid_baseline_snapshot")
                validate_time(period["start"])
                if "end" in period:
                    validate_time(period["end"])
                if "duration_seconds" in period and type(period["duration_seconds"]) is not int:
                    raise MonitorError("invalid_baseline_snapshot")
            else:
                validate_time(value)

    seen = set()
    for event in snapshot["events"]:
        if not isinstance(event, dict) or set(event) - EVENT_FIELDS:
            raise MonitorError("invalid_baseline_snapshot")
        identifier(event["uid"])
        if event["key"] != digest([event["uid"], event.get("recurrence_id")]) or event["key"] in seen:
            raise MonitorError("invalid_baseline_snapshot")
        seen.add(event["key"])
        if "summary" in event and safe_title(event["summary"]) != event["summary"]:
            raise MonitorError("invalid_baseline_snapshot")
        if "recurrence_range" in event and event["recurrence_range"] != "THISANDFUTURE":
            raise MonitorError("invalid_baseline_snapshot")
        if "dtstart" not in event and event.get("status") != "CANCELLED":
            raise MonitorError("invalid_baseline_snapshot")
        for name in ("dtstart", "dtend", "recurrence_id", "created", "last_modified"):
            if name in event:
                validate_time(event[name])
        if "rrule" in event:
            validate_rule(event["rrule"])
        for name in ("rdate", "exdate"):
            if name in event:
                validate_dates(event[name])
        for name in ("sequence", "duration_seconds"):
            if name in event and type(event[name]) is not int:
                raise MonitorError("invalid_baseline_snapshot")
        if event.get("sequence", 0) < 0 or event.get("duration_seconds", 1) <= 0:
            raise MonitorError("invalid_baseline_snapshot")
        for name, choices in (("status", {"TENTATIVE", "CONFIRMED", "CANCELLED"}), ("transp", {"OPAQUE", "TRANSPARENT"})):
            if name in event and event[name] not in choices:
                raise MonitorError("invalid_baseline_snapshot")
    for zone in snapshot["timezones"]:
        if not isinstance(zone, dict) or set(zone) != {"tzid", "observances"}:
            raise MonitorError("invalid_baseline_snapshot")
        identifier(zone["tzid"], timezone_id=True)
        for obs in zone["observances"]:
            if not isinstance(obs, dict) or set(obs) - {"kind", "dtstart", "offset_from_seconds", "offset_to_seconds", "rrule", "rdate"}:
                raise MonitorError("invalid_baseline_snapshot")
            if obs["kind"] not in {"standard", "daylight"}:
                raise MonitorError("invalid_baseline_snapshot")
            validate_time(obs["dtstart"])
            for name in ("offset_from_seconds", "offset_to_seconds"):
                if type(obs[name]) is not int:
                    raise MonitorError("invalid_baseline_snapshot")
            if "rrule" in obs:
                validate_rule(obs["rrule"])
            if "rdate" in obs:
                validate_dates(obs["rdate"])


def compare(before, after):
    old = {event["key"]: event for event in before["events"]}
    new = {event["key"]: event for event in after["events"]}
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    missing = [old[key] for key in sorted(old.keys() - new.keys())]
    updated = []
    zones_changed = before["timezones"] != after["timezones"]
    for key in sorted(old.keys() & new.keys()):
        fields = sorted(name for name in old[key].keys() | new[key].keys() if old[key].get(name) != new[key].get(name))
        if not fields:
            continue
        categories = []
        if set(fields) & {"dtstart", "dtend", "duration_seconds"}:
            categories.append("time_changed")
        if set(fields) & {"rrule", "rdate", "exdate", "recurrence_range"}:
            categories.append("recurrence_changed")
        if "status" in fields:
            categories.append("status_changed")
        if not categories:
            categories.append("metadata_only")
        updated.append({"key": key, "fields": fields, "categories": categories, "before": old[key], "after": new[key]})
    old_schedules = {}
    for event in old.values():
        if "recurrence_id" not in event and "dtstart" in event:
            schedule = digest({name: event[name] for name in SCHEDULE_FIELDS if name in event})
            old_schedules.setdefault(schedule, []).append(event)
    candidates = []
    for event in added:
        if "recurrence_id" in event or "dtstart" not in event:
            continue
        schedule = digest({name: event[name] for name in SCHEDULE_FIELDS if name in event})
        for previous in old_schedules.get(schedule, []):
            if previous["uid"] != event["uid"]:
                candidates.append({
                    "previous_uid": previous["uid"], "new_uid": event["uid"],
                    "previous_still_observed": previous["key"] in new,
                })
    return {
        "added": added, "not_observed": missing, "updated": updated,
        "time_zone_context_changed": zones_changed,
        "same_schedule_series_candidates": candidates,
    }
