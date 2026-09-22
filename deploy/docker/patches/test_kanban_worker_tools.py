"""Unit tests for the worker-only kanban gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from apply_kanban_worker_tools import (
    HANDLERS,
    IMPORT_AFTER,
    ORCHESTRATOR_CHECK_FN,
    RELATIVE,
    UPSTREAM_CHECK_FN,
    WORKER_CHECK_FN,
    apply,
    check_handler_mapping,
)
from kanban_worker_tools import WORKER_ONLY_TOOLS, check_kanban_worker_mode

# Tools an orchestrator profile keeps — the surface agents/chat/SOUL.md §1.5
# permits the front door: create, read, route, comment, unblock.
ORCHESTRATOR_TOOLS = (
    "kanban_show",
    "kanban_comment",
    "kanban_create",
)

# Reproduces the shape of upstream tools/kanban_tools.py (v2026.9.14) closely
# enough that the locators have to be right: the two gate functions, then the
# table-driven registration — a ``_TOOLS`` tuple of rows and one ``for`` loop
# that picks a gate per row and calls ``registry.register`` once.
GATES = '''\
import os

from tools.registry import no_cache_check_fn, registry, tool_error


def _profile_has_kanban_toolset() -> bool:
    return False


@no_cache_check_fn
def _check_kanban_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


@no_cache_check_fn
def _check_kanban_orchestrator_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()
'''

ORCHESTRATOR_SET_LINE = '_ORCHESTRATOR_TOOLS = frozenset({"kanban_list", "kanban_unblock"})\n'

LOOP = '''\
for _name, _sch, _handler, _emoji in _TOOLS:
    _gate = _check_kanban_orchestrator_mode if _name in _ORCHESTRATOR_TOOLS else _check_kanban_mode
    registry.register(name=_name, toolset="kanban", schema=_sch, handler=_handler, emoji=_emoji,
                      check_fn=_gate)
'''

#: The statement the applier splices after the loop's ``_gate = ...`` line.
GATE_OVERRIDE = (
    "    if _name in _WORKER_ONLY_TOOLS:\n"
    f"        _gate = {WORKER_CHECK_FN}\n"
)

ALL_TOOLS = ("kanban_list", "kanban_unblock") + ORCHESTRATOR_TOOLS + WORKER_ONLY_TOOLS


def row(tool, handler=None, schema=None):
    handler = handler or HANDLERS.get(tool, f"_handle_{tool[len('kanban_'):]}")
    schema = schema or f"{tool.upper()}_SCHEMA"
    return f'    ("{tool}", {schema}, {handler}, "x"),\n'


def upstream_source(rows=None, orchestrator_set=ORCHESTRATOR_SET_LINE, loop=LOOP):
    rows = [row(tool) for tool in ALL_TOOLS] if rows is None else rows
    return GATES + "\n\n" + orchestrator_set + "_TOOLS = (\n" + "".join(rows) + ")\n\n" + loop


def patch_tree(source):
    """Write ``source`` as tools/kanban_tools.py under a temp root and patch it."""
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


def registered_gates(patched):
    """Execute the patched fixture against a recording registry: tool -> check_fn.

    An anchored edit that parses can still leave the loop choosing the wrong
    gate; the only proof is what the loop hands ``registry.register``. The two
    ``tools.*`` modules the fixture and the import block name are faked in
    ``sys.modules``, the way the delegation-context tests fake theirs.
    """
    gates = {}

    class Registry:
        @staticmethod
        def register(**kw):
            gates[kw["name"]] = kw["check_fn"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.registry = Registry()
    registry_mod.no_cache_check_fn = lambda fn: fn
    registry_mod.tool_error = lambda m: m
    worker_mod = types.ModuleType("tools.kanban_worker_tools")
    worker_mod.WORKER_ONLY_TOOLS = WORKER_ONLY_TOOLS
    worker_mod.check_kanban_worker_mode = check_kanban_worker_mode
    tools_pkg = types.ModuleType("tools")
    tools_pkg.registry = registry_mod
    tools_pkg.kanban_worker_tools = worker_mod
    ns = {f"{tool.upper()}_SCHEMA": {"name": tool} for tool in ALL_TOOLS}
    ns.update({HANDLERS.get(t, f"_handle_{t[len('kanban_'):]}"): (lambda a, **k: a) for t in ALL_TOOLS})
    with mock.patch.dict(
        sys.modules,
        {"tools": tools_pkg, "tools.registry": registry_mod,
         "tools.kanban_worker_tools": worker_mod},
    ):
        exec(compile(patched, "<patched>", "exec"), ns)
    return gates, ns


class CheckWorkerModeTest(unittest.TestCase):
    def test_true_only_inside_a_dispatched_run(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
            self.assertTrue(check_kanban_worker_mode())

    def test_false_for_an_orchestrator_profile(self):
        env = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(check_kanban_worker_mode())

    def test_an_empty_task_id_is_not_a_worker(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": ""}):
            self.assertFalse(check_kanban_worker_mode())

    def test_the_forbidden_six_are_all_covered(self):
        # The six agents/chat/SOUL.md §1.5 names explicitly. If a future edit
        # trims WORKER_ONLY_TOOLS, the prose and the schema set diverge again.
        for tool in (
            "kanban_complete", "kanban_block", "kanban_heartbeat", "kanban_link",
            "kanban_request_review", "kanban_request_changes",
        ):
            self.assertIn(tool, WORKER_ONLY_TOOLS)

    def test_the_review_flow_tools_are_worker_only(self):
        # v2026.9.14 opens both with _worker_guard, whose ownership check is a
        # no-op without HERMES_KANBAN_TASK — so an orchestrator offered the
        # schema could call either against any card.
        self.assertIn("kanban_request_review", WORKER_ONLY_TOOLS)
        self.assertIn("kanban_request_changes", WORKER_ONLY_TOOLS)
        self.assertEqual(len(WORKER_ONLY_TOOLS), 9)


def with_delegation_context(reader):
    """Patch a fake ``agent.delegation_context`` whose reader is *reader*.

    ``kanban_ownership`` imports it lazily per call, so a fake in ``sys.modules``
    is enough to exercise the branch on a host with no Hermes. The polarities
    themselves live in test_kanban_ownership.py; what is asserted here is that
    this gate asks for the right one.
    """
    fake = types.ModuleType("agent.delegation_context")
    fake.is_delegated_child_context = reader
    agent_pkg = types.ModuleType("agent")
    agent_pkg.delegation_context = fake
    return mock.patch.dict(
        sys.modules, {"agent": agent_pkg, "agent.delegation_context": fake}
    )


class DelegatedChildTest(unittest.TestCase):
    """A delegate_task child inherits ``HERMES_KANBAN_TASK`` and owns no card.

    The child runs ``run_conversation`` in the parent's own process, so the env
    var this gate keys off is the parent's and proves nothing about the child.
    Upstream's own two gates, ``_check_kanban_mode`` and
    ``_check_kanban_orchestrator_mode``, both open with the same short-circuit;
    without it this was the only kanban gate in the file that said *True* for a
    child, which would have offered it the nine worker-only tools and none of
    the five an orchestrator keeps.
    """

    def test_a_delegated_child_is_not_a_worker(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
            with with_delegation_context(lambda: True):
                self.assertFalse(check_kanban_worker_mode())

    def test_the_parent_worker_still_keeps_its_tools(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
            with with_delegation_context(lambda: False):
                self.assertTrue(check_kanban_worker_mode())

    def test_it_consults_the_real_delegation_context(self):
        """The gate takes no argument, so the module import is the only seam.

        ``check_fn`` is called with no arguments by ``tools/registry.py``, which
        means a short-circuit that did not reach ``agent.delegation_context``
        itself would be inert in the only place it matters.
        """
        module = importlib.import_module("kanban_worker_tools")
        with with_delegation_context(lambda: True):
            self.assertTrue(module.is_delegated_child(on_unknown=False))

    def test_a_host_without_hermes_is_not_a_delegated_child(self):
        """The import fails outside the image; that is not evidence of a child.

        Answering True there would hide ``kanban_complete`` and ``kanban_block``
        from every dispatcher-spawned worker in the image at once, and a worker
        with no terminal tool cannot end its run.
        """
        module = importlib.import_module("kanban_worker_tools")
        with mock.patch.dict(sys.modules, {"agent.delegation_context": None}):
            self.assertFalse(module.is_delegated_child(on_unknown=False))
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
                self.assertTrue(check_kanban_worker_mode())

    def test_a_raising_reader_leaves_the_worker_its_tools(self):
        """Uncertainty must not strand a card.

        This gate asks ``kanban_ownership.is_delegated_child`` with
        ``on_unknown=False``, the opposite of what
        ``kanban_guardrail_exit.should_record_missing_terminal`` asks for, whose
        wrong answer writes to the board. This one only chooses which schemas
        ship, and ``_reject_delegated_child_mutation`` still refuses a child's
        mutations.
        """
        module = importlib.import_module("kanban_worker_tools")

        def boom():
            raise RuntimeError("no delegation context")

        with with_delegation_context(boom):
            self.assertFalse(module.is_delegated_child(on_unknown=False))
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_c31a1f00"}):
                self.assertTrue(check_kanban_worker_mode())

    def test_a_child_of_an_orchestrator_is_not_a_worker_either(self):
        # No HERMES_KANBAN_TASK at all: the Chat Agent delegating to a
        # specialist. The gate was already False here; the short-circuit must
        # not have turned it into a way in.
        env = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}
        with mock.patch.dict(os.environ, env, clear=True):
            with with_delegation_context(lambda: True):
                self.assertFalse(check_kanban_worker_mode())


class ApplyTest(unittest.TestCase):
    def test_worker_only_tools_are_regated(self):
        gates, _ = registered_gates(patch_tree(upstream_source()))
        for tool in WORKER_ONLY_TOOLS:
            self.assertIs(gates[tool], check_kanban_worker_mode, f"{tool} was not re-gated")

    def test_orchestrator_tools_are_left_alone(self):
        gates, ns = registered_gates(patch_tree(upstream_source()))
        for tool in ORCHESTRATOR_TOOLS:
            self.assertIs(gates[tool], ns[UPSTREAM_CHECK_FN], tool)
        for tool in ("kanban_list", "kanban_unblock"):
            self.assertIs(gates[tool], ns[ORCHESTRATOR_CHECK_FN], tool)

    def test_every_upstream_tool_is_still_registered(self):
        gates, _ = registered_gates(patch_tree(upstream_source()))
        self.assertEqual(set(gates), set(ALL_TOOLS))

    def test_the_override_lands_once_inside_the_loop(self):
        patched = patch_tree(upstream_source())
        self.assertEqual(patched.count(GATE_OVERRIDE), 1)
        choice = patched.index(f"_gate = {ORCHESTRATOR_CHECK_FN} if")
        override = patched.index(GATE_OVERRIDE)
        register = patched.index("registry.register(")
        self.assertTrue(choice < override < register)

    def test_the_import_lands_above_the_loop(self):
        patched = patch_tree(upstream_source())
        import_at = patched.index("from tools.kanban_worker_tools import")
        first_use = patched.index(f"_gate = {WORKER_CHECK_FN}")
        # _gate is evaluated at import time, so a trailing import would
        # NameError at module load rather than at first tool call.
        self.assertLess(import_at, first_use)
        self.assertIn(f"no_cache_check_fn({WORKER_CHECK_FN})", patched)

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(upstream_source()))

    def test_reformatting_the_registration_no_longer_breaks_the_build(self):
        """The point of locating by AST: layout is not the contract.

        The table rows and the gate conditional are reflowed onto several
        lines each, with a comment in between; a literal anchor on either
        would have found nothing, and the patch must not care.
        """
        reflowed_rows = [
            f'    (\n        "{tool}",  # a comment upstream added\n'
            f"        {tool.upper()}_SCHEMA,\n"
            f"        {HANDLERS.get(tool, '_handle_' + tool[len('kanban_'):])},\n"
            '        "x",\n    ),\n'
            for tool in ALL_TOOLS
        ]
        reflowed_loop = LOOP.replace(
            f"    _gate = {ORCHESTRATOR_CHECK_FN} if _name in _ORCHESTRATOR_TOOLS else {UPSTREAM_CHECK_FN}\n",
            f"    _gate = (\n        {ORCHESTRATOR_CHECK_FN}\n"
            f"        if _name in _ORCHESTRATOR_TOOLS\n        else {UPSTREAM_CHECK_FN}\n    )\n",
        )
        patched = patch_tree(upstream_source(rows=reflowed_rows, loop=reflowed_loop))
        self.assertEqual(patched.count(GATE_OVERRIDE), 1)
        gates, _ = registered_gates(patched)
        for tool in WORKER_ONLY_TOOLS:
            self.assertIs(gates[tool], check_kanban_worker_mode, tool)

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(upstream_source())
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))

    def test_every_worker_only_tool_has_a_handler_mapping(self):
        self.assertEqual(set(WORKER_ONLY_TOOLS) - set(HANDLERS), set())
        check_handler_mapping()


class LocatorFailureTest(unittest.TestCase):
    """An AST locator that matched the wrong node would be worse than an anchor.

    A literal anchor fails loudly by construction — the text is either there or
    it is not. A locator has to be *made* to fail loudly, so each way upstream
    could move the table, a row, or the loop's gate choice out from under this
    patch gets a test.
    """

    def assert_refuses(self, source, *expected):
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(source)
        for fragment in expected:
            self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def rows_without(self, tool):
        return [row(t) for t in ALL_TOOLS if t != tool]

    def test_an_absent_row_fails_loudly(self):
        source = upstream_source(rows=self.rows_without("kanban_link"))
        self.assert_refuses(source, "_TOOLS row for kanban_link", "found 0")

    def test_a_duplicated_row_fails_loudly(self):
        """Two rows registering the same tool: which one is the patch for?"""
        source = upstream_source(rows=[row(t) for t in ALL_TOOLS] + [row("kanban_link")])
        self.assert_refuses(source, "_TOOLS row for kanban_link", "found 2")

    def test_a_renamed_tool_fails_loudly(self):
        source = upstream_source().replace('("kanban_attach",', '("kanban_file",')
        self.assert_refuses(source, "_TOOLS row for kanban_attach", "found 0")

    def test_a_renamed_handler_fails_loudly(self):
        """Found the row, but it is no longer wired to what we expected."""
        source = upstream_source().replace(
            row("kanban_complete"), row("kanban_complete", handler="_handle_finish")
        )
        self.assert_refuses(source, "kanban_complete row", "_handle_finish", "_handle_complete")

    def test_a_renamed_schema_fails_loudly(self):
        source = upstream_source().replace(
            row("kanban_block"), row("kanban_block", schema="KANBAN_PAUSE_SCHEMA")
        )
        self.assert_refuses(source, "kanban_block row", "KANBAN_PAUSE_SCHEMA", "KANBAN_BLOCK_SCHEMA")

    def test_a_table_that_is_not_a_tuple_fails_loudly(self):
        source = upstream_source().replace("_TOOLS = (\n", "_TOOLS = [\n").replace(")\n\nfor ", "]\n\nfor ")
        self.assert_refuses(source, "_TOOLS is", "not a tuple of rows")

    def test_upstream_routing_a_worker_tool_as_orchestrator_only_fails_loudly(self):
        """If upstream already hides a tool from workers, this patch's premise is gone."""
        source = upstream_source(
            orchestrator_set='_ORCHESTRATOR_TOOLS = frozenset({"kanban_list", "kanban_unblock", "kanban_link"})\n'
        )
        self.assert_refuses(source, "_ORCHESTRATOR_TOOLS now lists kanban_link")

    def test_a_renamed_gate_fails_loudly(self):
        """The check_fn upstream ships is asserted, not merely overridden."""
        source = upstream_source(loop=LOOP.replace(UPSTREAM_CHECK_FN + "\n", "_check_kanban_task_mode\n"))
        self.assert_refuses(source, "_gate is", "_check_kanban_task_mode", UPSTREAM_CHECK_FN)

    def test_a_renamed_loop_variable_fails_loudly(self):
        """Both arms intact, but the choice reads a loop variable the override does not."""
        source = upstream_source(loop=LOOP.replace("_name", "_tool"))
        self.assert_refuses(
            source, "_gate chooses on", "_tool in _ORCHESTRATOR_TOOLS", "'_name in ...'"
        )

    def test_a_gate_that_is_no_longer_a_choice_fails_loudly(self):
        source = upstream_source(loop=LOOP.replace(
            f"    _gate = {ORCHESTRATOR_CHECK_FN} if _name in _ORCHESTRATOR_TOOLS else {UPSTREAM_CHECK_FN}\n",
            f"    _gate = {UPSTREAM_CHECK_FN}\n",
        ))
        self.assert_refuses(source, "_gate is", "was expected")

    def test_a_second_gate_assignment_fails_loudly(self):
        """Two ``_gate = ...`` statements: the override would land after only one."""
        source = upstream_source(loop=LOOP + f"\n_gate = {UPSTREAM_CHECK_FN}\n")
        self.assert_refuses(source, "assignment to _gate", "found 2")

    def test_an_absent_import_site_fails_loudly(self):
        source = upstream_source().replace(f"def {IMPORT_AFTER}()", "def _check_other()")
        self.assert_refuses(source, "worker-gate import site", "found 0")

    def test_a_duplicated_import_site_fails_loudly(self):
        source = upstream_source() + (
            f"\n\ndef {IMPORT_AFTER}() -> bool:\n    return False\n"
        )
        self.assert_refuses(source, "worker-gate import site", "found 2")

    def test_nothing_is_written_when_a_locator_refuses(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        source = upstream_source().replace('("kanban_attach",', '("kanban_file",')
        target.write_text(source)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), source, "a refused run must not write")


if __name__ == "__main__":
    unittest.main()
