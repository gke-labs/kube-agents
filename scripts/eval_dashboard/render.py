#!/usr/bin/env python3
"""Render the eval dashboard from the collector's data.json.

Usage::

    python3 scripts/eval_dashboard/render.py --data data.json --out-dir out/

writes ``out/index.html`` (from ``template/index.html.tmpl``) and copies the
data file alongside it, so the published directory is self-contained.

The page tells one story in two bands:

* **THE AGENT** -- is the agent getting better or worse, measured on the
  merged-PR cohort (the final ``pr_merged`` run of each PR): weekly pass
  rate, human-annotated catches, domain coverage, and a by-day pass-fraction
  trend with named event markers.
* **THE GATE** -- is the gate trustworthy: false-red and infra-rep tiles, a
  case x run outcome matrix over the last runs, and a Pareto of normalized
  failure signatures.

Three rules shape everything here:

* **Computed-only.** Every figure on the page is derived from data.json --
  no hand-typed numbers can go stale in a template. The two optional extra
  inputs are ``case-notes.yaml`` (``--notes``: one-line annotations, issue
  links and badges per case) and ``events.yaml`` (``--events``: dated event
  markers plus the few human-judgment counts no log line carries). An absent
  file degrades to "no annotation" -- never an error.
* **INFRA is not failure.** A rep (or task) whose result is ``infra`` is
  excluded from every pass-fraction denominator, matching the suite's policy
  that infrastructure failures never count against a PR.
* **Run-level events are charged to the run, not the cases.** A run where at
  least ``RUN_EVENT_FAIL_FRACTION`` of its graded tasks failed (a broken PR,
  an endpoint outage) renders normally in the matrix but its failures are
  excluded from per-case aggregates such as the failure-signature Pareto.
  The exclusion is deliberately scoped to per-case aggregates: band 1's
  cohort is the final run of each *merged* PR, and a merged PR's own
  failures are that cohort's signal, not noise to exclude.

The reader contract is schema_version 1 of the collector's data.json.
Optional fields may be absent and unknown additive fields are ignored, so
this renderer and the collector can ship independently. In particular
``tasks[].reps`` and ``runs[].pr_merged`` are optional additive fields
(SCHEMA.md, "Optional run and task fields") that no collector version emits
yet; without them every task falls back to its single ``result`` and the
merged-PR cohort is simply empty.

The rendered page is also live: render.py bakes the data, notes and events
into the template, whose script re-renders in place from a fresh
``data.json`` fetch every 60 seconds and keeps a freshness badge honest (see
the template's "Live read side" comment). The Python fragment builders here
and the JS mirrors there are intentionally parallel -- change them together.

Only stdlib + PyYAML (already in requirements-test.txt) -- no build step.
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import math
import pathlib
import re
import shutil
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE / "template" / "index.html.tmpl"
DEFAULT_NOTES = HERE / "case-notes.yaml"
DEFAULT_EVENTS = HERE / "events.yaml"
ISSUE_URL = "https://github.com/gke-labs/kube-agents/issues"
ISSUE_RE = re.compile(r"^#(\d+)$")

# The 20-run yardstick the evidence bars are drawn against, borrowed from
# the screening window in docs/designs/testing-strategy.md. The bars show
# recorded history depth only -- admission to the gate is measured by the
# baseline store, which fills from main-branch runs alone
# (bench/baselines/README.md); the presubmit runs collected here never
# advance it, so a full bar is not admission.
SCREENING_WINDOW = 20

# The three results a rep (or a task) can carry; anything else is treated as
# "not measured" rather than guessed at.
REP_RESULTS = ("pass", "fail", "infra")

# Matrix window: the last N runs that measured at least one task. 30 columns
# is about two weeks of PR traffic and still fits one screen at 22px cells.
MATRIX_RUNS = 30
# The by-day trend keeps at most this many day buckets on screen.
TREND_DAYS = 30
# Failure-signature Pareto: reps from runs started within this many days of
# the data's generated_at, and at most this many bars.
PARETO_WINDOW_DAYS = 7
PARETO_MAX_ROWS = 8
# A run where at least this fraction of its graded tasks failed is a
# run-level event: the run is broken (a red PR, an outage), so its failures
# are charged to the run and excluded from per-case aggregates.
RUN_EVENT_FAIL_FRACTION = 0.8
# Weekly pass-rate window, in milliseconds (all time math here is epoch ms,
# matching the JS mirror's Date.parse).
WEEK_MS = 7 * 24 * 3600 * 1000
DAY_MS = 24 * 3600 * 1000
# An unrecognized failure reason is grouped by its first characters.
REASON_SNIPPET_CHARS = 60
# Prow's job verdict for a green run (SCHEMA.md: runs[].result).
RUN_RESULT_GREEN = "SUCCESS"

# --- failure-signature normalization -------------------------------------
# Deliberately small and documented: a reason that matches nothing renders
# as its own first-60-chars group with a neutral bar, so an unclassified
# failure is never silently blamed on infra, the checks, or the agent.
# Signature substrings (matched case-insensitively against reps[].reason):
SIG_429 = "http 429"  # endpoint saturation; infra-classed since #1095
SIG_NOT_REAL_RUN = ("not evidence of a real agent run", "not_a_real_run")
SIG_NEVER_RAN = "no agent ever ran"  # the never-ran signature; infra-classed since #1184
SIG_PHRASES_ABSENT = "required phrases absent"  # an exact-check miss
# "check <name>" inside a phrases-absent reason names the exact check.
CHECK_NAME_RE = re.compile(r"check[ :]+['\"]?([A-Za-z0-9_./-]+)", re.I)
# Bar-color classification keywords, applied to the raw reason:
INFRA_REASON_KEYWORDS = (
    "http 429",
    "rate limit",
    "timed out",
    "timeout",
    "connection",
    SIG_NEVER_RAN,
)
CHECK_REASON_KEYWORDS = (SIG_PHRASES_ABSENT,)
AGENT_REASON_KEYWORDS = ("false finding",)

# Pareto groups for reps that carry no reason string at all (the collector
# fallback path, and infra reps whose reason never got recorded). Split by
# result so an unexplained infra wave is never mistaken for agent failures.
LABEL_NO_REASON = "(no reason recorded)"
LABEL_NO_REASON_INFRA = "(infra, no reason recorded)"

# The run-level event rule, stated wherever per-case aggregates are shown.
RUN_EVENT_CAPTION = "run-level events excluded from per-case stats"

# Matrix cell rendering: CSS class and tooltip text per cell state.
CELL_CLASSES = {"pass": "c-g", "partial": "c-a", "fail": "c-r", "infra": "c-i", "none": "c-n"}
CELL_TITLES = {
    "pass": "passed all reps",
    "partial": "partial (some reps passed)",
    "fail": "failed all reps",
    "infra": "infra — excluded",
    "none": "not in run",
}

# Trend chart geometry (SVG user units) and axis furniture.
TREND_W, TREND_H = 1000, 196
TREND_PAD_X, TREND_PAD_Y = 34, 28
# Consecutive event labels alternate between two rows so adjacent markers
# do not overwrite each other.
TREND_EVENT_ROW_OFFSET = 10
TREND_GRIDLINES = (0.25, 0.5, 0.75, 1.0)
TREND_MAX_X_LABELS = 8
# Event labels on the chart are clipped so clustered events stay readable;
# the matrix footnote carries every label in full.
TREND_EVENT_LABEL_CHARS = 18

esc = html.escape


def fmt(value: float, digits: int = 0) -> str:
    """Format a non-negative number the way JS ``toFixed``/``Math.round``
    does. Python's ``:.Nf`` rounds half to even (0.25 -> "0.2"), JS rounds
    half away from zero (0.25 -> "0.3"); without this, numbers visibly
    change when the template's on-load re-render replaces the baked HTML."""
    factor = 10**digits
    return f"{math.floor(value * factor + 0.5) / factor:.{digits}f}"


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


def measured_runs(data: dict) -> list[dict]:
    """Runs that measured anything: at least one task row. An aborted or
    deadline-truncated build parses to zero tasks; giving one a matrix
    column or a trend point would chart a run that measured nothing. The
    header sha deliberately stays on sorted_runs."""
    return [r for r in sorted_runs(data) if run_tasks(r)]


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
    """Epoch milliseconds, the unit the JS mirror gets from Date.parse."""
    parsed = parse_iso(value)
    return parsed.timestamp() * 1000 if parsed else None


def utc_day(value) -> str | None:
    """'YYYY-MM-DD' in UTC; the trend's day bucket and the event-marker
    join key. Mirrors the JS ``toISOString().slice(0, 10)``."""
    parsed = parse_iso(value)
    return f"{parsed:%Y-%m-%d}" if parsed else None


def run_label(run: dict, index: int) -> str:
    if run.get("pr") is not None:
        return f"#{run['pr']}"
    build_id = str(run.get("build_id") or "")
    return build_id[:6] if build_id else f"run {index + 1}"


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
    """One task's matrix verdict: 'pass' (every graded rep passed),
    'partial' (some passed, some failed), 'fail' (every graded rep failed),
    'infra' (every counted rep was infra -- excluded, not failed), or
    'none' (nothing measured)."""
    reps = task_reps(task)
    if not reps:
        return "none"
    passed, failed, _ = rep_counts(reps)
    if passed and failed:
        return "partial"
    if failed:
        return "fail"
    if passed:
        return "pass"
    return "infra"


def is_run_event(run: dict) -> bool:
    """True when at least RUN_EVENT_FAIL_FRACTION of the run's graded tasks
    (state pass/partial/fail; infra and unmeasured don't grade) failed
    outright. Such a run is broken as a whole, so its failures are charged
    to the run, not the cases."""
    graded = [s for s in (cell_state(t) for t in run_tasks(run)) if s in ("pass", "partial", "fail")]
    if not graded:
        return False
    return graded.count("fail") / len(graded) >= RUN_EVENT_FAIL_FRACTION


# --------------------------------------------------------------------------
# cohorts and windows


def merged_final_runs(data: dict) -> list[dict]:
    """Band-1 cohort: for each PR, its final measured run with
    ``pr_merged: true`` -- the run whose codebase is (modulo the merge
    commit) what actually shipped. Runs without a PR number count
    individually. Empty when no collector has reported pr_merged yet."""
    runs = [r for r in measured_runs(data) if r.get("pr_merged") is True]

    def key(run: dict):
        # A run without a PR number is its own cohort entry (identity key).
        return f"pr:{run['pr']}" if run.get("pr") is not None else id(run)

    final = {key(run): run for run in runs}  # chronological: last one wins
    return [r for r in runs if final[key(r)] is r]


def reference_ms(data: dict) -> float | None:
    """The time axis anchor: generated_at, else the newest run's start.
    Deliberately not the wall clock, so the baked HTML and the JS re-render
    of the same data.json agree."""
    anchor = iso_ms(data.get("generated_at"))
    if anchor is not None:
        return anchor
    for run in reversed(sorted_runs(data)):
        anchor = iso_ms(run.get("started"))
        if anchor is not None:
            return anchor
    return None


def week_pass_stats(data: dict) -> dict:
    """Rep-level pass fraction of the merged-PR cohort for the 7 days ending
    at the reference time ('cur') and the 7 before that ('prev'), plus the
    current window's run count. A cohort with no usable timestamps counts
    entirely as current -- better one honest number than none."""
    anchor = reference_ms(data)
    cur = {"pass": 0, "fail": 0, "runs": 0}
    prev = {"pass": 0, "fail": 0}
    for run in merged_final_runs(data):
        started = iso_ms(run.get("started"))
        bucket = None
        if anchor is None or started is None:
            bucket = cur
        elif anchor - WEEK_MS < started <= anchor:
            bucket = cur
        elif anchor - 2 * WEEK_MS < started <= anchor - WEEK_MS:
            bucket = prev
        if bucket is None:
            continue
        passed = failed = 0
        for task in run_tasks(run):
            p, f, _ = rep_counts(task_reps(task))
            passed += p
            failed += f
        bucket["pass"] += passed
        bucket["fail"] += failed
        if bucket is cur:
            cur["runs"] += 1
    def rate(bucket):
        total = bucket["pass"] + bucket["fail"]
        return bucket["pass"] / total if total else None
    return {"cur": rate(cur), "prev": rate(prev), "runs_cur": cur["runs"]}


def day_points(data: dict) -> list[tuple[str, float]]:
    """(day, rep-level pass fraction) per UTC day of the merged-PR cohort,
    oldest first, capped to the last TREND_DAYS days that measured
    anything."""
    buckets: dict[str, list[int]] = {}
    for run in merged_final_runs(data):
        day = utc_day(run.get("started"))
        if day is None:
            continue
        bucket = buckets.setdefault(day, [0, 0])
        for task in run_tasks(run):
            p, f, _ = rep_counts(task_reps(task))
            bucket[0] += p
            bucket[1] += f
    points = [
        (day, counts[0] / (counts[0] + counts[1]))
        for day, counts in sorted(buckets.items())
        if counts[0] + counts[1]
    ]
    return points[-TREND_DAYS:]


# --------------------------------------------------------------------------
# case-notes.yaml (optional flavor; never an error)


def load_notes(path: pathlib.Path | None) -> dict[str, dict]:
    """``{case: {"note": str|None, "issues": [str, ...], "badge":
    str|None}}``. Absent file, empty file, or malformed entry all degrade
    to "no note"."""
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
        raw_issues = entry.get("issues")
        issues = [str(i) for i in raw_issues] if isinstance(raw_issues, list) else []
        badge = entry.get("badge")
        badge = str(badge) if isinstance(badge, str) else None
        if note or issues or badge:
            notes[str(name)] = {"note": note, "issues": issues, "badge": badge}
    return notes


def note_html(entry: dict | None) -> str:
    if not entry:
        return ""
    parts = []
    if entry.get("note"):
        parts.append(esc(str(entry["note"])))
    for issue in entry.get("issues", []):
        match = ISSUE_RE.match(issue.strip())
        if match:
            parts.append(f'<a href="{ISSUE_URL}/{match.group(1)}">{esc(issue)}</a>')
        else:
            parts.append(esc(issue))
    if not parts:
        return ""
    return f'<div class="tnote">{" · ".join(parts)}</div>'


def badge_html(entry: dict | None) -> str:
    """The held-out / new pill next to a matrix row name. Values come from
    case-notes.yaml, never from code; an unknown badge renders nothing."""
    badge = str((entry or {}).get("badge") or "").strip().lower()
    if badge == "held-out":
        return '<em class="b-hold">held out</em>'
    if badge == "new":
        return '<em class="b-new">new</em>'
    return ""


# --------------------------------------------------------------------------
# events.yaml (optional; dated markers + the human-judgment counts)


def load_events(path: pathlib.Path | None) -> dict:
    """``{"events": [{"date": "YYYY-MM-DD", "label": str}], "catches":
    dict|None, "false_reds_7d": value|None}``. Absent or malformed file
    degrades to no markers and em-dash tiles."""
    out = {"events": [], "catches": None, "false_reds_7d": None}
    if path is None or not path.exists():
        return out
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return out
    if not isinstance(raw, dict):
        return out
    for entry in raw.get("events") or []:
        if not isinstance(entry, dict):
            continue
        date, label = entry.get("date"), entry.get("label")
        if date is None or label is None:
            continue
        out["events"].append({"date": str(date)[:10], "label": str(label)})
    catches = raw.get("catches")
    if isinstance(catches, dict):
        out["catches"] = {
            "product_bugs": catches.get("product_bugs"),
            "prs_blocked": catches.get("prs_blocked"),
            "ledger": str(catches["ledger"]) if catches.get("ledger") is not None else None,
        }
    out["false_reds_7d"] = raw.get("false_reds_7d")
    return out


# --------------------------------------------------------------------------
# failure signatures


def reason_signature(reason: str | None) -> str:
    """The Pareto group a rep's reason string falls into. The map is small
    on purpose (module docstring: honest over clever); anything unmatched
    groups by its first REASON_SNIPPET_CHARS characters."""
    text = (reason or "").strip()
    if not text:
        return LABEL_NO_REASON
    low = text.lower()
    if SIG_429 in low:
        return "endpoint saturation (infra)"
    if SIG_NEVER_RAN in low:
        return "never ran: empty trajectory, zero tokens (infra)"
    if any(sig in low for sig in SIG_NOT_REAL_RUN):
        return "not a real agent run"
    if SIG_PHRASES_ABSENT in low:
        match = CHECK_NAME_RE.search(text)
        if match:
            return f"exact-check: {match.group(1)}"
        return "exact-check: required phrases absent"
    if len(text) > REASON_SNIPPET_CHARS:
        return text[:REASON_SNIPPET_CHARS] + "…"
    return text


def reason_class(reason: str | None) -> str:
    """Bar color: infra / check / agent by keyword, 'unknown' (neutral)
    otherwise -- an unclassified failure is not silently blamed."""
    low = (reason or "").lower()
    if any(kw in low for kw in INFRA_REASON_KEYWORDS):
        return "infra"
    if any(kw in low for kw in CHECK_REASON_KEYWORDS):
        return "check"
    if any(kw in low for kw in AGENT_REASON_KEYWORDS):
        return "agent"
    return "unknown"


def pareto_groups(data: dict) -> list[dict]:
    """Non-pass reps of the last PARETO_WINDOW_DAYS, grouped by normalized
    reason. Run-level events are excluded (charged to the run); runs
    without a started timestamp cannot be windowed and are skipped unless
    the data has no time anchor at all."""
    anchor = reference_ms(data)
    groups: dict[str, dict] = {}
    for run in sorted_runs(data):
        started = iso_ms(run.get("started"))
        if anchor is not None:
            if started is None:
                continue
            if started <= anchor - PARETO_WINDOW_DAYS * DAY_MS or started > anchor:
                continue
        if is_run_event(run):
            continue
        for task in run_tasks(run):
            for rep in task_reps(task):
                if rep["result"] == "pass":
                    continue
                reason = (rep["reason"] or "").strip()
                if reason:
                    label, cls = reason_signature(reason), reason_class(reason)
                elif rep["result"] == "infra":
                    # No reason, but the result itself says what it was.
                    label, cls = LABEL_NO_REASON_INFRA, "infra"
                else:
                    label, cls = LABEL_NO_REASON, "unknown"
                group = groups.setdefault(label, {"label": label, "count": 0, "cls": cls})
                group["count"] += 1
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["label"]))
    return ordered[:PARETO_MAX_ROWS]


# --------------------------------------------------------------------------
# HTML fragments


def delta_chip(cls: str, text: str) -> str:
    return f'<span class="delta {cls}">{esc(text)}</span>'


def tile(key: str, value_html: str, chip_html: str, detail: str) -> str:
    return (
        f'<div class="tile"><div class="k">{esc(key)}</div>'
        f'<div class="v">{value_html}{chip_html}</div>'
        f'<div class="d2">{esc(detail)}</div></div>'
    )


def agent_tiles_html(data: dict, events: dict) -> str:
    tiles = []

    # Pass rate, this week vs prior week.
    stats = week_pass_stats(data)
    if stats["cur"] is not None:
        pct = int(fmt(stats["cur"] * 100))
        chip = ""
        if stats["prev"] is not None:
            diff = pct - int(fmt(stats["prev"] * 100))
            if diff > 0:
                chip = delta_chip("up", f"▲ +{diff}pt vs prior week")
            elif diff < 0:
                chip = delta_chip("flat", f"▼ {diff}pt vs prior week")
            else:
                chip = delta_chip("flat", "= prior week")
        runs = stats["runs_cur"]
        detail = f"merged-PR cohort · {runs} run{'s' if runs != 1 else ''} this week"
        tiles.append(tile("Pass rate · this week", f"{pct}<small>%</small>", chip, detail))
    else:
        detail = (
            "no merged-PR runs this week"
            if merged_final_runs(data)
            else "no merged-PR runs on record"
        )
        tiles.append(tile("Pass rate · this week", "—", "", detail))

    # Product bugs caught -- human judgment, annotated in events.yaml.
    catches = events.get("catches") or {}
    if is_count(catches.get("product_bugs")):
        value = f"{int(catches['product_bugs'])}"
        if is_count(catches.get("prs_blocked")):
            value += f"<small> + {int(catches['prs_blocked'])} PRs blocked</small>"
        detail = (
            f"catch ledger {catches['ledger']}" if catches.get("ledger") else "events.yaml annotation"
        )
        tiles.append(tile("Product bugs caught", value, "", detail))
    else:
        tiles.append(tile("Product bugs caught", "—", "", "not annotated (events.yaml)"))

    # Domain coverage (collector-computed).
    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    covered, total = coverage.get("domains_covered"), coverage.get("domains_total")
    if is_count(covered) and is_count(total):
        covered, total = int(covered), int(total)
        raw_uncovered = coverage.get("uncovered")
        uncovered = [str(d) for d in raw_uncovered] if isinstance(raw_uncovered, list) else []
        chip = (
            delta_chip("up", "all covered")
            if covered >= total
            else delta_chip("flat", f"{total - covered} open")
        )
        cases = [c for c in data.get("cases") or [] if isinstance(c, dict)]
        blocking = sum(1 for c in cases if c.get("active"))
        if uncovered:
            detail = f"uncovered: {', '.join(uncovered)}"
        else:
            detail = f"{len(cases)} scenarios · {blocking} blocking"
        tiles.append(tile("Domains covered", f"{covered}<small>/ {total}</small>", chip, detail))
    else:
        tiles.append(tile("Domains covered", "—", "", "not reported"))

    return "".join(tiles)


def gate_tiles_html(data: dict, events: dict) -> str:
    tiles = []

    # False reds, 7d -- human classification, annotated in events.yaml.
    false_reds = events.get("false_reds_7d")
    if is_count(false_reds):
        tiles.append(tile("False reds · 7d", f"{int(false_reds)}", "", "human-classified · events.yaml"))
    else:
        tiles.append(tile("False reds · 7d", "—", "", "not annotated (events.yaml)"))

    # Infra-rep rate over the matrix window.
    window = measured_runs(data)[-MATRIX_RUNS:]
    total = infra = 0
    reps_reported = False
    for run in window:
        for task in run_tasks(run):
            raw = task.get("reps")
            if isinstance(raw, list) and any(
                isinstance(r, dict) and str(r.get("result", "")).lower() in REP_RESULTS
                for r in raw
            ):
                reps_reported = True
            for rep in task_reps(task):
                total += 1
                if rep["result"] == "infra":
                    infra += 1
    if total:
        value = f"{fmt(100 * infra / total, 1)}<small>%</small>"
        detail = (
            f"{infra} of {total} reps · last {len(window)} runs"
            if reps_reported
            else f"task-level fallback · last {len(window)} runs"
        )
        tiles.append(tile("Infra-rep rate", value, "", detail))
    else:
        tiles.append(tile("Infra-rep rate", "—", "", "no measured runs yet"))

    # Wall clock of the latest green full run.
    runs = sorted_runs(data)
    green = None
    green_index = -1
    for index in range(len(runs) - 1, -1, -1):
        run = runs[index]
        if (
            str(run.get("result") or "") == RUN_RESULT_GREEN
            and run_tasks(run)
            and isinstance(run.get("duration_s"), (int, float))
        ):
            green, green_index = run, index
            break
    if green:
        value = f"{fmt(green['duration_s'] / 60)}<small>min</small>"
        detail = f"latest green full run · {run_label(green, green_index)}"
        tiles.append(tile("Wall clock · green run", value, "", detail))
    else:
        tiles.append(tile("Wall clock · green run", "—", "", "no green full run on record"))

    # Queue wait: data.json carries no queued-at timestamp, so this cannot
    # be computed. An honest em-dash beats an invented number.
    tiles.append(tile("Queue wait · median", "—", "", "not reported in data.json"))

    return "".join(tiles)


def matrix_html(data: dict, notes: dict, events: dict) -> str:
    runs = measured_runs(data)[-MATRIX_RUNS:]
    cases = [
        c for c in data.get("cases") or [] if isinstance(c, dict) and c.get("active") is not False
    ]
    if not runs:
        return '<div class="cap" style="margin-top:12px">no measured runs yet</div>'
    if not cases:
        return '<div class="cap" style="margin-top:12px">no active cases on record yet</div>'

    event_days = {e["date"]: e["label"] for e in events.get("events") or []}
    run_days = [utc_day(r.get("started")) for r in runs]
    run_events = [is_run_event(r) for r in runs]
    task_maps = [{str(t.get("name")): t for t in run_tasks(r)} for r in runs]

    head = ['<div class="mx-row mx-head"><div class="mx-name"></div>']
    for index, run in enumerate(runs):
        day = run_days[index]
        marker = "▲" if day in event_days else ""
        title = f"{run_label(run, index)} · {day or '?'}"
        if day in event_days:
            title += f" · {event_days[day]}"
        if run_events[index]:
            title += " · run-level event"
        head.append(f'<div class="mx-col" title="{esc(title)}">{marker}</div>')
    head.append("</div>")

    rows = ["".join(head)]
    for case in cases:
        name = str(case.get("name") or "?")
        entry = notes.get(name)
        row = [f'<div class="mx-row"><div class="mx-name"><span>{esc(name)}</span>{badge_html(entry)}</div>']
        for index, run in enumerate(runs):
            task = task_maps[index].get(name)
            state = cell_state(task) if task else "none"
            title = f"{name} · {run_label(run, index)} · {CELL_TITLES[state]}"
            if run_events[index]:
                title += " · run-level event (charged to the run)"
            row.append(f'<div class="cell {CELL_CLASSES[state]}" title="{esc(title)}"></div>')
        row.append("</div>")
        rows.append("".join(row))

    marks = [
        f'<span>▲ {esc(day[5:])} {esc(event_days[day])}</span>'
        for day in sorted(set(d for d in run_days if d in event_days))
    ]
    if marks:
        rows.append(f'<div class="mx-ev">{"".join(marks)}</div>')
    return f'<div class="mx">{"".join(rows)}</div>'


def pareto_html(data: dict) -> str:
    groups = pareto_groups(data)
    if not groups:
        return (
            '<div class="cap" style="margin-top:12px">'
            f"no failing or infra reps in the last {PARETO_WINDOW_DAYS} days</div>"
        )
    top = groups[0]["count"]
    rows = []
    for group in groups:
        width = fmt(100 * group["count"] / top, 1)
        rows.append(
            f'<div class="pa-row"><div class="pa-bar-wrap">'
            f'<div class="pa-bar pa-{group["cls"]}" style="width:{width}%"></div></div>'
            f'<div class="pa-count">{group["count"]}</div>'
            f'<div class="pa-name">{esc(group["label"])}</div></div>'
        )
    return "".join(rows)


def trend_svg(points: list[tuple[str, float]], events: dict) -> str:
    """The band-1 by-day pass-fraction line, with vertical event markers on
    days that carry an events.yaml entry. Dots carry ``data-l`` labels the
    shared tooltip listener reads."""
    if len(points) < 2:
        return ""
    width, height, px, py = TREND_W, TREND_H, TREND_PAD_X, TREND_PAD_Y

    def xs(i: int) -> float:
        return px + i * (width - 2 * px) / (len(points) - 1)

    def ys(v: float) -> float:
        return height - py - v * (height - 2 * py)

    parts = [f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none">']
    for grid in TREND_GRIDLINES:
        parts.append(
            f'<line x1="{px}" y1="{fmt(ys(grid), 1)}" x2="{width - px}" y2="{fmt(ys(grid), 1)}" stroke="var(--line)"/>'
            f'<text x="{px - 6}" y="{fmt(ys(grid) + 3, 1)}" text-anchor="end" font-size="9" '
            f'fill="var(--text-muted)">{fmt(grid * 100)}</text>'
        )
    event_days = {e["date"]: e["label"] for e in events.get("events") or []}
    marker = 0
    for i, (day, _) in enumerate(points):
        if day in event_days:
            label = event_days[day]
            if len(label) > TREND_EVENT_LABEL_CHARS:
                label = label[:TREND_EVENT_LABEL_CHARS] + "…"
            text_y = py - 6 - (marker % 2) * TREND_EVENT_ROW_OFFSET
            marker += 1
            parts.append(
                f'<line x1="{fmt(xs(i), 1)}" y1="{py}" x2="{fmt(xs(i), 1)}" y2="{height - py}" '
                f'stroke="var(--accent)" stroke-opacity=".35" stroke-dasharray="3 3"/>'
                f'<text x="{fmt(xs(i), 1)}" y="{text_y}" text-anchor="middle" font-size="9" '
                f'fill="var(--accent-link,var(--accent))">{esc(label)}</text>'
            )
    poly = " ".join(f"{fmt(xs(i), 1)},{fmt(ys(v), 1)}" for i, (_, v) in enumerate(points))
    parts.append(
        f'<polyline points="{poly}" fill="none" stroke="var(--accent)" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    for i, (day, v) in enumerate(points):
        parts.append(
            f'<circle cx="{fmt(xs(i), 1)}" cy="{fmt(ys(v), 1)}" r="4" fill="var(--accent)" '
            f'stroke="var(--surface-1)" stroke-width="2" data-l="{esc(day)} · {fmt(v * 100)}%"/>'
        )
    step = max(1, math.ceil(len(points) / TREND_MAX_X_LABELS))
    for i, (day, _) in enumerate(points):
        if i % step == 0 or i == len(points) - 1:
            parts.append(
                f'<text x="{fmt(xs(i), 1)}" y="{height - 4}" text-anchor="middle" font-size="9.5" '
                f'font-weight="600" fill="var(--text-muted)">{esc(day[5:])}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def band_agent_html(data: dict, events: dict) -> str:
    chart = trend_svg(day_points(data), events)
    chart = chart or (
        '<div class="cap" style="margin-top:14px">not enough merged-PR days yet '
        "(needs runs with pr_merged from the collector)</div>"
    )
    return f"""
  <section class="band">
    <div class="band-h"><h2 id="agent">The agent</h2><span class="cohort">cohort: final run of each merged PR</span></div>
    <div class="sub">Measured only where the codebase is what actually shipped.</div>
    <div class="tiles tiles-3">{agent_tiles_html(data, events)}</div>
    <div class="card pad chartcard">
      <div class="t">Suite pass fraction, by day</div>
      <div class="s">merged-PR cohort · pass / (pass + fail) over reps, INFRA excluded · event markers from events.yaml</div>
      <div id="daytrend">{chart}</div>
    </div>
  </section>"""


def band_gate_html(data: dict, notes: dict, events: dict) -> str:
    matrix_runs = len(measured_runs(data)[-MATRIX_RUNS:])
    return f"""
  <section class="band">
    <div class="band-h"><h2 id="gate">The gate</h2><span class="cohort">cohort: all PR runs · {RUN_EVENT_CAPTION}</span></div>
    <div class="sub">Is the gate trustworthy — flake forensics, infra tax, and the cost of a run.</div>
    <div class="tiles">{gate_tiles_html(data, events)}</div>
    <div class="card pad">
      <div class="t">Case × run outcome matrix</div>
      <div class="s">one row per active case, one cell per run · last {matrix_runs} measured runs · ▲ = events.yaml marker · {RUN_EVENT_CAPTION}</div>
      {matrix_html(data, notes, events)}
      <div class="legend mx-legend">
        <span><span class="cell c-g"></span>passed all reps</span>
        <span><span class="cell c-a"></span>partial</span>
        <span><span class="cell c-r"></span>failed all reps</span>
        <span><span class="cell c-i"></span>infra — excluded</span>
        <span><span class="cell c-n"></span>not in run</span>
      </div>
    </div>
    <div class="card pad">
      <div class="t">Failure signatures · last {PARETO_WINDOW_DAYS} days</div>
      <div class="s">non-pass reps grouped by normalized reason · {RUN_EVENT_CAPTION} · gray = infra, violet = check design, amber = agent behaviour, neutral = unclassified</div>
      {pareto_html(data)}
    </div>
  </section>"""


def evidence_row(case: dict, notes: dict) -> str:
    name = str(case.get("name") or "?")
    have = case.get("runs_on_record")
    have = int(have) if is_count(have) else 0
    width = min(100.0, 100.0 * have / SCREENING_WINDOW)
    presubmit = (
        '<span class="pill p-pass">IN PRESUBMIT</span>'
        if case.get("active")
        else '<span class="pill p-fix">NOT IN PRESUBMIT</span>'
    )
    return (
        f'<tr><td><div class="tname">{esc(name)}</div>{note_html(notes.get(name))}</td>'
        f'<td><div style="display:flex;align-items:center;gap:10px">'
        f'<div class="prog"><i style="width:{fmt(width)}%"></i></div>'
        f'<span class="cap">{have} of {SCREENING_WINDOW}</span></div></td>'
        f"<td>{presubmit}</td></tr>"
    )


def evidence_html(data: dict, notes: dict) -> str:
    cases = sorted(
        (c for c in data.get("cases") or [] if isinstance(c, dict)),
        key=lambda c: (
            -(int(c["runs_on_record"]) if is_count(c.get("runs_on_record")) else 0),
            str(c.get("name") or ""),
        ),
    )
    rows = "".join(evidence_row(c, notes) for c in cases)
    if not rows:
        rows = '<tr><td colspan="3"><span class="cap">no cases on record yet</span></td></tr>'
    return f"""
  <h2 id="nightly">Evidence on record</h2>
  <div class="sub">Recorded task appearances per case (all collected runs, infra included), against the {SCREENING_WINDOW}-run yardstick · history depth, not admission progress — the screening window fills only from main-branch runs in the baseline store (bench/baselines/README.md) · annotations from case-notes.yaml</div>
  <div class="card"><table id="evidence">
    <thead><tr><th>Case</th><th>Evidence collected</th><th>In presubmit</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>"""


RELEASES_HTML = """
  <h2 id="release">Releases</h2>
  <div class="empty">
    <span class="banner">⏳ No RC in the gate window</span>
    <p style="margin-top:12px">When the next RC cuts, the four-gate checklist renders here automatically: E2E matrix · audit-machinery canary on the RC image · eval non-inferiority · operator sign-off.</p>
  </div>"""


HERO_LEDE = (
    "Every pull request runs the agent against a real seeded fleet. Exact checks "
    "gate; judged scores are recorded, never blocking; infrastructure failures "
    "never count against a PR."
)


HERO_HTML = f"""
  <section style="margin-top:30px">
    <span class="eyebrow">Agent evaluation · presubmit</span>
    <h1>Is the agent getting better or worse?</h1>
    <div class="lede">{HERO_LEDE}</div>
  </section>"""


def foot_html(data: dict) -> str:
    generated = parse_iso(data.get("generated_at"))
    generated_text = f"{generated:%Y-%m-%d %H:%M} UTC" if generated else "unknown time"
    runs = len(data.get("runs") or [])
    threshold = fmt(RUN_EVENT_FAIL_FRACTION * 100)
    return (
        f'<div class="foot" id="foot">Every number on this page is computed from '
        f"<code>data.json</code> (source: {esc(str(data.get('source') or '?'))}, "
        f"generated {generated_text}, {runs} run{'s' if runs != 1 else ''} on record). "
        f"Run-level event rule: a run where ≥{threshold}% of its graded tasks failed is "
        f"charged to the run, not the cases. Event markers and catch counts come from "
        f"<code>events.yaml</code>; row annotations and badges from "
        f"<code>case-notes.yaml</code>. All three are advisory only.</div>"
    )


EMPTY_STATE_HTML = """
  <section style="margin-top:30px" id="empty-state">
    <span class="eyebrow">Agent evaluation · presubmit</span>
    <h1>Is the agent getting better or worse?</h1>
    <div class="empty" style="margin-top:24px">
      <span class="banner">⏳ No evaluation data yet</span>
      <p style="margin-top:12px">No runs are on record in <code>data.json</code>. Once the collector publishes its first run, the band tiles, the day trend, the case × run matrix, the failure signatures and the evidence table all render from that file automatically — nothing else feeds this page.</p>
    </div>
  </section>"""


def app_html(data: dict, notes: dict, events: dict) -> str:
    if not (data.get("runs") or data.get("cases")):
        return EMPTY_STATE_HTML
    return (
        HERO_HTML
        + band_agent_html(data, events)
        + band_gate_html(data, notes, events)
        + evidence_html(data, notes)
        + RELEASES_HTML
        + foot_html(data)
    )


def meta_html(data: dict) -> str:
    runs = sorted_runs(data)
    if not runs:
        return "no runs on record"
    sha = str(runs[-1].get("head_sha") or "")[:7]
    return f"head {esc(sha)}" if sha else "head unknown"


def freshness_html(data: dict) -> str:
    generated = parse_iso(data.get("generated_at"))
    return f"updated {generated:%H:%M} UTC" if generated else "updated —"


def bootstrap_json(value) -> str:
    """JSON safe to inline in a <script> block: every '<' is emitted as the
    JSON escape \\u003c. Escaping only '</' is not enough -- the HTML
    tokenizer leaves script-data state on '<!--' too, and '<!--<script'
    puts it in the double-escaped state where the block's own '</script>'
    no longer closes it, so a hostile data string would silently disable
    the whole live read side."""
    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c")


def render_page(data: dict, notes: dict, events: dict) -> str:
    page = TEMPLATE.read_text()
    values = {
        "__META__": meta_html(data),
        "__FRESHNESS__": freshness_html(data),
        "__APP__": app_html(data, notes, events),
        # The live read side: the template's script re-renders from this
        # baked copy on load, then polls data.json every 60s.
        "__DATA_JSON__": bootstrap_json(data),
        "__NOTES_JSON__": bootstrap_json(notes),
        "__EVENTS_JSON__": bootstrap_json(events),
    }
    for token in values:
        if token not in page:
            raise SystemExit(f"ERROR: template is missing the {token} marker")
    # One pass over the template only: substituted values are never
    # re-scanned, so data that happens to contain a marker string (a case
    # *named* __DATA_JSON__, say) stays inert text instead of expanding
    # into the raw JSON bootstrap inside the page body.
    return re.sub(
        "|".join(re.escape(token) for token in values),
        lambda match: values[match.group(0)],
        page,
    )


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, help="collector data.json")
    parser.add_argument("--out-dir", required=True, help="directory to write into")
    parser.add_argument(
        "--notes",
        default=str(DEFAULT_NOTES),
        help="case-notes.yaml (optional annotations; absent file is fine)",
    )
    parser.add_argument(
        "--events",
        default=str(DEFAULT_EVENTS),
        help="events.yaml (optional markers and catch counts; absent file is fine)",
    )
    args = parser.parse_args(argv)

    data = load_data(pathlib.Path(args.data))
    notes = load_notes(pathlib.Path(args.notes))
    events = load_events(pathlib.Path(args.events))

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_page(data, notes, events))
    shutil.copyfile(args.data, out_dir / "data.json")
    print(f"wrote {out_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
