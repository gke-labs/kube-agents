#!/usr/bin/env python3
"""Render the eval dashboard from the collector's data.json.

Usage::

    python3 scripts/eval_dashboard/render.py --data data.json --out-dir out/ \\
        [--health health.json] [--health-history health-history.jsonl] \\
        [--public-url [BASE]]

writes five pages and two data files into ``out/``:

* ``index.html`` -- **the Brief**: what state the smoke gate is in, why the
  bot thinks so, what the agent saw, what changed right before, what is
  being done, the runs in the window and the last release-candidate eval
  runs. Scoped by ``#since=<ISO>&until=<ISO>&cases=a,b&view=gate`` to a
  past incident; ``#view=agent`` shows the last 24 hours in numbers
  (SCHEMA.md, "URL contract"; the older ``?cases=…#gate`` form is still
  read).
* ``run.html#build=<prow build id>`` -- **the PR view**: one run, its
  failed cases each tagged as the gate's or the pull request's
  (``classify.py``), and what to do.
* ``grid.html`` -- **the Grid**: every case by every presubmit run in a
  window, with the merges to main and the incidents marked between the
  columns; a cell opens the run's detail for that case. Scoped by the same
  ``#since=&until=&cases=`` the Brief carries.
* ``cases.html`` -- **the Cases page**: one row per case with its last
  ``STRIP_RUNS`` presubmit outcomes, its 7- and 30-day pass rates per tier,
  its roster status and its last failure; ``#<case>`` lands on the row.
* ``nightly.html[#build=<prow build id>]`` -- **the Nightly report**: last
  night's run of the nightly tier (or the night ``build`` names), its cases
  by domain with pass / partial / fail and the grader's reason, what is
  newly failing against the night before, the wall clock and whether the
  night was cut short (``nightly.py``).
* ``brief.json`` -- what the five pages render from: the per-run
  classification, the per-case record, the current health verdict, the
  incident history, the recent merges, the release-candidate runs and the
  nights on record.
  ``data.json`` is copied verbatim beside it.

Every page is rendered in the browser (``template/page.html.tmpl`` +
``template/pages.js``) from the brief.json document inlined into it as
``<script type="application/json" id="inline-brief">`` (the verdict it read
again as ``inline-health``), so a page needs no request beyond itself; the
60-second poll of the published ``brief.json`` and ``health.json`` is a
best-effort refresh on top, and a host that answers an XHR with a login
redirect (storage.cloud.google.com does) just leaves the inlined data on
screen. ``--public-url`` adds ``<base href>`` so every relative link
resolves to the published site wherever the browser landed after that
redirect. Every time the pages show is America/Toronto, formatted there.
The optional inputs are ``health.json`` (the CI health adjudicator's
verdict, published beside data.json; nothing here writes it),
``health-history.jsonl`` (one health.json document per line plus a ``tick``
stamp), ``case-notes.yaml`` (``--notes``: a one-line note and issue links
per case) and ``events.yaml`` (``--events``: the human-annotated catch
counts). Without any of them the pages show what the runs alone support,
never an error.

Two rules shape everything here:

* **INFRA is not failure.** A rep (or task) whose result is ``infra`` is
  excluded from every pass-fraction denominator, matching the suite's policy
  that infrastructure failures never count against a PR.
* **Run-level events are charged to the run, not the cases.** A run where at
  least ``RUN_EVENT_FAIL_FRACTION`` of its graded tasks failed (a broken PR,
  an endpoint outage) keeps its cells on the Grid and its bars on a case's
  strip, but its failures are excluded from the per-case pass rates.

The reader contract is schema_version 1 of the collector's data.json.
Optional fields may be absent and unknown additive fields are ignored, so
this renderer and the collector can ship independently. In particular
``tasks[].reps``, ``runs[].tier``, ``runs[].pr_merged``, ``pending_builds``
and ``releases[]`` are optional additive fields (SCHEMA.md); without them
every task falls back to its single ``result``, every run is the
presubmit's, and the Grid has no "still running" columns and the Brief no
release table.

Only stdlib + PyYAML (already in requirements-test.txt) -- no build step.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import pathlib
import re
import shutil
import subprocess
import sys

import yaml

try:
    from . import classify, nightly, post_health, tiers
except ImportError:  # run as a script: python3 scripts/eval_dashboard/render.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import classify
    import nightly
    import post_health
    import tiers

HERE = pathlib.Path(__file__).resolve().parent
PAGE_TEMPLATE = HERE / "template" / "page.html.tmpl"
PAGES_JS = HERE / "template" / "pages.js"
DEFAULT_NOTES = HERE / "case-notes.yaml"
DEFAULT_EVENTS = HERE / "events.yaml"

# --- the five pages and their data ----------------------------------------
BRIEF_PAGE = "index.html"
# The file post_health.run_link points at; one name for it.
RUN_PAGE = post_health.DASHBOARD_RUN_PAGE
GRID_PAGE = "grid.html"
CASES_PAGE = "cases.html"
NIGHTLY_PAGE = nightly.NIGHTLY_PAGE
BRIEF_JSON = "brief.json"
# Per page: the file and the <title>. The PR view has no tab of its own on
# the other pages; it appears only when opened.
PAGES = {
    "brief": {"file": BRIEF_PAGE, "title": "kube-agents · smoke gate brief"},
    "run": {"file": RUN_PAGE, "title": "kube-agents · smoke run"},
    "grid": {"file": GRID_PAGE, "title": "kube-agents · cases by run"},
    "cases": {"file": CASES_PAGE, "title": "kube-agents · how reliable is each test"},
    "nightly": {"file": NIGHTLY_PAGE, "title": "kube-agents · last night's run"},
}
# The object names the pages poll beside their own; the adjudicator job
# writes both (health-history.jsonl is appended one line per tick).
HEALTH_FILE = "health.json"
HEALTH_HISTORY_FILE = "health-history.jsonl"
# The ids of the <script type="application/json"> elements each page
# carries its data in; pages.js boots from them, so the
# page renders whole without a single fetch (module docstring).
INLINE_BRIEF_ID = "inline-brief"
INLINE_HEALTH_ID = "inline-health"
# Where the pages are published: the directory of the index.html URL the
# Chat messages and the gate comment (post_health.py's link builders)
# already link to, so `--public-url` with no value names the host they do
# rather than a second copy of it.
PUBLISHED_SITE = post_health.DASHBOARD_SITE
# The three states health.json can carry. The pages announce a state with
# a glyph and the word, never with colour alone.
HEALTH_STATES = ("GREEN", "DEGRADED", "OUTAGE")
# brief.json carries the runs started inside this many days of the data's
# generated_at -- the depth the collector keeps current (SCHEMA.md,
# --since-days 14) -- and run.html says so when a build id is not in it.
RUN_VIEW_DAYS = 14
# "What changed right before" lists merges to main from this far back; the
# page narrows it to the window between the last green run and the first
# red one. Read from the checkout's git log at render time; a shallow
# checkout (actions/checkout's default) has no usable history and the
# block is omitted rather than shown one commit deep.
MERGES_LOOKBACK_DAYS = 3
MERGES_MAX = 300
MERGE_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
GIT_TIMEOUT_S = 20
# A run finished this long after the last history tick still takes that
# tick's verdict; beyond it the current verdict is what the page has. A
# gap inside the history (the adjudicator down for a while) keeps the tick
# before the gap: the last thing it said is still what the space heard.
HEALTH_AT_TICK_SLACK_MS = 30 * 60 * 1000

# The three results a rep (or a task) can carry; anything else is treated as
# "not measured" rather than guessed at.
REP_RESULTS = ("pass", "fail", "infra")

# The Cases page's strip: a case's last N presubmit outcomes, newest last.
# 30 is about two weeks of PR traffic per case and fits one table cell.
STRIP_RUNS = 30
# A run where at least this fraction of its graded tasks failed is a
# run-level event: the run is broken (a red PR, an outage), so its failures
# are charged to the run and excluded from the per-case pass rates.
RUN_EVENT_FAIL_FRACTION = 0.8
DAY_MS = 24 * 3600 * 1000
# The Cases page's per-tier pass rates: rep-level pass / (pass + fail) over
# the runs started inside each of these windows before the reference time,
# run-level events excluded. Two tiers, two numbers, never pooled: the
# presubmit's is the gate's own history, the nightly's is the readable view
# of a case's record on main (what that record is for:
# docs/eval-gate-roster.md).
TIER_RATE_WINDOWS_DAYS = (7, 30)

# --- the roster: what blocks, what is held out, what was demoted when ------
# Admission is BOOTSTRAP_ADMITTED in hack/ci-eval-pr.sh (classify.py reads
# it). The demotion dates are prose: docs/eval-gate-roster.md's hold-out
# entries say "demoted YYYY-MM-DD" -- data.json carries no roster history,
# so that page is where the date lives, and this reads it the same way
# classify.py reads the script, degrading to "no date" when it cannot.
ROSTER_DOC = classify.REPO_ROOT / "docs" / "eval-gate-roster.md"
# One hold-out entry: a "- **case-name** —" bullet and its indented body,
# up to the next bullet or the next unindented line.
ROSTER_ENTRY_RE = re.compile(
    r"^- \*\*(?P<case>[A-Za-z0-9][A-Za-z0-9._-]*)\*\*(?P<body>.*?)(?=^- \*\*|^\S|\Z)",
    re.MULTILINE | re.DOTALL,
)
DEMOTED_RE = re.compile(r"\bdemoted (\d{4}-\d{2}-\d{2})")
# A case's roster status on the Cases page (the pill) and on the Grid (which
# rows are blocking). The words the pages print for each live in pages.js.
STATUS_BLOCKING = "blocking"  # active and in BOOTSTRAP_ADMITTED
STATUS_HELD_OUT = "held_out"  # active, never admitted (or no date on record)
STATUS_DEMOTED = "demoted"  # active, held out, with a demotion date
STATUS_NIGHTLY_ONLY = "nightly_only"  # in NIGHTLY_TASKS only
STATUS_RETIRED = "retired"  # in neither matrix on this checkout
# A case's state in one run, on the strip and in a Grid cell: every graded
# rep passed, some failed (the gate counts that as a pass), every graded rep
# failed, every rep was infra (excluded, not failed), or nothing measured.
STRIP_PASS = "pass"
STRIP_PARTIAL = "partial"
STRIP_FAIL = "fail"
STRIP_INFRA = "infra"
STRIP_NONE = "none"
# The domain of a case whose task.yaml is not on this checkout.
DOMAIN_UNKNOWN = "unknown"
# A pending build first seen longer ago than this is not still running: the
# presubmit's ceiling is 360 minutes (AGENTS.md), and a build past it with
# no finished.json is a pod that died without uploading, which the
# collector keeps on pending_builds for two days (SCHEMA.md,
# PENDING_RETRY_DAYS). The Grid shows it as running only inside this window.
PENDING_MAX_AGE_MS = 8 * 3600 * 1000

# --- release candidates (SCHEMA.md: releases[]) -----------------------------
# The Brief's release table: how many release-candidate eval runs it shows,
# newest first. RCs are cut per staging promotion, so ten rows is roughly a
# fortnight of them, and the whole list stays in data.json for anyone who
# wants further back.
RELEASES_MAX_ROWS = 10
# The only URL scheme a collected artifacts link may carry into an href. The
# value is read out of a build log, and a log line is the wrong place to be
# minting `javascript:`; anything else is dropped before it reaches the page.
RELEASE_URL_SCHEME = "https://"


def is_count(value) -> bool:
    """A non-negative whole number. Mirrors the template's
    ``Number.isInteger`` guard: bools and non-integral floats are data
    errors, rendered as "not reported" rather than interpolated raw."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and float(value).is_integer()
    )


def is_number(value) -> bool:
    """A finite real number. Mirrors the template's ``Number.isFinite``
    guard; bools are data errors, not zeroes and ones."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --------------------------------------------------------------------------
# data.json access (tolerant of absent optional fields)


def load_data(path: pathlib.Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {path} is not a JSON object")
    return data


def sorted_runs(data: dict) -> list[dict]:
    """Runs in chronological order; the collector's order is kept when any
    run lacks a ``started`` timestamp (ISO-8601 sorts lexicographically)."""
    runs = [r for r in data.get("runs") or [] if isinstance(r, dict)]
    if runs and all(isinstance(r.get("started"), str) for r in runs):
        runs.sort(key=lambda r: r["started"])
    return runs


def run_tasks(run: dict | None) -> list[dict]:
    if not run:
        return []
    return [t for t in run.get("tasks") or [] if isinstance(t, dict)]


def gate_runs(data: dict) -> list[dict]:
    """The presubmit's runs, chronological: what the Grid, the strips, the
    presubmit rates and the Brief are about (SCHEMA.md: runs[].tier; a run
    with no tier predates the field and is the presubmit)."""
    return tiers.presubmit_runs(sorted_runs(data))


def nightly_runs(data: dict) -> list[dict]:
    return tiers.nightly_runs(sorted_runs(data))


def parse_iso(value) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def iso_ms(value) -> float | None:
    """Epoch milliseconds, the unit the page gets from Date.parse."""
    parsed = parse_iso(value)
    return parsed.timestamp() * 1000 if parsed else None


# --------------------------------------------------------------------------
# reps, cell states, run-level events (the shared verdict vocabulary)


def task_reps(task: dict) -> list[dict]:
    """The counted repetitions of one task: ``tasks[].reps`` entries with a
    recognized result when the collector reported them, else one synthetic
    rep carrying the task's own single ``result``. Empty for a task that
    measured nothing either way."""
    reps = []
    raw = task.get("reps")
    if isinstance(raw, list):
        for rep in raw:
            if not isinstance(rep, dict):
                continue
            result = str(rep.get("result", "")).lower()
            if result in REP_RESULTS:
                reason = rep.get("reason")
                reps.append(
                    {"result": result, "reason": reason if isinstance(reason, str) else None}
                )
    if reps:
        return reps
    result = str(task.get("result", "")).lower()
    if result in REP_RESULTS:
        return [{"result": result, "reason": None}]
    return []


def rep_counts(reps: list[dict]) -> tuple[int, int, int]:
    passed = failed = infra = 0
    for rep in reps:
        if rep["result"] == "pass":
            passed += 1
        elif rep["result"] == "fail":
            failed += 1
        else:
            infra += 1
    return passed, failed, infra


def cell_state(task: dict) -> str:
    """One task's verdict in a run: 'pass' (every graded rep passed),
    'partial' (some passed, some failed), 'fail' (every graded rep failed),
    'infra' (every counted rep was infra -- excluded, not failed), or
    'none' (nothing measured)."""
    reps = task_reps(task)
    if not reps:
        return STRIP_NONE
    passed, failed, _ = rep_counts(reps)
    if passed and failed:
        return STRIP_PARTIAL
    if failed:
        return STRIP_FAIL
    if passed:
        return STRIP_PASS
    return STRIP_INFRA


def is_run_event(run: dict) -> bool:
    """True when at least RUN_EVENT_FAIL_FRACTION of the run's graded tasks
    (state pass/partial/fail; infra and unmeasured don't grade) failed
    outright. Such a run is broken as a whole, so its failures are charged
    to the run, not the cases."""
    graded = [s for s in (cell_state(t) for t in run_tasks(run)) if s in (STRIP_PASS, STRIP_PARTIAL, STRIP_FAIL)]
    if not graded:
        return False
    return graded.count(STRIP_FAIL) / len(graded) >= RUN_EVENT_FAIL_FRACTION


# --------------------------------------------------------------------------
# windows and rates


def reference_ms(data: dict) -> float | None:
    """The time axis anchor: generated_at, else the newest run's start.
    Deliberately not the wall clock, so two renders of the same data.json
    agree."""
    anchor = iso_ms(data.get("generated_at"))
    if anchor is not None:
        return anchor
    for run in reversed(sorted_runs(data)):
        anchor = iso_ms(run.get("started"))
        if anchor is not None:
            return anchor
    return None


def tier_pass_rates(data: dict, tier: str, days: int) -> dict[str, tuple[int, int]]:
    """{case: (passed reps, failed reps)} over the runs of `tier` started
    inside the last `days` before the reference time, run-level events
    excluded and infra reps uncounted. A data.json with no time anchor
    counts every run of the tier; a run without a start time is skipped
    when there is one."""
    anchor = reference_ms(data)
    runs = gate_runs(data) if tier == tiers.TIER_PRESUBMIT else nightly_runs(data)
    tally: dict[str, list[int]] = {}
    for run in runs:
        started = iso_ms(run.get("started"))
        if anchor is not None and (
            started is None or started <= anchor - days * DAY_MS or started > anchor
        ):
            continue
        if is_run_event(run):
            continue
        for task in run_tasks(run):
            p, f, _ = rep_counts(task_reps(task))
            bucket = tally.setdefault(str(task.get("name")), [0, 0])
            bucket[0] += p
            bucket[1] += f
    return {name: (p, f) for name, (p, f) in tally.items()}


# --------------------------------------------------------------------------
# case-notes.yaml and events.yaml (optional flavor; never an error)


def load_notes(path: pathlib.Path | None) -> dict[str, dict]:
    """``{case: {"note": str|None, "issues": [str, ...]}}``. Absent file,
    empty file, or malformed entry all degrade to "no note"."""
    if path is None or not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("notes")
    if not isinstance(entries, dict):
        return {}
    notes = {}
    for name, entry in entries.items():
        if isinstance(entry, str):
            entry = {"note": entry}
        if not isinstance(entry, dict):
            continue
        note = entry.get("note")
        note = str(note) if isinstance(note, (str, int, float)) and note != "" else None
        raw_issues = entry.get("issues")
        issues = [str(i) for i in raw_issues] if isinstance(raw_issues, list) else []
        if note or issues:
            notes[str(name)] = {"note": note, "issues": issues}
    return notes


def load_events(path: pathlib.Path | None) -> dict:
    """``{"catches": {"product_bugs", "prs_blocked", "ledger"} | None}``:
    the human-judgment counts the Cases page's footer quotes. Absent or
    malformed file degrades to no counts."""
    out = {"catches": None}
    if path is None or not path.exists():
        return out
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return out
    if not isinstance(raw, dict):
        return out
    catches = raw.get("catches")
    if isinstance(catches, dict):
        out["catches"] = {
            "product_bugs": catches["product_bugs"] if is_count(catches.get("product_bugs")) else None,
            "prs_blocked": catches["prs_blocked"] if is_count(catches.get("prs_blocked")) else None,
            "ledger": str(catches["ledger"]) if catches.get("ledger") is not None else None,
        }
    return out


# --------------------------------------------------------------------------
# health.json and health-history.jsonl (optional; the verdict and its past)


def normalize_health(raw) -> dict | None:
    """The fields the pages read from a health.json document, each
    defaulted: absent, unparseable, a non-object, or a state outside
    HEALTH_STATES all read as "no verdict" (None). Mirrors the template's
    ``normalizeHealth``."""
    if not isinstance(raw, dict):
        return None
    state = str(raw.get("state") or "").upper()
    if state not in HEALTH_STATES:
        return None

    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    def strings(key: str) -> list[str]:
        value = raw.get(key)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    incident = raw.get("incident") if isinstance(raw.get("incident"), dict) else None
    return {
        "state": state,
        "condition": raw["condition"] if isinstance(raw.get("condition"), str) else None,
        "since": raw["since"] if iso_ms(raw.get("since")) is not None else None,
        "cause": text("cause"),
        "advice": text("advice"),
        "failing_cases": strings("failing_cases"),
        "tracking_issues": strings("tracking_issues"),
        "recovering": raw.get("recovering") is True,
        "stale": raw.get("stale") is True,
        "generated_at": raw["generated_at"] if iso_ms(raw.get("generated_at")) is not None else None,
        "incident": {
            "prs": [p for p in incident.get("prs") or [] if isinstance(p, int)] if incident else [],
            "runs": incident.get("runs") if incident and is_count(incident.get("runs")) else None,
            "window_start": incident.get("window_start") if incident and iso_ms(incident.get("window_start")) is not None else None,
            "window_end": incident.get("window_end") if incident and iso_ms(incident.get("window_end")) is not None else None,
        } if incident else None,
        "tick": raw["tick"] if iso_ms(raw.get("tick")) is not None else None,
    }


def load_health(path: pathlib.Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return normalize_health(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def load_health_history(path: pathlib.Path | None) -> list[dict] | None:
    """health-history.jsonl: one JSON object per line, each the health.json
    published at that tick plus ``"tick": "<ISO UTC>"``. Returns the
    normalized ticks in tick order (a line without a parseable tick uses
    its generated_at), or None when the file is absent or unreadable --
    the pages then show the current state only. A malformed line is
    skipped, never fatal."""
    if path is None or not path.exists():
        return None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    ticks = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        entry = normalize_health(raw)
        if entry is None:
            continue
        if entry["tick"] is None:
            entry["tick"] = entry["generated_at"]
        if entry["tick"] is None:
            continue
        ticks.append(entry)
    ticks.sort(key=lambda t: iso_ms(t["tick"]))
    return ticks


def history_incidents(ticks: list[dict]) -> list[dict]:
    """The episodes in a tick list: a run of non-GREEN ticks is one
    incident, from its first tick's ``since`` to the first GREEN tick
    after it (``until`` is None while it is still open). State is the
    worst seen, cases the union, condition the last one reported."""
    incidents: list[dict] = []
    open_incident = None
    rank = {state: i for i, state in enumerate(HEALTH_STATES)}
    for tick in ticks:
        if tick["state"] == "GREEN":
            if open_incident is not None:
                open_incident["until"] = tick["tick"]
                open_incident = None
            continue
        if open_incident is None:
            open_incident = {
                "since": tick["since"] or tick["tick"],
                "until": None,
                "state": tick["state"],
                "condition": tick["condition"],
                "failing_cases": [],
                "tracking_issues": [],
                "cause": tick["cause"],
                "advice": tick["advice"],
            }
            incidents.append(open_incident)
        if rank[tick["state"]] > rank[open_incident["state"]]:
            open_incident["state"] = tick["state"]
        if tick["condition"]:
            open_incident["condition"] = tick["condition"]
        if tick["cause"]:
            open_incident["cause"] = tick["cause"]
        if tick["advice"]:
            open_incident["advice"] = tick["advice"]
        for case in tick["failing_cases"]:
            if case not in open_incident["failing_cases"]:
                open_incident["failing_cases"].append(case)
        for issue in tick["tracking_issues"]:
            if issue not in open_incident["tracking_issues"]:
                open_incident["tracking_issues"].append(issue)
    return incidents


def health_at(ticks: list[dict] | None, when_ms: float | None, incidents: list[dict] | None = None) -> dict | None:
    """The verdict in force at ``when_ms``: the last tick at or before it
    (a run finished within HEALTH_AT_TICK_SLACK_MS after the last tick
    still gets that tick). None without history or before its first
    tick. Carries the incident's ``until`` when the tick sits inside a
    closed episode, so the PR view can link the past brief."""
    if not ticks or when_ms is None:
        return None
    chosen = None
    for tick in ticks:
        tick_ms = iso_ms(tick["tick"])
        if tick_ms is not None and tick_ms <= when_ms:
            chosen = tick
        else:
            break
    if chosen is None:
        return None
    last_ms = iso_ms(ticks[-1]["tick"])
    if chosen is ticks[-1] and last_ms is not None and when_ms - last_ms > HEALTH_AT_TICK_SLACK_MS:
        return None
    until = None
    for incident in incidents or []:
        since_ms, until_ms = iso_ms(incident["since"]), iso_ms(incident["until"])
        if since_ms is not None and until_ms is not None and since_ms <= when_ms <= until_ms:
            until = incident["until"]
    return {
        "state": chosen["state"],
        "condition": chosen["condition"],
        "since": chosen["since"],
        "until": until,
        "failing_cases": chosen["failing_cases"],
        "recovering": chosen["recovering"],
        "cause": chosen["cause"],
        "tick": chosen["tick"],
    }


# --------------------------------------------------------------------------
# merges to main (optional; from the checkout's git log)


def recent_merges(repo_root: pathlib.Path, now_ms: float | None, runner=subprocess.run) -> list[dict] | None:
    """Commits on the checkout's first-parent line from the last
    MERGES_LOOKBACK_DAYS before ``now_ms``: ``[{sha, at, title, pr}]``,
    newest first. None when git is unavailable, the checkout is shallow
    (its log would be one commit deep and read as "one merge"), or the
    command fails -- the pages omit the block and the markers rather than
    guess."""
    if now_ms is None:
        return None
    try:
        shallow = runner(
            ["git", "-C", str(repo_root), "rev-parse", "--is-shallow-repository"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )
        if shallow.returncode != 0 or shallow.stdout.strip() != "false":
            return None
        since = ms_to_utc(now_ms - MERGES_LOOKBACK_DAYS * DAY_MS)
        log = runner(
            [
                "git", "-C", str(repo_root), "log", "--first-parent", f"--max-count={MERGES_MAX}",
                f"--since={since:%Y-%m-%dT%H:%M:%S}+00:00", "--format=%H%x1f%cI%x1f%s", "HEAD",
            ],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )
        if log.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    merges = []
    for line in log.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3 or iso_ms(parts[1]) is None:
            continue
        match = MERGE_PR_RE.search(parts[2])
        merges.append({
            "sha": parts[0],
            "at": parts[1],
            "title": MERGE_PR_RE.sub("", parts[2]).strip(),
            "pr": int(match.group(1)) if match else None,
        })
    return merges


def ms_to_utc(ms: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)


# --------------------------------------------------------------------------
# the roster: status and demotion dates


def demotion_dates(doc: pathlib.Path = ROSTER_DOC) -> dict[str, str]:
    """``{case: "YYYY-MM-DD"}`` for every hold-out entry in the roster page
    whose text says when the case was demoted. An unreadable page, or an
    entry without a date, is simply no date -- the pill then says "held
    out" without one."""
    try:
        text = doc.read_text()
    except OSError:
        return {}
    dates = {}
    for match in ROSTER_ENTRY_RE.finditer(text):
        when = DEMOTED_RE.search(match.group("body"))
        if when:
            dates[match.group("case")] = when.group(1)
    return dates


def case_status(case: dict, admitted: frozenset | None, demoted: dict[str, str]) -> tuple[str, str | None]:
    """(status, demoted_on). Blocking is active *and* on the roster; an
    active case off the roster is held out, "demoted" when the roster page
    dates it; a case only the nightly runs is nightly-only; a case in
    neither matrix on this checkout is retired. An unreadable roster
    (``admitted`` None) reads every active case as blocking, which
    over-reports rather than hides, as classify.py does."""
    name = str(case.get("name"))
    if case.get("active") is True:
        if admitted is None or name in admitted:
            return STATUS_BLOCKING, None
        if name in demoted:
            return STATUS_DEMOTED, demoted[name]
        return STATUS_HELD_OUT, None
    if case.get("nightly_active") is True:
        return STATUS_NIGHTLY_ONLY, None
    return STATUS_RETIRED, None


# --------------------------------------------------------------------------
# brief.json: what the pages render from


def compact_run(run: dict, verdict: dict, at: dict | None) -> dict:
    """One run as the pages need it: identity, timing, and classify.py's
    result (SCHEMA.md, "brief.json")."""
    return {
        "build": verdict["build"],
        "pr": run.get("pr"),
        "head_sha": run.get("head_sha") if isinstance(run.get("head_sha"), str) else None,
        "project": run.get("project") if isinstance(run.get("project"), str) else None,
        "started": run.get("started") if iso_ms(run.get("started")) is not None else None,
        "finished": run.get("finished") if iso_ms(run.get("finished")) is not None else None,
        "duration_s": run.get("duration_s") if isinstance(run.get("duration_s"), (int, float)) else None,
        "result": str(run.get("result") or "").upper() or None,
        "verdict": verdict["verdict"],
        "headline": verdict["headline"],
        "lede": verdict.get("lede", ""),
        "matches_incident": verdict["matches_incident"],
        "setup_death": verdict.get("setup_death", False),
        "storm_reps": verdict.get("storm_reps", 0),
        "do": verdict.get("do", ""),
        "cases": verdict["cases"],
        "health_at": at,
    }


def appearances_by_case(runs: list[dict]) -> dict[str, list[tuple[dict, dict]]]:
    """{case: [(run, task), ...]} in run order, one entry per task row a
    run recorded for the case (an unmeasured task row is skipped)."""
    out: dict[str, list[tuple[dict, dict]]] = {}
    for run in runs:
        for task in run_tasks(run):
            if cell_state(task) != STRIP_NONE:
                out.setdefault(str(task.get("name")), []).append((run, task))
    return out


def run_when(run: dict) -> str | None:
    """The stamp a case's history is placed by: the run's finish, else its
    start (the Brief lists runs the same way)."""
    for key in ("finished", "started"):
        if iso_ms(run.get(key)) is not None:
            return run[key]
    return None


def case_strip(appearances: list[tuple[dict, dict]]) -> list[dict]:
    """The case's last STRIP_RUNS presubmit outcomes, oldest first:
    ``{build, pr, at, state, event}`` -- ``event`` marks a run-level event,
    whose failure the rates do not count but the strip still shows."""
    return [
        {
            "build": str(run.get("build_id") or ""),
            "pr": run.get("pr") if isinstance(run.get("pr"), int) else None,
            "at": run_when(run),
            "state": cell_state(task),
            "event": is_run_event(run),
        }
        for run, task in appearances[-STRIP_RUNS:]
    ]


def last_failure(name: str, gate: list[tuple[dict, dict]], nightly: list[tuple[dict, dict]], classified: dict[str, dict]) -> dict | None:
    """The case's newest non-pass appearance: the presubmit's first, the
    nightly's only when the presubmit has none on record. Carries the
    grader's reason and excerpt (classify.py's readers) and, for a run the
    Brief classified, that run's tag for the case (``cls``,
    ``also_failing_prs``) so the page can say whose failure it was."""
    for tier, rows in ((tiers.TIER_PRESUBMIT, gate), (tiers.TIER_NIGHTLY, nightly)):
        for run, task in reversed(rows):
            state = cell_state(task)
            if state not in (STRIP_FAIL, STRIP_PARTIAL):
                continue
            build = str(run.get("build_id") or "")
            tagged = classified.get(build, {}).get(name) or {}
            passed, failed, infra = rep_counts(task_reps(task))
            return {
                "tier": tier,
                "build": build,
                "pr": run.get("pr") if isinstance(run.get("pr"), int) else None,
                "at": run_when(run),
                "state": state,
                "reps": {"pass": passed, "fail": failed, "infra": infra},
                "reason": classify.first_reason(task),
                "excerpt": classify.excerpt_of(task),
                "cls": tagged.get("cls"),
                "also_failing_prs": tagged.get("also_failing_prs", 0),
                "event": is_run_event(run),
            }
    return None


def rate_pair(counts: tuple[int, int] | None) -> list[int] | None:
    """``[passed, failed]`` over graded reps, or None when nothing graded."""
    if not counts or counts[0] + counts[1] == 0:
        return None
    return [counts[0], counts[1]]


def case_documents(data: dict, notes: dict, admitted: frozenset | None, demoted: dict[str, str], classified: dict[str, dict]) -> dict[str, dict]:
    """``brief.json``'s ``cases{}``: per case, what the Cases page and the
    Grid need beyond the runs -- roster status, domain, notes, the per-tier
    7- and 30-day rates, the strip and the last failure."""
    gate = appearances_by_case(gate_runs(data))
    nightly = appearances_by_case(nightly_runs(data))
    rates = {
        (tier, days): tier_pass_rates(data, tier, days)
        for tier in tiers.TIERS
        for days in TIER_RATE_WINDOWS_DAYS
    }
    cases = {}
    for case in data.get("cases") or []:
        if not isinstance(case, dict) or case.get("name") is None:
            continue
        name = str(case["name"])
        status, demoted_on = case_status(case, admitted, demoted)
        note = notes.get(name) or {}
        cases[name] = {
            "active": case.get("active") is True,
            "nightly_active": case.get("nightly_active") is True,
            "admitted": admitted is None or name in admitted,
            "domain": str(case.get("domain") or DOMAIN_UNKNOWN),
            "status": status,
            "demoted_on": demoted_on,
            "note": note.get("note"),
            "issues": list(note.get("issues") or []),
            "rates": {
                tier: [rate_pair(rates[(tier, days)].get(name)) for days in TIER_RATE_WINDOWS_DAYS]
                for tier in tiers.TIERS
            },
            "strip": case_strip(gate.get(name, [])),
            "last_failure": last_failure(name, gate.get(name, []), nightly.get(name, []), classified),
        }
    return cases


def sorted_releases(data: dict) -> list[dict]:
    """Newest first. data.json is re-sorted here rather than trusted: the
    collector writes it in order, but the renderer also runs against files
    edited by hand."""
    releases = [r for r in data.get("releases") or [] if isinstance(r, dict)]
    releases.sort(
        key=lambda r: (
            str(r.get("started") or ""),
            int(r["build_id"]) if str(r.get("build_id", "")).isdigit() else 0,
        ),
        reverse=True,
    )
    return releases[:RELEASES_MAX_ROWS]


def compact_release(release: dict) -> dict:
    """One release-candidate run as the Brief's table needs it, every field
    validated here so the page formats and never judges: strings kept as
    strings, numbers only when finite, the artifacts link only when it is
    https, and the graded outcome counted rather than shipped task by task."""
    def text(key: str) -> str | None:
        value = release.get(key)
        return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) else None

    def number(key: str) -> float | None:
        value = release.get(key)
        return value if is_number(value) else None

    tasks = run_tasks(release)
    infra = sum(1 for t in tasks if t.get("result") == "infra")
    url = release.get("artifacts_url")
    return {
        "build": text("build_id"),
        "rc_tag": text("rc_tag"),
        "commit": text("commit"),
        "tier": text("tier"),
        "verdict": text("verdict"),
        "result": text("result"),
        "started": release.get("started") if iso_ms(release.get("started")) is not None else None,
        "duration_s": release["duration_s"] if is_count(release.get("duration_s")) and release["duration_s"] > 0 else None,
        "artifacts_url": url if isinstance(url, str) and url.startswith(RELEASE_URL_SCHEME) else None,
        "pass_rate": number("pass_rate"),
        "baseline_rate": number("baseline_rate"),
        "margin": number("margin"),
        "cases": {
            "passed": sum(1 for t in tasks if t.get("result") == "pass"),
            "graded": len(tasks) - infra,
            "infra": infra,
        } if tasks else None,
    }


def pending_builds(data: dict) -> list[dict]:
    """``pending_builds`` as the Grid's "still running" columns: id and when
    the collector first saw it, oldest first. A malformed entry is dropped,
    and so is one first seen more than PENDING_MAX_AGE_MS before the
    reference time: that build is not running any more (docstring of the
    constant)."""
    raw = data.get("pending_builds")
    if not isinstance(raw, list):
        return []
    anchor = reference_ms(data)
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or not str(entry.get("build_id") or "").isdigit():
            continue
        seen = iso_ms(entry.get("first_seen"))
        if seen is None or (anchor is not None and seen < anchor - PENDING_MAX_AGE_MS):
            continue
        out.append({"build": str(entry["build_id"]), "first_seen": entry["first_seen"]})
    out.sort(key=lambda e: int(e["build"]))
    return out


def brief_document(data: dict, health: dict | None, history: list[dict] | None, merges: list[dict] | None,
                   notes: dict | None = None, events: dict | None = None,
                   admitted: frozenset | None = None, demoted: dict[str, str] | None = None) -> dict:
    """The document every page reads. Runs are the presubmit's last
    RUN_VIEW_DAYS of data.json, oldest first, each classified against every
    run on record with the verdict in force when it finished. The nightly's
    runs are not listed -- they are nobody's pull request and the Brief is
    the gate's -- but they travel into the classification, which reads them
    for each case's nightly_failed_recent note, and into each case's record
    (its nightly rates, and its last failure when the presubmit has none).
    ``admitted`` and ``demoted`` default to the checkout's roster and roster
    page; tests pass their own."""
    anchor = reference_ms(data)
    runs = [r for r in data.get("runs") or [] if isinstance(r, dict)]
    now = ms_to_utc(anchor) if anchor is not None else None
    incidents = history_incidents(history) if history else []
    if admitted is None:
        admitted = classify.admitted_cases()
    if demoted is None:
        demoted = demotion_dates()
    out_runs = []
    classified: dict[str, dict] = {}
    for run in gate_runs(data):
        started = iso_ms(run.get("started"))
        if anchor is not None and started is not None and started < anchor - RUN_VIEW_DAYS * DAY_MS:
            continue
        finished = iso_ms(run.get("finished")) or started
        at = health_at(history, finished, incidents) if history else None
        # The verdict a run is judged against: its tick from the history;
        # the current verdict for a run newer than the history's last tick
        # (or when there is no history at all); none for a run that
        # predates the history -- today's outage says nothing about it.
        first_tick = iso_ms(history[0]["tick"]) if history else None
        predates = first_tick is not None and finished is not None and finished < first_tick
        against = at if at is not None else (None if predates else health)
        verdict = classify.classify_run(run, runs, health_at=against, now=now, admitted=admitted)
        out_runs.append(compact_run(run, verdict, at))
        classified[verdict["build"]] = {c["case"]: c for c in verdict["cases"]}
    return {
        "schema_version": 1,
        "generated_at": data.get("generated_at") if iso_ms(data.get("generated_at")) is not None else None,
        "stale_after_s": data.get("stale_after_s") if isinstance(data.get("stale_after_s"), (int, float)) else None,
        "run_days": RUN_VIEW_DAYS,
        "rate_windows_days": list(TIER_RATE_WINDOWS_DAYS),
        "strip_runs": STRIP_RUNS,
        "admitted": sorted(admitted) if admitted is not None else None,
        "health": health,
        "history": {"ticks": [
            {"tick": t["tick"], "state": t["state"], "condition": t["condition"], "since": t["since"],
             "failing_cases": t["failing_cases"], "recovering": t["recovering"]} for t in history
        ], "incidents": incidents} if history is not None else None,
        "merges": merges,
        "catches": (events or {}).get("catches"),
        "cases": case_documents(data, notes or {}, admitted, demoted, classified),
        "runs": out_runs,
        "pending": pending_builds(data),
        "releases": [compact_release(r) for r in sorted_releases(data)],
        "nightly": nightly.nightly_document(data),
    }


# --------------------------------------------------------------------------
# the pages


def render_page(page: str, brief: dict, data: dict, public_url: str | None = None) -> str:
    """One of the five pages from the shared template, with brief.json (and
    the health verdict, when there is one) inlined as JSON data elements and
    pages.js after them. ``page`` is a PAGES key."""
    template = PAGE_TEMPLATE.read_text()
    values = {
        "__TITLE__": PAGES[page]["title"],
        "__PAGE__": page,
        "__NAV_BRIEF__": 'class="on"' if page == "brief" else "",
        "__NAV_GRID__": 'class="on"' if page == "grid" else "",
        "__NAV_CASES__": 'class="on"' if page == "cases" else "",
        "__NAV_NIGHTLY__": 'class="on"' if page == "nightly" else "",
        # The PR view is a tab only while it is the page being read.
        "__NAV_RUN__": f'<a href="{RUN_PAGE}" class="on">PR view</a>' if page == "run" else "",
        "__BASE__": base_html(public_url),
        "__INLINE_BRIEF__": inline_json_html(INLINE_BRIEF_ID, brief),
        "__INLINE_HEALTH__": inline_json_html(INLINE_HEALTH_ID, brief["health"]) if brief.get("health") else "",
        "__META__": meta_html(data),
        "__FRESHNESS__": freshness_html(data),
        "__PAGES_JS__": PAGES_JS.read_text(),
    }
    for token in values:
        if token not in template:
            raise SystemExit(f"ERROR: page template is missing the {token} marker")
    # One pass over the template only: substituted values are never
    # re-scanned, so data that happens to contain a marker string stays
    # inert text instead of expanding into the raw JSON bootstrap.
    return re.sub(
        "|".join(re.escape(token) for token in values),
        lambda match: values[match.group(0)],
        template,
    )


def meta_html(data: dict) -> str:
    runs = sorted_runs(data)
    if not runs:
        return "no runs on record"
    sha = str(runs[-1].get("head_sha") or "")[:7]
    return f"head {esc(sha)}" if sha else "head unknown"


def freshness_html(data: dict) -> str:
    """The badge's baked text; the page rewrites it in Toronto time on load."""
    generated = parse_iso(data.get("generated_at"))
    return f"updated {generated:%H:%M} UTC" if generated else "updated —"


def esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#x27;")
    )


def bootstrap_json(value) -> str:
    """JSON safe to inline in a <script> block: every '<' is emitted as the
    JSON escape \\u003c. Escaping only '</' is not enough -- the HTML
    tokenizer leaves script-data state on '<!--' too, and '<!--<script'
    puts it in the double-escaped state where the block's own '</script>'
    no longer closes it, so a hostile data string would silently disable
    the whole live read side."""
    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c")


def inline_json_html(element_id: str, value) -> str:
    """A data element a page reads with JSON.parse on boot. bootstrap_json
    keeps every '<' out of it, so no data string can close the element."""
    return f'<script type="application/json" id="{element_id}">{bootstrap_json(value)}</script>'


def base_html(public_url: str | None) -> str:
    """``<base href>`` for the published site, so every relative link on the
    page (nav, footer, run.html#build=, the incident deep links) resolves
    there whatever URL the browser is showing; nothing when no public URL
    is known, which keeps a file:// render browsable."""
    if not public_url:
        return ""
    return f'<base href="{esc(public_url.rstrip("/") + "/")}">'


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, help="collector data.json")
    parser.add_argument("--out-dir", required=True, help="directory to write into")
    parser.add_argument(
        "--notes",
        default=str(DEFAULT_NOTES),
        help="case-notes.yaml (optional notes and issue links per case; absent file is fine)",
    )
    parser.add_argument(
        "--events",
        default=str(DEFAULT_EVENTS),
        help="events.yaml (optional catch counts; absent file is fine)",
    )
    parser.add_argument(
        "--health",
        default=None,
        help=f"{HEALTH_FILE} from the adjudicator (optional; absent or malformed renders no verdict)",
    )
    parser.add_argument(
        "--health-history",
        default=None,
        help=f"{HEALTH_HISTORY_FILE}, one health.json per line plus a tick stamp (optional; absent means current state only)",
    )
    parser.add_argument(
        "--repo-root",
        default=str(classify.REPO_ROOT),
        help="checkout whose git log lists the merges to main (a shallow checkout omits the block)",
    )
    parser.add_argument(
        "--public-url",
        nargs="?",
        const=PUBLISHED_SITE,
        default=None,
        metavar="BASE",
        help=f"emit <base href> so every link resolves to this site; the bare flag means the published dashboard ({PUBLISHED_SITE}); default (or an empty value): none, links stay relative",
    )
    args = parser.parse_args(argv)

    data = load_data(pathlib.Path(args.data))
    notes = load_notes(pathlib.Path(args.notes))
    events = load_events(pathlib.Path(args.events))
    health = load_health(pathlib.Path(args.health)) if args.health else None
    history = load_health_history(pathlib.Path(args.health_history)) if args.health_history else None
    merges = recent_merges(pathlib.Path(args.repo_root), reference_ms(data))
    brief = brief_document(data, health, history, merges, notes=notes, events=events)

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for page, spec in PAGES.items():
        (out_dir / spec["file"]).write_text(render_page(page, brief, data, args.public_url))
    (out_dir / BRIEF_JSON).write_text(json.dumps(brief, separators=(",", ":")))
    # health.json and health-history.jsonl are deliberately not copied into
    # the out-dir: the adjudicator owns those objects, and republishing a
    # copy would overwrite a fresher verdict with the one this render read.
    shutil.copyfile(args.data, out_dir / "data.json")
    print(f"wrote {', '.join(str(out_dir / spec['file']) for spec in PAGES.values())}, {BRIEF_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
