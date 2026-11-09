#!/usr/bin/env python3
"""Fail when a row of the upgrade page's breaking-changes table names a release that lacks its change.

    python3 scripts/check_upgrade_table_releases.py

The "Breaking changes in recent releases" table labels each row with the release an operator
first meets the change in. A pull request writes that label when it merges, naming the release
its author expects next, and a release cut from an earlier commit leaves the label wrong. Once a
release is tagged, this check finds the commit that added each row labelled with it (the oldest
commit whose diff adds a release or `pending` row whose change cell is the change text, after the
last commit that removed such a row) and requires the tag to contain that commit, or the tag's own copy of the table to hold the row (a change and its row cherry-picked
to the release branch). A row whose release has no tag yet is not checked. Nor is a row for a
release tagged before the table itself was added: those rows were written afterwards, about
releases that had already shipped. A row no commit can be found for is reported, not passed. The
change text is the row's key: a cell that keeps its text and loses a `#<number>: ` prefix still traces to the row's
first commit, and a cell reworded after its release traces to the rewording commit.

Two kinds of row cannot be traced to a commit their release contains: a shipped row whose change cell
was reworded, and a row added after its release was tagged. TRACES_FILE names, for such a row, the
full commit that shipped its change (on main, or the cherry-pick on a release branch); the check then
requires the release to contain that commit instead. A row whose entry a change adds, edits or removes
counts as a row that change touched. A broken entry (not `<40-character commit> <change text>`, a
second entry for the same row, a commit in neither HEAD's history nor a release's, or text that
matches no row) fails a push to main and a change that edits the page or the file, and is a warning
on any other change.

The check needs the history and the release tags, so it fails on a shallow clone or one with no
release tag rather than pass with nothing to compare.

A wrong label fails only where it can be fixed: on a push to main, and on a pull request or
merge-queue group that adds the row or changes its label or change text. A wrong label on a row the
change did not touch is printed as a warning and the check passes, so a release cut that leaves out a
labelled row does not turn every open pull request red, nor one that edits another part of the page;
main's push run stays red until a pull request relabels the row. Run locally, the check compares the
page with upstream/main or origin/main, whichever exists, and fails outright when neither does. A
local run also notes the releases its rows name that have no local tag, which it cannot check.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

UPGRADE_PAGE = "docs/site/src/content/docs/install/upgrade.md"
TABLE_HEADING = "### Breaking changes in recent releases"
# A table row: `| <release> | <change> | ...`. The change cell is the row's stable name.
ROW = re.compile(r"^\| (?P<release>\d+\.\d+\.\d+)\s*\| (?P<change>[^|]+?)\s*\|", re.MULTILINE)
# A table line in a diff: `git log -G` lists the commits that add or remove one. A commit counts as
# adding a row only when one of its added lines is a release row (or one still labelled `pending`)
# whose change cell is the change text, after an optional `#<number>: ` prefix that later cells
# dropped. So the same words in prose, in another table or inside a longer cell do not stand in for
# the row, and a row whose cell later lost the prefix still traces to its start.
DIFF_TABLE_LINE = r"^\| "
TABLE_LINE = re.compile(r"^\| (?:pending|\d+\.\d+\.\d+)\s*\| (?:#\d+:\s*)?(?P<change>[^|]+?)\s*\|")
ADDED = "+"
REMOVED = "-"
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
WARNING_NOTE = ("warning: this change does not add or relabel these rows, so they do not fail it; "
                "main's push run fails until a pull request relabels them.")
# One entry per line, `<full commit> <change text>`; a line whose first non-blank character is `#` is
# a comment. Only a full commit is accepted: a tag name or `HEAD~1` would pass any label, and a short
# hash can turn ambiguous.
TRACES_FILE = "scripts/upgrade_table_traces.txt"
TRACE_COMMENT = "#"
TRACE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
TRACE_HINT = (f" If this row was reworded after its release shipped, or added after its release was tagged, "
              f"name the commit that shipped it in {TRACES_FILE} instead.")
UNTAGGED_NOTE = ("note: rows labelled {releases} are not checked: no such tag is here. If a release is out, "
                 "run `git fetch --tags` and check again.")


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


@cache
def row_commits() -> list[str]:
    """The commits in HEAD's history whose diff of the page adds or removes a table line, oldest first."""
    return git("log", "--format=%H", "--reverse", "-G", DIFF_TABLE_LINE, "HEAD", "--", UPGRADE_PAGE).split()


@cache
def page_diff(commit: str) -> list[str]:
    return git("show", "--format=", commit, "--", UPGRADE_PAGE).splitlines()


def holds(diff: list[str], sign: str, change: str) -> bool:
    return any(line.startswith(sign) and (m := TABLE_LINE.match(line[1:])) and m["change"] == change for line in diff)


def row_commit(change: str) -> str | None:
    """The oldest commit that adds a table row holding `change` since the last commit that removed it."""
    origin = None
    for commit in row_commits():
        diff = page_diff(commit)
        if holds(diff, ADDED, change):
            origin = origin or commit
        elif holds(diff, REMOVED, change):
            origin = None
    return origin


def first_release_containing(commit: str) -> str | None:
    tags = [tag for tag in git("tag", "--contains", commit).split() if RELEASE_TAG.match(tag)]
    return min(tags, key=lambda tag: tuple(int(part) for part in tag.split("."))) if tags else None


def commit_time(commit: str) -> int:
    return int(git("log", "-1", "--format=%ct", commit).strip())


def tag_time(tag: str) -> int:
    return int(git("for-each-ref", "--format=%(creatordate:unix)", f"refs/tags/{tag}").strip())


def file_at(ref: str, path: str) -> str:
    result = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def page_at(tag: str) -> str:
    return file_at(tag, UPGRADE_PAGE)


def working_file(path: str) -> str:
    file = Path(git("rev-parse", "--show-toplevel").strip()) / path
    return file.read_text(encoding="utf-8") if file.exists() else ""


def trace_entries(text: str) -> tuple[dict[str, tuple[int, str]], list[str]]:
    """The well-formed entries of a TRACES_FILE text (change text -> (line, commit)), and the malformed."""
    entries: dict[str, tuple[int, str]] = {}
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(TRACE_COMMENT):
            continue
        where = f"{TRACES_FILE}:{number}"
        parts = stripped.split(None, 1)
        if len(parts) < 2 or not TRACE_COMMIT.match(parts[0]):
            found.append(f"{where}: an entry is `<full 40-character commit> <change text>`")
        elif parts[1].strip() in entries:
            found.append(f"{where}: a second entry for \"{parts[1].strip()}\"; keep one")
        else:
            entries[parts[1].strip()] = (number, parts[0])
    return entries, found


def traces() -> tuple[dict[str, str], list[str]]:
    """TRACES_FILE's usable entries (a row's change text -> commit), and what is wrong with the file."""
    entries, found = trace_entries(working_file(TRACES_FILE))
    changes = {row.change.strip(): row.change for row in page_rows()}
    tags = release_tags()
    usable: dict[str, str] = {}
    for change, (number, commit) in entries.items():
        where = f"{TRACES_FILE}:{number}"
        if not (git_ok("merge-base", "--is-ancestor", commit, "HEAD")
                or any(git_ok("merge-base", "--is-ancestor", commit, f"refs/tags/{tag}") for tag in tags)):
            found.append(f"{where}: {commit} is not a commit in this history or in a release's")
        elif change not in changes:
            found.append(f"{where}: no row of the table has the change text \"{change}\"; remove the entry")
        else:
            usable[changes[change]] = commit
    return usable, found


def clone_problem() -> str | None:
    """Why this clone cannot check anything, or None: it fails the run on every event."""
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return ("the clone is shallow: fetch the full history (fetch-depth: 0) so the commits that added "
                "the rows can be found")
    if not release_tags():
        return ("no release tag (<major>.<minor>.<patch>) is present: fetch the tags so the rows can be "
                "checked against the releases they name")
    return None


def page_rows() -> list[Row]:
    # The page as it is in the working tree, so the check reads an edit before it is committed.
    return rows(working_file(UPGRADE_PAGE))


def problems(traced: dict[str, str]) -> list[tuple[Row, str]]:
    tags = release_tags()
    found = []
    table_added = oldest_adding(TABLE_HEADING)
    table_time = commit_time(table_added) if table_added else 0
    for row in page_rows():
        if row.release not in tags or tag_time(row.release) < table_time:
            continue
        where = f"{UPGRADE_PAGE}:{row.line}"
        if row.change in traced:
            if git_ok("merge-base", "--is-ancestor", traced[row.change], f"refs/tags/{row.release}"):
                continue
            found.append((row, f"{where}: the row \"{row.change}\" is labelled {row.release}, but {row.release} "
                               f"does not contain {traced[row.change][:12]}, the commit {TRACES_FILE} names for it"))
            continue
        commit = row_commit(row.change)
        if commit is None:
            found.append((row, f"{where}: no commit in this history adds the row \"{row.change}\", so its "
                               f"{row.release} label cannot be checked"))
            continue
        if git_ok("merge-base", "--is-ancestor", commit, f"refs/tags/{row.release}"):
            continue
        if any(tagged.change == row.change for tagged in rows(page_at(row.release))):
            continue
        first = first_release_containing(commit)
        label = f"{first}, the first release that contains it" if first else "the next release, which will contain it"
        found.append((row, f"{where}: the row \"{row.change}\" is labelled {row.release}, but {row.release} "
                           f"does not contain {commit[:12]}, the commit that added it. Relabel it as {label}."
                           + TRACE_HINT))
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


def main() -> int:
    unusable = clone_problem()
    if unusable:
        print(unusable, file=sys.stderr)
        return EXIT_FAILED
    if not os.environ.get(EVENT_NAME_ENV):
        untagged = sorted({row.release for row in page_rows()} - release_tags())
        if untagged:
            print(UNTAGGED_NOTE.format(releases=", ".join(untagged)), file=sys.stderr)
    traced, trace_problems = traces()
    found = problems(traced)
    base = comparison_base()
    # The rows the base already had, with the same label, change text and trace entry: this change did not
    # touch them.
    untouched = {(row.release, row.change) for row in rows(page_at(base))} if base is not None else set()
    base_entries = trace_entries(file_at(base, TRACES_FILE))[0] if base is not None else {}
    entries = trace_entries(working_file(TRACES_FILE))[0]
    for row, _ in found:
        if base_entries.get(row.change.strip(), (0, ""))[1] != entries.get(row.change.strip(), (0, ""))[1]:
            untouched.discard((row.release, row.change))
    warned = [problem for row, problem in found if (row.release, row.change) in untouched]
    failed = [problem for row, problem in found if (row.release, row.change) not in untouched]
    for problem in warned:
        print(WARNING_PREFIX + problem, file=sys.stderr)
    if warned:
        print(WARNING_NOTE, file=sys.stderr)
    # A broken entry fails a push to main and a change that edits the page or the file, and warns on any other.
    if base is not None and (page_at(base), file_at(base, TRACES_FILE)) == (working_file(UPGRADE_PAGE),
                                                                             working_file(TRACES_FILE)):
        warned_traces, failed_traces = trace_problems, []
    else:
        warned_traces, failed_traces = [], trace_problems
    for problem in warned_traces:
        print(WARNING_PREFIX + problem, file=sys.stderr)
    for problem in failed + failed_traces:
        print(problem, file=sys.stderr)
    return EXIT_FAILED if failed or failed_traces else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
