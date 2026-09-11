"""File the tracking issue a new OUTAGE lacks, and tell it when the gate recovers.

Every shared break in the week of 2026-09-01 got an issue eventually -- #1171,
#1189, #1269, #1278 -- but hours after the first red, and the Chat message
meanwhile said "no issue yet, file one". This is the bot filing it:

    the state becomes OUTAGE
    and health.json names no tracking issue (case-notes.yaml has none)
    and no OPEN issue labelled `presubmit-gate` already names the same cases
    -> create one, labelled `presubmit-gate`, and say "Tracking #NNN"

The dedupe is against people: a human who filed first, with the case names
in the title or body, wins and the bot adopts their issue. A recovery gets
one comment ("Healthy again after Xh; bot will not close it"). The bot never
closes an issue -- a green gate is not proof the fixture is fixed, only that
three runs passed.

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
RECOVERY_COMMENT = "Healthy again after {lasted}; bot will not close it."
NO_EVIDENCE = "- (none recorded)"


def log(message: str) -> None:
    print(message, file=sys.stderr)


def names_all(text: str, cases: list[str]) -> bool:
    lowered = (text or "").lower()
    return bool(cases) and all(case.lower() in lowered for case in cases)


def as_issue(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict) or not payload.get("number"):
        return None
    return {"number": int(payload["number"]), "url": payload.get("html_url") or ""}


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


class Tracker:
    def __init__(self, gh):
        self.gh = gh

    def existing(self, cases: list[str]) -> dict | None:
        """An open `presubmit-gate` issue whose title or body names every
        failing case -- a human got there first."""
        issues = self.gh.call("GET", self.gh.path(OPEN_ISSUES_PATH), paginate=True)
        for issue in issues or []:
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            if names_all(f"{issue.get('title', '')}\n{issue.get('body', '')}", cases):
                return as_issue(issue)
        return None

    def ensure(self, health: dict, now, since_text: str, brief_link: str) -> dict | None:
        """The issue to cite: a human's if one names these cases, else a new one."""
        cases = list(health.get("failing_cases") or [])
        if not cases:
            return None
        found = self.existing(cases)
        if found:
            log(f"tracking issue: adopting open #{found['number']} (names every failing case)")
            return found
        payload = {"title": render_title(health, since_text), "body": render_body(health, since_text, brief_link), "labels": [LABEL]}
        created = as_issue(self.gh.call("POST", self.gh.path(ISSUES_PATH), payload))
        if created:
            log(f"tracking issue: filed #{created['number']}")
        return created

    def recovered(self, issue: dict, lasted: str) -> bool:
        number = (issue or {}).get("number")
        if not number:
            return False
        response = self.gh.call("POST", self.gh.path(COMMENTS_PATH.format(number=number)), {"body": RECOVERY_COMMENT.format(lasted=lasted)})
        return response is not None
