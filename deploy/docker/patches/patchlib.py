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
**Literal anchors** (:meth:`Patch.substitute`, :meth:`Patch.substitute_all`) pin
an edit to an exact slice of upstream source. They are precise and they fail
loudly, but every one of them is a separate way that a base-image bump breaks the
build, so the number of them is the cost metric this directory is managed
against.

**AST locators** (:meth:`Patch.find_call`, :meth:`Patch.find_def`,
:meth:`Patch.find_assign`) pin an edit to a *node* instead — the
``registry.register`` call that registers a named tool, the ``def`` whose
signature gains a parameter or whose tail an import has to land after, the
constant tuple a filter is spelled as. Reformatting upstream, adding a keyword
argument, or appending an element moves the text but not the node, so a locator
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

    def expect_keyword_only(self, *names: str) -> None:
        """Assert the signature still declares each keyword-only parameter.

        The half of a signature anchor worth keeping, in the same sense as
        :meth:`CallSite.expect`. A patch that adds a parameter of its own is
        entitled to fail when the parameters it reasons about have gone; it is
        not entitled to fail because upstream added one, which is what the
        literal signature anchor this replaces did on every base-image bump.
        """
        present = {argument.arg for argument in self.node.args.kwonlyargs}
        missing = [name for name in names if name not in present]
        if missing:
            raise self.patch._fail(
                f"the {self.label} def {self.node.name}() no longer takes "
                f"keyword-only {', '.join(missing)}. {self.patch.note}"
            )

    def keyword_only_end(self) -> int:
        """Offset just past the last keyword-only parameter, for adding one.

        Splicing ``", name=default"`` here appends a parameter without
        respelling the signature, so wrapping it onto three lines — which
        v2026.8.13 did to ``cron.scheduler.run_one_job`` — moves the offset
        instead of breaking the edit. A trailing comma after the last
        parameter is fine: the insert lands in front of it.
        """
        arguments = self.node.args
        if not arguments.kwonlyargs:
            raise self.patch._fail(
                f"the {self.label} def {self.node.name}() takes no "
                f"keyword-only parameters, so there is nowhere to add one. "
                f"{self.patch.note}"
            )
        # The default outruns the parameter it belongs to (``verbose: bool =
        # False`` ends at False, not at bool), and kw_defaults holds None for
        # a keyword-only parameter that has no default.
        last = arguments.kw_defaults[-1] or arguments.kwonlyargs[-1]
        starts = _line_starts(self.patch.source)
        return _offset(
            self.patch.source, starts, last.end_lineno, last.end_col_offset
        )


class Assignment(_Site):
    """A single-target ``NAME = ...`` located by name, at any nesting depth.

    Unlike :class:`Definition` and :class:`CallSite` this looks inside function
    bodies, because the constants worth pinning are not all module-level: the
    kind filter the kanban notifier claims events with is a local in the method
    that uses it. The uniqueness check is what keeps that honest — a name
    assigned in two places is refused rather than guessed at.
    """

    def __init__(self, patch: "Patch", node: ast.Assign, label: str) -> None:
        super().__init__(patch, node, label)
        self.node: ast.Assign = node
        starts = _line_starts(patch.source)
        #: Offsets of the right-hand side alone, for rewriting the value while
        #: leaving the target, the indent and any comment above it untouched.
        self.value_start = _offset(
            patch.source, starts, node.value.lineno, node.value.col_offset
        )
        self.value_end = _offset(
            patch.source, starts, node.value.end_lineno, node.value.end_col_offset
        )
        #: Start of the line the assignment begins on, for inserting a comment
        #: ahead of it at the statement's own indentation.
        self.line_start = starts[node.lineno]
        #: The assignment's own indentation, as literal text.
        self.indent = " " * node.col_offset

    @property
    def value_text(self) -> str:
        """The right-hand side exactly as it is spelled in the source."""
        return self.patch.source[self.value_start : self.value_end]

    def expect_contains(self, *values: object) -> None:
        """Assert the value is a tuple/list/set literal holding each of ``values``.

        This is the half of a literal anchor worth keeping, in the same sense as
        :meth:`CallSite.expect`. The five-element anchor this replaces was not
        only *finding* the tuple, it was asserting the tuple still held the
        kinds the patch reasons about; a locator that skipped that would happily
        widen a filter upstream had repurposed underneath us. What it
        deliberately does not assert is the *absence* of anything else, which is
        precisely the churn — a new upstream kind — that used to break the build
        for no reason.
        """
        node = self.node.value
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            raise self.patch._fail(
                f"the {self.label} is {_render(node)}, not a tuple/list/set "
                f"literal. {self.patch.note}"
            )
        present = {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant)
        }
        missing = [value for value in values if value not in present]
        if missing:
            raise self.patch._fail(
                f"the {self.label} no longer holds "
                f"{', '.join(repr(value) for value in missing)}. "
                f"{self.patch.note}"
            )


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

    def substitute_all(
        self, anchor: str, replacement: str, *, label: str, at_least: int = 1
    ) -> None:
        """Replace every occurrence, requiring at least ``at_least`` of them.

        For the anchor whose *number* of occurrences is upstream's business
        rather than this patch's. ``tools/kanban_tools.py`` emits the same
        "task_id is required" message once per Kanban lifecycle tool, and
        v2026.8.13 shipped two more tools; an exact count there fails the build
        to announce that upstream grew a feature, which is precisely the noise
        the AST locators exist to remove. Finding the anchor nowhere at all
        still means the edit would silently not happen, so the floor is checked.
        """
        found = self.source.count(anchor)
        if found < at_least:
            raise self._fail(
                f"expected at least {at_least} occurrence(s) of the {label} "
                f"anchor, found {found}. {self.note}\n--- anchor ---\n{anchor}"
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

    def find_assign(self, name: str, *, label: str) -> Assignment:
        """Locate the one ``name = ...`` anywhere in the file.

        ``ast.walk`` rather than ``tree.body``: the constants these patches
        widen live inside the method that reads them, not at module level.
        Augmented and annotated assignments are deliberately not matched — a
        patch that means to rewrite a value should fail rather than splice into
        a ``name += ...`` it was not derived against.
        """
        found = [
            node
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ]
        if len(found) != 1:
            raise self._fail(
                f"expected 1 assignment to {name} for the {label}, found "
                f"{len(found)}. {self.note}"
            )
        return Assignment(self, found[0], label)

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
                # compile(), not ast.parse(). ast.parse accepts a ``continue``
                # outside a loop and only the compile step rejects it, so a
                # branch spliced one indent level out of its ``for`` would parse
                # here and fail at import, inside the running gateway.
                # apply_kanban_progress_lines.py inserts exactly such a branch.
                compile(self.source, self.relative, "exec")
            except SyntaxError as e:
                raise SystemExit(
                    f"{self.prefix} patch: {self.relative} no longer parses "
                    f"after patching: {e}"
                )
        self.path.write_text(self.source)
        print(f"{self.prefix} patch: {self.relative} ({summary})")
