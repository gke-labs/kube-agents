#!/usr/bin/env python3
"""Post the gate's health to Google Chat -- on state changes, plus one digest a day.

health.py decides GREEN / DEGRADED / OUTAGE every tick; this is the half that
tells people, and its whole design is about NOT telling them most of the
time. It reads the current health.json and the state it last posted, and
sends a message only when:

    the state changed                       -> "CI health: DEGRADED (was GREEN)"
    the state returned to GREEN             -> the recovery, with how long it lasted
    an OUTAGE grew to name a new case       -> the same shape, rate-limited
    it is the digest hour and none went out -> the daily digest with the 24h numbers,
                                               plus one line on last night's
                                               nightly run when --data is given

Everything else is silence. The last-posted state lives in a small JSON file
(`--state`, a local path or a gs:// object) that this script is the only
writer of; the scheduled job is otherwise stateless.

A fourth message, rarer than the others: when health.json reports that
data.json itself has stopped refreshing (`stale`), the space is told once,
and once more when it resumes -- a silent stall would otherwise freeze the
state and keep the digest reporting old numbers as current. A fifth, of the
same shape: when the hourly seeded-fleet scan could check no pool project
at all (health.json's `fixture_state.unknown`, the bot's grant missing), the
space is told once, and once more when the scan sees the fleet again. That
is never a drift. The digest carries one line on the latest scan.

A fifth, one line and once per episode: when health.json carries a `slow`
note (the gate's green runs are taking far longer than usual, #1586), the
space hears it the first tick it appears and not again until it has cleared
and come back; the digest repeats the line while it lasts. It is not a state
change -- nothing is broken and /retest does not help -- so it moves nothing
else.

Every time a reader sees is on the reader's clock: America/Toronto, written
"7:30 AM ET", never UTC (the deep links and the state file keep ISO UTC).
The digest hour is a Toronto hour too, and "once a day" is a Toronto day.

One side effect beyond posting: on a new OUTAGE with no tracking issue --
none in case-notes.yaml, none open under the `presubmit-gate` label naming
the same cases -- gate_issue.py files one with the workflow's GitHub token
(`gh api`, GH_TOKEN), the "broken" message says "Tracking #NNN", and the
recovery comments on it. A new `lost_pods` condition (the build cluster lost
the nodes under running jobs, #1478) files one the same way, addressed to
the cluster owner, unless an open `presubmit-gate` issue already names the
lost nodes. A new `fixture_drift` condition (a seeded fixture out of its
designed state on two consecutive hourly scans or on three pool projects at
once, #1550) files one for the fleet owner the same way, unless an open
`presubmit-gate` issue already names the drifted roles. It never closes an
issue. A GitHub failure is a warning: the message goes out with "no issue
yet" and the next change asks again.

Delivery is the Google Chat REST API with the job's service account acting
as a Chat app: POST https://chat.googleapis.com/v1/{space}/messages with an
OAuth token bearing the chat.bot scope (the workflow mints one with `gcloud
auth print-access-token --scopes=...` and passes it in CI_HEALTH_CHAT_TOKEN;
incoming webhooks are disabled org-wide). The space id comes from
CI_HEALTH_CHAT_SPACE (`spaces/XXXX`, not a secret). An incoming-webhook URL
in CI_HEALTH_CHAT_WEBHOOK is the optional alternative, used when the space
and token are not both present. Neither the token nor the webhook URL is
ever printed: a failure logs the HTTP status and nothing from the request.
With nothing configured the script says so and exits 0 -- the job must not
fail while the space is being set up.

Run:  python3 scripts/eval_dashboard/post_health.py --health health.json --state state.json --dry-run
Test: cd scripts && python3 -m unittest test_eval_dashboard_post_health
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from eval_dashboard import gate_issue, ghcli, nightly

    # By name, not as a module: `health` is the parameter every render_*
    # function here takes, and importing the module would shadow it.
    from eval_dashboard.health import POOL_BREACH, POOL_STALE, POOL_UNMEASURED, minutes_text, pool_span, wait_text
except ImportError:  # run as a script: scripts/eval_dashboard/post_health.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import gate_issue, ghcli, nightly
    from eval_dashboard.health import POOL_BREACH, POOL_STALE, POOL_UNMEASURED, minutes_text, pool_span, wait_text

STATE_SCHEMA_VERSION = 1

# The reader's clock. Every time rendered into a message is converted here
# and written "7:30 AM ET"; DST is zoneinfo's problem, not ours. The zone
# and its label are fixed together: --digest-tz moves only the digest's
# clock, never the wording, so the label cannot drift from the zone.
DEFAULT_TZ = "America/Toronto"
TZ_LABEL = "ET"
LOCAL_TZ = ZoneInfo(DEFAULT_TZ)
AM, PM = "AM", "PM"
NOON = 12

# health.json's vocabulary (scripts/eval_dashboard/health.py owns it).
GREEN = "GREEN"
OUTAGE = "OUTAGE"
CONDITION_LOST_PODS = "lost_pods"
CONDITION_SHARED_BREAK = "shared_break"
CONDITION_FIXTURE_DRIFT = "fixture_drift"
CONDITION_DELEGATION_CEILING = "delegation_ceiling"
CONDITION_DEADLINE_KILL = "deadline_kill"
# The presubmit job's timeout in minutes (health.py PROW_JOB_TIMEOUT owns it;
# copied so this module imports nothing that reads data.json).
DEADLINE_MINUTES = 360
# health.json's summary of the hourly seeded-fleet scan (health.py,
# fixture_state_block); absent before the scan has ever published.
FIXTURE_STATE_KEY = "fixture_state"
# The 24h window health.py reports metrics over, for a health.json that
# predates the `window_hours` field.
DEFAULT_WINDOW_HOURS = 24
# The trailing window health.py's slow-gate note measures "usual" over, for
# a note without `baseline_days`.
DEFAULT_SLOW_BASELINE_DAYS = 7

# pool-pressure.json's vocabulary (scripts/pool_pressure.py owns it), carried
# through health.json's `pool` note. Copied rather than imported, and
# test_integration_contracts.py fails if the copy drifts. The four causes have
# four different remedies, one of which spends money, so the message branches
# on them rather than printing the label.
CAUSE_CAPACITY = "CAPACITY"
CAUSE_CONCURRENCY_CAP = "CONCURRENCY_CAP"
CAUSE_CONTROL_PLANE = "CONTROL_PLANE"
# Not a cause the periodic emits: the key `pool_causes` records when
# CONTROL_PLANE is announced without the queue, which says something different
# and names no build cluster. See `pool_cause_key`.
CAUSE_CONTROL_PLANE_UNREAD = "CONTROL_PLANE:queue-unread"
# The periodic's own "I could not tell", distinct from a label added later that
# this file has no remedy for. Both ask for nothing; only this one knows why.
CAUSE_UNKNOWN = "UNKNOWN"

# The kinds of message this script sends.
KIND_CHANGE = "change"  # a new state, condition or (in an OUTAGE) case list
KIND_RECOVERY = "recovery"  # back to GREEN, with how long it took
KIND_STALE = "stale"  # data.json stopped refreshing, or started again
KIND_SLOW = "slow"  # the gate's runs are far longer than usual; once per episode
KIND_POOL = "pool"  # runs are waiting to start; once per episode
KIND_POOL_CLEAR = "pool_clear"  # ... and once when they stop
KIND_DIGEST = "digest"  # the daily numbers
KIND_FIXTURE_SCAN = "fixture_scan"  # the fleet scan sees nothing, or sees again

# Where the message goes. The space is a resource name, the token a bearer
# credential minted by the workflow; the webhook is the legacy alternative.
SPACE_ENV = "CI_HEALTH_CHAT_SPACE"
TOKEN_ENV = "CI_HEALTH_CHAT_TOKEN"
WEBHOOK_ENV = "CI_HEALTH_CHAT_WEBHOOK"
CHAT_API_ROOT = "https://chat.googleapis.com/v1"
CHAT_MESSAGES_PATH = "{space}/messages"
CHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
SPACE_PREFIX = "spaces/"
NOT_CONFIGURED = "webhook not configured: set CI_HEALTH_CHAT_SPACE (+ CI_HEALTH_CHAT_TOKEN) or CI_HEALTH_CHAT_WEBHOOK; nothing posted"
REQUEST_TIMEOUT_S = 30
USER_AGENT = "kube-agents-ci-health"

# The digest goes out once per local day (`--digest-tz`, Toronto by
# default), on the first tick inside [digest_hour:00 - window, digest_hour:00
# + window] local time. The job runs every 15 minutes, so a 20-minute window
# always contains at least one tick and the per-day marker in the state file
# (the local date) stops the second one from repeating it.
DEFAULT_DIGEST_HOUR = 9
DIGEST_WINDOW = timedelta(minutes=20)

# Inside an OUTAGE the cause grows as more cases collapse. Each growth is
# worth a message -- a reader deciding whether their red is the outage needs
# the current list -- but on 2026-09-02 the list changed nine times in ten
# hours, so a re-post needs a new case to have joined, and at most one per
# interval. A case dropping off is not news until the state changes.
OUTAGE_REPOST_INTERVAL = timedelta(hours=2)

DASHBOARD_URL = "https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html"
# The directory the pages are published in; the PR view lives beside the Brief.
DASHBOARD_SITE = DASHBOARD_URL.rsplit("/", 1)[0]
DASHBOARD_RUN_PAGE = "run.html"
# Every message ends with a deep link into the dashboard, on a line of its
# own so Chat auto-links it. The shape is a contract with the dashboard
# (`linkState()` in template/pages.js reads it; SCHEMA.md states it): the
# whole scope travels in the URL fragment,
# `#since=<ISO 8601 UTC>[&until=<ISO 8601 UTC>][&cases=<comma-separated
# case ids>]&view=gate` for an incident message and `view=agent` for the
# digest, `run.html#build=<prow build id>` for one run. The fragment
# because the published host's login redirect drops a query string and a
# browser carries the fragment through a redirect. Commas and colons stay
# literal. dashboard_link and run_link are the only Python writers of
# these shapes: gate_comment.py imports them and gate_issue.py is handed
# the finished link, so neither spells a second copy.
DASHBOARD_VIEW_GATE = "gate"
DASHBOARD_VIEW_AGENT = "agent"
# The Nightly report page beside the Brief; the digest's nightly line links
# to it (nightly.py derives both the line and the page's data).
NIGHTLY_URL = f"{DASHBOARD_SITE}/{nightly.NIGHTLY_PAGE}"

# The message wording. One sentence of cause, one of what to do, then the
# link; the details live behind the link. Case names are read by a human
# deciding "is it me?": a family of cases is named by its family word, the
# prefix tokens below being too generic to be one.
GENERIC_TOKENS = frozenset({"cluster", "agent", "the", "a"})
CASES_NAMED_IN_FULL = 3
NO_ISSUE_TEXT = "no issue yet — file one with the presubmit-gate label"
# Mirrors health.py's STORM_COOLDOWN: a run started the minute the last
# storm-hit run finished still overlaps its tail.
STORM_COOLDOWN = timedelta(minutes=30)
# Where the fleet scan's grant and semantics are written down, for the
# message that says the scan is blind.
FIXTURE_SCAN_DOC = "docs/ci-health.md"
FIXTURE_RECONCILE_HINT = "Fleet owner: re-apply bench/tf/fleet in the projects named."

# Rule 8 sends the reader somewhere. The build cluster is named by its real
# identifiers because `build-kube-agents` is Prow's context alias for it
# (oss-test-infra prow/oss/cluster/kubeconfigs/kubeconfigs.yaml), and the alias
# finds nothing in kubectl.
POOL_BUILD_CLUSTER = "kube-agents-prow"
POOL_BUILD_PROJECT = "kube-agents-prow"
POOL_PRESSURE_JOB = "ci-kube-agents-pool-pressure"
POOL_JOB_HISTORY_URL = (
    f"https://oss.gprow.dev/job-history/gs/kube-agents-prow/logs/{POOL_PRESSURE_JOB}"
)

# gsutil is how the state object is read and written; publish.py uses the
# same header so a reader never gets an hour-stale copy.
GSUTIL = "gsutil"
GS_PREFIX = "gs://"
CACHE_CONTROL = "Cache-Control: no-cache"

UTC = timezone.utc


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #


def read_state(location: str, runner=subprocess.run) -> dict | None:
    """The last posted state, or None when there is none yet."""
    if location.startswith(GS_PREFIX):
        result = runner([GSUTIL, "-q", "cat", location], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        text = result.stdout
    else:
        path = pathlib.Path(location)
        if not path.is_file():
            return None
        text = path.read_text()
    try:
        loaded = json.loads(text)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def write_state(location: str, state: dict, runner=subprocess.run) -> None:
    text = json.dumps(state, indent=2) + "\n"
    if location.startswith(GS_PREFIX):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write(text)
            name = handle.name
        try:
            runner([GSUTIL, "-q", "-h", CACHE_CONTROL, "cp", name, location], check=True)
        finally:
            os.unlink(name)
        return
    pathlib.Path(location).write_text(text)


# --------------------------------------------------------------------------- #
# Deciding what to say
# --------------------------------------------------------------------------- #


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def local_date(now: datetime, tz=LOCAL_TZ) -> str:
    """The per-day digest marker: the date on the reader's clock."""
    return now.astimezone(tz).date().isoformat()


def in_digest_window(now: datetime, digest_hour: int, tz=LOCAL_TZ) -> bool:
    local = now.astimezone(tz)
    anchor = local.replace(hour=digest_hour, minute=0, second=0, microsecond=0)
    return anchor - DIGEST_WINDOW <= local <= anchor + DIGEST_WINDOW


def pool_was_read(health: dict) -> bool:
    """Whether the tick saw the artifact at all.

    A note proves it. Without one the flag decides, because an absent note
    means a healthy pool or a failed fetch and the two want opposite handling.
    `queue_wait_p50_s` cannot stand in: it is also None on a day with no runs.
    """
    if health.get("pool"):
        return True
    return bool((health.get("metrics") or {}).get("queue_wait_read"))


def pool_advisable(pool: dict, drained: bool = False) -> bool:
    """Whether the note is worth posting, and worth recording as told. The two
    answers have to match: a breach withheld here but written to `pool_verdict`
    reads later as already said, and the next live queue under the same cause
    would go unannounced.

    A breach needs a live backlog, because the verdict lasts a week while the
    remedy is read fresh each hour. Unknown is not a refusal -- an unreadable
    queue withholds nothing, and `pool_cause_text` drops the diagnosis instead
    -- except after the queue was last seen drained, when a Deck that fails
    every other hour would announce a jam nothing has measured since.
    """
    if pool.get("verdict") != POOL_BREACH:
        return True
    live = pool.get("waiting_now")
    return live is True or (live is None and not drained)


def pool_cause_key(pool: dict) -> str | None:
    """What a ⏳ would tell the reader, which is not always its cause.

    `pool_causes` is what this episode has already said, and CONTROL_PLANE says
    two things: the build cluster to go and check, or that the queue could not
    be read so nothing can be blamed. Keyed on the cause alone, an hour of
    unreadable Deck at the start of an episode would record the vague one as
    the remedy and the remedy would never post.
    """
    if pool.get("cause") == CAUSE_CONTROL_PLANE and pool.get("waiting_now") is None:
        return CAUSE_CONTROL_PLANE_UNREAD
    return pool.get("cause")


def pool_told_keys(pool: dict) -> list[str]:
    """The keys a sent ⏳ marks as told, which the diagnosis widens.

    Once the build cluster has been named, the unread-queue message is less
    than the reader already has, so it is not owed later in the same episode.
    """
    key = pool_cause_key(pool)
    return [key, CAUSE_CONTROL_PLANE_UNREAD] if key == CAUSE_CONTROL_PLANE else [key]


def decide(health: dict, prev: dict | None, now: datetime, digest_hour: int, tz=LOCAL_TZ) -> list[str]:
    """Which message kinds go out this tick.

    A change is a new state, a new condition within the same state (a storm
    giving way to setup deaths is different advice), or -- inside an
    OUTAGE -- a new case joining, rate-limited. Staleness flipping either
    way is its own kind. The digest is independent of all of them.
    """
    kinds = []
    state = health.get("state")
    prev_state = (prev or {}).get("state")
    if prev is None:
        # First tick ever. A non-green start is worth a message; a green
        # one is not -- nobody needs to hear that nothing is wrong.
        if state != GREEN:
            kinds.append(KIND_CHANGE)
    elif state != prev_state:
        kinds.append(KIND_RECOVERY if state == GREEN else KIND_CHANGE)
    elif health.get("condition") != prev.get("condition"):
        kinds.append(KIND_CHANGE)
    elif state == OUTAGE:
        new_cases = set(health.get("failing_cases") or []) - set(prev.get("failing_cases") or [])
        last = parse_iso(prev.get("posted_at"))
        if new_cases and (last is None or now - last >= OUTAGE_REPOST_INTERVAL):
            kinds.append(KIND_CHANGE)

    if bool(health.get("stale")) != bool((prev or {}).get("stale")):
        kinds.append(KIND_STALE)
    if fixture_unknown(health) != bool((prev or {}).get("fixture_unknown")):
        kinds.append(KIND_FIXTURE_SCAN)

    # The slow note goes out when the note appears, not when it clears: the
    # digest carries it while it lasts, and "back to normal" is not news.
    if health.get("slow") and not (prev or {}).get("slow"):
        kinds.append(KIND_SLOW)

    # Rule 8, once per episode, plus a re-post on a verdict change or a cause
    # not yet named this episode. A breach message also needs a live queue: the
    # verdict spans seven days while the remedy is read live, so one bad day
    # keeps the verdict for a week and the remedy tracks a pool that has since
    # drained. `over_threshold` shares the cause's instant. The two monitoring
    # verdicts are exempt -- neither advises anything.
    pool = health.get("pool") or {}
    told = prev or {}
    if (
        pool
        and pool_advisable(pool, bool(told.get("pool_drained")))
        and (
            pool.get("verdict") != told.get("pool_verdict")
            or pool_cause_key(pool) not in (told.get("pool_causes") or [])
        )
    ):
        kinds.append(KIND_POOL)
    # Unlike rule 7, rule 8 says when it is over. The periodic judges a rolling
    # seven-day window, so an episode outlives the bad day by up to a week and
    # "it cleared" is news rather than noise. Two limits on it. Only a breach
    # clears: a ⚪ monitoring episode never claimed the queue was bad, so
    # "starting on time again" would assert what nothing measured. And only on
    # a reading: the note disappears when the artifact does, and that is the
    # bot going blind, not the queue draining.
    #
    # The question is whether this episode ever breached, not what it said
    # last. A breach whose periodic then dies goes ⚪, and reading the last
    # verdict would owe that episode no ✅ however the queue ends up.
    elif not pool and (prev or {}).get("pool_breached") and pool_was_read(health):
        kinds.append(KIND_POOL_CLEAR)

    if in_digest_window(now, digest_hour, tz) and (prev or {}).get("last_digest_date") != local_date(now, tz):
        kinds.append(KIND_DIGEST)
    return kinds


# --------------------------------------------------------------------------- #
# Rendering: plain Chat text, *bold*, bare URLs
# --------------------------------------------------------------------------- #


def duration_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def seconds_text(seconds) -> str:
    if seconds is None:
        return "n/a"
    return duration_text(timedelta(seconds=seconds))


def percent(value) -> str:
    return "n/a" if value is None else f"{round(value * 100)}%"


def clock(value: datetime | None, weekday: bool = False, label: bool = True) -> str:
    """A time on the reader's clock: "7:30 AM ET", "Sun 7:30 AM ET" with
    `weekday`, and without the zone label when it ends a range whose second
    half carries it ("1:15 PM–2:25 PM ET")."""
    if not value:
        return "?"
    local = value.astimezone(LOCAL_TZ)
    text = f"{local.hour % NOON or NOON}:{local.minute:02d} {AM if local.hour < NOON else PM}"
    if weekday:
        text = f"{local.strftime('%a')} {text}"
    return f"{text} {TZ_LABEL}" if label else text


def clock_range(start: datetime | None, end: datetime | None) -> str:
    return f"{clock(start, label=False)}–{clock(end)}"


def iso_z(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def dashboard_link(view: str, cases=(), since: datetime | None = None, until: datetime | None = None) -> str:
    """The deep link into the Brief (see DASHBOARD_VIEW_*): every parameter
    in the fragment, `view` last, empty parameters omitted, nothing
    percent-encoded -- case ids are slugs and the timestamps are the `Z`
    form."""
    params = []
    if since:
        params.append(f"since={iso_z(since)}")
    if until:
        params.append(f"until={iso_z(until)}")
    if cases:
        params.append("cases=" + ",".join(cases))
    params.append(f"view={view}")
    return f"{DASHBOARD_URL}#{'&'.join(params)}"


def incident_link(health: dict, until: datetime | None = None) -> str:
    return dashboard_link(DASHBOARD_VIEW_GATE, health.get("failing_cases") or [], parse_iso(health.get("since")), until)


def run_link(build_id) -> str:
    """The PR view for one prow build: `run.html#build=<id>`."""
    return f"{DASHBOARD_SITE}/{DASHBOARD_RUN_PAGE}#build={build_id}"


def describe_cases(cases) -> str:
    """The failing cases in the words a reader scans: one case by name, a
    family by its family word ("the 3 crashloop tests"), up to three by
    name, more as a count with the first three."""
    cases = list(cases)
    if not cases:
        return "tests"
    if len(cases) == 1:
        return cases[0]
    tokens = [case.split("-") for case in cases]
    shared = 0
    while all(len(t) > shared + 1 and t[shared] == tokens[0][shared] for t in tokens):
        shared += 1
    family = [t for t in tokens[0][:shared] if t not in GENERIC_TOKENS]
    if family:
        return f"the {len(cases)} {family[-1]} tests"
    if len(cases) <= CASES_NAMED_IN_FULL:
        return ", ".join(cases[:-1]) + " and " + cases[-1]
    return f"{len(cases)} tests ({', '.join(cases[:CASES_NAMED_IN_FULL])} and {len(cases) - CASES_NAMED_IN_FULL} more)"


def issue_tag(issue) -> str | None:
    """"#1278" for an {number, url} the bot filed or found; None otherwise."""
    number = (issue or {}).get("number") if isinstance(issue, dict) else None
    return f"#{number}" if number else None


def issue_for(issue, condition: str | None) -> dict | None:
    """The issue when it was filed for this condition (its `condition` key,
    gate_issue.as_issue); one without the key predates it and is an
    outage's, the only kind filed then. Mirrors health.py's issue_for."""
    if not issue_tag(issue):
        return None
    return issue if (issue.get("condition") or CONDITION_SHARED_BREAK) == condition else None


def episode_issues(state: dict) -> list[dict]:
    """Every issue the recorded episode filed or adopted: the state's
    `issues`, plus its `issue` when a state written before `issues` existed
    holds one; deduplicated by number, oldest first."""
    out: list[dict] = []
    for candidate in [*(state.get("issues") or []), state.get("issue")]:
        tag = issue_tag(candidate)
        if tag and tag not in {issue_tag(seen) for seen in out}:
            out.append(candidate)
    return out


def tracking_text(issues, issue=None) -> str:
    issues = list(issues or [])
    tag = issue_tag(issue)
    if tag and tag not in issues:
        issues.append(tag)
    return ", ".join(issues) if issues else NO_ISSUE_TEXT


def nodes_text(nodes) -> str:
    """"a node" or "5 nodes": how many the build cluster lost."""
    count = len(nodes or {})
    return f"{count} nodes" if count > 1 else "a node"


def fixture_state_of(health: dict) -> dict:
    block = health.get(FIXTURE_STATE_KEY)
    return block if isinstance(block, dict) else {}


def fixture_unknown(health: dict) -> bool:
    """The latest scan could check no pool project (the grant is missing,
    or kubectl is): the bot is blind to the fleet, which is not a drift."""
    return bool(fixture_state_of(health).get("unknown"))


def plural(count: int, one: str, many: str | None = None) -> str:
    return one if count == 1 else (many if many is not None else one + "s")


def fixture_drift_sentence(health: dict, since: str) -> str:
    incident = health.get("incident") or {}
    roles = list(incident.get("roles") or [])
    projects = list(incident.get("projects") or [])
    return (
        f"seeded {plural(len(roles), 'fixture')} {', '.join(roles) or '(unnamed)'} out of designed state"
        f" on {len(projects)} pool {plural(len(projects), 'project')} since {since};"
        f" a red on a case that depends on {plural(len(roles), 'it', 'them')} from a run that leased one of those projects is the fixture, not the code."
    )


def cause_sentence(health: dict) -> str:
    """One sentence a reader can answer "is it me?" from."""
    incident = health.get("incident") or {}
    prs = len(incident.get("prs") or [])
    since = clock(parse_iso(health.get("since")))
    condition = health.get("condition")
    if condition == CONDITION_LOST_PODS:
        when = clock(parse_iso(incident.get("window_start"))) if incident.get("window_start") else since
        runs = incident.get("runs", 0)
        if incident.get("event"):
            return f"the build cluster lost {nodes_text(incident.get('nodes'))} at {when}; {runs} runs on {prs} PRs died mid-run."
        return f"{runs} runs on {prs} PRs died with their build node at {when}."
    if condition == CONDITION_DEADLINE_KILL:
        start, end = parse_iso(incident.get("window_start")), parse_iso(incident.get("window_end"))
        window = clock_range(start, end) if start and end else f"since {since}"
        return (
            f"{incident.get('runs', 0)} runs on {prs} PRs were killed at the {DEADLINE_MINUTES}-minute deadline with no verdict {window};"
            " nothing is being graded, so the gate cannot pass anyone."
        )
    if condition == "shared_break":
        return (
            f"{describe_cases(health.get('failing_cases'))} fail on every PR since {since}"
            f" ({prs} PRs so far). Shared test fixture, not your code."
        )
    if condition == "storm":
        start, end = parse_iso(incident.get("window_start")), parse_iso(incident.get("window_end"))
        window = clock_range(start, end) if start and end else f"since {since}"
        return f"quota storm {window} hit {prs} PRs."
    if condition == "setup_deaths":
        return f"{incident.get('runs', 0)} runs on {prs} PRs died during setup since {since}."
    if condition == CONDITION_FIXTURE_DRIFT:
        return fixture_drift_sentence(health, since)
    if condition == CONDITION_DELEGATION_CEILING:
        start, end = parse_iso(incident.get("window_start")), parse_iso(incident.get("window_end"))
        window = clock_range(start, end) if start and end else f"since {since}"
        return (
            f"{incident.get('reps', 0)} repetitions on {prs} PRs ended with the worker still running {window};"
            " nothing was graded and nothing counts against a case."
        )
    return health.get("cause") or "no single cause"


def render_change(health: dict, prev: dict | None, issue: dict | None = None) -> str:
    condition = health.get("condition")
    if health.get("state") == OUTAGE:
        lines = [
            f"🔴 *Smoke gate: broken* — {cause_sentence(health)}",
            f"Don't retest yet. Tracking {tracking_text(health.get('tracking_issues'), issue)}.",
        ]
    elif condition == "storm":
        end = parse_iso((health.get("incident") or {}).get("window_end"))
        when = f"after {clock(end + STORM_COOLDOWN)}" if end else "once the storm has passed"
        lines = [f"🟡 *Smoke gate: flaky* — {cause_sentence(health)}  Passing runs still count; if yours went red, retest {when}."]
    elif condition == CONDITION_DELEGATION_CEILING:
        lines = [
            f"🟡 *Smoke gate: flaky* — {cause_sentence(health)} Those runs read NOT EVALUATED, not red; retest once workers are"
            " finishing again. The gateway log in a run's artifacts says whether the dispatcher stalled (#1879)."
        ]
    elif condition == CONDITION_LOST_PODS:
        tag = issue_tag(issue)
        tracking = f" Tracking {tag}." if tag else ""
        lines = [f"🟡 *Smoke gate: flaky* — {cause_sentence(health)} Not your code; retest once new jobs are running.{tracking}"]
    elif condition == CONDITION_FIXTURE_DRIFT:
        tag = issue_tag(issue)
        tracking = f" Tracking {tag}." if tag else ""
        lines = [f"🟡 *Smoke gate: flaky* — {cause_sentence(health)} Retest once the fleet is re-applied. {FIXTURE_RECONCILE_HINT}{tracking}"]
    else:
        lines = [f"🟡 *Smoke gate: flaky* — {cause_sentence(health)}  Passing runs still count; if yours died before any test ran, retest."]
    lines.append(incident_link(health))
    return "\n".join(lines)


def render_stale(health: dict) -> str:
    refreshed = clock(parse_iso(health.get("generated_at")))
    if health.get("stale"):
        return f"⚪ *Smoke gate: no fresh data since {refreshed}* — the health bot can't see recent runs. Someone check the refresh job."
    return f"⚪ *Smoke gate: fresh data again* — refreshed {refreshed}; the gate reads {health.get('state', '?')}."


def figure(value) -> str:
    """A count from the pool note, which sets a field it could not read to None.
    Zero is a real reading -- `or "?"` would hide a cap of 0, the one worth
    saying out loud. (A `free` of 0 is not: cause() calls that CAPACITY.)"""
    return "?" if value is None else str(value)


def slow_text(slow: dict) -> str:
    """The numbers behind a slow gate, in minutes, as one clause: "the last
    5 full runs took 152–213 min (median 183) against a 7-day typical of
    151 min (p90 198); 2 reps lost to 429s"."""
    # health.py's storm_reps: 429s and empty records alike (rep_kind), so the
    # note names both rather than calling an empty record a 429.
    lost = f"{slow['infra_reps']} reps lost to 429s or empty records" if slow.get("infra_reps") else "no reps lost"
    return (
        f"the last {slow.get('runs', 0)} full runs took {minutes_text(slow.get('min_s'))}–{minutes_text(slow.get('max_s'))} min"
        f" (median {minutes_text(slow.get('median_s'))}) against a {slow.get('baseline_days', DEFAULT_SLOW_BASELINE_DAYS)}-day typical"
        f" of {minutes_text(slow.get('baseline_p50_s'))} min (p90 {minutes_text(slow.get('baseline_p90_s'))}); {lost}"
    )


def render_slow(health: dict) -> str:
    slow = health.get("slow") or {}
    return "\n".join(
        [
            f"🐢 *Smoke gate: slow* — {slow_text(slow)}. Not a break, and /retest won't make yours faster.",
            # No `since`: the Brief resolves a `since` to an incident, and the
            # note's start is a GREEN tick that names none, so the page would
            # show a synthetic past incident. The bare agent view is the
            # healthy headline, which carries the same sentence.
            dashboard_link(DASHBOARD_VIEW_AGENT),
        ]
    )


def pool_numbers(pool: dict) -> list[str]:
    """What tripped the verdict, with each figure beside its own limit.
    "against 15/45" makes the reader pair four numbers positionally, and gets
    it wrong. The periodic breaches on a day's row, on runs queued past p95
    right now, or on both, so the message quotes whichever it was -- the
    seven-day window it is not judged on can sit well inside its own limit.

    The recent stretch leads when the periodic could judge it, and the worst
    breached day stands in when it could not; `pool_note` picks between them
    and only one of `window_hours` and `day` survives that choice."""
    lines = []
    span = pool_span(pool)
    if span:
        lines.append(
            f"{span.capitalize()}: median wait {wait_text(pool.get('p50_s'))}"
            f" against a {minutes_text(pool.get('threshold_p50_s'))} min limit;"
            f" p95 {wait_text(pool.get('p95_s'))} against {minutes_text(pool.get('threshold_p95_s'))}."
        )
    waiting = pool.get("over_threshold") or 0
    if waiting:
        lines.append(
            f"{waiting} {plural(waiting, 'run')} waiting right now,"
            f" past the {minutes_text(pool.get('threshold_p95_s'))} min p95 limit."
        )
    return lines


def pool_cause_text(pool: dict) -> str:
    """What to do, by cause. The four remedies differ and one of them spends
    money, so an unrecognised cause falls through to the message that asks for
    nothing."""
    cause = pool.get("cause")
    if cause == CAUSE_CAPACITY:
        # The full pool is this hour's Boskos reading and carries the remedy on
        # its own. The clause needs a backlog Deck actually saw: unread, it would
        # assert one from a verdict up to a week old, and under the limit there
        # may be no run queued at all.
        queuing = " and runs are queuing" if pool.get("waiting_now") else ""
        return (
            f"*Smoke gate: pool full* — all {figure(pool.get('total'))} projects are leased"
            f"{queuing}. Consider onboarding a project."
        )
    if cause == CAUSE_CONCURRENCY_CAP:
        return (
            f"*Smoke gate: concurrency cap* — the pool has {figure(pool.get('total'))} projects"
            f" but the concurrency cap is only {figure(pool.get('max_concurrency'))}. Raise the cap."
        )
    if cause == CAUSE_CONTROL_PLANE:
        if pool.get("waiting_now") is None:
            # This cause is a residual: the pool looks fine, so Prow must be at
            # fault. That only follows while something is queued, and here the
            # queue was not read -- the free count is live, the waits can be six
            # days old. Send someone to the build cluster on that pairing and
            # they find nothing wrong, which is what the verdict's week-long
            # reach costs when nothing checks it.
            return (
                f"*Smoke gate: queue backed up* — {figure(pool.get('free'))} of"
                f" {figure(pool.get('total'))} projects are free, but the job could not read"
                " the queue, so this bot cannot say whether Prow or the pool is at fault."
            )
        # The queue and the occupancy are read in one pass, and both this and
        # decide() require a live backlog, so both describe one moment. That is
        # what used to need "looks like".
        return (
            f"*Smoke gate: runs not starting* — {figure(pool.get('free'))} of"
            f" {figure(pool.get('total'))} projects"
            " were free while runs waited, so this is Prow rather than the pool.\n"
            f"Check the build cluster: {POOL_BUILD_CLUSTER}, project {POOL_BUILD_PROJECT}."
        )
    if cause == CAUSE_UNKNOWN:
        return (
            "*Smoke gate: queue backed up* — cause unclear: the job couldn't read"
            " how many projects were in use."
        )
    # A label this file has no remedy for. Naming UNKNOWN's reason here would
    # be a diagnosis nothing supports, so say only what is known.
    return (
        "*Smoke gate: queue backed up* — cause unclear:"
        f" the check reported {cause or 'no cause'}, which this bot has no advice for."
    )


def render_pool(health: dict) -> str:
    """Rule 8. Two monitoring failures in ⚪, the colour this file already uses
    for the bot losing sight of its data; a real backlog in ⏳, headed by its
    cause."""
    pool = health.get("pool") or {}
    if pool.get("verdict") == POOL_STALE:
        # No numbers: a reading hours old is not evidence about now, and a
        # figure in the message gets read as current whatever the caveat says.
        measured = parse_iso(pool.get("measured_at"))
        # Two ways to stop: the job stops running, or it runs and publishes
        # nothing. The second has no last reading to quote, and the link is
        # what tells them apart.
        lost = (
            f"last reading {clock(measured)}; {POOL_PRESSURE_JOB} runs hourly and has missed the last few"
            if measured
            else f"{POOL_PRESSURE_JOB} ran but published no numbers"
        )
        return "\n".join(
            [
                f"⚪ *Smoke gate: pool check stopped* — {lost}."
                " If the next one doesn't land, it needs checking.",
                POOL_JOB_HISTORY_URL,
            ]
        )
    if pool.get("verdict") == POOL_UNMEASURED:
        return "\n".join(
            [
                "⚪ *Smoke gate: wait unknown* — the hourly pool check ran but couldn't read how long"
                " recent runs waited.",
                # Its own job's history, not the dashboard: the dashboard has
                # no number to show when this is the message.
                POOL_JOB_HISTORY_URL,
            ]
        )
    return "\n".join(
        [
            f"⏳ {pool_cause_text(pool)}",
            *pool_numbers(pool),
            "Runs still pass; /retest makes the queue longer.",
            # The agent view, not a scoped one: rule 8 rides beside the state
            # and can start mid-incident, so there is no window to scope to.
            dashboard_link(DASHBOARD_VIEW_AGENT),
        ]
    )


def render_pool_clear(health: dict) -> str:
    """The episode's end. No limits and no cause -- the episode is over, and
    the digest's `typical wait` is where the numbers live from here. A day with
    no concluded runs has no median; the clause goes rather than print the "?"
    wait_text owes a fixed-width field."""
    wait = (health.get("metrics") or {}).get("queue_wait_p50_s")
    typical = f", typical wait {wait_text(wait)}" if wait is not None else ""
    return f"✅ *Smoke gate: queue clear* — runs are starting on time again{typical}."


def render_fixture_scan(health: dict) -> str:
    block = fixture_state_of(health)
    when = clock(parse_iso(block.get("scanned_at")))
    total = block.get("projects") or 0
    if block.get("unknown"):
        reason = f" ({block['reason']})" if block.get("reason") else ""
        return (
            f"⚪ *Seeded-fleet scan can't see the fleet* — the {when} scan checked none of {total} pool projects{reason}."
            f" Fixture drift goes unseen until that is fixed; the bot's grant is in {FIXTURE_SCAN_DOC}."
        )
    return f"⚪ *Seeded-fleet scan sees the fleet again* — the {when} scan checked {block.get('checked', 0)} of {total} pool projects."


def fixture_digest_line(health: dict) -> str | None:
    """One line on the latest fleet scan for the digest; None before the
    scan has ever published."""
    block = fixture_state_of(health)
    if not block:
        return None
    when = clock(parse_iso(block.get("scanned_at")))
    total = block.get("projects") or 0
    checked = block.get("checked") or 0
    if block.get("stale"):
        return f"🧭 *Seeded fleet:* the last scan ({when}) is stale; someone check the scan job."
    if block.get("unknown"):
        reason = f" ({block['reason']})" if block.get("reason") else ""
        return f"🧭 *Seeded fleet:* the {when} scan could check none of {total} pool projects{reason}."
    drifted = block.get("drifted") or {}
    if drifted:
        roles = sorted({role for roles in drifted.values() for role in roles})
        return (
            f"🧭 *Seeded fleet:* {len(drifted)} of {checked} checked pool projects drifted at {when}"
            f" ({', '.join(roles)}); a red on a case that depends on {plural(len(roles), 'it', 'them')} there is the fixture, not the code."
        )
    unchecked = f", {total - checked} not checked" if total > checked else ""
    return f"🧭 *Seeded fleet:* {checked} of {total} pool projects checked at {when}, every fixture in its designed state{unchecked}."


def short_cause(prev: dict) -> str:
    condition = prev.get("condition")
    if condition == "shared_break":
        return f"{describe_cases(prev.get('failing_cases'))} were failing"
    if condition == "storm":
        return "quota storm"
    if condition == "setup_deaths":
        return "setup failures"
    if condition == CONDITION_LOST_PODS:
        return "the build cluster lost nodes"
    if condition == CONDITION_FIXTURE_DRIFT:
        return "seeded fixtures had drifted"
    if condition == CONDITION_DELEGATION_CEILING:
        return "workers were not finishing"
    if condition == CONDITION_DEADLINE_KILL:
        return "runs were being killed at the deadline"
    return prev.get("cause") or "unknown cause"


def render_recovery(health: dict, prev: dict, now: datetime) -> str:
    since = parse_iso(prev.get("since"))
    lasted = duration_text(now - since) if since else "a while"
    parts = [short_cause(prev)]
    issues = list(prev.get("tracking_issues") or [])
    for each in episode_issues(prev):
        tag = issue_tag(each)
        if tag not in issues:
            issues.append(tag)
    if issues:
        parts.append(", ".join(issues))
    # No "retests running" clause: this job queues none.
    lines = [
        f"🟢 *Smoke gate: healthy again* — fixed after {lasted} ({', '.join(parts)}).",
        # The closed incident: the cases and start the space was told, and
        # now as its end.
        dashboard_link(DASHBOARD_VIEW_GATE, prev.get("failing_cases") or [], since, now),
    ]
    return "\n".join(lines)


def pool_digest_line(pool: dict) -> str:
    """One line under the digest while the episode lasts. Cause-free: a line
    under a summary cannot branch four ways, and the alert already named it."""
    verdict = pool.get("verdict")
    if verdict == POOL_STALE:
        measured = parse_iso(pool.get("measured_at"))
        since = f" since {clock(measured)}" if measured else ""
        return f"⚪ No pool numbers{since} — {POOL_PRESSURE_JOB} has stopped reporting."
    if verdict == POOL_UNMEASURED:
        return "⚪ Queue wait unknown — the hourly pool check couldn't read how long recent runs waited."
    # The verdict lasts a week, so most mornings of an episode find the queue
    # already drained. Saying "is backed up" then sends a reader to look for a
    # jam that ended on Monday. Three tenses, one per thing known: it is, it
    # was and has cleared, it was and the queue went unread.
    live = pool.get("waiting_now")
    headline = "⏳ Queue backed up" if live else "⏳ Queue was backed up"
    # False is "no run has waited past the limit", not "the queue is empty":
    # a busy weekday has runs queued under it all day, and saying nothing is
    # waiting would be wrong on most mornings of most episodes.
    cleared = "" if live is not False else " No backlog right now."
    span = pool_span(pool)
    if not span:
        waiting = pool.get("over_threshold") or 0
        return (
            f"{headline} — {waiting} {plural(waiting, 'run')} waiting"
            f" past the {minutes_text(pool.get('threshold_p95_s'))} min p95 limit.{cleared}"
        )
    # Both figures, as pool_numbers does: the stretch breaches on p50 or p95, so
    # the median on its own can be a passing number standing in as the reason.
    return (
        f"{headline} — {span}:"
        f" median wait {wait_text(pool.get('p50_s'))} against a {minutes_text(pool.get('threshold_p50_s'))} min limit;"
        f" p95 {wait_text(pool.get('p95_s'))} against {minutes_text(pool.get('threshold_p95_s'))}.{cleared}"
    )


def render_digest(health: dict, now: datetime, data: dict | None = None) -> str:
    """The 24h numbers, the stale note while a stall lasts, and -- when
    data.json was given -- one line on last night's nightly run with a link
    to its report: the counts, what is newly failing against the night
    before and the wall clock, or that the night was truncated or missing."""
    metrics = health.get("metrics") or {}
    p50 = metrics.get("wall_clock_p50_s")
    typical = f"{int(p50 // 60)} min" if p50 is not None else "n/a"
    # How long a run waited before it started, every morning and not only
    # during an alert: the wait is normally seconds, and a number nobody sees
    # on an ordinary day is a number nobody can read on a bad one.
    wait = metrics.get("queue_wait_p50_s")
    headline = (
        f"📊 *Smoke gate, last {metrics.get('window_hours', DEFAULT_WINDOW_HOURS)}h:*"
        f" {metrics.get('full_runs', 0)} runs · {metrics.get('green_runs', 0)} green"
        f" · {metrics.get('pr_caused_reds', 0)} PR-caused red · {metrics.get('infra_reds', 0)} infra"
        f" · typical run {typical} · typical wait {wait_text(wait) if wait is not None else 'n/a'}"
    )
    lines = [headline]
    if health.get("stale"):
        # The window is measured from the data's horizon, so during a stall
        # these are the same numbers every morning; say so every morning.
        lines.append(f"⚪ No fresh data since {clock(parse_iso(health.get('generated_at')))} — these numbers stop there. Someone check the refresh job.")
    if health.get("slow"):
        lines.append(f"🐢 Slow since {clock(parse_iso(health['slow'].get('since')))}: {slow_text(health['slow'])}.")
    ceiling = metrics.get("ceiling_reps") or 0
    if ceiling:
        # Apart from the headline's infra count on purpose: these repetitions
        # were neither lost to 429s nor graded (#1874). Only on a day that
        # had one; a zero line every morning would be read past.
        lines.append(
            f"⏳ {ceiling} repetitions ended at the delegation ceiling with the worker still running;"
            " not counted as infra or against any case."
        )
    if health.get("pool"):
        lines.append(pool_digest_line(health["pool"]))
    if data is not None:
        lines.append(nightly.digest_line(data, now, clock=lambda value: clock(value, weekday=True)))
        lines.append(NIGHTLY_URL)
    fleet = fixture_digest_line(health)
    if fleet:
        lines.append(fleet)
    lines.append(dashboard_link(DASHBOARD_VIEW_AGENT, health.get("failing_cases") or [], parse_iso(health.get("since"))))
    return "\n".join(lines)


def render(kind: str, health: dict, prev: dict | None, now: datetime, issue: dict | None = None, data: dict | None = None) -> str:
    if kind == KIND_RECOVERY:
        return render_recovery(health, prev or {}, now)
    if kind == KIND_DIGEST:
        return render_digest(health, now, data)
    if kind == KIND_STALE:
        return render_stale(health)
    if kind == KIND_FIXTURE_SCAN:
        return render_fixture_scan(health)
    if kind == KIND_SLOW:
        return render_slow(health)
    if kind == KIND_POOL:
        return render_pool(health)
    if kind == KIND_POOL_CLEAR:
        return render_pool_clear(health)
    return render_change(health, prev, issue)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


class Sender:
    """One configured destination. `describe()` never includes a secret."""

    def __init__(self, space: str = "", token: str = "", webhook: str = "", opener=urllib.request.urlopen):
        self.space = space.strip()
        if self.space and not self.space.startswith(SPACE_PREFIX):
            self.space = SPACE_PREFIX + self.space
        self.token = token.strip()
        self.webhook = webhook.strip()
        self.opener = opener

    @classmethod
    def from_env(cls, environ=os.environ, opener=urllib.request.urlopen) -> Sender:
        return cls(
            space=environ.get(SPACE_ENV, ""),
            token=environ.get(TOKEN_ENV, ""),
            webhook=environ.get(WEBHOOK_ENV, ""),
            opener=opener,
        )

    @property
    def configured(self) -> bool:
        return bool(self.space and self.token) or bool(self.webhook)

    def describe(self) -> str:
        if self.space and self.token:
            return "chat api"
        if self.space:
            return f"chat api (space set, {TOKEN_ENV} missing)"
        return "webhook" if self.webhook else "unconfigured"

    def request(self, text: str) -> urllib.request.Request:
        body = json.dumps({"text": text}).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=UTF-8", "User-Agent": USER_AGENT}
        if self.space and self.token:
            url = f"{CHAT_API_ROOT}/{CHAT_MESSAGES_PATH.format(space=self.space)}"
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            url = self.webhook
        return urllib.request.Request(url, data=body, headers=headers, method="POST")

    def send(self, text: str) -> bool:
        """POST once. True on 2xx; a failure is logged without the URL."""
        try:
            with self.opener(self.request(text), timeout=REQUEST_TIMEOUT_S) as response:
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            log(f"post failed: HTTP {exc.code} from {self.describe()}")
            return False
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            log(f"post failed: {type(exc).__name__} ({type(reason).__name__}) from {self.describe()}")
            return False
        if not 200 <= status < 300:
            log(f"post failed: HTTP {status} from {self.describe()}")
            return False
        return True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run(
    health: dict,
    prev: dict | None,
    now: datetime,
    digest_hour: int,
    sender: Sender,
    dry_run: bool,
    tz=LOCAL_TZ,
    tracker: gate_issue.Tracker | None = None,
    data: dict | None = None,
) -> tuple[dict, list[tuple[str, str]], list[str]]:
    """Decide, render, send. Returns (new state, [(kind, text)], kinds that failed).

    `data`, when given, is the collector's data.json; the digest reads last
    night's nightly run from it.

    `tracker`, when given, files the tracking issue a new OUTAGE or a new
    lost-pods condition lacks before the message is rendered (so it can say
    "Tracking #NNN") and comments on it after a recovery goes out. An issue
    filed for one condition is never cited for another (issue_for): the
    outage's issue is not the cluster owner's, nor the reverse."""
    kinds = decide(health, prev, now, digest_hour, tz)
    before = prev or {}
    condition = health.get("condition")
    # Every issue this episode filed or adopted rides in `issues` until
    # GREEN, whatever condition the gate has moved on to, so the recovery can
    # comment on each of them; `issue` is the one for the current condition,
    # the only one the message cites and the only one that decides whether a
    # new one is needed.
    carried = episode_issues(before)
    if issue_tag(health.get("issue")) and health["issue"] not in carried:
        carried.append(health["issue"])
    issue = next((candidate for candidate in [health.get("issue"), *carried] if issue_for(candidate, condition)), None)
    wants_issue = (
        KIND_CHANGE in kinds
        and (health.get("state") == OUTAGE or condition in (CONDITION_LOST_PODS, CONDITION_FIXTURE_DRIFT))
        and not issue
        and not health.get("tracking_issues")
    )
    if tracker is not None and wants_issue:
        incident = health.get("incident") or {}
        since = parse_iso(health.get("since"))
        if condition == CONDITION_LOST_PODS:
            start, end = parse_iso(incident.get("window_start")), parse_iso(incident.get("window_end"))
            issue = tracker.ensure(health, now, clock(start or since, weekday=True), incident_link(health), clock_range(start, end) if start and end else clock(since, weekday=True))
        else:
            issue = tracker.ensure(health, now, clock(since, weekday=True), incident_link(health))
        if issue and issue not in carried:
            carried.append(issue)
    messages = [(kind, render(kind, health, prev, now, issue, data)) for kind in kinds]
    failed = []
    for kind, text in messages:
        if dry_run:
            log(f"--dry-run: would post [{kind}]\n{text}\n")
        elif not sender.send(text):
            failed.append(kind)
    sent = [kind for kind in kinds if kind not in failed]
    if tracker is not None and KIND_RECOVERY in sent:
        began = parse_iso(before.get("since"))
        for each in episode_issues(before):
            tracker.recovered(each, duration_text(now - began) if began else "a while")

    # The state file records what the space was last TOLD, kind by kind,
    # so the next tick asks its questions -- did the state change, did a new
    # case join, did staleness flip -- against what the readers have. A sent
    # change or recovery advances the state, condition, cause and case list;
    # a sent stale notice advances the stale bit; a sent digest advances the
    # digest date; a sent pool note or clear advances the pool verdict and the
    # breached bit, and only on a tick that read the artifact. The pool
    # episode's start rides beside them on any tick that read one, sent or
    # not: health.py reads it back, and it is a clock, not a message.
    # Nothing else moves.
    # A kind that failed, or was not due,
    # leaves its part where it was, so the next tick re-asks exactly that
    # question: a change that failed beside a stale notice that succeeded is
    # posted next tick, and a stale flip posted mid-OUTAGE does not swallow a
    # case that joined inside OUTAGE_REPOST_INTERVAL. The slow bit follows
    # the note down silently (a cleared note is not posted) and up only once
    # the note has gone out, so a failed one is retried. The tracking issues
    # ride along while the recorded state is not GREEN -- `issue` for the
    # current condition, `issues` for every one this episode filed or
    # adopted -- and are dropped by the recovery; health.py reads them back
    # from here (`--posted-state`) into the next health.json.
    told_state = KIND_CHANGE in sent or KIND_RECOVERY in sent
    told_stale = KIND_STALE in sent
    told_fixture = KIND_FIXTURE_SCAN in sent
    told_slow = KIND_SLOW in sent or KIND_SLOW not in kinds
    # The pool note records its verdict rather than a bit, because a verdict
    # change inside one episode is its own message (see decide). The clear is
    # held to the same bar: dropping the verdict on a send that failed would
    # lose the only "it is over" the space ever gets.
    # A withheld breach is not one of the "nothing was due" cases: see
    # pool_advisable.
    withheld = bool(health.get("pool")) and not pool_advisable(health["pool"], bool(before.get("pool_drained")))
    told_pool = (
        (KIND_POOL in sent or KIND_POOL not in kinds)
        and (KIND_POOL_CLEAR in sent or KIND_POOL_CLEAR not in kinds)
        and not withheld
    )
    # `pool_verdict` cannot answer "did this episode breach": a breach that goes
    # ⚪ overwrites it, and the ✅ is then owed to nobody. This bit outlives the
    # ⚪ and only the clear drops it. Set on the send, because a breach decide()
    # withheld for want of a live queue was never announced, and the ✅ would
    # then end an episode the space never heard begin.
    pool_breached = bool(before.get("pool_breached"))
    if KIND_POOL in sent:
        pool_breached = pool_breached or (health.get("pool") or {}).get("verdict") == POOL_BREACH
    if KIND_POOL_CLEAR in sent:
        pool_breached = False
    # The causes this backlog has named. Appended only on a send, so a failed
    # one is retried; emptied by a reading that shows the queue drained, note
    # gone or verdict still standing on a bad day up to a week old. Kept per
    # backlog rather than per episode because the pool refills: a capacity jam
    # announced on Monday would otherwise be silent every afternoon until the
    # window rolls off it, and those are the afternoons people /retest into.
    pool_causes = list(before.get("pool_causes") or [])
    if KIND_POOL in sent:
        pool_causes += [key for key in pool_told_keys(health.get("pool") or {}) if key not in pool_causes]
    if pool_was_read(health) and (not health.get("pool") or withheld):
        pool_causes = []
    # `pool_drained` is what stops an unread queue re-opening the ⏳ every time
    # Deck fails: the memory above is empty, so the cause looks new. Only a
    # withheld breach sets it -- a reading with no note at all is the episode
    # ending, and the next one is entitled to open on an unread queue. Dropped
    # by that, and by a reading with a backlog in it, which is the jam the
    # empty memory is there to announce.
    pool_drained = bool(before.get("pool_drained"))
    if withheld:
        pool_drained = True
    elif pool_was_read(health) and (not health.get("pool") or health["pool"].get("waiting_now")):
        pool_drained = False
    if prev is None:
        # First tick: whatever was not due is recorded as told, so a green,
        # fresh start is not announced later as a change.
        told_state = told_state or (KIND_CHANGE not in kinds and KIND_RECOVERY not in kinds)
        told_stale = told_stale or KIND_STALE not in kinds
        told_fixture = told_fixture or KIND_FIXTURE_SCAN not in kinds
    source = health if told_state else before
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "state": source.get("state"),
        "condition": source.get("condition"),
        "cause": source.get("cause"),
        "failing_cases": source.get("failing_cases") or [],
        "tracking_issues": source.get("tracking_issues") or [],
        "since": source.get("since"),
        "issue": issue if source.get("state") not in (None, GREEN) else None,
        "issues": carried if source.get("state") not in (None, GREEN) else [],
        "stale": bool(health.get("stale")) if told_stale else bool(before.get("stale")),
        "fixture_unknown": fixture_unknown(health) if told_fixture else bool(before.get("fixture_unknown")),
        "slow": bool(health.get("slow")) if told_slow else bool(before.get("slow")),
        # No artifact is not a reading: clearing the verdict on a blind tick
        # re-posts the same breach once the fetch recovers.
        "pool_verdict": (
            (health.get("pool") or {}).get("verdict")
            if told_pool and pool_was_read(health)
            else before.get("pool_verdict")
        ),
        "pool_breached": pool_breached,
        "pool_causes": pool_causes,
        "pool_drained": pool_drained,
        "posted_at": before.get("posted_at"),
        "last_digest_date": before.get("last_digest_date"),
        "updated_at": now.isoformat(timespec="seconds"),
    }
    if KIND_CHANGE in sent or KIND_RECOVERY in sent:
        state["posted_at"] = now.isoformat(timespec="seconds")
    if KIND_DIGEST in sent:
        state["last_digest_date"] = local_date(now, tz)
    return state, messages, failed


def parse_tz(name: str):
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"unknown time zone {name!r}") from exc


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--health", type=pathlib.Path, required=True, help="the health.json health.py wrote")
    parser.add_argument("--state", required=True, help="last-posted state: local path or gs:// object (this script's only write)")
    parser.add_argument("--data", type=pathlib.Path, default=None, help="the collector's data.json; the digest then carries one line on last night's nightly run (unreadable: a warning and the line says so)")
    parser.add_argument("--digest-hour", type=int, default=DEFAULT_DIGEST_HOUR, help="hour of the daily digest, in --digest-tz")
    parser.add_argument("--digest-tz", type=parse_tz, default=LOCAL_TZ, help=f"IANA zone the digest hour and day are read in (default {DEFAULT_TZ}); times in messages stay {DEFAULT_TZ} ({TZ_LABEL}) regardless")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO, help="owner/repo the tracking issue is filed in")
    parser.add_argument("--now", help="evaluate as of this ISO 8601 time (default: now)")
    parser.add_argument("--dry-run", action="store_true", help="print the messages (and the issue) instead of posting; still updates --state")
    return parser.parse_args(argv)


def main(argv=None, environ=os.environ, opener=urllib.request.urlopen, runner=subprocess.run, gh_runner=None) -> int:
    args = parse_args(argv)
    try:
        health = json.loads(args.health.read_text())
    except (OSError, ValueError) as exc:
        log(f"ERROR: {args.health}: {exc}")
        return 1
    now = parse_iso(args.now) or datetime.now(UTC)
    data = None
    if args.data is not None:
        try:
            data = json.loads(args.data.read_text())
        except (OSError, ValueError) as exc:
            # The digest still goes out; its nightly line says the data was
            # unreadable rather than inventing a quiet night.
            log(f"warning: {args.data}: {exc}; the digest's nightly line will say so")
            data = {}
        if not isinstance(data, dict):
            data = {}
    sender = Sender.from_env(environ, opener)
    if not sender.configured and not args.dry_run:
        log(NOT_CONFIGURED)
        return 0
    # The issue is filed with the workflow's token; without one (a laptop
    # dry run, a muted job) nothing is filed and the message says so.
    tracker = None
    if environ.get(ghcli.TOKEN_ENV) or args.dry_run:
        tracker = gate_issue.Tracker(ghcli.Gh(args.repo, gh_runner or runner, dry_run=args.dry_run))

    prev = read_state(args.state, runner)
    state, messages, failed = run(health, prev, now, args.digest_hour, sender, args.dry_run, args.digest_tz, tracker, data)
    if prev is None and failed and len(failed) == len(messages):
        # Nothing has ever been told and nothing got through: there is no
        # state worth recording, and the next tick starts from scratch.
        log("not recording state: every post failed on the first tick")
        return 1
    # Always written past this point, even after a partial failure: the
    # parts that were told are recorded and the failed kind is re-asked next
    # tick from the same file.
    write_state(args.state, state, runner)
    sent = ", ".join(kind for kind, _ in messages if kind not in failed) or "nothing"
    log(f"{health.get('state')} via {sender.describe()}: posted {sent}")
    if failed:
        log(f"failed to post: {', '.join(failed)}; the next tick retries")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
