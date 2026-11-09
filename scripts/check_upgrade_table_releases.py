#!/usr/bin/env python3
"""Fail when a row of the upgrade page's breaking-changes table names a release that lacks its change.

    python3 scripts/check_upgrade_table_releases.py

The "Breaking changes in recent releases" table labels each row with the release an operator
first meets the change in. A pull request writes that label when it merges, naming the release
its author expects next, and a release cut from an earlier commit leaves the label wrong. Once a
release is tagged, this check finds the commit that added each row labelled with it (the oldest
commit whose diff adds a table cell holding the row's change text) and requires the tag to contain
that commit, or the tag's own copy of the table to hold the row (a change and its row cherry-picked
to the release branch). A row whose release has no tag yet is not checked. Nor is a row for a
release tagged before the table itself was added: those rows were written afterwards, about
releases that had already shipped. A row no commit can be found for is reported, not passed. The
change text is the row's key: a cell that keeps its text and loses a prefix still traces to the row's
first commit, and a cell reworded after its release traces to the rewording commit.

The check needs the history and the release tags, so it fails on a shallow clone or one with no
release tag rather than pass with nothing to compare.

A wrong label fails only where it can be fixed: a push to main, and a pull request or merge-queue
group that changes the page. Elsewhere it is printed as a warning and the check passes, so a release
cut that leaves out a labelled row does not turn every open pull request red; main's push run stays
red until a pull request relabels the row. Run locally, the check compares the page with
upstream/main or origin/main, whichever exists, and fails outright when neither does.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UPGRADE_PAGE = "docs/site/src/content/docs/install/upgrade.md"
TABLE_HEADING = "### Breaking changes in recent releases"
# A table row: `| <release> | <change> | ...`. The change cell is the row's stable name.
ROW = re.compile(r"^\| (?P<release>\d+\.\d+\.\d+)\s*\| (?P<change>[^|]+?)\s*\|", re.MULTILINE)
# A line a diff adds to the table starts with this; a commit counts as adding a row only when one of
# its added lines is a table row holding the change text, so a mention of the same words in prose
# does not stand in for the row, and a row whose cell later lost a prefix still traces to its start.
ADDED_TABLE_LINE = "+|"
RELEASE_TAG = re.compile(r"^\d+\.\d+\.\d+$")
EXIT_OK = 0
EXIT_FAILED = 1
# The GitHub Actions events this check reads, and the base each one is compared with.
EVENT_NAME_ENV = "GITHUB_EVENT_NAME"
EVENT_PATH_ENV = "GITHUB_EVENT_PATH"
PULL_REQUEST_EVENT = "pull_request"
MERGE_GROUP_EVENT = "merge_group"
PUSH_EVENT = "push"
# Run outside CI, the page is compared with the first of these that exists.
LOCAL_BASE_REFS = ("upstream/main", "origin/main")
WARNING_PREFIX = "warning: "
WARNING_NOTE = ("warning: this change does not touch the upgrade page, so these rows do not fail it; "
                "main's push run fails until a pull request relabels them.")


@dataclass(frozen=True)
class Row:
    line: int
    release: str
    change: str


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], capture_output=True).returncode == 0


def rows(text: str) -> list[Row]:
    return [Row(text.count("\n", 0, match.start()) + 1, match["release"], match["change"])
            for match in ROW.finditer(text)]


def release_tags() -> set[str]:
    return {tag for tag in git("tag", "--list").split() if RELEASE_TAG.match(tag)}


def commits_adding(text: str) -> list[str]:
    """The commits in HEAD's history whose diff of the page changes how often `text` occurs, oldest first."""
    return git("log", "--format=%H", "--reverse", "-S", text, "HEAD", "--", UPGRADE_PAGE).split()


def oldest_adding(text: str) -> str | None:
    commits = commits_adding(text)
    return commits[0] if commits else None


def row_commit(change: str) -> str | None:
    """The oldest commit whose diff of the page adds a table row holding `change`."""
    for commit in commits_adding(change):
        diff = git("show", "--format=", commit, "--", UPGRADE_PAGE).splitlines()
        if any(line.startswith(ADDED_TABLE_LINE) and change in line for line in diff):
            return commit
    return None


def commit_time(commit: str) -> int:
    return int(git("log", "-1", "--format=%ct", commit).strip())


def tag_time(tag: str) -> int:
    return int(git("for-each-ref", "--format=%(creatordate:unix)", f"refs/tags/{tag}").strip())


def page_at(tag: str) -> str:
    result = subprocess.run(["git", "show", f"{tag}:{UPGRADE_PAGE}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def clone_problem() -> str | None:
    """Why this clone cannot check anything, or None: it fails the run on every event."""
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return ("the clone is shallow: fetch the full history (fetch-depth: 0) so the commits that added "
                "the rows can be found")
    if not release_tags():
        return ("no release tag (<major>.<minor>.<patch>) is present: fetch the tags so the rows can be "
                "checked against the releases they name")
    return None


def problems() -> list[str]:
    tags = release_tags()
    found = []
    table_added = oldest_adding(TABLE_HEADING)
    table_time = commit_time(table_added) if table_added else 0
    # The page as it is in the working tree, so the check reads an edit before it is committed.
    page = Path(git("rev-parse", "--show-toplevel").strip()) / UPGRADE_PAGE
    for row in rows(page.read_text(encoding="utf-8")):
        if row.release not in tags or tag_time(row.release) < table_time:
            continue
        where = f"{UPGRADE_PAGE}:{row.line}"
        commit = row_commit(row.change)
        if commit is None:
            found.append(f"{where}: no commit in this history adds the row \"{row.change}\", so its "
                         f"{row.release} label cannot be checked")
            continue
        if git_ok("merge-base", "--is-ancestor", commit, f"refs/tags/{row.release}"):
            continue
        if any(tagged.change == row.change for tagged in rows(page_at(row.release))):
            continue
        found.append(f"{where}: the row \"{row.change}\" is labelled {row.release}, but {row.release} does "
                     f"not contain {commit[:12]}, the commit that added it. Relabel it with the first "
                     "release that contains that commit. (If that commit only reworded the change cell "
                     "of a row that shipped earlier, keep the cell's released wording.)")
    return found


def comparison_base() -> str | None:
    """The commit this run's page is compared with, or None when every wrong label fails the run."""
    event = os.environ.get(EVENT_NAME_ENV, "")
    if event == PUSH_EVENT:
        return None
    if event in (PULL_REQUEST_EVENT, MERGE_GROUP_EVENT):
        # A pull request runs on its merge commit, whose first parent is the base it merges into.
        if event == PULL_REQUEST_EVENT and len(git("rev-list", "--parents", "-n", "1", "HEAD").split()) > 2:
            return git("rev-parse", "HEAD^1").strip()
        payload = json.loads(Path(os.environ.get(EVENT_PATH_ENV, "")).read_text(encoding="utf-8"))
        if event == MERGE_GROUP_EVENT:
            return payload["merge_group"]["base_sha"]
        return payload["pull_request"]["base"]["sha"]
    for ref in LOCAL_BASE_REFS:
        if git_ok("rev-parse", "--verify", "-q", ref):
            return git("merge-base", "HEAD", ref).strip()
    return None


def page_changed_since(base: str) -> bool:
    page = Path(git("rev-parse", "--show-toplevel").strip()) / UPGRADE_PAGE
    before = subprocess.run(["git", "show", f"{base}:{UPGRADE_PAGE}"], capture_output=True, text=True)
    return before.returncode != 0 or before.stdout != page.read_text(encoding="utf-8")


def main() -> int:
    unusable = clone_problem()
    if unusable:
        print(unusable, file=sys.stderr)
        return EXIT_FAILED
    found = problems()
    if not found:
        return EXIT_OK
    base = comparison_base()
    if base is not None and not page_changed_since(base):
        for problem in found:
            print(WARNING_PREFIX + problem, file=sys.stderr)
        print(WARNING_NOTE, file=sys.stderr)
        return EXIT_OK
    for problem in found:
        print(problem, file=sys.stderr)
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
