#!/usr/bin/env python3
"""Open one issue per domain with no blocking presubmit seat, keep it current,
and close it when a roster case covers the domain again.

A domain is covered when a case on ``hack/eval/blocking-roster.txt`` claims it
(``domain:`` in its task.yaml) and carries a non-empty ``verification_spec``:
the rule ``scripts/test_domain_coverage.py`` holds the allowlist in
``docs/designs/domains.yaml`` to, read here through the same function
(``domain_coverage.domain_claims``). A demotion that takes a domain's only seat puts the domain
on that allowlist, and until #3968 nothing brought it back but someone
checking by hand. On every CI-health tick this step::

    a domain is uncovered
    -> one open issue for it, labelled `presubmit-gate` and `domain:<slug>`,
       assigned to the domain's `owner:` (`backup:` mentioned), naming how
       long it has been uncovered, why (the allowlist entry and its issue),
       and the domain's nightly cases ranked by their record; the body is
       edited in place when any of that changes

    a domain with an open issue is covered again
    -> one comment naming the roster case that covers it, and the issue is
       closed

**The candidates** are the domain's cases on the nightly matrix
(``nightly.expected_cases``), not marked ``expected_fail``, not on the roster.
Each carries its graded repetitions over the nightly nights on record, read by
the same rules the nightly bug filer reads them by (``nightly_bugs.graded_nights``:
a night cut off at the deadline, an all-infra night and a run-level event give
no verdict), and whether that record meets the roster's bar, 90% over at
least 20 graded runs (docs/eval-gate-roster.md, "The admission bar, and who
clears it"). When
``--store`` names the store.json the tick read, each also carries what the
evidence store says at the current next-mode version key, the mode the main nightly grades (``trend.record_state``:
``would-admit``, ``would-demote``, ``collecting`` with the runs still needed
for a full window of 20). They are ranked store ``would-admit`` first, then
by whether the nightly record meets the bar, then by pass rate.

**How long.** From the allowlist record's ``since:`` when the entry is a record
(#3968 section 1), else from ``since YYYY-MM-DD`` in the comment directly
above a bare slug, the way the entries are written today; with neither, the
issue says the start is not recorded.

**Expiry.** An allowlist record past its ``expires`` date fails no pull
request; this issue chases it. Which records have expired is
``domain_allowlist.expired_records`` (UTC dates), imported when present. Its
body says the exception expired and how long ago, the digest line adds
"exception expired N days ago", and the owner and backup are @-mentioned in a
comment at most once every 24 hours. Without that module, and for a bare
slug, there is no expiry.

**Dedupe.** The issue carries ``<!-- kube-agents-bot:coverage-gap
domain=<slug> -->`` and is re-found by that marker alone. The listing is the
nightly bug filer's (``nightly_bugs.read_listings`` for the label): when GraphQL
disagrees with the REST listing on every read, nothing is written that tick, so
an empty listing cannot open a second issue. A human who closes the bot's
issue as not planned while the domain is still uncovered keeps it closed:
the bot files again only for a gap that started after that close.

**What it does not do.** It opens no roster pull request: the CI-health
workflow holds no branch write path, and the eval crew approves every roster
line (hack/OWNERS). The issue names the move; a person makes it.

Dry run is the default: reads happen, every write is printed. ``--live``
writes; the workflow passes it under the switch the nightly bug filer uses
(``NIGHTLY_BUGS_LIVE=true`` and ``CI_HEALTH_MUTE`` not ``true``).
``digest_lines`` gives the 11 AM digest one line per uncovered domain
(``post_health.coverage_lines``); it reads no GitHub.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import pathlib
import re
import sys

import yaml

try:
    import case_owner
    import domain_coverage
    import eval_rosters
except ImportError:  # scripts/ is not on sys.path yet
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import case_owner
    import domain_coverage
    import eval_rosters
try:
    # Which allowlist records are past their `expires` date: the module the
    # record-shaped allowlist ships with (#3981). Before it lands, entries
    # are bare slugs with no expiry.
    import domain_allowlist
except ImportError:
    domain_allowlist = None
from eval_dashboard import ghcli, nightly, nightly_bugs, trend
from eval_dashboard.post_health import NIGHTLY_URL

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BLOCKING_ROSTER_PATH = domain_coverage.BLOCKING_ROSTER_PATH
PRESUBMIT_CASES_PATH = pathlib.Path("hack") / "eval" / "presubmit-cases.txt"

LABEL = "presubmit-gate"
DOMAIN_LABEL = nightly_bugs.DOMAIN_LABEL
MARKER = "<!-- kube-agents-bot:coverage-gap domain={slug} -->"
MARKER_RE = re.compile(r"<!-- kube-agents-bot:coverage-gap domain=([A-Za-z0-9_-]+) -->")
COVERED_MARKER = "<!-- kube-agents-bot:coverage-gap-covered domain={slug} -->"
CLOSE_PAYLOAD = {"state": "closed", "state_reason": "completed"}
# How far back the closed listing reaches when a gap's start is not recorded.
CLOSED_LOOKBACK = datetime.timedelta(days=90)
# The roster's bar (trend.py mirrors baselines.py's defaults).
BAR_RATE = trend.ADMISSION_RATE
BAR_RUNS = trend.ADMISSION_MIN_RUNS
CANDIDATES_IN_ISSUE = 5
# "remediation, since 2026-10-17 (#3120): ..." in the comment above a bare slug.
# Only the block's own opening ("<slug>, since ..."): a later sentence's
# "since" dates something else.
COMMENT_SINCE_RE = r"^#\s*{slug}, since (\d{{4}}-\d{{2}}-\d{{2}})(?: \(#(\d+)\))?"
ALLOWLIST_KEY_RE = re.compile(r"^allowlist:\s*(#.*)?$")
TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:")
BARE_ENTRY_RE = re.compile(r"^\s*-\s*([A-Za-z0-9_-]+)\s*(#.*)?$")

TITLE = "Presubmit coverage: {slug} has no blocking roster seat"
BODY = """\
{marker}
**{name}** (`{slug}`) has no case on `hack/eval/blocking-roster.txt` {how_long}, so no pull request can go red on it. The domain: {meaning}

{addressee}

{expiry}**Why it is uncovered:** {why}

**Candidates** (the domain's cases on the nightly matrix with a `verification_spec`, not `expected_fail`, best first). The nightly record is the graded repetitions over the last {nights} nightly nights, read as the nightly bug filer reads them; **Bar** holds it to the roster's {bar_pct}% over at least {bar_runs} graded runs, and the evidence store applies the same bar at the current next-mode version key.

{candidates}

Nightly report: {nightly_url}

**What the owner decides** (one pull request, approved by the eval crew)

- **Seat a candidate:** move its line from `hack/eval/nightly-cases.txt` into `hack/eval/presubmit-cases.txt` and `hack/eval/blocking-roster.txt`, delete the domain's `allowlist` entry in `docs/designs/domains.yaml`, and cite the record above.
- **Fix the demoted case** and restore its roster line the same way.
- **Keep it held out** on the presubmit (docs/eval-gate-roster.md), with the allowlist entry kept and its exit condition named.

Kept current by `scripts/eval_dashboard/coverage_gaps.py` on the CI-health tick (docs/ci-health.md, "Coverage gaps"). It rewrites this body as the record moves, so put notes in comments; it finds the issue by the `presubmit-gate` label and the marker at the top, so keep both. Closing it as not planned keeps it closed for this gap; any other close is filed again on the next tick. It closes the issue with a comment when a roster case covers the domain again. It does not open the roster pull request.
"""
TABLE_HEAD = "| Case | Nightly record | Bar | Evidence store |\n|---|---|---|---|"
TABLE_ROW = "| `{case}`{held} | {record} | {bar} | {store} |"
CANDIDATES_NOT_READ = "Not read this tick: the data.json it read held no cases."
NO_CANDIDATES = "None: no case of this domain on the nightly matrix carries a `verification_spec` without `expected_fail`, off the roster. The domain needs a new case, or a spec on one it has, and this issue is where to ask for it."
# Where the owner comes from: the domain row, or the allowlist record's own
# `owner:`, which wins.
OWNER_FROM_DOMAIN = "the domain owner in `docs/designs/domains.yaml`"
OWNER_FROM_RECORD = "the owner on the domain's allowlist record"
ASSIGNED = "**Assigned to** @{owner} ({source}).{backup}"
BACKUP = " Backup: @{backup}."
UNASSIGNED_REFUSED = "**Unassigned:** GitHub refused `{owner}`, {source}, as assignee.{backup}"
UNASSIGNED_NO_OWNER = "**Unassigned:** `docs/designs/domains.yaml` names no owner for `{slug}`."
# An open issue's line, from its assignees as GitHub holds them: the bot
# never changes them, so the body must not claim an assignment it lacks.
ASSIGNED_NOW = "**Assigned to** {logins}.{owner}{backup}"
OWNER_TOO = " Owner: @{owner} ({source})."
UNASSIGNED_NOW = "**Unassigned.** Owner: @{owner} ({source}).{backup}"
REFUSED_RE = re.compile(r"\*\*Unassigned:\*\* GitHub refused `([^`]+)`")
# A record past its `expires` date does not fail pull requests; this issue
# is where it is chased (``expired``).
EXPIRED_LINE = "**The allowlist exception expired on {expires}, {days} ago.** Seat a case, keep one held out, or move `expires` in a pull request that says why.\n\n"
EXPIRED_MARKER = "<!-- kube-agents-bot:coverage-gap-expired domain={slug} day={day} -->"
EXPIRED_MARKER_RE = re.compile(r"<!-- kube-agents-bot:coverage-gap-expired domain=([A-Za-z0-9_-]+) day=")
EXPIRED_COMMENT = """\
{marker}
{mentions}: the allowlist exception for `{slug}` in `docs/designs/domains.yaml` expired on {expires}, {days} ago, and the domain still has no blocking roster case. Pick one of the moves in the issue body, or move `expires` in a pull request that says why.
"""
# At most one expiry mention per issue in this long.
EXPIRED_PING_INTERVAL = datetime.timedelta(hours=24)
COVERED_COMMENT = """\
{marker}
`{slug}` is covered again: {cases} on `hack/eval/blocking-roster.txt` claims it with a `verification_spec`. Closing.
"""
DIGEST_LINE = "🚧 Coverage gap: {slug} uncovered{how_long}{expired} (owner: {owner}); {best}."
DIGEST_EXPIRED = ", exception expired {days} ago"
DIGEST_BEST = "best candidate {case}, {record}{store}"
DIGEST_NONE = "no candidate on the nightly record"
DIGEST_NOT_READ = "candidates not read (no cases in data.json)"


class BadRead(ValueError):
    """The repository read cannot be trusted: a gap off the allowlist."""


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# the repository: domains, the allowlist, the roster


def allowlist_entry(raw) -> dict | None:
    """One ``allowlist`` item as ``{slug, since, issue, owner, held_out}``:
    a bare slug or a record (#3968 section 1), so either shape reads the
    same; None for an item that is neither. Expiry is ``domain_allowlist``'s
    (``expired``)."""
    if isinstance(raw, str) and raw.strip():
        return {"slug": raw.strip(), "since": None, "issue": None, "owner": None, "held_out": None}
    if isinstance(raw, dict) and isinstance(raw.get("slug"), str):
        def date(value):
            # A timestamp (a datetime, which is a date subclass) is its day.
            if isinstance(value, datetime.datetime):
                return value.date()
            if isinstance(value, datetime.date):
                return value
            try:
                return datetime.date.fromisoformat(str(value)) if value else None
            except ValueError:
                return None
        issue = raw.get("issue")
        issue = int(str(issue).lstrip("#")) if str(issue or "").lstrip("#").isdigit() else None
        return {"slug": raw["slug"], "since": date(raw.get("since")), "issue": issue,
                "owner": raw.get("owner") if isinstance(raw.get("owner"), str) else None,
                "held_out": raw.get("held_out") if isinstance(raw.get("held_out"), str) else None}
    return None


def comment_dates(text: str) -> dict[str, tuple[datetime.date | None, int | None]]:
    """``{slug: (since, issue)}`` from the comment block directly above each
    bare slug under ``allowlist:`` (``remediation, since 2026-10-17 (#3120)``),
    for entries written before they became records."""
    out, block, inside = {}, [], False
    for line in text.splitlines():
        if ALLOWLIST_KEY_RE.match(line):
            inside, block = True, []
            continue
        if not inside:
            continue
        if TOP_LEVEL_KEY_RE.match(line):
            break
        stripped = line.strip()
        if not stripped:
            continue  # a blank line between a block and its entry keeps the block
        if stripped.startswith("#"):
            block.append(stripped)
            continue
        found = BARE_ENTRY_RE.match(line)
        if found and block:
            match = re.match(COMMENT_SINCE_RE.format(slug=re.escape(found.group(1))), block[0])
            try:
                if match:
                    out[found.group(1)] = (datetime.date.fromisoformat(match.group(1)), int(match.group(2)) if match.group(2) else None)
            except ValueError:  # a typo reads as "start date not recorded", as on the record path
                log(f"warning: allowlist comment for {found.group(1)}: {match.group(1)} is not a date")
        block = []
    return out


def load_domains(repo_root: pathlib.Path) -> tuple[list[dict], dict[str, dict]]:
    """``(domain rows, {slug: allowlist entry})`` from domains.yaml."""
    path = repo_root / case_owner.DOMAINS_PATH
    doc = nightly_bugs.load_yaml(path)
    rows = [row for row in doc.get("domains") or [] if isinstance(row, dict) and isinstance(row.get("slug"), str)]
    try:
        noted = comment_dates(path.read_text())
    except OSError:
        noted = {}
    allowlist = {}
    for raw in doc.get("allowlist") or []:
        entry = allowlist_entry(raw)
        if entry is None:
            continue
        if entry["since"] is None and entry["slug"] in noted:
            entry["since"], noted_issue = noted[entry["slug"]]
            entry["issue"] = entry["issue"] or noted_issue
        allowlist[entry["slug"]] = entry
    return rows, allowlist


def expired(repo_root: pathlib.Path, today: datetime.date) -> dict[str, tuple[datetime.date, int]]:
    """``{slug: (expires, days past)}`` for the records ``domain_allowlist``
    reads as expired on ``today`` (UTC); empty without that module, or when
    domains.yaml cannot be read."""
    if domain_allowlist is None:
        return {}
    try:
        owners, entries = domain_allowlist.load(repo_root / case_owner.DOMAINS_PATH)
    except (OSError, yaml.YAMLError) as exc:
        log(f"warning: could not read the allowlist's expiry: {exc}")
        return {}
    return {r.slug: (r.expires, r.days_past) for r in domain_allowlist.expired_records(entries, owners, today)}


def case_of(path: str) -> str:
    return path.split("/")[2]


# --------------------------------------------------------------------------
# the record


def store_records(store: dict | None, data: dict) -> dict[str, dict] | None:
    """``{case: trend.record_state}`` at the current next-mode key, the
    mode the main nightly and the blocking presubmit grade (today's series
    only when the store holds no next-mode records); None without a store,
    or for the last published one written back after a failed read
    (``error`` set) or a read stopped at its deadline (``partial``), whose
    verdicts are not this tick's."""
    # A read stopped at its deadline (``partial``) holds only some cases.
    if not isinstance(store, dict) or store.get("error") or trend.partial_read(store):
        return None
    document = trend.trend_document(store, data)
    cases = (document.get("next") or document).get("cases") or {}
    return {name: doc.get("record") for name, doc in cases.items() if doc.get("record")}


def candidate(case: str, verdicts: list[dict], records: dict | None, held_out: bool) -> dict:
    passed = sum(v["passed"] for v in verdicts)
    graded = sum(v["graded"] for v in verdicts)
    rate = passed / graded if graded else None
    record = (records or {}).get(case)
    return {"case": case, "nights": len(verdicts), "passed": passed, "graded": graded, "rate": rate,
            "meets_bar": graded >= BAR_RUNS and rate >= BAR_RATE, "held_out": held_out,
            "store": None if records is None else (record or {"state": "none"})}


def rank_key(c: dict):
    store_admits = bool(c["store"]) and c["store"].get("state") == "would-admit"
    return (not store_admits, not c["meets_bar"], -(c["rate"] or 0), -c["graded"], c["case"])


def find_gaps(repo_root: pathlib.Path, data: dict, store: dict | None, now: datetime.datetime) -> list[dict]:
    """Every domain with no blocking seat, oldest gap first, each with its
    owner, how long, why, and its ranked candidates (None when ``data``
    holds no cases to rank). Raises ValueError when domains.yaml gives no
    domains (an unreadable file must not read as every domain covered), and
    BadRead when a gap is off the allowlist (a bad roster or task read must
    not file or close anything)."""
    rows, allowlist = load_domains(repo_root)
    if not rows:
        raise ValueError(f"{repo_root / case_owner.DOMAINS_PATH} gave no domains")
    claims = domain_coverage.domain_claims(repo_root)
    roster = set(eval_rosters.blocking_roster(repo_root / BLOCKING_ROSTER_PATH))
    try:
        presubmit = set(eval_rosters.presubmit_cases(repo_root / PRESUBMIT_CASES_PATH))
    except (OSError, ValueError):
        presubmit = set()
    nights = nightly_bugs.graded_nights(data)
    records = store_records(store, data)
    matrix = set(nightly.expected_cases(data))
    # One calendar for both day counts in the body, UTC, the one expiry is
    # read on: they roll over on the same tick.
    today = now.astimezone(datetime.timezone.utc).date()
    past = expired(repo_root, today)
    # A case with no verification_spec would cover nothing once seated.
    specced = {case_of(path) for path, _, has_spec in domain_coverage.task_claims(repo_root) if has_spec}
    by_domain: dict[str, list[str]] = {}
    for case in sorted((matrix - roster) & specced):
        fields = nightly_bugs.task_fields(case, repo_root)
        if fields and fields.get("domain") and not fields["expected_fail"]:
            by_domain.setdefault(fields["domain"], []).append(case)
    gaps = []
    for row in rows:
        slug = row["slug"]
        if any(counts for _, counts in claims.get(slug, [])):
            continue
        entry = allowlist.get(slug)
        since = entry["since"] if entry else None
        if since and since > today:
            log(f"warning: the allowlist entry for {slug} has since {since}, after today; read as not recorded")
            since = None
        cands = sorted((candidate(c, nights.get(c, []), records, c in presubmit) for c in by_domain.get(slug, [])), key=rank_key) if data.get("cases") else None
        gaps.append({
            "slug": slug, "name": row.get("name") or slug, "meaning": row.get("meaning") or "",
            "owner": (entry or {}).get("owner") or row.get("owner"), "owner_from_record": bool((entry or {}).get("owner")), "backup": row.get("backup"),
            "since": since, "days": (today - since).days if since else None,
            "allowlisted": entry is not None, "issue": entry["issue"] if entry else None,
            "held_out_case": entry["held_out"] if entry else None,
            "expires": past[slug][0] if slug in past else None, "expired_days": past[slug][1] if slug in past else None,
            "candidates": cands,
        })
    # scripts/test_domain_coverage.py holds every uncovered domain to the
    # allowlist on main, so a gap off it can only come from a bad read (a
    # truncated roster, no bench/tasks): nothing is trusted that tick.
    off = sorted(g["slug"] for g in gaps if not g["allowlisted"])
    if off:
        for slug in off:
            log(f"warning: {slug} reads as uncovered but is not on the allowlist in docs/designs/domains.yaml")
        raise BadRead(f"{len(off)} of {len(gaps)} uncovered domains are not on the allowlist ({', '.join(off)}): a bad roster or bench/tasks read")
    return sorted(gaps, key=lambda g: (g["since"] is None, g["since"] or datetime.date.max, g["slug"]))


# --------------------------------------------------------------------------
# words


def days_text(days: int) -> str:
    return f"{days} {'day' if days == 1 else 'days'}"


def expiry_text(gap: dict) -> str:
    if gap.get("expired_days") is None:
        return ""
    return EXPIRED_LINE.format(expires=gap["expires"].isoformat(), days=days_text(gap["expired_days"]))


def how_long(gap: dict) -> str:
    if gap["days"] is None:
        return "(start date not recorded)"
    return f"{days_text(gap['days'])} (since {gap['since'].isoformat()})"


def percent(rate: float | None) -> str:
    # Floored, so 26/29 (89.7%) never reads "90%" beside "below 90%".
    return "n/a" if rate is None else f"{math.floor(rate * 100)}%"


def record_text(c: dict) -> str:
    if not c["graded"]:
        return f"no graded night in the last {nightly_bugs.LOOKBACK_NIGHTS}"
    return f"{c['passed']}/{c['graded']} over {c['nights']} {'night' if c['nights'] == 1 else 'nights'} ({percent(c['rate'])})"


def bar_text(c: dict) -> str:
    if c["meets_bar"]:
        return f"meets {percent(BAR_RATE)}"
    if c["graded"] < BAR_RUNS:
        return f"too few runs, {c['graded']} of {BAR_RUNS}"
    return f"below {percent(BAR_RATE)}"


def store_text(store: dict | None) -> str:
    if store is None:
        return "not read this tick"
    state = store.get("state")
    if state == "none":
        return "no record at the current key"
    runs, passes = store.get("runs") or 0, store.get("passes") or 0
    if state == "collecting":
        return f"collecting, {passes}/{runs}, {max(BAR_RUNS - runs, 0)} more runs for a full window"
    if state in ("would-admit", "would-demote"):
        return f"{state}, {passes}/{runs}"
    return f"{state}, {passes}/{runs}" if runs else str(state)


def why_text(gap: dict) -> str:
    parts = ["on the `allowlist` in `docs/designs/domains.yaml`"]
    if gap["issue"]:
        parts.append(f"for #{gap['issue']}")
    if gap["since"]:
        parts.append(f"since {gap['since'].isoformat()}")
    text = " ".join(parts) + "; the comment above the entry carries the full reason and its exit condition."
    if gap["held_out_case"]:
        text += f" `{gap['held_out_case']}` still runs on the presubmit as a held-out seat."
    return text


def addressee(gap: dict, refused: bool = False, assignees: list[str] | None = None) -> str:
    """The assignment line: as filed (``assignees`` None: the owner, or
    ``refused``), or for an open issue from the logins GitHub holds."""
    backup = BACKUP.format(backup=gap["backup"]) if gap.get("backup") else ""
    owner = gap.get("owner")
    source = OWNER_FROM_RECORD if gap.get("owner_from_record") else OWNER_FROM_DOMAIN
    if assignees and owner and [a.lower() for a in assignees] == [owner.lower()]:
        # As filed: the same line, so the first tick after filing edits nothing.
        return ASSIGNED.format(owner=owner, source=source, backup=backup)
    if assignees:
        too = OWNER_TOO.format(owner=owner, source=source) if owner and owner.lower() not in {a.lower() for a in assignees} else ""
        return ASSIGNED_NOW.format(logins=", ".join(f"@{a}" for a in assignees), owner=too, backup=backup)
    if not owner:
        return UNASSIGNED_NO_OWNER.format(slug=gap["slug"]) + backup
    if refused:
        return UNASSIGNED_REFUSED.format(owner=owner, source=source, backup=backup)
    if assignees is not None:
        return UNASSIGNED_NOW.format(owner=owner, source=source, backup=backup)
    return ASSIGNED.format(owner=owner, source=source, backup=backup)


def candidates_block(gap: dict) -> str:
    if gap["candidates"] is None:
        return CANDIDATES_NOT_READ
    if not gap["candidates"]:
        return NO_CANDIDATES
    rows = [TABLE_ROW.format(case=c["case"], held=" (held out on the presubmit)" if c["held_out"] else "", record=record_text(c),
                             bar=bar_text(c), store=store_text(c["store"]))
            for c in gap["candidates"][:CANDIDATES_IN_ISSUE]]
    more = len(gap["candidates"]) - CANDIDATES_IN_ISSUE
    return "\n".join([TABLE_HEAD, *rows] + ([f"\nAnd {more} more on the Nightly report."] if more > 0 else []))


def render_issue(gap: dict, refused: bool = False, assignees: list[str] | None = None) -> tuple[str, str]:
    title = TITLE.format(slug=gap["slug"])
    body = BODY.format(
        marker=MARKER.format(slug=gap["slug"]), name=gap["name"], slug=gap["slug"], how_long="for " + how_long(gap) if gap["days"] is not None else how_long(gap),
        meaning=gap["meaning"], addressee=addressee(gap, refused, assignees), expiry=expiry_text(gap), why=why_text(gap), nights=nightly_bugs.LOOKBACK_NIGHTS,
        bar_pct=round(BAR_RATE * 100), bar_runs=BAR_RUNS, candidates=candidates_block(gap), nightly_url=NIGHTLY_URL)
    return title[:nightly_bugs.TITLE_MAX_CHARS], body[:nightly_bugs.ISSUE_BODY_MAX_CHARS]


def digest_lines(gaps: list[dict]) -> list[str]:
    """One digest line per uncovered domain; none when every domain is covered."""
    lines = []
    for gap in gaps:
        best = gap["candidates"][0] if gap["candidates"] else None
        if best:
            record = f"{best['passed']}/{best['graded']} on the nightly ({percent(best['rate'])}, {bar_text(best)})"
            store = f"; store {store_text(best['store'])}" if best["store"] is not None else ""
            best_text = DIGEST_BEST.format(case=best["case"], record=record, store=store)
        else:
            best_text = DIGEST_NONE if gap["candidates"] is not None else DIGEST_NOT_READ
        long = f" {days_text(gap['days'])}" if gap["days"] is not None else ", start date not recorded"
        expired = DIGEST_EXPIRED.format(days=days_text(gap["expired_days"])) if gap.get("expired_days") is not None else ""
        lines.append(DIGEST_LINE.format(slug=gap["slug"], how_long=long, expired=expired, owner=gap.get("owner") or "none on record", best=best_text))
    return lines


# --------------------------------------------------------------------------
# GitHub


def marker_slug(issue: dict) -> str | None:
    found = MARKER_RE.search(issue.get("body") or "")
    return found.group(1) if found else None


class GapFiler(nightly_bugs.Filer):
    """The nightly bug filer's GitHub plumbing (``create`` with its refused-
    assignee fallback, ``comment_once``) for the coverage issues."""

    claims = None  # domain_coverage.domain_claims, read once a closing is due

    def run_gaps(self, gaps: list[dict], now: datetime.datetime) -> dict:
        result = {"filed": [], "updated": [], "unchanged": [], "pinged": [], "kept_closed": [], "closed": [], "failed": []}
        starts = [datetime.datetime.combine(g["since"], datetime.time(), datetime.timezone.utc) for g in gaps if g["since"]]
        # Back to the oldest dated gap, and CLOSED_LOOKBACK for an undated one.
        horizon = min(starts + ([now - CLOSED_LOOKBACK] if any(g["since"] is None for g in gaps) or not starts else []))
        open_issues, closed_issues, doubt = nightly_bugs.read_listings(self.gh, horizon.strftime(nightly_bugs.GITHUB_TIME_FORMAT), label=LABEL)
        if open_issues is None or closed_issues is None:
            log(f"warning: could not list the {LABEL} issues; nothing written this pass")
            return result
        if doubt:
            log(f"warning: no trusted {LABEL} issue listing this pass; nothing written, retried next tick")
            return result
        ours: dict[str, dict] = {}
        for issue in sorted((i for i in open_issues if isinstance(i, dict) and not i.get("pull_request") and i.get("number")), key=lambda i: i["number"]):
            slug = marker_slug(issue)
            if slug and slug not in ours:
                ours[slug] = issue
        uncovered = {g["slug"] for g in gaps}
        for gap in gaps:
            issue = ours.get(gap["slug"])
            if issue:
                self.update(gap, issue, result)
                if gap.get("expired_days") is not None:
                    self.ping_expired(gap, issue, now, result)
                continue
            closed = self.handed_off(closed_issues, gap, now)
            if closed:
                result["kept_closed"].append({"slug": gap["slug"], "issue": closed["number"]})
                continue
            self.file_gap(gap, result)
        for slug, issue in sorted(ours.items()):
            if slug not in uncovered:
                self.close_gap(slug, issue, result)
        return result

    def handed_off(self, closed_issues: list, gap: dict, now: datetime.datetime) -> dict | None:
        """A bot issue for this domain a human closed as not planned after
        the gap began, or within CLOSED_LOOKBACK when its start is not
        recorded (the listing filters on the last update, not the close)."""
        for issue in closed_issues or []:
            if not isinstance(issue, dict) or marker_slug(issue) != gap["slug"]:
                continue
            if issue.get("state_reason") not in nightly_bugs.HANDED_OFF_REASONS:
                continue
            closed = nightly.parse_iso(issue.get("closed_at"))
            if closed and (closed >= now - CLOSED_LOOKBACK if gap["since"] is None else closed.date() >= gap["since"]):
                return issue
        return None

    def file_gap(self, gap: dict, result: dict) -> None:
        labels = [LABEL, DOMAIN_LABEL.format(slug=gap["slug"])]
        attempts = []
        for owner, refused in ([(gap["owner"], False), (None, True)] if gap.get("owner") else [(None, False)]):
            title, body = render_issue(gap, refused)
            attempts.append((owner, {"title": title, "body": body, "labels": labels, **({"assignees": [owner]} if owner else {})}))
        created, assignee = self.create(attempts, gap["slug"])
        number = created.get("number") if isinstance(created, dict) else None
        entry = {"slug": gap["slug"], "assignee": assignee, "mention": gap.get("backup"), "labels": labels, "issue": number, "gap": gap}
        result["filed" if number or self.gh.dry_run else "failed"].append(entry)

    def update(self, gap: dict, issue: dict, result: dict) -> None:
        """Edit the body (and title) in place when the rendered one differs.
        Assignees are never touched (a person may have reassigned it); the
        assignment line reads the ones GitHub holds. Not while the
        candidates could not be read: a good body is not overwritten with
        none."""
        if gap["candidates"] is None:
            result["unchanged"].append({"slug": gap["slug"], "issue": issue["number"]})
            return
        logins = [a.get("login") for a in issue.get("assignees") or [] if isinstance(a, dict) and a.get("login")]
        # Still "refused" only for the login GitHub refused; a corrected owner
        # was never tried, and the line says so plainly.
        was = REFUSED_RE.search(issue.get("body") or "")
        refused = not logins and bool(was) and was.group(1).lower() == (gap.get("owner") or "").lower()
        title, body = render_issue(gap, refused, logins)
        if body.strip() == (issue.get("body") or "").strip() and title == issue.get("title"):
            result["unchanged"].append({"slug": gap["slug"], "issue": issue["number"]})
            return
        done = self.gh.call("PATCH", self.gh.path(f"issues/{issue['number']}"), {"title": title, "body": body})
        result["updated" if done is not None or self.gh.dry_run else "failed"].append({"slug": gap["slug"], "issue": issue["number"], "gap": gap})

    def ping_expired(self, gap: dict, issue: dict, now: datetime.datetime, result: dict) -> None:
        """@-mention the owner and backup on an expired record, at most once
        per EXPIRED_PING_INTERVAL: not when the last such comment, or the
        issue itself when it was filed after the expiry (its body carries
        the mentions), is younger than that. Comments that cannot be read,
        or fewer than the listing counts, wait for the next tick."""
        created = nightly.parse_iso(issue.get("created_at"))
        if created and created.date() > gap["expires"] and now - created < EXPIRED_PING_INTERVAL:
            return
        number = issue["number"]
        comments = self.gh.call("GET", self.gh.path(nightly_bugs.COMMENTS_READ_PATH.format(number=number)), paginate=True)
        if comments is None or (isinstance(issue.get("comments"), int) and len(comments) < issue["comments"]):
            log(f"warning: could not read every comment on #{number}; no expiry mention this tick")
            result["failed"].append({"slug": gap["slug"], "issue": number})
            return
        pinged = [nightly.parse_iso(c.get("created_at")) for c in comments
                  if isinstance(c, dict) and gap["slug"] in EXPIRED_MARKER_RE.findall(c.get("body") or "")]
        last = max((t for t in pinged if t), default=None)
        if any(t is None for t in pinged) or (last and now - last < EXPIRED_PING_INTERVAL):
            return
        mentions = " ".join(f"@{login}" for login in dict.fromkeys(x for x in (gap.get("owner"), gap.get("backup")) if x)) or "Domain owner"
        body = EXPIRED_COMMENT.format(marker=EXPIRED_MARKER.format(slug=gap["slug"], day=now.astimezone(datetime.timezone.utc).date().isoformat()),
                                      mentions=mentions, slug=gap["slug"], expires=gap["expires"].isoformat(), days=days_text(gap["expired_days"]))
        done = self.gh.call("POST", self.gh.path(nightly_bugs.COMMENTS_PATH.format(number=number)), {"body": body})
        result["pinged" if done is not None or self.gh.dry_run else "failed"].append({"slug": gap["slug"], "issue": number, "mentions": mentions, "days": gap["expired_days"]})

    def close_gap(self, slug: str, issue: dict, result: dict) -> None:
        if self.claims is None:
            self.claims = domain_coverage.domain_claims(self.repo_root)
        cases = [case_of(path) for path, counts in self.claims.get(slug, []) if counts]
        if not cases:
            # Not covered, only gone from domains.yaml (renamed, removed):
            # saying "covered again" would be false.
            log(f"warning: #{issue['number']} tracks {slug}, which no roster case covers and domains.yaml no longer lists; left open")
            return
        named = ", ".join(f"`{c}`" for c in cases)
        marker = COVERED_MARKER.format(slug=slug)
        posted = self.comment_once(issue, marker, COVERED_COMMENT.format(marker=marker, slug=slug, cases=named))
        if posted is False:
            result["failed"].append({"slug": slug, "issue": issue["number"]})
            return
        done = self.gh.call("PATCH", self.gh.path(f"issues/{issue['number']}"), CLOSE_PAYLOAD)
        result["closed" if done is not None or self.gh.dry_run else "failed"].append({"slug": slug, "issue": issue["number"], "cases": cases})


# --------------------------------------------------------------------------
# entry point


def summary(result: dict, dry_run: bool) -> str:
    would = "would " if dry_run else ""
    head = (f"coverage gaps ({'dry run' if dry_run else 'live'}): {would}file {len(result['filed'])}, {would}update {len(result['updated'])}, "
            f"{len(result['unchanged'])} unchanged, {would}mention {len(result['pinged'])} expired, {would}close {len(result['closed'])}, {len(result['kept_closed'])} closed by a person, {len(result['failed'])} failed")
    lines = [head]
    for f in result["filed"]:
        gap = f["gap"]
        who = f"assign @{f['assignee']}" if f["assignee"] else "unassigned"
        mention = f"; mention @{f['mention']}" if f["mention"] else ""
        expired = f"; exception expired {gap['expires'].isoformat()}, {days_text(gap['expired_days'])} ago" if gap.get("expired_days") is not None else ""
        lines.append(f"  file{' #' + str(f['issue']) if f['issue'] else ''}: {gap['slug']} [{', '.join(f['labels'])}] {who}{mention}; uncovered {how_long(gap)}{expired}")
        lines.extend(f"    {c['case']}: {record_text(c)}; bar: {bar_text(c)}; store: {store_text(c['store'])}"
                     for c in (gap["candidates"] or [])[:CANDIDATES_IN_ISSUE])
        if not gap["candidates"]:
            lines.append(f"    {DIGEST_NONE if gap['candidates'] is not None else DIGEST_NOT_READ}")
    lines += [f"  update: {u['slug']} on #{u['issue']}" for u in result["updated"]]
    lines += [f"  unchanged: {u['slug']} on #{u['issue']}" for u in result["unchanged"]]
    lines += [f"  expired: {e['slug']} on #{e['issue']}, mention {e['mentions']} ({days_text(e['days'])} past)" for e in result["pinged"]]
    lines += [f"  close: {c['slug']} #{c['issue']} (covered by {', '.join(c['cases']) or 'a roster case'})" for c in result["closed"]]
    lines += [f"  closed by a person: {k['slug']} #{k['issue']} (not planned; not filed again for this gap)" for k in result["kept_closed"]]
    lines += [f"  failed: {f['slug']} ({nightly_bugs.FAILED_NOTE})" for f in result["failed"]]
    return "\n".join(lines)


def read_json(path: pathlib.Path | None, what: str):
    if path is None:
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log(f"warning: {what} {path}: {exc}; read without it")
        return None
    return doc if isinstance(doc, dict) else None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=pathlib.Path, help="data.json the collector wrote")
    parser.add_argument("--store", type=pathlib.Path, default=None, help="store.json the tick read (optional: each candidate then carries the evidence store's verdict)")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO)
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT, help="checkout holding bench/tasks, hack/eval and domains.yaml")
    parser.add_argument("--now", help="evaluate as of this ISO 8601 time (default: now)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="print every write instead of sending it (the default)")
    mode.add_argument("--live", dest="dry_run", action="store_false", help="send the writes")
    return parser.parse_args(argv)


def main(argv=None, gh_runner=None) -> int:
    args = parse_args(argv)
    data = read_json(args.data, "data.json")
    if data is None:
        log(f"ERROR: {args.data} is not a readable data.json")
        return 1
    now = nightly.parse_iso(args.now) or datetime.datetime.now(datetime.timezone.utc)
    try:
        gaps = find_gaps(args.repo_root, data, read_json(args.store, "store.json"), now)
    except (ValueError, AssertionError) as exc:
        log(f"ERROR: {exc}; nothing read from GitHub or written")
        return 1
    gh = ghcli.Gh(args.repo, gh_runner, dry_run=args.dry_run) if gh_runner else ghcli.Gh(args.repo, dry_run=args.dry_run)
    result = GapFiler(gh, args.repo_root).run_gaps(gaps, now)
    print(summary(result, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
