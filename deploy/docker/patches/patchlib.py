#!/usr/bin/env python3
"""Shared machinery for the anchored source rewrites in this directory.

Every ``apply_*.py`` here does the same four things to a file in the Hermes
source tree: read it, assert that a literal anchor occurs the exact number of
times expected, substitute, and refuse to write anything that no longer parses.
Thirteen appliers had thirteen copies of that loop, which meant thirteen places
for the invariant to drift — and the invariant is the entire safety story. A
patch that silently matches nothing ships an image whose behaviour is not the
one the config describes, and the only thing standing between us and that is
the exact-count check. It belongs in one place.

This module is **build-time only**. It is never installed into the image: the
Dockerfile stages each applier into ``/tmp`` and deletes it in the same ``RUN``
layer, and Python puts the running script's own directory at the front of
``sys.path``, so ``import patchlib`` resolves to a sibling copy staged next to
the applier. Two appliers already relied on that (``apply_kanban_worker_tools``
imports ``kanban_worker_tools``, ``apply_kanban_result_required`` imports
``kanban_result_required``); this is the same mechanism. Nothing inside the
running image imports it.

Two kinds of edit site
----------------------
**Literal anchors** (:meth:`Patch.substitute`) pin an edit to an exact slice of
upstream source. They are precise and they fail loudly, but every one of them is
a separate way that a base-image bump breaks the build, so the number of them is
the cost metric this directory is managed against.

**AST locators** (:meth:`Patch.find_call`, :meth:`Patch.find_def`) pin an edit to
a *node* instead — the ``registry.register`` call that registers a named tool,
the ``def`` whose tail an import has to land after. Reformatting upstream, or
adding a keyword argument, moves the text but not the node, so a locator
survives churn that a literal anchor does not.

An AST locator that silently matched the wrong node would be strictly worse than
a literal anchor that failed loudly, so every locator here asserts it matched
exactly one node, and :meth:`CallSite.expect` re-asserts the parts of the call
the literal anchor used to spell out. A locator that finds nothing, finds two
things, or finds something whose shape has changed raises ``SystemExit`` naming
the file, the site, and what it expected.

Nothing is written until :meth:`Patch.commit`, so an applier that fails halfway
through a file leaves that file untouched rather than half-patched.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The standard tail on an anchor-mismatch message. Says what broke (upstream
#: moved) and what to do about it (re-derive before bumping), because the person
#: reading it is looking at a red build and not at this directory.
DRIFT_NOTE = (
    "Upstream Hermes changed — re-derive the anchor before bumping the base image."
)


class Ident(str):
    """An expected keyword value that must be a bare name, not a string literal.

    ``expect(toolset="kanban")`` asserts the argument is the *string* ``kanban``;
    ``expect(handler=Ident("_handle_complete"))`` asserts it is the *name*
    ``_handle_complete``. Without the distinction a locator could not tell
    ``check_fn=_check_kanban_mode`` from ``check_fn="_check_kanban_mode"``.
    """


def _line_starts(source: str) -> list[int]:
    """Character offset of the first character of every 1-based line.

    Index ``lineno`` is the start of that line and index ``lineno + 1`` is the
    start of the next one, which is what an insert-after-a-node needs. Index 0
    is unused padding so the 1-based arithmetic reads straight.
    """
    starts = [0, 0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _offset(source: str, starts: list[int], lineno: int, col: int) -> int:
    """Translate an ``ast`` position into an index into ``source``.

    ``ast`` reports columns as UTF-8 *byte* counts and the source being spliced
    is a ``str``, so the two agree only while the line is ASCII. Every span the
    current locators return happens to sit on an ASCII line, so this is
    defensive rather than load-bearing today — but ``tools/kanban_tools.py``
    carries an ``emoji=`` argument in every registration this patch touches, and
    the next locator added has no way to know which lines are safe. Getting it
    wrong would not raise: it would splice three characters off target.
    """
    start = starts[lineno]
    line = source[start : starts[lineno + 1]]
    return start + len(line.encode("utf-8")[:col].decode("utf-8"))


def _render(node: ast.AST) -> str:
    """A short, readable rendering of what a node actually is, for error text."""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse handles every real node
        return type(node).__name__


def _matches(node: ast.AST, expected: object) -> bool:
    """True when ``node`` is the name or the literal that ``expected`` describes."""
    if isinstance(expected, Ident):
        return isinstance(node, ast.Name) and node.id == str(expected)
    return isinstance(node, ast.Constant) and node.value == expected


def _describe(expected: object) -> str:
    """How an expectation is spelled in a failure message."""
    return str(expected) if isinstance(expected, Ident) else repr(expected)


class _Site:
    """A located node, and the spans of ``patch.source`` it occupies."""

    def __init__(self, patch: "Patch", node: ast.AST, label: str) -> None:
        self.patch = patch
        self.node = node
        self.label = label
        starts = _line_starts(patch.source)
        self.start = _offset(patch.source, starts, node.lineno, node.col_offset)
        self.end = _offset(
            patch.source, starts, node.end_lineno, node.end_col_offset
        )
        #: Start of the line after the node — the insertion point for anything
        #: that has to follow a whole ``def`` rather than sit inside it.
        self.after = starts[node.end_lineno + 1]


class Definition(_Site):
    """A module-level ``def`` located by name."""


class CallSite(_Site):
    """A module-level call statement located by its callee and its arguments."""

    def __init__(
        self, patch: "Patch", stmt: ast.stmt, call: ast.Call, label: str
    ) -> None:
        super().__init__(patch, stmt, label)
        self.call = call

    def _keyword(self, name: str) -> ast.keyword:
        for keyword in self.call.keywords:
            if keyword.arg == name:
                return keyword
        raise self.patch._fail(
            f"the {self.label} sets no {name}= argument. {self.patch.note}"
        )

    def expect(self, **keywords: object) -> None:
        """Assert each named argument is the name or literal given.

        This is the half of a literal anchor worth keeping. The five-line anchor
        this replaces was not only *finding* the registration, it was asserting
        that the call still passed the schema and handler it was expected to;
        drop that and a locator would happily re-gate a tool upstream had
        rewired underneath us.
        """
        for name, expected in keywords.items():
            keyword = self._keyword(name)
            if not _matches(keyword.value, expected):
                raise self.patch._fail(
                    f"the {self.label} sets {name}={_render(keyword.value)} "
                    f"where {_describe(expected)} was expected. "
                    f"{self.patch.note}"
                )

    def keyword_span(self, name: str) -> tuple[int, int]:
        """Offsets of the *value* of a named argument, for a surgical splice."""
        value = self._keyword(name).value
        starts = _line_starts(self.patch.source)
        return (
            _offset(self.patch.source, starts, value.lineno, value.col_offset),
            _offset(
                self.patch.source, starts, value.end_lineno, value.end_col_offset
            ),
        )


class Patch:
    """One file on its way from upstream source to patched source.

    Holds the text in memory and writes it exactly once, in :meth:`commit`, so a
    run that fails on the third of four edits leaves the file as upstream shipped
    it. Every failure is a ``SystemExit`` naming the file, because the reader is
    looking at a Docker build log.
    """

    def __init__(
        self,
        root: Path,
        relative: str,
        *,
        prefix: str,
        note: str = DRIFT_NOTE,
    ) -> None:
        self.prefix = prefix
        self.relative = relative
        self.note = note
        self.path = Path(root) / relative
        if not self.path.is_file():
            raise SystemExit(f"{prefix} patch: {self.path} does not exist")
        self.source = self.path.read_text()

    def _fail(self, detail: str) -> SystemExit:
        return SystemExit(f"{self.prefix} patch: {self.relative}: {detail}")

    # -- guards ---------------------------------------------------------------

    def refuse_if_patched(self, *markers: str) -> None:
        """Refuse a second run, for patches whose anchors survive their own edit.

        An insert that sits *next to* its anchor rather than consuming it leaves
        the anchor count at one, so the count check cannot tell a fresh file from
        an already-patched one, and a second pass would stack a second copy of
        the insert. Each marker must be text that only exists after a successful
        run.
        """
        for marker in markers:
            if marker in self.source:
                raise SystemExit(
                    f"{self.prefix} patch: {self.relative} is already patched "
                    f"({marker!r} is present)"
                )

    # -- literal anchors ------------------------------------------------------

    def substitute(
        self,
        anchor: str,
        replacement: str,
        *,
        label: str | None = None,
        expected: int = 1,
    ) -> None:
        """Replace ``anchor`` exactly ``expected`` times, or fail the build.

        The count is the whole point: an anchor found zero times means upstream
        moved and the edit would silently not happen, and an anchor found twice
        means it is no longer pinning what it was derived against. Either way the
        image must not be built.
        """
        found = self.source.count(anchor)
        if found != expected:
            named = f"the {label} anchor" if label else "anchor"
            raise self._fail(
                f"expected {expected} occurrence(s) of {named}, found {found}. "
                f"{self.note}\n--- anchor ---\n{anchor}"
            )
        self.source = self.source.replace(anchor, replacement)

    def append(self, trailer: str) -> None:
        """Add text to the end of the file, for imports resolved at call time."""
        self.source += trailer

    # -- AST locators ---------------------------------------------------------

    def _tree(self) -> ast.Module:
        try:
            return ast.parse(self.source)
        except SyntaxError as e:
            raise self._fail(f"does not parse, so it cannot be located in: {e}")

    def find_def(self, name: str, *, label: str) -> Definition:
        """Locate the one module-level ``def`` called ``name``."""
        found = [
            node
            for node in self._tree().body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ]
        if len(found) != 1:
            raise self._fail(
                f"expected 1 module-level def {name}() for the {label}, found "
                f"{len(found)}. {self.note}"
            )
        return Definition(self, found[0], label)

    def find_call(self, callee: str, *, label: str, **select: object) -> CallSite:
        """Locate the one module-level call to ``callee`` matching ``select``.

        ``select`` narrows by keyword argument the way a human would read the
        block — ``find_call("registry.register", name="kanban_complete")`` is
        "the statement that registers kanban_complete", which stays true across
        any reformatting of it. Use :meth:`CallSite.expect` for the arguments
        that must be *asserted* rather than searched on, so that a renamed
        handler fails with "sets handler=X where Y was expected" instead of
        vanishing into a "found 0".
        """
        found = []
        for stmt in self._tree().body:
            if not isinstance(stmt, ast.Expr) or not isinstance(
                stmt.value, ast.Call
            ):
                continue
            call = stmt.value
            if _render(call.func) != callee:
                continue
            if all(
                any(
                    kw.arg == name and _matches(kw.value, expected)
                    for kw in call.keywords
                )
                for name, expected in select.items()
            ):
                found.append((stmt, call))
        if len(found) != 1:
            criteria = ", ".join(
                f"{name}={_describe(expected)}" for name, expected in select.items()
            )
            raise self._fail(
                f"expected 1 module-level {callee}({criteria}) call for the "
                f"{label}, found {len(found)}. {self.note}"
            )
        return CallSite(self, found[0][0], found[0][1], label)

    # -- splicing -------------------------------------------------------------

    def splice(self, start: int, end: int, text: str) -> None:
        """Replace ``source[start:end]``. Offsets come from a locator."""
        self.source = self.source[:start] + text + self.source[end:]

    def insert(self, offset: int, text: str) -> None:
        """Insert ``text`` at ``offset``. Offsets come from a locator."""
        self.source = self.source[:offset] + text + self.source[offset:]

    # -- commit ---------------------------------------------------------------

    def commit(self, summary: str, *, parse: bool = True) -> None:
        """Parse-check and write, then report the edit on stdout.

        ``parse=False`` is for the one non-Python target in this directory, the
        mcp-remote TypeScript source; everything else must still be importable
        Python before it is allowed to reach the image.
        """
        if parse:
            try:
                ast.parse(self.source)
            except SyntaxError as e:
                raise SystemExit(
                    f"{self.prefix} patch: {self.relative} no longer parses "
                    f"after patching: {e}"
                )
        self.path.write_text(self.source)
        print(f"{self.prefix} patch: {self.relative} ({summary})")
