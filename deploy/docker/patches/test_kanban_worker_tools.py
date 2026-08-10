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
    RELATIVE,
    UPSTREAM_CHECK_FN,
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

# Reproduces the shape of upstream tools/kanban_tools.py closely enough that the
# anchors have to be right: the two gate functions, then the registrations.
GATES = '''\
import os


def _profile_has_kanban_toolset() -> bool:
    return False


def _check_kanban_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()
'''


def registration(tool, check_fn, handler=None):
    handler = handler or HANDLERS.get(tool, f"_handle_{tool[len('kanban_'):]}")
    return (
        "\n\nregistry.register(\n"
        f'    name="{tool}",\n'
        '    toolset="kanban",\n'
        f"    schema={tool.upper()}_SCHEMA,\n"
        f"    handler={handler},\n"
        f"    check_fn={check_fn},\n"
        '    emoji="x",\n'
        ")\n"
    )


def upstream_source():
    src = GATES
    src += registration("kanban_list", "_check_kanban_orchestrator_mode")
    src += registration("kanban_unblock", "_check_kanban_orchestrator_mode")
    for tool in ORCHESTRATOR_TOOLS:
        src += registration(tool, "_check_kanban_mode")
    for tool in WORKER_ONLY_TOOLS:
        src += registration(tool, "_check_kanban_mode")
    return src


def patch_tree(source):
    """Write ``source`` as tools/kanban_tools.py under a temp root and patch it."""
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


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

    def test_the_forbidden_four_are_all_covered(self):
        # The four agents/chat/SOUL.md §1.5 names explicitly. If a future edit
        # trims WORKER_ONLY_TOOLS, the prose and the schema set diverge again.
        for tool in ("kanban_complete", "kanban_block", "kanban_heartbeat", "kanban_link"):
            self.assertIn(tool, WORKER_ONLY_TOOLS)


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
    child, which would have offered it the seven worker-only tools and none of
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
        patched = patch_tree(upstream_source())
        for tool in WORKER_ONLY_TOOLS:
            self.assertIn(
                registration(tool, "_check_kanban_worker_mode"),
                patched,
                f"{tool} was not re-gated",
            )

    def test_orchestrator_tools_are_left_alone(self):
        patched = patch_tree(upstream_source())
        for tool in ORCHESTRATOR_TOOLS:
            self.assertIn(registration(tool, "_check_kanban_mode"), patched)
        for tool in ("kanban_list", "kanban_unblock"):
            self.assertIn(registration(tool, "_check_kanban_orchestrator_mode"), patched)

    def test_the_import_lands_above_the_registrations(self):
        patched = patch_tree(upstream_source())
        import_at = patched.index("from tools.kanban_worker_tools import")
        first_use = patched.index("check_fn=_check_kanban_worker_mode")
        # check_fn= is evaluated at import time, so a trailing import would
        # NameError at module load rather than at first tool call.
        self.assertLess(import_at, first_use)

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(upstream_source()))

    def test_reformatting_the_registration_no_longer_breaks_the_build(self):
        """The point of locating by AST: layout is not the contract.

        The five-line slice this applier used to anchor on would have found
        nothing here, and eight anchors would have failed at once over
        whitespace that changes no behaviour.
        """
        reflowed = upstream_source().replace(
            '    name="kanban_complete",\n    toolset="kanban",\n',
            '    name="kanban_complete",\n\n    # a comment upstream added\n'
            '    toolset="kanban",\n',
        )
        patched = patch_tree(reflowed)
        self.assertIn("check_fn=_check_kanban_worker_mode", patched)
        self.assertEqual(
            patched.count("check_fn=_check_kanban_worker_mode"),
            len(WORKER_ONLY_TOOLS),
        )

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(upstream_source())
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        # The second run gets as far as the registration and finds the gate
        # already swapped, which is the loud version of "already patched".
        self.assertIn(f"where {UPSTREAM_CHECK_FN} was expected", str(ctx.exception))

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
    could move a registration out from under this patch gets a test.
    """

    def assert_refuses(self, source, *expected):
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(source)
        for fragment in expected:
            self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_an_absent_registration_fails_loudly(self):
        source = upstream_source().replace(
            registration("kanban_link", "_check_kanban_mode"), ""
        )
        self.assert_refuses(source, "kanban_link registration", "found 0")

    def test_a_duplicated_registration_fails_loudly(self):
        """Two calls registering the same tool: which one is the patch for?"""
        source = upstream_source() + registration("kanban_link", "_check_kanban_mode")
        self.assert_refuses(source, "kanban_link registration", "found 2")

    def test_a_renamed_tool_fails_loudly(self):
        source = upstream_source().replace('name="kanban_attach"', 'name="kanban_file"')
        self.assert_refuses(source, "kanban_attach registration", "found 0")

    def test_a_renamed_handler_fails_loudly(self):
        """Found the call, but it is no longer wired to what we expected."""
        source = upstream_source().replace(
            registration("kanban_complete", "_check_kanban_mode"),
            registration("kanban_complete", "_check_kanban_mode", handler="_handle_finish"),
        )
        self.assert_refuses(
            source, "kanban_complete registration", "_handle_finish", "_handle_complete"
        )

    def test_a_renamed_gate_fails_loudly(self):
        """The check_fn upstream ships is asserted, not merely overwritten."""
        source = upstream_source().replace(
            registration("kanban_block", "_check_kanban_mode"),
            registration("kanban_block", "_check_kanban_task_mode"),
        )
        self.assert_refuses(
            source, "kanban_block registration", "_check_kanban_task_mode"
        )

    def test_a_registration_moved_out_of_module_scope_fails_loudly(self):
        """Wrapped in a `def register_all()`, the call is no longer ours to find."""
        source = upstream_source().replace(
            registration("kanban_heartbeat", "_check_kanban_mode"),
            "\n\ndef _register_late():\n"
            + "\n".join(
                "    " + line
                for line in registration(
                    "kanban_heartbeat", "_check_kanban_mode"
                ).strip("\n").split("\n")
            )
            + "\n",
        )
        self.assert_refuses(source, "kanban_heartbeat registration", "found 0")

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
        source = upstream_source().replace('name="kanban_attach"', 'name="kanban_file"')
        target.write_text(source)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), source, "a refused run must not write")


if __name__ == "__main__":
    unittest.main()
