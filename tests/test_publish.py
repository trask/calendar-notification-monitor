import json
import subprocess

import pytest

from calendar_notification_monitor.collector import collect
from calendar_notification_monitor.model import MonitorError
from calendar_notification_monitor.publish import publish, run_git
from calendar_notification_monitor.storage import write_manifest
from conftest import response


@pytest.fixture
def data_repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "--bare", str(remote))
    main = tmp_path / "main"
    main.mkdir()
    run_git(main, "init", "-b", "main")
    run_git(main, "config", "user.name", "Synthetic test")
    run_git(main, "config", "user.email", "synthetic@example.invalid")
    (main / "README").write_text("Synthetic code branch")
    run_git(main, "add", "README")
    run_git(main, "commit", "-m", "Synthetic main")
    run_git(main, "remote", "add", "origin", str(remote))
    run_git(main, "push", "origin", "main")
    data = tmp_path / "data"
    data.mkdir()
    run_git(data, "init", "-b", "data")
    run_git(data, "config", "user.name", "Synthetic test")
    run_git(data, "config", "user.email", "synthetic@example.invalid")
    run_git(data, "commit", "--allow-empty", "-m", "Synthetic orphan")
    run_git(data, "remote", "add", "origin", str(remote))
    run_git(data, "push", "origin", "data")
    run_git(data, "fetch", "origin")
    monkeypatch.setattr("calendar_notification_monitor.publish.EXPECTED_REMOTE", str(remote))
    return data, remote, main


def test_only_named_paths_published_on_independent_data_history(data_repo, tmp_path, sample):
    data, remote, main = data_repo
    main_before = run_git(main, "rev-parse", "HEAD")
    manifest = tmp_path / "outputs.json"
    first = collect(data, manifest, fetcher=response(sample))
    (data / "private.ics").write_text("DO_NOT_PUBLISH")
    (data / "code.py").write_text("raise Exception('NEVER_EXECUTE')")
    first_commit = publish(data, manifest)
    assert run_git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/data") == first_commit
    assert run_git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/main") == main_before
    assert run_git(data, "merge-base", "HEAD", "origin/main", allowed_codes=(0, 1)) == ""
    assert run_git(data, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == sorted(json.loads(manifest.read_bytes()))
    assert "Co-authored-by: Copilot App" in run_git(data, "log", "-1", "--format=%B")
    restored = tmp_path / "restored"
    run_git(tmp_path, "clone", "--branch", "data", str(remote), str(restored))
    second = collect(restored, manifest, fetcher=response(sample))
    assert second["baseline"]["observation_id"] == first["id"]
    assert second["snapshot_blob_created"] is False
    second_commit = publish(restored, manifest)
    assert run_git(restored, "rev-parse", "HEAD^") == first_commit
    assert run_git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/data") == second_commit
    assert len(run_git(restored, "ls-tree", "-r", "--name-only", "HEAD", "snapshots").splitlines()) == 1


@pytest.mark.parametrize("path", ["private.ics", "../private.json", ".git/config", "state/other.json", "snapshots/private.json.gz"])
def test_invalid_named_paths_are_not_staged(data_repo, tmp_path, path):
    data, _, _ = data_repo
    manifest = tmp_path / "outputs.json"
    manifest.write_text(json.dumps([path]))
    before = run_git(data, "rev-parse", "HEAD")
    with pytest.raises(MonitorError):
        publish(data, manifest)
    assert run_git(data, "rev-parse", "HEAD") == before
    assert run_git(data, "diff", "--cached", "--name-only") == ""


def test_existing_staged_file_blocks_publication(data_repo, tmp_path, sample):
    data, _, _ = data_repo
    manifest = tmp_path / "outputs.json"
    collect(data, manifest, fetcher=response(sample))
    (data / "private.ics").write_text("PRIVATE")
    run_git(data, "add", "private.ics")
    with pytest.raises(MonitorError, match="unexpected_staged_files"):
        publish(data, manifest)


def test_wrong_branch_and_remote_are_rejected(data_repo, tmp_path, sample):
    data, remote, _ = data_repo
    manifest = tmp_path / "outputs.json"
    collect(data, manifest, fetcher=response(sample))
    run_git(data, "switch", "-c", "wrong")
    with pytest.raises(MonitorError, match="unsafe_publication_target"):
        publish(data, manifest)
    run_git(data, "switch", "data")
    run_git(data, "remote", "set-url", "origin", str(tmp_path / "wrong.git"))
    with pytest.raises(MonitorError, match="unsafe_publication_target"):
        publish(data, manifest)


def test_nonorphan_data_branch_is_rejected(data_repo, tmp_path, sample):
    data, _, _ = data_repo
    run_git(data, "switch", "--detach", "origin/main")
    run_git(data, "branch", "-f", "data")
    run_git(data, "switch", "data")
    manifest = tmp_path / "outputs.json"
    collect(data, manifest, fetcher=response(sample))
    with pytest.raises(MonitorError, match="data_branch_not_orphan"):
        publish(data, manifest)


def test_observation_history_cannot_be_rewritten(data_repo, tmp_path, sample):
    data, _, _ = data_repo
    manifest = tmp_path / "outputs.json"
    collect(data, manifest, fetcher=response(sample))
    publish(data, manifest)
    relative = next(name for name in json.loads(manifest.read_bytes()) if name.startswith("observations/"))
    (data / relative).write_text('{"rewritten":true}\n')
    write_manifest(manifest, [relative])
    with pytest.raises(MonitorError, match="observation_history_rewrite"):
        publish(data, manifest)


def test_existing_snapshots_are_immutable(data_repo, tmp_path, sample):
    data, _, _ = data_repo
    manifest = tmp_path / "outputs.json"
    collect(data, manifest, fetcher=response(sample))
    publish(data, manifest)
    relative = next(name for name in json.loads(manifest.read_bytes()) if name.startswith("snapshots/"))
    (data / relative).write_bytes(b"INVALID")
    write_manifest(manifest, [relative])
    with pytest.raises(MonitorError, match="immutable_publication_path"):
        publish(data, manifest)


def test_failure_observation_can_be_published_without_moving_state(data_repo, tmp_path, sample):
    data, _, _ = data_repo
    manifest = tmp_path / "outputs.json"
    collect(data, manifest, fetcher=response(sample))
    publish(data, manifest)
    original = (data / "state" / "current.json").read_bytes()
    failure = collect(data, manifest, fetcher=response(b"invalid calendar"))
    assert failure["outcome"] == "failed"
    assert json.loads(manifest.read_bytes()) == [f"observations/{failure['started_at'][:10]}.jsonl"]
    publish(data, manifest)
    assert (data / "state" / "current.json").read_bytes() == original
    assert run_git(data, "diff", "HEAD^", "HEAD", "--name-only") == json.loads(manifest.read_bytes())[0]


def test_non_fast_forward_publication_never_force_pushes(data_repo, tmp_path, sample):
    data, remote, _ = data_repo
    concurrent = tmp_path / "concurrent"
    run_git(tmp_path, "clone", "--branch", "data", str(remote), str(concurrent))
    manifest = tmp_path / "outputs.json"
    collect(data, manifest, fetcher=response(sample))
    published = publish(data, manifest)
    collect(concurrent, manifest, fetcher=response(sample))
    with pytest.raises(MonitorError, match="git_command_failed"):
        publish(concurrent, manifest)
    assert run_git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/data") == published


def test_windows_subprocess_launches_are_hidden(monkeypatch, tmp_path):
    observed = []
    def fake_run(args, **kwargs):
        observed.append(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="synthetic\n")
    # Patching os.name also changes Path's platform selection, so use a string root.
    monkeypatch.setattr("calendar_notification_monitor.publish.os.name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_git(str(tmp_path), "status") == "synthetic"
    assert observed[0]["creationflags"] == 0x08000000
