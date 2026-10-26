#!/usr/bin/env python3
"""Fail when a document states an installer flag, a CRD field set, or a shipped count the source contradicts.

Three kinds of fact are copied into the docs by hand and had no check: the long
options ``install.sh``, ``upgrade.sh`` and ``uninstall.sh`` accept; the fields of
the ``PlatformAgent``, ``AgentProfile`` and ``AgentPlugin`` custom resources; and
the counts of fleet audits, per-persona skills, A2A images and release images.
Each one has a source of truth in the tree, so each one is read from it at run
time and compared with what the documents say:

* **Flags** -- the long options a script accepts are the ``--name`` alternatives
  of its ``case`` arms. A document cites a flag on a script when the flag follows
  the script's name in one shell command (a fenced block line, continuations
  joined, or an inline code span) whose command word is the script, run
  directly, through ``bash``, or as ``bash <(curl …)``; through the
  ``curl … | bash -s -- --flag`` form; or when the flag stands alone in a code
  span inside a paragraph that names exactly one of the three scripts. A script
  named as another command's argument (``curl -o install.sh …``) cites nothing. A command's flag must be one the
  named script accepts; a standalone span's flag must be one some installer
  script accepts, since a paragraph about one script often quotes another's.
* **CRD fields** -- every ``json`` field name reachable from a kind's ``Spec``
  and ``Status`` structs, inline embeds followed, must appear as a word on the
  kind's CRD page. Only structs of the API package are walked: the fields
  inside a ``corev1`` or ``metav1`` type are Kubernetes' own and not required.
* **Counts** -- a number, as digits or a word, directly before ``fleet audits``,
  ``governance watchdogs``, ``audit crons``, ``A2A … images``, ``release
  images`` or ``<persona> skills`` must equal the count the source gives:
  enabled ``jobs.json`` entries carrying the ``fleet-audit`` skill, ``SKILL.md``
  directories under each persona, ``a2a-*`` entries in ``images.json``, and the
  ``REQUIRED_RELEASE_IMAGES`` array. The noun is plural: "four of the fleet
  audits" and "one fleet audit" are not counts.

The documents scanned are the published site, ``INSTALL.md`` and ``README.md``.

Known mismatches live in ``scripts/docs_contracts_allowlist.txt``, one
``<check> <path> <token>`` line each. An allowlisted finding passes; an entry
that matches no finding fails the run until it is dropped, so the list only
shrinks. A source file that is missing, or that yields nothing (a script with no
flag arms, a kind with no fields, an empty image list), fails the run rather
than passing with nothing checked.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_contracts.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- Documents ---------------------------------------------------------------
# The published site plus the two root documents a reader lands on; design
# documents are left to the terminology guard.
SITE_ROOT_REL = Path("docs/site/src/content/docs")
SITE_SUFFIXES = (".md", ".mdx")
ROOT_DOCUMENTS_REL = (Path("INSTALL.md"), Path("README.md"))

# --- Allowlist ---------------------------------------------------------------
ALLOWLIST_REL = Path("scripts/docs_contracts_allowlist.txt")
ALLOWLIST_COMMENT_PREFIX = "#"
# Fields on one allowlist line: the check, the document path, the token.
ALLOWLIST_FIELDS = 3

# --- Check names, as they appear in reports and allowlist lines ----------------
CHECK_FLAG = "flag"
CHECK_FIELD = "field"
CHECK_COUNT = "count"
CHECKS = (CHECK_FLAG, CHECK_FIELD, CHECK_COUNT)

# --- Installer flags -----------------------------------------------------------
INSTALLER_SCRIPTS_REL = (Path("install.sh"), Path("upgrade.sh"), Path("uninstall.sh"))
# One `case` arm whose pattern carries a long option: `--flag)`, `--flag=*)`,
# `-y|--yes|--non-interactive)`. The help text lists the same names but follows
# each with a value or a description rather than `)`, so it does not match.
CASE_ARM_RE = re.compile(r"^\s*(?:[^\s()|]+\|)*--[a-z0-9][a-z0-9-]*(?:=\*)?(?:\|[^\s()]+)*\)")
CASE_ARM_SEPARATOR = "|"
CASE_ARM_CLOSE = ")"
LONG_OPTION_PREFIX = "--"
LONG_OPTION_VALUE_SEPARATOR = "="
# A long option wherever it appears in a command; `--` alone is bash's end of
# options and does not match.
LONG_OPTION_RE = re.compile(r"(?<![\w-])--([a-z0-9][a-z0-9-]*)")
# A script's name as a command: not a suffix of another word, so `uninstall.sh`
# is not a mention of `install.sh`.
SCRIPT_NAME_RE = re.compile(r"(?<![\w-])(install|upgrade|uninstall)\.sh\b")
SCRIPT_NAME_SUFFIX = ".sh"
# Where one shell command ends and the next begins.
COMMAND_SEPARATOR_RE = re.compile(r"\|\||&&|;|\|")
# The segment after `curl … script.sh |` that runs what curl fetched: optional
# environment assignments, optional sudo, then bash or sh.
# An environment assignment before a command; its value may be a `<(…)`
# process substitution, which carries spaces and pipes of its own.
ASSIGNMENT_RE_FRAGMENT = r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:<\([^)]*\)|\S)*\s+)*"
PIPED_RUNNER_RE = re.compile(r"^\s*" + ASSIGNMENT_RE_FRAGMENT + r"(?:sudo(?:\s+-\S+)*\s+)?(?:bash|sh)\b")
# What may stand before a script's name when the script is the segment's
# command: a prompt, environment assignments, sudo or time with options, a
# bash/sh runner with options, a `<(curl …` process substitution feeding it,
# and the path the name hangs off. Anything else (`curl -o`, `diff`) makes the
# script an argument, and the options that follow are that command's.
SCRIPT_COMMAND_PREFIX_RE = re.compile(
    r"^\s*(?:\$\s+)?" + ASSIGNMENT_RE_FRAGMENT + r"(?:(?:sudo|time)(?:\s+-\S+)*\s+)?"
    r"(?:(?:bash|sh)(?:\s+-\S+)*\s+(?:<\(\s*\S+(?:\s+\S+)*?\s+)?)?\S*$"
)
# Separators are read outside parentheses only, so `X=<(a | b) ./install.sh`
# stays one command.
GROUP_OPEN = "("
GROUP_CLOSE = ")"
# A shell comment: `#` at the start of a line or after whitespace, so a URL
# fragment is kept.
SHELL_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")
LINE_CONTINUATION = "\\"
# Markdown structure the document scan reads.
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# A code span that is nothing but one long option, with or without `=value`.
STANDALONE_FLAG_SPAN_RE = re.compile(r"^--([a-z0-9][a-z0-9-]*)(?:=\S*)?$")

# --- CRD fields --------------------------------------------------------------------
API_TYPES_DIR_REL = Path("k8s-operator/api/v1alpha1")
GO_SUFFIX = ".go"
GO_TEST_SUFFIX = "_test.go"
GO_GENERATED_PREFIX = "zz_generated"
# Each kind's root struct, and the site page that documents it.
CRD_PAGES_REL = {
    "PlatformAgent": SITE_ROOT_REL / "operator/platformagent-crd.md",
    "AgentProfile": SITE_ROOT_REL / "operator/agentprofile-crd.md",
    "AgentPlugin": SITE_ROOT_REL / "operator/agentplugin-crd.md",
}
# The two fields of a root struct whose types are walked.
ROOT_STRUCT_FIELDS = ("Spec", "Status")
GO_STRUCT_START_RE = re.compile(r"^type (\w+) struct \{")
GO_STRUCT_END = "}"
GO_LINE_COMMENT = "//"
GO_JSON_TAG_RE = re.compile(r'json:"([^"]*)"')
GO_TAG_OPEN = "`"
GO_TAG_OPTION_SEPARATOR = ","
GO_TAG_INLINE = "inline"
GO_TAG_SKIP = "-"
# `*T`, `[]T`, `map[string]T` all reach T; a dotted type lives in another package.
GO_TYPE_WRAPPER_RE = re.compile(r"^(?:\*|\[\]|map\[[^\]]+\])+")
GO_PACKAGE_SEPARATOR = "."
# A page is read as the set of identifiers it contains, so a field name must
# appear whole: `image` is not satisfied by `imagePullPolicy`.
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_]+")

# --- Counts ----------------------------------------------------------------------------
JOBS_REL = Path("agents/platform/cron/jobs.json")
JOBS_KEY = "jobs"
JOB_SKILLS_KEY = "skills"
JOB_ENABLED_KEY = "enabled"
FLEET_AUDIT_SKILL = "fleet-audit"
AGENTS_DIR_REL = Path("agents")
SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
IMAGES_REL = Path("images.json")
IMAGES_KEY = "images"
IMAGE_NAME_KEY = "name"
A2A_IMAGE_PREFIX = "a2a-"
RELEASE_COMMON_REL = Path("scripts/release/common.sh")
RELEASE_IMAGES_OPEN_RE = re.compile(r"^(?:export\s+|readonly\s+)?REQUIRED_RELEASE_IMAGES=\($")
RELEASE_IMAGES_CLOSE = ")"
RELEASE_IMAGE_ENTRY_RE = re.compile(r'^\s*"([^"]+)"\s*$')
# Names a count subject goes by in prose; each resolves to one source below.
COUNT_FLEET_AUDITS = "fleet audits"
COUNT_A2A_IMAGES = "A2A images"
COUNT_RELEASE_IMAGES = "release images"
COUNT_PERSONA_SKILLS = "{persona} skills"
NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
)
# Not a suffix of a longer word or a hyphenated number: `twenty-one` is not `one`.
# Emphasis or a code span may sit between the number and its noun: `**ten** fleet audits`.
# The number is read off the text before a phrase match, anchored at its end, so
# the phrase is what the document is searched for and the number is checked only
# where a phrase stands: searching for the number first ran over the two-second
# budget.
NUMBER_BEFORE_RE = re.compile(
    r"(?<![\w-])[*_`]*(?P<number>\d+|" + "|".join(NUMBER_WORDS) + r")[*_`]*\s+[*_`]*$", re.IGNORECASE
)
# How far before a phrase the number may start: the longest number word, its
# markup and the whitespace between.
NUMBER_WINDOW = 24
# The subject phrases, keyed by the count they state. `A2A` may carry up to
# two words before `images` (`A2A next-stack images`); the persona list is
# filled in at run time from the directories under agents/.
# The nouns are plural: a count is a total, and "one fleet audit" names an
# item, not a total of one.
COUNT_PHRASES = {
    COUNT_FLEET_AUDITS: r"(?:fleet audits|governance watchdogs|audit crons)",
    COUNT_A2A_IMAGES: r"A2A(?:\s+[\w`-]+){0,2}\s+images",
    COUNT_RELEASE_IMAGES: r"(?:required\s+)?release images",
}
PERSONA_SKILLS_RE_TEMPLATE = r"(?P<persona>{personas})(?:[ -]agent)?(?:'s)?[ -]skills"
# Characters stripped from a matched phrase before it is reported or compared
# with the allowlist.
TOKEN_MARKUP_RE = re.compile(r"[*_`]")
TOKEN_SPACE_RE = re.compile(r"\s+")

# --- Messages ----------------------------------------------------------------------------
NOT_FOUND_ERROR = "{path} not found; the {check} check cannot run"
NO_PAGES_ERROR = "no .md/.mdx page found under {root}; the site root moved and this check scans nothing"
NO_FLAGS_ERROR = "no long option read from the case arms of {path}; the {check} check cannot run"
NO_KIND_ERROR = "no `type {kind} struct` with Spec and Status fields under {path}; the {check} check cannot run"
NO_FIELDS_ERROR = "no json field reached from {kind}'s Spec and Status; the {check} check cannot run"
NO_FLEET_AUDITS_ERROR = "no enabled entry of {path} carries the {skill} skill; the {check} check cannot run"
NO_PERSONAS_ERROR = "no persona directory with a {skills} directory under {path}; the {check} check cannot run"
NO_A2A_IMAGES_ERROR = "no {prefix}* image in {path}; the {check} check cannot run"
NO_RELEASE_IMAGES_ERROR = "no REQUIRED_RELEASE_IMAGES array read from {path}; the {check} check cannot run"
BAD_JSON_ERROR = "{path} is not JSON ({error}); the {check} check cannot run"
BAD_ALLOWLIST_LINE_ERROR = "{path}:{line}: expected `<check> <path> <token>` with check one of {checks}, got {text!r}"
UNKNOWN_COMMAND_FLAG = "`{script}` accepts no `{flag}` (cited on it in a command)"
UNKNOWN_STANDALONE_FLAG = "no installer script accepts `{flag}` (cited in a paragraph about `{script}`)"
MISSING_FIELD = "{kind} field `{name}` (Go type {owner}) appears nowhere on the page"
STALE_COUNT = "says {number} {subject} ({phrase!r}); {source} gives {actual}"
STALE_ALLOWLIST_ENTRY = "{check} {path} {token}: allowlisted but no finding matches it -- drop the entry"
# How a count is reported: the subject's noun phrase and where its number comes from.
COUNT_SOURCE_FLEET_AUDITS = "enabled fleet-audit entries of {path}"
COUNT_SOURCE_PERSONA_SKILLS = "SKILL.md directories under {path}"
COUNT_SOURCE_A2A_IMAGES = "a2a-* entries of {path}"
COUNT_SOURCE_RELEASE_IMAGES = "REQUIRED_RELEASE_IMAGES in {path}"

SUMMARY_FAIL = "{count} documentation contract finding(s) (allowlist: {allowlist}); fix the document, or add a reviewed allowlist line while the fix is pending:"
SUMMARY_STALE = "{count} stale allowlist entr{plural} in {allowlist}:"
SUMMARY_PASS = "Documentation contracts hold across {pages} documents ({allowlisted} allowlisted finding(s))."
PLURAL_ENTRY = ("y", "ies")

DISPLAY_LINE_UNKNOWN = 0


class SourceError(Exception):
    """A source of truth is missing or yields nothing; the run cannot be trusted."""


class Finding:
    """One contradiction: where it is, which check found it, and the token the allowlist names."""

    __slots__ = ("check", "path", "line", "token", "detail")

    def __init__(self, check: str, path: Path, line: int, token: str, detail: str) -> None:
        self.check = check
        self.path = path
        self.line = line
        self.token = token
        self.detail = detail

    def key(self) -> tuple[str, str, str]:
        return (self.check, self.path.as_posix(), normalize_token(self.token))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.check}, {self.path.as_posix()}:{self.line}, {self.token!r})"


def normalize_token(token: str) -> str:
    """Lowercase, markup stripped, spaces collapsed: the form allowlist lines are compared in."""
    return TOKEN_SPACE_RE.sub(" ", TOKEN_MARKUP_RE.sub("", token)).strip().lower()


# --- Documents -----------------------------------------------------------------------


def documents(repo: Path) -> list[Path]:
    """Repository-relative paths of every document scanned, the site first."""
    root = repo / SITE_ROOT_REL
    pages = sorted(p for p in root.rglob("*") if p.suffix in SITE_SUFFIXES and p.is_file())
    if not pages:
        raise SourceError(NO_PAGES_ERROR.format(root=SITE_ROOT_REL))
    rel = [p.relative_to(repo) for p in pages]
    for extra in ROOT_DOCUMENTS_REL:
        if not (repo / extra).is_file():
            raise SourceError(NOT_FOUND_ERROR.format(path=extra, check=CHECK_FLAG))
        rel.append(extra)
    return rel


class Document:
    """A document split the way the checks read it: command lines, prose blocks, raw lines."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines()
        # (line number, command text) for every fenced line, continuations
        # joined and comments stripped, and for every inline code span.
        self.commands: list[tuple[int, str]] = []
        # (start line, block text) for every run of non-blank lines outside a fence.
        self.blocks: list[tuple[int, str]] = []
        self._split()

    def _split(self) -> None:
        in_fence = False
        fence_marker = ""
        pending: list[str] = []
        pending_start = 0
        block: list[str] = []
        block_start = 0

        def flush_command() -> None:
            if pending:
                self.commands.append((pending_start, " ".join(pending)))
                pending.clear()

        def flush_block() -> None:
            if block:
                self.blocks.append((block_start, "\n".join(block)))
                block.clear()

        for number, line in enumerate(self.lines, start=1):
            fence = FENCE_RE.match(line)
            if fence and (not in_fence or fence.group(1) == fence_marker):
                if in_fence:
                    flush_command()
                else:
                    flush_block()
                    fence_marker = fence.group(1)
                in_fence = not in_fence
                continue
            if in_fence:
                code = SHELL_COMMENT_RE.sub("", line).rstrip()
                if not pending:
                    pending_start = number
                if code.endswith(LINE_CONTINUATION):
                    pending.append(code[: -len(LINE_CONTINUATION)].rstrip())
                    continue
                pending.append(code)
                flush_command()
                continue
            for span in INLINE_CODE_RE.finditer(line):
                self.commands.append((number, span.group(1)))
            if not line.strip():
                flush_block()
                continue
            if not block:
                block_start = number
            block.append(line)
        flush_command()
        flush_block()


# --- Installer flags ---------------------------------------------------------------------


def script_flags(path: Path) -> set[str]:
    """The long options a script's case arms accept, as `--name`."""
    flags: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        arm = CASE_ARM_RE.match(line)
        if not arm:
            continue
        pattern = arm.group(0).strip()[: -len(CASE_ARM_CLOSE)]
        for alternative in pattern.split(CASE_ARM_SEPARATOR):
            if alternative.startswith(LONG_OPTION_PREFIX):
                flags.add(alternative.split(LONG_OPTION_VALUE_SEPARATOR)[0])
    return flags


def installer_flags(repo: Path) -> dict[str, set[str]]:
    """Script file name -> the long options it accepts; a script with none is an error."""
    accepted: dict[str, set[str]] = {}
    for rel in INSTALLER_SCRIPTS_REL:
        path = repo / rel
        if not path.is_file():
            raise SourceError(NOT_FOUND_ERROR.format(path=rel, check=CHECK_FLAG))
        flags = script_flags(path)
        if not flags:
            raise SourceError(NO_FLAGS_ERROR.format(path=rel, check=CHECK_FLAG))
        accepted[rel.name] = flags
    return accepted


def split_commands(command: str) -> list[str]:
    """The simple commands of a command line, split at `|`, `||`, `&&` and `;` outside parentheses."""
    segments: list[str] = []
    depth = 0
    start = 0
    position = 0
    while position < len(command):
        char = command[position]
        if char == GROUP_OPEN:
            depth += 1
        elif char == GROUP_CLOSE and depth:
            depth -= 1
        separator = COMMAND_SEPARATOR_RE.match(command, position) if not depth else None
        if separator:
            segments.append(command[start:position])
            position = separator.end()
            start = position
            continue
        position += 1
    segments.append(command[start:])
    return segments


def command_flag_citations(command: str) -> list[tuple[str, str]]:
    """(script file name, flag) for every long option a command passes to an installer script.

    The options after the script's name count when the script is the segment's
    command (``SCRIPT_COMMAND_PREFIX_RE``); the next segment's options count when
    it is a ``bash``/``sh`` runner fed by this one (``curl … | bash -s -- …``).
    """
    segments = split_commands(command)
    citations: list[tuple[str, str]] = []
    for index, segment in enumerate(segments):
        for script in SCRIPT_NAME_RE.finditer(segment):
            name = script.group(1) + SCRIPT_NAME_SUFFIX
            flags: list[str] = []
            if SCRIPT_COMMAND_PREFIX_RE.match(segment[: script.start()]):
                tail = segment[script.end() :]
                flags.extend(LONG_OPTION_PREFIX + m.group(1) for m in LONG_OPTION_RE.finditer(tail))
            if index + 1 < len(segments) and PIPED_RUNNER_RE.match(segments[index + 1]):
                flags.extend(LONG_OPTION_PREFIX + m.group(1) for m in LONG_OPTION_RE.finditer(segments[index + 1]))
            citations.extend((name, flag) for flag in flags)
    return citations


def flag_findings(rel: Path, document: Document, accepted: dict[str, set[str]]) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[int, str, str]] = set()
    for line, command in document.commands:
        for script, flag in command_flag_citations(command):
            if flag in accepted[script] or (line, script, flag) in seen:
                continue
            seen.add((line, script, flag))
            findings.append(Finding(CHECK_FLAG, rel, line, flag, UNKNOWN_COMMAND_FLAG.format(script=script, flag=flag)))
    every_flag = set().union(*accepted.values())
    for start, block in document.blocks:
        named = {m.group(1) + SCRIPT_NAME_SUFFIX for m in SCRIPT_NAME_RE.finditer(block)}
        if len(named) != 1:
            continue
        script = named.pop()
        for span in INLINE_CODE_RE.finditer(block):
            standalone = STANDALONE_FLAG_SPAN_RE.match(span.group(1))
            if not standalone:
                continue
            flag = LONG_OPTION_PREFIX + standalone.group(1)
            line = start + block.count("\n", 0, span.start())
            if flag in every_flag or (line, script, flag) in seen:
                continue
            seen.add((line, script, flag))
            findings.append(
                Finding(CHECK_FLAG, rel, line, flag, UNKNOWN_STANDALONE_FLAG.format(script=script, flag=flag))
            )
    return findings


# --- CRD fields ----------------------------------------------------------------------------


class GoField:
    __slots__ = ("json_name", "type_name", "inline")

    def __init__(self, json_name: str, type_name: str, inline: bool) -> None:
        self.json_name = json_name
        self.type_name = type_name
        self.inline = inline


def parse_go_structs(directory: Path) -> dict[str, list[GoField]]:
    """Struct name -> its json-tagged fields, across the package's hand-written files."""
    structs: dict[str, list[GoField]] = {}
    for path in sorted(directory.glob("*" + GO_SUFFIX)):
        if path.name.endswith(GO_TEST_SUFFIX) or path.name.startswith(GO_GENERATED_PREFIX):
            continue
        current: list[GoField] | None = None
        for raw in path.read_text(encoding="utf-8").splitlines():
            start = GO_STRUCT_START_RE.match(raw)
            if start:
                current = structs.setdefault(start.group(1), [])
                continue
            if current is None:
                continue
            if raw.startswith(GO_STRUCT_END):
                current = None
                continue
            field = parse_go_field(raw)
            if field:
                current.append(field)
    return structs


def parse_go_field(raw: str) -> GoField | None:
    line = raw.strip()
    if line.startswith(GO_LINE_COMMENT):
        return None
    tag_at = line.find(GO_TAG_OPEN)
    if tag_at < 0:
        return None
    tag = GO_JSON_TAG_RE.search(line[tag_at:])
    if not tag:
        return None
    options = tag.group(1).split(GO_TAG_OPTION_SEPARATOR)
    json_name = options[0]
    inline = GO_TAG_INLINE in options[1:]
    if json_name == GO_TAG_SKIP:
        return None
    tokens = line[:tag_at].split()
    if not tokens:
        return None
    type_name = tokens[0] if len(tokens) == 1 else tokens[1]
    return GoField(json_name, type_name, inline or (not json_name and len(tokens) == 1))


def local_type(type_name: str) -> str | None:
    """The package-local struct a field type reaches, or None for a builtin or another package's type."""
    bare = GO_TYPE_WRAPPER_RE.sub("", type_name)
    if GO_PACKAGE_SEPARATOR in bare:
        return None
    return bare


def kind_fields(structs: dict[str, list[GoField]], kind: str) -> dict[str, str]:
    """json field name -> the Go struct that declares it, for every field reachable from the kind's Spec and Status."""
    root = structs.get(kind)
    roots = [f for f in (root or []) if f.json_name in [name.lower() for name in ROOT_STRUCT_FIELDS]]
    if root is None or len(roots) != len(ROOT_STRUCT_FIELDS):
        raise SourceError(NO_KIND_ERROR.format(kind=kind, path=API_TYPES_DIR_REL, check=CHECK_FIELD))
    fields: dict[str, str] = {}
    queue = [local_type(f.type_name) for f in roots]
    visited: set[str] = set()
    while queue:
        name = queue.pop()
        if name is None or name in visited or name not in structs:
            continue
        visited.add(name)
        for field in structs[name]:
            nested = local_type(field.type_name)
            if field.json_name and not field.inline:
                fields.setdefault(field.json_name, name)
            if nested:
                queue.append(nested)
    if not fields:
        raise SourceError(NO_FIELDS_ERROR.format(kind=kind, check=CHECK_FIELD))
    return fields


def field_findings(repo: Path) -> list[Finding]:
    directory = repo / API_TYPES_DIR_REL
    if not directory.is_dir():
        raise SourceError(NOT_FOUND_ERROR.format(path=API_TYPES_DIR_REL, check=CHECK_FIELD))
    structs = parse_go_structs(directory)
    findings: list[Finding] = []
    for kind, page_rel in CRD_PAGES_REL.items():
        page = repo / page_rel
        if not page.is_file():
            raise SourceError(NOT_FOUND_ERROR.format(path=page_rel, check=CHECK_FIELD))
        identifiers = set(IDENTIFIER_RE.findall(page.read_text(encoding="utf-8")))
        for name, owner in sorted(kind_fields(structs, kind).items()):
            if name in identifiers:
                continue
            findings.append(
                Finding(
                    CHECK_FIELD,
                    page_rel,
                    DISPLAY_LINE_UNKNOWN,
                    name,
                    MISSING_FIELD.format(kind=kind, name=name, owner=owner),
                )
            )
    return findings


# --- Counts ----------------------------------------------------------------------------------


def load_json(repo: Path, rel: Path) -> object:
    path = repo / rel
    if not path.is_file():
        raise SourceError(NOT_FOUND_ERROR.format(path=rel, check=CHECK_COUNT))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SourceError(BAD_JSON_ERROR.format(path=rel, error=error, check=CHECK_COUNT)) from error


def fleet_audit_count(repo: Path) -> int:
    roster = load_json(repo, JOBS_REL)
    jobs = roster.get(JOBS_KEY, []) if isinstance(roster, dict) else []
    count = sum(
        1
        for job in jobs
        if isinstance(job, dict)
        and job.get(JOB_ENABLED_KEY, True)
        and FLEET_AUDIT_SKILL in (job.get(JOB_SKILLS_KEY) or [])
    )
    if not count:
        raise SourceError(NO_FLEET_AUDITS_ERROR.format(path=JOBS_REL, skill=FLEET_AUDIT_SKILL, check=CHECK_COUNT))
    return count


def persona_skill_counts(repo: Path) -> dict[str, int]:
    """Persona directory name -> SKILL.md directories under it, for every persona with a skills directory."""
    agents = repo / AGENTS_DIR_REL
    if not agents.is_dir():
        raise SourceError(NOT_FOUND_ERROR.format(path=AGENTS_DIR_REL, check=CHECK_COUNT))
    counts = {
        persona.name: sum(1 for skill in (persona / SKILLS_DIR).iterdir() if (skill / SKILL_FILE).is_file())
        for persona in sorted(agents.iterdir())
        if (persona / SKILLS_DIR).is_dir()
    }
    if not counts:
        raise SourceError(NO_PERSONAS_ERROR.format(skills=SKILLS_DIR, path=AGENTS_DIR_REL, check=CHECK_COUNT))
    return counts


def a2a_image_count(repo: Path) -> int:
    inventory = load_json(repo, IMAGES_REL)
    images = inventory.get(IMAGES_KEY, []) if isinstance(inventory, dict) else []
    count = sum(1 for image in images if isinstance(image, dict) and str(image.get(IMAGE_NAME_KEY, "")).startswith(A2A_IMAGE_PREFIX))
    if not count:
        raise SourceError(NO_A2A_IMAGES_ERROR.format(prefix=A2A_IMAGE_PREFIX, path=IMAGES_REL, check=CHECK_COUNT))
    return count


def release_image_count(repo: Path) -> int:
    path = repo / RELEASE_COMMON_REL
    if not path.is_file():
        raise SourceError(NOT_FOUND_ERROR.format(path=RELEASE_COMMON_REL, check=CHECK_COUNT))
    count = 0
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if RELEASE_IMAGES_OPEN_RE.match(line.strip()):
            inside = True
            continue
        if inside and line.strip() == RELEASE_IMAGES_CLOSE:
            break
        if inside and RELEASE_IMAGE_ENTRY_RE.match(line):
            count += 1
    if not count:
        raise SourceError(NO_RELEASE_IMAGES_ERROR.format(path=RELEASE_COMMON_REL, check=CHECK_COUNT))
    return count


class CountRule:
    """One subject phrase, the number the source gives for it, and how to name that source."""

    __slots__ = ("subject", "pattern", "actual", "source")

    def __init__(self, subject: str, phrase: str, actual: int, source: str) -> None:
        self.subject = subject
        self.pattern = re.compile(phrase, re.IGNORECASE)
        self.actual = actual
        self.source = source


def count_rules(repo: Path) -> list[CountRule]:
    rules = [
        CountRule(
            COUNT_FLEET_AUDITS,
            COUNT_PHRASES[COUNT_FLEET_AUDITS],
            fleet_audit_count(repo),
            COUNT_SOURCE_FLEET_AUDITS.format(path=JOBS_REL),
        ),
        CountRule(
            COUNT_A2A_IMAGES,
            COUNT_PHRASES[COUNT_A2A_IMAGES],
            a2a_image_count(repo),
            COUNT_SOURCE_A2A_IMAGES.format(path=IMAGES_REL),
        ),
        CountRule(
            COUNT_RELEASE_IMAGES,
            COUNT_PHRASES[COUNT_RELEASE_IMAGES],
            release_image_count(repo),
            COUNT_SOURCE_RELEASE_IMAGES.format(path=RELEASE_COMMON_REL),
        ),
    ]
    for persona, count in persona_skill_counts(repo).items():
        rules.append(
            CountRule(
                COUNT_PERSONA_SKILLS.format(persona=persona),
                PERSONA_SKILLS_RE_TEMPLATE.format(personas=re.escape(persona)),
                count,
                COUNT_SOURCE_PERSONA_SKILLS.format(path=AGENTS_DIR_REL / persona / SKILLS_DIR),
            )
        )
    return rules


def number_value(text: str) -> int:
    lowered = text.lower()
    if lowered.isdigit():
        return int(lowered)
    return NUMBER_WORDS.index(lowered)


def count_findings(rel: Path, document: Document, rules: list[CountRule]) -> list[Finding]:
    """Every count phrase whose number, written directly before it, is not the source's."""
    findings: list[Finding] = []
    text = document.text
    for rule in rules:
        for match in rule.pattern.finditer(text):
            before = NUMBER_BEFORE_RE.search(text[max(0, match.start() - NUMBER_WINDOW) : match.start()])
            if not before:
                continue
            stated = number_value(before.group("number"))
            if stated == rule.actual:
                continue
            phrase = before.group(0) + match.group(0)
            findings.append(
                Finding(
                    CHECK_COUNT,
                    rel,
                    text.count("\n", 0, match.start()) + 1,
                    phrase,
                    STALE_COUNT.format(
                        number=stated, subject=rule.subject, phrase=phrase, source=rule.source, actual=rule.actual
                    ),
                )
            )
    return findings


# --- Allowlist ----------------------------------------------------------------------------------


def load_allowlist(path: Path) -> list[tuple[str, str, str]]:
    """(check, path, normalized token) per entry; a malformed line is an error."""
    entries: list[tuple[str, str, str]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.strip()
        if not text or text.startswith(ALLOWLIST_COMMENT_PREFIX):
            continue
        parts = text.split(None, ALLOWLIST_FIELDS - 1)
        if len(parts) != ALLOWLIST_FIELDS or parts[0] not in CHECKS:
            raise SourceError(
                BAD_ALLOWLIST_LINE_ERROR.format(path=path, line=number, checks=CHECKS, text=text)
            )
        entries.append((parts[0], parts[1], normalize_token(parts[2])))
    return entries


# --- Driver ---------------------------------------------------------------------------------------


def scan(repo: Path) -> list[Finding]:
    """Every finding across the scanned documents, sorted by path and line."""
    scanned = documents(repo)
    accepted = installer_flags(repo)
    rules = count_rules(repo)
    findings = field_findings(repo)
    for rel in scanned:
        document = Document((repo / rel).read_text(encoding="utf-8"))
        findings.extend(flag_findings(rel, document, accepted))
        findings.extend(count_findings(rel, document, rules))
    findings.sort(key=lambda f: (f.path.as_posix(), f.line, f.check, f.token))
    return findings


def main(repo: Path = REPO) -> int:
    allowlist_path = repo / ALLOWLIST_REL
    try:
        if not allowlist_path.is_file():
            raise SourceError(NOT_FOUND_ERROR.format(path=ALLOWLIST_REL, check="allowlist"))
        allowlist = load_allowlist(allowlist_path)
        findings = scan(repo)
        pages = len(documents(repo))
    except SourceError as error:
        print(f"ERROR: {error}")
        return 1

    allowed = set(allowlist)
    matched: set[tuple[str, str, str]] = set()
    reported: list[Finding] = []
    for finding in findings:
        if finding.key() in allowed:
            matched.add(finding.key())
        else:
            reported.append(finding)
    stale = [entry for entry in allowlist if entry not in matched]

    failed = False
    if reported:
        failed = True
        print(SUMMARY_FAIL.format(count=len(reported), allowlist=ALLOWLIST_REL))
        for finding in reported:
            where = f"{finding.path.as_posix()}:{finding.line}" if finding.line else finding.path.as_posix()
            print(f"  {where}: {finding.check} -- {finding.detail}")
    if stale:
        failed = True
        plural = PLURAL_ENTRY[1] if len(stale) != 1 else PLURAL_ENTRY[0]
        print(SUMMARY_STALE.format(count=len(stale), plural=plural, allowlist=ALLOWLIST_REL))
        for check, path, token in stale:
            print("  " + STALE_ALLOWLIST_ENTRY.format(check=check, path=path, token=token))
    if failed:
        return 1
    print(SUMMARY_PASS.format(pages=pages, allowlisted=len(matched)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
