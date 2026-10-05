import argparse
import sys
from pathlib import Path

from .collector import collect, context
from .model import MonitorError
from .publish import publish


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only public calendar structural evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("collect")
    capture.add_argument("--data-dir", type=Path, required=True)
    capture.add_argument("--manifest", type=Path)
    capture.add_argument("--code-revision")
    capture.add_argument("--baseline-commit")
    capture.add_argument("--run-id")
    capture.add_argument("--run-attempt")
    publication = commands.add_parser("publish")
    publication.add_argument("--data-dir", type=Path, required=True)
    publication.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            commit = publish(args.data_dir, args.manifest)
            print(f"Published data commit {commit}")
            return 0
        invocation = context(args.code_revision, args.baseline_commit, args.run_id, args.run_attempt)
        observation = collect(args.data_dir, args.manifest, invocation)
        if observation["outcome"] == "failed":
            print(f"Collection failed: {observation['error']}; failure observation recorded.", file=sys.stderr)
            return 1
        print(f"{observation['outcome']}: {observation['event_count']} instances; {observation['id']}")
        return 0
    except MonitorError as exc:
        print(f"Monitor failed: {exc.code}", file=sys.stderr)
        return 1
    except Exception:
        print("Monitor failed: internal_or_storage_error; publication not confirmed.", file=sys.stderr)
        return 1
