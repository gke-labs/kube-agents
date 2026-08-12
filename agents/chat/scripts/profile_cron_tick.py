#!/usr/bin/env python3
"""Tick the cron store of every named profile.

Hermes binds its cron ticker to one ``HERMES_HOME``: the ticker is a single
thread started by ``hermes gateway run``, and every path it touches — the job
store, the execution ledger, ``.tick.lock`` — resolves from *that process's*
home. It never enumerates profiles. Upstream satisfies this by running one
gateway per profile (``S6ServiceManager.register_profile_gateway``), but this
image replaces the s6 entrypoint with a single ``hermes gateway run`` homed at
``/opt/data``, so exactly one store ticks: the ``default`` (Chat Agent) one.

Every job under ``profiles/<name>/cron/jobs.json`` was therefore inert. On the
Platform Agent's roster that was the entire watchdog set — the compliance,
obtainability and AI-workload-security audits, the security-patch
orchestrator, the cost and drift sweeps, and the every-30-minutes GitHub issue
resolver. They sat at ``state: scheduled`` with a ``next_run_at`` in the past,
``enabled: true``, and no error anywhere; the only runs any of them ever had
came from an agent calling ``cronjob(action='run')`` by hand.

Those seven ran for a while from the Chat Agent's roster instead, each one a
``no_agent`` script that filed a kanban card for the Platform Agent to pick up.
That indirection bought a ticker at the cost of the scheduler's per-job knobs —
a card is not a cron run, so ``skills``, ``model`` and ``deliver`` stopped
reaching the thing that ran — and it split every watchdog across three files: an
entry on one roster, a wrapper script, and a tombstone on the other. This script
removes the reason for all of it, so the seven are back on the Platform Agent's
own roster and the dispatch layer is gone. What else needs a ticker here is
anything an operator schedules on a named profile's own store.

This script is the ticker those profiles never had. It runs as a ``no_agent``
job on the one store that does tick, and each minute runs ``hermes cron tick``
against every named profile that has work due::

    gateway ticker           →  profile-cron-tick  →  hermes cron tick
    (HERMES_HOME=/opt/data)     (this script)         (HERMES_HOME=<profile>)

A subprocess rather than an in-process ``tick()`` call because the store, the
ledger and the lock are all resolved from the process home and some are frozen
at import (``cron/executions.py`` binds ``EXECUTIONS_FILE`` when it loads). A
fresh process is the only way to get all of them pointing at one profile, and
it is the same path ``hermes cron tick`` takes for a manual tick — so a job
fires through the identical execute → save → deliver → mark body the gateway
ticker uses, with the profile's own persona, toolsets and ``max_turns``.

Correctness comes from ``tick()`` itself, not from bookkeeping here:

* it takes an exclusive file lock on the profile's store while it decides what
  is due and claims it, so two spawns cannot dispatch the same tick. The lock
  is released once dispatch is done, not held for the run -- holding it for the
  run is what used to starve every other job on the profile behind a fleet
  audit (see ``deploy/docker/patches/cron_tick_lock_scope.py``);
* it advances every due job's ``next_run_at`` under that lock *before*
  executing, so the next pass no longer sees the job as due, and it holds a
  per-job lock for the run so a job that outlives its own period is not fired
  twice by two spawns;
* ``get_due_jobs()`` collapses a backlog — a job 57 hours overdue fires once,
  not 114 times.

:func:`due_job_ids` is only a filter. It decides whether starting an
interpreter is worth it, never whether a job runs, and it errs towards
spawning: a spurious tick is a no-op, a missed one is the bug this exists to
fix.

Output is empty on a quiet minute (``deliver: local`` plus empty stdout is a
silent run). It speaks only when a tick was dispatched, and exits non-zero if
one failed to start or came back non-zero within
:data:`DEFAULT_BUDGET_SECONDS` — so a broken ticker shows up as a failed cron
run instead of reproducing the silence it was written to end. A tick still
running at the deadline is reported and handed off rather than waited out; see
that constant for why the wait is short.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# How long to wait for the ticks this run started before handing them off.
#
# Deliberately under the one-minute dispatch interval. The gateway ticker skips
# any job already in flight (`_running_job_ids` in `cron/scheduler.py::tick`),
# so a dispatcher that sat waiting out a 20-minute fleet audit would suppress
# its own next twenty dispatches — and with them every OTHER profile's tick.
# The wait is here to catch the failures that happen at startup (no `hermes`,
# an unreadable store, an import error), and those land in seconds; a tick
# still alive at the deadline has already proved it started, so it is reported
# and left to finish on its own. Its outcome is recorded either way, in the
# profile's own execution ledger.
DEFAULT_BUDGET_SECONDS = 45
BUDGET_ENV = "PROFILE_CRON_TICK_BUDGET_SECONDS"

# Rotated at this size. Small on purpose: a healthy tick logs almost nothing
# (Hermes' own tick output goes to the profile's logger, not to stdout), so
# anything in here is a traceback, and only the recent ones are worth keeping.
LOG_MAX_BYTES = 1 << 20
TICK_LOG = "tick.log"

# How many job ids a per-profile summary line names before it counts the rest.
# A profile's whole roster can come due at once after an outage.
SUMMARY_IDS = 5


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def hermes_bin() -> Path:
    """Locate ``hermes``, preferring the one beside the running interpreter.

    The scheduler launches a ``no_agent`` script with ``sys.executable``, which
    is the gateway's own venv interpreter — so its sibling ``hermes`` is by
    construction the same install the gateway runs, whatever PATH says. PATH is
    the fallback for a dev checkout, where the two need not sit together.
    """
    sibling = Path(sys.executable).with_name("hermes")
    if sibling.is_file():
        return sibling
    found = shutil.which("hermes")
    if found:
        return Path(found)
    raise FileNotFoundError(f"no `hermes` beside {sys.executable} or on PATH")


def _budget_seconds() -> int:
    try:
        value = int(os.environ.get(BUDGET_ENV, "").strip())
    except ValueError:
        return DEFAULT_BUDGET_SECONDS
    return value if value > 0 else DEFAULT_BUDGET_SECONDS


def due_job_ids(store: Path, now: datetime) -> list[str]:
    """Ids of the jobs in ``store`` that look due — the spawn filter.

    Deliberately more eager than Hermes' own ``_get_due_jobs_locked``: every
    shape this cannot read (torn JSON, a ``next_run_at`` that is not a
    parseable timestamp) is reported as due, because Hermes repairs most of
    them on the next tick and the one failure this script must never have is a
    profile it quietly stops visiting.

    ``enabled`` is the only gate. It covers pausing too — ``cron.pause_job``
    sets ``enabled: False`` alongside ``state: "paused"`` — and it is the same
    key the real scheduler tests first.
    """
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except OSError:
        return []  # no readable store, so nothing here claims to be scheduled
    except ValueError:
        # Half-written or corrupt. Hermes' loader repairs several such shapes
        # under its lock; hand it the chance rather than skip the profile.
        return ["<unparseable jobs.json>"]

    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return ["<unparseable jobs.json>"]

    due: list[str] = []
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        job_id = str(job.get("id") or job.get("name") or "?")
        next_run = job.get("next_run_at")
        if not isinstance(next_run, str) or not next_run:
            # Never scheduled — a job the image has just added, or one whose
            # timestamp Hermes stripped as malformed. Due by definition:
            # something has to compute the first `next_run_at`.
            due.append(job_id)
            continue
        try:
            when = datetime.fromisoformat(next_run)
        except ValueError:
            due.append(job_id)
            continue
        if when.tzinfo is None:
            # Hermes writes offset-aware timestamps; a naive one predates that
            # and meant local time when it was written.
            when = when.astimezone()
        if when <= now:
            due.append(job_id)
    return due


def profiles_with_due_jobs(home: Path, now: datetime) -> list[tuple[Path, list[str]]]:
    """Every named profile home with at least one due job, in name order.

    A profile with no ``cron/jobs.json`` is skipped rather than ticked: the
    Cluster Agents ship no roster, and starting an interpreter a minute for
    each of them would cost more than everything this script exists to run.
    """
    base = home / "profiles"
    if not base.is_dir():
        return []
    work: list[tuple[Path, list[str]]] = []
    for profile in sorted(p for p in base.iterdir() if p.is_dir()):
        store = profile / "cron" / "jobs.json"
        if not store.is_file():
            continue
        due = due_job_ids(store, now)
        if due:
            work.append((profile, due))
    return work


def _rotate(log_path: Path) -> None:
    try:
        if log_path.is_file() and log_path.stat().st_size > LOG_MAX_BYTES:
            os.replace(log_path, log_path.with_name(log_path.name + ".1"))
    except OSError:
        pass  # logging must never be the reason a tick does not happen


# Home-target routing keys, per platform: (chat id, thread id).
#
# ``deliver=all`` resolves through ``cron/scheduler.py``'s
# ``_get_home_target_chat_id``, which reads ``os.getenv`` and nothing else — it
# never consults ``config.yaml``. So the child needs these in its environment
# or it has nowhere to post.
#
# Slack-only because the entry criterion is "the platform's home-channel var is
# on the provider-env blocklist", not "the harness supports the platform". Of
# the platforms shipped here, ``tools/environments/local.py`` blocks Slack's
# (and Telegram's, Discord's and Signal's); ``GOOGLE_CHAT_HOME_CHANNEL`` is
# absent from that list, so it survives ``build_subprocess_env`` and the child
# inherits it with nothing to restore. Add a platform here when its var joins
# the blocklist, not when the platform is enabled.
HOME_TARGET_ENV_KEYS = {
    "slack": ("SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_THREAD_ID"),
}


def _mapping(value: object) -> dict:
    """``value`` if it is a mapping, else an empty one.

    Every read below goes through this. ``config.yaml`` is the file the running
    agent writes to, and this ticker is the one process that must never fail on
    it: an exception here escapes ``main`` and *no* profile ticks, every minute,
    for as long as the shape is wrong. A hand-edited or half-written
    ``platforms: slack`` therefore costs one un-restored home target rather than
    the whole schedule.
    """
    return value if isinstance(value, dict) else {}


def home_target_env(root_home: Path) -> dict[str, str]:
    """The home-target variables a cron child cannot inherit, re-read from disk.

    ``SLACK_HOME_CHANNEL`` sits on Hermes' provider-env blocklist
    (``tools/environments/local.py``), so ``build_subprocess_env`` strips it
    from every ``no_agent`` script's environment — including this ticker's.
    The value is therefore already gone by the time we spawn, and passing
    ``os.environ`` through re-exports the hole: the child resolves
    ``deliver=all`` to nothing and every job on a named profile records "no
    delivery target resolved for deliver=all" while posting nowhere.
    ``/sethome`` is not at fault; its value simply cannot cross the boundary.

    Read it back from the root profile's ``config.yaml`` — the file
    ``/sethome`` writes — rather than from the environment it was scrubbed
    from. A channel id is routing metadata, not a credential: it is on that
    blocklist because Hermes manages it as config, alongside
    ``SLACK_ALLOWED_USERS``. Restoring these two keys here leaves the blocklist
    intact for everything it exists to protect — the tokens, the relay secret,
    the provider API keys.

    Both keys move together on purpose. ``SLACK_HOME_CHANNEL_THREAD_ID`` is
    *not* on the blocklist and does survive the spawn, so restoring only the
    channel id would leave a stale inherited thread id pointing into a thread
    the new home knows nothing about.

    The thread id is cleared rather than forwarded. ``/sethome`` records
    whichever thread it happened to be typed in, and a cron brief is not a
    reply to that conversation — forwarding it files every scheduled report
    under one ageing thread, where the first one that arrived is the only one
    anybody sees. Posting flat is a property of cron delivery here, not a
    consequence of where the command was run, so re-homing from inside a
    thread cannot quietly bury the briefs again. A job that wants a thread
    still gets one by naming an explicit ``deliver=`` target.
    """
    try:
        import yaml

        config = yaml.safe_load((root_home / "config.yaml").read_text()) or {}
    except Exception:
        # Never the reason a tick does not happen: a job with an explicit
        # target still delivers, and `deliver=all` degrades to today's
        # behaviour rather than to a crash.
        return {}

    platforms = _mapping(config).get("platforms")
    restored: dict[str, str] = {}
    for platform, (chat_key, thread_key) in HOME_TARGET_ENV_KEYS.items():
        home = _mapping(_mapping(platforms).get(platform)).get("home_channel")
        home = _mapping(home)
        chat_id = home.get("chat_id")
        if not chat_id:
            continue
        restored[chat_key] = str(chat_id)
        # Empty means "unset it in the child", not "leave it alone" — see the
        # docstring: a thread id from a previous home outlives the re-home,
        # and a cron brief posts flat regardless of the one on file.
        restored[thread_key] = ""
    return restored


def spawn_tick(
    binary: Path, profile_home: Path, home_env: dict[str, str] | None = None
) -> subprocess.Popen:
    """Start ``hermes cron tick`` against one profile home.

    ``cwd`` is the credential proxy's workspace root when the pod declares one.
    The ``gcloud``/``kubectl`` shims POST their argv to a sidecar that rejects
    any call made from outside it, and the cwd this script inherits is the
    scripts directory — outside. Nothing in the child is known to depend on its
    own cwd (the terminal tool resolves its own), but a working directory the
    shim would refuse is not the one to hand a job that runs ``gcloud``.

    ``home_env`` is :func:`home_target_env`'s output. It is applied over the
    inherited environment because ``config.yaml`` is what ``/sethome`` wrote
    last; an inherited value that survived scrubbing came from an earlier
    write. A key it maps to the empty string is removed rather than blanked,
    so the child cannot tell a cleared thread id from one never set.
    """
    workspace = os.environ.get("CREDENTIAL_PROXY_WORKSPACE_ROOT", "")
    cwd = workspace if workspace and Path(workspace).is_dir() else str(profile_home)

    # Only the keys `home_env` blanks are dropped; an inherited variable that
    # is legitimately empty stays empty.
    cleared = {key for key, value in (home_env or {}).items() if value == ""}
    env = {
        **{key: value for key, value in os.environ.items() if key not in cleared},
        **{key: value for key, value in (home_env or {}).items() if value != ""},
        "HERMES_HOME": str(profile_home),
    }

    log_path = profile_home / "cron" / TICK_LOG
    _rotate(log_path)
    handle = log_path.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            [str(binary), "cron", "tick"],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        handle.close()  # the child holds its own duplicate


def _summarise(due: list[str]) -> str:
    head = ", ".join(due[:SUMMARY_IDS])
    extra = len(due) - SUMMARY_IDS
    return f"{head}, +{extra} more" if extra > 0 else head


def main() -> int:
    now = datetime.now(timezone.utc)
    work = profiles_with_due_jobs(hermes_home(), now)
    if not work:
        return 0  # quiet minute: no output, silent run

    try:
        binary = hermes_bin()
    except FileNotFoundError as exc:
        names = ", ".join(profile.name for profile, _ in work)
        print(f"cannot tick {names}: {exc}")
        return 1

    # Read once: every child gets the same home target, and re-parsing the
    # root config per profile would only widen the window for the two to differ.
    home_env = home_target_env(hermes_home())

    failed = False
    lines: list[str] = []
    started: list[tuple[Path, list[str], subprocess.Popen]] = []
    for profile, due in work:
        try:
            started.append((profile, due, spawn_tick(binary, profile, home_env)))
        except OSError as exc:
            failed = True
            lines.append(
                f"{profile.name}: could not start `hermes cron tick` ({exc}); "
                f"{len(due)} job(s) stay due"
            )

    budget = _budget_seconds()
    deadline = time.monotonic() + budget
    for profile, due, child in started:
        try:
            code = child.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            # Handed off, not killed. `next_run_at` was advanced before the job
            # started and the child holds a per-job lock for as long as it
            # lives, so the next pass costs one spawn that re-dispatches
            # nothing for this job — while still dispatching anything else that
            # has come due. Killing it would abandon work the ledger already
            # records as running.
            lines.append(
                f"{profile.name}: tick still running after {budget}s "
                f"(pid {child.pid}); handed off, see {profile / 'cron' / TICK_LOG}"
            )
            continue
        if code == 0:
            lines.append(f"{profile.name}: ticked {len(due)} due job(s) — {_summarise(due)}")
        else:
            failed = True
            lines.append(
                f"{profile.name}: `hermes cron tick` exited {code} with {len(due)} job(s) "
                f"due ({_summarise(due)}); see {profile / 'cron' / TICK_LOG}"
            )

    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
