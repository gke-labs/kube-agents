#!/usr/bin/env python3
"""Offer auto-merge for a kind of fix once it has earned it in one repository's dev.

For the post-merge driver to run in the shell sandbox once a fix's check
passes and `clean_merges.ready_in`, read in the agent pod from Session KV, says
the kind is ready in the fix's own repository and environment. The counts are
never taken from the caller: the sandbox cannot read Session KV, and an
argument from it is model-controlled. So the text states the bar the kind met
(`clean_merges.CLEAN_MERGES_NEEDED` merges on `DAYS_NEEDED` days), which holds
whenever the driver calls this. Trust is per repository. This opens a DRAFT
pull request in the fix's repository adding an allow rule for that kind and
environment to its `.kube-agents/automation.yaml` (creating the file when
absent), and comments once on the fix to link it. Marking the draft ready and
merging it is how the customer says yes; closing it says no.

Only when `KUBEAGENTS_AUTOMERGE` is not `off`, and only in an environment in
`OFFER_ENVIRONMENTS`. Never twice for one kind and environment in a repository:
the draft's branch there is named after them, and any pull request ever opened on it, open,
merged or closed, ends the offer. A rules file that already allows or denies
the kind there gets no offer either.

Nothing calls this yet: the post-merge driver wires it, and the reference page
(`docs/site/src/content/docs/reference/automation-rules.md`) gains its section
then. A run that stops between opening the draft and commenting is finished
by the next run on the same fix: the draft's last line names the fix it was
made on, so the comment goes there once, behind the marker check, and never
on a later fix. A draft already closed or merged gets no late comment.
The marker check and the comment are two calls, so two runs finishing the
same fix at once could both comment, as with the step-down; the driver that
wires this must run one pass per fix at a time.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent))

import automation_rules
import clean_merges
import post_merge_stepdown
import repo_ref

OFFER_BRANCH = "platform-agent/automerge-offer-{kind}-{environment}"
# Auto-merge is offered for dev first; production gets a higher bar later.
OFFER_ENVIRONMENTS = frozenset({"dev"})
MARKER = "<!-- kube-agents:automerge-offer -->"
# The last line of the offer's body: which fix it was made on.
OFFERED_ON = "Offered on "

Forge = post_merge_stepdown.Forge


class NoOffer(RuntimeError):
    pass


def repo_name(repo: str) -> str:
    """``org/infra`` for ``https://github.com/org/infra.git``: the repository as a person reads it."""
    ref = repo_ref.try_parse(repo)
    return ref.path if ref is not None else repo


def _allow_writer(kind: str, environment: str) -> Callable[[Path, Any, str, str], None]:
    def write(tree: Path, vcs_client: Any, repo: str, branch: str) -> None:
        rules = automation_rules.load(tree)
        path = tree / automation_rules.RULES_FILE
        if rules.status == automation_rules.ABSENT and (path.exists() or path.parent.is_file()):
            # `load` reads a directory, or a file where the directory goes, as absent.
            raise NoOffer(f"{automation_rules.RULES_FILE} is not a file")
        if rules.status == automation_rules.ABSENT:
            path.parent.mkdir(parents=True, exist_ok=True)
            edited = automation_rules.new_file(kind, environment)
        elif rules.status != automation_rules.LOADED:
            raise NoOffer(f"{automation_rules.RULES_FILE} is refused ({rules.error})")
        else:
            if automation_rules.decide(rules, kind, environment).allowed:
                raise NoOffer(f"{automation_rules.RULES_FILE} already allows {kind} in {environment}")
            with path.open(encoding="utf-8", newline="") as handle:
                text = handle.read()
            try:
                # Refuses an allow a deny already overrides.
                edited = automation_rules.with_allow(text, kind, environment)
            except automation_rules.RulesError as exc:
                raise NoOffer(str(exc)) from exc
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(edited)
        vcs_client.commit(
            f"chore(kube-agents): auto-merge {kind} fixes in {environment}",
            paths=[automation_rules.RULES_FILE],
            spec=repo,
            key=branch,
        )

    return write


def offer(
    repo: str,
    number: int,
    kind: str,
    environment: str,
    *,
    automerge: str | None = None,
    forge: Forge | None = None,
    open_proposal: Callable[..., str] | None = None,
) -> dict:
    """Make the offer on fix `number`, if it is due. Returns what was done, as the CLI prints it.

    The driver checks `clean_merges.ready_in` before calling this; see the
    module docstring for why the counts are not an argument.
    """
    if automerge is None:
        import kubeagents_switches

        automerge = kubeagents_switches.switch("AUTOMERGE")
    if automerge == "off":
        return {"action": "none", "reason": "KUBEAGENTS_AUTOMERGE is off"}
    if environment not in OFFER_ENVIRONMENTS:
        return {"action": "none", "reason": f"auto-merge is offered in {', '.join(sorted(OFFER_ENVIRONMENTS))} only"}
    if kind not in automation_rules.KNOWN_FIX_TYPES:
        return {"action": "none", "reason": f"{kind} is not a fix kube-agents opens"}
    forge = forge or post_merge_stepdown._forge(repo)
    open_proposal = open_proposal or post_merge_stepdown._open
    branch = OFFER_BRANCH.format(kind=kind, environment=environment)
    fix = forge("proposal-view", {"number": number}).get("proposal") or {}
    fix_url = str(fix.get("url") or f"#{number}")
    name = automation_rules.FIX_TYPE_NAMES.get(kind, f"{kind} fixes")
    bar = f"{clean_merges.CLEAN_MERGES_NEEDED} {name} have merged on {clean_merges.DAYS_NEEDED} or more days"

    existing = post_merge_stepdown._existing_proposal(forge, branch)
    if existing:
        return _finish(forge, number, existing, fix_url, name, environment, repo)
    if fix.get("state") != "merged":
        return {"action": "none", "reason": f"#{number} is {fix.get('state') or 'unknown'}, not merged"}
    if (forge("branch-view", {"branch": branch}).get("branch") or {}).get("exists"):
        # `_open` would open a published branch as it stands, unchecked by the
        # writer, under a body that promises one allow rule; never for an offer.
        return {"action": "none", "reason": f"{branch} is published without a pull request; not offering from it"}
    try:
        url = open_proposal(
            forge, repo, branch, str(fix.get("target") or ""),
            f"Auto-merge {name} in {environment}",
            f"At least {bar} in {repo_name(repo)} with nobody changing them, passed the after-merge "
            "check, and were not undone. Marking this draft ready and merging it adds one allow rule to "
            f"`{automation_rules.RULES_FILE}`, so your pipeline may merge kube-agents' "
            f"`{kind}` fixes on clusters labelled `{environment}` without a review, while "
            "`KUBEAGENTS_AUTOMERGE` is `on`. kube-agents never merges anything itself.\n\n"
            "Close this pull request to say no; kube-agents will not offer it again.\n\n"
            f"{OFFERED_ON}{fix_url}",
            _allow_writer(kind, environment),
            draft=True,
        )
    except NoOffer as exc:
        return {"action": "none", "reason": str(exc)}
    except Exception:
        # Another run may have opened it between the check and now.
        if post_merge_stepdown._existing_proposal(forge, branch):
            return {"action": "none", "reason": "already offered"}
        raise
    return _comment(forge, number, url, name, environment, repo)


def _comment(forge: Forge, number: int, url: str, name: str, environment: str, repo: str) -> dict:
    line = (
        f"{name[0].upper()}{name[1:]}: at least {clean_merges.CLEAN_MERGES_NEEDED} merged in {repo_name(repo)}, "
        "none changed or undone. "
        f"Auto-merge fixes like this in {environment}? I opened a draft pull request that adds them "
        f"to your rules file: {url}. Marking it ready and merging it turns that on; closing it means no, "
        "and I won't ask again."
    )
    answer = forge("proposal-comment", {"number": number, "body": f"{line}\n\n{MARKER}"})
    return {"action": "offered", "offer": url, "comment": (answer.get("comment") or {}).get("url")}


def _finish(forge: Forge, number: int, existing: dict, fix_url: str, name: str, environment: str, repo: str) -> dict:
    """Post the comment a run left undone, when ``existing`` is a still-open
    offer this install opened, from a branch in ``repo`` itself, on this very
    fix, and the fix carries no offer comment from it yet.

    The branch name and the body are no proof of who opened it: anyone who can
    fork the repository can open a pull request from a same-named branch whose
    body ends with the fix's URL, and commenting would then present theirs as
    the offer. Such a draft only ends the offer, as before.
    """
    url = str(existing.get("url") or "")
    already = {"action": "none", "reason": "already offered", "offer": url}
    viewer = str((forge("identity", {}).get("identity") or {}).get("login") or "")
    if not viewer:
        # Without it neither the draft's author nor the marker can be told from someone else's.
        return {"action": "retry", "reason": "the forge did not say who this install is", "offer": url}
    body = str(existing.get("body") or "").rstrip().splitlines()
    # The whole last line, so the offer made on .../pull/12 is not finished on .../pull/123.
    if existing.get("state") != "open" or not body or body[-1].strip() != f"{OFFERED_ON}{fix_url}":
        return already
    author = existing.get("author")
    author = author.get("login") if isinstance(author, dict) else author
    source_repo = str(existing.get("sourceRepo") or "").casefold()
    if post_merge_stepdown._login(author) != post_merge_stepdown._login(viewer) or source_repo != repo_name(repo).casefold():
        return already
    found = post_merge_stepdown._marker_comment(forge, number, viewer, MARKER)
    if found == post_merge_stepdown._TRUNCATED:
        return {"action": "none", "reason": f"#{number} has more comments than one read returns; not commenting twice", "offer": url}
    if found is not None:
        return already
    return _comment(forge, number, url, name, environment, repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True, type=int, help="the merged fix to comment on")
    parser.add_argument("--kind", required=True)
    parser.add_argument("--environment", required=True)
    args = parser.parse_args(argv)
    result = offer(args.repo, args.number, args.kind, args.environment)
    print(json.dumps(result, indent=2))
    # As the step-down's CLI does: a retry is a run that did not finish.
    return 1 if result["action"] == "retry" else 0


if __name__ == "__main__":
    sys.exit(main())
