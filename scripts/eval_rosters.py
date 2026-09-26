"""The eval rosters under hack/eval/, read the way hack/ci-eval-pr.sh reads them.

Three files hold what the eval gate runs and what blocks (#1546, 2026-09-15):

- ``hack/eval/presubmit-cases.txt`` -- the presubmit matrix, one
  ``./tasks/<id>/task.yaml`` path per line (the script's ``TASKS``);
- ``hack/eval/nightly-cases.txt`` -- what ``EVAL_TIER=nightly`` appends
  (``NIGHTLY_TASKS``);
- ``hack/eval/blocking-roster.txt`` -- one case id per line, the default of
  ``BOOTSTRAP_ADMITTED``.

In every file ``#`` starts a comment and blank lines are skipped, which is
exactly the ``sed | grep`` the script applies; this module is the Python copy
of that parse so the registration lint, the domain-coverage test and the eval
dashboard read the files the shell does and cannot disagree with it about
what is registered. It is stdlib-only on purpose: the dashboard's collector
runs in a workflow with no third-party imports.

The files replaced three bash arrays in the script. ``parse_script_roster``
reads the old ``BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-a,b}"`` default line
out of the text of ``hack/ci-eval-pr.sh`` from a commit before the move
(``git show <commit>:hack/ci-eval-pr.sh``, for roster history). It is a
separate function from ``parse_blocking_roster`` on purpose: a reader always
knows which shape it holds, and a parser that guessed would take a comment in
the roster file that quotes the override syntax for the roster itself.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "hack" / "eval"
PRESUBMIT_CASES_FILE = EVAL_DIR / "presubmit-cases.txt"
NIGHTLY_CASES_FILE = EVAL_DIR / "nightly-cases.txt"
BLOCKING_ROSTER_FILE = EVAL_DIR / "blocking-roster.txt"
# The inject lane's exclusions (#2039): case ids the script drops from TASKS
# under AGENT_TRANSPORT=inject, each with its reason as the comment block
# directly above it.
INJECT_LANE_EXCLUSIONS_FILE = EVAL_DIR / "inject-lane-exclusions.txt"
# An issue reference a reason must carry, so every exclusion names what
# decides when it goes.
ISSUE_REFERENCE_RE = re.compile(r"#\d+")

# One case-file entry: the path the script hands to devops-bench, relative to
# bench/. Anchored, so a commented-out copy is not an entry.
CASE_ENTRY_RE = re.compile(r"^\./tasks/([A-Za-z0-9_-]+)/task\.yaml$")
# A case path anywhere on a line, comment or not: how the validator finds a
# commented-out registration, the parking state that is retired.
CASE_PATH_ANYWHERE_RE = re.compile(r"tasks/([A-Za-z0-9_-]+)/task\.yaml")
# The old script's default line, for text from before the split.
SCRIPT_ROSTER_RE = re.compile(r'BOOTSTRAP_ADMITTED="\$\{BOOTSTRAP_ADMITTED:-([^}]*)\}"')
# bench-gate accepts comma- or whitespace-separated ids in the variable.
ROSTER_SEPARATORS_RE = re.compile(r"[,\s]+")


def entries(text: str) -> list[str]:
    """The non-comment, non-blank lines of one roster file, trimmed, in order."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def read_entries(path: pathlib.Path) -> list[str]:
    return entries(path.read_text(encoding="utf-8"))


def case_names(text: str) -> list[str]:
    """The case ids of a case file's entries, in file order.

    Raises ValueError on an entry that is not a ``./tasks/<id>/task.yaml``
    path, which is what the script does too: a malformed line is a job that
    stops before it spends a cluster, not a case silently skipped.
    """
    names = []
    for entry in entries(text):
        match = CASE_ENTRY_RE.match(entry)
        if match is None:
            raise ValueError(f"{entry!r} is not a ./tasks/<id>/task.yaml path")
        names.append(match.group(1))
    return names


def commented_out_cases(text: str) -> list[str]:
    """Case ids that appear only inside comments: the retired parking state."""
    live = set(case_names(text))
    found = []
    for raw in text.splitlines():
        if "#" not in raw:
            continue
        comment = raw.split("#", 1)[1]
        for name in CASE_PATH_ANYWHERE_RE.findall(comment):
            if name not in live and name not in found:
                found.append(name)
    return found


def parse_blocking_roster(text: str) -> list[str]:
    """Case ids of the blocking roster file: one id per line, ``#`` comments.

    Comments are stripped before anything is read, so a comment that quotes
    the ``BOOTSTRAP_ADMITTED`` override syntax cannot become the roster.
    """
    return entries(text)


def parse_script_roster(text: str) -> list[str]:
    """Case ids of the ``BOOTSTRAP_ADMITTED`` default line in pre-split script text.

    For ``hack/ci-eval-pr.sh`` as it stood before 2026-09-15, where the
    roster was the variable's default. Split the way bench-gate splits the
    variable (comma or whitespace). Raises ValueError when the text carries no
    such line, so a caller that hands it a roster file by mistake hears about
    it rather than getting an empty roster.
    """
    match = SCRIPT_ROSTER_RE.search(text)
    if match is None:
        raise ValueError("no BOOTSTRAP_ADMITTED default line in the text")
    return [name for name in ROSTER_SEPARATORS_RE.split(match.group(1)) if name]


def presubmit_cases(path: pathlib.Path = PRESUBMIT_CASES_FILE) -> list[str]:
    return case_names(path.read_text(encoding="utf-8"))


def nightly_cases(path: pathlib.Path = NIGHTLY_CASES_FILE) -> list[str]:
    return case_names(path.read_text(encoding="utf-8"))


def blocking_roster(path: pathlib.Path = BLOCKING_ROSTER_FILE) -> list[str]:
    return parse_blocking_roster(path.read_text(encoding="utf-8"))


def parse_lane_exclusions(text: str) -> dict[str, str]:
    """Case ids of a lane-exclusion file, each with the reason above it.

    The shape is the blocking roster's -- one id per line, ``#`` comments --
    plus a convention the reason test enforces: the comment lines directly
    above an entry (up to the previous blank line or entry) are its reason.
    A header comment separated from the first entry by a blank line is not a
    reason. The shell reads only the ids, exactly as ``entries`` does; this
    parser exists so the roster test can hold every exclusion to a reason.
    """
    reasons: dict[str, str] = {}
    pending: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            pending = []
            continue
        if stripped.startswith("#"):
            # One marker only: a reason that opens with an issue number
            # (`# #2039: ...`, or `##2039` by mistake) keeps its `#`.
            pending.append(stripped[1:].strip())
            continue
        case = stripped.split("#", 1)[0].strip()
        reasons[case] = " ".join(part for part in pending if part)
        pending = []
    return reasons


def inject_lane_exclusions(path: pathlib.Path = INJECT_LANE_EXCLUSIONS_FILE) -> dict[str, str]:
    return parse_lane_exclusions(path.read_text(encoding="utf-8"))
