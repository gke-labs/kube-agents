#!/usr/bin/env python3
"""Fail when a published site page carries a maintainer identifier.

``docs/site/src/content/docs/`` is for people installing and operating
kube-agents on their own clusters (``.agents/rules/documentation.md``, "Who
the site is for"). Maintainer runbooks drift onto it one paragraph at a time,
and each one brings the identifiers a runbook needs: a workflow secret name, a
GitHub App ID, the Prow project, a service account in a project no reader owns.
Pull-request and issue numbers drift on the same way, as the reason a
behaviour exists (``.agents/rules/documentation.md``, "PR and issue numbers
are history, not reasons"). This check makes both a CI failure instead of a
review-time hope. Four sources feed it:

* **The denylist** -- ``scripts/docs_audience_denylist.txt``, one regular
  expression per line. Shapes (``secrets.X``) and the few literals a shape
  cannot express live there, so the next identifier is a one-line addition.
* **Project IDs from ``hack/ci-env.sh``** -- every ``export ..PROJECT_ID=``
  line's literal default, read at run time so the denylist never repeats a
  value that script already owns.
* **Service-account emails** -- any ``@<project>.iam.gserviceaccount.com``
  whose project segment is a real-looking ID rather than a placeholder
  (``<project>``, ``${PROJECT_ID}``, ``your-project``) or a Google service
  agent domain (``gcp-sa-*``, ``*-robot``, ``*-system``, ``cloudservices``).
* **Pull-request and issue citations** -- a bare ``#NNN`` of three or more
  digits, a ``github.com/gke-labs/kube-agents/pull|issues/N`` URL, or a link
  whose text carries the number and whose target starts with that URL (any
  path, query, fragment or title after it is part of the link), counted once
  each; a reference-style link counts its text and its definition
  separately. This shape reads prose only: fenced code blocks, inline code
  spans and HTML or MDX comments are blanked first, line by line so the
  reported line numbers stay true, which is what keeps a shell comment inside
  a fence from matching. A comment opener nothing later in the page closes is
  text, as ``scripts/check_docs_links.py`` reads it, so a stray ``<!--`` does
  not hide the rest of the page. A ``#NNN`` glued to a word or a ``/``
  (``kubernetes/kubernetes#68078``) is a cross-repository reference, one
  followed by a word character, ``-`` or ``;`` (``#404-errors``, ``&#160;``)
  is an anchor or an entity, and both are excused. ``contributing.md`` is
  exempt from this shape alone: it is the one site page written for
  contributors.

The pages that carried findings when a shape arrived are pinned in
``scripts/docs_audience_allowlist.txt``: one line per page and shape, with
the count the page has today. Only the shapes that arrived with a backlog
can be pinned; a denylist shape that has always held at zero stays
unpinnable, so the list cannot be used to ship a workflow secret or a
project ID. The list only shrinks. A page with more findings than its entry
fails as a rise; a page with fewer fails until the entry is lowered or
removed; an entry naming a page that is gone, another shape, a zero count,
a repeated page and shape, or a malformed line fails the run. A page with
no entry is held at zero.

The check also fails when it finds no site page at all, or derives no project
ID from ``hack/ci-env.sh``: either would otherwise turn the gate green with
nothing checked, the moment the site root moves or the export is reworded.

The check prints the file, line, and the shape that matched. It prints the
matched text too: the text is already in a tracked site page, so the log
reveals nothing the tree does not.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_audience.py
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The published site: the only tree the audience rule binds.
SITE_ROOT = REPO / "docs" / "site" / "src" / "content" / "docs"
SITE_SUFFIXES = (".md", ".mdx")

# One regular expression per line; `#` lines and blank lines are ignored, as
# in every list file this check reads.
DENYLIST = REPO / "scripts" / "docs_audience_denylist.txt"
LIST_FILE_COMMENT_PREFIX = "#"

# The script CI sources before every eval run; its `export` lines carry the
# maintainers' project IDs as literal defaults. Only variables named *PROJECT_ID
# are read: the same file exports the default host-cluster name, which the
# quickstart legitimately shows. Either quote style, a `${VAR:-default}` or a
# bare literal, and a trailing comment are accepted, so a reformat of the line
# does not empty the derived list.
CI_ENV = REPO / "hack" / "ci-env.sh"
CI_ENV_PROJECT_EXPORT_RE = re.compile(
    r"^export\s+[A-Z_]*PROJECT_ID="
    r"(?:[\"']?\$\{[A-Z_]+:-[\"']?(?P<default>[a-z][a-z0-9-]+)[\"']?\}[\"']?"
    r"|[\"']?(?P<literal>[a-z][a-z0-9-]+)[\"']?)"
    r"\s*(?:#.*)?$"
)
# A derived project ID is matched as a whole word, so `evals` inside another
# identifier is not a hit while `<id>-3`, a pool member, still is.
WORD_BOUNDARY = r"\b"

# A service-account email in a real project. The project segment is matched
# case-sensitively in lowercase, which is what keeps `PROJECT_ID`, `<project>`
# and `${PROJECT_ID}` from matching at all; the placeholder words a doc writes
# in lowercase are excused below, as are Google's own service-agent domains
# (the `gcp-sa-*` family and the older per-product ones a GKE prerequisite page
# has to show).
SERVICE_ACCOUNT_RE = re.compile(r"@(?P<project>[a-z][a-z0-9-]+)\.iam\.gserviceaccount\.com")
PLACEHOLDER_PROJECT_RE = re.compile(r"^(?:your-|my-|example|project)")
GOOGLE_AGENT_PROJECT_RE = re.compile(
    r"^(?:gcp-sa-|cloudservices$|cloudbuild$|containerregistry$|[a-z-]+-robot$|[a-z-]+-system$)"
)
SERVICE_ACCOUNT_SHAPE = "service-account email in a non-placeholder project"

# A pull-request or issue citation in prose. The three forms are one
# alternation so `finditer` counts a linked citation once: the link branch is
# tried first and consumes the number in its text, the URL it points at and
# whatever follows the URL up to the closing parenthesis (a `/files` path, a
# `?diff=` query, a `#issuecomment` fragment, a quoted title), so no form of
# the target falls through to the other two branches; a reference-style link
# keeps its text and its definition on separate lines and counts twice, a
# limit this grammar accepts. The bare `#NNN` branch asks for three or more digits so a
# `#2` list marker does not match, its lookbehind excuses a number glued to a
# word or a `/` (a cross-repository reference, `kubernetes/kubernetes#68078`),
# and its lookahead excuses a number that runs on into a word, a `-` or a `;`
# (a heading anchor such as `#404-errors`, a numeric entity such as `&#160;`).
REPO_ISSUE_URL_RE = r"(?:https?://)?github\.com/gke-labs/kube-agents/(?:pull|issues)/[0-9]+"
CITATION_RE = re.compile(
    rf"\[[^\]]*#[0-9]{{3,}}[^\]]*\]\({REPO_ISSUE_URL_RE}[^)]*\)"
    rf"|(?<![\w/])#[0-9]{{3,}}(?![-;\w])"
    rf"|{REPO_ISSUE_URL_RE}"
)
CITATION_SHAPE = "pull-request or issue citation"
# The one site page written for contributors (.agents/rules/documentation.md,
# "Who the site is for"); relative to the site root.
CITATION_EXEMPT_PAGES = (Path("contributing.md"),)

# What the citation shape does not read. A fenced code block: a backtick or
# tilde fence at any indentation (a fence inside a list item sits at the item's
# content column), closed by a fence of the same character at least as long
# with nothing after it; a backtick fence's info string may not contain a
# backtick (CommonMark), which is what keeps a line opening with a
# triple-backtick code span from opening a fence, while a tilde fence's may.
# An inline code span: a backtick run closed by a run of the same length on
# the same line. A comment, HTML or MDX, which may span lines, believed only
# when its closer follows somewhere in the page. All are blanked in place so
# line numbers stay true, and code spans go first so a comment opener quoted
# in one does not open a comment; the text after a comment closes gets the
# same treatment.
FENCE_RE = re.compile(r"^\s*(?P<fence>(?P<backtick>`{3,})|~{3,})(?P<info>(?(backtick)[^`]*|.*))$")
CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?!`).+?(?<!`)(?P=ticks)(?!`)")
COMMENT_DELIMITERS = (("<!--", "-->"), ("{/*", "*/}"))
COMMENT_CLOSERS = dict(COMMENT_DELIMITERS)
# A blanked span or comment leaves one space, so the text either side of it
# does not fuse into a token neither side wrote.
BLANK = " "

# The shrinking allowlist: `<page> <count> <shape>` per line, the page relative
# to the site root, the shape exactly as this check reports it. `#` lines and
# blank lines are ignored. An entry pins the findings a page carried when its
# shape arrived; the count may only go down. Only the shapes that arrived with
# a backlog can be pinned: the citation shape and the `evalcand_` denylist
# line. Every other shape has held at zero since it arrived and has nothing to
# pin, so an entry naming one is refused rather than becoming the first way a
# maintainer identifier passes the gate.
ALLOWLIST = REPO / "scripts" / "docs_audience_allowlist.txt"
ALLOWLIST_FIELDS = 3
PINNABLE_SHAPES = (CITATION_SHAPE, "denylist: evalcand_")

# How a denylist line or a derived project ID is named in the report.
DENYLIST_SHAPE = "denylist: {pattern}"
CI_ENV_SHAPE = "project ID exported by hack/ci-env.sh"

# How an allowlist entry that no longer matches the site is reported.
ALLOWLIST_MALFORMED_ERROR = "{allowlist}:{line}: expected `<page> <count> <shape>` with a count above zero, got {text!r}"
ALLOWLIST_UNPINNABLE_ERROR = "{allowlist}:{line}: shape {shape!r} cannot be pinned; only {pinnable} arrived with a backlog"
ALLOWLIST_DUPLICATE_ERROR = "{allowlist}:{line}: {page} already has an entry of shape {shape!r} at line {first}"
ALLOWLIST_MISSING_PAGE_ERROR = "{allowlist}:{line}: {page} is not a site page; remove the entry"
ALLOWLIST_RISE_ERROR = (
    "{page}: {found} finding(s) of shape {shape!r}, the allowlist entry at {allowlist}:{line} allows {allowed}; "
    "the list only shrinks, so remove enough of the {found} listed below to return to {allowed}"
)
ALLOWLIST_DROP_ERROR = (
    "{page}: {found} finding(s) of shape {shape!r}, the allowlist entry at {allowlist}:{line} allows {allowed}; "
    "lower the entry to {found}"
)
ALLOWLIST_CLEAN_ERROR = (
    "{page}: no finding of shape {shape!r} left, the allowlist entry at {allowlist}:{line} allows {allowed}; "
    "remove the entry"
)

# Why the check refuses to report a clean run.
NO_PAGES_ERROR = "no .md/.mdx page found under {root}; the site root moved and this check scans nothing"
NO_PROJECT_ERROR = "no project ID derived from {ci_env}; its PROJECT_ID export changed shape"


class Finding:
    """One match: where it is and which shape caught it."""

    __slots__ = ("path", "line", "shape", "text")

    def __init__(self, path: Path, line: int, shape: str, text: str) -> None:
        self.path = path
        self.line = line
        self.shape = shape
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.path!s}:{self.line}, {self.shape!r}, {self.text!r})"


class AllowlistEntry:
    """One pinned (page, shape) count and the allowlist line that carries it."""

    __slots__ = ("page", "count", "shape", "line")

    def __init__(self, page: str, count: int, shape: str, line: int) -> None:
        self.page = page
        self.count = count
        self.shape = shape
        self.line = line

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AllowlistEntry({self.page} {self.count} {self.shape!r} @{self.line})"


def list_file_lines(path: Path) -> Iterator[tuple[int, str]]:
    """(line number, stripped line) for every line of a list file that is not blank or a comment."""
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith(LIST_FILE_COMMENT_PREFIX):
            yield number, line


def load_denylist(path: Path = DENYLIST) -> list[re.Pattern[str]]:
    """Compile every non-comment, non-blank line of the denylist."""
    return [re.compile(line) for _, line in list_file_lines(path)]


def ci_env_project_ids(path: Path = CI_ENV) -> list[str]:
    """Literal project IDs that hack/ci-env.sh exports, in file order."""
    ids: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = CI_ENV_PROJECT_EXPORT_RE.match(raw.strip())
        if not match:
            continue
        value = match.group("default") or match.group("literal")
        if value and value not in ids:
            ids.append(value)
    return ids


def service_account_findings(path: Path, text: str) -> list[Finding]:
    """Service-account emails whose project segment is not a placeholder."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in SERVICE_ACCOUNT_RE.finditer(line):
            project = match.group("project")
            if PLACEHOLDER_PROJECT_RE.match(project) or GOOGLE_AGENT_PROJECT_RE.match(project):
                continue
            findings.append(Finding(path, number, SERVICE_ACCOUNT_SHAPE, match.group(0)))
    return findings


def pattern_findings(path: Path, text: str, shapes: Iterable[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    """Every (shape name, pattern) hit in the text, one Finding per occurrence, as the citation shape counts."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for shape, pattern in shapes:
            for match in pattern.finditer(line):
                findings.append(Finding(path, number, shape, match.group(0)))
    return findings


def blank_comments(line: str, open_delimiter: str | None, later_closers: set[str]) -> tuple[str, str | None]:
    """The line with every comment segment blanked, and the delimiter of a comment left open at its end.

    Code spans are blanked before the comment scan on every prose segment,
    including the text after a comment closes, so an opener quoted in one
    never opens a comment. An opener whose closer is neither on this line nor
    among ``later_closers``, the closers that appear on a later line, is text.
    """
    kept: list[str] = []
    pos = 0
    while pos < len(line):
        if open_delimiter is not None:
            close = COMMENT_CLOSERS[open_delimiter]
            end = line.find(close, pos)
            if end == -1:
                return "".join(kept), open_delimiter
            open_delimiter = None
            pos = end + len(close)
            line = line[:pos] + CODE_SPAN_RE.sub(BLANK, line[pos:])
            continue
        starts = [(line.find(opener, pos), opener) for opener, _ in COMMENT_DELIMITERS]
        starts = [(at, opener) for at, opener in starts if at != -1]
        if not starts:
            kept.append(line[pos:])
            break
        at, opener = min(starts)
        after = at + len(opener)
        close = COMMENT_CLOSERS[opener]
        if line.find(close, after) == -1 and close not in later_closers:
            kept.append(line[pos:after])
            pos = after
            continue
        open_delimiter = opener
        kept.append(line[pos:at])
        kept.append(BLANK)
        pos = after
    return "".join(kept), open_delimiter


def unfenced_lines(text: str) -> list[str]:
    """The page's lines with every fenced code block, fences included, replaced by an empty line."""
    lines: list[str] = []
    fence: str | None = None
    for raw in text.splitlines():
        match = FENCE_RE.match(raw)
        if fence is None and match:
            fence = match.group("fence")
            lines.append("")
        elif fence is not None:
            if match and match.group("fence").startswith(fence) and not match.group("info").strip():
                fence = None
            lines.append("")
        else:
            lines.append(raw)
    return lines


def prose_lines(text: str) -> list[str]:
    """The page's lines with fenced code, inline code spans and comments blanked, one entry per source line."""
    unfenced = unfenced_lines(text)
    # The last unfenced line each closer appears on, so an opener is believed
    # only when its closer follows it somewhere in the page.
    last_closer_line = {
        close: max((index for index, line in enumerate(unfenced) if close in line), default=-1)
        for _, close in COMMENT_DELIMITERS
    }
    lines: list[str] = []
    open_delimiter: str | None = None
    for index, raw in enumerate(unfenced):
        later_closers = {close for close, last in last_closer_line.items() if last > index}
        line = raw if open_delimiter is not None else CODE_SPAN_RE.sub(BLANK, raw)
        line, open_delimiter = blank_comments(line, open_delimiter, later_closers)
        lines.append(line)
    return lines


def citation_findings(path: Path, text: str) -> list[Finding]:
    """Every pull-request or issue citation in the page's prose, one Finding per occurrence."""
    findings = []
    for number, line in enumerate(prose_lines(text), start=1):
        for match in CITATION_RE.finditer(line):
            findings.append(Finding(path, number, CITATION_SHAPE, match.group(0)))
    return findings


def load_allowlist(path: Path = ALLOWLIST) -> tuple[list[AllowlistEntry], list[str]]:
    """Every entry of the allowlist, and the lines that are not one."""
    entries: list[AllowlistEntry] = []
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    shown = display_path(path)
    for number, line in list_file_lines(path):
        fields = line.split(None, ALLOWLIST_FIELDS - 1)
        count = int(fields[1]) if len(fields) == ALLOWLIST_FIELDS and fields[1].isdecimal() else 0
        if count < 1:
            errors.append(ALLOWLIST_MALFORMED_ERROR.format(allowlist=shown, line=number, text=line))
            continue
        page, shape = fields[0], fields[2]
        if shape not in PINNABLE_SHAPES:
            pinnable = ", ".join(repr(s) for s in PINNABLE_SHAPES)
            errors.append(ALLOWLIST_UNPINNABLE_ERROR.format(allowlist=shown, line=number, shape=shape, pinnable=pinnable))
            continue
        if (page, shape) in seen:
            first = seen[(page, shape)]
            errors.append(ALLOWLIST_DUPLICATE_ERROR.format(allowlist=shown, line=number, page=page, shape=shape, first=first))
            continue
        seen[(page, shape)] = number
        entries.append(AllowlistEntry(page, count, shape, number))
    return entries, errors


def reconcile(
    findings: Iterable[Finding],
    entries: Iterable[AllowlistEntry],
    root: Path = SITE_ROOT,
    allowlist: Path = ALLOWLIST,
) -> tuple[list[Finding], list[str]]:
    """Findings the allowlist does not cover, and the entries the site no longer matches exactly.

    An entry covers the first ``count`` findings of its shape on its page and no
    more; a page with fewer findings than its entry is reported too, so the list
    only shrinks. A page with no entry is held at zero.
    """
    by_key: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        by_key.setdefault((finding.path.relative_to(root).as_posix(), finding.shape), []).append(finding)
    pages = {page.relative_to(root).as_posix() for page in site_pages(root)}
    shown = display_path(allowlist)
    uncovered: list[Finding] = []
    problems: list[str] = []
    for entry in entries:
        if entry.page not in pages:
            problems.append(ALLOWLIST_MISSING_PAGE_ERROR.format(allowlist=shown, line=entry.line, page=entry.page))
            continue
        matched = by_key.pop((entry.page, entry.shape), [])
        if len(matched) == entry.count:
            continue
        fields = dict(
            page=entry.page, found=len(matched), shape=entry.shape, allowlist=shown, line=entry.line, allowed=entry.count
        )
        if len(matched) > entry.count:
            problems.append(ALLOWLIST_RISE_ERROR.format(**fields))
            uncovered.extend(matched)
        elif matched:
            problems.append(ALLOWLIST_DROP_ERROR.format(**fields))
        else:
            problems.append(ALLOWLIST_CLEAN_ERROR.format(**fields))
    for matched in by_key.values():
        uncovered.extend(matched)
    uncovered.sort(key=lambda f: (str(f.path), f.line, f.shape))
    return uncovered, problems


def site_pages(root: Path = SITE_ROOT) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in SITE_SUFFIXES and p.is_file())


def scan(
    root: Path = SITE_ROOT,
    denylist: Path = DENYLIST,
    ci_env: Path = CI_ENV,
) -> list[Finding]:
    """Every finding across the site, for the four sources described above, before the allowlist."""
    shapes: list[tuple[str, re.Pattern[str]]] = [
        (DENYLIST_SHAPE.format(pattern=p.pattern), p) for p in load_denylist(denylist)
    ]
    shapes.extend(
        (CI_ENV_SHAPE, re.compile(WORD_BOUNDARY + re.escape(project) + WORD_BOUNDARY))
        for project in ci_env_project_ids(ci_env)
    )

    findings: list[Finding] = []
    for page in site_pages(root):
        text = page.read_text(encoding="utf-8")
        findings.extend(pattern_findings(page, text, shapes))
        findings.extend(service_account_findings(page, text))
        if page.relative_to(root) not in CITATION_EXEMPT_PAGES:
            findings.extend(citation_findings(page, text))
    findings.sort(key=lambda f: (str(f.path), f.line, f.shape))
    return findings


def preconditions(root: Path = SITE_ROOT, ci_env: Path = CI_ENV) -> list[str]:
    """Reasons a run cannot be trusted to have checked anything."""
    errors = []
    if not site_pages(root):
        errors.append(NO_PAGES_ERROR.format(root=root))
    if not ci_env_project_ids(ci_env):
        errors.append(NO_PROJECT_ERROR.format(ci_env=ci_env))
    return errors


def display_path(path: Path) -> Path:
    """Repository-relative when the path is inside the repository, otherwise as given."""
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def main(
    root: Path = SITE_ROOT,
    denylist: Path = DENYLIST,
    ci_env: Path = CI_ENV,
    allowlist: Path = ALLOWLIST,
) -> int:
    errors = preconditions(root, ci_env)
    entries, malformed = load_allowlist(allowlist)
    errors.extend(malformed)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    findings, problems = reconcile(scan(root, denylist, ci_env), entries, root, allowlist)
    pages = len(site_pages(root))
    if findings or problems:
        print(
            f"{len(findings)} maintainer identifier(s) or pull-request citation(s) on the published site outside "
            f"or above what {display_path(allowlist)} allows, and {len(problems)} allowlist entr(y/ies) out of step "
            f"with the site (rule: .agents/rules/documentation.md; shapes: {display_path(denylist)}). "
            "Move the page to the home the AGENTS.md canonical-home table names, replace "
            "the value with a placeholder the reader fills in, or state the reason in prose:"
        )
        for problem in problems:
            print(f"  {problem}")
        for finding in findings:
            print(f"  {display_path(finding.path)}:{finding.line}: {finding.shape} -- {finding.text!r}")
        return 1
    print(
        f"No maintainer identifiers or pull-request citations on the {pages} published site pages "
        f"beyond the {len(entries)} allowlist entr(y/ies), each matched exactly."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
