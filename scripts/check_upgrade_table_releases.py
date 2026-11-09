#!/usr/bin/env python3
"""Fail when a row of the upgrade page's breaking-changes table names a release that lacks its change.

    python3 scripts/check_upgrade_table_releases.py

The "Breaking changes in recent releases" table labels each row with the release an operator
first meets the change in. A pull request writes that label when it merges, naming the release
its author expects next, and a release cut from an earlier commit leaves the label wrong. Once a
release is tagged, this check finds the commit that added each row labelled with it (the oldest
commit whose diff adds the row's change text) and requires the tag to contain that commit, or the
tag's own copy of the page to hold the row (a change and its row cherry-picked to the release
branch). A row added after the tag by a commit that touches only documentation is a record
written about a release that already shipped, and is not checked. A row whose release has no tag
yet is not checked either. A row no commit can be found for is reported, not passed.

The check needs the history and the release tags, so it fails on a shallow clone or one with no
release tag rather than pass with nothing to compare.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UPGRADE_PAGE = "docs/site/src/content/docs/install/upgrade.md"
# A table row: `| <release> | <change> | ...`. The change cell is the row's stable name.
ROW = re.compile(r"^\| (?P<release>\d+\.\d+\.\d+)\s*\| (?P<change>[^|]+?)\s*\|", re.MULTILINE)
RELEASE_TAG = re.compile(r"^\d+\.\d+\.\d+$")
# A commit whose files all match one of these only documents; it changes nothing an install runs.
DOCUMENTATION_PREFIXES = ("docs/",)
DOCUMENTATION_SUFFIXES = (".md",)
EXIT_OK = 0
EXIT_FAILED = 1


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


def adding_commit(change: str) -> str | None:
    """The oldest commit in HEAD's history whose diff of the page adds `change`."""
    commits = git("log", "--format=%H", "--reverse", "-S", change, "HEAD", "--", UPGRADE_PAGE).split()
    return commits[0] if commits else None


def documentation_only(commit: str) -> bool:
    files = git("show", "--format=", "--name-only", commit).split()
    return bool(files) and all(f.startswith(DOCUMENTATION_PREFIXES) or f.endswith(DOCUMENTATION_SUFFIXES)
                               for f in files)


def commit_time(commit: str) -> int:
    return int(git("log", "-1", "--format=%ct", commit).strip())


def tag_time(tag: str) -> int:
    return int(git("for-each-ref", "--format=%(creatordate:unix)", f"refs/tags/{tag}").strip())


def page_at(tag: str) -> str:
    result = subprocess.run(["git", "show", f"{tag}:{UPGRADE_PAGE}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def problems() -> list[str]:
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return ["the clone is shallow: fetch the full history (fetch-depth: 0) so the commits that added "
                "the rows can be found"]
    tags = release_tags()
    if not tags:
        return ["no release tag (<major>.<minor>.<patch>) is present: fetch the tags so the rows can be "
                "checked against the releases they name"]
    found = []
    # The page as it is in the working tree, so the check reads an edit before it is committed.
    page = Path(git("rev-parse", "--show-toplevel").strip()) / UPGRADE_PAGE
    for row in rows(page.read_text(encoding="utf-8")):
        if row.release not in tags:
            continue
        where = f"{UPGRADE_PAGE}:{row.line}"
        commit = adding_commit(row.change)
        if commit is None:
            found.append(f"{where}: no commit in this history adds the row \"{row.change}\", so its "
                         f"{row.release} label cannot be checked")
            continue
        if git_ok("merge-base", "--is-ancestor", commit, f"refs/tags/{row.release}"):
            continue
        if row.change in page_at(row.release):
            continue
        if commit_time(commit) > tag_time(row.release) and documentation_only(commit):
            continue
        found.append(f"{where}: the row \"{row.change}\" is labelled {row.release}, but {row.release} does "
                     f"not contain {commit[:12]}, the commit that added it. Relabel it with the first "
                     "release that contains that commit.")
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(problem, file=sys.stderr)
    return EXIT_FAILED if found else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
