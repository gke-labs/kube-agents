#!/usr/bin/env python3
"""Offer auto-merge for a kind of fix once it has earned it in one repository's dev.

For the post-merge driver to run in the shell sandbox once a fix's check
passes and `clean_merges.ready_in` says the kind has enough clean merges in the
fix's own repository and environment, with the fix's repository, number, kind,
environment and that entry's clean and days counts. Trust is per repository:
merges in one repository never make an offer in another. This opens a
DRAFT pull request in the fix's repository adding an allow rule for that kind
and environment to its `.kube-agents/automation.yaml` (creating the file when
absent), and comments once on the fix to link it. Both say how many merged in
that repository. Merging the draft is how the customer says yes; closing it
says no.

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
    text = repo.strip().removesuffix("/").removesuffix(".git")
    if "://" in text:
        text = text.split("://", 1)[1].split("/", 1)[-1]
    elif "@" in text.split("/", 1)[0] and ":" in text:
        text = text.split(":", 1)[1]
    return text or repo


def _allow_writer(kind: str, environment: str) -> Callable[[Path, Any, str, str], None]:
    def write(tree: Path, vcs_client: Any, repo: str, branch: str) -> None:
        rules = automation_rules.load(tree)
        path = tree / automation_rules.RULES_FILE
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
    clean: int,
    days: int,
    *,
    automerge: str | None = None,
    forge: Forge | None = None,
    open_proposal: Callable[..., str] | None = None,
) -> dict:
    """Make the offer on fix `number`, if it is due. Returns what was done, as the CLI prints it.

    `clean` and `days` are `clean_merges.ready_in`'s counts for the kind in
    `repo` and `environment`; both thresholds are checked here as well as there.
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
    if clean < clean_merges.CLEAN_MERGES_NEEDED or days < clean_merges.DAYS_NEEDED:
        return {
            "action": "none",
            "reason": f"{clean} clean merges on {days} days; {clean_merges.CLEAN_MERGES_NEEDED} on "
            f"{clean_merges.DAYS_NEEDED} needed",
        }
    forge = forge or post_merge_stepdown._forge(repo)
    open_proposal = open_proposal or post_merge_stepdown._open
    branch = OFFER_BRANCH.format(kind=kind, environment=environment)
    fix = forge("proposal-view", {"number": number}).get("proposal") or {}
    fix_url = str(fix.get("url") or f"#{number}")
    name = automation_rules.FIX_TYPE_NAMES.get(kind, f"{kind} fixes")

    if post_merge_stepdown._existing_proposal(forge, branch):
        return {"action": "none", "reason": "already offered"}
    if fix.get("state") != "merged":
        return {"action": "none", "reason": f"#{number} is {fix.get('state') or 'unknown'}, not merged"}
    try:
        url = open_proposal(
            forge, repo, branch, str(fix.get("target") or ""),
            f"Auto-merge {name} in {environment}",
            f"{clean} {name} have merged in {repo_name(repo)} with nobody changing them, passed the after-merge "
            "check, and were not undone. Merging this pull request adds one allow rule to "
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
        f"{name[0].upper()}{name[1:]}: {clean} merged in {repo_name(repo)}, none changed or undone. "
        f"Auto-merge fixes like this in {environment}? I opened a draft pull request that adds them "
        f"to your rules file: {url}. Merging it turns that on; closing it means no, and I won't ask again."
    )
    answer = forge("proposal-comment", {"number": number, "body": f"{line}\n\n{MARKER}"})
    return {"action": "offered", "offer": url, "comment": (answer.get("comment") or {}).get("url")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True, type=int, help="the merged fix to comment on")
    parser.add_argument("--kind", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--clean", required=True, type=int, help="clean merges of the kind in this repository there")
    parser.add_argument("--days", required=True, type=int, help="the distinct days they merged on")
    args = parser.parse_args(argv)
    print(json.dumps(offer(args.repo, args.number, args.kind, args.environment, args.clean, args.days), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
