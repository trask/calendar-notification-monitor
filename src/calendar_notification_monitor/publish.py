import json
import os
import subprocess
from pathlib import Path

from .model import MonitorError
from .storage import is_output_path

EXPECTED_REMOTE = "https://github.com/trask/calendar-notification-monitor.git"
TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"


def run_git(root, *args, allowed_codes=(0,)):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    completed = subprocess.run(
        ["git", "--no-pager", "-C", str(root), *args],
        check=False, capture_output=True, text=True, **options,
    )
    if completed.returncode not in allowed_codes:
        raise MonitorError("git_command_failed")
    return completed.stdout.strip()


def publish(root, manifest):
    root = Path(root).resolve()
    branch = run_git(root, "symbolic-ref", "--short", "HEAD")
    remote = run_git(root, "remote", "get-url", "--push", "origin")
    git_root = Path(run_git(root, "rev-parse", "--show-toplevel")).resolve()
    if (
        branch != "data"
        or remote not in {EXPECTED_REMOTE, EXPECTED_REMOTE.removesuffix(".git")}
        or git_root != root
    ):
        raise MonitorError("unsafe_publication_target")
    base = run_git(root, "rev-parse", "HEAD")
    if run_git(root, "merge-base", "HEAD", "origin/main", allowed_codes=(0, 1)):
        raise MonitorError("data_branch_not_orphan")
    try:
        outputs = json.loads(Path(manifest).read_bytes())
    except (OSError, ValueError) as exc:
        raise MonitorError("invalid_publication_manifest") from exc
    if (
        not isinstance(outputs, list) or not outputs
        or any(not isinstance(output, str) for output in outputs)
        or len(set(outputs)) != len(outputs)
    ):
        raise MonitorError("invalid_publication_manifest")
    tracked = set(run_git(root, "ls-tree", "-r", "--name-only", base).splitlines())
    for relative in outputs:
        if not isinstance(relative, str) or not is_output_path(relative):
            raise MonitorError("invalid_publication_path")
        path = root / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise MonitorError("invalid_publication_path")
        if any(parent.is_symlink() for parent in path.parents if parent != root):
            raise MonitorError("invalid_publication_path")
        if relative in tracked:
            if relative.startswith(("snapshots/", "changes/")):
                raise MonitorError("immutable_publication_path")
            if relative.startswith("observations/"):
                previous = run_git(root, "show", f"{base}:{relative}") + "\n"
                contents = path.read_bytes().replace(b"\r\n", b"\n")
                if not contents.startswith(previous.encode("utf-8")):
                    raise MonitorError("observation_history_rewrite")
    if run_git(root, "diff", "--cached", "--name-only", "-z"):
        raise MonitorError("unexpected_staged_files")
    run_git(root, "add", "--", *outputs)
    staged = run_git(root, "diff", "--cached", "--name-only", "-z").split("\0")
    if any(name not in outputs for name in staged if name):
        raise MonitorError("unexpected_staged_files")
    if not any(staged):
        raise MonitorError("nothing_to_publish")
    run_git(root, "-c", "user.name=Copilot App", "-c", "user.email=223556219+Copilot@users.noreply.github.com",
            "commit", "-m", f"Record public calendar observation\n\n{TRAILER}")
    commit = run_git(root, "rev-parse", "HEAD")
    parents = run_git(root, "rev-list", "--parents", "-n", "1", commit).split()
    if parents != [commit, base]:
        raise MonitorError("unexpected_publication_parent")
    run_git(root, "push", "origin", "HEAD:refs/heads/data")
    return commit
