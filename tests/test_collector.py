import gzip
import json

import pytest

from calendar_notification_monitor.cli import main
from calendar_notification_monitor.collector import collect, context
from calendar_notification_monitor.fetch import FetchFailure
from calendar_notification_monitor.model import canonical
from calendar_notification_monitor.storage import load_baseline
from conftest import calendar, event, response


def observations(root):
    return [
        json.loads(line)
        for path in sorted((root / "observations").glob("*.jsonl"))
        for line in path.read_bytes().splitlines()
    ]


def test_two_polls_restore_baseline_and_deduplicate(tmp_path, sample):
    first = collect(tmp_path, fetcher=response(sample, cache={"etag": '"public123"'}))
    second = collect(tmp_path, fetcher=response(sample))
    assert first["outcome"] == "initial_capture"
    assert "change_counts" not in first
    assert second["outcome"] == "unchanged"
    assert second["baseline"]["observation_id"] == first["id"]
    assert second["baseline"]["snapshot_digest"] == first["snapshot_digest"]
    assert second["snapshot_blob_created"] is False
    assert len(list((tmp_path / "snapshots").glob("*.json.gz"))) == 1
    assert len(observations(tmp_path)) == 2
    assert not (tmp_path / "changes").exists()
    state, _ = load_baseline(tmp_path)
    assert state["observation_id"] == second["id"]


def test_304_reuses_validated_snapshot(tmp_path, sample):
    first = collect(tmp_path, fetcher=response(sample, cache={"etag": '"public123"'}))
    second = collect(tmp_path, fetcher=response(None, 304))
    assert second["outcome"] == "not_modified"
    assert second["snapshot_digest"] == first["snapshot_digest"]
    assert load_baseline(tmp_path)[0]["cache"] == {"etag": '"public123"'}


def test_304_without_baseline_is_failure_not_initial_capture(tmp_path):
    result = collect(tmp_path, fetcher=response(None, 304))
    assert result["outcome"] == "failed"
    assert result["error"] == "not_modified_without_baseline"
    assert not (tmp_path / "state" / "current.json").exists()


def test_failed_initial_poll_can_recover_with_a_labeled_initial_capture(tmp_path, sample):
    failed = collect(tmp_path, fetcher=response(b"malformed"))
    initial = collect(tmp_path, fetcher=response(sample))
    assert failed["outcome"] == "failed"
    assert initial["outcome"] == "initial_capture"
    assert initial["baseline"]["status"] == "absent"
    assert "change_counts" not in initial
    assert len(observations(tmp_path)) == 2


def test_unconditional_304_cannot_replace_good_state(tmp_path, sample):
    collect(tmp_path, fetcher=response(sample))
    previous = (tmp_path / "state" / "current.json").read_bytes()
    result = collect(tmp_path, fetcher=response(None, 304))
    assert result["outcome"] == "failed"
    assert result["error"] == "not_modified_without_baseline"
    assert (tmp_path / "state" / "current.json").read_bytes() == previous


@pytest.mark.parametrize("mode", ["malformed", "network", "unexpected"])
def test_failure_persists_observation_and_preserves_valid_state(tmp_path, sample, mode):
    first = collect(tmp_path, fetcher=response(sample))
    original = (tmp_path / "state" / "current.json").read_bytes()
    def broken(cache):
        if mode == "malformed":
            return response(b"password=PRIVATE")(cache)
        if mode == "network":
            raise FetchFailure("network_error", {"attempts": [], "started_at": "2026-10-05T00:00:00+00:00", "finished_at": "2026-10-05T00:00:01+00:00"})
        raise ValueError("password=PRIVATE")
    result = collect(tmp_path, fetcher=broken)
    assert result["outcome"] == "failed"
    assert result["baseline"]["observation_id"] == first["id"]
    assert (tmp_path / "state" / "current.json").read_bytes() == original
    assert len(observations(tmp_path)) == 2
    assert "PRIVATE" not in canonical(result).decode()
    assert len(list((tmp_path / "snapshots").glob("*"))) == 1


@pytest.mark.parametrize("mode", ["missing_state", "invalid_state", "missing_blob", "corrupt_blob", "private_blob", "missing_observation"])
def test_unavailable_baseline_never_becomes_verified_unchanged(tmp_path, sample, mode):
    first = collect(tmp_path, fetcher=response(sample))
    path = tmp_path / "state" / "current.json"
    snapshot = tmp_path / "snapshots" / f"{first['snapshot_digest']}.json.gz"
    if mode == "missing_state":
        path.unlink()
    elif mode == "invalid_state":
        path.write_text('{"password":"PRIVATE"}')
    elif mode == "missing_blob":
        snapshot.unlink()
    elif mode == "corrupt_blob":
        snapshot.write_bytes(b"PRIVATE")
    elif mode == "private_blob":
        contents = json.loads(gzip.decompress(snapshot.read_bytes()))
        contents["events"][0]["description"] = "PRIVATE"
        snapshot.write_bytes(gzip.compress(canonical(contents)))
    else:
        next((tmp_path / "observations").glob("*.jsonl")).unlink()
    original = path.read_bytes() if path.exists() else None
    def no_fetch(cache):
        pytest.fail("must not fetch after an invalid baseline")
    result = collect(tmp_path, fetcher=no_fetch)
    assert result["outcome"] == "failed"
    assert result["baseline"]["status"] == "unavailable"
    assert "change_counts" not in result
    assert len(observations(tmp_path)) == (1 if mode == "missing_observation" else 2)
    assert (path.read_bytes() if path.exists() else None) == original
    assert "PRIVATE" not in canonical(result).decode()


def test_changes_record_not_observed_without_claiming_deletion(tmp_path):
    collect(tmp_path, fetcher=response(calendar(event(), event(uid="missing@example.invalid"))))
    payload = calendar(event(extra="STATUS:CANCELLED\n"))
    result = collect(tmp_path, fetcher=response(payload))
    changes = json.loads(gzip.decompress((tmp_path / result["changes_path"]).read_bytes()))
    assert result["outcome"] == "changed"
    assert result["change_counts"]["status_changed"] == 1
    assert result["change_counts"]["not_observed_instances"] == 1
    assert changes["not_observed"][0]["uid"] == "missing@example.invalid"
    assert "deleted" not in canonical(changes).decode()


def test_excluded_secrets_never_reach_any_output_or_logs(tmp_path, monkeypatch, capsys):
    secret = "PRIVATE_SENTINEL_DO_NOT_STORE"
    extra = (
        f"DESCRIPTION:{secret}\nLOCATION:{secret}\nURL:https://private.invalid/{secret}\n"
        f"ORGANIZER:mailto:{secret}@example.invalid\nATTENDEE;RSVP=TRUE:mailto:{secret}@example.invalid\n"
        f"X-GOOGLE-CONFERENCE:{secret}\nX-GOOGLE-GUEST-CAN-INVITE:TRUE\n"
        f"BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:{secret}\nTRIGGER:-PT10M\nEND:VALARM\n"
    )
    payload = calendar(event(extra=extra)).replace(b"SUMMARY:Public working group", f"SUMMARY:password={secret} https://meeting.invalid/secret".encode())
    first = collect(tmp_path, fetcher=response(payload))
    changed = payload.replace(b"DTEND:20261005T180000Z", b"DTEND:20261005T190000Z")
    monkeypatch.setattr("calendar_notification_monitor.cli.collect", lambda *args: collect(tmp_path, fetcher=response(changed)))
    assert main(["collect", "--data-dir", str(tmp_path)]) == 0
    malformed = changed.replace(b"DTSTART:20261005T170000Z", f"DTSTART:{secret}".encode())
    monkeypatch.setattr("calendar_notification_monitor.cli.collect", lambda *args: collect(tmp_path, fetcher=response(malformed)))
    assert main(["collect", "--data-dir", str(tmp_path)]) == 1
    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
            assert secret.encode() not in content
            assert b"meeting.invalid" not in content
            for excluded in (b"description", b"location", b"attendee", b"organizer", b"conference", b"rsvp"):
                assert excluded not in content.lower()
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert first["outcome"] == "initial_capture"


@pytest.mark.parametrize("summary", [
    "https://meeting.invalid/secret", "www.example.com/secret", "meet.google.com/secret",
    "password: private", "passcode 12345", "token private", "Bearer private", "api_key private",
    "person@example.invalid", "A" * 40,
    "meeting.company.cloud/secret", "192.0.2.1/secret", "username: private",
])
def test_title_redaction(tmp_path, summary):
    payload = calendar(event()).replace(b"SUMMARY:Public working group", f"SUMMARY:{summary}".encode())
    collect(tmp_path, fetcher=response(payload))
    assert load_baseline(tmp_path)[1]["events"][0]["summary"] == "[redacted title]"


def test_invocation_and_source_times_are_separate(tmp_path, sample):
    invocation = context("a" * 40, "b" * 40, "1234", "2")
    observation = collect(tmp_path, invocation=invocation, fetcher=response(sample))
    assert observation["invocation"] == {
        "code_revision": "a" * 40, "baseline_commit": "b" * 40,
        "workflow_run_id": "1234", "workflow_run_attempt": "2",
    }
    assert observation["source_times"]["last_modified"]["maximum"] == "2026-10-01T00:00:00+00:00"
    assert observation["request"]["started_at"] != observation["started_at"]


def test_manifest_is_explicit_and_outside_data(tmp_path, sample):
    root = tmp_path / "data"
    manifest = tmp_path / "outputs.json"
    collect(root, manifest=manifest, fetcher=response(sample))
    names = json.loads(manifest.read_bytes())
    assert "state/current.json" in names
    assert len(names) == 3
    assert all((root / name).is_file() for name in names)
    assert not (root / "outputs.json").exists()
