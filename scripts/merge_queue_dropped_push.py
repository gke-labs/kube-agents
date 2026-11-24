#!/usr/bin/env python3
"""Tell the author when the merge queue merged less than the pull request's last head.

GitHub's merge queue merges the commit it built when the pull request entered
the queue. On a push to the pull request after that, GitHub does not remove it
from the queue and does not change what merges, but still shows the pushed commit
as the merged head. The pushed commits are then not on the base branch, and
nothing says so. `merge-gate.yml` runs this once a pull request is merged, and
it posts one comment when that happened: which commits are missing, and what
to do.

The test is about content, not commit ids. The queue squash-merges, so the
merged commit never has the head's tree once the base has moved, and the queue
entry records the queue's own commit rather than the head it was built from.
So this repeats the queue's merge: it merges the final head into the merged
commit's parent with `git merge-tree`, and compares the result with the tree
that was merged.

  - Same tree: the base branch has everything the final head has. A late push
    that only merged the base branch into the pull request lands here, because
    it adds nothing of the author's.
  - Different tree: the queue merged an earlier head. The comment goes out. To
    name the missing commits, the same merge is tried on the head's ancestors,
    newest first; the first one that gives the merged tree is the head the
    queue built from, and the commits after it, merges left out, are the
    missing ones. When no ancestor gives it (the head was force-pushed after
    the entry was made), the comment goes out without a list.
  - No answer: the final head conflicts with the merged commit's parent, or the
    merged commit is not a squash (one parent). Nothing is posted and the log
    says why. A wrong notice costs more than a missed one.

Git reads the two commits from a fetch into an empty repository of its own.
Nothing from the pull request is checked out or run: `git merge-tree` writes
tree objects and touches no work tree, and an empty bare repository holds no
hook, attribute or merge driver for it to read.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from github_api import GitHubAPI, has_marker_comment, log  # noqa: E402

#: Opens the comment, so that a re-run of the job can tell it already posted one.
MARKER = "<!-- merge-queue-dropped-push -->"
USER_AGENT = "kube-agents-merge-queue-dropped-push"
#: Where the commits are fetched from when `GITHUB_SERVER_URL` is not set.
DEFAULT_SERVER_URL = "https://github.com"
SERVER_URL_ENV = "GITHUB_SERVER_URL"
REPOSITORY_ENV = "GITHUB_REPOSITORY"
TOKEN_ENV = "GITHUB_TOKEN"
FETCH_URL = "{server}/{repo}.git"
#: The branch the comment names when the pull request payload carries none.
DEFAULT_BASE = "main"
#: Characters of a commit id the comment and the log show.
SHORT_SHA = 12
#: Ancestors of the final head tried as the head the queue built from. A late push is a few
#: commits; past this many the comment goes out without the list.
CANDIDATES_CEILING = 50
#: Missing commits the comment lists by name; the rest are counted.
COMMITS_LISTED = 20
#: Characters of a commit subject the comment shows.
SUBJECT_MAX = 100
#: A commit subject is the author's text. It goes in a code span, so that an `@name` or `#123` in
#: it notifies nobody and links nothing; a backtick would end the span, so it is replaced.
CODE_SPAN_DELIMITER = "`"
CODE_SPAN_REPLACEMENT = "'"
#: Seconds one git command may take. The fetch of the whole history is the long one: about
#: twelve seconds for this repository.
GIT_TIMEOUT_SECONDS = 300
#: `git merge-tree --write-tree` exits 0 on a clean merge and 1 on a conflict.
MERGE_CLEAN = 0
MERGE_CONFLICT = 1
#: `cat-file -e` exits with one of these when the repository does not hold the object.
OBJECT_ABSENT = (1, 128)
#: A squash commit has one parent.
SQUASH_PARENTS = 1
#: Keeps git from waiting on a prompt for credentials the fetch does not need.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
#: `git log` fields of one missing commit, split on the tab.
COMMIT_FORMAT = "--format=%H%x09%s"
FIELD_SEPARATOR = "\t"

COMPLETE = "complete"
DROPPED = "dropped"
UNDECIDED = "undecided"

HEADLINE_ONE = "The merge queue merged this pull request without its last commit. `{base}` does not have it:"
HEADLINE_MANY = "The merge queue merged this pull request without its last {count} commits. `{base}` does not have them:"
HEADLINE_UNLISTED = (
    "The merge queue merged an earlier version of this pull request. "
    "`{base}` does not have all the changes of its final head {head}."
)
COMMIT_LINE = "- {sha} `{subject}`"
MORE_LINE = "- and {count} more"
BUILT_FROM = (
    "The queue merges what it built when the pull request entered the queue. "
    "It built from {built} and merged the result as {merged}. "
    "A push after that time does not change what the queue merges."
)
BUILT_FROM_UNKNOWN = (
    "The queue merges what it built when the pull request entered the queue. "
    "It merged the result as {merged}. "
    "A push after that time does not change what the queue merges."
)
ADVICE = (
    "To merge the missing changes, rebase the branch onto `{base}` and open a new pull request from it. "
    "Do not push to a pull request while it is in the merge queue."
)


class Git:
    """Git commands against one repository directory."""

    def __init__(self, directory):
        self.directory = str(directory)

    def run(self, *args, ok=(0,)):
        """The command's exit code and stripped output; an exit code outside `ok` is raised."""
        result = subprocess.run(
            ["git", "-C", self.directory, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env={**os.environ, **GIT_ENV},
        )
        if result.returncode not in ok:
            raise RuntimeError(f"git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
        return result.returncode, result.stdout.strip()

    def output(self, *args):
        return self.run(*args)[1]


def short(sha):
    return sha[:SHORT_SHA]


def merged_tree(git, base, head):
    """The tree a merge of `head` into `base` gives, or None when the two conflict."""
    code, out = git.run("merge-tree", "--write-tree", base, head, ok=(MERGE_CLEAN, MERGE_CONFLICT))
    if code == MERGE_CONFLICT:
        return None
    return out.split()[0]


def built_from(git, parent, head, tree):
    """The newest ancestor of `head` whose merge into `parent` gives `tree`, or None."""
    listed = git.output("rev-list", "--topo-order", f"--max-count={CANDIDATES_CEILING + 1}", head, f"^{parent}")
    # The first one is the head itself, which the caller has already tried.
    for candidate in listed.split()[1:]:
        if merged_tree(git, parent, candidate) == tree:
            return candidate
    return None


def missing_commits(git, built, head, merge_commit):
    """`(sha, subject)` of the commits on `head` that are neither on `built` nor on the base, oldest first.

    Merge commits are left out: a merge of the base adds nothing of the author's, and the
    commits it brings in are on the base already.
    """
    out = git.output(
        "log", "--topo-order", "--reverse", "--no-merges", COMMIT_FORMAT, head, f"^{built}", f"^{merge_commit}"
    )
    # An empty subject leaves a line that ends at the separator, or at the commit id on the last line.
    return [(sha, subject) for sha, _, subject in (line.partition(FIELD_SEPARATOR) for line in out.splitlines()) if sha]


def decide(git, merge_commit, head):
    """What the merged commit holds of the final head: a verdict and what the comment needs.

    Returns `{"verdict": COMPLETE | DROPPED | UNDECIDED, "why": str, "built": sha | None,
    "commits": [(sha, subject), ...]}`. The module docstring has the rule.
    """
    parents = git.output("rev-list", "--parents", "--max-count=1", merge_commit).split()[1:]
    if len(parents) != SQUASH_PARENTS:
        return _result(UNDECIDED, f"{short(merge_commit)} has {len(parents)} parents; it is not a squash merge")
    parent = parents[0]
    tree = git.output("rev-parse", f"{merge_commit}^{{tree}}")
    expected = merged_tree(git, parent, head)
    if expected is None:
        return _result(UNDECIDED, f"the final head {short(head)} conflicts with {short(parent)}, the base it merged on")
    if expected == tree:
        return _result(COMPLETE, f"{short(merge_commit)} holds everything the final head {short(head)} has")
    built = built_from(git, parent, head, tree)
    if built is None:
        return _result(DROPPED, f"{short(merge_commit)} differs from the final head {short(head)}; no ancestor gives it")
    commits = missing_commits(git, built, head, merge_commit)
    why = f"{short(merge_commit)} was built from {short(built)}; later commits missing: {len(commits)}"
    return _result(DROPPED, why, built=built, commits=commits)


def _result(verdict, why, built=None, commits=()):
    return {"verdict": verdict, "why": why, "built": built, "commits": list(commits)}


def _subject(text):
    return text.replace(CODE_SPAN_DELIMITER, CODE_SPAN_REPLACEMENT)[:SUBJECT_MAX]


def comment_body(base, merge_commit, head, built=None, commits=()):
    """The comment for a merge that left commits out. It names no account."""
    commits = list(commits)
    if not built or not commits:
        lines = [MARKER, HEADLINE_UNLISTED.format(base=base, head=short(head)), ""]
        lines.append(BUILT_FROM_UNKNOWN.format(merged=short(merge_commit)))
    else:
        headline = (HEADLINE_ONE if len(commits) == 1 else HEADLINE_MANY).format(base=base, count=len(commits))
        lines = [MARKER, headline, ""]
        lines += [COMMIT_LINE.format(sha=short(sha), subject=_subject(subject)) for sha, subject in commits[:COMMITS_LISTED]]
        if len(commits) > COMMITS_LISTED:
            lines.append(MORE_LINE.format(count=len(commits) - COMMITS_LISTED))
        lines += ["", BUILT_FROM.format(built=short(built), merged=short(merge_commit))]
    lines += ["", ADVICE.format(base=base), ""]
    return "\n".join(lines)


def fetch(git, url, commits):
    """Bring the commits and their history into the repository, unless it already has them."""
    wanted = [sha for sha in commits if git.run("cat-file", "-e", f"{sha}^{{commit}}", ok=(0, *OBJECT_ABSENT))[0]]
    if wanted:
        git.run("fetch", "--quiet", "--no-tags", url, *wanted)


def check(api, git, number, url, dry_run=False):
    """Decide for one pull request and post the comment when commits were left out. Returns a log line."""
    pull = api.get(f"/repos/{api.repo}/pulls/{number}")
    merge_commit = pull.get("merge_commit_sha")
    head = (pull.get("head") or {}).get("sha")
    if not pull.get("merged") or not merge_commit or not head:
        return f"#{number}: not merged; nothing to compare"
    fetch(git, url, (merge_commit, head))
    result = decide(git, merge_commit, head)
    if result["verdict"] != DROPPED:
        return f"#{number}: {result['verdict']}: {result['why']}"
    if has_marker_comment(api, number, MARKER):
        return f"#{number}: {result['why']}; comment already posted"
    base = (pull.get("base") or {}).get("ref") or DEFAULT_BASE
    body = comment_body(base, merge_commit, head, result["built"], result["commits"])
    if dry_run:
        return f"#{number}: {result['why']}; would post this comment:\n{body}"
    api.post(f"/repos/{api.repo}/issues/{number}/comments", {"body": body})
    return f"#{number}: {result['why']}; posted the comment"


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get(REPOSITORY_ENV), help="owner/name")
    parser.add_argument("--number", type=int, required=True, help="the merged pull request")
    parser.add_argument("--dry-run", action="store_true", help="log the verdict, post nothing")
    parser.add_argument("--git-dir", help="a full clone to read instead of a new fetch, for a local run")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    token = os.environ.get(TOKEN_ENV)
    if not args.repo or not token:
        print(f"{REPOSITORY_ENV} (or --repo) and {TOKEN_ENV} are required", file=sys.stderr)
        return 2
    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
    url = FETCH_URL.format(server=os.environ.get(SERVER_URL_ENV) or DEFAULT_SERVER_URL, repo=args.repo)
    if args.git_dir:
        log(check(api, Git(args.git_dir), args.number, url, dry_run=args.dry_run))
        return 0
    with tempfile.TemporaryDirectory() as directory:
        git = Git(directory)
        git.run("init", "--quiet", "--bare")
        log(check(api, git, args.number, url, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
