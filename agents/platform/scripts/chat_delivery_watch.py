#!/usr/bin/env python3
"""chat_delivery_watch.py - notice when scheduled reports stop reaching chat.

Every failed delivery of a scheduled report is recorded: the Hermes scheduler
writes the failure into ``last_delivery_error`` on the job's entry in that
profile's ``cron/jobs.json``. Until this job existed nothing read that field
(#1102). #1094 is what that cost: seven days of six audits composed, dropped,
and recorded as dropped, and the only signal anyone received was that the
daily reports had stopped arriving.

The obvious watcher, a job that posts to chat when deliveries fail, reports
through the leg it is monitoring and is silent in exactly the case it exists
for. This one reports through two channels that do not depend on chat and
need no privilege the pod does not already hold:

* a **GitHub ledger issue** in the install's configured repository, one per
  install, labelled ``agent:delivery-watch``. It is opened when any job crosses
  the failure threshold, edited when the picture changes, and closed with a
  comment when every leg has recovered. The forge call goes the same route the
  GitHub watcher's does (``forge.run_gh`` through the sandbox and the
  credential proxy);
* an **``ALERT chat_delivery_watch`` line appended to
  ``<agent home>/logs/chat_delivery_watch.log``**. The gateway pod's fluent-bit
  sidecar tails ``logs/*.log`` and ships it to the container's stdout and so
  to Cloud Logging. Note that this job's own stdout is *not* that route: the
  scheduler captures a ``no_agent`` job's stdout into ``cron/output/`` and,
  with ``deliver: "local"``, sends it nowhere. Stdout is kept for a person
  running this by hand.

``deliver: "local"`` is the point, and this is the one platform-roster job
allowed to use it (``agents/platform/cron/README.md``): a delivery leg for the
report that a leg is down would be circular.

Why a ledger of its own. ``last_delivery_error`` holds only the latest run
and is set back to ``None`` by the next run that delivered cleanly or, less
obviously, delivered nothing: a run whose answer was ``[SILENT]`` clears it
too. "Failed for N consecutive runs" therefore needs state this job keeps
itself, advancing a job's streak only when a *new* run has appeared, and
treating a silent run as no evidence either way rather than as a recovery.

Grading. A hard failure means the report reached no platform: the relay
answered 502 (``composed but not delivered to <platforms>``), was unreachable,
or had no key. A partial one means it landed somewhere but not everywhere
(``chat relay partial: the report did not reach <platforms>``); a degraded
one means it was posted but the Chat Agent's turn failed. All three count
toward the streak; the grade and the platforms are carried into the issue so
a partial outage of one platform reads differently from a dead relay.

Run by the platform roster every half hour. A tick sees each job's latest run
only, so a job that runs more often than that is under-counted (never
over-counted), and cadence bounds how late a failure is noticed. Exit code is
0 on every path a cron tick
can reach: a non-zero exit would only make the scheduler build a failure
summary that ``local`` then drops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Siblings in `$HERMES_HOME/scripts`, this script's own directory and therefore
# `sys.path[0]` when the scheduler runs it.
import forge
import gitops_workspace
from cluster_agent_profile import RESERVED_PROFILES

# --- channels -----------------------------------------------------------------
# Stable prefix for a log filter. Cloud Logging:
#   resource.type="k8s_container" resource.labels.container_name="fluent-bit"
#   jsonPayload.log:"ALERT chat_delivery_watch"
LOG_PREFIX = "ALERT chat_delivery_watch"
# Housekeeping the file also carries, under a prefix the alert filter does not
# match: an unreadable store or a log-append failure is not a dead leg.
WARN_PREFIX = "WARN chat_delivery_watch"
LOGS_DIR = "logs"
ALERT_FILE_NAME = "chat_delivery_watch.log"

# --- where the scheduler keeps its state --------------------------------------
PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"
# The chat/default profile's store is the agent home itself.
DEFAULT_PROFILE_LABEL = "default"
CRON_DIR = "cron"
JOBS_FILE = "jobs.json"
OUTPUT_DIR = "output"
OUTPUT_SUFFIX = ".md"

# --- this job's own state -----------------------------------------------------
STATE_FILE_NAME = "chat_delivery_watch.json"
STATE_PATH_ENV = "CHAT_DELIVERY_WATCH_STATE"
STATE_SCHEMA_VERSION = 1
STATE_TMP_SUFFIX = ".tmp"
# A second consecutive miss on a daily job is two days of silence; a single miss
# is what a relay restart during the run looks like, and is not worth an issue.
THRESHOLD_ENV = "CHAT_DELIVERY_ALERT_THRESHOLD"
THRESHOLD_DEFAULT = 2
MAX_ERROR_CHARS = 500
# The scheduler saves the run's output first and stamps `last_run_at` last, after
# delivery; a silent run skips delivery, so the two are seconds apart. An output
# file older than the run by more than this belongs to an earlier run.
SILENT_OUTPUT_SLACK_S = 300

# --- grades, from the strings deploy/docker/plugins/chat/adapter.py produces ---
# The scheduler itself writes two more before any adapter is reached: a
# platform named in `deliver` that is not enabled, and a `deliver` that resolved
# to no target at all. Both mean nothing was delivered, so both grade hard.
GRADE_HARD = "hard"
GRADE_PARTIAL = "partial"
GRADE_DEGRADED = "degraded"
PARTIAL_RE = re.compile(r"chat relay partial: the report did not reach ([^.]+)\.")
# Several targets' errors arrive joined with "; ", and the relay may append a
# "(target …)" note, so the platform list ends at either.
HARD_UNDELIVERED_RE = re.compile(r"composed but not delivered to ([^;(\n]+)")
NOT_CONFIGURED_RE = re.compile(r"platform '([^']+)' not configured/enabled")
# Notes the scheduler files under the same field for a report that did arrive:
# a thread it fell back from, an attachment it could not confirm. An error that
# carries one of these and none of the failure shapes is a delivered report.
DELIVERED_NOTE_MARKERS = ("delivered without thread_id", "attachment(s) not delivered")
FAILURE_MARKERS = (
    "composed but not delivered",
    "chat relay answered HTTP",
    "chat relay unreachable",
    "chat relay partial",
    "chat relay degraded",
    "not configured/enabled",
    "no delivery target resolved",
    "SESSION_KV_API_KEY",
    "failed:",
)
DEGRADED_MARKER = "chat relay degraded:"
# Silence, as the scheduler recognises it (`cron/scheduler.py::_is_cron_silence_response`):
# one of these tokens as the whole first or last line of the response, compared
# case-insensitively, or a response that starts with `[SILENT]`. The saved
# document puts the response under its last `## Response` heading; a no_agent
# job's document says `**Status:** silent` instead.
SILENT_TOKENS = frozenset({"[silent]", "silent", "no_reply", "no reply"})
SILENT_PREFIX = "[silent]"
RESPONSE_HEADING = "## Response"
SILENT_STATUS_MARKER = "**Status:** silent"
# A job the scheduler will not run again has no next run to recover with, so it
# holds no streak: `enabled: false` (a retirement tombstone) or a paused state.
PAUSED_STATE = "paused"
# A run the scheduler recorded as anything but ok either failed before delivery
# (a gateway shutdown, an exception) and wrote no output, which says nothing
# about the leg, or failed and had its failure summary delivered, which proves
# the leg works. The presence of an output file for the run tells them apart.
STATUS_OK = "ok"

# --- the ledger issue ---------------------------------------------------------
# Where the issue lives: this variable when set, otherwise the install's managed
# GitHub repository. The override is for an install whose ledger should sit
# somewhere other than its GitOps repository, and for a hand run.
LEDGER_REPO_ENV = "CHAT_DELIVERY_LEDGER_REPO"
LABEL = "agent:delivery-watch"
LABEL_COLOR = "D73A4A"
LABEL_DESCRIPTION = "Scheduled-report delivery to chat is failing; maintained by chat_delivery_watch.py"
TITLE_PREFIX = "Scheduled-report delivery is failing: "
BODY_MARKER = "<!-- chat-delivery-watch -->"
GH_LIST_LIMIT = "20"
CLOSE_REASON = "completed"
# `gh issue create` prints the new issue's URL; the number is its last segment.
ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$")
EMPTY_CELL = "—"
EMPTY_FIELD = "-"

LEDGER_NONE = "none"
FOOTER_PREFIX = "_Maintained by"
LEDGER_CLOSED = "closed"
LEDGER_DRY_RUN = "dry-run"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def threshold() -> int:
    raw = os.environ.get(THRESHOLD_ENV, "").strip()
    if not raw:
        return THRESHOLD_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return THRESHOLD_DEFAULT
    return value if value >= 1 else THRESHOLD_DEFAULT


# --- reading the schedulers' stores -------------------------------------------


def roster_paths(agent_home: Path) -> list[tuple[str, Path]]:
    """Every cron store on this volume, as (profile label, jobs.json path)."""
    paths = [
        (DEFAULT_PROFILE_LABEL, agent_home / CRON_DIR / JOBS_FILE),
        (PLATFORM_PROFILE, agent_home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / JOBS_FILE),
    ]
    profiles_dir = agent_home / PROFILES_DIR
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if entry.is_dir() and entry.name not in RESERVED_PROFILES:
                paths.append((entry.name, entry / CRON_DIR / JOBS_FILE))
    return paths


def load_jobs(path: Path) -> list[dict]:
    """The live job dicts in a store; `{"jobs": [...]}` or a bare list, as Hermes accepts.

    A disabled or paused entry is left out: it will not run again, so a streak
    it carries could never recover, and a retirement tombstone (the README's
    two-release sequence leaves one) would otherwise hold the issue open.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError(f"{path}: `jobs` is {type(jobs).__name__}, not a list")
    return [
        j
        for j in jobs
        if isinstance(j, dict) and j.get("id") and j.get("enabled", True) is not False and j.get("state") != PAUSED_STATE
    ]


def newest_output(store_path: Path, job_id: str, last_run_at: str | None) -> str | None:
    """The document the job's latest run saved, or None when there is none for that run.

    Best effort: a missing directory, an unreadable file, or a newest file that
    predates the run all read as "no document".
    """
    output_dir = store_path.parent / OUTPUT_DIR / job_id
    if not output_dir.is_dir():
        return None
    candidates = [p for p in output_dir.iterdir() if p.is_file() and p.suffix == OUTPUT_SUFFIX]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    run_at = parse_iso(last_run_at)
    if run_at is not None and newest.stat().st_mtime < run_at.timestamp() - SILENT_OUTPUT_SLACK_S:
        return None
    try:
        return newest.read_text(encoding="utf-8")
    except OSError:
        return None


def newest_output_is_silent(store_path: Path, job_id: str, last_run_at: str | None) -> bool:
    """Whether the job's latest run delivered nothing on purpose.

    Deliberately biased: with no document for the run, the run is treated as a
    real delivery, so an unreadable history closes an alert rather than holding
    one open.
    """
    text = newest_output(store_path, job_id, last_run_at)
    return bool(text) and is_silent_document(text)


def is_silent_document(text: str) -> bool:
    """Whether a saved run document records a response the scheduler treated as silence."""
    if SILENT_STATUS_MARKER in text:
        return True
    response = text.rsplit(RESPONSE_HEADING, 1)[-1] if RESPONSE_HEADING in text else text
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if not lines:
        return False
    first, last = lines[0].lower(), lines[-1].lower()
    return first in SILENT_TOKENS or last in SILENT_TOKENS or first.startswith(SILENT_PREFIX)


# --- grading ------------------------------------------------------------------


def is_delivered_note(error: str) -> bool:
    """Whether the error only annotates a report that arrived anyway."""
    return any(m in error for m in DELIVERED_NOTE_MARKERS) and not any(m in error for m in FAILURE_MARKERS)


def grade_error(error: str) -> str:
    if PARTIAL_RE.search(error):
        return GRADE_PARTIAL
    if DEGRADED_MARKER in error:
        return GRADE_DEGRADED
    return GRADE_HARD


def platforms_from(error: str) -> list[str]:
    match = PARTIAL_RE.search(error) or HARD_UNDELIVERED_RE.search(error)
    if match:
        return [p.strip() for p in match.group(1).split(",") if p.strip()]
    return sorted(set(NOT_CONFIGURED_RE.findall(error)))


# --- the streak ledger --------------------------------------------------------


def empty_state() -> dict:
    return {
        "version": STATE_SCHEMA_VERSION,
        "last_tick_at": None,
        "last_tick_ok": None,
        "last_tick_error": None,
        "ledger": {"repo": None, "issue_number": None, "fingerprint": None},
        "jobs": {},
    }


def load_state(path: Path) -> dict:
    """The previous tick's state, or a fresh one when there is none or it is unreadable.

    A corrupt file is not fatal: the next tick rebuilds streaks from one run of
    evidence, which under-counts for a tick rather than stopping the watcher.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        return empty_state()
    state = empty_state()
    state.update({k: v for k, v in data.items() if k in state})
    if not isinstance(state.get("ledger"), dict):
        state["ledger"] = empty_state()["ledger"]
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + STATE_TMP_SUFFIX)
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def new_entry() -> dict:
    return {
        "last_seen_run_at": None,
        "streak": 0,
        "grade": None,
        "first_failure_at": None,
        "last_failure_at": None,
        "last_error": None,
        "platforms": [],
        "alerted": False,
    }


def run_changed(entry: dict, job: dict) -> bool:
    run_at = job.get("last_run_at")
    return bool(run_at) and run_at != entry.get("last_seen_run_at")


def advance(entry: dict, job: dict, *, silent: bool, has_output: bool = True) -> dict:
    """One job's ledger entry after seeing its current store record.

    Pure: the caller decides `silent` and `has_output` from the run's saved
    document (see `newest_output`). The streak moves only when `last_run_at`
    differs from the last run this ledger saw, so a half-hourly tick over a
    daily job counts runs, not ticks.
    """
    if not run_changed(entry, job):
        return entry
    run_at = job["last_run_at"]
    updated = dict(entry)
    updated["last_seen_run_at"] = run_at
    error = job.get("last_delivery_error")
    if error and is_delivered_note(str(error)):
        error = None
    if error:
        error = str(error)
        updated["streak"] = int(entry.get("streak") or 0) + 1
        updated["grade"] = grade_error(error)
        updated["platforms"] = platforms_from(error)
        updated["first_failure_at"] = entry.get("first_failure_at") or run_at
        updated["last_failure_at"] = run_at
        updated["last_error"] = error[:MAX_ERROR_CHARS]
        return updated
    if silent or (job.get("last_status") != STATUS_OK and not has_output):
        # Delivered nothing: a silent answer, or a run that failed before it
        # saved anything. Says nothing about the leg. A failed run that did
        # save its document had its failure summary delivered, and that is a
        # working leg, so it falls through to the reset.
        return updated
    updated["streak"] = 0
    updated["grade"] = None
    updated["platforms"] = []
    updated["first_failure_at"] = None
    updated["last_failure_at"] = None
    updated["last_error"] = None
    return updated


# --- the ledger issue ---------------------------------------------------------


class LedgerError(RuntimeError):
    """A GitHub step failed in a way that must not be read as 'nothing to do'."""


def gh(argv: list[str], repo: str, *, stdin: str | None = None, check: bool = True):
    result = forge.run_gh(argv, repo, stdin=stdin)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise LedgerError(f"gh {' '.join(argv[:2])} exited {result.returncode}: {detail[-1] if detail else ''}")
    return result


def ensure_label(repo: str) -> None:
    gh(
        ["label", "create", LABEL, "-R", repo, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force"],
        repo,
        check=False,
    )


def find_ledger_issue(repo: str) -> int | None:
    """The open ledger issue, if any: highest-numbered open issue carrying the marker."""
    result = gh(
        ["issue", "list", "-R", repo, "--label", LABEL, "--state", "open", "--json", "number,body", "--limit", GH_LIST_LIMIT],
        repo,
    )
    try:
        issues = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise LedgerError(f"gh issue list returned unparseable JSON: {exc}") from exc
    numbers = [int(i["number"]) for i in issues if isinstance(i, dict) and BODY_MARKER in str(i.get("body") or "")]
    return max(numbers) if numbers else None


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("`", "'").replace("\n", " ")


def render_issue(degraded: list[tuple[str, dict]], now: str, threshold_value: int) -> tuple[str, str]:
    """(title, body) for the ledger issue covering every degraded job."""
    profiles = sorted({key.split("/", 1)[0] for key, _ in degraded})
    title = f"{TITLE_PREFIX}{len(degraded)} job(s) on {', '.join(profiles)}"
    rows = ["| Profile | Job | Grade | Consecutive | Did not reach | First failure | Last failure | Last error |", "|---|---|---|---|---|---|---|---|"]
    for key, entry in sorted(degraded):
        profile, job_id = key.split("/", 1)
        rows.append(
            "| "
            + " | ".join(
                [
                    _cell(profile),
                    f"`{_cell(job_id)}`",
                    _cell(entry.get("grade") or ""),
                    str(entry.get("streak") or 0),
                    _cell(", ".join(entry.get("platforms") or []) or EMPTY_CELL),
                    _cell(entry.get("first_failure_at") or ""),
                    _cell(entry.get("last_failure_at") or ""),
                    f"`{_cell(entry.get('last_error') or '')}`",
                ]
            )
            + " |"
        )
    body = "\n".join(
        [
            BODY_MARKER,
            f"Scheduled reports have failed to reach chat on {len(degraded)} job(s) for at least "
            f"{threshold_value} consecutive run(s). The runs themselves completed; what failed is the "
            "delivery. Each report is kept under the profile's `cron/output/<job>/` directory, so do "
            "not re-run a job to resend it.",
            "",
            *rows,
            "",
            "**Grades.** `hard`: the leg recorded no delivery at all (the relay answered 502, was unreachable, "
            "or had no key; for a job on `deliver: \"all\"`, one of its legs failed outright). `partial`: the relay "
            "landed on some platforms and not the ones listed. `degraded`: it was posted but the Chat Agent's turn failed.",
            "",
            "**What to check.**",
            "- The relay route: `logs/session_kv_server.log` in the agent home, around each last-failure time.",
            "- The profile's own view: `hermes cron list` in the profile shows `last_delivery_error`.",
            "- For a `partial`: whether the platform named has a home channel configured and a working connection.",
            "- For a `hard`: whether every enabled chat platform is really configured on this install.",
            "",
            f"{FOOTER_PREFIX} `chat_delivery_watch.py`; updated {now}. It closes this issue itself once every leg has recovered._",
        ]
    )
    return title, body


def fingerprint(title: str, body: str) -> str:
    digest = hashlib.sha256()
    digest.update(title.encode("utf-8"))
    digest.update(b"\0")
    # The timestamp line changes every tick; everything above it is the state.
    digest.update(body.rsplit("\n" + FOOTER_PREFIX, 1)[0].encode("utf-8"))
    return digest.hexdigest()


def reconcile_issue(repo: str, degraded: list[tuple[str, dict]], state: dict, now: str, threshold_value: int, *, recovering: bool = False) -> str:
    """Bring the ledger issue in line with `degraded`; returns the `ledger=` value for the ALERT lines.

    `recovering` says this tick saw a job come back after alerting, which is
    when an issue whose number the ledger no longer holds is worth one lookup.
    """
    ledger = state["ledger"]
    if ledger.get("issue_number") is not None and ledger.get("repo") and ledger.get("repo") != repo:
        # The ledger repository changed under an open issue: close it there so
        # it is not left open forever, and start afresh in the new one.
        gh(
            ["issue", "close", str(ledger["issue_number"]), "-R", ledger["repo"], "--reason", CLOSE_REASON, "--comment",
             f"The delivery ledger moved to {repo} as of {now}; closing this copy."],
            ledger["repo"],
        )
        state["ledger"] = ledger = {"repo": None, "issue_number": None, "fingerprint": None}
    if degraded:
        # Listed every time rather than trusting the cached number: an issue a
        # person closed by hand must not keep being edited while closed.
        number = find_ledger_issue(repo)
        title, body = render_issue(degraded, now, threshold_value)
        digest = fingerprint(title, body)
        if number is None:
            ensure_label(repo)
            result = gh(
                ["issue", "create", "-R", repo, "--title", title, "--body-file", forge.BODY_STDIN, "--label", LABEL],
                repo,
                stdin=body,
            )
            match = ISSUE_URL_RE.search(result.stdout or "")
            number = int(match.group(1)) if match else None
        elif digest != ledger.get("fingerprint") or number != ledger.get("issue_number"):
            gh(["issue", "edit", str(number), "-R", repo, "--title", title, "--body-file", forge.BODY_STDIN], repo, stdin=body)
        state["ledger"] = {"repo": repo, "issue_number": number, "fingerprint": digest}
        return f"{repo}#{number}" if number else repo
    number = ledger.get("issue_number") if ledger.get("repo") == repo else None
    if number is None and recovering:
        # The number can be lost (a create whose URL did not parse, a replaced
        # state file); a tick that just saw a recovery looks the issue up once.
        number = find_ledger_issue(repo)
    if number is not None:
        # One call, so a close that fails cannot leave a comment behind to be
        # repeated on every later tick.
        gh(
            ["issue", "close", str(number), "-R", repo, "--reason", CLOSE_REASON, "--comment",
             f"Every scheduled report reached chat again as of {now}; closing."],
            repo,
        )
        state["ledger"] = {"repo": None, "issue_number": None, "fingerprint": None}
        return LEDGER_CLOSED
    return LEDGER_NONE


class LedgerRepoAmbiguous(RuntimeError):
    """More than one managed repository and no `CHAT_DELIVERY_LEDGER_REPO` to choose."""


def ledger_repo() -> str | None:
    """The ledger repository, or None for log-only mode.

    The override wins; otherwise the one managed GitHub repository. Several
    managed repositories and no override is refused rather than guessed, the
    way `gitops_workspace.resolve_repo` and the audit ledger refuse it.
    """
    configured = os.environ.get(LEDGER_REPO_ENV, "").strip()
    if configured:
        return configured
    repos = sorted(gitops_workspace.get_managed_github_repos())
    if len(repos) > 1:
        raise LedgerRepoAmbiguous(f"{len(repos)} managed repositories ({', '.join(repos)}); set {LEDGER_REPO_ENV}")
    return repos[0] if repos else None


# --- the ALERT lines ----------------------------------------------------------


def format_alert(key: str, entry: dict, threshold_value: int, ledger: str) -> str:
    profile, job_id = key.split("/", 1)
    return " ".join(
        [
            LOG_PREFIX,
            f"job={job_id}",
            f"profile={profile}",
            f"grade={entry.get('grade')}",
            f"streak={entry.get('streak')}",
            f"threshold={threshold_value}",
            f"platforms={','.join(entry.get('platforms') or []) or EMPTY_FIELD}",
            f"since={entry.get('first_failure_at')}",
            f"ledger={ledger}",
            f"error={json.dumps(entry.get('last_error') or '', ensure_ascii=False)}",
        ]
    )


def format_recovery(key: str, ledger: str) -> str:
    profile, job_id = key.split("/", 1)
    return f"{LOG_PREFIX} job={job_id} profile={profile} recovered=true streak=0 ledger={ledger}"


def format_self_error(exc: BaseException) -> str:
    return f"{LOG_PREFIX} self=error kind={type(exc).__name__} detail={json.dumps(str(exc)[:MAX_ERROR_CHARS])}"


def emit(lines: list[str], agent_home: Path, now: str) -> None:
    """Print each line and append it, timestamped, to the file fluent-bit ships."""
    if not lines:
        return
    for line in lines:
        print(line)
    log_path = agent_home / LOGS_DIR / ALERT_FILE_NAME
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("".join(f"{now} {line}\n" for line in lines))
    except OSError as exc:
        print(f"{WARN_PREFIX} detail={json.dumps(f'could not append to {log_path}: {exc}')}")


# --- the tick -----------------------------------------------------------------


def parse_roster_arg(value: str) -> tuple[str, Path]:
    profile, sep, path = value.partition("=")
    if not sep or not profile or not path:
        raise argparse.ArgumentTypeError("expected PROFILE=PATH")
    return profile, Path(path)


def tick(agent_home: Path, state_path: Path, rosters: list[tuple[str, Path]], *, dry_run: bool) -> list[str]:
    """One pass: advance every streak, reconcile the issue, return the ALERT lines."""
    now = now_iso()
    limit = threshold()
    state = load_state(state_path)
    previous = state["jobs"]
    current: dict[str, dict] = {}
    unreadable: list[str] = []

    for profile, store in rosters:
        try:
            jobs = load_jobs(store)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            unreadable.append(f"{profile}: {exc}")
            continue
        for job in jobs:
            key = f"{profile}/{job['id']}"
            entry = previous.get(key) or new_entry()
            silent = has_output = False
            error = job.get("last_delivery_error")
            if run_changed(entry, job) and (not error or is_delivered_note(str(error))):
                document = newest_output(store, job["id"], job.get("last_run_at"))
                has_output = document is not None
                silent = bool(document) and is_silent_document(document)
            current[key] = advance(entry, job, silent=silent, has_output=has_output)

    degraded = [(k, e) for k, e in current.items() if int(e.get("streak") or 0) >= limit]
    recovered = [k for k, e in current.items() if e.get("alerted") and int(e.get("streak") or 0) == 0]

    ledger_ref = LEDGER_DRY_RUN if dry_run else LEDGER_NONE
    lines: list[str] = []
    # GitHub is consulted only when there is something to say or something to
    # close: a quiet tick makes no call at all, and a tick on an install with no
    # forge configured does not log a discovery failure every half hour.
    if not dry_run and (degraded or recovered or state["ledger"].get("issue_number") is not None):
        # Finding the repository and talking to GitHub are both allowed to fail;
        # the log line below is the channel that must not depend on either.
        try:
            repo = ledger_repo()
        except Exception as exc:  # noqa: BLE001 - discovery shells out; any failure means log-only
            repo = None
            ledger_ref = f"error:{type(exc).__name__}"
            lines.append(format_self_error(exc))
            if not degraded and state["ledger"].get("repo"):
                # Nothing to report and an issue we opened is still out there:
                # the repository we recorded is enough to close it.
                repo = state["ledger"]["repo"]
        if repo:
            try:
                ledger_ref = reconcile_issue(repo, degraded, state, now, limit, recovering=bool(recovered))
            except Exception as exc:  # noqa: BLE001 - whatever GitHub does, the ALERT lines below still go out
                ledger_ref = f"error:{type(exc).__name__}"
                lines.append(format_self_error(exc))
    for key, entry in sorted(degraded):
        lines.append(format_alert(key, entry, limit, ledger_ref))
        entry["alerted"] = True
    for key in sorted(recovered):
        lines.append(format_recovery(key, ledger_ref if ledger_ref == LEDGER_CLOSED else LEDGER_NONE))
        current[key]["alerted"] = False
    for note in unreadable:
        lines.append(f"{WARN_PREFIX} detail={json.dumps(f'unreadable cron store {note}')}")

    state["jobs"] = current
    state["last_tick_at"] = now
    state["last_tick_ok"] = True
    state["last_tick_error"] = None
    if dry_run:
        for line in lines:
            print(line)
        return lines
    save_state(state_path, state)
    emit(lines, agent_home, now)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="compute and print; write no state and touch no issue")
    parser.add_argument("--state", type=Path, help=f"ledger path (default: <home>/profiles/platform/cron/{STATE_FILE_NAME}, or ${STATE_PATH_ENV})")
    parser.add_argument("--roster", type=parse_roster_arg, action="append", help="PROFILE=PATH; scan only these stores (repeatable)")
    args = parser.parse_args(argv)

    agent_home = Path(gitops_workspace.agent_home())
    state_path = args.state or Path(os.environ.get(STATE_PATH_ENV) or agent_home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / STATE_FILE_NAME)
    rosters = args.roster or roster_paths(agent_home)
    try:
        tick(agent_home, state_path, rosters, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - the cron path must exit 0 and say why on the log channel
        now = now_iso()
        if args.dry_run:
            print(format_self_error(exc))
            return 0
        emit([format_self_error(exc)], agent_home, now)
        try:
            state = load_state(state_path)
            state["last_tick_at"] = now
            state["last_tick_ok"] = False
            state["last_tick_error"] = str(exc)[:MAX_ERROR_CHARS]
            if not args.dry_run:
                save_state(state_path, state)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
