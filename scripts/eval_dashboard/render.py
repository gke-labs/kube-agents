#!/usr/bin/env python3
"""Render the eval dashboard from the collector's data.json.

Usage::

    python3 scripts/eval_dashboard/render.py --data data.json --out-dir out/

writes ``out/index.html`` (from ``template/index.html.tmpl``) and copies the
data file alongside it, so the published directory is self-contained.

Two rules shape everything here:

* **Computed-only.** Every figure on the page is derived from data.json --
  no hand-typed numbers can go stale in a template. The one optional extra
  input is ``case-notes.yaml`` (``--notes``): human one-line annotations and
  issue links per case. An absent notes file, or a case with no entry, is
  simply a row without a note -- never an error.
* **INFRA is not failure.** A task whose result is ``infra`` renders with its
  own pill and history-dot color and is excluded from pass-fraction math,
  matching the suite's policy that infrastructure failures never count
  against a PR.

The reader contract is schema_version 1 of the collector's data.json.
Optional fields may be absent and unknown additive fields are ignored, so
this renderer and the collector can ship independently.

The rendered page is also live: render.py bakes the data and notes into the
template, whose script re-renders in place from a fresh ``data.json`` fetch
every 60 seconds and keeps a freshness badge honest (see the template's
"Live read side" comment). The Python fragment builders here and the JS
mirrors there are intentionally parallel -- change them together.

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
import statistics
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE / "template" / "index.html.tmpl"
DEFAULT_NOTES = HERE / "case-notes.yaml"
ISSUE_URL = "https://github.com/gke-labs/kube-agents/issues"
ISSUE_RE = re.compile(r"^#(\d+)$")

# Judge scores are advisory; the tick every score bar carries sits here.
JUDGE_THRESHOLD = 0.8
# The 20-run yardstick the evidence bars are drawn against, borrowed from
# the screening window in docs/designs/testing-strategy.md. The bars show
# recorded history depth only -- admission to the gate is measured by the
# baseline store, which fills from main-branch runs alone
# (bench/baselines/README.md); the presubmit runs collected here never
# advance it, so a full bar is not admission.
SCREENING_WINDOW = 20
# Trend charts stay readable; older runs fall off the left edge.
MAX_TREND_POINTS = 10

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

RESULT_PILLS = {
    "pass": '<span class="pill p-pass">PASSED</span>',
    "fail": '<span class="pill p-fail">FAILED</span>',
    # Deliberately its own pill: INFRA is never rendered as a failure.
    "infra": '<span class="pill p-infra">INFRA</span>',
    "pend": '<span class="pill p-pend">IN BUILD</span>',
}

HIST_CLASSES = {"pass": "h-pass", "fail": "h-fail", "infra": "h-infra", "na": "h-na"}


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
    deadline-truncated build parses to zero tasks; anchoring the header
    tiles or the suite table to one would render vacuous 0/0 states
    ("0 / 0 passed · all passed" on a run that measured nothing). The
    trend charts and the header sha deliberately stay on sorted_runs."""
    return [r for r in sorted_runs(data) if run_tasks(r)]


def counted_results(run: dict | None) -> tuple[int, int, int]:
    """(passed, failed, infra) for one run. Only pass+fail gate."""
    passed = failed = infra = 0
    for task in run_tasks(run):
        result = str(task.get("result", "")).lower()
        if result == "pass":
            passed += 1
        elif result == "fail":
            failed += 1
        elif result == "infra":
            infra += 1
    return passed, failed, infra


def pass_fraction(run: dict) -> float | None:
    passed, failed, _ = counted_results(run)
    total = passed + failed
    return passed / total if total else None


def median_task_minutes(run: dict | None) -> float | None:
    durations = [
        t["duration_s"]
        for t in run_tasks(run)
        if isinstance(t.get("duration_s"), (int, float))
    ]
    return statistics.median(durations) / 60 if durations else None


def latest_task_for(case_name: str, run: dict | None) -> dict | None:
    for task in run_tasks(run):
        if task.get("name") == case_name:
            return task
    return None


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


def run_label(run: dict, index: int) -> str:
    if run.get("pr") is not None:
        return f"#{run['pr']}"
    build_id = str(run.get("build_id") or "")
    return build_id[:6] if build_id else f"run {index + 1}"


# --------------------------------------------------------------------------
# case-notes.yaml (optional flavor; never an error)


def load_notes(path: pathlib.Path | None) -> dict[str, dict]:
    """``{case: {"note": str|None, "issues": [str, ...]}}``. Absent file,
    empty file, or malformed entry all degrade to "no note"."""
    if path is None or not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}
    notes = {}
    for name, entry in (raw.get("notes") or {}).items():
        if isinstance(entry, str):
            entry = {"note": entry}
        if not isinstance(entry, dict):
            continue
        note = entry.get("note")
        issues = [str(i) for i in entry.get("issues") or []]
        if note or issues:
            notes[str(name)] = {"note": note, "issues": issues}
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


def ratio_chip(
    current: float | None,
    previous: float | None,
    unit: str,
    have_previous_run: bool = False,
) -> str:
    """Cheaper/slower chip vs the previous run; flat when nothing moved.
    "first run" only when there is genuinely no prior run -- a prior run
    that just failed to report this metric gets no chip at all."""
    if current is None or previous is None or not previous or not current:
        if previous is None and not have_previous_run:
            return delta_chip("flat", "first run")
        return ""
    if previous / current >= 1.5:
        return delta_chip("up", f"▲ {fmt(previous / current, 1)}× cheaper")
    if current / previous >= 1.5:
        return delta_chip("flat", f"▼ {fmt(current / previous, 1)}× slower")
    return delta_chip("flat", f"≈ prev {fmt(previous)}{unit}")


def tiles_html(data: dict) -> str:
    runs = measured_runs(data)
    latest = runs[-1] if runs else None
    previous = runs[-2] if len(runs) > 1 else None
    tiles = []

    # Latest run
    if latest:
        passed, failed, infra = counted_results(latest)
        counted = passed + failed
        if failed:
            chip = delta_chip("flat", f"{failed} failed")
        elif infra:
            chip = delta_chip("flat", f"{infra} infra")
        elif passed:
            chip = delta_chip("up", "all passed")
        else:
            # Unreachable for a measured run, but "all passed" must never
            # be claimed by a run that measured nothing.
            chip = ""
        build_id = str(latest.get("build_id") or "?")
        detail = f"build {build_id[:8]}… · {latest.get('project') or '?'}"
        tiles.append(
            tile(
                "Latest run",
                f"{passed}<small>/ {counted} passed</small>",
                chip,
                detail,
            )
        )
    else:
        detail = "no measured runs yet" if sorted_runs(data) else "no runs on record"
        tiles.append(tile("Latest run", "—", "", detail))

    # Domain coverage
    coverage = data.get("coverage") or {}
    covered, total = coverage.get("domains_covered"), coverage.get("domains_total")
    if is_count(covered) and is_count(total):
        covered, total = int(covered), int(total)
        uncovered = [str(d) for d in coverage.get("uncovered") or []]
        chip = (
            delta_chip("up", "all covered")
            if covered >= total
            else delta_chip("flat", f"{total - covered} open")
        )
        detail = (
            f"uncovered: {', '.join(uncovered)}" if uncovered else "all domains covered"
        )
        tiles.append(tile("Domain coverage", f"{covered}<small>/ {total}</small>", chip, detail))
    else:
        tiles.append(tile("Domain coverage", "—", "", "not reported"))

    # Median case cost
    med = median_task_minutes(latest)
    if med is not None:
        chip = ratio_chip(med, median_task_minutes(previous), "min", previous is not None)
        detail = f"median across {len(run_tasks(latest))} cases · latest run"
        tiles.append(tile("Median case cost", f"{fmt(med, 1)}<small>min</small>", chip, detail))
    else:
        tiles.append(tile("Median case cost", "—", "", "no task durations yet"))

    # Wall clock
    duration = latest.get("duration_s") if latest else None
    if isinstance(duration, (int, float)):
        prev_duration = previous.get("duration_s") if previous else None
        prev_min = prev_duration / 60 if isinstance(prev_duration, (int, float)) else None
        chip = ratio_chip(duration / 60, prev_min, "min", previous is not None)
        finished = parse_iso(latest.get("finished"))
        detail = (
            f"finished {finished:%Y-%m-%d %H:%M} UTC" if finished else "whole-run wall clock"
        )
        tiles.append(tile("Wall clock", f"{fmt(duration / 60)}<small>min</small>", chip, detail))
    else:
        tiles.append(tile("Wall clock", "—", "", "not reported"))

    return "".join(tiles)


def score_html(judge) -> str:
    if not isinstance(judge, (int, float)):
        return '<span class="cap">—</span>'
    judge = min(max(float(judge), 0.0), 1.0)
    return (
        f'<div class="score"><div class="bar">'
        f'<div class="fill" style="width:{fmt(judge * 100)}%"></div>'
        f'<div class="thr" style="left:{JUDGE_THRESHOLD * 100:.0f}%"></div></div>'
        f'<span class="val">{fmt(judge, 1)}</span></div>'
    )


def case_row(case: dict, latest_run: dict | None, notes: dict) -> str:
    name = str(case.get("name") or "?")
    task = latest_task_for(name, latest_run)

    if task:
        status = str(task.get("result", "")).lower()
        if status not in ("pass", "fail", "infra"):
            status = "pend"
    else:
        measured = [
            h for h in case.get("last3") or [] if str(h).lower() in ("pass", "fail", "infra")
        ]
        status = str(measured[-1]).lower() if measured else "pend"

    last3 = [str(h).lower() for h in (case.get("last3") or [])][-3:]
    last3 = ["na"] * (3 - len(last3)) + [h if h in HIST_CLASSES else "na" for h in last3]
    hist = "".join(f'<i class="{HIST_CLASSES[h]}" title="{h}"></i>' for h in last3)

    judge = task.get("outcome_validity") if task else None
    if judge is None:
        ov_history = [
            h for h in case.get("ov_history") or [] if isinstance(h.get("value"), (int, float))
        ]
        judge = ov_history[-1]["value"] if ov_history else None

    duration = task.get("duration_s") if task else None
    if not isinstance(duration, (int, float)):
        duration = (case.get("durations") or {}).get("med")
    duration_text = f"{fmt(duration)}s" if isinstance(duration, (int, float)) else "—"

    domain = case.get("domain")
    domain_html = f'<span class="tdom">{esc(str(domain))}</span>' if domain else ""

    return (
        f"<tr><td>{RESULT_PILLS[status]}</td>"
        f'<td><div class="tname">{esc(name)}</div>{note_html(notes.get(name))}</td>'
        f"<td>{domain_html}</td>"
        f'<td><span class="hist">{hist}</span></td>'
        f"<td>{score_html(judge)}</td>"
        f'<td class="num">{duration_text}</td></tr>'
    )


def suite_html(data: dict, notes: dict) -> str:
    runs = measured_runs(data)
    latest = runs[-1] if runs else None
    rows = "".join(case_row(c, latest, notes) for c in data.get("cases") or [])
    if not rows:
        rows = '<tr><td colspan="6"><span class="cap">no cases on record yet</span></td></tr>'
    return f"""
  <h2 id="suite">Test suite</h2>
  <div class="sub">Latest measured result per case · <b>Result</b> is the gating exact-check verdict · <b>Judge</b> is advisory (threshold tick at {JUDGE_THRESHOLD}) · history dots = last 3 runs</div>
  <div class="card"><table id="cases">
    <thead><tr><th>Result</th><th>Test case</th><th>Domain</th><th>History</th><th>Judge (advisory)</th><th style="text-align:right">Duration</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <div class="legend">
    <span><i class="h-pass"></i>pass</span><span><i class="h-fail"></i>fail</span>
    <span><i class="h-infra"></i>INFRA — excluded from verdict</span><span><i class="h-na"></i>not in run</span>
  </div>"""


def chart_svg(points: list[tuple[str, float]], threshold: float | None = None) -> str:
    """The spec page's line chart, one to one: dots carry ``data-l`` labels
    the shared tooltip listener reads."""
    if len(points) < 2:
        return ""
    width, height, px, py, ymax = 480, 170, 14, 16, 1.0

    def xs(i: int) -> float:
        return px + i * (width - 2 * px) / (len(points) - 1)

    def ys(v: float) -> float:
        return height - py - (v / ymax) * (height - 2 * py)

    poly = " ".join(f"{fmt(xs(i), 1)},{fmt(ys(v), 1)}" for i, (_, v) in enumerate(points))
    dots = "".join(
        f'<circle cx="{fmt(xs(i), 1)}" cy="{fmt(ys(v), 1)}" r="5" fill="var(--accent)" '
        f'stroke="var(--surface-1)" stroke-width="2" data-l="{esc(label)} · {fmt(v, 2)}"/>'
        for i, (label, v) in enumerate(points)
    )
    xlab = "".join(
        f'<text x="{fmt(xs(i), 1)}" y="{height - 2}" text-anchor="middle" font-size="10.5" '
        f'font-weight="600" fill="var(--text-muted)">{esc(label)}</text>'
        for i, (label, _) in enumerate(points)
    )
    thr_line = (
        f'<line x1="{px}" y1="{fmt(ys(threshold), 1)}" x2="{width - px}" y2="{fmt(ys(threshold), 1)}" '
        f'stroke="var(--line-2)" stroke-dasharray="3 4"/>'
        if threshold is not None
        else ""
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
        f'{thr_line}<line x1="{px}" y1="{fmt(ys(0), 1)}" x2="{width - px}" y2="{fmt(ys(0), 1)}" stroke="var(--line)"/>'
        f'<polyline points="{poly}" fill="none" stroke="var(--accent)" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>{dots}{xlab}</svg>'
    )


def rate_points(data: dict) -> list[tuple[str, float]]:
    points = []
    for index, run in enumerate(sorted_runs(data)):
        fraction = pass_fraction(run)
        if fraction is not None:
            points.append((run_label(run, index), fraction))
    return points[-MAX_TREND_POINTS:]


def judge_trend_case(data: dict) -> dict | None:
    """The case with the deepest judge history -- the one with a story."""
    candidates = [
        c for c in data.get("cases") or [] if len(c.get("ov_history") or []) >= 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: len(c["ov_history"]))


def trends_html(data: dict) -> str:
    # No threshold line here: 0.8 is the judge threshold, and drawing it on
    # the pass-fraction chart would present it as a pass-rate target.
    rate = chart_svg(rate_points(data))
    rate = rate or '<div class="cap" style="margin-top:14px">not enough runs yet</div>'

    # ov_history entries carry build ids; label them with the matching run's
    # PR when the run is on record -- a raw build-id fragment tells a reader
    # nothing.
    labels = {
        str(run.get("build_id")): run_label(run, index)
        for index, run in enumerate(sorted_runs(data))
    }
    judge_case = judge_trend_case(data)
    if judge_case:
        points = [
            (
                labels.get(str(h.get("build_id")), str(h.get("build_id") or "?")[:6]),
                float(h["value"]),
            )
            for h in judge_case["ov_history"]
            if isinstance(h.get("value"), (int, float))
        ][-MAX_TREND_POINTS:]
        judge_title = f"{judge_case.get('name', '?')} · judge score"
        judge = chart_svg(points, threshold=JUDGE_THRESHOLD)
    else:
        judge_title = "judge score"
        judge = ""
    judge = judge or '<div class="cap" style="margin-top:14px">no judge history yet</div>'

    return f"""
  <h2 id="trends">Trends</h2>
  <div class="split">
    <div class="card pad chartcard">
      <div class="t">Suite pass fraction, per run</div>
      <div class="s">pass / (pass + fail) — INFRA excluded</div>
      <div id="ratechart">{rate}</div>
    </div>
    <div class="card pad chartcard">
      <div class="t">{esc(judge_title)}</div>
      <div class="s">advisory · threshold tick at {JUDGE_THRESHOLD}</div>
      <div id="judgechart">{judge}</div>
    </div>
  </div>"""


def evidence_row(case: dict) -> str:
    name = str(case.get("name") or "?")
    have = case.get("runs_on_record")
    have = int(have) if isinstance(have, (int, float)) else 0
    width = min(100.0, 100.0 * have / SCREENING_WINDOW)
    rate = case.get("pass_rate")
    rate_text = f"{fmt(rate * 100)}%" if isinstance(rate, (int, float)) else "—"
    presubmit = (
        '<span class="pill p-pass">IN PRESUBMIT</span>'
        if case.get("active")
        else '<span class="pill p-fix">NOT IN PRESUBMIT</span>'
    )
    return (
        f'<tr><td style="font-weight:650">{esc(name)}</td>'
        f'<td><div style="display:flex;align-items:center;gap:10px">'
        f'<div class="prog"><i style="width:{fmt(width)}%"></i></div>'
        f'<span class="cap">{have} of {SCREENING_WINDOW}</span></div></td>'
        f'<td class="num" style="text-align:left">{rate_text}</td>'
        f"<td>{presubmit}</td></tr>"
    )


def evidence_html(data: dict) -> str:
    cases = sorted(
        data.get("cases") or [],
        key=lambda c: (-(c.get("runs_on_record") or 0), str(c.get("name") or "")),
    )
    rows = "".join(evidence_row(c) for c in cases)
    if not rows:
        rows = '<tr><td colspan="4"><span class="cap">no cases on record yet</span></td></tr>'
    return f"""
  <h2 id="nightly">Evidence on record</h2>
  <div class="sub">Recorded task appearances per case (all collected runs, infra included), against the {SCREENING_WINDOW}-run yardstick · history depth, not admission progress — the screening window fills only from main-branch runs in the baseline store (bench/baselines/README.md)</div>
  <div class="card"><table id="evidence">
    <thead><tr><th>Case</th><th>Evidence collected</th><th>Pass rate</th><th>In presubmit</th></tr></thead>
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


def hero_html(data: dict) -> str:
    return f"""
  <section style="margin-top:30px">
    <span class="eyebrow">Agent evaluation · presubmit</span>
    <h1>Is the agent getting better or worse?</h1>
    <div class="lede">{HERO_LEDE}</div>
    <div class="tiles" id="tiles">{tiles_html(data)}</div>
  </section>"""


def foot_html(data: dict) -> str:
    generated = parse_iso(data.get("generated_at"))
    generated_text = f"{generated:%Y-%m-%d %H:%M} UTC" if generated else "unknown time"
    runs = len(data.get("runs") or [])
    return (
        f'<div class="foot" id="foot">Every number on this page is computed from '
        f"<code>data.json</code> (source: {esc(str(data.get('source', '?')))}, "
        f"generated {generated_text}, {runs} run{'s' if runs != 1 else ''} on record). "
        f"Row annotations come from <code>case-notes.yaml</code> and are advisory only.</div>"
    )


EMPTY_STATE_HTML = """
  <section style="margin-top:30px" id="empty-state">
    <span class="eyebrow">Agent evaluation · presubmit</span>
    <h1>Is the agent getting better or worse?</h1>
    <div class="empty" style="margin-top:24px">
      <span class="banner">⏳ No evaluation data yet</span>
      <p style="margin-top:12px">No runs are on record in <code>data.json</code>. Once the collector publishes its first run, the header tiles, the per-case suite table, the trend charts and the nightly evidence all render from that file automatically — nothing else feeds this page.</p>
    </div>
  </section>"""


def app_html(data: dict, notes: dict) -> str:
    if not (data.get("runs") or data.get("cases")):
        return EMPTY_STATE_HTML
    return (
        hero_html(data)
        + suite_html(data, notes)
        + trends_html(data)
        + evidence_html(data)
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


def render_page(data: dict, notes: dict) -> str:
    page = TEMPLATE.read_text()
    values = {
        "__META__": meta_html(data),
        "__FRESHNESS__": freshness_html(data),
        "__APP__": app_html(data, notes),
        # The live read side: the template's script re-renders from this
        # baked copy on load, then polls data.json every 60s.
        "__DATA_JSON__": bootstrap_json(data),
        "__NOTES_JSON__": bootstrap_json(notes),
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
    args = parser.parse_args(argv)

    data = load_data(pathlib.Path(args.data))
    notes = load_notes(pathlib.Path(args.notes))

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_page(data, notes))
    shutil.copyfile(args.data, out_dir / "data.json")
    print(f"wrote {out_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
