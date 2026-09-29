"""File the tracking issue a new OUTAGE lacks -- or the one a build-cluster
event owes its cluster owner -- and tell it when the gate recovers.

Every shared break in the week of 2026-09-01 got an issue eventually -- #1171,
#1189, #1269, #1278 -- but hours after the first red, and the Chat message
meanwhile said "no issue yet, file one". This is the bot filing it:

    the state becomes OUTAGE
    and health.json names no tracking issue (case-notes.yaml has none)
    and no OPEN issue labelled `presubmit-gate` already names the same cases
    -> create one, labelled `presubmit-gate`, and say "Tracking #NNN"

The second shape (#1478): on 2026-09-11 five nodes of the Prow build cluster
went NotReady and twelve runs died mid-run; nobody owning the cluster was
told. So:

    the condition becomes `lost_pods`
    and no OPEN issue labelled `presubmit-gate` already names every lost node
    -> create one addressed to the cluster owner, and say "Tracking #NNN"

The third shape (#1550): the hourly seeded-fleet scan finds a fixture role out
of its designed state on the same project two scans running, or on three
projects at once, and every case depending on it reds on the runs that
lease those projects. So:

    the condition becomes `fixture_drift`
    and no OPEN issue labelled `presubmit-gate` already names every drifted role
    -> create one addressed to the fleet owner, and say "Tracking #NNN"

The fourth shape (#1894): runs Prow killed at the job deadline with no
verdict, 3+ on 2+ pull requests in 2 hours, so nothing is graded and nobody
can pass. So:

    the condition becomes `deadline_kill`
    and no OPEN issue labelled `presubmit-gate` has "deadline" and "smoke" in its TITLE
    -> create one for whoever owns the gate, and say "Tracking #NNN"

The fifth shape (#1967): the hourly pool-state scan finds the same finding on
the same pool project two scans running, or on three projects at once. So:

    the condition becomes `pool_drift`
    and no OPEN issue labelled `presubmit-gate` has every finding id in its TITLE
    -> create one for the pool owner, with the repair per project

The dedupe is against people: a human who filed first, with the case names
(or the node names, or the role names) in the title or body -- or, for the
deadline kills and the pool findings, in the title -- wins and the bot adopts
their issue. A recovery gets one comment ("Healthy again after Xh; bot will not
close it"). The bot never closes an issue -- a green gate is not proof the
fixture is fixed, only that three runs passed, and the node events are still
worth reading after the pool has healed itself.

All GitHub traffic goes through ghcli.Gh (`gh api`, the workflow's token),
best-effort: a failure leaves the message at "no issue yet" and the next
change asks again. post_health.py owns when this is called; this module
owns what the issue says.
"""

from __future__ import annotations

import re
import sys

LABEL = "presubmit-gate"
JOB_NAME = "pull-kube-agents-smoke-test"
# Where the bot looks for a human's issue first.
OPEN_ISSUES_PATH = f"issues?labels={LABEL}&state=open&per_page=100"
ISSUES_PATH = "issues"
COMMENTS_PATH = "issues/{number}/comments"
# health.json's conditions this module files for besides an OUTAGE
# (health.py owns the vocabulary).
CONDITION_LOST_PODS = "lost_pods"
CONDITION_FIXTURE_DRIFT = "fixture_drift"
CONDITION_POOL_DRIFT = "pool_drift"
CONDITION_DEADLINE_KILL = "deadline_kill"
# The presubmit job's timeout in minutes (health.py PROW_JOB_TIMEOUT owns it).
DEADLINE_MINUTES = 360
# health.py RECOVERY_GREEN_RUNS, the bar the body quotes.
RECOVERY_RUNS = 3
# What an open issue's TITLE must carry to be adopted as the deadline-kill
# tracker. Title only: every bot-filed body names the job and quotes the
# evidence block, which mentions deadline kills whenever one sits in the
# window, so a body match would adopt a shared-break issue.
DEADLINE_KILL_NAMES = ("deadline", "smoke")
# The other direction: the deadline body quotes only the deadline evidence
# lines. health.py's evidence also carries per-case collapse lines whenever a
# case clears the shared-break floors, and a body naming those cases would
# be adopted as the tracker of a break that fires later.
DEADLINE_EVIDENCE_PREFIX = "deadline kills:"
# GitHub rejects a longer title; the node list is compacted, then dropped
# for a count, to stay under it.
TITLE_MAX_CHARS = 256
# GitHub's issue body limit; a pool-wide drift with a repair block per
# (project, finding) can pass it, and a body that is refused files nothing.
ISSUE_BODY_MAX_CHARS = 65536
POOL_DRIFT_PROJECTS_OMITTED = "- … and {count} more project(s) omitted to fit the issue; pool-state.json carries every project's repair command (docs/ci-health.md, The pool-state scan)"

TITLE = "Smoke gate outage: {count} {noun} failing on every PR since {since}"
CASE_NOUN = ("case", "cases")
BODY = """\
The smoke gate (`{job}`) is in OUTAGE: the cases below fail every repetition on every pull request that runs them, so a red on an open PR is not that PR's code.

**Failing cases**

{cases}

**Window:** since {since} ({since_iso}), {prs} PRs red so far.
**Class:** {cause} (`{condition}`).
**Evidence:**

{evidence}

Incident brief: {brief}

Filed automatically by the smoke health bot; edit freely. Fix PRs: reference this issue.
"""
LOST_PODS_TITLE = "Build cluster lost node(s) {nodes} at {when}: {runs} smoke runs on {prs} PRs died mid-run"
# When even the compacted names would push the title past GitHub's limit.
LOST_PODS_TITLE_MANY = "Build cluster lost {count} nodes at {when}: {runs} smoke runs on {prs} PRs died mid-run"
LOST_PODS_BODY = """\
The Prow build cluster (`kube-agents-prow`) lost the node(s) below at {when}; {runs} `{job}` runs on {prs} pull requests died mid-run. Each pod's last event is `NodeNotReady`, or the pod never uploaded a build log. Nothing about those pull requests is implied.

**Nodes**

{nodes}

**Window:** {window} ({window_iso}).
**Affected PRs:** {pr_list}.
**Evidence:**

{evidence}

**Advice for authors:** nothing about your change; `/retest` once new jobs are progressing.

Incident brief: {brief}

Filed automatically by the smoke health bot; the cluster owner should check the node events and autorepair; the bot will not close it.
"""
DEADLINE_KILL_TITLE = "Smoke gate outage: {runs} runs on {prs} PRs killed at the {minutes}-minute deadline with no verdict since {since}"
DEADLINE_KILL_BODY = """\
The smoke gate (`{job}`) is in OUTAGE: since {since} ({since_iso}), {runs} runs on {prs} pull requests ran to Prow's {minutes}-minute deadline and were killed with no eval verdict. Nothing is being graded, so no pull request can pass, and a red on an open PR from this window is not that PR's code.

**Window:** {window} ({window_iso}).
**Affected PRs:** {pr_list}.
**Evidence:**

{evidence}

**Advice for authors:** don't retest until the Chat space reports the gate healthy; a run started now ends the same way.

The rest of the evidence (any case collapsing underneath the kills) is in the brief.

**For whoever picks this up:** each killed run's `build-log.txt` shows how far its units got (on 2026-09-22 every unit reached the delegation ceiling, #1880); the gateway and dispatcher lines in the eval project's Cloud Logging say what the workers were doing. Recovery is reported after {recovery} runs with a verdict, green or red, on distinct PRs.

Incident brief: {brief}

Filed automatically by the smoke health bot; edit freely. Fix PRs: reference this issue.
"""
FIXTURE_DRIFT_TITLE = "Seeded fleet drift: {roles} out of designed state on {projects} pool {noun} since {since}"
PROJECT_NOUN = ("project", "projects")
FIXTURE_DRIFT_BODY = """\
The seeded fleet's fixture role(s) below are present but not in the state the cases depend on, on the pool projects named, and have been so on two consecutive hourly scans or on three projects at once. Nothing in the presubmit runs this check and nothing acts on a drift (decision 2026-09-14: evals v1 detects, does not act), so the cases that depend on a drifted role run against it and red on every run that leases one of these projects; that red is the fixture's, not the pull request's, and a retest is worth it only after the re-apply below.

**Drifted roles**

{roles}

**Per project** (the assertion from `bench/tf/fleet/fixtures.json`, and what the scan observed)

{projects}

**Window:** since {since} ({since_iso}); latest scan {scanned_at}.
**Evidence:**

{evidence}

**Reconcile:** re-apply `bench/tf/fleet` in each project named (`bench/tf/fleet/README.md`, "State and reconcile"), then wait for the next hourly scan or run `python3 scripts/verify_ci_pool_project.py --project-id <project>`.

Incident brief: {brief}

Filed automatically by the smoke health bot; the fleet owner should re-apply the stack in the projects named; the bot will not close it.
"""
POOL_DRIFT_TITLE = "Pool drift: {findings} on {projects} pool {noun} since {since}"
# Four finding ids pass GitHub's title limit; the count stands in.
POOL_DRIFT_TITLE_MANY = "Pool drift: {count} findings on {projects} pool {noun} since {since}"
# The bot's own pool-drift issue carries this, with the ids that fired, so it
# can re-find it when the title has fallen back to a count. The ids live in
# the marker rather than being matched over the body, because the body also
# quotes the evidence, which names findings that did not fire.
POOL_DRIFT_MARKER = "<!-- kube-agents-bot:pool-drift {findings} -->"
POOL_DRIFT_MARKER_RE = re.compile(r"<!-- kube-agents-bot:pool-drift ([^ >]*) -->")
POOL_DRIFT_BODY = """\
{marker}
The hourly pool-state scan (`scripts/eval_dashboard/pool_state.py`, which runs `scripts/verify_ci_pool_project.py`'s read-only checks against every pool project) found the pool projects below no longer shaped the way the verifier requires, on two consecutive hourly scans or on three projects at once. Nothing in the presubmit runs this check and nothing acts on a drift (decision 2026-09-14: evals v1 detects, does not act), so a run that leases one of these projects fails on the missing grant, API, key or cluster -- a 403 or a missing resource in the agent's transcript -- and that red is the pool's, not the pull request's. A retest is worth it only after the repair below.

**Findings**

{findings}

**Per project** (what the scan observed, and the command that repairs it; a line starting `# confirm first:` removes something and is yours to confirm)

{projects}

**Window:** since {since} ({since_iso}); latest scan {scanned_at}.
**Evidence:**

{evidence}

**Repair:** run each project's command above, then wait for the next hourly scan or run `python3 scripts/verify_ci_pool_project.py --project-id <project>` (docs/ci-health.md, "The pool-state scan"; docs/ci-pool-projects.md section 3 for the IAM bundles).

Incident brief: {brief}

Filed automatically by the smoke health bot; whoever holds the pool should run the repairs in the projects named; the bot will not close it.
"""
RECOVERY_COMMENT = "Healthy again after {lasted}; bot will not close it."
NO_EVIDENCE = "- (none recorded)"
UNKNOWN_NODE = "(node name not recorded)"


def log(message: str) -> None:
    print(message, file=sys.stderr)


def names_all(text: str, names: list[str]) -> bool:
    lowered = (text or "").lower()
    return bool(names) and all(name.lower() in lowered for name in names)


def as_issue(payload: dict | None, condition: str | None = None) -> dict | None:
    """{number, url, condition} for a GitHub issue payload; the condition
    records which incident kind the issue belongs to (health.issue_for)."""
    if not isinstance(payload, dict) or not payload.get("number"):
        return None
    issue = {"number": int(payload["number"]), "url": payload.get("html_url") or ""}
    if condition:
        issue["condition"] = condition
    return issue


def render_title(health: dict, since_text: str) -> str:
    cases = health.get("failing_cases") or []
    return TITLE.format(count=len(cases), noun=CASE_NOUN[len(cases) != 1], since=since_text)


def render_body(health: dict, since_text: str, brief_link: str) -> str:
    cases = health.get("failing_cases") or []
    incident = health.get("incident") or {}
    evidence = [f"- {line}" for line in health.get("evidence") or []]
    return BODY.format(
        job=JOB_NAME,
        cases="\n".join(f"- `{case}`" for case in cases),
        since=since_text,
        since_iso=health.get("since") or "?",
        prs=len(incident.get("prs") or []),
        cause=health.get("cause") or "shared break",
        condition=health.get("condition") or "?",
        evidence="\n".join(evidence) or NO_EVIDENCE,
        brief=brief_link,
    )


def compact_nodes(names: list[str]) -> str:
    """The node names for a title: one in full, several as their shared
    prefix plus the suffixes
    ("gke-kube-agents-prow-default-pool-eb220b2a-{er33,pe72,sgnk}")."""
    if not names:
        return UNKNOWN_NODE
    if len(names) == 1:
        return names[0]
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
    cut = prefix.rfind("-") + 1
    if cut == 0:
        return ", ".join(names)
    return prefix[:cut] + "{" + ",".join(name[cut:] for name in names) + "}"


def render_lost_pods_title(health: dict, when_text: str) -> str:
    incident = health.get("incident") or {}
    nodes = sorted(incident.get("nodes") or {})
    fields = {"when": when_text, "runs": incident.get("runs", 0), "prs": len(incident.get("prs") or [])}
    title = LOST_PODS_TITLE.format(nodes=compact_nodes(nodes), **fields)
    if len(title) > TITLE_MAX_CHARS:
        title = LOST_PODS_TITLE_MANY.format(count=len(nodes), **fields)
    return title


def render_lost_pods_body(health: dict, when_text: str, window_text: str, brief_link: str) -> str:
    incident = health.get("incident") or {}
    nodes = incident.get("nodes") or {}
    prs = incident.get("prs") or []
    evidence = [f"- {line}" for line in health.get("evidence") or []]
    return LOST_PODS_BODY.format(
        job=JOB_NAME,
        when=when_text,
        runs=incident.get("runs", 0),
        prs=len(prs),
        nodes="\n".join(f"- `{name}` ({count} {'run' if count == 1 else 'runs'})" for name, count in sorted(nodes.items())) or f"- {UNKNOWN_NODE}",
        window=window_text,
        window_iso=f"{incident.get('window_start') or '?'} – {incident.get('window_end') or '?'}",
        pr_list=", ".join(f"#{pr}" for pr in prs) or "none recorded",
        evidence="\n".join(evidence) or NO_EVIDENCE,
        brief=brief_link,
    )


def render_deadline_kill_title(health: dict, since_text: str) -> str:
    incident = health.get("incident") or {}
    return DEADLINE_KILL_TITLE.format(runs=incident.get("runs", 0), prs=len(incident.get("prs") or []), minutes=DEADLINE_MINUTES, since=since_text)


def render_deadline_kill_body(health: dict, since_text: str, window_text: str, brief_link: str) -> str:
    incident = health.get("incident") or {}
    prs = incident.get("prs") or []
    evidence = [f"- {line}" for line in health.get("evidence") or [] if str(line).startswith(DEADLINE_EVIDENCE_PREFIX)]
    return DEADLINE_KILL_BODY.format(
        job=JOB_NAME,
        since=since_text,
        # The same instant as `since_text`: the outage's first kill, not the
        # tick that declared the state after the third.
        since_iso=incident.get("first_kill") or incident.get("window_start") or health.get("since") or "?",
        runs=incident.get("runs", 0),
        prs=len(prs),
        minutes=DEADLINE_MINUTES,
        window=window_text,
        window_iso=f"{incident.get('window_start') or '?'} – {incident.get('window_end') or '?'}",
        pr_list=", ".join(f"#{pr}" for pr in prs) or "none recorded",
        evidence="\n".join(evidence) or NO_EVIDENCE,
        recovery=RECOVERY_RUNS,
        brief=brief_link,
    )


def render_fixture_drift_title(health: dict, since_text: str) -> str:
    incident = health.get("incident") or {}
    roles = list(incident.get("roles") or [])
    projects = list(incident.get("projects") or [])
    return FIXTURE_DRIFT_TITLE.format(
        roles=", ".join(roles) or "fixture role(s)",
        projects=len(projects),
        noun=PROJECT_NOUN[len(projects) != 1],
        since=since_text,
    )


def render_fixture_drift_body(health: dict, since_text: str, brief_link: str) -> str:
    incident = health.get("incident") or {}
    roles = list(incident.get("roles") or [])
    drift = incident.get("drift") or {}
    evidence = [f"- {line}" for line in health.get("evidence") or []]
    per_project = []
    for project in sorted(drift):
        per_project.append(f"- `{project}`")
        for role, lines in sorted((drift.get(project) or {}).items()):
            per_project.append(f"  - `{role}`")
            per_project.extend(f"    - {line}" for line in lines or ["(no detail recorded)"])
    return FIXTURE_DRIFT_BODY.format(
        roles="\n".join(f"- `{role}`" for role in roles) or "- (none recorded)",
        projects="\n".join(per_project) or "- (none recorded)",
        since=since_text,
        since_iso=health.get("since") or "?",
        scanned_at=incident.get("window_start") or "?",
        evidence="\n".join(evidence) or NO_EVIDENCE,
        brief=brief_link,
    )


def render_pool_drift_title(health: dict, since_text: str) -> str:
    incident = health.get("incident") or {}
    findings = list(incident.get("roles") or [])
    projects = list(incident.get("projects") or [])
    noun = PROJECT_NOUN[len(projects) != 1]
    title = POOL_DRIFT_TITLE.format(findings=", ".join(findings) or "finding(s)", projects=len(projects), noun=noun, since=since_text)
    if len(title) > TITLE_MAX_CHARS:
        title = POOL_DRIFT_TITLE_MANY.format(count=len(findings), projects=len(projects), noun=noun, since=since_text)
    return title


# The pool scan's evidence lines: the finding ids, or the pool scan's own
# wording ("<id> found on N pool project(s)", "pool-state scan …"). The
# fixture scan's lines say "drifted on" and name fixture roles, and the
# fixture tracker adopts an open issue whose body names its roles, so
# quoting them here would hand it the pool's ticket.
POOL_EVIDENCE_MARKERS = (" found on ", "pool-state", "pool drift")


def _pool_evidence(lines, findings):
    kept = []
    for line in lines:
        text = str(line)
        if any(finding in text for finding in findings) or any(marker in text.lower() for marker in POOL_EVIDENCE_MARKERS):
            kept.append(text)
    return kept


def render_pool_drift_body(health: dict, since_text: str, brief_link: str) -> str:
    incident = health.get("incident") or {}
    findings = list(incident.get("roles") or [])
    drift = incident.get("drift") or {}
    repairs = incident.get("repairs") or {}
    evidence = [f"- {line}" for line in _pool_evidence(health.get("evidence") or [], findings)]
    blocks = []
    for project in sorted(drift):
        block = [f"- `{project}`"]
        for finding, lines in sorted((drift.get(project) or {}).items()):
            block.append(f"  - `{finding}`")
            block.extend(f"    - {line}" for line in lines or ["(no detail recorded)"])
            repair = (repairs.get(project) or {}).get(finding)
            if repair:
                block.append("    - repair:")
                block.append("      ```")
                block.extend(f"      {line}" for line in str(repair).splitlines())
                block.append("      ```")
        blocks.append("\n".join(block))

    def render(kept):
        omitted = len(blocks) - len(kept)
        projects = list(kept) + ([POOL_DRIFT_PROJECTS_OMITTED.format(count=omitted)] if omitted else [])
        return POOL_DRIFT_BODY.format(
            marker=POOL_DRIFT_MARKER.format(findings=",".join(findings)),
            findings="\n".join(f"- `{finding}`" for finding in findings) or "- (none recorded)",
            projects="\n".join(projects) or "- (none recorded)",
            since=since_text,
            since_iso=health.get("since") or "?",
            scanned_at=incident.get("window_start") or "?",
            evidence="\n".join(evidence) or NO_EVIDENCE,
            brief=brief_link,
        )

    # Whole projects are dropped from the end until the body fits GitHub's
    # limit; the findings list, the evidence and the marker always stay.
    kept = list(blocks)
    body = render(kept)
    while len(body) > ISSUE_BODY_MAX_CHARS and kept:
        kept.pop()
        body = render(kept)
    return body


class Tracker:
    def __init__(self, gh):
        self.gh = gh

    def existing(self, names: list[str], title_only: bool = False, marker: re.Pattern | None = None) -> dict | None:
        """An open `presubmit-gate` issue whose title or body names every
        one of `names` (the failing cases, or the lost nodes) -- a human got
        there first. `title_only` for names too common in bot-filed bodies;
        `marker` for the bot's own issue of one kind, matched on the ids its
        marker carries and nothing else in the body."""
        issues = self.gh.call("GET", self.gh.path(OPEN_ISSUES_PATH), paginate=True)
        for issue in issues or []:
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            body = issue.get("body") or ""
            if marker is not None:
                found = marker.search(body)
                if found and set(names) <= set(found.group(1).split(",")):
                    return as_issue(issue)
                continue
            text = issue.get("title", "") if title_only else f"{issue.get('title', '')}\n{body}"
            if names_all(text, names):
                return as_issue(issue)
        return None

    def ensure(self, health: dict, now, since_text: str, brief_link: str, window_text: str | None = None) -> dict | None:
        """The issue to cite: a human's if one names these cases (or, for
        lost pods, these nodes; for deadline kills, the two title words),
        else a new one. `since_text` is the incident's start on the reader's
        clock; `window_text` the span of the losses or kills, for those two
        bodies."""
        condition = health.get("condition")
        if condition == CONDITION_LOST_PODS:
            return self._ensure_lost_pods(health, since_text, window_text or since_text, brief_link)
        if condition == CONDITION_FIXTURE_DRIFT:
            return self._ensure_fixture_drift(health, since_text, brief_link)
        if condition == CONDITION_POOL_DRIFT:
            return self._ensure_pool_drift(health, since_text, brief_link)
        if condition == CONDITION_DEADLINE_KILL:
            return self._ensure_deadline_kill(health, since_text, window_text or since_text, brief_link)
        cases = list(health.get("failing_cases") or [])
        if not cases:
            return None
        found = self.existing(cases)
        if found:
            log(f"tracking issue: adopting open #{found['number']} (names every failing case)")
            return dict(found, condition=condition) if condition else found
        payload = {"title": render_title(health, since_text), "body": render_body(health, since_text, brief_link), "labels": [LABEL]}
        created = as_issue(self.gh.call("POST", self.gh.path(ISSUES_PATH), payload), condition)
        if created:
            log(f"tracking issue: filed #{created['number']}")
        return created

    def _ensure_lost_pods(self, health: dict, when_text: str, window_text: str, brief_link: str) -> dict | None:
        nodes = sorted((health.get("incident") or {}).get("nodes") or {})
        found = self.existing(nodes) if nodes else None
        if found:
            log(f"tracking issue: adopting open #{found['number']} (names every lost node)")
            return dict(found, condition=CONDITION_LOST_PODS)
        payload = {
            "title": render_lost_pods_title(health, when_text),
            "body": render_lost_pods_body(health, when_text, window_text, brief_link),
            "labels": [LABEL],
        }
        created = as_issue(self.gh.call("POST", self.gh.path(ISSUES_PATH), payload), CONDITION_LOST_PODS)
        if created:
            log(f"tracking issue: filed #{created['number']} for the cluster owner")
        return created

    def _ensure_deadline_kill(self, health: dict, since_text: str, window_text: str, brief_link: str) -> dict | None:
        found = self.existing(list(DEADLINE_KILL_NAMES), title_only=True)
        if found:
            log(f"tracking issue: adopting open #{found['number']} (its title names the deadline kills)")
            return dict(found, condition=CONDITION_DEADLINE_KILL)
        payload = {
            "title": render_deadline_kill_title(health, since_text),
            "body": render_deadline_kill_body(health, since_text, window_text, brief_link),
            "labels": [LABEL],
        }
        created = as_issue(self.gh.call("POST", self.gh.path(ISSUES_PATH), payload), CONDITION_DEADLINE_KILL)
        if created:
            log(f"tracking issue: filed #{created['number']} for the gate's deadline kills")
        return created

    def _ensure_fixture_drift(self, health: dict, since_text: str, brief_link: str) -> dict | None:
        roles = sorted((health.get("incident") or {}).get("roles") or [])
        found = self.existing(roles) if roles else None
        if found:
            log(f"tracking issue: adopting open #{found['number']} (names every drifted role)")
            return dict(found, condition=CONDITION_FIXTURE_DRIFT)
        payload = {
            "title": render_fixture_drift_title(health, since_text),
            "body": render_fixture_drift_body(health, since_text, brief_link),
            "labels": [LABEL],
        }
        created = as_issue(self.gh.call("POST", self.gh.path(ISSUES_PATH), payload), CONDITION_FIXTURE_DRIFT)
        if created:
            log(f"tracking issue: filed #{created['number']} for the fleet owner")
        return created

    def _ensure_pool_drift(self, health: dict, since_text: str, brief_link: str) -> dict | None:
        # Title only: bot-filed bodies quote the evidence, which carries
        # every finding line whether or not it fires.
        findings = sorted((health.get("incident") or {}).get("roles") or [])
        found = self.existing(findings, title_only=True) if findings else None
        if not found and findings:
            # The bot's own, when its title fell back to a count.
            found = self.existing(findings, marker=POOL_DRIFT_MARKER_RE)
        if found:
            log(f"tracking issue: adopting open #{found['number']} (it names every finding)")
            return dict(found, condition=CONDITION_POOL_DRIFT)
        payload = {
            "title": render_pool_drift_title(health, since_text),
            "body": render_pool_drift_body(health, since_text, brief_link),
            "labels": [LABEL],
        }
        created = as_issue(self.gh.call("POST", self.gh.path(ISSUES_PATH), payload), CONDITION_POOL_DRIFT)
        if created:
            log(f"tracking issue: filed #{created['number']} for the pool owner")
        return created

    def recovered(self, issue: dict, lasted: str) -> bool:
        number = (issue or {}).get("number")
        if not number:
            return False
        response = self.gh.call("POST", self.gh.path(COMMENTS_PATH.format(number=number)), {"body": RECOVERY_COMMENT.format(lasted=lasted)})
        return response is not None
