import re
import uuid
from pathlib import Path

from .fetch import FetchFailure, fetch
from .model import CALENDAR_ID, SCHEMA, MonitorError, compare, digest, parse_calendar, utc_now
from .storage import load_baseline, persist, write_manifest


def context(code_revision=None, baseline_commit=None, run_id=None, run_attempt=None):
    result = {}
    for name, value, pattern in (
        ("code_revision", code_revision, r"[0-9a-f]{40}"),
        ("baseline_commit", baseline_commit, r"[0-9a-f]{40}"),
        ("workflow_run_id", run_id, r"[0-9]{1,20}"),
        ("workflow_run_attempt", run_attempt, r"[0-9]{1,8}"),
    ):
        if value is not None:
            if not re.fullmatch(pattern, value):
                raise MonitorError("invalid_invocation_context")
            result[name] = value
    return result


def source_times(snapshot):
    result = {}
    for name in ("created", "last_modified"):
        values = sorted(event[name]["utc"] for event in snapshot["events"] if name in event)
        result[name] = {"count": len(values), "minimum": values[0] if values else None, "maximum": values[-1] if values else None}
    return result


def change_counts(changes):
    return {
        "added_instances": len(changes["added"]),
        "not_observed_instances": len(changes["not_observed"]),
        "updated_instances": len(changes["updated"]),
        "added_series": len({event["uid"] for event in changes["added"] if "recurrence_id" not in event}),
        "not_observed_series": len({event["uid"] for event in changes["not_observed"] if "recurrence_id" not in event}),
        "added_overrides": sum("recurrence_id" in event for event in changes["added"]),
        "not_observed_overrides": sum("recurrence_id" in event for event in changes["not_observed"]),
        "time_changed": sum("time_changed" in event["categories"] for event in changes["updated"]),
        "recurrence_changed": sum("recurrence_changed" in event["categories"] for event in changes["updated"]),
        "status_changed": sum("status_changed" in event["categories"] for event in changes["updated"]),
        "metadata_only": sum(event["categories"] == ["metadata_only"] for event in changes["updated"]),
        "time_zone_context_changed": changes["time_zone_context_changed"],
        "same_schedule_series_candidates": len(changes["same_schedule_series_candidates"]),
    }


def collect(root, manifest=None, invocation=None, fetcher=fetch):
    root = Path(root)
    invocation = invocation or {}
    started = utc_now()
    observation = {
        "schema_version": SCHEMA,
        "id": re.sub(r"[-:.+]", "", started[:26]) + "Z-" + uuid.uuid4().hex[:12],
        "calendar_id": CALENDAR_ID, "started_at": started,
        "invocation": invocation, "baseline": {"status": "unavailable"},
        "request": None,
    }
    snapshot = None
    changes = None
    cache = {}
    try:
        state, previous = load_baseline(root)
        observation["baseline"] = {
            "status": "verified" if state else "absent",
            "snapshot_digest": state["snapshot_digest"] if state else None,
            "observation_id": state["observation_id"] if state else None,
            "data_commit": invocation.get("baseline_commit"),
        }
        payload, status, request = fetcher(state["cache"] if state else {})
        observation["request"] = request
        response_cache = request["attempts"][-1].get("cache", {})
        cache = {name: response_cache[name] for name in ("etag", "last_modified") if name in response_cache}
        if status == 304:
            if previous is None or not request["conditional"]:
                raise MonitorError("not_modified_without_baseline")
            snapshot = previous
            cache = state["cache"] | cache
            observation["outcome"] = "not_modified"
        else:
            snapshot = parse_calendar(payload)
            if previous is None:
                observation["outcome"] = "initial_capture"
            elif digest(previous) == digest(snapshot):
                observation["outcome"] = "unchanged"
            else:
                observation["outcome"] = "changed"
                changes = compare(previous, snapshot)
                observation["change_counts"] = change_counts(changes)
        observation["event_count"] = len(snapshot["events"])
        observation["override_count"] = sum("recurrence_id" in event for event in snapshot["events"])
        observation["source_times"] = source_times(snapshot)
        observation["finished_at"] = utc_now()
    except FetchFailure as exc:
        observation["request"] = exc.request_metadata
        observation["outcome"] = "failed"
        observation["error"] = exc.code
        snapshot = None
        changes = None
    except MonitorError as exc:
        observation["outcome"] = "failed"
        observation["error"] = exc.code
        snapshot = None
        changes = None
    except Exception:
        # The privacy boundary must never print parser exceptions containing ICS.
        observation["outcome"] = "failed"
        observation["error"] = "internal_error"
        snapshot = None
        changes = None
    observation.setdefault("finished_at", utc_now())
    outputs = persist(root, observation, snapshot, changes, cache)
    if manifest is not None:
        write_manifest(manifest, outputs)
    return observation
