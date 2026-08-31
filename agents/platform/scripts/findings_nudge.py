#!/usr/bin/env python3
"""The morning nudge: a short chat message naming the top critical findings.

Backs the ``findings-morning-nudge`` cron job, which runs with ``no_agent:
true`` and ``deliver: "chat"``. Its stdout is delivered verbatim, so everything
this prints is what the user reads, and printing nothing relays nothing.

No model turn, because there is no judgement to make: the ordering is the
queue's (`GET /v1/findings/ranked`), the severity is computed by the rubric at
registration, and the recommendation was written by whoever found the thing.
What is left is counting and formatting.

This is §7.2 of `docs/designs/inventory-findings-queue.md` minus two things.
The design's nudge links to a backlog document holding the whole queue; that
publisher (§7.1) is not built, so the message says how many findings it did not
name rather than pointing at a list of them. And the design gates every message
on the list having changed, paired with a weekly message so a silent week
cannot be confused with a broken job. Here the gate applies only to a morning
that names no critical the last sweep still saw: such a critical is repeated
daily until it is fixed, which makes the weekly floor unnecessary for the case
that would cost something to lose.
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:8699"
TIMEOUT_SECONDS = 30

# The user asked for the top two. The count of what is left is printed either
# way, so raising this widens the message rather than revealing anything new.
TOP_N = 2

# `deliver: "chat"` relays this through a Chat Agent turn. Without a heading the
# empty-queue message is one greeting-shaped sentence, which reads as
# conversation rather than as a report to reproduce.
HEADING = "Findings queue — morning nudge"


def _request(endpoint: str, path: str, body: dict | None = None, method: str = "") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{endpoint}{path}", data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _where(finding: dict) -> str:
    cluster = finding.get("cluster") or ""
    # A cluster-scoped finding names the cluster as its object, and reading
    # `prod/prod` back is a puzzle rather than a location.
    parts = [cluster, finding.get("namespace") or "", finding.get("object") or ""]
    if parts[2] == cluster:
        parts = parts[:2]
    return "/".join(part for part in parts if part)


def compose(findings: list[dict]) -> str:
    """The message, from the ranked list. Pure, so the wording earns a test."""
    return f"{HEADING}\n\n{_body(findings)}"


def _rolled_up(finding: dict) -> bool:
    """§4.4: a provider-managed observation is not named, a provider-managed fault is.

    The fault exception turns on `actionable`, which the design defines as
    whether a next step exists rather than who takes it -- a support case counts.
    """
    return bool(finding.get("provider_managed")) and not finding.get("actionable", True)


def _managed_line(rolled_up: list[dict]) -> str:
    if not rolled_up:
        return ""
    return (
        f"\n\n{len(rolled_up)} provider-managed "
        f"{'item is' if len(rolled_up) == 1 else 'items are'} also on the list, with no next "
        "step the operator can take. Not named here."
    )


def _body(findings: list[dict]) -> str:
    if not findings:
        return "Good morning. The findings queue is empty."

    rolled_up = [f for f in findings if _rolled_up(f)]
    nameable = [f for f in findings if not _rolled_up(f)]
    criticals = [f for f in nameable if f.get("severity") == "critical"]
    if not criticals:
        if not nameable:
            return (
                f"Good morning. Nothing on the queue has a next step you can take: all "
                f"{len(findings)} open items are provider-managed observations."
            )
        # Never "no criticals are open" on its own: a rolled-up row can be
        # critical, and the queue would then hold one this message did not name.
        hidden = [f for f in rolled_up if f.get("severity") == "critical"]
        opener = (
            "No critical findings are open"
            if not hidden
            else f"No critical findings name work for you ({len(hidden)} provider-managed open)"
        )
        top = nameable[0]
        return (
            f"Good morning. {opener}, and {len(findings)} "
            f"in the queue. The highest is {top.get('severity')}: {top.get('title')} "
            f"({_where(top)})." + _managed_line(rolled_up)
        )

    lines = [
        f"Good morning. {len(criticals)} critical "
        f"{'finding is' if len(criticals) == 1 else 'findings are'} open, "
        f"{len(findings)} in the queue."
    ]
    for position, finding in enumerate(criticals[:TOP_N], start=1):
        action = (finding.get("recommendation") or {}).get("action") or ""
        lines.append(
            f"\n{position}. [{finding.get('rank_score')}] {finding.get('title')}"
            f"\n   {_where(finding)}"
            + (f"\n   {action}" if action else "")
        )

    remaining = len(criticals) - min(TOP_N, len(criticals))
    if remaining:
        lines.append(
            f"\n{remaining} more critical "
            f"{'finding' if remaining == 1 else 'findings'} not named here. "
            "Ask for the full list."
        )
    return "\n".join(lines) + _managed_line([f for f in findings if _rolled_up(f)])


PUBLISHER = "nudge"

# The rubric's confidence for "inferred from absence" (§5.2), as the ranked
# payload renders it.
CONFIDENCE_ABSENT = 0.6


def _still_seen(finding: dict) -> bool:
    return ((finding.get("rubric") or {}).get("C") or 1.0) > CONFIDENCE_ABSENT


def _last_posted_hash(endpoint: str) -> str | None:
    try:
        return (_request(endpoint, f"/v1/findings/publication/{PUBLISHER}") or {}).get("content_hash")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def main(argv: list[str] | None = None) -> int:
    endpoint = (os.environ.get("SESSION_KV_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")

    try:
        findings = _request(endpoint, "/v1/findings/ranked").get("findings") or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        detail = exc.read().decode("utf-8", "replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
        # Loudly, and with nothing on stdout: the scheduler turns a non-zero
        # exit into a delivered failure summary, and a run that could not read
        # the queue must not read as a quiet morning.
        sys.stderr.write(f"findings_nudge: could not read the queue at {endpoint}: {detail}\n")
        return 1

    named = [f for f in findings if f.get("severity") == "critical" and not _rolled_up(f)][:TOP_N]

    message = compose(findings)
    # The message itself is what "the list changed" is about, so hash that
    # rather than the findings it was built from.
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    # A critical the last complete sweep still saw is nagged about every
    # morning: the gate is for the quiet mornings, and it would otherwise lose a
    # critical to a delivery failure the script cannot observe. `C` is the only
    # shipped signal that a finding stopped being reported -- the absence rule
    # drops it to 0.6 -- and without that test a critical the user has fixed
    # keeps its floor severity and repeats forever.
    if not any(_still_seen(f) for f in named):
        try:
            if _last_posted_hash(endpoint) == digest:
                return 0
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Post anyway: a repeated message is a smaller failure than a lost one.
            sys.stderr.write(f"findings_nudge: could not read the last posted hash: {exc}\n")

    sys.stdout.write(message + "\n")
    sys.stdout.flush()

    try:
        _request(
            endpoint,
            f"/v1/findings/publication/{PUBLISHER}",
            {"target_kind": "chat", "content_hash": digest},
            method="PUT",
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stderr.write(f"findings_nudge: could not record the posted hash: {exc}\n")

    # After the message, and best-effort: `surface_count` and `surfaced_at` are
    # how a finding stuck at the top of the list becomes visible as its own
    # problem, and leaving them at zero would make that undetectable. A failure
    # here costs that bookkeeping, not the message, which is already out.
    for finding in named:
        try:
            _request(endpoint, f"/v1/findings/{finding['id']}/surfaced", {})
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            sys.stderr.write(f"findings_nudge: could not mark {finding.get('id')} surfaced: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
