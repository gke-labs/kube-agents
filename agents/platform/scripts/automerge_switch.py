#!/usr/bin/env python3
"""Turn auto-merge on or off for one kind of fix, as a pull request to the rules file.

What the autonomy dial's per-kind switch runs, in the shell sandbox, once an
install admin requests or stops auto-merge for one repository (the dial's board
action in the agent pod runs it through `sandbox_exec`; nothing calls it yet).
For each repository named, it proposes the `.kube-agents/automation.yaml` change that makes the file say what the switch
now says, and changes nothing else:

- on: an allow rule for the kind in the environment (`automation_rules.with_allow`,
  or `new_file` when the repository has none);
- off: a deny rule for it (`automation_rules.with_deny`), since deny beats allow
  and the customer's own allow rule stays theirs to remove.

Nothing merges here: the pull request is how the customer's own review says yes.
A file that already says what the switch says gets no pull request. A switch
flipped back before its pull request merged closes that pull request (only one
this switch opened, on its own branch prefix) and, when the file already says
what the switch now says, opens nothing. Turning on takes out the deny rules an
earlier Stop auto-merge added (found by :data:`DENY_NOTE`, block-style files only); a
deny rule without that note is a person's decision, and blocks it.

With `--check`, it opens nothing: for each `<repository>#<number>` it reports
whether that pull request is still open, merged or closed, which is how the
dial learns that a pending switch has taken effect.

Only with `KUBEAGENTS_DIAL` on. The reference page is
`docs/site/src/content/docs/reference/automation-rules.md`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent))

import automation_rules
import post_merge_stepdown

# Under the agent's branch namespace, so the broker treats them as the agent's
# own. A new branch per flip: a name is never reused, so a merged or closed
# pull request's branch never stands in the way of the next one.
BRANCH_PREFIX = "platform-agent/automerge-switch-"
BRANCH = BRANCH_PREFIX + "{state}-{kind}-{environment}-{stamp}"

# The comment above every deny rule this switch adds, which is how Request auto-merge finds
# the rules it may take out again: a deny rule without it is a person's.
DENY_NOTE = "Added by kube-agents: auto-merge turned off on the dial."

ON = "on"
OFF = "off"
PROPOSED = "proposed"
BLOCKED = "blocked"

Forge = post_merge_stepdown.Forge


def _branch(state: str, kind: str, environment: str, now: float) -> str:
    return BRANCH.format(state=state, kind=kind, environment=environment, stamp=time.strftime("%Y%m%d%H%M%S", time.gmtime(now)))


def _wording(kind: str, environment: str, on: bool) -> tuple[str, str, str]:
    name = automation_rules.FIX_TYPE_NAMES.get(kind, f"{kind} fixes")
    if on:
        return (
            f"Auto-merge {name} in {environment}",
            f"chore(kube-agents): auto-merge {kind} fixes in {environment}",
            (
                f"Opened by kube-agents' per-kind auto-merge switch. Merging this pull request changes "
                f"`{automation_rules.RULES_FILE}` so that it allows kube-agents' `{kind}` fixes on clusters "
                f"labelled `{environment}`: your pipeline may then merge them without a review, while "
                "`KUBEAGENTS_AUTOMERGE` is `on`. kube-agents never merges anything itself. Check with your "
                "install's admins that they meant to turn this on.\n\nClose this pull request to leave it off."
            ),
        )
    return (
        f"Stop auto-merging {name} in {environment}",
        f"chore(kube-agents): stop auto-merging {kind} fixes in {environment}",
        (
            f"Opened by kube-agents' per-kind auto-merge switch. Merging this pull request adds one deny rule "
            f"to `{automation_rules.RULES_FILE}`, which outranks any allow rule, so your pipeline stops "
            f"merging kube-agents' `{kind}` fixes on clusters labelled `{environment}` without a review."
        ),
    )


def _edited(tree: Path, kind: str, environment: str, on: bool) -> tuple[str | None, str | None, str]:
    """`(new text, None, why)` for the file to write, or `(None, state, why)` when no pull request is due."""
    rules = automation_rules.load(tree)
    if rules.status == automation_rules.INVALID:
        return None, BLOCKED, f"{automation_rules.RULES_FILE} is refused ({rules.error})"
    allowed = rules.status == automation_rules.LOADED and automation_rules.decide(rules, kind, environment).allowed
    if allowed == on:
        return None, ON if on else OFF, f"{automation_rules.RULES_FILE} already {'allows' if on else 'does not allow'} it"
    if rules.status == automation_rules.ABSENT:
        # Only reached when turning on: an absent file allows nothing.
        return automation_rules.new_file(kind, environment), None, "new rules file"
    with (tree / automation_rules.RULES_FILE).open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    why = "allow rule added"
    if on and any(rule.matches(kind, environment) for rule in rules.deny):
        text, removed = _without_switch_denies(text, kind, environment)
        edited = automation_rules.parse(text)
        if not removed or edited.status != automation_rules.LOADED or edited.allow != rules.allow:
            return None, BLOCKED, f"{automation_rules.RULES_FILE} denies {kind} in {environment}; a person must remove that rule"
        if any(rule.matches(kind, environment) for rule in edited.deny):
            return None, BLOCKED, f"{automation_rules.RULES_FILE} denies {kind} in {environment}; a person must remove that rule"
        if automation_rules.decide(edited, kind, environment).allowed:
            return text, None, "switch's deny rule taken out"
        why = "switch's deny rule taken out, allow rule added"
    try:
        if on:
            return automation_rules.with_allow(text, kind, environment), None, why
        return (
            automation_rules.with_deny(text, kind, environment, DENY_NOTE),
            None,
            "deny rule added",
        )
    except automation_rules.RulesError as exc:
        return None, BLOCKED, str(exc)


def _without_switch_denies(text: str, kind: str, environment: str) -> tuple[str, int]:
    """`text` without the block-style deny rules `with_deny` wrote for this switch, and how many there were."""
    pattern = re.compile(
        rf"^([ \t]*)# {re.escape(DENY_NOTE)}\r?\n"
        rf"\1- fix: {re.escape(json.dumps(kind))}\r?\n"
        rf"\1  environments: \[{re.escape(json.dumps(environment))}\]\r?\n",
        re.MULTILINE,
    )
    return pattern.subn("", text)


def toggle(
    repo: str,
    kind: str,
    environment: str,
    on: bool,
    pending: int | None = None,
    *,
    forge: Forge | None = None,
    vcs: Any = None,
    now: Callable[[], float] = time.time,
) -> dict:
    """Make `repo`'s rules file say `on` for the kind there. Returns what was done, as the CLI prints it."""
    if vcs is None:
        import vcs_client as vcs
    forge = forge or post_merge_stepdown._forge(repo)
    state = ON if on else OFF
    out: dict[str, Any] = {"repo": repo}
    if pending:
        proposal = forge("proposal-view", {"number": pending}).get("proposal") or {}
        source = str(proposal.get("source") or "")
        if proposal.get("state") == "open" and source.startswith(BRANCH_PREFIX):
            if source.startswith(f"{BRANCH_PREFIX}{state}-{kind}-{environment}-"):
                return out | {"state": PROPOSED, "pr": str(proposal.get("url") or ""), "number": pending, "reason": "already proposed"}
            if source.startswith(f"{BRANCH_PREFIX}{OFF if on else ON}-{kind}-{environment}-"):
                forge("proposal-close", {"number": pending})
                out["closed"] = str(proposal.get("url") or f"#{pending}")
                # The branch is spent now; take it off the forge rather than
                # leave one behind per flip. Best effort: the broker refuses a
                # branch that moved past what the pull request carried.
                try:
                    forge("branch-delete", {"branch": source, "revision": str(proposal.get("sourceRevision") or "")})
                except Exception as exc:  # noqa: BLE001 — a kept branch is reported, not fatal
                    out["branchKept"] = f"{source}: {exc}"
    branch = _branch(state, kind, environment, now())
    try:
        cloned = vcs.clone(repo, force=True, key=branch)
        base = str(cloned["branch"])
        tree = Path(cloned["path"])
        text, settled, why = _edited(tree, kind, environment, on)
        if text is None:
            return out | {"state": settled, "reason": why}
        switched = vcs.branch(repo, branch, key=branch)
        if switched["exitCode"] != 0:
            return out | {"state": BLOCKED, "reason": f"could not take {branch}: {switched['stderr']}"}
        path = tree / automation_rules.RULES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        title, message, body = _wording(kind, environment, on)
        vcs.commit(message, paths=[automation_rules.RULES_FILE], spec=repo, key=branch)
        vcs.publish(repo, target=base, key=branch)
    finally:
        try:
            vcs.discard(repo, key=branch)
        except Exception as exc:  # noqa: BLE001 — a copy that could not be tidied is no reason to fail
            sys.stderr.write(f"automerge_switch: could not discard the copy of {repo}: {exc}\n")
    answer = forge("proposal-create", {"title": title, "body": body, "source": branch, "target": base})
    return out | {"state": PROPOSED, "pr": str(answer["proposal"]["url"]), "number": answer["proposal"].get("number"), "reason": why}


def check(repo: str, number: int, *, forge: Forge | None = None) -> dict:
    """Whether the switch's pull request `number` in `repo` is open, merged or closed."""
    forge = forge or post_merge_stepdown._forge(repo)
    proposal = forge("proposal-view", {"number": number}).get("proposal") or {}
    return {"repo": repo, "number": number, "state": str(proposal.get("state") or ""), "pr": str(proposal.get("url") or "")}


def _repo_arg(value: str) -> tuple[str, int | None]:
    repo, _, number = value.partition("#")
    if not repo or (number and not number.isdigit()):
        raise argparse.ArgumentTypeError("expected <repository> or <repository>#<pull request number>")
    return repo, int(number) if number else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", required=True, choices=sorted(automation_rules.KNOWN_FIX_TYPES))
    parser.add_argument("--environment", required=True)
    parser.add_argument("--state", choices=(ON, OFF))
    parser.add_argument("--check", action="store_true", help="report each #N pull request's state; open nothing")
    parser.add_argument(
        "--repo", required=True, action="append", type=_repo_arg,
        help="a managed repository, with #N for the switch's pull request still pending there",
    )
    args = parser.parse_args(argv)
    if args.check and any(pending is None for _repo, pending in args.repo):
        parser.error("--check needs <repository>#<pull request number>")
    if not args.check and args.state is None:
        parser.error("--state is required unless --check")
    import kubeagents_switches

    if kubeagents_switches.switch("DIAL") != "on":
        print(json.dumps([{"state": BLOCKED, "reason": "KUBEAGENTS_DIAL is off"}]))
        return 1
    results = []
    for repo, pending in args.repo:
        if args.check:
            try:
                results.append(check(repo, pending))
            except Exception as exc:  # noqa: BLE001 — unknown is reported, the rest still run
                results.append({"repo": repo, "number": pending, "state": "", "reason": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            results.append(toggle(repo, args.kind, args.environment, args.state == ON, pending))
        except Exception as exc:  # noqa: BLE001 — one repository's failure is reported, the rest still run
            results.append({"repo": repo, "state": BLOCKED, "reason": f"{type(exc).__name__}: {exc}"})
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
