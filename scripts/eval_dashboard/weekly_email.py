#!/usr/bin/env python3
"""The weekly health email: one week of the nightly tier, for leadership and
the team, as an email-safe HTML page -- rendered only when every integrity
check below passes.

It reads the same inputs the dashboard does -- ``data.json`` (the nightly
runs, each case's domain) and ``store.json`` (the evidence store's records,
which carry the model each night ran) -- plus ``docs/designs/domains.yaml``
(each area's name and plain-language ``meaning:``) and GitHub (the open
``nightly-red`` issues per ``domain:<slug>`` label). Every number in the
email is recomputed from those on each run; none is typed by hand.

**The week** is the seven nightly dates ending on ``--week-end`` (by
default the Sunday before today, UTC), and **last week** the seven before
it. A night is dated as ``nightly.night_date`` dates it.

**A counted night** ran to its verdict: none of its parts was cut off at
the job deadline (``nightly.run_truncated``), it has a main part, and it
is not a run-level event (``classify.is_run_event``: nearly every graded
case failed). Two counted nights on one date are one night: per case the
newer verdict stands, as ``nightly_bugs.graded_nights`` reads a re-run. A
repetition the classifier calls infrastructure is never graded
(``classify.rep_kind``).

**The numbers** are graded repetitions: a pass rate is passes over graded
repetitions, over the scenarios that were graded in both weeks -- the same
set both weeks, and the count the email shows. A scenario graded in one
week only is left out of every number and counted in the closing note.

**The integrity checks** (#3440, "Integrity checks") run over the computed
week and the rendered page; any failure refuses the render and prints every
reason:

1. same cases: both weeks' numbers are over one set of scenarios, and the
   counts shown add up to it;
2. truncated nights excluded: no counted night was cut off;
3. not enough data: with fewer than MIN_COUNTED_NIGHTS counted nights in
   either week, nothing is called up, down or steady;
4. few scenarios: an area with fewer than MIN_TREND_SCENARIOS scenarios
   gets no trend call;
5. claims follow rules: every sentence comes from CLAIM_TEMPLATES and
   meets its rule (``claim_failures``);
6. needs attention is computed: below NEEDS_ATTENTION_BELOW_PCT, or down
   NEEDS_ATTENTION_DROP_PTS points or more;
7. bugs exist: every scenario the bug filer files for right now (failed
   every graded repetition on its last ``nightly_bugs.STREAK_NIGHTS`` graded
   nights) has a ``nightly-red`` issue tracking it, open or closed and still
   tracking as the filer decides (``owed_bugs``), and every open-bug count
   shown was read from GitHub;
8. links are well-formed: every ``href`` and ``src`` is an https URL with
   nothing a mail client would cut it at;
9. domains match: every domain in the data is a row in domains.yaml, and
   every row has data that week unless it says ``new: true``;
10. the footer is read from run metadata: the model and dates it names are
    the counted nights' own, and every counted night has a model on record.

The rendered page goes next to ``data.json`` as ``health-email.html`` (the
workflow's render step). TODO(#3440): sending it -- to whom, from which
account, and the one chat line linking to it -- is not built; it needs a
mail route and permissions a maintainer decides. A refused render then
posts its reasons to the CI health space instead of sending.
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import pathlib
import re
import statistics
import sys
import urllib.parse

try:
    import yaml
except ImportError:  # the workflow installs pyyaml; main() says so if absent
    yaml = None

try:
    import case_owner

    from eval_dashboard import classify, ghcli, nightly, nightly_bugs, nightly_infra
    from eval_dashboard.post_health import DASHBOARD_URL
except ImportError:  # run as a script: scripts/eval_dashboard/weekly_email.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import case_owner

    from eval_dashboard import classify, ghcli, nightly, nightly_bugs, nightly_infra
    from eval_dashboard.post_health import DASHBOARD_URL

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# The artifact's name beside data.json in the bucket.
OUT_NAME = "health-email.html"

# The week: seven nightly dates, and the week before it.
WEEK_DAYS = 7
# Fewer counted nights than this in either week and no change is called
# (#3440: "fewer than four good nights").
MIN_COUNTED_NIGHTS = 4
# An area with fewer scenarios than this is "few scenarios": no trend call.
MIN_TREND_SCENARIOS = 3
# A change smaller than this many points is "steady"; this many or more is
# up or down ("down N pts" only for a drop of 3+ points).
TREND_PTS = 3
# "In great shape": at least this rate, and not down.
GREAT_SHAPE_PCT = 85
# "Less than half the time": below this rate.
LESS_THAN_HALF_PCT = 50
# Needs attention: below this rate, or down this many points or more.
NEEDS_ATTENTION_BELOW_PCT = 65
NEEDS_ATTENTION_DROP_PTS = 5
PERCENT = 100
SECONDS_PER_MINUTE = 60

TREND_UP = "up"
TREND_DOWN = "down"
TREND_STEADY = "steady"

# The labels the bug filer puts on a domain bug (nightly_bugs.py).
BUG_LABEL = nightly_bugs.LABEL
DOMAIN_LABEL = nightly_bugs.DOMAIN_LABEL
GRAPHQL_PATH = "graphql"
GRAPHQL_METHOD = "POST"
# One page of the issues carrying the bug label in the given states, with
# what the bug filer reads of each (``nightly_bugs.tracking_issue``,
# ``still_tracks``); ``since`` bounds the closed ones by their last update,
# as the filer's own read of closed issues does.
BUGS_QUERY = """query($owner: String!, $name: String!, $label: String!, $states: [IssueState!], $since: DateTime, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(labels: [$label], states: $states, filterBy: {since: $since}, first: 100, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { number title body createdAt closedAt stateReason labels(first: 50) { nodes { name } } }
    }
  }
}"""
STATES_OPEN = ["OPEN"]
STATES_CLOSED = ["CLOSED"]
ISSUES_SEARCH_URL = "https://github.com/{repo}/issues?q={query}"
BUG_QUERY = "is:issue is:open label:{label} label:{domain_label}"
# The robot, from the published docs site (docs/site/public/agent-icon.png).
DOCS_SITE_URL = "https://gke-labs.github.io/kube-agents"
ROBOT_URL = f"{DOCS_SITE_URL}/agent-icon.png"
# A link a mail client keeps whole: https, a host, and only characters RFC
# 3986 allows in a URL, with every % starting an escape.
LINK_ATTR_RE = re.compile(r'\b(?:href|src)="([^"]*)"')
URL_CHARS_RE = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
BAD_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
HTTPS = "https"
# domains.yaml names carry a parenthetical gloss ("Capacity
# (stockout-prevention)"); the email shows the name before it.
NAME_GLOSS_RE = re.compile(r"\s*\(.*\)\s*$")
# A domains.yaml row with this key set true may have no data yet.
NEW_DOMAIN_KEY = "new"
# The evidence store record's model: its version key's setup_id
# (SCHEMA.md, store.json).
STORE_KEY = "key"
STORE_SETUP_KEY = "setup_id"
STORE_BUILD_KEY = "build"

DATE_SHORT = "{day:%b} {day.day}"
DATE_RANGE_SAME_MONTH = "{start:%b} {start.day}–{end.day}"
DATE_RANGE = "{start:%b} {start.day} – {end:%b} {end.day}"
TIME_FORMAT = "%Y-%m-%d %H:%M UTC"

# ---------------------------------------------------------------------------
# The text. Every sentence of the intro is a claim built from one of these
# templates, and claim_failures holds each to its rule.

CLAIM_LEAD = "lead"
CLAIM_GREAT = "great_shape"
CLAIM_HALF = "less_than_half"
CLAIM_DOWN = "down"
CLAIM_NOT_ENOUGH = "not_enough_data"
CLAIM_MODEL_CHANGE = "model_change"
CLAIM_TEMPLATES = {
    CLAIM_LEAD: "Here is how kube-agents did in the nightly tests for {range}: {passed} of {graded} test runs passed "
    "({pct}%) across {scenarios} scenarios, counting {nights} of the week's {dates} nights (the note at the end says why any is left out).",
    CLAIM_GREAT: f"In great shape ({GREAT_SHAPE_PCT}% or more, and not down): {{names}}.",
    CLAIM_HALF: "Passed less than half the time: {names}.",
    CLAIM_DOWN: "Down from last week: {items}.",
    CLAIM_NOT_ENOUGH: "Not enough data this week to show a trend: {why}.",
    CLAIM_MODEL_CHANGE: "This week ran {model}; last week ran {prev_model}, so the change compares two models.",
}
DOWN_ITEM = "{name} (down {pts} pts)"
NOT_ENOUGH_WHY = "{prev} of last week's and {this} of this week's nights counted, and a trend needs {need} in each"
NAMES_JOIN = ", "

TITLE = "kube-agents weekly health, week of {day}"
HEADER_KICKER = "kube-agents &middot; weekly health"
HEADER_TITLE = "Week of {day}"
TILE_RATE_LABEL = "of test runs passed"
TILE_SCENARIOS_LABEL = "scenarios compared"
TILE_SCENARIOS_SUB = "graded both weeks"
TILE_TIME_LABEL = "typical task time"
TILE_TIME_VALUE = "{minutes} min"
TILE_TIME_SUB = "middle of {count} timed runs"
TILE_TIME_NONE = "n/a"
TILE_TIME_NONE_SUB = "no timing on record"
CHANGE_UP = "&#9650; {pts} pts vs last week"
CHANGE_DOWN = "&#9660; {pts} pts vs last week"
CHANGE_STEADY = "&#8594; steady vs last week"
CHANGE_NONE = "not enough data this week"
ROW_UP = "&#9650;&nbsp;{pts}&nbsp;pts"
ROW_DOWN = "&#9660;&nbsp;{pts}&nbsp;pts"
ROW_STEADY = "&#8594;&nbsp;steady"
ROW_FEW = "few scenarios"
ROW_NO_TREND = "no trend this week"
ATTENTION_TITLE = "Needs attention"
ATTENTION_INTRO = (f"Areas below {NEEDS_ATTENTION_BELOW_PCT}% or down {NEEDS_ATTENTION_DROP_PTS} points or more. "
                   "Each links to its open nightly bugs.")
ATTENTION_NONE = (f"Nothing this week: every area is at {NEEDS_ATTENTION_BELOW_PCT}% or more and none is down "
                  f"{NEEDS_ATTENTION_DROP_PTS} points or more.")
# A week with too few counted nights computes no change, so it claims none.
ATTENTION_NONE_NO_TREND = f"Nothing this week: every area is at {NEEDS_ATTENTION_BELOW_PCT}% or more; no trend this week."
ATTENTION_RATE = "{pct}%"
ATTENTION_DOWN = "{pct}%, down {pts}"
ATTENTION_FEW = "{pct}%, few scenarios"
OPEN_BUGS = "Open bugs ({count}) &rarr;"
BY_AREA_TITLE = "By area"
BY_AREA_SUB = "Pass rate this week, and change from last week"
SCENARIOS = "{count} scenarios"
SCENARIO = "1 scenario"
FIX_TITLE = "How we fix what fails"
FIX_TEXT = (f"When a scenario fails every run {nightly_bugs.STREAK_NIGHTS} nights in a row, a bug is filed automatically "
            "and triaged within a week: a product bug gets a fix and a date, a broken test gets fixed or marked as a "
            "known gap. Infrastructure problems are tracked separately and don&rsquo;t count against an area.")
DASHBOARD_BUTTON = "Open the eval dashboard"
HOW_TO_READ = ("How to read this: each area has a set of test scenarios that plant a real problem on our test fleet "
               "and check the agent&rsquo;s answer. Some scenarios are hard on purpose, so watch the trend, not the "
               "absolute number. Areas with few scenarios swing more. Compared on the {scenarios} scenarios graded "
               "both weeks{left_out}; {cut}.")
LEFT_OUT = " ({count} graded in only one week are left out)"
CUT_NONE = "no night was cut off by a time limit"
CUT_SOME = "these nights are left out: {days}"
CUT_DAY = "{day} ({why})"
FOOTER = "Data from the nightly runs of {range} &middot; model: {models} &middot; rendered {rendered}"
FOOTER_MODELS = "{this} (this week), {prev} (last week)"

# The inline styles, so a mail client that strips <style> renders the same.
FONT = "'Google Sans',Roboto,Helvetica,Arial,sans-serif"
INK = "#202124"
MUTED = "#5f6368"
FAINT = "#80868b"
RULE = "#e8eaed"
LINK = "#1a73e8"
GOOD = "#137333"
BAD = "#c5221f"
PAGE_BG = "#f1f3f4"
ATTENTION_BG = "#fef7e0"
NOTE_BG = "#f8f9fa"
BAR_BG = "#e8f0fe"
HEADER_BG = "#0b1736"
HEADER_GRADIENT = "linear-gradient(135deg,#0b1736 0%,#1a3a8f 100%)"
KICKER = "#8ab4f8"
CARD_WIDTH_PX = 640
BAR_WIDTH_PX = 160
BAR_MIN_PX = 2
BAR_CELL_PX = 170
ROWS_JOIN = "\n"
# Why a night is not counted, as the footer names it.
WHY_CUT = "cut off"
WHY_NO_MAIN = "no main part"
WHY_RUN_EVENT = "nearly every scenario failed"
WHY_NO_RUN = "no run on record"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title></head>
<body style="margin:0;padding:0;background:{page_bg};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{page_bg};"><tr><td align="center" style="padding:32px 12px;">
<table role="presentation" width="{width}" cellpadding="0" cellspacing="0" style="max-width:{width}px;width:100%;background:#ffffff;border-radius:16px;font-family:{font};">
{rows}
</table>
<div style="font-size:11px;color:{faint};padding-top:12px;font-family:Roboto,Arial,sans-serif;"><img src="{robot}" width="16" height="16" alt="" style="vertical-align:middle;border-radius:4px;border:0;">&nbsp; {footer}</div>
</td></tr></table></body></html>
"""
HEADER_ROW = """<tr><td style="padding:0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{bg}" style="background:{bg};background-image:{gradient};border-radius:16px 16px 0 0;">
<tr><td style="padding:28px 32px;vertical-align:middle;width:72px;"><img src="{robot}" width="64" height="64" alt="kube-agents robot" style="display:block;border:0;border-radius:16px;"></td>
<td style="padding:28px 32px 28px 0;vertical-align:middle;">
<div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:{kicker};font-weight:600;">{kicker_text}</div>
<div style="font-size:26px;font-weight:600;color:#ffffff;padding-top:4px;letter-spacing:-0.3px;">{title}</div>
</td></tr></table></td></tr>"""
INTRO_ROW = """<tr><td style="padding:24px 32px 0;"><div style="font-size:15px;line-height:22px;color:{muted};padding-top:8px;">{text}</div></td></tr>"""
TILES_ROW = """<tr><td style="padding:20px 26px 8px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
{tiles}
</tr></table></td></tr>"""
TILE = """<td style="width:33%;padding:0 6px;vertical-align:top;"><div style="border:1px solid {rule};border-radius:12px;padding:16px;">
<div style="font-size:28px;font-weight:600;letter-spacing:-0.5px;color:{ink};line-height:32px;">{value}</div>
<div style="font-size:13px;color:{ink};padding-top:4px;">{label}</div><div style="font-size:12px;color:{faint};padding-top:2px;">{sub}</div></div></td>"""
ATTENTION_ROW = """<tr><td style="padding:24px 32px 0;"><div style="background:{bg};border-radius:12px;padding:16px 18px;">
<div style="font-size:15px;font-weight:600;color:{ink};">{title}</div>
<div style="font-size:13px;color:{muted};padding:2px 0 6px;">{intro}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{items}</table></div></td></tr>"""
ATTENTION_ITEM = """<tr><td style="padding:8px 0;border-top:1px solid {rule};font-size:14px;color:{ink};font-weight:600;">{name}</td><td style="padding:8px 0;border-top:1px solid {rule};font-size:13px;color:{muted};">{state}</td><td style="padding:8px 0;border-top:1px solid {rule};font-size:13px;color:{ink};text-align:right;"><a href="{url}" style="color:{link};text-decoration:none;font-weight:600;">{bugs}</a></td></tr>"""
ATTENTION_EMPTY = """<tr><td style="padding:8px 0;border-top:1px solid {rule};font-size:13px;color:{muted};">{text}</td></tr>"""
AREA_ROW = """<tr><td style="padding:24px 32px 0;"><div style="font-size:17px;font-weight:600;color:{ink};">{title}</div>
<div style="font-size:13px;color:{faint};padding-top:2px;padding-bottom:6px;">{sub}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{items}</table></td></tr>"""
AREA_ITEM = """<tr><td style="padding:14px 0;border-top:1px solid {rule};vertical-align:top;">
<div style="font-size:15px;font-weight:600;color:{ink};">{name} <span style="font-size:12px;font-weight:400;color:{faint};">&middot; {scenarios}</span></div>
<div style="font-size:13px;line-height:18px;color:{muted};padding-top:2px;">{meaning}</div>
</td>
<td style="padding:14px 0 14px 16px;border-top:1px solid {rule};vertical-align:top;width:{bar_cell}px;">
<table role="presentation" cellpadding="0" cellspacing="0" style="width:{bar}px;background:{bar_bg};border-radius:4px;"><tr><td style="height:8px;width:{filled}px;background:{link};border-radius:4px;font-size:0;line-height:0;">&nbsp;</td><td style="font-size:0;line-height:0;">&nbsp;</td></tr></table>
<div style="font-size:13px;color:{muted};padding-top:6px;"><span style="font-size:15px;font-weight:600;color:{ink};">{pct}%</span>&nbsp;&nbsp;<span style="{change_style}">{change}</span></div></td></tr>"""
FIX_ROW = """<tr><td style="padding:20px 32px 0;"><div style="background:{bg};border-radius:12px;padding:16px 18px;">
<div style="font-size:14px;font-weight:600;color:{ink};">{title}</div>
<div style="font-size:13px;line-height:20px;color:{muted};padding-top:4px;">{text}</div>
</div></td></tr>"""
CLOSE_ROW = """<tr><td style="padding:20px 32px 32px;">
<a href="{dashboard}" style="display:inline-block;background:{link};color:#ffffff;text-decoration:none;font-size:14px;font-weight:600;padding:10px 18px;border-radius:20px;">{button}</a>
<div style="font-size:12px;line-height:18px;color:{faint};padding-top:16px;">{how}</div>
</td></tr>"""

# Why a render is refused, one per integrity check.
FAIL_NO_YAML = "pyyaml is not installed, so docs/designs/domains.yaml cannot be read"
FAIL_NO_NIGHTS = "no counted night in the week of {range}: nothing to report"
FAIL_NO_COMMON = "no scenario was graded in both weeks, so nothing can be compared"
FAIL_SAME_CASES = "same cases: {what}"
FAIL_TRUNCATED = "truncated nights: {day} is counted but was cut off at the deadline or is not a counted night"
FAIL_NOT_ENOUGH = "not enough data: {counted} and {prev} counted nights, yet a trend is called for {what}"
FAIL_FEW = "few scenarios: {name} has {count} scenarios, yet its change is called {trend}"
FAIL_CLAIM = "claims: {what}"
WHY_NOT_TEMPLATE = "{kind!r} is not a template"
WHY_NO_ROW = "{kind} names {slug}, which has no row"
WHY_GREAT = "'in great shape' for {name} at {pct}% ({trend})"
WHY_HALF = "'less than half the time' for {name} at {pct}%"
WHY_DOWN = "'down {pts} pts' for {name}: dropped {drop} pts over {count} scenarios"
WHY_NOT_ENOUGH_FALSE = "'not enough data' on a week with enough"
WHY_NOT_ENOUGH_MISSING = "the week has too few counted nights and the intro does not say 'not enough data'"
WHY_TREND = "{name}'s change {change} is called {trend}"
WHY_DOUBLE = "an area counts a scenario twice, or one not graded both weeks"
WHY_TILE = "the tile shows {shown} scenarios but {count} were compared"
WHY_NOT_COMMON = "the compared scenarios are not the ones graded in both weeks"
WHY_SUM = "the overall {key} count is not the sum over the compared scenarios"
NO_TREND = "no trend"
KEY_SEP = "_"
SPACE = " "
THE_WEEK = "the week"
FAIL_ATTENTION = "needs attention: computed {want}, rendered {got}"
FAIL_BUGS_UNREAD = "bugs: the nightly-red issues could not be read from GitHub"
FAIL_BUGS_ZERO = "bugs: {name} has {cases} failing every run on {nights}+ nights in a row and no nightly-red issue tracking it; the bug filer has not filed"
FAIL_BUG_COUNT = "bugs: {name} shows {shown} open bugs but GitHub has {count}"
FAIL_LINK = "links: {url!r} is not a well-formed https link"
FAIL_LINK_MISSING = "links: the open-bugs link for {name} is missing from the page"
FAIL_DOMAIN_UNKNOWN = "domains: {domain} (cases {cases}) is in the data but not in docs/designs/domains.yaml"
FAIL_DOMAIN_NO_DATA = "domains: {domain} is in docs/designs/domains.yaml but has no nightly data this week; mark it `new: true` if it has no cases yet"
FAIL_FOOTER_MODEL = "footer: {night} has no model in the evidence store (store.json), so the footer cannot name it"
FAIL_FOOTER = "footer: it says {got}, but run metadata says {want}"


def log(message: str) -> None:
    print(message, file=sys.stderr)


def pct_of(passed: int, graded: int) -> int:
    """Whole percent, half rounded up, so 84.5% is 85% as a reader expects."""
    return (passed * PERCENT * 2 + graded) // (graded * 2)


def short_date(day: datetime.date) -> str:
    return DATE_SHORT.format(day=day)


def date_range(start: datetime.date, end: datetime.date) -> str:
    if start.month == end.month and start.year == end.year:
        return DATE_RANGE_SAME_MONTH.format(start=start, end=end)
    return DATE_RANGE.format(start=start, end=end)


def default_week_end(today: datetime.date) -> datetime.date:
    """The Sunday before ``today`` (a Monday gives yesterday)."""
    return today - datetime.timedelta(days=today.isoweekday())


def short_name(name: str | None, slug: str) -> str:
    return NAME_GLOSS_RE.sub("", name or "") or slug


# ---------------------------------------------------------------------------
# inputs


def load_domains(repo_root: pathlib.Path) -> list[dict]:
    """domains.yaml's rows, in file order: ``{slug, name, meaning, new}``."""
    doc = yaml.safe_load((repo_root / case_owner.DOMAINS_PATH).read_text()) or {}
    rows = []
    for row in doc.get("domains") or []:
        if isinstance(row, dict) and isinstance(row.get("slug"), str):
            rows.append({"slug": row["slug"], "name": short_name(row.get("name"), row["slug"]),
                         "meaning": str(row.get("meaning") or ""), "new": row.get(NEW_DOMAIN_KEY) is True})
    return rows


def store_models(store: dict | None) -> dict[str, set[str]]:
    """``{build id: {setup_id, ...}}`` from the evidence store's records."""
    out: dict[str, set[str]] = {}
    for record in (store or {}).get("records") or []:
        if not isinstance(record, dict):
            continue
        build, key = record.get(STORE_BUILD_KEY), record.get(STORE_KEY)
        setup = key.get(STORE_SETUP_KEY) if isinstance(key, dict) else None
        if isinstance(build, str) and isinstance(setup, str) and setup:
            out.setdefault(build, set()).add(setup)
    return out


def read_issues(gh: ghcli.Gh, states: list[str], since: str | None, owner_repo: str) -> list[dict] | None:
    """The ``nightly-red`` issues in ``states`` in the REST shape the bug
    filer reads (``number, title, body, created_at, closed_at, state_reason,
    labels``), or None when GitHub could not be read. GraphQL, whose
    ``totalCount`` lets the read be checked: the REST issue list has
    answered an empty page for a label that has open issues."""
    owner, name = owner_repo.split("/", 1)
    out: list[dict] = []
    cursor = None
    while True:
        reply = gh.call(GRAPHQL_METHOD, GRAPHQL_PATH, {"query": BUGS_QUERY, "variables": {
            "owner": owner, "name": name, "label": BUG_LABEL, "states": states, "since": since, "after": cursor}})
        try:
            issues = reply["data"]["repository"]["issues"]
            total, nodes, page = issues["totalCount"], issues["nodes"], issues["pageInfo"]
        except (KeyError, TypeError):
            return None
        for node in nodes:
            labels = [{"name": label.get("name")} for label in (node.get("labels") or {}).get("nodes") or [] if isinstance(label, dict)]
            out.append({"number": node.get("number"), "title": node.get("title"), "body": node.get("body"),
                        "created_at": node.get("createdAt"), "closed_at": node.get("closedAt"),
                        "state_reason": str(node.get("stateReason") or "").lower() or None, "labels": labels})
        if not page.get("hasNextPage"):
            break
        if not page.get("endCursor"):
            return None
        cursor = page["endCursor"]
    return out if len(out) == total else None


def read_bugs(gh: ghcli.Gh, data: dict, owner_repo: str = ghcli.DEFAULT_REPO) -> dict | None:
    """``{open: [...], closed: [...]}``: the open ``nightly-red`` issues, and
    the closed ones updated since the bug filer's horizon (the start of the
    first night it reads); None when either read failed."""
    horizon = filer_horizon(data)
    opened = read_issues(gh, STATES_OPEN, None, owner_repo)
    closed = read_issues(gh, STATES_CLOSED, horizon.strftime(nightly_bugs.GITHUB_TIME_FORMAT), owner_repo) if horizon else []
    if opened is None or closed is None:
        return None
    return {"open": opened, "closed": closed}


def filer_horizon(data: dict) -> datetime.datetime | None:
    """The bug filer's horizon, computed as ``Filer.run`` computes it."""
    parts = nightly_infra.part_nights(data, nightly_bugs.LOOKBACK_NIGHTS)
    starts = [nightly.parse_iso(n.get("started")) for ns in parts.values() for n in ns]
    return min((t for t in starts if t), default=None)


def domain_counts(issues: list[dict], slugs: list[str]) -> dict[str, int]:
    """``{slug: issues labelled domain:<slug>}``."""
    counts = dict.fromkeys(slugs, 0)
    for issue in issues:
        labels = {label.get("name") for label in issue.get("labels") or []}
        for slug in slugs:
            if DOMAIN_LABEL.format(slug=slug) in labels:
                counts[slug] += 1
    return counts


def bugs_url(repo: str, slug: str) -> str:
    query = BUG_QUERY.format(label=BUG_LABEL, domain_label=DOMAIN_LABEL.format(slug=slug))
    return ISSUES_SEARCH_URL.format(repo=repo, query=urllib.parse.quote(query, safe=""))


# ---------------------------------------------------------------------------
# the nights


def night_status(night: dict[str, dict]) -> str | None:
    """None for a counted night, else why it is left out."""
    parts = nightly.night_parts(night)
    if any(nightly.run_truncated(run) for run in parts):
        return WHY_CUT
    if nightly.PART_MAIN not in night:
        return WHY_NO_MAIN
    if classify.is_run_event({"tasks": [t for run in parts for t in classify.run_tasks(run)]}):
        return WHY_RUN_EVENT
    return None


def nights_by_date(data: dict) -> tuple[dict[datetime.date, list[dict]], dict[datetime.date, str]]:
    """``({date: [counted night, ...] oldest first}, {date: why left out})``;
    a date with no counted night is left out with its newest night's reason."""
    counted: dict[datetime.date, list[dict]] = {}
    left_out: dict[datetime.date, str] = {}
    for night in nightly.group_nights(nightly.sorted_nightly_runs(data)):
        day = nightly.night_date(nightly.night_parts(night)[0])
        if day is None:
            continue
        why = night_status(night)
        if why is None:
            counted.setdefault(day, []).append(night)
            left_out.pop(day, None)
        elif day not in counted:
            left_out[day] = why
    return counted, left_out


def night_reps(night: dict[str, dict]) -> dict[str, dict]:
    """``{case: {passed, graded, agent_s: [...]}}`` for every case the night
    graded; a case both parts recorded counts as the main part recorded it."""
    out: dict[str, dict] = {}
    for run in nightly.night_parts(night):
        for task in classify.run_tasks(run):
            name = task.get("name")
            if not isinstance(name, str) or name in out:
                continue
            counts = classify.rep_counts(task)
            graded = counts["pass"] + counts["fail"]
            if not graded:
                continue
            times = [rep["agent_s"] for rep in classify.task_reps(task)
                     if classify.rep_kind(rep) in (classify.REP_PASS, classify.REP_FAIL) and isinstance(rep.get("agent_s"), (int, float))]
            out[name] = {"passed": counts["pass"], "graded": graded, "agent_s": times}
    return out


def date_reps(nights: list[dict]) -> dict[str, dict]:
    """One date's verdicts: per case, the newest counted night that graded
    it stands, as ``nightly_bugs.graded_nights`` reads a re-run, so a main
    re-run with no writers part keeps the writers cases of the cron night."""
    out: dict[str, dict] = {}
    for night in nights:
        out.update(night_reps(night))
    return out


def week_cases(counted: dict[datetime.date, list[dict]], days: list[datetime.date]) -> dict[str, dict]:
    """``{case: {passed, graded, agent_s}}`` summed over the week's counted dates."""
    out: dict[str, dict] = {}
    for day in days:
        for name, reps in date_reps(counted.get(day, [])).items():
            row = out.setdefault(name, {"passed": 0, "graded": 0, "agent_s": []})
            row["passed"] += reps["passed"]
            row["graded"] += reps["graded"]
            row["agent_s"] += reps["agent_s"]
    return out


def week_models(counted: dict, days: list[datetime.date], models: dict[str, set[str]]) -> tuple[list[str], list[datetime.date]]:
    """``(sorted models the week's counted nights ran, counted dates with none on record)``."""
    found: set[str] = set()
    missing = []
    for day in days:
        if day not in counted:
            continue
        builds = [run["build_id"] for night in counted[day] for run in nightly.night_parts(night) if isinstance(run.get("build_id"), str)]
        night_models = set().union(*(models.get(b, set()) for b in builds))
        if not night_models:
            missing.append(day)
        found |= night_models
    return sorted(found), missing


def streak_cases(data: dict, repo_root: pathlib.Path) -> dict[str, dict]:
    """``{case: {domain, oldest, newest, passes}}``: the cases on a red
    streak the bug filer files for right now -- failed every graded repetition on their
    last STREAK_NIGHTS graded nights on record (``nightly_bugs.graded_nights``),
    on this checkout's nightly matrix, not ``expected_fail`` -- with the
    domain their task.yaml claims (the label the filer puts on the bug), the
    streak's oldest and newest nights and the passing nights, which
    ``owed_bugs`` reads as the filer does. Judged on every night on record,
    not only the week's: a case red through Sunday that passed Monday's
    night is owed nothing."""
    active = set(nightly.expected_cases(data))
    out: dict[str, dict] = {}
    for case, verdicts in nightly_bugs.graded_nights(data).items():
        streak = nightly_bugs.red_streak(verdicts)
        fields = nightly_bugs.task_fields(case, repo_root)
        if not streak or fields is None or case not in active or fields["expected_fail"]:
            continue
        out[case] = {"domain": fields["domain"] or nightly.DOMAIN_UNKNOWN, "oldest": streak[0], "newest": streak[-1],
                     "passes": [v for v in verdicts if v["outcome"] == classify.OUTCOME_PASSED]}
    return out


def owed_bugs(streaks: dict[str, dict], bugs: dict, horizon: datetime.datetime | None) -> dict[str, list[str]]:
    """``{domain: [case, ...]}``: streak cases no issue tracks -- no open
    ``nightly-red`` issue names the case, and no closed one still tracks it
    (``nightly_bugs.still_tracks``: closed after the streak's oldest night
    started, or handed off as not planned with no pass since). These are
    the bugs the filer should have filed and has not."""
    out: dict[str, list[str]] = {}
    for case, info in sorted(streaks.items()):
        if nightly_bugs.tracking_issue(bugs["open"], case):
            continue
        closed = [i for i in bugs["closed"] if nightly_bugs.still_tracks(i, info["newest"], info["passes"], horizon, info["oldest"])]
        if nightly_bugs.tracking_issue(closed, case):
            continue
        out.setdefault(info["domain"], []).append(case)
    return out


# ---------------------------------------------------------------------------
# the week, as data


def trend_of(change: int | None) -> str | None:
    if change is None:
        return None
    if change <= -TREND_PTS:
        return TREND_DOWN
    if change >= TREND_PTS:
        return TREND_UP
    return TREND_STEADY


def needs_attention(row: dict) -> bool:
    return row["pct"] < NEEDS_ATTENTION_BELOW_PCT or (row["trend"] == TREND_DOWN and -row["change"] >= NEEDS_ATTENTION_DROP_PTS)


def build_week(data: dict, store: dict | None, domain_rows: list[dict], week_end: datetime.date,
               repo_root: pathlib.Path = REPO_ROOT) -> dict:
    """Everything the email says, computed; ``integrity_failures`` checks it
    and ``render`` prints it. Raises nothing: a week with no counted night
    or no common scenario carries ``fatal``."""
    days = [week_end - datetime.timedelta(days=i) for i in range(WEEK_DAYS - 1, -1, -1)]
    prev_days = [d - datetime.timedelta(days=WEEK_DAYS) for d in days]
    counted, left_out = nights_by_date(data)
    this_nights = [d for d in days if d in counted]
    prev_nights = [d for d in prev_days if d in counted]
    this_cases, prev_cases = week_cases(counted, days), week_cases(counted, prev_days)
    common = sorted(set(this_cases) & set(prev_cases))
    domain_of = nightly.domains(data)
    trend_ok = len(this_nights) >= MIN_COUNTED_NIGHTS and len(prev_nights) >= MIN_COUNTED_NIGHTS
    models = store_models(store)
    this_models, this_missing = week_models(counted, days, models)
    prev_models, prev_missing = week_models(counted, prev_days, models)
    week = {
        "week_end": week_end.isoformat(), "days": [d.isoformat() for d in days], "prev_days": [d.isoformat() for d in prev_days],
        "counted": [d.isoformat() for d in this_nights], "prev_counted": [d.isoformat() for d in prev_nights],
        "left_out": {d.isoformat(): left_out.get(d, WHY_NO_RUN) for d in days + prev_days if d not in counted},
        "trend_ok": trend_ok, "cases": common,
        "one_week_only": sorted((set(this_cases) | set(prev_cases)) - set(common)),
        "data_domains": {}, "rows": [], "attention": [], "claims": [], "fatal": None,
        "footer": {"range": [prev_days[0].isoformat(), week_end.isoformat()], "models": this_models,
                   "prev_models": prev_models, "missing": [d.isoformat() for d in this_missing + prev_missing]},
        "streaks": streak_cases(data, repo_root), "horizon": filer_horizon(data),
    }
    for case in sorted(set(this_cases) | set(prev_cases)):
        week["data_domains"].setdefault(domain_of.get(case, nightly.DOMAIN_UNKNOWN), []).append(case)
    if not this_nights:
        week["fatal"] = FAIL_NO_NIGHTS.format(range=date_range(days[0], days[-1]))
        return week
    if not common:
        week["fatal"] = FAIL_NO_COMMON
        return week

    def tally(cases):
        passed = sum(this_cases[c]["passed"] for c in cases)
        graded = sum(this_cases[c]["graded"] for c in cases)
        prev_passed = sum(prev_cases[c]["passed"] for c in cases)
        prev_graded = sum(prev_cases[c]["graded"] for c in cases)
        pct, prev_pct = pct_of(passed, graded), pct_of(prev_passed, prev_graded)
        return {"passed": passed, "graded": graded, "prev_passed": prev_passed, "prev_graded": prev_graded,
                "pct": pct, "prev_pct": prev_pct}

    overall = tally(common)
    overall["scenarios"] = len(common)
    overall["change"] = overall["pct"] - overall["prev_pct"] if trend_ok else None
    overall["trend"] = trend_of(overall["change"])
    times = [t for c in common for t in this_cases[c]["agent_s"]]
    overall["agent_s_median"] = statistics.median(times) if times else None
    overall["timed_runs"] = len(times)
    week["overall"] = overall

    known = {row["slug"]: row for row in domain_rows}
    for slug, info in known.items():
        cases = [c for c in common if domain_of.get(c) == slug]
        if not cases:
            continue
        row = {"slug": slug, "name": info["name"], "meaning": info["meaning"], "cases": cases, "scenarios": len(cases)}
        row.update(tally(cases))
        row["few"] = len(cases) < MIN_TREND_SCENARIOS
        row["change"] = row["pct"] - row["prev_pct"] if trend_ok and not row["few"] else None
        row["trend"] = trend_of(row["change"])
        week["rows"].append(row)
    week["rows"].sort(key=lambda r: (-r["pct"], r["name"]))
    week["attention"] = [r["slug"] for r in week["rows"] if needs_attention(r)]
    week["claims"] = build_claims(week, days)
    return week


def build_claims(week: dict, days: list[datetime.date]) -> list[dict]:
    """The intro's sentences as ``{kind, subjects, values}``; ``claim_text``
    turns one into text, ``claim_failures`` holds it to its rule."""
    overall = week["overall"]
    claims = [{"kind": CLAIM_LEAD, "subjects": [], "values": {
        "range": date_range(days[0], days[-1]), "passed": overall["passed"], "graded": overall["graded"],
        "pct": overall["pct"], "scenarios": overall["scenarios"], "nights": len(week["counted"]), "dates": len(days)}}]
    rows = week["rows"]
    great = [r["slug"] for r in rows if r["pct"] >= GREAT_SHAPE_PCT and r["trend"] in (TREND_UP, TREND_STEADY)]
    half = [r["slug"] for r in rows if r["pct"] < LESS_THAN_HALF_PCT]
    down = [r["slug"] for r in rows if r["trend"] == TREND_DOWN]
    if great:
        claims.append({"kind": CLAIM_GREAT, "subjects": great, "values": {}})
    if half:
        claims.append({"kind": CLAIM_HALF, "subjects": half, "values": {}})
    if down:
        claims.append({"kind": CLAIM_DOWN, "subjects": down,
                       "values": {"pts": {r["slug"]: -r["change"] for r in rows if r["slug"] in down}}})
    if not week["trend_ok"]:
        claims.append({"kind": CLAIM_NOT_ENOUGH, "subjects": [], "values": {
            "this": len(week["counted"]), "prev": len(week["prev_counted"])}})
    footer = week["footer"]
    if week["trend_ok"] and footer["models"] and footer["prev_models"] and footer["models"] != footer["prev_models"]:
        claims.append({"kind": CLAIM_MODEL_CHANGE, "subjects": [], "values": {
            "model": NAMES_JOIN.join(footer["models"]), "prev_model": NAMES_JOIN.join(footer["prev_models"])}})
    return claims


def claim_text(claim: dict, rows_by_slug: dict[str, dict]) -> str:
    template = CLAIM_TEMPLATES[claim["kind"]]
    names = NAMES_JOIN.join(rows_by_slug[s]["name"] for s in claim["subjects"])
    values = claim["values"]
    if claim["kind"] == CLAIM_DOWN:
        items = NAMES_JOIN.join(DOWN_ITEM.format(name=rows_by_slug[s]["name"], pts=values["pts"][s]) for s in claim["subjects"])
        return template.format(items=items)
    if claim["kind"] == CLAIM_NOT_ENOUGH:
        why = NOT_ENOUGH_WHY.format(prev=values["prev"], this=values["this"], need=MIN_COUNTED_NIGHTS)
        return template.format(why=why)
    return template.format(names=names, **values)


# ---------------------------------------------------------------------------
# the checks


def claim_failures(week: dict) -> list[str]:
    """Check 5: every claim is a known template and meets its rule."""
    out = []
    rows = {r["slug"]: r for r in week["rows"]}
    for claim in week["claims"]:
        kind = claim.get("kind")
        if kind not in CLAIM_TEMPLATES:
            out.append(FAIL_CLAIM.format(what=WHY_NOT_TEMPLATE.format(kind=kind)))
            continue
        for slug in claim.get("subjects") or []:
            row = rows.get(slug)
            if row is None:
                out.append(FAIL_CLAIM.format(what=WHY_NO_ROW.format(kind=kind, slug=slug)))
            elif kind == CLAIM_GREAT and not (row["pct"] >= GREAT_SHAPE_PCT and row["trend"] in (TREND_UP, TREND_STEADY)):
                out.append(FAIL_CLAIM.format(what=WHY_GREAT.format(name=row["name"], pct=row["pct"], trend=row["trend"] or NO_TREND)))
            elif kind == CLAIM_HALF and not row["pct"] < LESS_THAN_HALF_PCT:
                out.append(FAIL_CLAIM.format(what=WHY_HALF.format(name=row["name"], pct=row["pct"])))
            elif kind == CLAIM_DOWN:
                pts = (claim.get("values") or {}).get("pts", {}).get(slug)
                drop = row["prev_pct"] - row["pct"]
                if not (row["scenarios"] >= MIN_TREND_SCENARIOS and week["trend_ok"] and drop >= TREND_PTS and pts == drop):
                    out.append(FAIL_CLAIM.format(what=WHY_DOWN.format(pts=pts, name=row["name"], drop=drop, count=row["scenarios"])))
        if kind == CLAIM_NOT_ENOUGH and week["trend_ok"]:
            out.append(FAIL_CLAIM.format(what=WHY_NOT_ENOUGH_FALSE))
    if not week["trend_ok"] and not any(c.get("kind") == CLAIM_NOT_ENOUGH for c in week["claims"]):
        out.append(FAIL_CLAIM.format(what=WHY_NOT_ENOUGH_MISSING))
    for row in week["rows"]:
        if row["trend"] != trend_of(row["change"]) or (row["change"] is not None and row["change"] != row["pct"] - row["prev_pct"]):
            out.append(FAIL_CLAIM.format(what=WHY_TREND.format(name=row["name"], change=row["change"], trend=row["trend"])))
    return out


def integrity_failures(week: dict, domain_rows: list[dict], bugs: dict | None,
                       data: dict, page: str | None = None) -> list[str]:
    """Every integrity check (module docstring) over the computed week and,
    when given, the rendered page. Empty means the email may be rendered."""
    if week.get("fatal"):
        return [week["fatal"]]
    out: list[str] = []
    rows = week["rows"]
    common = set(week["cases"])

    # 1. same cases
    shown = [c for r in rows for c in r["cases"]]
    if len(shown) != len(set(shown)) or not set(shown) <= common:
        out.append(FAIL_SAME_CASES.format(what=WHY_DOUBLE))
    if week["overall"]["scenarios"] != len(common):
        out.append(FAIL_SAME_CASES.format(what=WHY_TILE.format(shown=week["overall"]["scenarios"], count=len(common))))
    counted, _ = nights_by_date(data)
    this_cases = week_cases(counted, [datetime.date.fromisoformat(d) for d in week["days"]])
    prev_cases = week_cases(counted, [datetime.date.fromisoformat(d) for d in week["prev_days"]])
    if common != set(this_cases) & set(prev_cases):
        out.append(FAIL_SAME_CASES.format(what=WHY_NOT_COMMON))
    else:
        for key, source in (("passed", this_cases), ("graded", this_cases), ("prev_passed", prev_cases), ("prev_graded", prev_cases)):
            field = key.removeprefix("prev_")
            if week["overall"][key] != sum(source[c][field] for c in common):
                out.append(FAIL_SAME_CASES.format(what=WHY_SUM.format(key=key.replace(KEY_SEP, SPACE))))

    # 2. truncated nights excluded
    for day in week["counted"] + week["prev_counted"]:
        if datetime.date.fromisoformat(day) not in counted:
            out.append(FAIL_TRUNCATED.format(day=day))

    # 3. not enough data
    enough = len(week["counted"]) >= MIN_COUNTED_NIGHTS and len(week["prev_counted"]) >= MIN_COUNTED_NIGHTS
    if not enough:
        called = [r["name"] for r in rows if r["trend"] is not None] + (["overall"] if week["overall"]["trend"] else [])
        if week["trend_ok"] or called:
            out.append(FAIL_NOT_ENOUGH.format(counted=len(week["counted"]), prev=len(week["prev_counted"]), what=NAMES_JOIN.join(called) or THE_WEEK))

    # 4. few scenarios
    for row in rows:
        if row["scenarios"] < MIN_TREND_SCENARIOS and row["trend"] is not None:
            out.append(FAIL_FEW.format(name=row["name"], count=row["scenarios"], trend=row["trend"]))

    # 5. claims
    out += claim_failures(week)

    # 6. needs attention
    want = [r["slug"] for r in rows if needs_attention(r)]
    if want != week["attention"]:
        out.append(FAIL_ATTENTION.format(want=want, got=week["attention"]))

    # 7. bugs exist
    names = {r["slug"]: r["name"] for r in domain_rows}
    if bugs is None:
        out.append(FAIL_BUGS_UNREAD)
    else:
        for domain, cases in sorted(owed_bugs(week["streaks"], bugs, week["horizon"]).items()):
            out.append(FAIL_BUGS_ZERO.format(name=names.get(domain, domain), cases=NAMES_JOIN.join(cases),
                                             nights=nightly_bugs.STREAK_NIGHTS))
        counts = domain_counts(bugs["open"], list(week.get("shown_bugs") or {}))
        for slug, shown_count in (week.get("shown_bugs") or {}).items():
            if shown_count != counts.get(slug):
                out.append(FAIL_BUG_COUNT.format(name=names.get(slug, slug), shown=shown_count, count=counts.get(slug)))

    # 8. links
    if page is not None:
        for url in LINK_ATTR_RE.findall(page):
            if not well_formed(html.unescape(url)):
                out.append(FAIL_LINK.format(url=url))
        for slug in week["attention"]:
            if html.escape(bugs_url(week.get("repo", ghcli.DEFAULT_REPO), slug)) not in page:
                out.append(FAIL_LINK_MISSING.format(name=names.get(slug, slug)))

    # 9. domains match. A case with no task.yaml on this checkout (renamed
    # or deleted, its old runs still on record) is "unknown", the dashboard's
    # "no domain" bucket, not a domains.yaml row to add.
    known = {r["slug"] for r in domain_rows}
    for domain, cases in sorted(week["data_domains"].items()):
        if domain not in known and domain != nightly.DOMAIN_UNKNOWN:
            out.append(FAIL_DOMAIN_UNKNOWN.format(domain=domain, cases=NAMES_JOIN.join(cases)))
    this_domains = {nightly.domains(data).get(c) for c in this_cases}
    for row in domain_rows:
        if row["slug"] not in this_domains and not row["new"]:
            out.append(FAIL_DOMAIN_NO_DATA.format(domain=row["slug"]))

    # 10. footer from metadata
    footer = week["footer"]
    for night in footer["missing"]:
        out.append(FAIL_FOOTER_MODEL.format(night=night))
    want_range = [week["prev_days"][0], week["week_end"]]
    if footer["range"] != want_range:
        out.append(FAIL_FOOTER.format(got=footer["range"], want=want_range))
    if page is not None:
        rendered = footer_text(week)
        if html.escape(rendered, quote=False) not in page and rendered not in page:
            out.append(FAIL_FOOTER.format(got="something else", want=rendered))
    return out


def well_formed(url: str) -> bool:
    if not URL_CHARS_RE.fullmatch(url or "") or BAD_ESCAPE_RE.search(url):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == HTTPS and bool(parts.hostname)


def recheck_footer(week: dict, data: dict, store: dict | None) -> list[str]:
    """Check 10's other half: the models named are the ones the store
    records for the counted nights' builds, recomputed from the inputs."""
    counted, _ = nights_by_date(data)
    models = store_models(store)
    out = []
    for key, days in (("models", week["days"]), ("prev_models", week["prev_days"])):
        want, _ = week_models(counted, [datetime.date.fromisoformat(d) for d in days], models)
        if week["footer"][key] != want:
            out.append(FAIL_FOOTER.format(got=week["footer"][key], want=want))
    return out


# ---------------------------------------------------------------------------
# the page


def footer_text(week: dict) -> str:
    footer = week["footer"]
    start, end = (datetime.date.fromisoformat(d) for d in footer["range"])
    this, prev = NAMES_JOIN.join(footer["models"]), NAMES_JOIN.join(footer["prev_models"])
    models = this if this == prev or not prev else FOOTER_MODELS.format(this=this, prev=prev)
    return FOOTER.format(range=date_range(start, end), models=html.escape(models), rendered=week.get("rendered", ""))


def change_label(trend: str | None, change: int | None) -> tuple[str, str]:
    if trend == TREND_UP:
        return ROW_UP.format(pts=change), f"color:{GOOD};font-weight:600;"
    if trend == TREND_DOWN:
        return ROW_DOWN.format(pts=-change), f"color:{BAD};font-weight:600;"
    if trend == TREND_STEADY:
        return ROW_STEADY, f"color:{MUTED};"
    return "", f"color:{MUTED};"


def render(week: dict, bugs: dict, repo: str = ghcli.DEFAULT_REPO) -> str:
    """The email's HTML. Sets ``week['shown_bugs']`` to the counts it printed."""
    rows_by_slug = {r["slug"]: r for r in week["rows"]}
    day = datetime.date.fromisoformat(week["days"][0])
    overall = week["overall"]
    esc = html.escape

    header = HEADER_ROW.format(bg=HEADER_BG, gradient=HEADER_GRADIENT, robot=ROBOT_URL, kicker=KICKER,
                               kicker_text=HEADER_KICKER, title=esc(HEADER_TITLE.format(day=short_date(day))))
    intro = INTRO_ROW.format(muted=MUTED, text=esc(" ".join(claim_text(c, rows_by_slug) for c in week["claims"])))

    if overall["trend"] == TREND_UP:
        change = CHANGE_UP.format(pts=overall["change"])
    elif overall["trend"] == TREND_DOWN:
        change = CHANGE_DOWN.format(pts=-overall["change"])
    elif overall["trend"] == TREND_STEADY:
        change = CHANGE_STEADY
    else:
        change = CHANGE_NONE
    if overall["agent_s_median"] is not None:
        time_value = TILE_TIME_VALUE.format(minutes=round(overall["agent_s_median"] / SECONDS_PER_MINUTE))
        time_sub = TILE_TIME_SUB.format(count=overall["timed_runs"])
    else:
        time_value, time_sub = TILE_TIME_NONE, TILE_TIME_NONE_SUB
    tile = {"rule": RULE, "ink": INK, "faint": FAINT}
    tiles = TILES_ROW.format(tiles="".join([
        TILE.format(value=f"{overall['pct']}%", label=TILE_RATE_LABEL, sub=change, **tile),
        TILE.format(value=overall["scenarios"], label=TILE_SCENARIOS_LABEL, sub=TILE_SCENARIOS_SUB, **tile),
        TILE.format(value=time_value, label=TILE_TIME_LABEL, sub=time_sub, **tile),
    ]))

    shown = {}
    items = []
    bug_counts = domain_counts(bugs["open"], week["attention"])
    for slug in week["attention"]:
        row = rows_by_slug[slug]
        if row["few"]:
            state = ATTENTION_FEW.format(pct=row["pct"])
        elif row["trend"] == TREND_DOWN:
            state = ATTENTION_DOWN.format(pct=row["pct"], pts=-row["change"])
        else:
            state = ATTENTION_RATE.format(pct=row["pct"])
        shown[slug] = bug_counts.get(slug, 0)
        items.append(ATTENTION_ITEM.format(rule=RULE, ink=INK, muted=MUTED, link=LINK, name=esc(row["name"]), state=state,
                                           url=esc(bugs_url(repo, slug)), bugs=OPEN_BUGS.format(count=shown[slug])))
    if not items:
        items.append(ATTENTION_EMPTY.format(rule=RULE, muted=MUTED, text=ATTENTION_NONE if week["trend_ok"] else ATTENTION_NONE_NO_TREND))
    week["shown_bugs"] = shown
    week["repo"] = repo
    attention = ATTENTION_ROW.format(bg=ATTENTION_BG, ink=INK, muted=MUTED, title=ATTENTION_TITLE, intro=ATTENTION_INTRO, items="".join(items))

    areas = []
    for row in week["rows"]:
        label, style = change_label(row["trend"], row["change"])
        if row["few"]:
            label = ROW_FEW
        elif row["trend"] is None:
            label = ROW_NO_TREND
        filled = max(BAR_MIN_PX, round(row["pct"] * BAR_WIDTH_PX / PERCENT))
        areas.append(AREA_ITEM.format(
            rule=RULE, ink=INK, faint=FAINT, muted=MUTED, link=LINK, bar_bg=BAR_BG, name=esc(row["name"]),
            scenarios=SCENARIO if row["scenarios"] == 1 else SCENARIOS.format(count=row["scenarios"]),
            meaning=esc(row["meaning"]), bar_cell=BAR_CELL_PX, bar=BAR_WIDTH_PX, filled=filled,
            pct=row["pct"], change=label, change_style=style))
    by_area = AREA_ROW.format(ink=INK, faint=FAINT, title=BY_AREA_TITLE, sub=BY_AREA_SUB, items="".join(areas))

    fix = FIX_ROW.format(bg=NOTE_BG, ink=INK, muted=MUTED, title=FIX_TITLE, text=FIX_TEXT)
    left = [d for d in week["days"] + week["prev_days"] if d in week["left_out"]]
    cut = CUT_SOME.format(days=NAMES_JOIN.join(CUT_DAY.format(day=short_date(datetime.date.fromisoformat(d)), why=week["left_out"][d])
                                               for d in sorted(left))) if left else CUT_NONE
    left_out = LEFT_OUT.format(count=len(week["one_week_only"])) if week["one_week_only"] else ""
    how = HOW_TO_READ.format(scenarios=overall["scenarios"], left_out=left_out, cut=cut)
    close = CLOSE_ROW.format(dashboard=esc(DASHBOARD_URL), link=LINK, faint=FAINT, button=DASHBOARD_BUTTON, how=how)

    return PAGE.format(title=esc(TITLE.format(day=short_date(day))), page_bg=PAGE_BG, width=CARD_WIDTH_PX, font=FONT,
                       faint=FAINT, robot=ROBOT_URL, footer=footer_text(week),
                       rows=ROWS_JOIN.join([header, intro, tiles, attention, by_area, fix, close]))


# ---------------------------------------------------------------------------


def produce(data: dict, store: dict | None, domain_rows: list[dict], week_end: datetime.date, bugs: dict | None,
            now: datetime.datetime, repo: str = ghcli.DEFAULT_REPO, repo_root: pathlib.Path = REPO_ROOT) -> tuple[str | None, list[str]]:
    """``(html, [])`` when every check passes, else ``(None, reasons)``."""
    week = build_week(data, store, domain_rows, week_end, repo_root)
    week["rendered"] = now.strftime(TIME_FORMAT)
    failures = integrity_failures(week, domain_rows, bugs, data)
    if failures:
        return None, failures
    failures = recheck_footer(week, data, store)
    page = render(week, bugs, repo)
    failures += integrity_failures(week, domain_rows, bugs, data, page)
    return (None, sorted(set(failures), key=failures.index)) if failures else (page, [])


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=pathlib.Path, help="data.json the collector wrote")
    parser.add_argument("--store", type=pathlib.Path, help="store.json store.py wrote (the model each night ran)")
    parser.add_argument("--out", required=True, type=pathlib.Path, help=f"where to write the HTML ({OUT_NAME} beside data.json)")
    parser.add_argument("--week-end", type=datetime.date.fromisoformat, help="the week's last nightly date (default: the Sunday before today, UTC)")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO)
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT, help="checkout holding domains.yaml and bench/tasks")
    return parser.parse_args(argv)


def main(argv=None, gh_runner=None, now: datetime.datetime | None = None) -> int:
    args = parse_args(argv)
    if yaml is None:
        log(f"ERROR: {FAIL_NO_YAML}")
        return 1
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        data = json.loads(args.data.read_text())
        store = json.loads(args.store.read_text()) if args.store and args.store.is_file() else None
    except (OSError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return 1
    domain_rows = load_domains(args.repo_root)
    gh = ghcli.Gh(args.repo, gh_runner) if gh_runner else ghcli.Gh(args.repo)
    bugs = read_bugs(gh, data if isinstance(data, dict) else {}, args.repo)
    week_end = args.week_end or default_week_end(now.date())
    page, failures = produce(data if isinstance(data, dict) else {}, store, domain_rows, week_end, bugs, now, args.repo, args.repo_root)
    if failures:
        log(f"weekly health email for the week ending {week_end}: NOT rendered; an integrity check failed:")
        for reason in failures:
            log(f"  - {reason}")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(f"weekly health email for the week ending {week_end}: wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
