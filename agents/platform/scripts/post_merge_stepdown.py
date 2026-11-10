#!/usr/bin/env python3
"""Undo an auto-merged fix that is still broken, and stop auto-merging its kind.

When the post-merge check finds a fix still broken, and that fix merged without
a human review (it carries `automation_rules.AUTOMERGE_LABEL`), this opens two
ordinary pull requests and says so once on the fix:

1. a revert that undoes the fix's diff, and
2. an edit of `.kube-agents/automation.yaml` adding a deny rule for that kind
   of fix in that environment (`automation_rules.with_deny`).

Both go through normal review. kube-agents never merges either, and closes,
reopens or edits nothing it did not open here.

Only with `KUBEAGENTS_AUTOMERGE=on`. Under `off` or `practice` nothing merged
unattended, so the post-merge report is the whole answer and this does
nothing. It runs in the shell sandbox, where `vcs_client` has its git, for the
post-merge driver to run with the record's repository, number, kind,
environment and outcome. The forge, not the caller, says whether the fix merged
and whether it carries the label.

Safe to run again: the branches are named after the fix's number, an existing
pull request on either is reused, and the comment is written once, found again
by `MARKER`.

The reference page is `docs/site/src/content/docs/reference/automation-rules.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent))

import automation_rules

STILL_BROKEN = "still_broken"
MARKER = "<!-- kube-agents:post-merge-step-down -->"
# Under the agent's branch namespace (`vcs_broker.AGENT_BRANCH_PREFIX`), so the
# broker will delete them once their pull requests close, as it does the agent's own.
REVERT_BRANCH = "platform-agent/revert-{number}"
STEP_DOWN_BRANCH = "platform-agent/automerge-step-down-{number}"
COMMENTS_LIMIT = 100
_TRUNCATED = "\0truncated"

Forge = Callable[[str, dict], dict]


class StepDownError(RuntimeError):
    pass


def _forge(repo: str) -> Forge:
    import vcs_client

    return lambda verb, payload: vcs_client.forge(verb, dict(payload), repository=repo)


def _existing_proposal(forge: Forge, branch: str) -> dict | None:
    answer = forge("proposal-list", {"source": branch, "state": "all", "limit": 1})
    proposals = answer.get("proposals") or []
    return proposals[0] if proposals else None


def _login(login: object) -> str:
    # The App suffix comes off a comment's author (`translate.actor`); take it off both sides.
    return str(login or "").strip().removesuffix("[bot]").casefold()


def _marker_comment(forge: Forge, number: int, viewer: str) -> str | None:
    answer = forge("proposal-view", {"number": number, "comments": True, "limit": COMMENTS_LIMIT})
    if answer.get("commentsTruncated"):
        return _TRUNCATED
    for comment in answer.get("comments") or []:
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else author
        if MARKER in str(comment.get("body") or "") and _login(login) == _login(viewer):
            return str(comment.get("url") or "")
    return None


def _open(forge: Forge, repo: str, branch: str, base: str, title: str, body: str,
          write: Callable[[Path, Any, str, str], None]) -> str:
    """Cut `branch` from `base`, let `write` change the copy, publish, and open the pull request."""
    import vcs_client

    held = forge("branch-view", {"branch": branch}).get("branch") or {}
    if held.get("exists"):
        # An earlier run published the branch and stopped before the pull
        # request; open it from what is there rather than refuse forever.
        answer = forge("proposal-create", {"title": title, "body": body, "source": branch, "target": base})
        return str(answer["proposal"]["url"])
    try:
        # Of `base`, the branch the fix merged into, which need not be the default.
        cloned = vcs_client.clone(repo, branch=base, force=True, key=branch)
        tree = Path(cloned["path"])
        switched = vcs_client.branch(repo, branch, key=branch)
        if switched["exitCode"] != 0:
            raise StepDownError(f"{branch}: could not take the branch: {switched['stderr']}")
        write(tree, vcs_client, repo, branch)
        vcs_client.publish(repo, target=base, key=branch)
    finally:
        try:
            vcs_client.discard(repo, key=branch)
        except vcs_client.VcsError:
            pass
    answer = forge("proposal-create", {"title": title, "body": body, "source": branch, "target": base})
    return str(answer["proposal"]["url"])


def _revert_writer(fix: dict, number: int, diff: str) -> Callable[[Path, Any, str, str], None]:
    """Undo the pull request's own diff, rather than `git revert` its merge commit.

    After a rebase merge of several commits the merge commit holds only the
    last of them, and a squash and a rebase both leave one parent, so the
    merge commit alone cannot say how much to undo. The pull request's diff is
    the whole change whichever way it merged.
    """

    def write(tree: Path, vcs_client: Any, repo: str, branch: str) -> None:
        if not diff.strip():
            raise StepDownError(f"#{number} has no diff to undo")
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as handle:
            handle.write(diff if diff.endswith("\n") else diff + "\n")
        try:
            done = vcs_client.local_git(tree, "apply", "--reverse", "--index", handle.name, check=False)
        finally:
            os.unlink(handle.name)
        if done.returncode != 0:
            raise StepDownError(
                f"its changes no longer undo cleanly on the base branch; it may already be "
                f"reverted or changed since ({done.stderr.strip()[:300]})"
            )
        staged = vcs_client.local_git(tree, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            raise StepDownError("its changes are already gone from the base branch")
        vcs_client.commit(
            f'Revert "{fix.get("title") or f"#{number}"}"\n\n'
            f"This undoes the changes of {fix.get('url') or f'#{number}'} "
            f"(merge commit {fix.get('mergeCommit')}).",
            spec=repo,
            key=branch,
        )

    return write


def _step_down_writer(kind: str, environment: str, fix_url: str) -> Callable[[Path, Any, str, str], None]:
    def write(tree: Path, vcs_client: Any, repo: str, branch: str) -> None:
        path = tree / automation_rules.RULES_FILE
        if automation_rules.load(tree).status != automation_rules.LOADED:
            raise StepDownError(f"{automation_rules.RULES_FILE} is absent or refused on the base branch")
        # `newline=""` both ways: a CRLF file comes back CRLF, not rewritten whole.
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        try:
            edited = automation_rules.with_deny(
                text, kind, environment, f"Added by kube-agents: {fix_url} was still broken after merge."
            )
        except automation_rules.RulesError as exc:
            raise StepDownError(str(exc)) from exc
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(edited)
        vcs_client.commit(
            f"chore(kube-agents): stop auto-merging {kind} fixes in {environment}",
            paths=[automation_rules.RULES_FILE],
            spec=repo,
            key=branch,
        )

    return write


def step_down(
    repo: str,
    number: int,
    kind: str,
    environment: str,
    outcome: str,
    *,
    automerge: str | None = None,
    forge: Forge | None = None,
    open_proposal: Callable[..., str] | None = None,
) -> dict:
    """Act on one post-merge outcome. Returns what was done, as the CLI prints it."""
    if automerge is None:
        import kubeagents_switches

        automerge = kubeagents_switches.switch("AUTOMERGE")
    if automerge != "on":
        return {"action": "none", "reason": f"KUBEAGENTS_AUTOMERGE is {automerge}; report only"}
    if outcome != STILL_BROKEN:
        return {"action": "none", "reason": f"outcome is {outcome}"}
    forge = forge or _forge(repo)
    open_proposal = open_proposal or _open
    fix = forge("proposal-view", {"number": number}).get("proposal") or {}
    if fix.get("state") != "merged" or not fix.get("mergeCommit"):
        return {"action": "none", "reason": f"#{number} is {fix.get('state') or 'unknown'}, not merged"}
    if automation_rules.AUTOMERGE_LABEL not in (fix.get("labels") or []):
        return {"action": "none", "reason": f"#{number} has no {automation_rules.AUTOMERGE_LABEL} label; a person merged it"}
    viewer = str((forge("identity", {}).get("identity") or {}).get("login") or "")
    if not viewer:
        # Without it the marker cannot be told from a copy someone else wrote,
        # and every run would comment again.
        return {"action": "retry", "reason": "the forge did not say who this install is"}
    already = _marker_comment(forge, number, viewer)
    if already == _TRUNCATED:
        return {"action": "none", "reason": f"#{number} has more comments than one read returns; not acting twice"}
    if already is not None:
        return {"action": "none", "reason": "already stepped down", "comment": already}

    fix_url = str(fix.get("url") or f"#{number}")
    base = str(fix.get("target") or "")
    result: dict[str, Any] = {"action": "stepped-down", "fix": fix_url}
    # A `StepDownError` is a refusal that will repeat (changes that no longer undo, a rules
    # file this cannot edit) and is said in the comment. Anything else -- the
    # broker or the forge not answering -- is retried on the next run, so the
    # comment, whose marker ends the retries, waits for it.
    refused: dict[str, str] = {}
    errors: list[str] = []

    revert_branch = REVERT_BRANCH.format(number=number)
    try:
        existing = _existing_proposal(forge, revert_branch)
        if existing:
            result["revert"] = existing.get("url")
        else:
            diff = str(forge("proposal-view", {"number": number, "diff": True}).get("diff") or "")
            result["revert"] = open_proposal(
                forge, repo, revert_branch, base,
                f'Revert "{fix.get("title") or f"#{number}"}"',
                f"{fix_url} merged without a human review, and the post-merge check found the "
                f"problem still there. This undoes its changes (merge commit {fix['mergeCommit']}).\n\n"
                "Review it like any other change. kube-agents does not merge it.",
                _revert_writer(fix, number, diff),
            )
    except StepDownError as exc:
        refused["revert"] = str(exc)
    except Exception as exc:  # noqa: BLE001 -- one failure must not stop the other pull request
        errors.append(f"revert: {exc}")

    step_down_branch = STEP_DOWN_BRANCH.format(number=number)
    try:
        existing = _existing_proposal(forge, step_down_branch)
        if existing:
            result["stepDown"] = existing.get("url")
        else:
            result["stepDown"] = open_proposal(
                forge, repo, step_down_branch, base,
                f"Stop auto-merging {kind} fixes in {environment}",
                f"{fix_url} merged without a human review and was still broken after merge, so "
                f"this adds a deny rule for `{kind}` in `{environment}` to "
                f"`{automation_rules.RULES_FILE}`. Until it merges, fixes of this kind can still "
                "merge on their own. Remove the rule later to allow them again.",
                _step_down_writer(kind, environment, fix_url),
            )
    except StepDownError as exc:
        refused["stepDown"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"step-down: {exc}")

    if refused:
        result["refused"] = refused
    if errors:
        result["errors"] = errors
        result["action"] = "retry"
        return result
    opened = []
    if result.get("revert"):
        opened.append(f"a revert ({result['revert']})")
    if result.get("stepDown"):
        opened.append(f"a pull request to stop auto-merging {kind} fixes in {environment} ({result['stepDown']})")
    line = f"Still broken after merge, so I opened {' and '.join(opened)}." if opened else "Still broken after merge."
    if "revert" in refused:
        line += f" I did not open a revert: {refused['revert']}."
    if "stepDown" in refused:
        line += f" I did not change {automation_rules.RULES_FILE}: {refused['stepDown']}."
    answer = forge("proposal-comment", {"number": number, "body": f"{line}\n\n{MARKER}"})
    result["comment"] = (answer.get("comment") or {}).get("url")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--kind", required=True, help="the fix type, e.g. no-pdb")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--outcome", required=True)
    args = parser.parse_args(argv)
    result = step_down(args.repo, args.number, args.kind, args.environment, args.outcome)
    print(json.dumps(result, indent=2))
    return 1 if result["action"] == "retry" else 0


if __name__ == "__main__":
    sys.exit(main())
