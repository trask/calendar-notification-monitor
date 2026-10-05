# Public calendar change monitor

Read-only structural evidence from the public OpenTelemetry calendar. The collector monitors every exported event, including recurring masters and explicit overrides, without expanding recurrence rules.

This repository and its `data` branch are public. Every committed snapshot and observation must remain sanitized public structural data.

This evidence cannot show attendee lists, RSVP changes, guest permissions, or who caused a change. A new series with the same schedule is a diagnostic candidate, not proof of a series split or wrongdoing. There is no audit-import tool, authenticated Google API, mailbox access, or calendar-writing code.

## Source and privacy

The only network source is this [anonymous public iCalendar feed](https://calendar.google.com/calendar/ical/c_2bf73e3b6b530da4babd444e72b76a6ad893a5c3f43cf40467abc7a9a897f977%40group.calendar.google.com/public/basic.ics).

Calendar ID:

```text
c_2bf73e3b6b530da4babd444e72b76a6ad893a5c3f43cf40467abc7a9a897f977@group.calendar.google.com
```

The persisted allowlist is UID and instance key, redacted public summary, DTSTART/DTEND or DURATION, RRULE/RDATE/EXDATE, RECURRENCE-ID and its RANGE, CREATED, LAST-MODIFIED, SEQUENCE, STATUS, TRANSP, and structural VTIMEZONE context. Date-only values, floating times, named time zones, and UTC comparison values remain distinct. Time-zone observances retain only their kind, start, offsets, rules, and recurrence dates.

Titles containing URLs, bare domains, email addresses, credential-related words, long token-like strings, or control characters become `[redacted title]`. This deliberately redacts some harmless titles. Raw ICS is processed in memory and never saved. Descriptions, locations, conference links/passwords, alarms, organizer/guest contacts, and guest settings do not enter snapshots or changes. Response bodies and parser exception text are never logged.

Google requests contain no authorization headers and do not use environment proxy credentials. GitHub's supplied token is used only by the separate data checkout's Git operations.

## Run locally

Python 3.12 or later and Git are required. Dependencies, including transitive/build/test dependencies, are pinned in `requirements.lock`.

```sh
python -m venv .venv
# Activate .venv using your shell's normal command.
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation .
python -m pytest -q
calendar-monitor collect --data-dir local-data
```

On Windows, activate with `.\.venv\Scripts\Activate.ps1`. Without activation, use `.\.venv\Scripts\python.exe -m calendar_notification_monitor collect --data-dir local-data`. The publisher's Python subprocess launcher uses `CREATE_NO_WINDOW` on Windows.

`local-data` is ignored by Git. Local collection does not publish anything. The source and calendar ID are fixed in `model.py`; there is no credential configuration. Optional provenance arguments are `--code-revision`, `--baseline-commit`, `--run-id`, and `--run-attempt`.

Do not point local collection at an active shared data checkout. Use a separate directory, or inspect a downloaded copy without running collection.

## Persistent history

Code lives on `main`. Sanitized evidence lives on the independent orphan [`data` branch](https://github.com/trask/calendar-notification-monitor/tree/data). It has no common ancestor with `main`. Never merge `data` into `main`, force-push it, or prune its history.

| Path on `data` | Contents |
| --- | --- |
| `state/current.json` | Latest successful observation, snapshot digest, and usable HTTP validators |
| `snapshots/<sha256>.json.gz` | Immutable, compressed canonical snapshots, shared by identical captures |
| `observations/YYYY-MM-DD.jsonl` | One UTC observation per poll, including unchanged and failed polls |
| `changes/<observation-id>.json.gz` | Sanitized added, not-observed, and updated instances with before/after values |

Each observation records its actual collection and HTTP request windows, retry attempts, accepted source/cache headers, outcome, baseline provenance, workflow run/attempt, and code revision. Event CREATED/LAST-MODIFIED timestamps are source data, not observation timestamps. Observations also summarize their ranges.

Instance identity hashes the pair of UID and canonical RECURRENCE-ID. A master and all its overrides have distinct keys. Canonical JSON sorts event, timezone, property, and recurrence-list ordering. DTSTAMP and other export clock fields do not affect snapshots. LAST-MODIFIED/SEQUENCE/title-only updates produce `metadata_only` changes; schedule/recurrence changes and status changes have separate categories. A VTIMEZONE-only change is recorded as `time_zone_context_changed`.

Named-zone UTC mapping uses the locked `tzdata` package rather than the operating system's timezone database. Custom VTIMEZONE definitions are scoped to one parse. Dependency upgrades can change timezone mapping and must be reviewed as code changes.

Outcomes are `initial_capture`, `unchanged`, `not_modified`, `changed`, or `failed`. The first successful capture has no additions diff. A missing or corrupt baseline after history exists fails explicitly rather than reporting thousands of additions or a verified unchanged capture. Missing instances are `not_observed`, not proven deleted.

`same_schedule_series_candidates` pairs a newly observed master UID with previous masters having exactly the same structural schedule. It does not match approximate schedules, infer actors, or assert a split. Cancellations appear as STATUS changes or canceled newly observed records. The source may omit canceled records entirely.

Malformed/partial responses, duplicate instance identities, duplicate singleton fields, unsupported recurrence structures, unresolved zones, ambiguous/nonexistent DST wall times, and empty feeds fail the entire capture. No valid state is replaced. Recurrence rules are retained, never expanded into an unbounded occurrence list.

There is no retention limit, automatic archive, or pruning. Compression and identical-snapshot reuse reduce growth, but changed snapshots, changes, daily observation files, and their Git history accumulate indefinitely. Monitor repository size and Actions usage. The public repository avoids the private-repository Actions-minute quota that blocked the initial deployment.

## Inspect or download

Browse the [data tree](https://github.com/trask/calendar-notification-monitor/tree/data), or download a branch ZIP from GitHub. An independent read-only checkout is also useful:

```sh
git clone --single-branch --branch data https://github.com/trask/calendar-notification-monitor.git calendar-evidence
```

Do not execute files from `data`. There is no code there. JSONL observations can be read line by line with a JSON reader. Use the digest in `state/current.json` to locate the current snapshot. To read a compressed snapshot or change document locally:

```sh
python -c "import gzip,json,sys; print(json.dumps(json.load(gzip.open(sys.argv[1], 'rt')), indent=2))" calendar-evidence/snapshots/DIGEST.json.gz
```

Replace `DIGEST` with the recorded digest, or pass a path under `changes`. On Windows use backslashes in local paths and set `$env:PYTHONIOENCODING = "utf-8"` before commands that print non-ASCII text.

To check branch independence after fetching both branches, `git merge-base origin/main origin/data` must return no output and exit status 1.

## Scheduled operation

The [collection workflow](https://github.com/trask/calendar-notification-monitor/actions/workflows/collect.yml) runs at minutes 7, 22, 37, and 52 of each hour. Manual dispatch and scheduled polls share one concurrency group and never cancel an in-progress capture. GitHub may replace an older pending run when multiple polls queue, so this is not a guaranteed 15-minute timer.

Only runs on `main` can collect, and code is checked out explicitly from `main`. `data` is a separate full-history checkout, never a source of executable code. Only the collection job has `contents: write`; synthetic tests are read-only and run for code changes targeting `main`, not data commits. Actions are pinned to verified full commit SHAs.

The publisher requires branch `data`, the exact personal repository remote, no common ancestor with `origin/main`, and an empty staging area. It stages only the collector's explicitly named output manifest. Existing snapshot/change blobs cannot be overwritten; existing observation files must retain their full prefix. Pushes are ordinary fast-forwards. A concurrent remote update fails publication instead of rewriting history.

Collection failures return a nonzero exit status. A later publication step may still append the sanitized failure observation, but the run remains failed and the last successful state remains unchanged. Setup, checkout, runner termination, disk failure, or publication failure can prevent an observation from reaching GitHub. Check failed runs in Actions rather than assuming an absent observation means no change.

HTTP fetching uses up to three attempts, a 20-second socket timeout, a 90-second response deadline checked between bounded reads, and delays of one and two seconds. Each response is capped at 32 MiB. Redirects are rejected. ETag and Last-Modified validators are used when valid and present; a 304 requires an intact verified baseline. A 200 response is always parsed in full, even when its sanitized snapshot is unchanged.

The public feed can lag behind edits because of Google's export/cache behavior. HTTP dates, cache directives, and age are hints, not proof of freshness. Scheduler delay, queued runs, retry time, and between-poll reversions can all hide short-lived changes. This collector cannot establish exactly when a user edited a meeting.

To change cadence, edit the cron in `.github/workflows/collect.yml` on `main`. To stop collection without deleting history:

```sh
gh workflow disable collect.yml --repo trask/calendar-notification-monitor
```

Re-enable with `gh workflow enable collect.yml --repo trask/calendar-notification-monitor`. Dispatch manually with `gh workflow run collect.yml --repo trask/calendar-notification-monitor --ref main`.

Private audit exports, private calendar data, and any future Google credentials must stay out of this repository. Any later investigation or authenticated collection requires separate approval and separate tooling.
