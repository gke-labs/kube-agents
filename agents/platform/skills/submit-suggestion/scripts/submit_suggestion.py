#!/opt/hermes/.venv/bin/python3
"""
GKE Platform Agent — GitOps PR Suggestion Submitter

Two commands, because a pull request takes two turns of the agent's shell and
the agent has to know *where* to work in between:

    prepare  -> lease a private clone, take the branch, print the workspace
    (agent edits files, `git add`, `git commit` — inside that workspace)
    submit   -> verify the lease is still ours, push, open/refresh the pull request

`prepare` exists because this script used to have no working directory at all.
It ran `git push -f` in whatever directory the agent's shell happened to be in,
and its SKILL.md told the agent to `git checkout -b …` without naming a
directory either. In a pod where six audit crons and every kanban worker share
one volume, that meant branching and force-pushing inside a clone another agent
was in the middle of using. `gitops_workspace` hands out one clone per lease;
this script takes one, and refuses to write in anyone else's.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Append global scripts path to allow importing the shared helpers
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
# The same directory in a source checkout, where nothing is staged into /opt.
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import gitops_workspace
from github_token_refresh import refresh_git_credentials, log

# Branches a suggestion may never target. `main` and `master` are the GitOps
# rollout branches; `production` is the convention some fleets use instead.
PROTECTED_BRANCHES = {"main", "master", "production"}

OWNER = "submit-suggestion"


def check_branch(branch_name: str) -> str:
    branch = (branch_name or "").strip()
    if not branch:
        raise ValueError("--branch is required and must not be empty")
    if branch.lower() in PROTECTED_BRANCHES:
        raise ValueError(
            f"CRITICAL SECURITY REFUSAL: Force-pushing to protected branch "
            f"'{branch_name}' is strictly blocked by GKE SRE guardrails!"
        )
    return branch


def git(argv: list, workspace: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git inside the leased workspace.

    Every call names `cwd` explicitly. The credential proxy runs the real git in
    the sidecar's filesystem at whatever directory this process reports, so an
    unstated cwd is not "the obvious one" — it is the sidecar's default, which
    now holds no lease and is refused outright.
    """
    return gitops_workspace.run_git(argv, workspace, check=check)


def handle_prepare(args) -> int:
    branch = check_branch(args.branch)
    lease = gitops_workspace.lease_id(args.lease)
    repo = gitops_workspace.resolve_repo()

    # Repo-scoped, and needed before the clone: the clone is what a token would
    # otherwise have to be derived from.
    refresh_git_credentials(repo)

    workspace = gitops_workspace.ensure_workspace(
        repo,
        _runner,
        lease=lease,
        reset=True,
        owner=OWNER,
    )
    gitops_workspace.configure_identity(workspace, _runner)
    base = gitops_workspace.resolve_base_branch(workspace, _runner)

    # Continue the branch when the remote already has it; only cut a new one
    # from the base when it does not.
    #
    # This is Step 5 of the SKILL, and getting it wrong destroyed work. "Address
    # the review feedback" runs `prepare --branch <headRefName>` against the
    # branch an open pull request is already sitting on. Resetting that branch
    # to `origin/<base>` and force-pushing does not amend the pull request — it
    # replaces every reviewed commit with one that no longer contains them.
    # `--force-with-lease` cannot object, either: `ensure_workspace` fetched the
    # very ref the lease would have been compared against, moments earlier.
    start = f"origin/{branch}" if remote_branch_exists(branch, workspace) else f"origin/{base}"

    # `-B` rather than `-b`: a retried card must land on the same branch it was
    # working on rather than failing with "already exists". The tree was just
    # reset, so there is nothing in it to lose.
    git(["checkout", "-B", branch, start], workspace)

    print(json.dumps({
        "workspace": str(workspace),
        "lease": lease,
        "branch": branch,
        "base": base,
        "repo": repo,
        "started_from": start,
    }))
    return 0


def remote_branch_exists(branch: str, workspace) -> bool:
    """Whether `origin/<branch>` is present in this workspace's refs.

    Reads the remote-tracking ref rather than the network: `ensure_workspace`
    has just fetched with `--prune`, so the local answer is both current and
    free. Fully-qualified under `refs/remotes/` so a branch sharing a name with
    a tag — or one called `HEAD` — cannot resolve to something else.
    """
    res = git(
        ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        workspace,
        check=False,
    )
    return res.returncode == 0


def handle_submit(args) -> int:
    branch = check_branch(args.branch)
    workspace = args.workspace or os.getcwd()
    lease = args.lease or gitops_workspace.session_lease()
    if not lease:
        # Never `lease_id` here. It would mint `adhoc-<random>` — a *different*
        # random string from the one `prepare` minted in its own process — and
        # the ownership check below would then refuse the workspace `prepare`
        # had just handed over, identically on every retry. Naming the fix is
        # the difference between a one-line correction and an unrecoverable
        # loop.
        raise ValueError(
            "no lease to check this workspace against: neither --lease nor a "
            "session identity (HERMES_KANBAN_TASK, HERMES_SESSION_ID) is set. "
            "`prepare` printed the lease it took as the `lease` field of its "
            "JSON line — pass that back as `--lease <lease>`."
        )

    # The check the credential proxy cannot make. It can see that a push is
    # happening inside *some* lease, because it only receives argv and a working
    # directory — never a caller identity. Whether the lease is ours is knowable
    # only here.
    gitops_workspace.assert_lease_owner(workspace, lease)

    on = git(["rev-parse", "--abbrev-ref", "HEAD"], workspace, check=False)
    current = (on.stdout or "").strip()
    if current != branch:
        raise ValueError(
            f"{workspace} is on branch '{current}', not '{branch}'. Commit your "
            f"changes on '{branch}' before submitting, or pass the branch you "
            "are actually on."
        )

    repo = args.repo or gitops_workspace.resolve_repo()
    refresh_git_credentials(repo)

    push_branch(branch, workspace)
    base = gitops_workspace.resolve_base_branch(workspace, _runner)
    pr_url = create_pull_request(branch, args.title, args.body, workspace, repo, base)
    log(f"PR SUBMITTED SUCCESSFULLY! 🏆 URL: {pr_url}")

    # Print raw URL to stdout for the MCP tool to parse
    print(pr_url)
    return 0


def push_branch(branch_name: str, workspace: str) -> None:
    """Push the branch, without the right to destroy someone else's.

    This used to be `git push -f`. The `-f` was there for a real reason — a card
    that comes back for another round of review feedback has to update the
    branch its pull request already points at — but a blind force also silently
    discards a branch of the same name that another agent pushed while this one
    was working. `--force-with-lease` keeps the first case and refuses the
    second: it overwrites the remote ref only if it still matches the value
    `prepare` fetched into this workspace.

    Deliberately no `git fetch` first. Fetching immediately before a
    `--force-with-lease` is the classic way to defeat it: the fetch moves the
    remote-tracking ref onto whatever the other agent just pushed, the lease
    then compares that value against itself, and the force goes through. The
    ref this push is leased against has to be the one from `prepare`.
    """
    log(f"Pushing active branch '{branch_name}' securely to origin...")
    git(["push", "--force-with-lease", "origin", branch_name], workspace)


def create_pull_request(
    branch: str, title: str, body: str, workspace: str, repo: str, base: str
) -> str:
    """Open the pull request — or refresh the one that is already open.

    `gh pr create` fails with "a pull request for branch … already exists"
    every time a card comes back for a second round, and it fails *after* the
    push has landed. Read as an error that is the worst possible shape: the
    branch was updated and the reviewer will see the new commits, but the skill
    reports the whole submission as failed, so the agent retries, pushes again,
    and fails again — for as many rounds of feedback as the pull request gets.
    An existing pull request is the success case for a resubmission.

    It is refreshed rather than merely located. Step 5 of the SKILL hands this
    function a title and body written for the commits it just pushed; leaving
    the old description in place would describe work the branch no longer
    contains. `audit_report.open_remediation_pr` edits its own pull requests
    for the same reason.
    """
    log(f"Submitting GitOps Pull Request for branch '{branch}'...")

    cmd = [
        "gh", "pr", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--base", base,
        "--head", branch
    ]

    res = subprocess.run(
        cmd, cwd=workspace, capture_output=True, text=True, check=False
    )
    if res.returncode == 0:
        return res.stdout.strip()

    if "already exists" not in f"{res.stdout}\n{res.stderr}".lower():
        # Anything else — no permission, a protected base, gh not authenticated
        # — is a real failure and keeps the shape `main` already handles.
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)

    log(f"A pull request for '{branch}' is already open; updating it in place.")
    return update_pull_request(branch, title, body, workspace, repo)


def update_pull_request(
    branch: str, title: str, body: str, workspace: str, repo: str
) -> str:
    """Point the existing pull request for `branch` at the work just pushed."""
    subprocess.run(
        ["gh", "pr", "edit", branch, "--repo", repo, "--title", title, "--body", body],
        cwd=workspace, capture_output=True, text=True, check=True,
    )
    res = subprocess.run(
        ["gh", "pr", "view", branch, "--repo", repo, "--json", "url", "--jq", ".url"],
        cwd=workspace, capture_output=True, text=True, check=True,
    )
    url = res.stdout.strip()
    if not url:
        raise RuntimeError(
            f"`gh pr view {branch}` returned no URL for the pull request it just "
            "reported as already existing. The push landed; find the pull request "
            "on GitHub rather than resubmitting."
        )
    return url


def _runner(cmd: list, *, cwd=None, check: bool = True):
    """Adapter so gitops_workspace's git calls are logged like everything else."""
    log(f"$ {' '.join(cmd)}" + (f"  (in {cwd})" if cwd else ""))
    return subprocess.run(
        cmd, cwd=cwd, check=check, capture_output=True, text=True
    )


COMMANDS = ("prepare", "submit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Secure GitOps PR Suggestion Submitter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="Lease a private clone and take the branch"
    )
    prepare.add_argument("--branch", required=True, help="Branch to create")
    prepare.add_argument(
        "--lease", default=None, help="Lease id (defaults to the kanban task)"
    )

    submit = subparsers.add_parser("submit", help="Push the branch and open the PR")
    submit.add_argument("--branch", required=True, help="Active Git branch name")
    submit.add_argument("--title", required=True, help="Pull Request title")
    submit.add_argument("--body", required=True, help="Pull Request description body")
    submit.add_argument(
        "--workspace", default=None, help="The leased workspace from `prepare`"
    )
    submit.add_argument(
        "--lease", default=None, help="Lease id (defaults to the kanban task)"
    )
    submit.add_argument("--repo", default=None, help="Target repository as owner/name")
    return parser


def normalise_argv(argv: list) -> list:
    """Accept the pre-`prepare` call shape, which had no subcommand at all.

    The skill used to invoke this with a bare `--branch/--title/--body`. A
    session already mid-flight when this ships must not die on "invalid choice",
    so an argv that does not name a command is read as `submit` — except a bare
    help request, which has to keep printing the help for the whole script.
    """
    argv = list(argv)
    if not argv or argv[0] in COMMANDS or argv[0] in ("-h", "--help"):
        return argv
    return ["submit", *argv]


def dispatch(argv: list) -> int:
    """Parse and run, letting failures out as themselves.

    Separate from `main` so a caller — the tests, mainly — can see the
    `PermissionError` a foreign lease raises rather than an exit code.
    """
    args = build_parser().parse_args(normalise_argv(argv))
    if args.command == "prepare":
        return handle_prepare(args)
    return handle_submit(args)


def main():
    try:
        sys.exit(dispatch(sys.argv[1:]))

    except PermissionError as e:
        # The foreign-lease refusal. Distinct from the generic failure below
        # because it is the one an agent can act on without an operator.
        log(f"REFUSED: {e}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        log("FATAL ERROR: GitOps subprocess execution failed!")
        log(f"Exit Code: {e.returncode}")
        if e.stderr:
            log(f"Stderr Output:\n{e.stderr.strip()}")
        if e.stdout:
            log(f"Stdout Output:\n{e.stdout.strip()}")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL ERROR: GitOps suggestion submission failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
