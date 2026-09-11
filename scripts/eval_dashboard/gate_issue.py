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

The dedupe is against people: a human who filed first, with the case names
(or the node names) in the title or body, wins and the bot adopts their
issue. A recovery gets one comment ("Healthy again after Xh; bot will not
close it"). The bot never closes an issue -- a green gate is not proof the
fixture is fixed, only that three runs passed, and the node events are still
worth reading after the pool has healed itself.

All GitHub traffic goes through ghcli.Gh (`gh api`, the workflow's token),
best-effort: a failure leaves the message at "no issue yet" and the next
change asks again. post_health.py owns when this is called; this module
owns what the issue says.
"""

from __future__ import annotations

import sys

LABEL = "presubmit-gate"
JOB_NAME = "pull-kube-agents-smoke-test"
# Where the bot looks for a human's issue first.
OPEN_ISSUES_PATH = f"issues?labels={LABEL}&state=open&per_page=100"
ISSUES_PATH = "issues"
COMMENTS_PATH = "issues/{number}/comments"
# health.json's condition this module files for besides an OUTAGE
# (health.py owns the vocabulary).
CONDITION_LOST_PODS = "lost_pods"
# GitHub rejects a longer title; the node list is compacted, then dropped
# for a count, to stay under it.
TITLE_MAX_CHARS = 256

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


class Tracker:
    def __init__(self, gh):
        self.gh = gh

    def existing(self, names: list[str]) -> dict | None:
        """An open `presubmit-gate` issue whose title or body names every
        one of `names` (the failing cases, or the lost nodes) -- a human got
        there first."""
        issues = self.gh.call("GET", self.gh.path(OPEN_ISSUES_PATH), paginate=True)
        for issue in issues or []:
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            if names_all(f"{issue.get('title', '')}\n{issue.get('body', '')}", names):
                return as_issue(issue)
        return None

    def ensure(self, health: dict, now, since_text: str, brief_link: str, window_text: str | None = None) -> dict | None:
        """The issue to cite: a human's if one names these cases (or, for
        lost pods, these nodes), else a new one. `since_text` is the
        incident's start on the reader's clock; `window_text` the span of
        the losses, for the lost-pod body."""
        condition = health.get("condition")
        if condition == CONDITION_LOST_PODS:
            return self._ensure_lost_pods(health, since_text, window_text or since_text, brief_link)
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

    def recovered(self, issue: dict, lasted: str) -> bool:
        number = (issue or {}).get("number")
        if not number:
            return False
        response = self.gh.call("POST", self.gh.path(COMMENTS_PATH.format(number=number)), {"body": RECOVERY_COMMENT.format(lasted=lasted)})
        return response is not None
