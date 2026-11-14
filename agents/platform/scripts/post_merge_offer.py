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
then. A run that stops between opening the draft and commenting leaves the
draft without the comment; the next run reads that as already offered.
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

    if post_merge_stepdown._existing_proposal(forge, branch):
        return {"action": "none", "reason": "already offered"}
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
    line = (
        f"{name[0].upper()}{name[1:]}: at least {clean_merges.CLEAN_MERGES_NEEDED} merged in {repo_name(repo)}, "
        "none changed or undone. "
        f"Auto-merge fixes like this in {environment}? I opened a draft pull request that adds them "
        f"to your rules file: {url}. Marking it ready and merging it turns that on; closing it means no, "
        "and I won't ask again."
    )
    answer = forge("proposal-comment", {"number": number, "body": f"{line}\n\n{MARKER}"})
    return {"action": "offered", "offer": url, "comment": (answer.get("comment") or {}).get("url")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True, type=int, help="the merged fix to comment on")
    parser.add_argument("--kind", required=True)
    parser.add_argument("--environment", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(offer(args.repo, args.number, args.kind, args.environment), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
