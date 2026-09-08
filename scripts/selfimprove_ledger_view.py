#!/usr/bin/env python3
"""Render the self-improvement ledger out of a running install.

The ledger is a ConfigMap rather than a file -- `kube-agents-selfimprove-ledger`
in the install's namespace, one `ledger.json` key -- so the read path the design
gives a developer is `kubectl get configmap ... | jq`, and what comes back is
twenty kilobytes of nested JSON in which the questions anyone actually opens it
for are several screens apart. This renders the same document as a report: when
the loop last ran and how many runs are behind it, then the run history, then
the findings ranked worst-first with the gate verdict each would get next run,
then every pull request it has opened.

Read-only and cluster-optional. `--file` takes a ledger somebody has already
pulled down, which is also how the tests exercise every renderer without a
cluster.

The gate column is a simulation, not a record: it replays
`selfimprove_ledger.evaluate_gate` over every finding in the ledger, as if the
next run re-found all of them, using the gate the live CronJob is configured
with. That is the honest answer to "what would happen next hour", and it is
deliberately not the same thing as what any past run decided -- a run only ever
gates the findings it saw that hour.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The pure half of the ledger module -- fingerprints, the rolling occurrence
# count, the gate -- imports no Kubernetes client at module scope, which is what
# makes reusing it here possible. Reimplementing `occurrences_in_window` or the
# gate's three conditions would give this tool a second opinion about the same
# ledger, and the two would drift the first time either changed. If the import
# fails (the script copied out of the tree, a branch without the loop) every
# derived column degrades to "?" rather than the whole report failing.
sys.path.insert(0, str(REPO_ROOT / "agents" / "selfimprove" / "scripts"))
try:  # pragma: no cover - the failure branch needs the module absent
    import selfimprove_ledger as ledger_mod
except Exception:  # noqa: BLE001 - any import failure is the same degradation
    ledger_mod = None

DEFAULT_NAMESPACE = "kubeagents-system"
DEFAULT_CONFIGMAP = "kube-agents-selfimprove-ledger"
DEFAULT_CRONJOB = "kube-agents-selfimprove"
LEDGER_KEY = "ledger.json"

#: Mirrors `selfimprove_ledger.LEDGER_MAX_BYTES`, used only on the degraded
#: path above; when the module imports, its value wins and the two cannot
#: disagree.
FALLBACK_MAX_BYTES = 768 * 1024
SEVERITY_ORDER = ("critical", "high", "medium", "low")


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------

# SGR colour codes and OSC 8 hyperlink wrappers, both of which occupy no
# columns. Measuring a hyperlinked cell without stripping the OSC sequence
# counts the URL itself as visible text and every border below it misaligns.
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\")

RESET = "\033[0m"
STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "crit": "\033[1;31m",
    "head": "\033[1;4m",
}

SEVERITY_STYLE = {"critical": "crit", "high": "red", "medium": "yellow", "low": "cyan"}
# Anything not listed is styled as a warning rather than as a success: an
# outcome this tool has never heard of is exactly the one a reader should look
# at, and defaulting it to green would hide it.
OUTCOME_STYLE = {"ok": "green", "killed": "red", "error": "red", "failed": "red"}


class Palette:
    """Applies or discards styles, so no renderer has to know which."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, style: Optional[str]) -> str:
        if not self.enabled or not style or not text:
            return text
        code = STYLES.get(style)
        return "%s%s%s" % (code, text, RESET) if code else text


def want_colour(choice: str, stream=None) -> bool:
    """`--color` plus the two conventions a terminal tool is expected to honour.

    NO_COLOR is checked after the explicit flag, because a flag typed on the
    command line is a stronger statement than a variable inherited from a shell
    profile, and before the TTY test, because its whole point is that a user
    who sets it means it on an interactive terminal too.
    """
    if choice == "always":
        return True
    if choice == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    stream = stream if stream is not None else sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def plain(text: str) -> str:
    """Width-measuring view of a string: what it looks like with colour off.

    This strips the two forms this file emits and nothing else, which is only a
    correct measurement because `scrub_document` has already taken the control
    characters out of everything else. An escape sequence that reached a cell
    from the ledger would measure here as zero columns wide, so the table would
    both misalign and pass the assertions that exist to catch misalignment.
    """
    return _ANSI.sub("", text)


# --------------------------------------------------------------------------
# Untrusted text
# --------------------------------------------------------------------------

#: Everything a terminal acts on rather than draws: the C0 controls except
#: newline and tab, DEL, the C1 range (0x9B is CSI to a terminal that decodes
#: it), the Unicode line and paragraph separators, the bidirectional overrides,
#: and the zero-width no-break space.
_CONTROL = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f"
    r"\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]"
)


def scrub(value: Any) -> Any:
    """Make one string safe to print, without hiding what it said.

    Every field this report draws is text the investigating agent wrote out of
    production logs, and a log line holds whatever reached it. A `summary`
    carrying an OSC 8 introducer renders as an ordinary sentence linked to
    somebody else's site -- the same sequence this file uses for its own links,
    so a terminal has no way to tell them apart -- and a `title` carrying the
    CSI sequence for "erase display" clears the screen the report is being read
    on and leaves the row it was in short of its own width.

    Only the control characters are removed. What surrounded them stays and
    prints as the literal `]8;;https://evil.example/pwn` it is: inert, and
    visible, which deleting the whole sequence would not be -- a reader would
    be looking at doctored text with nothing to say so.

    Non-strings pass through unchanged, so this can be mapped over a decoded
    JSON document without turning its numbers into strings.
    """
    if not isinstance(value, str):
        return value
    return _CONTROL.sub("", value.replace("\t", " "))


def scrub_document(value: Any) -> Any:
    """`scrub` over every string in a decoded JSON document, keys included.

    At the boundary rather than at each call site: a ledger field reaches the
    terminal from a dozen places -- table cells, `--detail` blocks, the header,
    a hyperlink's label -- and one of them forgotten is the whole property lost.
    Keys are scrubbed alongside values because the findings map is keyed by
    fingerprint and the gate's verdicts are looked up by the `fingerprint`
    field, so scrubbing one and not the other would quietly stop them matching.
    """
    if isinstance(value, dict):
        return {scrub_document(key): scrub_document(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_document(item) for item in value]
    return scrub(value)


_PR_URL = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$")


def pr_ref(url: str) -> str:
    """`owner/repo#123` for a GitHub pull-request URL, else the URL unchanged.

    A 50-character URL in a table column wraps, and a wrapped URL is no longer
    one a terminal will make clickable or a reader will copy in one go. The
    short form is half the width, and `hyperlink` restores the full link on any
    terminal that supports OSC 8.
    """
    match = _PR_URL.match((url or "").strip())
    return "%s/%s#%s" % match.groups() if match else url


#: The only schemes this will make clickable. A promotion's `url` is a ledger
#: field like every other, and OSC 8 does not care what it wraps -- a terminal
#: handed `file:///` or a scheme the desktop has registered may pass it to the
#: operating system on a click. An unlinkable URL still prints as its own text,
#: so nothing is hidden by declining to link it.
_LINKABLE_URL = re.compile(r"https?://", re.IGNORECASE)


def hyperlink(text: str, url: str, palette: Palette, link_id: str = "") -> str:
    """OSC 8, gated on the same signal as colour.

    Terminals that do not implement it ignore the sequence, but a pipe or a
    file keeps the bytes, so this follows `--color`: that flag already means
    "a human is looking at this in a terminal".

    `link_id` is OSC 8's `id=` parameter, which exists to say that two
    separately-emitted runs are one hyperlink. Anything that wraps across table
    rows needs it: without it a terminal treats each row as its own link and
    highlights only the line under the pointer, and with it the whole location
    lights up as one.
    """
    if not palette.enabled or not url or not _LINKABLE_URL.match(url):
        return text
    return "\x1b]8;%s;%s\x1b\\%s\x1b]8;;\x1b\\" % ("id=%s" % link_id if link_id else "", url, text)


#: A pull-request reference in text, as the filing turn's refusal vocabulary
#: writes one: `already filed as #161`, `SKIPPED: fixed in #874`. The
#: `owner/repo#123` form is accepted alongside the bare one because a reference
#: to another repository has no other way to say so. Neither may be preceded by
#: a word character, which leaves a `foo#2` fragment alone, and both are bounded
#: on the right so `#874x` is not read as 874.
_ISSUE_REF = re.compile(r"(?<![\w#/-])(?:(?P<repo>[\w.-]+/[\w.-]+)#|#)(?P<number>\d+)\b")


def issue_url(repo: str, number: str) -> str:
    """GitHub's URL for a `#123` reference, or "" if it cannot be built safely.

    `/pull/` rather than `/issues/`, and that one form is right for both: GitHub
    answers `/pull/123` with a 302 to `/issues/123` when 123 turns out to be an
    issue. The refusal vocabulary only ever names pull requests, but it names
    them out of a repository that has issues too.

    Unlike `blob_url` there is nothing agent-written in the path unless the
    reference was qualified, and `_url_safe` is what makes that case safe: a
    `../..#1` would otherwise walk the link into another repository while the
    label beside it went on naming this one.
    """
    if not repo or not _url_safe(repo, segments=2) or not number.isdigit():
        return ""
    return "https://github.com/%s/pull/%s" % (repo, number)


class Refs:
    """What a `#123` in a gate verdict or a refusal resolves to, if anything.

    A bare `#123` does not say which repository it belongs to, and on this loop
    there can be two answers. The vocabulary the filing skill dictates --
    `already filed as #161` -- means a pull request the loop itself opened,
    against the base repository. But a refusal reason is prose, and the other
    thing it reaches for is a number it read in the checkout: git squash-merges
    append `(#874)` to the subject line, and a warning in the source can cite an
    issue by number. Those numbers were assigned wherever the project's pull
    requests are numbered, which for a fork is not the fork.

    So the base repository answers only for the numbers the ledger can vouch
    for -- it recorded opening that number there -- and everything else goes to
    `parent`, the root of the base repository's fork network. On an install that
    files against the project itself the two are the same repository and the
    distinction costs nothing; on one whose base is a fork it is the difference
    between `#874` reaching the pull request that carries the fix and reaching a
    404. A qualified `owner/repo#123` skips both, because it has said which
    repository it means.
    """

    def __init__(
        self,
        repo: str = "",
        filed: Iterable[Tuple[str, str]] = (),
        parent: str = "",
    ):
        self.repo = repo or ""
        self.filed = frozenset(filed)
        self.parent = parent or ""

    def url(self, match) -> str:
        """The URL for one `_ISSUE_REF` match, or "" to leave it as text."""
        number = match.group("number")
        qualified = match.group("repo")
        if qualified:
            return issue_url(qualified, number)
        if self.repo and (self.repo, number) in self.filed:
            return issue_url(self.repo, number)
        return issue_url(self.parent or self.repo, number)

    def first(self, text: str) -> str:
        """The URL for the first linkable reference in `text`, else ""."""
        for match in _ISSUE_REF.finditer(text or ""):
            url = self.url(match)
            if url:
                return url
        return ""

    def linkify(self, text: str, palette: Palette) -> str:
        """Every linkable reference in `text`, made clickable where it stands.

        For output that is not in a table. `render_table` measures the width of
        every cell it lays out, so an escape sequence inserted mid-string would
        be counted as visible characters and throw the column widths out; there
        a paragraph is linked as a whole instead, and only its first reference
        is reachable. `--detail` prints plain lines and is wrapped before this
        runs, so each reference on them can carry its own link.
        """

        def one(match):
            url = self.url(match)
            return hyperlink(match.group(0), url, palette) if url else match.group(0)

        return _ISSUE_REF.sub(one, text or "")


#: The resolver for a view that has no ledger or no CronJob to build one from.
#: Links nothing, which is what `--file` on a bare ledger should do.
NO_REFS = Refs()


#: A URL anywhere in a string, stripped before locations are parsed so that the
#: `github.com` in one is not mistaken for a file called `com`.
_URL_ANYWHERE = re.compile(r"https?://\S+")

#: Either a dotted path with an optional `:line` or `:line-line`, or a bare
#: `:line` continuing the path before it. The extension must start with a
#: letter: that is what keeps `v2026.8.13` and `1.5s` out without a list of
#: known suffixes.
_LOCATION_REF = re.compile(
    r"(?P<path>(?:[\w.+-]+/)*[\w.+-]+\.[A-Za-z]\w*)(?::(?P<line>\d+(?:-\d+)?))?"
    r"|(?<![\w.]):(?P<bare>\d+(?:-\d+)?)"
)


def repo_toplevel(root: Optional[str] = None) -> frozenset:
    """Top-level entries of the kube-agents checkout this script ships in.

    This set is what tells a repo-relative path from everything else a location
    string contains, and it has to be derived rather than listed. The live
    ledger holds a finding in `agent/anthropic_adapter.py`, which is the Hermes
    harness and not this repository at all; linking it to a kube-agents blob URL
    would send the reader to a 404 that looks like the finding is stale rather
    than like the link is wrong. Deriving the set also means a new top-level
    directory needs no edit here.

    Empty when the directory is not a kube-agents checkout, which switches every
    file link off rather than guessing.
    """
    base = REPO_ROOT if root is None else pathlib.Path(root)
    if not (base / "AGENTS.md").is_file():
        return frozenset()
    try:
        return frozenset(entry.name for entry in base.iterdir() if entry.name != ".git")
    except OSError:
        return frozenset()


def location_refs(location: str, roots: frozenset) -> List[Tuple[str, Optional[str]]]:
    """The `path:line` references a location string names, in order, deduped.

    A location is whatever the investigating agent wrote. Most are a bare
    `path:line`, but the ones that are not run to prose -- a parenthetical after
    the path, then a second reference given as a bare `:1162` -- so a bare line
    number attaches to the path before it.

    A candidate whose first segment is not a top-level entry of this repository
    is dropped. That single rule does two jobs: it rejects the things that only
    look like paths (`e.g.` parses as a file named `e` with extension `g`) and
    it rejects real paths in other repositories.
    """
    if not roots:
        return []
    refs: List[Tuple[str, Optional[str]]] = []
    current: Optional[str] = None
    for match in _LOCATION_REF.finditer(_URL_ANYWHERE.sub(" ", location or "")):
        path, line, bare = match.group("path"), match.group("line"), match.group("bare")
        if path:
            if path.split("/")[0] not in roots:
                if "/" in path:
                    # A real path, in another repository. Forget the running
                    # one, so a trailing `:120` is not attached to whatever
                    # repo-relative path came before *that*.
                    current = None
                # Without a slash it is far more likely to be code than a path
                # -- `r.Status()` in a backticked snippet parses as one -- and
                # letting that clear the running path costs the `:1162` that
                # follows it a link it should have had.
                continue
            current = path
            refs.append((path, line))
        elif bare and current:
            refs.append((current, bare))
    seen = set()
    return [ref for ref in refs if not (ref in seen or seen.add(ref))]


#: One segment of a path or a repository name, in the character set
#: `location_refs` yields. `.` and `..` match it and are rejected separately,
#: because those two are the segments a browser resolves rather than requests.
_URL_SEGMENT = re.compile(r"^[\w.+-]+$")

#: `10` or `10-20`, which is all a line anchor is ever built from.
_LINE_ANCHOR = re.compile(r"^\d+(?:-\d+)?$")


def _url_safe(value: str, segments: int = 0) -> bool:
    """True when `value` is `/`-joined segments a URL can carry unchanged."""
    parts = value.split("/")
    if segments and len(parts) != segments:
        return False
    return all(
        part not in (".", "..") and _URL_SEGMENT.match(part) is not None for part in parts
    )


#: Extensions GitHub renders as a document rather than showing as source. The
#: rendered view has no line-number gutter, so a `#L12` anchor finds nothing to
#: scroll to and drops the reader at the top of the file -- which for a long
#: design document is no better than no link at all. `?plain=1` asks for the
#: source view, which does have the gutter, and the anchor works there.
#:
#: Only the markup formats need it. A `.py` or `.go` blob is already source, and
#: adding the parameter to those would be noise in a URL a human reads.
_RENDERED_EXTENSIONS = frozenset(
    (
        ".adoc",
        ".asc",
        ".asciidoc",
        ".creole",
        ".csv",
        ".ipynb",
        ".markdown",
        ".md",
        ".mdown",
        ".mediawiki",
        ".mkd",
        ".mkdn",
        ".org",
        ".pod",
        ".rdoc",
        ".rst",
        ".textile",
        ".tsv",
        ".wiki",
    )
)


def blob_url(repo: str, revision: str, path: str, line: Optional[str] = None) -> str:
    """GitHub's permalink for `path` at `revision`, anchored on `line`.

    Pinned to the revision the finding was made against rather than to a branch:
    the line number is only meaningful against the code the agent read, and a
    branch link drifts out from under it on the next commit.

    A file GitHub renders as a document gets `?plain=1` alongside the anchor, so
    that the line number has a gutter to land on; see `_RENDERED_EXTENSIONS`.

    None of the three pieces is trusted to be what it is called. The path is cut
    out of a location string the investigating agent wrote and the revision is a
    ledger field beside it, and a `..` segment in either walks the link out of
    the repository while the label next to it goes on naming a file inside it: a
    browser resolves
    `github.com/o/r/blob/../../../../attacker/repo/blob/main/x.py` to
    `github.com/attacker/repo/blob/main/x.py`, and what the reader sees is
    `x.py:12` and a link they have every reason to trust. So each piece has to
    be plain path segments, and anything else is given no link at all -- the
    same degradation as a finding with no revision.
    """
    if not repo or not revision or not path:
        return ""
    if not _url_safe(repo, segments=2) or not _url_safe(revision, segments=1):
        return ""
    if not _url_safe(path):
        return ""
    url = "https://github.com/%s/blob/%s/%s" % (repo, revision, path)
    if not line:
        return url
    if not _LINE_ANCHOR.match(str(line)):
        return url
    # Only once there is an anchor to honour: without a line the rendered
    # preview is the better page to land on, and `?plain=1` would take it away.
    if os.path.splitext(path)[1].lower() in _RENDERED_EXTENSIONS:
        url += "?plain=1"
    # GitHub spells a range `#L10-L20`, with the `L` repeated; a location writes
    # it `10-20`.
    return url + "#L%s" % line.replace("-", "-L")


def location_links(
    entry: Dict[str, Any], repo: str, roots: frozenset
) -> List[Tuple[str, str]]:
    """`(label, url)` for every file reference in a finding's location."""
    revision = str(entry.get("revision") or "")
    links = []
    for path, line in location_refs(str(entry.get("location") or ""), roots):
        url = blob_url(repo, revision, path, line)
        if url:
            links.append(("%s:%s" % (path, line) if line else path, url))
    return links


def target_repo(env: Dict[str, str]) -> str:
    """The `owner/name` a finding's revision can be resolved against.

    `SELFIMPROVE_SOURCE_REPO` when the CronJob sets it, because answering this
    exact question is that variable's whole job: it names the repository the
    runner fetched its own source from, so it is the one repository the stamped
    revision is known to exist in. The fork is a push target, which is a
    different question -- a link is not resolved by the ability to push to it.

    The older pair is the fallback, for an install whose CronJob predates the
    variable. It reaches the right answer often enough to have hidden this:
    every repository in a GitHub fork network serves every commit in that
    network, so a fork of the source resolves a blob URL at a commit it has
    never held. A push target that is not a fork of the source -- a mirror, or
    a fork of something else -- does not, and links every file to a 404.
    """
    source = env.get("SELFIMPROVE_SOURCE_REPO")
    if source:
        return source
    if env.get("SELFIMPROVE_MODE") == "report-only":
        return env.get("SELFIMPROVE_UPSTREAM_REPO", "") or ""
    return env.get("SELFIMPROVE_FORK_REPO") or env.get("SELFIMPROVE_UPSTREAM_REPO") or ""


def pull_request_repo(env: Dict[str, str]) -> str:
    """The `owner/name` a `#123` in a gate verdict or a refusal belongs to.

    `SELFIMPROVE_UPSTREAM_REPO`, which is the pull request's base in every mode
    -- the upstream under `upstream` and `report-only`, the fork under `fork`.

    A different question from `target_repo`, which answers where a finding's
    *revision* resolves and so follows the source repository. The two hold the
    same value on an install that files against the repository it reads itself
    from, which is why they are easy to conflate; telling them apart matters on
    one that does not, where sending `#161` to the source repository points it
    at whatever pull request happens to carry that number there.
    """
    return env.get("SELFIMPROVE_UPSTREAM_REPO") or ""


def filed_pull_requests(ledger: Dict[str, Any]) -> frozenset:
    """`(repo, number)` for every pull request this ledger records opening.

    The set `Refs` checks a bare `#123` against before assuming it came out of
    the project's history. A promotion's `url` is the only record of a number
    the loop assigned itself rather than read somewhere, so a pruned or
    truncated ledger shrinks this set and sends those numbers to the fork
    parent -- wrong on an install whose base is a fork, and invisible on one
    where base and parent are the same repository.
    """
    filed = set()
    findings = ledger.get("findings")
    for entry in (findings.values() if isinstance(findings, dict) else findings or []):
        if not isinstance(entry, dict):
            continue
        for promotion in entry.get("promotions") or []:
            if not isinstance(promotion, dict):
                continue
            match = _PR_URL.match(str(promotion.get("url") or "").strip())
            if match:
                owner, name, number = match.groups()
                filed.add(("%s/%s" % (owner, name), number))
    return frozenset(filed)


def fork_parent(repo: str, timeout: int = 10) -> str:
    """The root of `repo`'s fork network, or "" when it is not a fork.

    Which repository numbers the pull requests a finding's prose cites. The
    runner's environment cannot answer this -- every variable it carries names
    a repository the loop reads or writes, and the project a fork descends from
    is neither -- so it is one `gh` call, made once per invocation and only
    when there is a base repository to ask about.

    Every failure is "": `gh` absent, unauthenticated, offline, a repository
    that has been deleted. The reference still links -- `Refs.url` falls back
    to the base repository, which is where bare references belonged before any
    of this existed -- so nothing that worked stops working when the call does
    not land. A repository that is not a fork takes the same path, and there
    the base is not a degradation but the right answer.
    """
    if not repo or not _url_safe(repo, segments=2):
        return ""
    try:
        proc = subprocess.run(
            ["gh", "api", "repos/%s" % repo, "--jq", ".source.full_name // empty"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    parent = proc.stdout.strip()
    return parent if parent and parent != repo and _url_safe(parent, segments=2) else ""


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

BOX_UNICODE = {
    "h": "─", "v": "│",
    "tl": "┌", "tm": "┬", "tr": "┐",
    "ml": "├", "mm": "┼", "mr": "┤",
    "bl": "└", "bm": "┴", "br": "┘",
}
BOX_ASCII = {
    "h": "-", "v": "|",
    "tl": "+", "tm": "+", "tr": "+",
    "ml": "+", "mm": "+", "mr": "+",
    "bl": "+", "bm": "+", "br": "+",
}


class Column:
    """One column.

    `wrap` marks a column that gives up width first, down to `min_width`.
    `expendable` is the next concession after that: a positive value means the
    column may be dropped entirely on a terminal too narrow to hold the table
    even at its minimums, highest value first. Zero -- the default -- means the
    column is load-bearing and the table runs wide instead.
    """

    def __init__(
        self,
        title: str,
        align: str = "l",
        wrap: bool = False,
        min_width: int = 12,
        expendable: int = 0,
    ) -> None:
        self.title = title
        self.align = align
        self.wrap = wrap
        self.min_width = min_width
        self.expendable = expendable


def _pad(text: str, width: int, align: str) -> str:
    gap = max(0, width - len(plain(text)))
    if align == "r":
        return " " * gap + text
    if align == "c":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def _cell_lines(text: str, width: int) -> List[Tuple[str, int]]:
    """Wrap one cell to `width`, as `(line, source paragraph index)` pairs.

    Deliberate newlines are preserved, and the paragraph index rides along so a
    cell that stacks several facts -- a finding's title, its location, its gate
    verdict -- can colour each one differently even after wrapping has turned
    them into an indeterminate number of lines.

    `break_long_words` is on because the cells most likely to overflow are file
    paths and fingerprints, which have no spaces to break at -- left unbroken
    they push the column past its allotment and every border below misaligns.
    """
    out: List[Tuple[str, int]] = []
    for index, para in enumerate((text or "").split("\n")):
        if not para:
            out.append(("", index))
            continue
        for line in (
            textwrap.wrap(para, width=max(1, width), break_long_words=True, break_on_hyphens=False)
            or [""]
        ):
            out.append((line, index))
    return out or [("", 0)]


def _natural_widths(columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]]) -> List[int]:
    """The width each column would take if nothing had to give."""
    natural = []
    for index, column in enumerate(columns):
        widest = len(plain(column.title))
        for row in rows:
            text = row[index][0] if index < len(row) else ""
            for line in str(text).split("\n"):
                widest = max(widest, len(plain(line)))
        natural.append(widest)
    return natural


def _overhead(count: int) -> int:
    """Borders and padding: `| ` before each cell and ` |` after the last."""
    return 3 * count + 1


def _minimum_width(columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]]) -> int:
    """The narrowest this table can be drawn without dropping a column."""
    natural = _natural_widths(columns, rows)
    return _overhead(len(columns)) + sum(
        column.min_width if column.wrap else natural[index]
        for index, column in enumerate(columns)
    )


def _fit_columns(
    columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]], total: int
) -> Tuple[List[Column], List[List[Sequence[Any]]], List[str]]:
    """Drop expendable columns until the table fits, worst-value first.

    An eighty-column terminal cannot hold the findings table: nine columns of
    borders alone are twenty-eight characters, and the columns that carry the
    finding itself want another eighty. Left to run wide the terminal hard-wraps
    every row and the result is less readable than the JSON this replaces. So
    the least load-bearing columns come out first, and the caller is told which
    -- a table that silently drops a column is a table that lies about what the
    ledger holds.
    """
    kept = list(columns)
    trimmed = [list(row) for row in rows]
    dropped: List[str] = []
    while _minimum_width(kept, trimmed) > total:
        candidates = [i for i, column in enumerate(kept) if column.expendable > 0]
        if not candidates:
            break
        victim = max(candidates, key=lambda i: (kept[i].expendable, i))
        dropped.append(kept[victim].title)
        kept.pop(victim)
        for row in trimmed:
            if victim < len(row):
                row.pop(victim)
    return kept, trimmed, dropped


def _resolve_widths(columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]], total: int) -> List[int]:
    natural = _natural_widths(columns, rows)

    overhead = _overhead(len(columns))
    available = max(total - overhead, 10)
    if sum(natural) <= available:
        return natural

    flex = [i for i, c in enumerate(columns) if c.wrap]
    if not flex:
        return natural

    fixed = sum(w for i, w in enumerate(natural) if i not in flex)
    room = available - fixed
    floor = sum(columns[i].min_width for i in flex)
    if room < floor:
        # Nothing left to give. Honour the minimums and let the table run wide:
        # a table one column too wide is legible, a table with three-character
        # title cells is not.
        return [columns[i].min_width if i in flex else natural[i] for i in range(len(columns))]

    share = float(sum(natural[i] for i in flex)) or 1.0
    widths = list(natural)
    for i in flex:
        widths[i] = max(columns[i].min_width, int(room * (natural[i] / share)))
    # Integer division loses a column or two of the budget; hand the remainder
    # to the widest flexible column rather than leaving the table short.
    drift = room - sum(widths[i] for i in flex)
    if drift > 0:
        widths[max(flex, key=lambda i: widths[i])] += drift
    return widths


def row_separator(row_style: str) -> str:
    """`render_table`'s `separator` for a `--rows` choice."""
    return {"spaced": "blank", "ruled": "rule"}.get(row_style, "none")


def render_table(
    columns: Sequence[Column],
    rows: Sequence[Sequence[Sequence[Any]]],
    palette: Palette,
    width: int,
    box: Dict[str, str],
    separator: str = "none",
) -> List[str]:
    """Render `rows` into a bordered table.

    A cell is a tuple of up to five parts: the text, a style for all of it, a
    URL to hyperlink it with, a `{paragraph index: style}` override for a cell
    whose newline-separated parts want colouring individually, and a
    `{paragraph index: URL}` for one that wants them linked individually.

    The two URL forms differ in what has to be unwrapped for the link to be
    drawn -- the whole cell for the plain one, only the paragraph itself for the
    per-paragraph one. A cell stacking a title over a location has no single-line
    form to reach, so a whole-cell URL on it would never render at all.

    `separator` puts a `blank` line or a `rule` between rows, for a table whose
    rows are several lines tall: the row number is on the first of them and
    every other line of the cell is blank in the narrow columns, so without one
    there is nothing to say where one record stops and the next starts. A table
    of one-line rows wants `none`, which is the default.
    """
    columns, rows, dropped = _fit_columns(columns, rows, width)
    widths = _resolve_widths(columns, rows, width)

    def rule(left: str, mid: str, right: str) -> str:
        return palette(left + mid.join(box["h"] * (w + 2) for w in widths) + right, "dim")

    vertical = palette(box["v"], "dim")

    # Distinguishes one wrapped link from another, so that two locations in the
    # same table are never fused into one hyperlink by a shared `id=`.
    link_seq = [0]

    def emit(cells: Sequence[Sequence[Any]]) -> List[str]:
        wrapped = [
            _cell_lines(str(cells[i][0]) if i < len(cells) else "", widths[i])
            for i in range(len(columns))
        ]
        height = max(len(w) for w in wrapped)
        # How many lines each paragraph of each cell ended up occupying, which
        # is what decides whether a per-paragraph link needs an `id=` to hold
        # its pieces together.
        spans = [collections.Counter(para for _, para in w) for w in wrapped]
        link_seq[0] += 1
        row_seq = link_seq[0]
        lines = []
        for line_no in range(height):
            pieces = []
            for i, column in enumerate(columns):
                raw, para = wrapped[i][line_no] if line_no < len(wrapped[i]) else ("", -1)
                cell = cells[i] if i < len(cells) else ("",)
                style = cell[1] if len(cell) > 1 else None
                url = cell[2] if len(cell) > 2 else None
                per_line = cell[3] if len(cell) > 3 else None
                per_line_url = cell[4] if len(cell) > 4 else None
                if per_line and para in per_line:
                    style = per_line[para]
                # A whole-cell URL is drawn only on an unwrapped cell, because
                # it has no way to say which of several paragraphs it belongs
                # to. A blank line is never linked either -- the padding
                # beneath a short cell, which a taller neighbouring column
                # produces on nearly every row, would otherwise carry a
                # zero-width link with nothing for a reader to click.
                linkable = bool(url) and len(wrapped[i]) == 1
                link_id = ""
                if per_line_url and para in per_line_url:
                    url = per_line_url[para]
                    # A per-paragraph link is drawn even when its paragraph
                    # wraps, joined across the rows by `id=`. Dropping it
                    # instead cost the location column every link it had at any
                    # normal terminal width: a path with a line number needs
                    # around 120 columns of FINDING to fit on one line, so an
                    # 80-column terminal rendered no file links at all.
                    linkable = True
                    if spans[i][para] > 1:
                        link_id = "%d.%d.%d" % (row_seq, i, para)
                rendered = palette(raw, style)
                if url and raw.strip() and linkable:
                    rendered = hyperlink(rendered, url, palette, link_id)
                pieces.append(_pad(rendered, widths[i], column.align))
            lines.append(vertical + " " + (" " + vertical + " ").join(pieces) + " " + vertical)
        return lines

    # Built from the resolved widths rather than by blanking a rule, so that a
    # `--color` run's dim escapes around the borders survive into it.
    spacer = vertical + " " + (" " + vertical + " ").join(" " * w for w in widths) + " " + vertical

    out = [rule(box["tl"], box["tm"], box["tr"])]
    out.extend(emit([(c.title, "head") for c in columns]))
    out.append(rule(box["ml"], box["mm"], box["mr"]))
    for index, row in enumerate(rows):
        if index and separator == "rule":
            out.append(rule(box["ml"], box["mm"], box["mr"]))
        elif index and separator == "blank":
            out.append(spacer)
        out.extend(emit(row))
    out.append(rule(box["bl"], box["bm"], box["br"]))
    if dropped:
        out.append(
            palette(
                "  %s dropped to fit %d columns; --width for a wider table"
                % (", ".join(dropped), width),
                "dim",
            )
        )
    return out


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


#: Where an unparseable or missing timestamp sorts: before every real one, so a
#: "newest first" list puts the rows nobody can date at the bottom.
UNDATED = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)


def parse_iso(text: Any) -> Optional[_dt.datetime]:
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        when = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)


def humanise_delta(seconds: float) -> str:
    seconds = abs(int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return "%dh%02dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(seconds // 3600, 24)
    return "%dd%dh" % (days, hours) if hours else "%dd" % days


def ago(when: Optional[_dt.datetime], now: _dt.datetime) -> str:
    if when is None:
        return "never"
    delta = (now - when).total_seconds()
    return "in %s" % humanise_delta(delta) if delta < 0 else "%s ago" % humanise_delta(delta)


def stamp(when: Optional[_dt.datetime], utc: bool) -> str:
    """A wall-clock time a reader can compare against their own logs.

    Local by default with the zone spelled out, because the question this
    answers is almost always "did that happen while I was looking at it".

    A stamp at either end of the calendar cannot be moved into another zone --
    `0001-01-01T00:00:00Z` read anywhere west of UTC is before `datetime.min`,
    and `astimezone` raises `OverflowError` rather than clamping. `parse_iso`
    survives that input deliberately, because refusing to parse it would lose
    the row it belongs to, so the conversion is where the guard belongs. Such a
    stamp is printed as stored rather than dropped: it is almost certainly the
    zero value of something that failed to write a real one, which is worth
    seeing.
    """
    if when is None:
        return "-"
    try:
        local = when.astimezone(_dt.timezone.utc if utc else None)
    except (OverflowError, OSError, ValueError):
        return when.isoformat(sep=" ", timespec="minutes")
    if utc:
        return local.strftime("%Y-%m-%d %H:%M UTC")
    # %-I is a glibc/BSD extension, which covers Linux and macOS; the zero-pad
    # fallback keeps this from raising anywhere else.
    try:
        rendered = local.strftime("%Y-%m-%d %-I:%M %p %Z")
    except ValueError:  # pragma: no cover - platform-dependent
        rendered = local.strftime("%Y-%m-%d %I:%M %p %Z")
    return rendered.replace("AM", "am").replace("PM", "pm")


def compact_count(value: int) -> str:
    if value < 1000:
        return str(value)
    if value < 1000000:
        return "%.1fk" % (value / 1000.0)
    return "%.1fM" % (value / 1000000.0)


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def meter(fraction: float, cells: int = 18) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    filled = int(round(fraction * cells))
    return "█" * filled + "░" * (cells - filled)


def short_rev(revision: Any) -> str:
    text = str(revision or "").strip()
    return text[:7] if text else "-"


# --------------------------------------------------------------------------
# Cluster reads
# --------------------------------------------------------------------------


class LoadError(RuntimeError):
    pass


def kubectl_json(args: Sequence[str], context: Optional[str], timeout: int = 30) -> Dict[str, Any]:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += list(args) + ["-o", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise LoadError("kubectl is not on PATH; pass --file to read a ledger you already have") from exc
    except subprocess.TimeoutExpired as exc:
        raise LoadError("kubectl timed out after %ds: %s" % (timeout, " ".join(cmd))) from exc
    if proc.returncode != 0:
        raise LoadError((proc.stderr or proc.stdout or "kubectl failed").strip())
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise LoadError("kubectl returned output that is not JSON: %s" % proc.stdout[:200]) from exc


def current_context(context: Optional[str]) -> str:
    if context:
        return context
    try:
        proc = subprocess.run(
            ["kubectl", "config", "current-context"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return "-"
    return proc.stdout.strip() if proc.returncode == 0 else "-"


def load_from_cluster(namespace: str, name: str, context: Optional[str]) -> Tuple[Dict[str, Any], str]:
    cm = kubectl_json(["-n", namespace, "get", "configmap", name], context)
    raw = (cm.get("data") or {}).get(LEDGER_KEY)
    if raw is None:
        raise LoadError(
            "ConfigMap %s/%s has no %r key. The chart renders it empty and the first run fills it in, "
            "so this is a loop that has not completed a run yet." % (namespace, name, LEDGER_KEY)
        )
    return json.loads(raw), raw


def load_from_file(path: str) -> Tuple[Dict[str, Any], str]:
    raw = sys.stdin.read() if path == "-" else pathlib.Path(path).read_text(encoding="utf-8")
    document = json.loads(raw)
    # Accepts either the ledger itself or the whole ConfigMap, because both are
    # things a person ends up with in a file: `kubectl get cm -o json > x.json`
    # is the shorter command and the likelier one.
    if isinstance(document, dict) and isinstance(document.get("data"), dict):
        inner = document["data"].get(LEDGER_KEY)
        if isinstance(inner, str):
            return json.loads(inner), inner
    return document, raw


def load_cronjob(namespace: str, name: str, context: Optional[str]) -> Optional[Dict[str, Any]]:
    """The CronJob carries the gate and the mode, and it may not exist.

    None is a normal answer -- the loop is off, or `--file` was used -- and
    every consumer treats it as "say nothing about the gate" rather than as an
    error.
    """
    try:
        return kubectl_json(["-n", namespace, "get", "cronjob", name], context)
    except LoadError:
        return None


def cronjob_env(cronjob: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not cronjob:
        return {}
    try:
        containers = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError):
        return {}
    env: Dict[str, str] = {}
    for container in containers or []:
        for item in container.get("env") or []:
            if isinstance(item, dict) and "name" in item and "value" in item:
                env.setdefault(str(item["name"]), str(item["value"]))
    return env


# --------------------------------------------------------------------------
# Derived views of the ledger
# --------------------------------------------------------------------------


def records(value: Any) -> List[Dict[str, Any]]:
    """The object members of a field that ought to be a list of them.

    A ledger is JSON that something else wrote: an older run, a `kubectl edit`,
    a hand-assembled file passed to `--file`. `sightings: 4` and
    `promotions: "none"` are not reasons for a read-only report to end in a
    traceback, and iterating a field that is not a list is how it did.
    """
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def sorted_findings(ledger: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings = ledger.get("findings")
    if isinstance(findings, dict):
        return [entry for entry in findings.values() if isinstance(entry, dict)]
    return records(findings)


def severity_rank(entry: Dict[str, Any]) -> int:
    severity = str(entry.get("severity", "")).lower()
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def severity_floor_rank(entry: Dict[str, Any]) -> int:
    """`severity_rank` for the `--severity` floor, where unknown sits with `low`.

    Ranking an unrecognised severity past `low` is right for the sort -- a
    severity this tool cannot interpret belongs at the end of the table -- and
    wrong for the filter, which keeps everything at or above the floor. The help
    text calls `--severity` a floor and `low` is the bottom of the scale, so
    `--severity low` has to hide nothing. It was hiding precisely the findings
    nobody can triage at a glance, and saying nothing about having done it.
    """
    return min(severity_rank(entry), len(SEVERITY_ORDER) - 1)


def occurrences(entry: Dict[str, Any], now: _dt.datetime) -> Optional[int]:
    if ledger_mod is None:
        return None
    try:
        return ledger_mod.occurrences_in_window(entry, now)
    except (AttributeError, TypeError, ValueError):
        # The loop's own function, handed a ledger the loop did not write: it
        # iterates `sightings`, and `sightings: 4` is not iterable. A count this
        # cannot establish renders as "?", which is what a missing module
        # already renders as.
        return None


def reported(entry: Dict[str, Any], now: _dt.datetime) -> Optional[int]:
    if ledger_mod is None:
        return None
    try:
        return ledger_mod.reported_occurrences_in_window(entry, now)
    except (AttributeError, TypeError, ValueError):
        return None


def promotions_today(ledger: Dict[str, Any], now: _dt.datetime) -> Optional[int]:
    """`ledger_mod.promotions_today`, or None when it cannot be asked.

    It indexes `ledger["findings"]` as a mapping and reads every entry as one,
    while `sorted_findings` deliberately accepts the list form too -- so the
    header can be handed a ledger the rest of the report renders happily and
    this number cannot be had from it. None means "?", not zero: a budget line
    reading "0 of 3" on a ledger nobody could count would be a wrong answer
    rather than a missing one.
    """
    if ledger_mod is None or not isinstance(ledger.get("findings"), dict):
        return None
    try:
        return ledger_mod.promotions_today(ledger, now)
    except (AttributeError, TypeError, ValueError):
        return None


def gate_verdicts(ledger: Dict[str, Any], gate: Dict[str, Any], now: _dt.datetime) -> Dict[str, str]:
    """`evaluate_gate` replayed over the whole ledger. See the module docstring."""
    if ledger_mod is None or not gate:
        return {}
    findings = ledger.get("findings")
    if not isinstance(findings, dict):
        return {}
    # Only the entries that are objects, and filtered before the sort rather
    # than inside the `try` below it: `severity_rank` reads an entry as a
    # mapping, so one scalar value in the findings map raised `AttributeError`
    # out here where nothing caught it. Passing the filtered map on also means a
    # malformed entry costs its own verdict instead of everybody's, which is
    # what the blanket `except` made of it once the sort was survivable.
    usable = {fp: entry for fp, entry in findings.items() if isinstance(entry, dict)}
    order = sorted(usable.items(), key=lambda kv: (severity_rank(kv[1]), str(kv[0])))
    try:
        _, reasons = ledger_mod.evaluate_gate(
            {"findings": usable, "runs": ledger.get("runs", [])},
            gate,
            [fp for fp, _ in order],
            now,
        )
    except Exception:  # noqa: BLE001 - a viewer must not fail on a malformed gate
        return {}
    return reasons


def select_findings(
    ledger: Dict[str, Any],
    now: _dt.datetime,
    sort: str = "severity",
    min_severity: Optional[str] = None,
    signal: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """The findings the table lists, filtered and ordered exactly as it lists them.

    One function rather than one per caller, because the row numbers the table
    prints are positions in this list and `--detail 3` has to mean the third row
    the reader just saw. It did not: the detail path rebuilt the list with the
    default severity sort and no filters at all, so under `--sort seen` row 1
    and `--detail 1` were two different findings, and `--severity high
    --detail 3` opened one the table had never listed.
    """
    entries = sorted_findings(ledger)
    if min_severity:
        ceiling = SEVERITY_ORDER.index(min_severity)
        entries = [e for e in entries if severity_floor_rank(e) <= ceiling]
    if signal:
        entries = [e for e in entries if str(e.get("signal", "")).lower() == signal.lower()]

    if sort == "seen":
        entries.sort(key=lambda e: -(occurrences(e, now) or 0))
    elif sort == "last":
        entries.sort(key=lambda e: str(e.get("last_seen", "")), reverse=True)
    elif sort == "first":
        entries.sort(key=lambda e: str(e.get("first_seen", "")))
    else:
        entries.sort(key=lambda e: (severity_rank(e), -(occurrences(e, now) or 0)))
    return entries


def verdict_style(verdict: str) -> str:
    if verdict.startswith("promoted"):
        return "green"
    if "refused" in verdict:
        return "magenta"
    return "yellow"


def parse_gate(env: Dict[str, str]) -> Dict[str, Any]:
    try:
        gate = json.loads(env.get("SELFIMPROVE_GATE", "") or "{}")
    except ValueError:
        return {}
    return gate if isinstance(gate, dict) else {}


def collect_promotions(entries: Sequence[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Every promotion in the ledger, newest first, paired with its finding.

    Every one rather than the latest per finding: a finding filed twice has two
    pull requests against it, and the second is the more interesting of the two
    -- either the first was closed unmerged or the cooldown lapsed with the
    finding still live. A promotion with no URL is kept and rendered as such,
    because that is `record_promotion(confirmed=False)`: a filing turn that
    charged the budget without printing a link, which is precisely the row
    somebody has to go and look for by hand.
    """
    pairs = [
        (promotion, entry)
        for entry in entries
        for promotion in records(entry.get("promotions"))
    ]
    # Ordered on the parsed instant rather than on the text of it. `to_iso`
    # writes `...Z`, which happens to sort correctly as a string, and the same
    # instant written `+00:00` by whoever last edited the ConfigMap does not:
    # `19:00:00-01:00` is an hour after `19:00:00Z` and sorts an hour before it,
    # because `-` is below `Z`. The table says newest first.
    pairs.sort(key=lambda pair: parse_iso(pair[0].get("at")) or UNDATED, reverse=True)
    return pairs


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def render_header(
    ledger: Dict[str, Any],
    raw: str,
    source: str,
    namespace: str,
    name: str,
    cronjob: Optional[Dict[str, Any]],
    env: Dict[str, str],
    gate: Dict[str, Any],
    now: _dt.datetime,
    palette: Palette,
    utc: bool,
) -> List[str]:
    runs = records(ledger.get("runs"))
    entries = sorted_findings(ledger)
    last = runs[-1] if runs else None
    last_at = parse_iso(last.get("at")) if last else None
    outcome = str(last.get("outcome", "?")) if last else "-"

    # The lead line, deliberately: the first question anyone opens the ledger
    # with is whether the loop is still running, and the second is how much
    # history is behind what follows.
    lead = "%s  %s  %s" % (
        palette("last run", "dim"),
        palette(stamp(last_at, utc) if last_at else "never", "bold"),
        palette("(%s)" % ago(last_at, now), "dim"),
    )
    if last:
        lead += "  %s %s" % (
            palette("·", "dim"),
            palette(outcome, OUTCOME_STYLE.get(outcome.lower(), "yellow")),
        )
    lead += "  %s %s" % (
        palette("·", "dim"),
        palette("%d run%s recorded" % (len(runs), "" if len(runs) == 1 else "s"), "bold"),
    )

    lines = [lead, ""]

    def field(label: str, value: str) -> str:
        return "  %s %s" % (palette(label.ljust(10), "dim"), value)

    # Not "all time", which is what this claimed. `prune` deletes a finding a
    # month after its last sighting and keeps only the ten most recent
    # promotions on the ones that survive, so the number goes down as well as
    # up and an install that has filed for a year reports a fraction of it.
    # What it counts is the promotion records the document still holds -- and a
    # record is not proof of a pull request either, since
    # `record_promotion(confirmed=False)` writes one for a filing turn that
    # ended without a URL, so those are counted out loud rather than folded in.
    filed = collect_promotions(entries)
    unconfirmed = sum(1 for promotion, _ in filed if promotion.get("unconfirmed"))
    filed_text = "· %d pull request(s) the ledger still lists" % len(filed)
    if unconfirmed:
        filed_text += " (%d unconfirmed)" % unconfirmed
    lines.append(
        field("findings", "%d in the ledger  %s" % (len(entries), palette(filed_text, "dim")))
    )
    lines.append(field("source", source))
    if source != "file":
        lines.append(field("configmap", "%s/%s" % (namespace, name)))

    mode = env.get("SELFIMPROVE_MODE")
    if mode:
        target = env.get("SELFIMPROVE_FORK_REPO") or env.get("SELFIMPROVE_UPSTREAM_REPO") or ""
        base = env.get("SELFIMPROVE_BASE_BRANCH") or ""
        detail = ""
        if mode != "report-only" and target:
            shown = hyperlink(target, "https://github.com/%s" % target, palette)
            detail = " → %s%s" % (shown, " (base %s)" % base if base else "")
        lines.append(field("mode", palette(mode, "bold") + detail))

    if cronjob:
        schedule = str((cronjob.get("spec") or {}).get("schedule") or "?")
        suspended = bool((cronjob.get("spec") or {}).get("suspend"))
        state = palette("SUSPENDED", "red") if suspended else palette("active", "green")
        scheduled_at = parse_iso((cronjob.get("status") or {}).get("lastScheduleTime"))
        lines.append(
            field(
                "schedule",
                "%s  %s  %s"
                % (schedule, state, palette("last scheduled %s" % ago(scheduled_at, now), "dim")),
            )
        )

    if gate:
        # The gate's own reading of its own numbers, not the raw ones. This line
        # printed what the ConfigMap said while `evaluate_gate` ran both through
        # the sanitisers below, so the two disagreed exactly where it mattered:
        # `{maxPullRequestsPerDay: .inf, cooldownHours: .inf}` rendered
        # "1 of inf ... infh cooldown" against an enforced 1000000 and 24 hours,
        # which a maintainer reads as "nothing will ever re-file" on an install
        # re-filing every day. A quoted `"3"` did not render at all -- comparing
        # the spend against a string raised TypeError and took the report down.
        if ledger_mod is None:
            budget_text, cooldown_text, style = "? of ?", "?", None
        else:
            budget, _ = ledger_mod.sanitise_gate_count(
                gate.get("maxPullRequestsPerDay", 0), "maxPullRequestsPerDay", 0
            )
            cooldown, _ = ledger_mod.sanitise_cooldown_hours(
                gate.get("cooldownHours", ledger_mod.COUNT_WINDOW_HOURS)
            )
            spent = promotions_today(ledger, now)
            budget_text = "%s of %d" % ("?" if spent is None else spent, budget)
            cooldown_text = "%g" % cooldown
            style = "yellow" if (spent is not None and budget and spent >= budget) else None
        lines.append(
            field(
                "budget",
                "%s pull requests in the last 24h  %s"
                % (palette(budget_text, style), palette("· %sh cooldown" % cooldown_text, "dim")),
            )
        )

    cap = ledger_mod.LEDGER_MAX_BYTES if ledger_mod else FALLBACK_MAX_BYTES
    size = len(raw.encode("utf-8"))
    fraction = size / float(cap)
    size_style = "red" if fraction > 0.9 else ("yellow" if fraction > 0.7 else "dim")
    lines.append(
        field(
            "size",
            "%s %s"
            % (
                palette(meter(fraction), size_style),
                palette(
                    "%.1f KiB of %d KiB (%.1f%%)" % (size / 1024.0, cap // 1024, fraction * 100),
                    "dim",
                ),
            ),
        )
    )
    return lines


def render_runs(
    ledger: Dict[str, Any],
    limit: int,
    now: _dt.datetime,
    palette: Palette,
    width: int,
    box: Dict[str, str],
    utc: bool,
) -> List[str]:
    runs = records(ledger.get("runs"))
    if not runs:
        return [palette("  no runs recorded yet", "dim")]
    shown = runs[-limit:] if limit > 0 else runs
    # NOTE goes first on a narrow terminal because it is empty on almost every
    # run, then REVISION, then the absolute WHEN -- AGE answers the same
    # question in a third of the width, and "how long ago" is the question a
    # run history is usually being scanned for.
    columns = [
        Column("WHEN", expendable=1),
        Column("AGE", align="r"),
        Column("OUTCOME"),
        Column("FOUND", align="r"),
        Column("PROMOTED", align="r"),
        Column("FILED", align="r"),
        Column("REVISION", expendable=2),
        Column("NOTE", wrap=True, min_width=14, expendable=3),
    ]
    rows = []
    for run in reversed(shown):
        at = parse_iso(run.get("at"))
        outcome = str(run.get("outcome", "?"))
        rows.append(
            [
                (stamp(at, utc), None),
                (ago(at, now), "dim"),
                (outcome, OUTCOME_STYLE.get(outcome.lower(), "yellow")),
                (str(run.get("findings", 0)), None),
                (str(run.get("promoted", 0)), "green" if run.get("promoted") else "dim"),
                (str(run.get("filed", 0)), "green" if run.get("filed") else "dim"),
                (short_rev(run.get("revision")), "dim"),
                (str(run.get("note") or ""), "dim"),
            ]
        )
    out = render_table(columns, rows, palette, width, box)
    if limit > 0 and len(runs) > limit:
        out.append(palette("  %d older run(s) not shown; --runs 0 for all" % (len(runs) - limit), "dim"))
    return out


def render_findings(
    ledger: Dict[str, Any],
    verdicts: Dict[str, str],
    now: _dt.datetime,
    palette: Palette,
    width: int,
    box: Dict[str, str],
    sort: str,
    min_severity: Optional[str],
    signal: Optional[str],
    repo: str = "",
    roots: frozenset = frozenset(),
    refs: Refs = NO_REFS,
    row_style: str = "spaced",
) -> Tuple[List[str], List[Dict[str, Any]]]:
    entries = select_findings(ledger, now, sort, min_severity, signal)

    if not entries:
        return [palette("  no findings match", "dim")], entries

    # What a narrow terminal loses, in order. REPORTED is the agent's own
    # untrusted number and never gates anything; CONF is one word; SIGNAL is
    # recoverable from the finding text. SEVERITY, SEEN and PRS stay because
    # they are what the table is sorted and scanned by, and FINDING stays
    # because without it there is no table. `--detail` still has all of it.
    columns = [
        Column("#", align="r"),
        Column("SEVERITY"),
        Column("SIGNAL", expendable=1),
        Column("CONF", expendable=2),
        Column("SEEN", align="r"),
        Column("REPORTED", align="r", expendable=3),
        Column("PRS", align="r"),
        Column("LAST", align="r"),
        Column("FINDING", wrap=True, min_width=28),
    ]
    rows = []
    for index, entry in enumerate(entries, start=1):
        severity = str(entry.get("severity", "?")).lower()
        seen = occurrences(entry, now)
        said = reported(entry, now)
        promotions = records(entry.get("promotions"))
        # Three facts stacked in one cell, coloured apart so the eye can pick
        # out the one it came for. Location goes under the title rather than
        # into a column of its own because it is a `path:line` routinely longer
        # than the title, and a column would either let it dominate the table
        # or truncate it past the point of being usable for the one thing it is
        # for. It is clipped to the first of several locations and to a line's
        # worth, because the ones that run to 400 characters are prose about
        # the location rather than a `path:line`; `--detail` has all of it.
        parts = [(str(entry.get("title") or "(untitled)"), None)]
        para_urls: Dict[int, str] = {}
        location = str(entry.get("location") or "")
        if location:
            shown = clip(location.split(" and ")[0], 110)
            parts.append((shown, "cyan"))
            # The paragraph is linked as a whole, to the first file the shown
            # text names -- not to the first one the whole location names, which
            # is a different reference whenever the split or the clip drops a
            # file. `agent/anthropic_adapter.py:42 and
            # agents/selfimprove/scripts/selfimprove_ledger.py:9` labelled the
            # first, which is the Hermes harness and unlinkable, and linked the
            # second: a label and a destination naming different files, in the
            # one column whose whole job is telling a maintainer where to look.
            # No reference inside the shown text, no link. `--detail` links each
            # of them separately, which is the only place a location naming
            # three files has the room to offer three links.
            for label, url in location_links(entry, repo, roots):
                if label in shown:
                    para_urls[len(parts) - 1] = url
                    break
        verdict = verdicts.get(str(entry.get("fingerprint", "")), "")
        if not verdict and isinstance(entry.get("refused"), dict):
            # The gate verdict says this too, but only where there is a gate to
            # replay -- and there is none under `--file`, none under
            # `--no-cronjob`, and none on an install whose CronJob has been
            # removed. A permanent refusal is not a simulation of anything: it
            # is a decision a filing turn already made and wrote into the
            # ledger, and a row that omits it reads as an ordinary live finding
            # the loop is still working on.
            verdict = "refused permanently: %s" % (
                entry["refused"].get("reason") or "no reason recorded"
            )
        if verdict:
            parts.append((verdict, verdict_style(verdict)))
            # A verdict that names a pull request -- `already filed as #161`,
            # `fixed in #874` -- links the paragraph to it. Whole-paragraph
            # rather than on the `#161` itself because the cell is measured for
            # wrapping; `Refs.linkify` says why, and `--detail` is where a
            # verdict naming two numbers gets a link to each. Only the verdict
            # is scanned, never the title: the vocabulary here is one the
            # filing skill dictates, whereas a `#12` in a title the agent wrote
            # about a log line is as likely to be a hostname suffix as a pull
            # request, and a wrong link is worse than none.
            url = refs.first(verdict)
            if url:
                para_urls[len(parts) - 1] = url
        rows.append(
            [
                (str(index), "dim"),
                (severity.upper(), SEVERITY_STYLE.get(severity, "dim")),
                (str(entry.get("signal", "?")), None),
                (str(entry.get("confidence") or "unstated"), "dim"),
                ("?" if seen is None else "%dx" % seen, None),
                ("?" if said is None else compact_count(said), "dim"),
                (str(len(promotions)) if promotions else "-", "green" if promotions else "dim"),
                (ago(parse_iso(entry.get("last_seen")), now), "dim"),
                (
                    "\n".join(text for text, _ in parts),
                    None,
                    None,
                    {i: style for i, (_, style) in enumerate(parts) if style},
                    para_urls,
                ),
            ]
        )
    # Every finding is a stack of title, location and verdict, so a row here is
    # four or five lines tall with only its first line filled in outside the
    # FINDING column. `--rows` is what says where one stops.
    separator = row_separator(row_style)
    return render_table(columns, rows, palette, width, box, separator), entries


def render_promotions(
    pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
    now: _dt.datetime,
    palette: Palette,
    width: int,
    box: Dict[str, str],
    utc: bool,
    row_style: str = "spaced",
) -> List[str]:
    # The pull-request reference is the point of this table, so it and the
    # finding it answers are the last things to go; the severity is already in
    # the findings table above.
    columns = [
        Column("WHEN", expendable=1),
        Column("AGE", align="r"),
        Column("SEV", expendable=2),
        Column("PULL REQUEST"),
        Column("FINDING", wrap=True, min_width=24),
    ]
    rows = []
    for promotion, entry in pairs:
        at = parse_iso(promotion.get("at"))
        severity = str(entry.get("severity", "?")).lower()
        url = str(promotion.get("url") or "")
        if url:
            label, style = pr_ref(url), "blue"
        else:
            label, style = "(filed, no URL recorded)", "yellow"
        if promotion.get("unconfirmed"):
            label += " [unconfirmed]"
            style = "yellow"
        rows.append(
            [
                (stamp(at, utc), None),
                (ago(at, now), "dim"),
                (severity.upper(), SEVERITY_STYLE.get(severity, "dim")),
                (label, style, url),
                (str(entry.get("title") or "(untitled)"), "dim"),
            ]
        )
    # A wrapped finding title makes these rows several lines tall too, and the
    # date that starts one is no more visible than the number in the table above.
    return render_table(columns, rows, palette, width, box, row_separator(row_style))


def render_detail(
    entry: Dict[str, Any],
    verdict: str,
    now: _dt.datetime,
    palette: Palette,
    width: int,
    utc: bool,
    repo: str = "",
    roots: frozenset = frozenset(),
    refs: Refs = NO_REFS,
) -> List[str]:
    severity = str(entry.get("severity", "?")).lower()
    wrap = max(40, width - 4)

    def block(
        label: str, text: str, style: Optional[str] = None, link_refs: bool = False
    ) -> List[str]:
        # `link_refs` linkifies `#123` in the block's text, and is set only on
        # the blocks whose wording the filing skill dictates. Linking happens
        # after the wrap, so the escape sequences cannot affect where lines
        # break.
        #
        # `break_on_hyphens=False` because a reference is only recognised on
        # the line it survives on whole, and every owner here has a hyphen in
        # it. Breaking after one left `labs/kube-agents#874` starting a line,
        # which matches as a qualified reference and links to a repository
        # called `labs/kube-agents` that does not exist -- 13 of the 64
        # terminal widths between 36 and 99 columns did that, and another 17
        # split the slug somewhere that matched nothing and dropped the link.
        # Hyphens are not the only place `textwrap` can break, but they are
        # the only one a repository slug offers it.
        if not text:
            return []
        lines = [palette("  " + label, "dim")]
        for para in str(text).split("\n"):
            for line in textwrap.wrap(para, wrap - 4, break_on_hyphens=False) or [""]:
                lines.append(
                    "    " + palette(refs.linkify(line, palette) if link_refs else line, style)
                )
        return lines + [""]

    head = "%s  %s  %s" % (
        palette(severity.upper(), SEVERITY_STYLE.get(severity, "dim")),
        palette(str(entry.get("signal", "?")), "bold"),
        palette(str(entry.get("fingerprint", "?")), "dim"),
    )
    out = [head, ""]
    out.extend(block("title", str(entry.get("title") or "(untitled)"), "bold"))
    out.extend(block("location", str(entry.get("location") or "(not localised)"), "cyan"))

    # The location as written is prose and stays that way; these are the file
    # references pulled out of it, one per line so each is short enough to
    # survive as a single clickable link, pinned to the revision the finding was
    # made against. A location that names files in another repository, or names
    # none at all, produces no block rather than a dead link.
    links = location_links(entry, repo, roots)
    if links:
        out.append(palette("  open", "dim"))
        out.extend("    " + hyperlink(palette(label, "blue"), url, palette) for label, url in links)
        out.append("")

    seen = occurrences(entry, now)
    said = reported(entry, now)
    out.extend(
        block(
            "counts",
            "seen %s in the last 24h (runs) · reported %s occurrence(s) · confidence %s"
            % (
                "?" if seen is None else "%dx" % seen,
                "?" if said is None else compact_count(said),
                entry.get("confidence") or "unstated",
            ),
        )
    )
    out.extend(
        block(
            "timeline",
            "first seen %s\nlast seen  %s\nrevision   %s"
            % (
                stamp(parse_iso(entry.get("first_seen")), utc),
                stamp(parse_iso(entry.get("last_seen")), utc),
                str(entry.get("revision") or "-"),
            ),
        )
    )
    if verdict:
        out.extend(block("gate", verdict, verdict_style(verdict), link_refs=True))
    out.extend(block("summary", str(entry.get("summary") or "")))
    out.extend(block("user impact", str(entry.get("user_impact") or "")))
    out.extend(block("evidence", str(entry.get("evidence") or ""), "dim"))
    out.extend(block("proposed fix", str(entry.get("proposed_fix") or "")))

    promotions = records(entry.get("promotions"))
    if promotions:
        out.extend(
            block(
                "pull requests",
                "\n".join(
                    "%s  %s%s"
                    % (
                        stamp(parse_iso(p.get("at")), utc),
                        p.get("url") or "(no URL recorded)",
                        "  [unconfirmed]" if p.get("unconfirmed") else "",
                    )
                    for p in promotions
                ),
                "green",
            )
        )

    refusal = entry.get("refused")
    if isinstance(refusal, dict):
        out.extend(
            block(
                "refused",
                "%s\nat %s (%s)"
                % (
                    refusal.get("reason") or "no reason recorded",
                    stamp(parse_iso(refusal.get("at")), utc),
                    short_rev(refusal.get("revision")),
                ),
                "magenta",
                link_refs=True,
            )
        )
    return out


def match_finding(
    entries: List[Dict[str, Any]],
    needle: str,
    pool: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Accepts a table row number or a fingerprint prefix, in that order.

    Row number first because it is what the reader has just been shown, and a
    16-hex-character fingerprint is never a bare integer, so the two cannot
    collide. "What the reader has just been shown" only holds if `entries` is
    the list the table numbered -- `--sort` applied, `--severity` and `--signal`
    applied -- which is what `select_findings` is for.

    A fingerprint is looked up in `pool` instead, the whole ledger by default.
    Unlike a row number it is a name that outlives the table it was read from,
    so `--severity critical --detail cccc` opens the finding rather than
    reporting that nothing matches.
    """
    if needle.isdigit():
        index = int(needle)
        if 1 <= index <= len(entries):
            return entries[index - 1]
    lowered = needle.lower()
    searched = entries if pool is None else pool
    hits = [e for e in searched if str(e.get("fingerprint", "")).lower().startswith(lowered)]
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    return _add_arguments(
        argparse.ArgumentParser(
            prog="selfimprove_ledger_view.py",
            description="Render the self-improvement ledger ConfigMap as a readable report.",
            epilog=(
                "examples:\n"
                "  scripts/selfimprove_ledger_view.py\n"
                "  scripts/selfimprove_ledger_view.py --severity medium --sort seen\n"
                "  scripts/selfimprove_ledger_view.py --detail 3\n"
                "  scripts/selfimprove_ledger_view.py --json | jq '.findings'\n"
                "  kubectl -n kubeagents-system get cm kube-agents-selfimprove-ledger -o json > l.json\n"
                "  scripts/selfimprove_ledger_view.py --file l.json\n"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
    )


def _add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "-n", "--namespace", default=os.environ.get("SELFIMPROVE_NAMESPACE", DEFAULT_NAMESPACE)
    )
    parser.add_argument(
        "-c",
        "--configmap",
        default=os.environ.get("SELFIMPROVE_LEDGER_CONFIGMAP", DEFAULT_CONFIGMAP),
    )
    parser.add_argument("--cronjob", default=DEFAULT_CRONJOB, help="CronJob to read the mode and gate from")
    parser.add_argument(
        "--no-cronjob", action="store_true", help="skip the CronJob read (no mode, schedule or gate)"
    )
    parser.add_argument("--context", default=None, help="kubectl context; defaults to the current one")
    parser.add_argument(
        "-f", "--file", default=None, help="read a ledger or ConfigMap from a file, or - for stdin"
    )
    parser.add_argument("--detail", default=None, metavar="N|FINGERPRINT", help="full record for one finding")
    parser.add_argument(
        "--runs", type=int, default=10, help="runs to show, newest first; 0 for all (default 10)"
    )
    parser.add_argument(
        "--severity", choices=SEVERITY_ORDER, default=None, help="hide findings below this severity"
    )
    parser.add_argument("--signal", default=None, help="only findings in this signal class")
    parser.add_argument("--sort", choices=("severity", "seen", "last", "first"), default="severity")
    parser.add_argument("--json", action="store_true", help="print the raw ledger JSON and exit")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument(
        "--ascii", action="store_true", help="ASCII borders instead of box-drawing characters"
    )
    parser.add_argument("--utc", action="store_true", help="timestamps in UTC instead of local time")
    parser.add_argument(
        "--rows",
        choices=("spaced", "ruled", "compact"),
        default="spaced",
        help="separate table rows with a blank line, a rule, or nothing (default spaced)",
    )
    parser.add_argument("--width", type=int, default=0, help="output width; 0 detects the terminal")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.file:
            ledger, raw = load_from_file(args.file)
            source = "file"
            cronjob = None
        else:
            ledger, raw = load_from_cluster(args.namespace, args.configmap, args.context)
            source = current_context(args.context)
            cronjob = None if args.no_cronjob else load_cronjob(args.namespace, args.cronjob, args.context)
    except LoadError as exc:
        # Scrubbed like everything else: a LoadError carries kubectl's stderr,
        # which is a server message this tool did not compose.
        print("error: %s" % scrub(str(exc)), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print("error: could not read the ledger: %s" % scrub(str(exc)), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(ledger, indent=2, sort_keys=True))
        return 0

    if not isinstance(ledger, dict) or "findings" not in ledger:
        print("error: that does not look like a ledger (no `findings` key)", file=sys.stderr)
        return 1

    # The boundary. Everything past this point draws ledger text into a
    # terminal, and this is the last place it is still data. `--json` above is
    # deliberately outside it: `json.dumps` escapes a control character to
    # `\u001b` rather than emitting it, so that path is already inert, and
    # somebody piping the document into `jq` wants what the ConfigMap holds.
    # `raw` stays as read too -- it is measured, never printed, and the size
    # meter should report the bytes the ConfigMap is actually carrying.
    ledger = scrub_document(ledger)
    cronjob = scrub_document(cronjob)
    source = scrub(source)

    palette = Palette(want_colour(args.color))
    box = BOX_ASCII if args.ascii else BOX_UNICODE
    width = args.width or shutil.get_terminal_size((120, 40)).columns
    width = max(60, min(width, 200))
    now = _dt.datetime.now(_dt.timezone.utc)

    env = cronjob_env(cronjob)
    gate = parse_gate(env)
    verdicts = gate_verdicts(ledger, gate, now)
    repo, roots = target_repo(env), repo_toplevel()
    # One `gh` call, and only for a view that has an install behind it -- under
    # `--file` there is no CronJob, so no base repository, so nothing to ask
    # about and no reference is resolved.
    pr_repo = pull_request_repo(env)
    refs = Refs(pr_repo, filed_pull_requests(ledger), fork_parent(pr_repo))

    if args.detail:
        # The same list the table would have printed, under the same `--sort`
        # and the same filters, because `--detail 3` means the third row of it.
        listed = select_findings(ledger, now, args.sort, args.severity, args.signal)
        entry = match_finding(listed, args.detail, sorted_findings(ledger))
        if entry is None:
            print(
                "error: no finding matches %r (try a row number or a fingerprint prefix)" % args.detail,
                file=sys.stderr,
            )
            return 1
        for line in render_detail(
            entry,
            verdicts.get(str(entry.get("fingerprint", "")), ""),
            now,
            palette,
            width,
            args.utc,
            repo,
            roots,
            refs,
        ):
            print(line)
        return 0

    out: List[str] = []
    out.extend(
        render_header(ledger, raw, source, args.namespace, args.configmap, cronjob, env, gate, now, palette, args.utc)
    )
    out.append("")
    out.append(palette("RUNS", "head"))
    out.extend(render_runs(ledger, args.runs, now, palette, width, box, args.utc))
    out.append("")
    out.append(palette("FINDINGS", "head"))
    table, entries = render_findings(
        ledger, verdicts, now, palette, width, box, args.sort, args.severity, args.signal, repo,
        roots, refs, args.rows,
    )
    out.extend(table)

    # Ledger-wide rather than filtered: "what has this loop actually opened" is
    # a fact about the install, and a --severity filter narrowing it would hide
    # pull requests still open against the findings it hid.
    promotions = collect_promotions(sorted_findings(ledger))
    out.append("")
    out.append(palette("PULL REQUESTS OPENED", "head"))
    if promotions:
        out.extend(render_promotions(promotions, now, palette, width, box, args.utc, args.rows))
    else:
        out.append(
            palette(
                "  none recorded. Under report-only the loop promotes and deliberately does not file;"
                " in fork or upstream mode an empty list under a non-zero promoted count means the"
                " GitHub path failed or the finding was refused.",
                "dim",
            )
        )

    out.append("")
    if verdicts:
        out.append(
            palette(
                "  gate lines simulate the next run re-finding everything, against the CronJob's current gate",
                "dim",
            )
        )
    out.append(palette("  --detail <#> for one finding in full · --help for filters", "dim"))

    for line in out:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
