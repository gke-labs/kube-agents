#!/usr/bin/env python3
"""Fail when a file under ``agents/`` adds a whole-cluster kubectl read.

A ``kubectl get <kinds> -A -o json`` returns every object of every kind named,
in every namespace, as one reply. In the agent's shell that reply crosses the
credential proxy, which cuts it at its output cap, so on a large cluster the
read fails and the collector or the model that issued it works from nothing.
The collectors and the upgrade scripts read clusters that way when this
check arrived (``scripts/cluster_reads_allowlist.txt`` lists them), and the
SOPs and skills tell the model to run the same shape. Nothing stopped a new
one being added; this check does. Three rules:

* ``multi-kind-dump``: a ``get`` whose kinds token names several kinds (holds
  a comma, or is ``all``), with ``-A``/``--all-namespaces`` and full objects
  out (``-o json`` or ``yaml``).
* ``unpaged-list``: one kind, the same flags, with no ``--chunk-size`` above
  zero, so the list is asked for in one page; and a ``get --raw`` of a
  whole-cluster collection (``/api/v1/<kind>``, ``/apis/<group>/<version>/<kind>``)
  with no ``limit=`` in the URL.
* ``unresolved-kinds``: the same flags with a kinds token the check cannot
  read (a parameter, an f-string), so it cannot show the read is one kind,
  and no flag on the line clears it.

``--chunk-size`` pages the requests kubectl makes and not the reply it
prints, so it bounds nothing the credential proxy sees; it passes the
``unpaged-list`` rule because the issue that asked for this check names it as
the paged form, and the remedies that bound the reply are the ones the hint
names first. Reads that pass: a namespaced ``get``; a ``-o jsonpath=``,
``custom-columns=``, ``name`` or ``wide`` projection; a table read with no
``-o`` at all on a shell line; a single object (``kind/name``, or a name
after the kind without ``-A``); and a ``--raw`` URL that names a namespace or
carries ``limit=``.

What is read, under ``agents/`` with the mirrored ``gke-*`` skills skipped
(their text changes on sync and is edited through the overlay):

* ``*.py`` through ``ast``: every list and tuple literal is walked and judged
  as the argv it builds. Names resolve against the module's top-level string
  and sequence constants, a starred name or literal splices its elements, a
  starred conditional splices both branches, a starred call splices one
  unknown token (a helper's prefix, stepped over as ``kubectl`` would be), an
  f-string resolves to its constant text with each ``{name}`` replaced by the
  module constant or loop element it names and ``{}`` where a value cannot
  be read, and a ``for`` loop over a literal sequence judges its body once
  per element. A list that opens with ``get`` (no ``kubectl`` before it) and
  carries ``-A`` with no ``-o`` is judged full-object: the audit scripts pass
  such lists to a helper that appends ``-o json``; a list that opens with
  ``kubectl`` and names no ``-o`` is a table, as on a shell line. A string
  literal, an f-string or a ``+`` of strings that carries ``kubectl`` is read
  as a shell line (``shell=True``, ``.split()``), docstrings excepted. Test
  files (``test_*.py``) and ``testdata/`` trees are not read.
* ``*.sh``: every command line, continuations joined, comments dropped, each
  simple command of a pipeline on its own, ``kubectl`` found wherever it
  stands in it (after ``KUBECONFIG=... ``, ``env``, ``timeout 60``). A quoted
  word that holds a whole command (``bash -c "kubectl ..."``, the
  ``"command": "kubectl ..."`` of a JSON example) is read as a line of its
  own. A kinds token that is a shell expansion (``"$KINDS"``) cannot be shown
  to be one kind and falls under ``unresolved-kinds``.
* ``*.md``: the fenced code blocks and the inline code spans. Prose is not
  read, so a sentence that names the shape to warn against it is not a hit.

The reads the tree carried when the check arrived are pinned in
``scripts/cluster_reads_allowlist.txt``: one line per file and rule, with the
count that file has today and the reason it is still there. The list only
shrinks. A file with more findings than its entry fails as a rise; a file
with fewer fails until the entry is lowered or removed; an entry naming a
file that is gone, an unknown rule, a zero count, a repeated file and rule,
or a malformed line fails the run. A file with no entry is held at zero.

The check also fails when it finds no file to scan: a moved tree would
otherwise turn the gate green with nothing checked.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_cluster_reads.py            # the gate
    python3 scripts/check_cluster_reads.py --counts   # allow-list lines for the tree as it is
"""

from __future__ import annotations

import argparse
import ast
import re
import shlex
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The tree the rule binds: the agent blueprints, whose scripts the collectors
# run and whose SOPs and skills the model reads.
SCAN_ROOT = REPO / "agents"
# Mirrored skills (AGENTS.md "Skills Guidelines"): generated from
# third_party/google-skills and the overlay, never edited in place.
SKIP_PATH_PREFIXES = ("platform/skills/gke-",)
PYTHON_SUFFIX = ".py"
SHELL_SUFFIX = ".sh"
MARKDOWN_SUFFIX = ".md"
SCAN_SUFFIXES = (PYTHON_SUFFIX, SHELL_SUFFIX, MARKDOWN_SUFFIX)
# Tests carry offending argv on purpose, as the input a test feeds; fixtures
# under testdata/ are captured output, not commands the agent runs.
TEST_FILE_PREFIX = "test_"
TESTDATA_DIR = "testdata"

# The two rules, named as the report and the allow-list spell them.
RULE_MULTI_KIND = "multi-kind-dump"
RULE_UNPAGED = "unpaged-list"
RULE_UNRESOLVED = "unresolved-kinds"
RULES = (RULE_MULTI_KIND, RULE_UNPAGED, RULE_UNRESOLVED)
RULE_HINTS = {
    RULE_MULTI_KIND: "several kinds across every namespace as full objects: read one kind per call",
    RULE_UNPAGED: (
        "one kind across every namespace as full objects in one reply: project the fields (-o jsonpath=, "
        "custom-columns=), read per namespace, or page it (--chunk-size above 0; limit= on a --raw URL)"
    ),
    RULE_UNRESOLVED: (
        "the kinds are not a literal this check can read, so it cannot show the read is one kind: "
        "pass a literal kind per call, or pin the site with a reason"
    ),
}

# The kubectl vocabulary the rules read.
KUBECTL = "kubectl"
KUBECTL_PATH_SUFFIX = "/" + KUBECTL
GET = "get"
ALL_NAMESPACES_FLAGS = ("-A", "--all-namespaces", "--all-namespaces=true")
# `-Ao json` is `-A` and `-o json` in one token; the `-A` is read off and the
# rest is a flag of its own.
ALL_NAMESPACES_SHORT_FLAG = "-A"
OUTPUT_SHORT_FLAG = "-o"
OUTPUT_LONG_FLAG = "--output"
OUTPUT_FLAGS = (OUTPUT_SHORT_FLAG, OUTPUT_LONG_FLAG)
FULL_OBJECT_FORMATS = ("json", "yaml")
CHUNK_SIZE_FLAG = "--chunk-size"
# kubectl documents `--chunk-size=0` as "disable chunking".
CHUNK_SIZE_DISABLED = "0"
RAW_FLAG = "--raw"
LIMIT_QUERY = "limit="
# A `--raw` URL that lists a whole-cluster collection: the core or a group
# API, a version, one resource segment and nothing after it but a query. A
# namespaced path (`/api/v1/namespaces/<ns>/pods`), a single object
# (`/api/v1/nodes/<name>`) and a discovery or health path have another shape.
RAW_COLLECTION_RE = re.compile(r"^/(?:api/v1|apis/[^/]+/[^/]+)/[^/?]+(?:\?|$)")
KIND_SEPARATOR = ","
# `kubectl get all` expands to several kinds on the server.
ALL_KINDS_ALIAS = "all"
NAME_SEPARATOR = "/"
FLAG_PREFIX = "-"
LONG_FLAG_PREFIX = FLAG_PREFIX * 2
FLAG_VALUE_SEPARATOR = "="
# `-o` is a short flag; `-ojson` is the same flag with its value glued on.
SHORT_FLAG_LENGTH = 2
# A flag that took the next token as its value spans two tokens.
FLAG_WITH_VALUE_WIDTH = 2
# Flags that take the next token as their value when not written with `=`,
# so that value is never mistaken for the kinds: kubectl's `get` flags and
# the global ones a command line carries. The credential proxy's transcript
# reader keeps the same table (`KUBECTL_VALUE_FLAGS` and `KUBECTL_FILE_FLAGS`
# in agents/platform/scripts/worker_transcript.py); the test holds this set
# to be a superset of those two and disjoint from its `KUBECTL_BOOLEAN_FLAGS`
# (a boolean flag listed here would eat the token after it), so a flag added
# there is added here and a boolean one never is.
FLAGS_WITH_VALUE = frozenset(
    {
        "-n", "--namespace", "-l", "--selector", "--field-selector", "-L", "--label-columns",
        "--sort-by", "--template", "--chunk-size", "--request-timeout", "--timeout", "--context",
        "--kubeconfig", "--cluster", "--user", "--token", "-s", "--server", "--as", "--as-group",
        "--as-uid", "--cache-dir", "--certificate-authority", "--client-certificate", "--client-key",
        "--tls-server-name", "--subresource", "--raw", "-f", "--filename",
        "-k", "--kustomize", "-v", "--v", "--field-manager", "--profile", "--profile-output",
        OUTPUT_SHORT_FLAG, OUTPUT_LONG_FLAG,
    }
)

# Shell reading. shlex with punctuation characters splits `|`, `;`, `&`,
# redirections and parentheses into their own tokens, and a token made of them
# ends a command, so `kubectl ... | jq` and `a && kubectl ...` are each read
# as their own argv. A line the lexer refuses (an unbalanced quote in an SOP
# fragment) is split on whitespace instead, so it is still read.
SHELL_PUNCTUATION = set("|&;<>()")
# The file descriptor of a redirection (`2>/dev/null`, `2>&1`) comes off the
# lexer as its own word before the `>`; it is not an argument, unless the
# word before it is a flag that takes the next token (`--chunk-size 500 >`).
REDIRECT_CHARS = set("<>")
# A word with a blank inside it came off the lexer from one quoted string; one
# that carries `kubectl` is a command of its own (`bash -c "kubectl ..."`).
QUOTED_COMMAND_RE = re.compile(r"\s")
# A shell expansion where the kinds should be (`"$KINDS"`, `${KINDS}`): the
# check cannot show it is one kind.
SHELL_EXPANSION_MARK = "$"
# What may stand before a bare `get` in the Python reading: a `NAME=value`
# assignment, `env`, or a spliced helper prefix the check could not read.
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ENV_COMMAND = "env"
# An SOP's `<ctx>` or `<target kubeconfig>` is a placeholder, not a pair of
# redirections: it is read as one word. A real `< in > out` has blanks inside
# the brackets and is left alone.
PLACEHOLDER_RE = re.compile(r"<(?=\S)([^<>\n]+?)(?<=\S)>")
PLACEHOLDER_JOIN = "_"
SHELL_COMMENT_PREFIX = "#"
SHELL_CONTINUATION = "\\"
# What a Python f-string resolves to where a value goes; a kinds token that
# carries it is not one kind the check can name.
FORMAT_PLACEHOLDER = "{}"

# Markdown reading: a fence opens on a line whose first non-blank characters
# (after quote markers and a bullet or numbered list marker) are three
# backticks or tildes and closes on the next such line; the lines between are
# shell lines. Outside fences, a code span is a run of backticks closed by a
# run of the same length, and may wrap onto the next line but not across a
# blank one (a paragraph ends there, so a stray backtick pairs with nothing
# past it).
FENCE_RE = re.compile(r"^[\s>*+-]*(?:\d+[.)]\s+)?(?:```|~~~)")
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?P<code>(?:(?!\n\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
LINE_BREAK = "\n"
SPACE = " "

# The shrinking allow-list: `<path> <rule> <count> <reason>` per line, the
# path relative to the repository root, the rule as this check reports it,
# the count above zero, the reason the rest of the line. `#` lines and blank
# lines are ignored.
ALLOWLIST = REPO / "scripts" / "cluster_reads_allowlist.txt"
ALLOWLIST_FIELDS = 4
LIST_FILE_COMMENT_PREFIX = "#"
ALLOWLIST_MALFORMED_ERROR = "{allowlist}:{line}: expected `<path> <rule> <count> <reason>` with a count above zero, got {text!r}"
ALLOWLIST_UNKNOWN_RULE_ERROR = "{allowlist}:{line}: rule {rule!r} is not one of {rules}"
ALLOWLIST_DUPLICATE_ERROR = "{allowlist}:{line}: {path} already has an entry for {rule!r} at line {first}"
ALLOWLIST_MISSING_PATH_ERROR = "{allowlist}:{line}: {path} is not a file this check scans; remove the entry"
ALLOWLIST_RISE_ERROR = (
    "{path}: {found} finding(s) of rule {rule!r}, the allow-list entry at {allowlist}:{line} allows {allowed}; "
    "the list only shrinks, so convert enough of the {found} listed below to return to {allowed}"
)
ALLOWLIST_DROP_ERROR = (
    "{path}: {found} finding(s) of rule {rule!r}, the allow-list entry at {allowlist}:{line} allows {allowed}; "
    "lower the entry to {found}"
)
ALLOWLIST_CLEAN_ERROR = (
    "{path}: no finding of rule {rule!r} left, the allow-list entry at {allowlist}:{line} allows {allowed}; "
    "remove the entry"
)
UNPINNED_ERROR = "{path}: {found} finding(s) of rule {rule!r} and no allow-list entry; convert the read, or pin it with a reason"
FINDING_LINE = "  {path}:{line}: {rule}: {text}"
HINT_LINE = "    {hint}"
COUNT_LINE = "{path} {rule} {count}"
NO_FILES_ERROR = "no .py/.sh/.md file found under {root}; the tree moved and this check scans nothing"
OK_MESSAGE = "cluster-reads-check: {files} file(s) read, {pinned} pinned finding(s), nothing new"


class Finding:
    """One whole-cluster read: where it is, which rule caught it, the argv as read."""

    __slots__ = ("path", "line", "rule", "text")

    def __init__(self, path: Path, line: int, rule: str, text: str) -> None:
        self.path = path
        self.line = line
        self.rule = rule
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.path!s}:{self.line}, {self.rule!r}, {self.text!r})"


class AllowlistEntry:
    """One pinned (path, rule) count and the allow-list line that carries it."""

    __slots__ = ("path", "rule", "count", "line")

    def __init__(self, path: str, rule: str, count: int, line: int) -> None:
        self.path = path
        self.rule = rule
        self.count = count
        self.line = line

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AllowlistEntry({self.path} {self.rule} {self.count} @{self.line})"


# --- judging one argv -------------------------------------------------------

Token = str | None


def is_flag(token: Token) -> bool:
    return token is not None and token.startswith(FLAG_PREFIX) and token != FLAG_PREFIX


def flag_name(token: str) -> str:
    """`--output=json` -> `--output`; `-ojson` -> `-o`; a long flag is itself."""
    if token.startswith(LONG_FLAG_PREFIX):
        return token.split(FLAG_VALUE_SEPARATOR, 1)[0]
    return token[:SHORT_FLAG_LENGTH]


def flag_value(token: str, following: Token) -> tuple[Token, bool]:
    """The value a flag carries and whether it consumed the following token."""
    if token.startswith(LONG_FLAG_PREFIX):
        if FLAG_VALUE_SEPARATOR in token:
            return token.split(FLAG_VALUE_SEPARATOR, 1)[1], False
        return following, True
    if len(token) > SHORT_FLAG_LENGTH:
        return token[SHORT_FLAG_LENGTH:].lstrip(FLAG_VALUE_SEPARATOR), False
    return following, True


def is_kubectl(token: Token) -> bool:
    return token == KUBECTL or (token is not None and token.endswith(KUBECTL_PATH_SUFFIX))


def get_argv_start(tokens: list[Token], bare_get: bool) -> int | None:
    """Index of the `get` verb when the tokens are a `kubectl get` argv, else None.

    `kubectl` may stand anywhere in the simple command (`KUBECONFIG=$KC
    kubectl ...`, `timeout 60 kubectl ...`). Between it and `get` only flags
    and their values may sit (`--kubeconfig x get ...`); any other word is
    another subcommand. With `bare_get`, the Python reading, a list that
    opens with `get` after assignments, `env` or tokens the check could not
    read (a spliced helper prefix) is an argv too: a helper prepends
    `kubectl`.
    """
    index = next((i for i, token in enumerate(tokens) if is_kubectl(token)), None)
    if index is None:
        if not bare_get:
            return None
        index = 0
        while index < len(tokens) and (
            tokens[index] is None or tokens[index] == ENV_COMMAND or ENV_ASSIGNMENT_RE.match(tokens[index])
        ):
            index += 1
        return index if index < len(tokens) and tokens[index] == GET else None
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token == GET:
            return index
        if not is_flag(token):
            return None
        _, consumed = flag_value(token, None)
        index += FLAG_WITH_VALUE_WIDTH if consumed and flag_name(token) in FLAGS_WITH_VALUE else 1
    return None


def judge(tokens: list[Token], python_argv: bool) -> str | None:
    """The rule a `kubectl get` argv breaks, or None.

    `python_argv` is the Python reading: a list may open with `get` (a helper
    prepends `kubectl`), and such a list that names no `-o` is handed to a
    helper that appends `-o json`, so it counts as full objects. A shell
    line, or a list that opens with `kubectl`, needs the `-o`; with none it
    is a table.
    """
    start = get_argv_start(tokens, bare_get=python_argv)
    if start is None:
        return None
    helper_argv = python_argv and not any(is_kubectl(token) for token in tokens[:start])
    positionals: list[Token] = []
    all_namespaces = False
    output: Token = None
    output_named = False
    paged = False
    raw_url: Token = None
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token in ALL_NAMESPACES_FLAGS:
            all_namespaces = True
            index += 1
            continue
        if (
            token is not None
            and token.startswith(ALL_NAMESPACES_SHORT_FLAG)
            and len(token) > SHORT_FLAG_LENGTH
            and token[SHORT_FLAG_LENGTH].isalpha()
        ):
            all_namespaces = True
            token = FLAG_PREFIX + token[SHORT_FLAG_LENGTH:]
        if is_flag(token):
            name = flag_name(token)
            value, consumed = flag_value(token, following)
            if name in OUTPUT_FLAGS:
                output, output_named = value, True
            elif name == CHUNK_SIZE_FLAG:
                # Only a literal `0` disables chunking; a value the check
                # cannot read (`str(CHUNK_SIZE)`, a parameter) was still written.
                paged = value != CHUNK_SIZE_DISABLED
            elif name == RAW_FLAG:
                raw_url = value
            index += FLAG_WITH_VALUE_WIDTH if consumed and name in FLAGS_WITH_VALUE else 1
            continue
        positionals.append(token)
        index += 1
    if raw_url is not None:
        if LIMIT_QUERY in raw_url or not RAW_COLLECTION_RE.match(raw_url):
            return None
        return RULE_UNPAGED
    if not all_namespaces or not positionals:
        return None
    # kubectl refuses a name with -A, so the first positional is the kinds
    # and anything after it is the value of a flag this check does not know.
    kinds = positionals[0]
    full_object = output in FULL_OBJECT_FORMATS if output_named else helper_argv
    if not full_object:
        return None
    if kinds is None or FORMAT_PLACEHOLDER in kinds or SHELL_EXPANSION_MARK in kinds:
        return RULE_UNRESOLVED
    if NAME_SEPARATOR in kinds:
        return None
    if KIND_SEPARATOR in kinds or kinds == ALL_KINDS_ALIAS:
        return RULE_MULTI_KIND
    if paged:
        return None
    return RULE_UNPAGED


def render(tokens: list[Token]) -> str:
    return SPACE.join(FORMAT_PLACEHOLDER if token is None else token for token in tokens)


# --- Python: argv literals through ast -------------------------------------


class ModuleConstants:
    """The module's top-level names bound to a string or a sequence of strings."""

    def __init__(self, module: ast.Module) -> None:
        self.nodes: dict[str, ast.expr] = {}
        for statement in module.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                self.nodes[statement.targets[0].id] = statement.value
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
                self.nodes[statement.target.id] = statement.value
        self.resolving: set[str] = set()
        # A loop variable bound to each element of a literal sequence in turn
        # (`for kind in KINDS:`), so the body is judged once per element: the
        # loop's first and last line, its variable and the elements. The
        # element in force while a literal is resolved sits in `bound`.
        self.loops: list[tuple[int, int, str, list[str]]] = []
        self.bound: dict[str, str] = {}
        for node in ast.walk(module):
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                values = self.sequence(node.iter)
                if values and all(isinstance(value, str) for value in values):
                    self.loops.append((node.lineno, node.end_lineno or node.lineno, node.target.id, values))

    def string(self, node: ast.expr) -> Token:
        """The string a node resolves to, or None."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            if node.id in self.bound:
                return self.bound[node.id]
            return self.named_string(node.id)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue) and (text := self.string(value.value)) is not None:
                    parts.append(text)
                else:
                    parts.append(FORMAT_PLACEHOLDER)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            # A side the check cannot read stands as `{}`, as in an f-string,
            # so the literal half of `"kubectl get ... " + rest` is still read.
            left, right = self.string(node.left), self.string(node.right)
            if left is None and right is None:
                return None
            return (FORMAT_PLACEHOLDER if left is None else left) + (FORMAT_PLACEHOLDER if right is None else right)
        return None

    def named_string(self, name: str) -> Token:
        node = self.nodes.get(name)
        if node is None or name in self.resolving:
            return None
        self.resolving.add(name)
        try:
            return self.string(node)
        finally:
            self.resolving.discard(name)

    def sequence(self, node: ast.expr) -> list[Token] | None:
        """The tokens a node resolves to as a sequence, or None when it is not one the check can read."""
        if isinstance(node, (ast.List, ast.Tuple)):
            tokens: list[Token] = []
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    tokens.extend(self.starred(element.value))
                else:
                    tokens.append(self.string(element))
            return tokens
        if isinstance(node, ast.Name):
            inner = self.nodes.get(node.id)
            if inner is None or node.id in self.resolving:
                return None
            self.resolving.add(node.id)
            try:
                return self.sequence(inner)
            finally:
                self.resolving.discard(node.id)
        return None

    def starred(self, node: ast.expr) -> list[Token]:
        """What a `*expr` splices: a known sequence's tokens, both branches of a conditional, else one unknown."""
        if isinstance(node, ast.IfExp):
            return self.starred(node.body) + self.starred(node.orelse)
        resolved = self.sequence(node)
        return [None] if resolved is None else resolved

    def bindings(self, node: ast.expr) -> Iterator[dict[str, str]]:
        """Each binding of the loop variable a literal names directly, from the innermost loop enclosing it.

        One empty binding when it names none; a loop variable that shadows a
        module constant is read as the constant. A list or tuple names its
        elements; an f-string the values it formats.
        """
        names = {element.id for element in direct_names(node) if element.id not in self.nodes}
        enclosing = [loop for loop in self.loops if loop[2] in names and loop[0] <= node.lineno <= loop[1]]
        if not enclosing:
            yield {}
            return
        _, _, name, values = max(enclosing, key=lambda loop: loop[0])
        for value in values:
            yield {name: value}


def direct_names(node: ast.expr) -> list[ast.Name]:
    """The names a list, tuple, f-string or `+` of strings refers to directly."""
    if isinstance(node, ast.Name):
        return [node]
    if isinstance(node, ast.BinOp):
        return direct_names(node.left) + direct_names(node.right)
    if isinstance(node, ast.JoinedStr):
        elements = [value.value for value in node.values if isinstance(value, ast.FormattedValue)]
    else:
        elements = list(getattr(node, "elts", ()))
    return [element for element in elements if isinstance(element, ast.Name)]


def is_string_expression(node: ast.AST) -> bool:
    """A string literal, an f-string, or a `+` of them: text a shell line may be written as."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return is_string_expression(node.left) or is_string_expression(node.right)
    return isinstance(node, ast.JoinedStr)


def docstring_ids(module: ast.Module) -> set[int]:
    """The ids of the string constants that are docstrings."""
    ids = set()
    for node in ast.walk(module):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            ids.add(id(body[0].value))
    return ids


def python_findings(path: Path, text: str) -> list[Finding]:
    try:
        module = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    constants = ModuleConstants(module)
    docstrings = docstring_ids(module)
    # The fragments of an f-string or a concatenation already read as one
    # text are not read again on their own.
    read_as_text: set[int] = set()
    findings = []
    for node in ast.walk(module):
        if id(node) in read_as_text:
            continue
        if is_string_expression(node) and id(node) not in docstrings:
            read_as_text.update(id(inner) for inner in ast.walk(node))
            for binding in constants.bindings(node):
                constants.bound = binding
                text = constants.string(node)
                constants.bound = {}
                if text is None or KUBECTL not in text:
                    continue
                for offset, line in enumerate(text.splitlines()):
                    for argv in commands(line):
                        rule = judge(argv, python_argv=False)
                        if rule is not None:
                            findings.append(Finding(path, node.lineno + offset, rule, render(argv)))
            continue
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        for binding in constants.bindings(node):
            constants.bound = binding
            tokens = constants.sequence(node)
            constants.bound = {}
            if tokens is None:
                continue
            rule = judge(tokens, python_argv=True)
            if rule is not None:
                findings.append(Finding(path, node.lineno, rule, render(tokens)))
    # `ast.walk` is breadth-first; the report reads in line order.
    return sorted(findings, key=lambda finding: finding.line)


# --- shell lines and Markdown code -----------------------------------------


def shell_tokens(line: str) -> list[str]:
    line = PLACEHOLDER_RE.sub(lambda match: PLACEHOLDER_JOIN.join(match.group(1).split()), line)
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def takes_next_token(token: Token) -> bool:
    """A flag written without `=` whose value is the token after it."""
    return is_flag(token) and FLAG_VALUE_SEPARATOR not in token and flag_name(token) in FLAGS_WITH_VALUE


def commands(line: str) -> Iterator[list[Token]]:
    """Each simple command on a shell line, as its tokens.

    A quoted word that holds a whole command (`bash -c "kubectl ..."`, a
    JSON example's `"command": "kubectl ..."`) is read as a line of its own
    as well as standing in the command it is an argument of.
    """
    current: list[Token] = []
    for token in shell_tokens(line):
        if token and set(token) <= SHELL_PUNCTUATION:
            if (
                current
                and token[0] in REDIRECT_CHARS
                and current[-1].isdecimal()
                and not (len(current) > 1 and takes_next_token(current[-2]))
            ):
                current.pop()
            if current:
                yield current
            current = []
        else:
            if KUBECTL in token and QUOTED_COMMAND_RE.search(token):
                yield from commands(token)
            current.append(token)
    if current:
        yield current


def joined_lines(lines: Iterable[tuple[int, str]]) -> Iterator[tuple[int, str]]:
    """(first line number, text) with backslash continuations joined onto the line that opened them."""
    start: int | None = None
    buffer: list[str] = []
    for number, line in lines:
        stripped = line.rstrip()
        if stripped.endswith(SHELL_CONTINUATION):
            if start is None:
                start = number
            buffer.append(stripped[: -len(SHELL_CONTINUATION)])
            continue
        if start is not None:
            buffer.append(line)
            yield start, SPACE.join(buffer)
            start, buffer = None, []
        else:
            yield number, line
    if start is not None:
        yield start, SPACE.join(buffer)


def shell_line_findings(path: Path, lines: Iterable[tuple[int, str]]) -> list[Finding]:
    findings = []
    for number, line in joined_lines(lines):
        if line.lstrip().startswith(SHELL_COMMENT_PREFIX):
            continue
        for argv in commands(line):
            rule = judge(argv, python_argv=False)
            if rule is not None:
                findings.append(Finding(path, number, rule, render(argv)))
    return findings


def shell_findings(path: Path, text: str) -> list[Finding]:
    return shell_line_findings(path, enumerate(text.splitlines(), start=1))


def markdown_code(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """(fenced lines, code spans) of a Markdown page, each with the line it starts on."""
    fenced: list[tuple[int, str]] = []
    prose: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        (fenced if in_fence else prose).append((number, line))
    spans: list[tuple[int, str]] = []
    joined = LINE_BREAK.join(line for _, line in prose)
    offsets = [0]
    for _, line in prose:
        offsets.append(offsets[-1] + len(line) + len(LINE_BREAK))
    for match in CODE_SPAN_RE.finditer(joined):
        index = max(i for i, offset in enumerate(offsets) if offset <= match.start())
        spans.append((prose[index][0], match.group("code").replace(LINE_BREAK, SPACE)))
    return fenced, spans


def markdown_findings(path: Path, text: str) -> list[Finding]:
    fenced, spans = markdown_code(text)
    return shell_line_findings(path, fenced) + shell_line_findings(path, spans)


# --- the tree and the allow-list -------------------------------------------


def scanned_files(root: Path = SCAN_ROOT) -> list[Path]:
    """Every file under the root the check reads, in path order."""
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(SKIP_PATH_PREFIXES) or TESTDATA_DIR in path.relative_to(root).parts:
            continue
        if path.suffix == PYTHON_SUFFIX and path.name.startswith(TEST_FILE_PREFIX):
            continue
        files.append(path)
    return files


def file_findings(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == PYTHON_SUFFIX:
        return python_findings(path, text)
    if path.suffix == SHELL_SUFFIX:
        return shell_findings(path, text)
    return markdown_findings(path, text)


def display_path(path: Path, base: Path = REPO) -> str:
    """The path as the report and the allow-list write it: relative to the scanned tree's parent when under it."""
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def list_file_lines(path: Path) -> Iterator[tuple[int, str]]:
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith(LIST_FILE_COMMENT_PREFIX):
            yield number, line


def load_allowlist(path: Path, scanned: set[str], base: Path = REPO) -> tuple[list[AllowlistEntry], list[str]]:
    """Every entry of the allow-list, and the lines that are not one."""
    entries: list[AllowlistEntry] = []
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    shown = display_path(path, base)
    for number, line in list_file_lines(path):
        fields = line.split(None, ALLOWLIST_FIELDS - 1)
        count = int(fields[2]) if len(fields) == ALLOWLIST_FIELDS and fields[2].isdecimal() else 0
        if count < 1:
            errors.append(ALLOWLIST_MALFORMED_ERROR.format(allowlist=shown, line=number, text=line))
            continue
        entry_path, rule = fields[0], fields[1]
        if rule not in RULES:
            errors.append(ALLOWLIST_UNKNOWN_RULE_ERROR.format(allowlist=shown, line=number, rule=rule, rules=", ".join(RULES)))
            continue
        if (entry_path, rule) in seen:
            errors.append(ALLOWLIST_DUPLICATE_ERROR.format(allowlist=shown, line=number, path=entry_path, rule=rule, first=seen[(entry_path, rule)]))
            continue
        seen[(entry_path, rule)] = number
        if entry_path not in scanned:
            errors.append(ALLOWLIST_MISSING_PATH_ERROR.format(allowlist=shown, line=number, path=entry_path))
            continue
        entries.append(AllowlistEntry(entry_path, rule, count, number))
    return entries, errors


def counts_by_path_rule(findings: Iterable[Finding], base: Path) -> dict[tuple[str, str], list[Finding]]:
    grouped: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        grouped.setdefault((display_path(finding.path, base), finding.rule), []).append(finding)
    return grouped


def check(root: Path = SCAN_ROOT, allowlist: Path = ALLOWLIST) -> tuple[list[str], int, int]:
    """(errors, files read, pinned findings) for the tree under the root against the allow-list.

    Paths are written relative to the root's parent, so the tree `agents/`
    reads as `agents/...` whether it is the repository's or a fixture's.
    """
    base = root.parent
    files = scanned_files(root)
    if not files:
        return [NO_FILES_ERROR.format(root=display_path(root, base))], 0, 0
    findings: list[Finding] = []
    for path in files:
        findings.extend(file_findings(path))
    grouped = counts_by_path_rule(findings, base)
    entries, errors = load_allowlist(allowlist, {display_path(path, base) for path in files}, base)
    shown = display_path(allowlist, base)
    pinned = 0
    covered: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.path, entry.rule)
        covered.add(key)
        found = grouped.get(key, [])
        context = dict(path=entry.path, rule=entry.rule, allowlist=shown, line=entry.line, allowed=entry.count, found=len(found))
        if not found:
            errors.append(ALLOWLIST_CLEAN_ERROR.format(**context))
        elif len(found) > entry.count:
            errors.append(ALLOWLIST_RISE_ERROR.format(**context))
            errors.extend(finding_lines(found, base))
        elif len(found) < entry.count:
            errors.append(ALLOWLIST_DROP_ERROR.format(**context))
        else:
            pinned += len(found)
    for key in sorted(grouped):
        if key in covered:
            continue
        found = grouped[key]
        errors.append(UNPINNED_ERROR.format(path=key[0], rule=key[1], found=len(found)))
        errors.extend(finding_lines(found, base))
    return errors, len(files), pinned


def finding_lines(found: list[Finding], base: Path) -> list[str]:
    lines = [FINDING_LINE.format(path=display_path(f.path, base), line=f.line, rule=f.rule, text=f.text) for f in found]
    lines.append(HINT_LINE.format(hint=RULE_HINTS[found[0].rule]))
    return lines


def count_lines(root: Path = SCAN_ROOT) -> list[str]:
    """`<path> <rule> <count>` for every (file, rule) with findings: the allow-list's first three fields."""
    findings: list[Finding] = []
    for path in scanned_files(root):
        findings.extend(file_findings(path))
    grouped = counts_by_path_rule(findings, root.parent)
    return [COUNT_LINE.format(path=path, rule=rule, count=len(found)) for (path, rule), found in sorted(grouped.items())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=SCAN_ROOT, help="the tree to read (default: agents/)")
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST, help="the pinned findings (default: scripts/cluster_reads_allowlist.txt)")
    parser.add_argument("--counts", action="store_true", help="print `<path> <rule> <count>` per finding group instead of checking")
    args = parser.parse_args(argv)
    if args.counts:
        for line in count_lines(args.root):
            print(line)
        return 0
    errors, files, pinned = check(args.root, args.allowlist)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(OK_MESSAGE.format(files=files, pinned=pinned))
    return 0


if __name__ == "__main__":
    sys.exit(main())
