"""The post-merge check's follow-ups on an outcome (design gke-labs/kube-agents#5054).

:func:`run_followups` acts on each ``still_broken`` or ``verified_fixed`` record
once, whatever ``KUBEAGENTS_AUTOMERGE`` says; each sandbox script reads the
switch itself:

- ``still_broken`` runs ``post_merge_stepdown.py`` in the shell sandbox: a
  revert for a fix that merged on its own (its label decides, so a switch
  turned off after a bad auto-merge still gets one) and, with the switch
  ``on``, a deny rule.
- ``verified_fixed`` asks ``clean_merges.ready_in``, here in the agent pod
  where Session KV is, whether the kind has earned the offer in the fix's own
  repository and environment, and only then runs ``post_merge_offer.py``. The
  offer script takes no counts: the sandbox cannot read Session KV, so this
  read is the gate. The offer script does nothing while the switch is ``off``.

A follow-up that answers ``retry``, or does not answer, is tried again on later
runs, :data:`FOLLOWUP_ATTEMPTS` times in all. Some failures repeat on every run
(a diff too large to revert, a base the broker will not write to). When a
step-down reaches the cap, :func:`run_followups` comments once on the fix with
the step-down's marker, so the customer is told and ``post_merge_stepdown.py``
treats the fix as handled. A record needs its ``environment`` first; one
without it waits.

:func:`notice_reverts` stamps ``reverted_at`` once the revert a step-down
opened has merged. The ``post-merge-check`` cron job runs both
(``post_merge_outcome.tick``). No model is involved.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from post_merge_check import (
    READS_PER_TICK,
    RECORDS_TABLE,
    STATE_MERGED,
    VIEW_TIMEOUT_SECONDS,
    _now_text,
    _parse_time,
)
import post_merge_report
from post_merge_report import STILL_BROKEN, VERIFIED_FIXED

logger = logging.getLogger("post_merge_check")

STEPDOWN_SCRIPT = "post_merge_stepdown.py"
OFFER_SCRIPT = "post_merge_offer.py"
#: Where the sandbox image installs both scripts (``deploy/sandbox/Dockerfile``).
SANDBOX_SCRIPTS_DIR = "/opt/defaults/scripts"
SCRIPT_TIMEOUT_SECONDS = 300
#: How many times a follow-up is run before it is given up.
FOLLOWUP_ATTEMPTS = 5
#: Only outcomes reached this recently are followed up, so turning
#: ``KUBEAGENTS_AUTOMERGE`` on does not act on old outcomes.
FOLLOWUP_WINDOW = timedelta(days=1)
#: How long after a step-down its revert is watched for the merge.
REVERT_WATCH = timedelta(days=8)
#: ``post_merge_stepdown.REVERT_BRANCH`` and ``MARKER``; equal, as a test pins.
REVERT_BRANCH = "platform-agent/revert-{number}"
STEPDOWN_MARKER = "<!-- kube-agents:post-merge-step-down -->"
#: How much of the last failure the give-up comment quotes.
REASON_CHARS = 300
#: ``followup_detail``'s bound, and each string field's within it.
DETAIL_CHARS = 500
FIELD_CHARS = 200
#: How long after the outcome a give-up comment the forge refuses is tried again.
GIVE_UP_WINDOW = 2 * FOLLOWUP_WINDOW
RETRY = "retry"
FAILED = "failed"

FOLLOWUP_COLUMNS = ("url", "repo", "number", "kind", "environment", "outcome", "outcome_at", "followup_state",
                    "followup_attempts", "followup_detail")
FOLLOWUP_SQL = (
    f"SELECT {', '.join(FOLLOWUP_COLUMNS)} FROM {RECORDS_TABLE} "
    f"WHERE outcome IN ('{STILL_BROKEN}', '{VERIFIED_FIXED}') "
    f"AND (followup_state IS NULL OR followup_state = '{RETRY}') ORDER BY outcome_at DESC"
)

#: Runs one sandbox script: ``script(name, args)`` -> its JSON answer; raises when it did not answer.
SandboxScript = Callable[[str, list], dict]
#: ``ready_in(conn, repo=, kind=, environment=, now=)`` -> ``clean_merges.ready_in``'s answer.
ReadyIn = Callable[..., dict]
#: Posts one comment: ``comment(repo, number, body)``.
Comment = Callable[[str, int, str], None]
#: Whether ``marker`` is in a comment on the pull request: ``has_marker(repo, number, marker)`` -> True,
#: False, or None when its comments are too many to read in one page.
HasMarker = Callable[[str, int, str], "bool | None"]


def last_json_document(text: str) -> dict | None:
    """The last JSON object in ``text`` that starts a line, or None."""
    lines = (text or "").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("{"):
            try:
                found = json.loads("\n".join(lines[index:]))
            except ValueError:
                continue
            return found if isinstance(found, dict) else None
    return None


def sandbox_script(name: str, args: list) -> dict:
    """``python3 -I <sandbox scripts>/<name> <args>`` in the shell sandbox; its last JSON document."""
    import sandbox_exec

    done = sandbox_exec.run(["python3", "-I", f"{SANDBOX_SCRIPTS_DIR}/{name}", *map(str, args)],
                            timeout=SCRIPT_TIMEOUT_SECONDS, check=False)
    answer = last_json_document(done.stdout or "")
    if answer is None:
        raise RuntimeError(f"{name} exited {done.returncode} without an answer")
    return answer


def module_ready_in(conn: sqlite3.Connection, *, repo: str, kind: str, environment: str, now: datetime) -> dict:
    import clean_merges

    return clean_merges.ready_in(conn, repo=repo, kind=kind, environment=environment, now=now)


def forge_comment(repo: str, number: int, body: str) -> None:
    import forge

    forge.call("proposal-comment", {"number": number, "body": body}, repo, timeout_s=VIEW_TIMEOUT_SECONDS * 3)


def forge_has_marker(repo: str, number: int, marker: str) -> bool | None:
    """``post_merge_report.forge_has_marker``: only this install's own comments count."""
    return post_merge_report.forge_has_marker(repo, number, marker)


def _since(stamp: object, now: datetime) -> timedelta | None:
    parsed = _parse_time(stamp)
    return None if parsed is None else now - parsed


def _update(conn: sqlite3.Connection, url: str, updates: dict[str, object], what: str) -> bool:
    try:
        with conn:
            conn.execute(
                f"UPDATE {RECORDS_TABLE} SET {', '.join(f'{key} = ?' for key in updates)} WHERE url = ?",
                (*updates.values(), url),
            )
    except sqlite3.Error as exc:
        logger.warning("post-merge check: could not record %s for %s: %r", what, url, exc)
        return False
    return True


def _reason(answer: dict) -> str:
    """The last failure in one line: the script's errors, else its reason."""
    errors = answer.get("errors")
    text = "; ".join(map(str, errors)) if isinstance(errors, list) and errors else str(answer.get("reason") or "")
    text = " ".join(text.split())
    return (text[:REASON_CHARS - 1] + "…") if len(text) > REASON_CHARS else (text or "no reason given")


def give_up_text(record: dict, answer: dict) -> str:
    """The one comment on a fix whose step-down stopped being retried, saying what did and did not open."""
    target = f"a pull request to stop auto-merging {record['kind']} fixes in {record['environment']}"
    revert = answer.get("revert")
    if isinstance(revert, str) and revert:
        done = f"I opened a revert ({revert}), but could not open {target}"
        advice = ""
    else:
        done = f"I could not open a revert or {target}"
        advice = " Revert this change by hand if it is still needed."
    return (f"Still broken after merge. {done}, and stopped trying. The last attempt failed: "
            f"{_reason(answer)}.{advice}\n\n{STEPDOWN_MARKER}")


def _give_up(record: dict, answer: dict, comment: Comment, has_marker: HasMarker) -> str:
    """Tell the fix the step-down stopped, once. Returns what happened, for ``followup_detail``."""
    present = has_marker(record["repo"], int(record["number"]), STEPDOWN_MARKER)
    if present is None:
        return "not commented: more comments than one read returns"
    if present:
        return "already commented"
    comment(record["repo"], int(record["number"]), give_up_text(record, answer))
    return "commented"


def _detail(answer: dict) -> str:
    """``answer`` as ``followup_detail``: whole JSON within :data:`DETAIL_CHARS`, never cut mid-document."""
    bounded = {key: (value[:FIELD_CHARS] if isinstance(value, str) else value) for key, value in answer.items()}
    if isinstance(bounded.get("errors"), list):
        bounded["errors"] = [str(error)[:FIELD_CHARS] for error in bounded["errors"][:3]]
    text = json.dumps(bounded, sort_keys=True)
    if len(text) <= DETAIL_CHARS:
        return text
    keep = {key: bounded[key] for key in ("action", "revert", "gaveUp") if isinstance(bounded.get(key), str)}
    return json.dumps({**keep, "reason": _reason(answer)}, sort_keys=True)


def _load_detail(text: object) -> dict:
    try:
        found = json.loads(str(text or "{}"))
    except ValueError:
        return {"reason": str(text)}
    return found if isinstance(found, dict) else {"reason": str(text)}


def run_followups(conn: sqlite3.Connection, script: SandboxScript = sandbox_script,
                  ready_in: ReadyIn = module_ready_in, comment: Comment = forge_comment,
                  has_marker: HasMarker = forge_has_marker, now: datetime | None = None,
                  limit: int = READS_PER_TICK) -> dict[str, list[str]]:
    """Step a still-broken fix down, and offer auto-merge once a kind has earned it; see the module docstring.

    Writes ``followup_state`` (the script's ``action``, ``retry`` while it is
    retried, ``failed`` once given up), ``followup_attempts`` and
    ``followup_detail``. At most ``limit`` records are acted on a call. A
    step-down still in ``retry`` when its outcome turns :data:`FOLLOWUP_WINDOW`
    old is given up as if capped, and its give-up comment posted.
    """
    now = now or datetime.now(timezone.utc)
    seen: dict[str, list[str]] = {
        "stepped-down": [], "offered": [], "none": [], RETRY: [], FAILED: [], "waiting": [], "errors": []
    }
    ran = 0
    for values in conn.execute(FOLLOWUP_SQL).fetchall():
        record = dict(zip(FOLLOWUP_COLUMNS, values))
        url, attempts = record["url"], int(record["followup_attempts"] or 0)
        # An outcome_at that cannot be read counts as old: never acted on fresh, never retried for ever.
        age = _since(record["outcome_at"], now)
        expired = age is None or age > FOLLOWUP_WINDOW
        if expired and record["followup_state"] != RETRY:
            continue  # an old outcome is not followed up, so turning the switch on does not act on history
        if record["environment"] is None:
            seen["waiting"].append(url)
            continue
        if ran >= limit:
            break
        ran += 1
        if expired or attempts >= FOLLOWUP_ATTEMPTS:
            # Out of time or out of attempts: nothing more is run, only the give-up is said.
            action, answer = RETRY, _load_detail(record["followup_detail"])
            give_up = True
        else:
            base = ["--repo", record["repo"], "--number", record["number"], "--kind", record["kind"],
                    "--environment", record["environment"]]
            try:
                if record["outcome"] == STILL_BROKEN:
                    answer = script(STEPDOWN_SCRIPT, [*base, "--outcome", STILL_BROKEN])
                else:
                    earned = ready_in(conn, repo=record["repo"], kind=record["kind"],
                                      environment=record["environment"], now=now)
                    if earned.get("ready"):
                        answer = script(OFFER_SCRIPT, base)
                    else:
                        answer = {"action": "none", "reason": (f"{earned.get('clean', 0)} clean merges on "
                                                               f"{earned.get('days', 0)} days; not ready")}
                action = str(answer.get("action") or "none")
            except Exception as exc:  # noqa: BLE001 — retried, a bounded number of times
                logger.warning("post-merge check: the follow-up for %s failed: %r", url, exc)
                action, answer = RETRY, {"reason": str(exc)}
            if action == RETRY:
                attempts += 1
            give_up = action == RETRY and attempts >= FOLLOWUP_ATTEMPTS
        state = FAILED if give_up else action
        if give_up and record["outcome"] == STILL_BROKEN:
            try:
                answer = {**answer, "gaveUp": _give_up(record, answer, comment, has_marker)}
            except Exception as exc:  # noqa: BLE001 — the comment is tried again, for a bounded time
                logger.warning("post-merge check: could not tell %s the step-down stopped: %r", url, exc)
                if age is not None and age <= GIVE_UP_WINDOW:
                    state = RETRY
                else:
                    answer = {**answer, "gaveUp": f"not commented: {exc}"}
        if not _update(conn, url, {"followup_state": state, "followup_attempts": attempts,
                                   "followup_detail": _detail(answer)}, "the follow-up"):
            seen["errors"].append(url)
            continue
        if state == FAILED:
            seen["errors"].append(url)
        seen[state if state in seen else "none"].append(url)
    return seen


#: The newest pull request from a branch: ``view_branch(repo, branch)`` -> the proposal, or None.
ViewBranch = Callable[[str, str], "dict | None"]


def forge_branch_proposal(repo: str, branch: str) -> dict | None:
    import forge

    answer = forge.call("proposal-list", {"source": branch, "state": "all", "limit": 5}, repo, retry_transient=True,
                        timeout_s=VIEW_TIMEOUT_SECONDS)
    proposals = answer.get("proposals") or []
    return max(proposals, key=lambda p: int(p.get("number") or 0)) if proposals else None


def notice_reverts(conn: sqlite3.Connection, view_branch: ViewBranch = forge_branch_proposal,
                   now: datetime | None = None, limit: int = READS_PER_TICK) -> dict[str, list[str]]:
    """``reverted_at`` once the revert a step-down opened has merged, watched for :data:`REVERT_WATCH`."""
    now = now or datetime.now(timezone.utc)
    seen: dict[str, list[str]] = {"reverted": [], "waiting": [], "errors": []}
    rows = conn.execute(
        f"SELECT url, repo, number, outcome_at FROM {RECORDS_TABLE} WHERE followup_state = 'stepped-down' "
        "AND reverted_at IS NULL ORDER BY outcome_at DESC"
    ).fetchall()
    rows = [row for row in rows if (_since(row[3], now) or timedelta(0)) <= REVERT_WATCH][:limit]
    for url, repo, number, _ in rows:
        try:
            proposal = view_branch(repo, REVERT_BRANCH.format(number=number))
        except Exception as exc:  # noqa: BLE001 — read again next run
            logger.warning("post-merge check: could not read the revert of %s: %r", url, exc)
            seen["errors"].append(url)
            continue
        if proposal and proposal.get("state") == STATE_MERGED:
            if not _update(conn, url, {"reverted_at": str(proposal.get("closed") or _now_text(now))}, "the revert"):
                seen["errors"].append(url)
                continue
            seen["reverted"].append(url)
        else:
            seen["waiting"].append(url)
    return seen
