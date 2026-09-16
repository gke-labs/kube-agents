#!/usr/bin/env python3
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

There are now two ways that middle turn can work, and which one runs depends on
whether the broker has content workspaces armed.

**Content mode.** `prepare` asks the broker to open a repository and gets back a
handle. There is no directory: the agent writes its files into any scratch
directory it likes, and `submit --from <dir>` reads them and hands the bytes to
the broker, which owns the only checkout. The agent never sees a `.git`, so it
cannot author the `.git/config` that every known code-execution route through
the credential container needs — a filter driver, an alias, a hook path. That is
a closed class rather than a longer list of blocked keys.

**Directory mode**, which is what ran before and still runs when the broker has
not been armed: a leased clone on the shared volume, and the agent commits in it.

The two are deliberately live at once. `prepare` reports which one it took as
the `mode` field of its JSON line, and `submit` follows the handle rather than a
flag, so a session that started under one does not finish under the other.
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

import credential_proxy_client
import gitops_workspace
from github_token_refresh import refresh_git_credentials, log


# Branches a suggestion may never target. `main` and `master` are the GitOps
# rollout branches; `production` is the convention some fleets use instead.
PROTECTED_BRANCHES = {"main", "master", "production"}

OWNER = "submit-suggestion"

# The one directory `--body-file` may name. The same bound `pr_conversation.py`
# and `github-issue-resolver`'s resolver put on their own body paths, for the
# same reason: what the file holds is posted publicly, so a path the model
# supplies is a way to publish any file the agent container can read.
SCRATCH_DIR = "/opt/data/scratch"

# `gh pr view` reports the branch's most recent pull request whatever its state,
# so "there is a pull request" and "there is one to keep the description of" are
# different questions. Ask for the state and require this.
PR_STATE_OPEN = "OPEN"

# How `gh pr view` says the branch has no pull request at all. Every other
# non-zero exit -- an expired token, a 502, a rate limit -- means the lookup
# failed rather than came back empty, and the two must not read alike.
NO_PULL_REQUEST_MARKER = "no pull requests found"


def check_branch(branch_name: str) -> str:
    branch = (branch_name or "").strip()
    if not branch:
        raise ValueError("--branch is required and must not be empty")
    # Compare the short name: "refs/heads/main" is not in PROTECTED_BRANCHES,
    # but pushing it moves main all the same.
    short = branch.lower()
    if short.startswith("refs/heads/"):
        short = short[len("refs/heads/"):]
    if short in PROTECTED_BRANCHES:
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


def validate_repo(repo: str) -> str:
    """Ensure repo is formatted as owner/name and is in the managed repos allowlist if configured."""
    if not repo or not gitops_workspace.is_valid_repo_slug(repo):
        raise ValueError(f"Invalid repository format: {repo!r}. Expected 'owner/name'.")
    managed = gitops_workspace.get_managed_github_repos()
    if managed and repo not in managed:
        raise ValueError(
            f"Repository {repo!r} is not in the managed repositories list: {managed}"
        )
    return gitops_workspace.validate_repo_org(repo)


def proxy_endpoint() -> str:
    return os.environ.get("CREDENTIAL_PROXY_URL", "").strip()


def content_mode_available() -> bool:
    """Whether the broker will take content rather than a directory.

    Asked of the broker rather than read from a local flag. The two run side by
    side during the migration, and the agent container is not where that switch
    lives -- the broker either has the routes or it does not.
    """
    endpoint = proxy_endpoint()
    if not endpoint:
        return False
    return credential_proxy_client.workspaces_available(endpoint)


def handle_prepare_content(args) -> int:
    branch = check_branch(args.branch)
    # `--repo` first, as the directory path reads it. Ignoring it here silently
    # opened the default repository under a flag that named another one, and a
    # fleet whose cards target several GitOps repositories writes every
    # suggestion to whichever one `resolve_repo` happens to answer with.
    repo = args.repo or gitops_workspace.resolve_repo()
    # Same allowlist the directory path answers to. Content mode reaches the
    # broker instead of a clone, and skipping the check here would make the
    # managed-repos list depend on which transport the run happened to pick.
    validate_repo(repo)
    refresh_git_credentials(repo)
    workspace = credential_proxy_client.Workspace.open(
        proxy_endpoint(), repo, branch=branch
    )
    # No lease and no workspace path. The handle is what `submit` presents, and
    # unlike the `.lease` file it replaces the agent cannot fabricate one -- it
    # is 128 bits the broker minted and never wrote to a shared volume. It is
    # still a bearer capability rather than an ownership check: the broker
    # cannot tell two sessions in the agent container apart, and nothing here
    # pretends otherwise.
    print(json.dumps({
        "mode": "content",
        "handle": workspace.handle,
        "branch": branch,
        "base": workspace.base,
        "baseSha": workspace.base_sha,
        "repo": workspace.repo,
        "started_from": workspace.started_from,
    }))
    return 0


def handle_prepare(args) -> int:
    if content_mode_available():
        return handle_prepare_content(args)
    branch = check_branch(args.branch)
    lease = gitops_workspace.lease_id(args.lease)
    repo = args.repo or gitops_workspace.resolve_repo()
    validate_repo(repo)

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
        "mode": "directory",
        "workspace": str(workspace),
        "lease": lease,
        "branch": branch,
        "base": base,
        "repo": repo,
        "started_from": start,
    }))
    return 0


def open_handle(args) -> "credential_proxy_client.Workspace":
    """Rebuild a client-side Workspace around a handle `prepare` printed.

    The handle is the whole state. `base`/`baseSha` are carried back only so the
    caller can pass `--base-sha` to the conflict check; nothing here holds a
    directory, which is what makes a second process able to pick the session up.
    """
    return credential_proxy_client.Workspace(
        proxy_endpoint(),
        {
            "handle": args.handle,
            "repo": args.repo or gitops_workspace.resolve_repo(),
            "base": getattr(args, "base", None) or "",
            "baseSha": getattr(args, "base_sha", None) or "",
        },
    )


def handle_list(args) -> int:
    """What the repository holds, without a checkout to look in."""
    entries = open_handle(args).list(args.prefix)
    # `truncated` is reported rather than swallowed. A listing that stopped at
    # the broker's ceiling and looks complete is how the next `fetch` ends up
    # naming a path nobody saw.
    print(
        json.dumps(
            {
                "entries": entries,
                "total": entries.total,
                "truncated": entries.truncated,
            }
        )
    )
    return 0


def handle_fetch(args) -> int:
    """Copy named repository files into a scratch directory to edit.

    The read half of content mode, and the reason it is a subcommand rather than
    something the agent works around: with the checkout on the broker's side
    there is no `cat` that reaches an existing file, so editing one would
    otherwise mean rewriting it from memory.

    Fetching into the same directory `submit --from` later reads is deliberate.
    That directory is the change set, so the files that arrive here are exactly
    the ones the commit may touch, and an untouched one contributes no diff.
    """
    workspace = open_handle(args)
    destination = Path(args.to).resolve()
    written = []
    for path in args.path:
        # Read before creating anything. The broker is the one validator of a
        # repository-relative path, and doing the mkdir first meant a `--path`
        # of `../../etc/foo` created directories out here before the refusal
        # arrived. Nothing local re-parses the path -- a second parser is how
        # the two halves come to disagree -- but the destination it resolves to
        # is a local question, so it gets a local answer.
        content = workspace.read(path)
        target = (destination / path).resolve()
        if target != destination and destination not in target.parents:
            raise ValueError(f"{path} resolves outside {destination}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(path)
    print(json.dumps({"to": str(destination), "files": written}))
    return 0


def collect_changes(source: str, deletes: list[str] | None) -> dict[str, bytes | None]:
    """Every file under `source`, keyed by its path relative to `source`.

    The agent's scratch directory *is* the change set. There is no `git add`
    equivalent to get wrong, and no wildcard to expand into something wider than
    was meant: what the directory holds is what the commit contains.

    Symlinks are skipped rather than followed. A link in a scratch directory
    resolves against the agent container's filesystem, and following it would
    read whatever it points at into a commit -- an agent's own credentials
    included -- while the request still looked like ordinary file content.
    """
    root = Path(source).resolve()
    if not root.is_dir():
        raise ValueError(f"--from {source} is not a directory")
    changes: dict[str, bytes | None] = {}
    skipped: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            skipped.append(str(path.relative_to(root)))
            continue
        if not path.is_file():
            continue
        if any(part == ".git" for part in path.relative_to(root).parts):
            skipped.append(str(path.relative_to(root)))
            continue
        changes[str(path.relative_to(root))] = path.read_bytes()
    if skipped:
        log(f"skipped (symlink or .git): {', '.join(skipped)}")
    for path in deletes or []:
        changes[path] = None
    if not changes:
        raise ValueError(
            f"--from {source} holds no regular files, so there is nothing to commit"
        )
    return changes


def handle_submit_content(args, body: str) -> int:
    branch = check_branch(args.branch)
    # `--keep-description` waives the body here, never the title. In directory
    # mode the caller has already made the commit and the title only ever
    # reaches `gh pr create`; here this script makes the commit, and the title
    # is its message. Leaving the pull request's description alone says nothing
    # about what the new commit should be called.
    if not args.title:
        raise ValueError(
            "--title is required with --handle: in content mode this script "
            "makes the commit, and --title is its message. With "
            "--keep-description the pull request's own description is still "
            "left as its author wrote it."
        )
    if not args.source:
        raise ValueError(
            "--from is required with --handle: in content mode the broker owns "
            "the checkout, so the files to commit come from a directory in this "
            "container rather than from a working tree the broker can see."
        )
    changes = collect_changes(args.source, args.delete)
    repo = args.repo or gitops_workspace.resolve_repo()
    # Before the credential is refreshed for it, not after. `--repo` is argv the
    # model controls, and everything downstream spends the agent's GitHub
    # credential on whatever it names: refresh_git_credentials mints an
    # installation token *for this repo*, and create_pull_request runs
    # `gh pr create --repo`. The allowlist is the boundary on where that
    # credential may be spent, so a path that skips it is a way around the
    # boundary rather than a missing convenience. Every sibling handler checks
    # here -- see handle_prepare_content, which says why the check cannot depend
    # on which transport the run picked.
    validate_repo(repo)
    refresh_git_credentials(repo)

    # Before the commit, not after the push. `create_pull_request` is the last
    # thing this handler does, and a refusal there leaves the commit already on
    # the branch -- at which point the retry the error asks for finds nothing
    # to commit and refuses too.
    keep_url = (
        _keep_description_url(branch, None, repo) if args.keep_description else ""
    )

    # `with`, so the broker's clone is released on the failure paths too. A
    # commit refused as a duplicate, a push that lost the lease, `gh` exiting
    # non-zero -- each used to leave a checkout and its handle on the broker
    # with nothing left that could name them, and the sidecar's disk is not
    # something a retry loop should be able to fill.
    with open_handle(args) as workspace:
        log(f"Sending {len(changes)} file(s) to the broker for branch '{branch}'...")
        result = workspace.commit(
            branch=branch,
            message=args.title,
            changes=changes,
            expected_base_sha=args.base_sha or None,
        )
        if not result["committed"]:
            # A submission whose files already match the branch. Refused rather
            # than reported as success: the caller asked for a pull request, and
            # answering "done" for a branch that may not have one is the wrong
            # half of the ambiguity to guess at.
            raise ValueError(
                f"the files in {args.source} are already what '{branch}' holds, so "
                "there is nothing to commit; check whether the pull request is "
                "already open before submitting again"
            )
        log(f"Committed {result['commit'][:12]} on '{branch}'; pushing...")
        workspace.push(branch)

        pr_url = create_pull_request(
            branch,
            args.title,
            body,
            None,
            repo,
            result["base"],
            keep_description_url=keep_url,
        )
    log(f"PR SUBMITTED SUCCESSFULLY! 🏆 URL: {pr_url}")
    print(pr_url)
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
    # Resolve and check the description *before* the dispatch below, so both
    # transports get the same answer. `CREDENTIAL_PROXY_CONTENT_WORKSPACE=1` is
    # rendered unconditionally by the operator, which makes content mode the
    # path every current install takes: a check that sits after the dispatch is
    # a check that runs almost nowhere.
    body = _submit_body(args)
    if not args.keep_description and not (args.title and body):
        raise ValueError(
            "--title and one of --body / --body-file are required unless "
            "--keep-description is given."
        )

    # Dispatch on the handle rather than on a flag or on what the broker
    # supports right now. A session that prepared in one mode has to finish in
    # that mode: prepared under content-passing there is no leased directory to
    # fall back to, and prepared under a lease there is no handle to present.
    if args.handle:
        return handle_submit_content(args, body)
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

    repo = args.repo or gitops_workspace.resolve_repo(workspace=workspace)
    validate_repo(repo)
    refresh_git_credentials(repo)

    # Before the push, for the reason `_keep_description_url` gives -- and here
    # so the two transports refuse at the same point rather than one of them
    # refusing after it has already moved the branch.
    keep_url = ""
    if args.keep_description:
        keep_url = _keep_description_url(branch, workspace, repo)
        if args.title:
            # Not silently. `--keep-description` keeps the title along with the
            # body, so a title passed here is read and discarded, and a caller
            # who passed one believes it landed.
            log(
                "--title is ignored under --keep-description: the title is part "
                "of the description being kept."
            )

    push_branch(branch, workspace)
    base = gitops_workspace.resolve_base_branch(workspace, _runner)
    pr_url = create_pull_request(
        branch,
        args.title,
        body,
        workspace,
        repo,
        base,
        keep_description_url=keep_url,
    )
    log(f"PR SUBMITTED SUCCESSFULLY! 🏆 URL: {pr_url}")

    # Print raw URL to stdout for the MCP tool to parse
    print(pr_url)
    return 0


def _submit_body(args) -> str:
    """The description text, from `--body-file` if one was given.

    A pull request body is long, full of backticks, and assembled by a model
    into a shell command. Through argv it is one `$(...)` away from executing
    in the leased clone, where a credentialed `gh` and `git` are on `PATH`, and
    one stray backtick away from silently deleting its own text. A file is the
    channel the rest of this repository already uses for model-written prose —
    `pr_conversation.py reply` and `resolver.py report` both take a path — and
    the asymmetry was that the larger document went the other way.

    The path is confined the way both of those confine theirs, and for the
    reason they give: the file's contents are published, so an unbounded path
    is a way to put `/proc/self/environ` into a public pull request
    description. Reaching for one is not something the agent has to intend --
    Step 5 of the SKILL has it read review comments, which are somebody else's
    text.

    `--body` stays because callers outside this repository pass it and short
    bodies are fine.
    """
    if not args.body_file:
        return args.body or ""
    # Resolved before the prefix test, so a symlink planted inside scratch
    # cannot reach out of it.
    scratch = os.path.realpath(SCRATCH_DIR)
    real = os.path.realpath(args.body_file)
    if not real.startswith(scratch + os.sep):
        raise ValueError(f"--body-file {args.body_file} resolves outside {scratch}.")
    if not os.path.isfile(real):
        raise ValueError(f"--body-file {args.body_file} does not exist.")
    body = Path(real).read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError(f"--body-file {args.body_file} is empty.")
    return body


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
    branch: str,
    title: str,
    body: str,
    workspace: str,
    repo: str,
    base: str,
    keep_description_url: str = "",
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

    `keep_description_url` inverts that, for a caller that is not re-describing
    the change but adding to it — a conflict merge or a CI fix pushed onto a
    pull request that has been under human review. There the description is
    somebody else's work and rewriting it is pure loss, invisible in the
    output: the skill prints a URL and says nothing about the body. The
    alternative on offer was prose telling the model to read the description
    and hand it back unchanged, which is asking it to reproduce a
    multi-kilobyte markdown document byte-for-byte through its own context.

    It arrives as the URL rather than as a flag because `_keep_description_url`
    has to run before the push, not here: see the reason in its own docstring.

    `workspace` is None in content mode, where there is no directory to run in.
    Nothing here needs one: every call names `--repo` explicitly.
    """
    log(f"Submitting GitOps Pull Request for branch '{branch}'...")

    if keep_description_url:
        log(f"Leaving the description of '{branch}' as its author wrote it.")
        return keep_description_url

    # `--body-file -` rather than `--body`. A pull-request body is the one
    # argument here that carries agent-authored prose of unbounded length, and
    # argv is not where that belongs: it used to be written to the shared volume
    # so the proxy's `gh` could open the path, which is one of the two remaining
    # reasons the two containers need a filesystem in common at all.
    cmd = [
        "gh", "pr", "create",
        "--repo", repo,
        "--title", title,
        "--body-file", "-",
        "--base", base,
        "--head", branch
    ]

    res = subprocess.run(
        cmd, cwd=workspace, input=body, capture_output=True, text=True, check=False
    )
    if res.returncode == 0:
        return res.stdout.strip()

    if "already exists" not in f"{res.stdout}\n{res.stderr}".lower():
        # Anything else — no permission, a protected base, gh not authenticated
        # — is a real failure and keeps the shape `main` already handles.
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)

    log(f"A pull request for '{branch}' is already open; updating it in place.")
    return update_pull_request(branch, title, body, workspace, repo)


def _open_pull_request(branch: str, workspace: str, repo: str) -> str:
    """The URL of the *open* pull request for `branch`, or "" if there is none.

    Two things this does not do, both of which a plain `gh pr view … --jq .url`
    did. It does not count a merged or closed pull request: branch names here
    are derived from the change (`platform-agent/<type>-<target>`), so a branch
    is reused after its pull request merges, and `gh pr view` answers with that
    one. And it does not read a failed lookup as an empty one -- an expired
    installation token and "this branch has no pull request" are opposite
    answers, and collapsing them into "" sends the caller down the branch that
    rewrites a description it was told to keep.
    """
    res = subprocess.run(
        [
            "gh", "pr", "view", branch, "--repo", repo,
            "--json", "url,state",
            "--jq", f'select(.state == "{PR_STATE_OPEN}") | .url',
        ],
        cwd=workspace, capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        if NO_PULL_REQUEST_MARKER in stderr.lower():
            return ""
        raise RuntimeError(
            f"`gh pr view {branch}` failed against {repo}, so whether a pull "
            f"request is open for it is unknown: {stderr or res.returncode}"
        )
    return res.stdout.strip()


def _keep_description_url(branch: str, workspace: str, repo: str) -> str:
    """The open pull request whose description this run must leave alone.

    Resolved before anything is pushed, because the failure it raises is one
    the caller cannot retry out of otherwise. In content mode this script makes
    the commit: discover after the push that there is no pull request to keep,
    and the retry that the error text asks for finds the files already on the
    branch, so `commit` reports nothing to do and refuses. The run would be
    left with commits pushed, no pull request, and no path through this script
    to open one.
    """
    url = _open_pull_request(branch, workspace, repo)
    if not url:
        raise RuntimeError(
            f"--keep-description was given but no pull request is open for "
            f"'{branch}' on {repo}. There is no description to keep. Open it "
            "with a --title and a --body-file first."
        )
    return url


def update_pull_request(
    branch: str, title: str, body: str, workspace: str, repo: str
) -> str:
    """Point the existing pull request for `branch` at the work just pushed.

    Overwrites the title and description. `create_pull_request`'s
    `keep_description_url` is the way past this for a caller that did not write
    them.
    """
    subprocess.run(
        ["gh", "pr", "edit", branch, "--repo", repo, "--title", title, "--body-file", "-"],
        cwd=workspace, input=body, capture_output=True, text=True, check=True,
    )
    url = _open_pull_request(branch, workspace, repo)
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


COMMANDS = ("prepare", "submit", "list", "fetch")


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
    prepare.add_argument("--repo", default=None, help="Target repository as owner/name")

    submit = subparsers.add_parser("submit", help="Push the branch and open the PR")
    submit.add_argument("--branch", required=True, help="Active Git branch name")
    # Not `required=True` any more: `--keep-description` submits without them,
    # and `handle_submit` refuses a call that gives neither. Argparse cannot
    # express "required unless" without a mutually-exclusive group that would
    # also forbid the legitimate `--title` + `--keep-description` combination.
    submit.add_argument("--title", default=None, help="Pull Request title")
    # One group of three, not two of two. `--keep-description` says the
    # description on the pull request is the one to publish, so a body handed
    # over beside it is a body the run would read and throw away.
    description = submit.add_mutually_exclusive_group()
    description.add_argument(
        "--body", default=None, help="Pull Request description body"
    )
    description.add_argument(
        "--body-file",
        default=None,
        help=f"File under {SCRATCH_DIR} holding the description; the safe "
             "channel for a long body",
    )
    description.add_argument(
        "--keep-description",
        action="store_true",
        help="Leave the open pull request's title and body as its author wrote them",
    )
    submit.add_argument(
        "--workspace", default=None, help="The leased workspace from `prepare`"
    )
    submit.add_argument(
        "--lease", default=None, help="Lease id (defaults to the kanban task)"
    )
    submit.add_argument("--repo", default=None, help="Target repository as owner/name")
    submit.add_argument(
        "--handle", default=None,
        help="The broker workspace handle from `prepare` (content mode)",
    )
    submit.add_argument(
        "--from", dest="source", default=None,
        help="Directory whose contents are the change set (content mode)",
    )
    submit.add_argument(
        "--delete", action="append", default=None,
        help="Repository-relative path to delete; repeatable (content mode)",
    )
    submit.add_argument("--base", default=None, help="Base branch (content mode)")
    submit.add_argument(
        "--base-sha", dest="base_sha", default=None,
        help="baseSha from `prepare`; makes the broker refuse a colliding commit",
    )

    # The read half of content mode. Directory mode needs neither: the files are
    # already in the leased clone.
    listing = subparsers.add_parser(
        "list", help="List the repository's files (content mode)"
    )
    listing.add_argument("--handle", required=True, help="Handle from `prepare`")
    listing.add_argument("--prefix", default=None, help="Limit to this directory")
    listing.add_argument("--repo", default=None, help="Target repository as owner/name")

    fetch = subparsers.add_parser(
        "fetch", help="Copy repository files into a scratch directory (content mode)"
    )
    fetch.add_argument("--handle", required=True, help="Handle from `prepare`")
    fetch.add_argument(
        "--path", action="append", required=True,
        help="Repository-relative path to fetch; repeatable",
    )
    fetch.add_argument(
        "--to", required=True, help="Scratch directory to write into"
    )
    fetch.add_argument("--repo", default=None, help="Target repository as owner/name")
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
    return {
        "prepare": handle_prepare,
        "list": handle_list,
        "fetch": handle_fetch,
        "submit": handle_submit,
    }[args.command](args)


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
