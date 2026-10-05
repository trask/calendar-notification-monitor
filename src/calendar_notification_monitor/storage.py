import gzip
import hashlib
import json
import os
import re
from pathlib import Path

from .fetch import cache_metadata
from .model import CALENDAR_ID, SCHEMA, MonitorError, canonical, validate_snapshot

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
DIGEST_PATTERN = r"[0-9a-f]{64}"
OBSERVATION_PATTERN = r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}"
OUTPUT_PATTERNS = (
    rf"state/current\.json",
    rf"snapshots/{DIGEST_PATTERN}\.json\.gz",
    rf"observations/[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}\.jsonl",
    rf"changes/{OBSERVATION_PATTERN}\.json\.gz",
)


def is_output_path(path):
    return any(re.fullmatch(pattern, path) for pattern in OUTPUT_PATTERNS)


def read_snapshot(path, expected_digest):
    try:
        with gzip.open(path, "rb") as stream:
            contents = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(contents) > MAX_SNAPSHOT_BYTES:
            raise MonitorError("baseline_too_large")
        if hashlib.sha256(contents).hexdigest() != expected_digest:
            raise MonitorError("baseline_digest_mismatch")
        snapshot = json.loads(contents)
        validate_snapshot(snapshot)
        if canonical(snapshot) != contents:
            raise MonitorError("noncanonical_baseline")
        return snapshot
    except MonitorError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
        raise MonitorError("invalid_baseline_snapshot") from exc


def load_baseline(root):
    path = root / "state" / "current.json"
    if not path.exists():
        if any((root / name).exists() for name in ("snapshots", "changes", "state")):
            raise MonitorError("baseline_missing")
        for log in (root / "observations").glob("*.jsonl"):
            try:
                lines = log.read_bytes().splitlines()
                if any(json.loads(line).get("outcome") != "failed" for line in lines):
                    raise MonitorError("baseline_missing")
            except (OSError, ValueError, AttributeError) as exc:
                raise MonitorError("invalid_baseline_observations") from exc
        return None, None
    try:
        state = json.loads(path.read_bytes())
        if (
            set(state) != {
                "schema_version", "calendar_id", "snapshot_digest", "observation_id",
                "observed_at", "cache",
            }
            or state["schema_version"] != SCHEMA
            or state["calendar_id"] != CALENDAR_ID
            or not re.fullmatch(DIGEST_PATTERN, state["snapshot_digest"])
            or not re.fullmatch(OBSERVATION_PATTERN, state["observation_id"])
            or not isinstance(state["cache"], dict)
            or set(state["cache"]) - {"etag", "last_modified"}
        ):
            raise MonitorError("invalid_baseline_state")
        headers = {}
        if "etag" in state["cache"]:
            headers["ETag"] = state["cache"]["etag"]
        if "last_modified" in state["cache"]:
            headers["Last-Modified"] = state["cache"]["last_modified"]
        if cache_metadata(headers) != state["cache"]:
            raise MonitorError("invalid_baseline_cache")
        from datetime import datetime
        if datetime.fromisoformat(state["observed_at"]).utcoffset().total_seconds() != 0:
            raise MonitorError("invalid_baseline_state")
        snapshot = read_snapshot(
            root / "snapshots" / f"{state['snapshot_digest']}.json.gz",
            state["snapshot_digest"],
        )
        stamp = state["observation_id"][:8]
        log = root / "observations" / f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}.jsonl"
        observations = [json.loads(line) for line in log.read_bytes().splitlines()]
        matches = [item for item in observations if item.get("id") == state["observation_id"]]
        if (
            len(matches) != 1
            or matches[0].get("outcome") not in {"initial_capture", "changed", "unchanged", "not_modified"}
            or matches[0].get("snapshot_digest") != state["snapshot_digest"]
            or matches[0].get("finished_at") != state["observed_at"]
        ):
            raise MonitorError("invalid_baseline_observation")
        return state, snapshot
    except MonitorError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise MonitorError("invalid_baseline_state") from exc


def atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def immutable_write(path, contents):
    if path.exists():
        if path.read_bytes() != contents:
            raise MonitorError("immutable_output_conflict")
        return False
    atomic_write(path, contents)
    return True


def persist(root, observation, snapshot=None, changes=None, cache=None):
    outputs = []
    if snapshot is not None:
        contents = canonical(snapshot)
        snapshot_digest = hashlib.sha256(contents).hexdigest()
        relative = f"snapshots/{snapshot_digest}.json.gz"
        path = root / relative
        if path.exists():
            read_snapshot(path, snapshot_digest)
            new_blob = False
        else:
            new_blob = immutable_write(path, gzip.compress(contents, compresslevel=6, mtime=0))
        observation["snapshot_blob_created"] = new_blob
        observation["snapshot_digest"] = snapshot_digest
        if new_blob:
            outputs.append(relative)
    if changes is not None:
        relative = f"changes/{observation['id']}.json.gz"
        immutable_write(root / relative, gzip.compress(canonical(changes), compresslevel=6, mtime=0))
        observation["changes_path"] = relative
        outputs.append(relative)
    relative = f"observations/{observation['started_at'][:10]}.jsonl"
    path = root / relative
    previous = path.read_bytes() if path.exists() else b""
    if previous and not previous.endswith(b"\n"):
        raise MonitorError("incomplete_observation_log")
    for line in previous.splitlines():
        try:
            existing = json.loads(line)
        except ValueError as exc:
            raise MonitorError("invalid_observation_log") from exc
        if existing.get("id") == observation["id"]:
            raise MonitorError("duplicate_observation")
    atomic_write(path, previous + canonical(observation) + b"\n")
    outputs.append(relative)
    if snapshot is not None:
        state = {
            "schema_version": SCHEMA, "calendar_id": CALENDAR_ID,
            "snapshot_digest": observation["snapshot_digest"],
            "observation_id": observation["id"],
            "observed_at": observation["finished_at"], "cache": cache or {},
        }
        atomic_write(root / "state" / "current.json", canonical(state) + b"\n")
        outputs.append("state/current.json")
    return outputs


def write_manifest(path, outputs):
    if any(not is_output_path(output) for output in outputs):
        raise MonitorError("invalid_output_path")
    atomic_write(Path(path), canonical(outputs) + b"\n")
