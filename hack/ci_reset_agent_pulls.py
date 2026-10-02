"""Leave ONE leased project's GitOps repository with no agent pull request open
and no branch but the default.

A remediation case opens a pull request in the leased project's GitOps
repository (`gke-agentic/<project>-infra`) and nothing closes it before the
next lease or the next repetition. The next run's agent finds it and builds on
it instead of investigating: `submit_suggestion.py` adds to its own open
proposal on the same branch, and the fleet audit skips a finding whose pull
request already exists. The pool sweep (hack/ci_sweep_agent_pulls.py) only
visits free projects, so it cannot help inside a job, and a project re-leased
inside its interval carries the leftovers in (#2260).

hack/ci-eval-pr.sh runs this twice: once when the lease begins, before any
unit, and once before every unit of a case that requests a pull request, under
that case's task lock, in the phase where no other unit writes. It then reads
the repository back and refuses to launch the unit unless the repository is
clean, which is the guarantee: every repetition starts as the first did.

What goes, and what stays:

* every open pull request authored by a `[bot]` login from a branch in the
  repository itself. Not by branch name: the agent names its own branches
  when it pushes with git from its sandbox, and most leftovers carry no
  `platform-agent/` prefix. A human's pull request stays open, and so does
  its branch.
* a pull request carrying `audit:remediation` is labelled `audit:stale-closed`
  before it is closed. The fleet audit reads an unlabelled close as a human's
  refusal and never re-proposes that fix in that repository (#2228); a label
  that will not stick leaves the pull request open for the next attempt.
* every branch but the default, whatever its name, unless an open pull request
  that stays still has it as head. A pool repository is a fixture nothing else
  keeps branches in.

Never any repository but the leased project's: `expected_repo` refuses a pair
that does not match, and the token the caller minted is narrowed to that one
repository as well. The token arrives in the environment
(`AGENT_PULLS_RESET_TOKEN`), never on argv. `--record` writes what happened as
JSON beside the job's artifacts, so every run carries its own proof;
`--dry-run` lists and writes nothing, for a laptop with a personal token.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse

# The sibling helper's GitHub call, repository guard and fault class. Run as
# `python3 hack/ci_reset_agent_pulls.py`, hack/ is sys.path[0], as it is for
# the sweep's `import boskos_pool`.
import ci_reset_audit_ledgers as ledgers

TOKEN_ENV = "AGENT_PULLS_RESET_TOKEN"
BOT_LOGIN_SUFFIX = ledgers.BOT_LOGIN_SUFFIX
# The audit's two labels, as audit_report.py writes and reads them; a test
# pins both to the sweep's and the audit's literals.
AUDIT_REMEDIATION_LABEL = "audit:remediation"
STALE_CLOSED_LABEL = "audit:stale-closed"
# GitHub's answers for a ref that is already gone: 422 "Reference does not
# exist", or 404. Neither is a failure.
REF_GONE_CODES = (404, 422)
# A second between writes, GitHub's published burst limit for an App. A lease
# with a dozen leftovers costs under a minute; the sweep paces the same way.
WRITE_PAUSE_SECONDS = 1.0
RECORD_SCHEMA_VERSION = 1
ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

pause = time.sleep
ResetError = ledgers.ResetError


def is_agent_pull_request(pull: dict, repo: str) -> bool:
    """A `[bot]` author, from a branch in the repository itself.

    The job's ledger App cannot ask GitHub for the minter App's slug the way
    the sweep does, and one App serves the pool, so any App bot that opened a
    pull request in a pool repository is the eval agent (#2133's rule).
    """
    head = pull.get("head") or {}
    head_repo = str((head.get("repo") or {}).get("full_name") or "")
    author = str((pull.get("user") or {}).get("login") or "")
    return author.endswith(BOT_LOGIN_SUFFIX) and head_repo.lower() == repo.lower()


def is_audit_pull_request(pull: dict) -> bool:
    return AUDIT_REMEDIATION_LABEL in ledgers.label_names(pull)


def open_pulls(repo: str, token: str) -> list[dict]:
    """Every open pull request, oldest first."""
    found: list[dict] = []
    for page in range(1, ledgers.MAX_PAGES + 1):
        query = urllib.parse.urlencode({"state": "open", "per_page": str(ledgers.PER_PAGE), "page": str(page)})
        batch = ledgers.api("GET", f"/repos/{repo}/pulls?{query}", token)
        if not isinstance(batch, list) or not all(isinstance(pull, dict) for pull in batch):
            raise ResetError(f"GitHub answered the pull-request listing for {repo} with a body that is not a list")
        if not batch:
            break
        found.extend(batch)
        if len(batch) < ledgers.PER_PAGE:
            break
    found.sort(key=lambda pull: int(pull.get("number") or 0))
    return found


def default_branch(repo: str, token: str) -> str:
    payload = ledgers.api("GET", f"/repos/{repo}", token)
    name = str((payload or {}).get("default_branch") or "") if isinstance(payload, dict) else ""
    if not name:
        raise ResetError(f"GitHub answered the repository lookup for {repo} without a default branch")
    return name


def branches(repo: str, token: str) -> list[str]:
    """Every branch name in the repository, the default included."""
    names: list[str] = []
    for page in range(1, ledgers.MAX_PAGES + 1):
        query = urllib.parse.urlencode({"per_page": str(ledgers.PER_PAGE), "page": str(page)})
        batch = ledgers.api("GET", f"/repos/{repo}/branches?{query}", token)
        if not isinstance(batch, list) or not all(isinstance(branch, dict) for branch in batch):
            raise ResetError(f"GitHub answered the branch listing for {repo} with a body that is not a list")
        names.extend(str(branch.get("name") or "") for branch in batch)
        if len(batch) < ledgers.PER_PAGE:
            break
    return [name for name in names if name]


def write(method: str, path: str, token: str, body: dict | None = None):
    """One GitHub write, then the pause that keeps the next one under the limit."""
    try:
        return ledgers.api(method, path, token, body)
    finally:
        pause(WRITE_PAUSE_SECONDS)


def delete_branch(repo: str, name: str, token: str) -> None:
    try:
        write("DELETE", f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(name, safe='/')}", token)
    except urllib.error.HTTPError as exc:
        if exc.code not in REF_GONE_CODES:
            raise


def _head_ref(pull: dict) -> str:
    return str((pull.get("head") or {}).get("ref") or "")


def new_record(repo: str, project: str, build: str, scope: str, dry_run: bool) -> dict:
    """What a call reports, before anything is read: the caller writes it on
    every exit, so the reset that faulted is the one with a record too."""
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "repo": repo,
        "project": project,
        "build": build,
        "scope": scope,
        "dry_run": dry_run,
        "started_at": time.strftime(ISO_UTC_FORMAT, time.gmtime(time.time())),
        "error": None,
        "open_before": 0,
        "kept_open": [],
        "labelled": [],
        "closed": [],
        "unclosed": [],
        "branches_before": 0,
        "deleted": [],
        "undeleted": [],
        "kept_branches": [],
        "open_after": None,
        "branches_after": None,
        "clean": False,
    }


def reset(repo: str, project: str, build: str, scope: str, token: str, dry_run: bool, record: dict | None = None) -> dict:
    """Close, delete, read back. Fills and returns the record; `clean` says
    whether the repository is now what a unit may start on."""
    if record is None:
        record = new_record(repo, project, build, scope, dry_run)
    ledgers.expected_repo(repo, project)
    pulls = open_pulls(repo, token)
    agent_pulls = [pull for pull in pulls if is_agent_pull_request(pull, repo)]
    agent_numbers = {pull.get("number") for pull in agent_pulls}
    record["open_before"] = len(agent_pulls)
    # Heads an open pull request that stays still needs: a human's, or one of
    # the agent's that would not close below.
    heads_in_use: set[str] = set()
    for pull in pulls:
        if pull.get("number") not in agent_numbers:
            record["kept_open"].append(pull.get("number"))
            print(f"  #{pull.get('number', '?')} left open, not the agent's ({_head_ref(pull)})")
            heads_in_use.add(_head_ref(pull))
    for pull in agent_pulls:
        number = pull["number"]
        ref = _head_ref(pull)
        audit = is_audit_pull_request(pull)
        print(f"  #{number} ({ref}){' audit, labelled first' if audit else ''}")
        if dry_run:
            continue
        # Each close stands alone: one that fails is reported and the rest
        # still close. The label goes on before the close, never after -- a
        # close the audit reads back unlabelled is a human's refusal to it --
        # so a label that will not stick leaves the pull request open.
        try:
            if audit:
                write("POST", f"/repos/{repo}/issues/{number}/labels", token, {"labels": [STALE_CLOSED_LABEL]})
                record["labelled"].append(number)
            write("PATCH", f"/repos/{repo}/pulls/{number}", token, {"state": "closed"})
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"  #{number} did not close ({exc})", file=sys.stderr)
            record["unclosed"].append(number)
            heads_in_use.add(ref)
            continue
        record["closed"].append(number)
    default = default_branch(repo, token)
    leftover = [name for name in branches(repo, token) if name != default]
    record["branches_before"] = len(leftover)
    for name in leftover:
        if name in heads_in_use:
            record["kept_branches"].append(name)
            print(f"  branch {name} kept, an open pull request has it as head")
            continue
        print(f"  branch {name}")
        if dry_run:
            continue
        try:
            delete_branch(repo, name, token)
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"  branch {name} was not deleted ({exc})", file=sys.stderr)
            record["undeleted"].append(name)
            continue
        record["deleted"].append(name)
    if dry_run:
        print(f"would close {len(agent_pulls)} pull request(s) and delete {len(leftover) - len(record['kept_branches'])} branch(es) in {repo} {scope}")
        return record
    # The read-back is the guarantee, not the writes: what is open now is
    # what the unit's agent would find.
    after_pulls = open_pulls(repo, token)
    still_agent = [pull["number"] for pull in after_pulls if is_agent_pull_request(pull, repo)]
    heads_after = {_head_ref(pull) for pull in after_pulls}
    still_branches = [name for name in branches(repo, token) if name != default and name not in heads_after]
    record["open_after"] = len(still_agent)
    record["branches_after"] = len(still_branches)
    record["clean"] = not still_agent and not still_branches
    record["finished_at"] = time.strftime(ISO_UTC_FORMAT, time.gmtime(time.time()))
    print(
        f"closed {len(record['closed'])} pull request(s) and deleted {len(record['deleted'])} branch(es) in {repo} {scope}; "
        f"{len(still_agent)} agent pull request(s) and {len(still_branches)} branch(es) remain"
    )
    return record


def write_record(path: str, record: dict) -> None:
    try:
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"WARNING: could not write the record to {path} ({exc})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="owner/name of the leased project's GitOps repository")
    parser.add_argument("--project", required=True, help="the leased PROJECT_ID the repository must belong to")
    parser.add_argument("--build", required=True, help="the eval build id, for the record")
    parser.add_argument("--scope", default="at lease time", help="what this reset is for, in the record and the log")
    parser.add_argument("--record", default="", help="write the JSON record of what happened here")
    parser.add_argument("--dry-run", action="store_true", help="list what would go; write nothing")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"ERROR: {TOKEN_ENV} is not set; nothing to authenticate with", file=sys.stderr)
        return 2
    record = new_record(args.repo, args.project, args.build, args.scope, args.dry_run)
    try:
        reset(args.repo, args.project, args.build, args.scope, token, args.dry_run, record)
    except ResetError as exc:
        record["error"] = str(exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        record["error"] = f"GitHub answered HTTP {exc.code} ({exc.reason}) reading {args.repo}"
        print(
            f"ERROR: {record['error']}; a 403 or 404 here is the token's reach, not an empty repository",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        record["error"] = f"could not reach api.github.com ({type(exc).__name__}: {exc})"
        print(f"ERROR: {record['error']}", file=sys.stderr)
        return 1
    finally:
        # On every exit, the faulted ones included: the record is the
        # artifact the shell step names as evidence.
        if args.record:
            write_record(args.record, record)
    if args.dry_run:
        return 0
    return 0 if record["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
