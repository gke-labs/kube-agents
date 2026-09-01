#!/usr/bin/env python3
"""Keep the always-loaded agent instruction files inside the harness's budget.

``AGENTS.md`` and ``CLAUDE.md`` are not documents an agent opens when it needs
them -- a coding harness loads them into the context window at the start of
every session, in every checkout, before the first prompt. Their size is a tax
on every task done in this repository, and nothing about paying it is visible
at review time: a pull request that adds a well-argued paragraph to ``AGENTS.md``
looks exactly like one that does not.

That is how this file came to exist. ``AGENTS.md`` went from 14.5k to 42.6k
characters in nine days, across a run of pull requests each adding a rule that
deserved to be there, and the first anyone noticed was Claude Code printing
``AGENTS.md is over the 40.0k-char limit`` at startup. The warning is
only a warning: the file is still loaded whole, so nothing breaks loudly. It
just gets more expensive, indefinitely, until someone re-reads the whole file
and splits it again.

So the budget is checked rather than watched. The remedy when this fails is
almost never to delete a rule -- it is to move the *mechanics* out to a
document the agent opens when it is carrying the rule out, the way
``docs/pull-request-workflow.md`` holds the commands whose rules live in
``AGENTS.md`` and ``.agents/rules/`` holds the mechanics that are prose rather
than commands. Raising ``BUDGET`` is the other option, and it is a real one, but
it should be a decision someone argues for in a pull request rather than the
path of least resistance.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_context_budget.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Chars. Claude Code warns at 40k; this sits below it so the check fires while
# there is still room to land the fix, rather than after the warning is already
# on everyone's screen.
BUDGET = 38_000

# The two roots. CLAUDE.md pulls AGENTS.md in with an `@AGENTS.md` line, which
# the harness expands in place, so counting both files naively would charge
# AGENTS.md twice. Imports are followed instead, and each file is charged once
# however many times it is reached -- the total is then what actually lands in
# the window rather than what is on disk.
FILES = ("AGENTS.md", "CLAUDE.md")

# A bare `@` followed by something path-shaped, alone on its line. The extension
# is required deliberately: `@me` and `@platform-agent` are handles that could
# plausibly appear here one day, and dropping them from the count would shrink
# the budget's idea of the file without anyone seeing it. Matching too narrowly
# fails in the safe direction instead -- an unrecognised import is charged as
# content, so the total is too high and the check complains rather than passing
# a file that had already outgrown the window.
IMPORT_RE = re.compile(r"@[\w./-]+\.[A-Za-z0-9]+")


def is_import(line: str) -> bool:
    """True for a harness import directive (``@AGENTS.md``) on its own line.

    Only a bare ``@path`` counts. A line that mentions an ``@`` inside prose,
    or carries a handle rather than a path, is content and is charged as such.
    """
    return IMPORT_RE.fullmatch(line.strip()) is not None


def loaded_size(path: Path, seen: set[Path] | None = None) -> int:
    """Characters this file contributes to the context window, imports included.

    An import line is replaced by the file it names, not merely dropped.
    Dropping it would make the obvious answer to a failure -- replace a section
    with `@docs/some-page.md` -- register as a saving that never happened: the
    harness still loads the target, so the context cost is unchanged while the
    check reports it gone. A file already counted is not counted again, which
    both models the harness expanding each import once and stops a cycle.
    """
    seen = set() if seen is None else seen
    resolved = path.resolve()
    if resolved in seen:
        return 0
    seen.add(resolved)

    total = 0
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if not is_import(line):
            total += len(line)
            continue
        target = path.parent / line.strip()[1:]
        if target.is_file():
            total += loaded_size(target, seen)
        else:
            # Nothing to expand, so the line is just text the harness shows.
            total += len(line)
    return total


def measure(names: tuple[str, ...] | None = None) -> dict[str, int]:
    """Per-root char counts, sharing one ``seen`` set across all the roots.

    Sharing it is what stops AGENTS.md being charged twice: CLAUDE.md's
    `@AGENTS.md` expands to nothing once AGENTS.md has been counted as a root
    in its own right. The roots are measured in ``FILES`` order, so the file
    that owns the content is the one the breakdown attributes it to.

    Raises ``FileNotFoundError`` if a root is missing -- an instruction file
    that has been renamed or deleted is a broken check, not a zero.
    """
    seen: set[Path] = set()
    sizes = {}
    for name in FILES if names is None else names:
        path = REPO / name
        if not path.is_file():
            raise FileNotFoundError(name)
        sizes[name] = loaded_size(path, seen)
    return sizes


def main() -> int:
    try:
        sizes = measure()
    except FileNotFoundError as missing:
        print(f"MISSING  {missing} -- expected at the repository root")
        return 1

    total = sum(sizes.values())
    breakdown = ", ".join(f"{name} {size / 1000:.1f}k" for name, size in sizes.items())

    if total > BUDGET:
        # Raw counts, not the rounded k figures used elsewhere. Growth arrives a
        # few hundred chars at a time, so the first failure is usually a couple
        # of hundred over -- and "0.0k over the 38k budget" reads as a check that
        # has miscounted rather than one that has just fired.
        over = total - BUDGET
        print(
            f"FAIL: the always-loaded instruction files total {total:,} chars "
            f"({breakdown}), {over:,} over the {BUDGET:,}-char budget.\n"
            "\n"
            "These files are loaded into every session in every checkout, so this is a\n"
            "cost paid by every task in the repository. Prefer moving mechanics out over\n"
            "deleting a rule: mechanics belong in a document the agent opens while\n"
            "carrying the rule out -- docs/pull-request-workflow.md for the ones that are\n"
            "commands, .agents/rules/ for the ones that are prose -- while the rule, its\n"
            "trigger, and its reason stay in AGENTS.md.\n"
            "Raising BUDGET in scripts/check_context_budget.py is a legitimate answer too,\n"
            "but argue for it in the pull request."
        )
        return 1

    print(
        f"Always-loaded instruction files total {total / 1000:.1f}k chars ({breakdown}), "
        f"{(BUDGET - total) / 1000:.1f}k under the {BUDGET:,}-char budget."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
