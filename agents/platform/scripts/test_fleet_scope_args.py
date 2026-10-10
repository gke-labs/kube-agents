"""fleet_scope_args: the flags that carry the declared scope into a collector.

Run: python3 -m unittest discover -s agents/platform/scripts -p 'test_fleet_scope_args.py' -v
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import os
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fleet_scope_args as fsa  # noqa: E402

# The holder reads the sandbox's root-owned scope file first, then
# KUBEAGENTS_SCOPE_DECLARED, when neither flag is passed; the host running this
# suite must not decide its answers through either.
os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)  # module-level, on purpose
fsa.SCOPE_DECLARED_FILE = "/nonexistent/kube-agents-sandbox/scope-declared"


class FleetScopeArgsTest(unittest.TestCase):
    def test_the_two_flags_parse_as_the_tool_spells_them(self):
        parser = argparse.ArgumentParser()
        fsa.add_scope_arguments(parser)
        args = parser.parse_args(["--scope-projects", "ops-mgmt,payments-prod", "--scope-unread", "payments-staging=denied"])
        self.assertEqual(fsa.parse_scope_projects(args.scope_projects), ["ops-mgmt", "payments-prod"])
        self.assertEqual(fsa.parse_scope_unread(args.scope_unread), [("payments-staging", "denied")])
        self.assertEqual((args.scope_projects, args.scope_unread), ("ops-mgmt,payments-prod", "payments-staging=denied"))
        self.assertEqual(parser.parse_args([]).scope_projects, None)

    def test_separators_and_a_missing_outcome(self):
        self.assertEqual(fsa.parse_scope_projects(" a, b  c\n"), ["a", "b", "c"])
        self.assertEqual(fsa.parse_scope_projects(None), [])
        self.assertEqual(fsa.parse_scope_unread("p=denied q"), [("p", "denied"), ("q", "unknown")])

    def test_the_holder_records_the_flags_and_reads_back_as_the_resolver_does(self):
        scope = fsa.DeclaredScope()
        self.assertFalse(scope.declared)
        self.assertIsNone(scope.projects)
        scope.set("a,b", "p=denied")
        self.assertTrue(scope.declared)
        self.assertEqual((scope.projects, scope.unread), (["a", "b"], [("p", "denied")]))
        self.assertIn("p (denied)", scope.note())
        scope.set(None, None)
        self.assertEqual((scope.declared, scope.projects, scope.note()), (False, None, None))

    def test_a_declared_scope_with_nothing_readable_is_declared_and_empty_not_absent(self):
        # `--scope-unread` alone, or a blank `--scope-projects`, is a boundary the
        # install could read nothing inside: the collector reports that and
        # must not fall through to the listing.
        scope = fsa.DeclaredScope()
        scope.set(None, "p=denied,q=unreachable")
        self.assertEqual((scope.declared, scope.projects), (True, []))
        self.assertIn("no project this install could read", scope.empty_error())
        self.assertIn("p (denied), q (unreachable)", scope.empty_error())
        scope.set("", None)
        self.assertEqual((scope.declared, scope.projects), (True, []))
        self.assertNotIn("(", scope.empty_error().split("this run")[1][:2])

    def test_the_operators_answer_makes_a_run_without_the_flags_declared_and_empty(self):
        # The sandbox cannot see the snapshot, so the operator sets the env:
        # on an install with a scope block a collector run without the flags
        # must report, not list. A checkout, or an install without a block,
        # leaves the flags alone to decide.
        scope = fsa.DeclaredScope()
        with mock.patch.dict(os.environ, {fsa.SCOPE_DECLARED_ENV: "true"}):
            scope.set(None, None)
            self.assertEqual((scope.declared, scope.args_missing, scope.projects), (True, True, []))
            self.assertIn("got no collector_args", scope.empty_error())
            scope.set("ops-mgmt", None)
            self.assertEqual((scope.declared, scope.args_missing, scope.projects), (True, False, ["ops-mgmt"]))
        with mock.patch.dict(os.environ, {fsa.SCOPE_DECLARED_ENV: "false"}):
            scope.set(None, None)
            self.assertEqual((scope.declared, scope.projects), (False, None))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fsa.SCOPE_DECLARED_ENV, None)
            scope.set(None, None)
            self.assertEqual((scope.declared, scope.projects), (False, None))

    def test_the_sweep_counts_as_declared_only_with_the_flags_and_no_override(self):
        scope = fsa.DeclaredScope()
        scope.set("ops-mgmt,payments-prod", None)
        self.assertTrue(scope.sweep_is_declared())
        self.assertFalse(scope.sweep_is_declared("ops-mgmt"), "a --project narrows the sweep below the declared scope")
        with mock.patch.dict(os.environ, {fsa.SCOPE_DECLARED_ENV: "true"}):
            scope.set(None, None)
            self.assertFalse(scope.sweep_is_declared(), "declared by the env alone is not the declared sweep")
        scope.set(None, None)
        self.assertFalse(scope.sweep_is_declared())
        # The unresolved-scope row: the members are what is unknown, so the
        # sweep is not the declared scope and the row's tail must not say it is.
        scope.set("ops-mgmt", f"{fsa.UNRESOLVED_SCOPE_ROW}=unresolved")
        self.assertTrue(scope.declared)
        self.assertFalse(scope.sweep_is_declared(), "an unresolved declaration is not the declared sweep")
        scope.set("ops-mgmt", "payments-prod=denied")
        self.assertTrue(scope.sweep_is_declared(), "an unread project is a known gap, not an unknown size")

    def test_the_root_owned_file_outranks_the_environment(self):
        # A session can unset or override the variable in one word; the file the
        # entrypoint writes as root is what the guard reads first.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scope-declared")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("true\n")
            scope = fsa.DeclaredScope()
            with mock.patch.object(fsa, "SCOPE_DECLARED_FILE", path), mock.patch.dict(os.environ, {fsa.SCOPE_DECLARED_ENV: "false"}):
                scope.set(None, None)
                self.assertTrue(scope.args_missing, "the file says declared; the session's variable does not get a vote")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("false\n")
            with mock.patch.object(fsa, "SCOPE_DECLARED_FILE", path), mock.patch.dict(os.environ, {fsa.SCOPE_DECLARED_ENV: "true"}):
                scope.set(None, None)
                self.assertFalse(scope.declared, "the file says no scope; a variable cannot invent one either")

    def test_a_project_number_override_is_refused_with_the_remedy_not_as_outside(self):
        scope = fsa.DeclaredScope()
        scope.set("ops-mgmt", None)
        self.assertIn("could not resolve", scope.override_error("123456789012"))
        self.assertNotIn("does not list", scope.override_error("123456789012"))
        scope.set(None, None)
        self.assertIsNone(scope.override_error("123456789012"), "without a declared scope an override is free")

    def test_the_unresolved_scope_row_is_rendered_as_a_sentence_not_a_project(self):
        note = fsa.unread_note([(fsa.UNRESOLVED_SCOPE_ROW, "unresolved"), ("payments-staging", "denied")])
        self.assertIn("not resolved yet", note)
        self.assertIn("names 1 project(s)", note, "the sentinel is not counted among the projects")
        self.assertNotIn("declared-scope (", note)
        self.assertIn("not resolved yet", fsa.unread_note([(fsa.UNRESOLVED_SCOPE_ROW, "unresolved")]))

    def test_an_override_naming_an_unread_declared_project_says_so(self):
        scope = fsa.DeclaredScope()
        scope.set("ops-mgmt", "payments-staging=denied")
        self.assertIn("could not read (denied)", scope.override_error("payments-staging"))
        self.assertIn("does not list", scope.override_error("acme-only"))

    def test_a_repeated_project_id_is_swept_once(self):
        self.assertEqual(fsa.parse_scope_projects("ops-mgmt,payments-prod,ops-mgmt"), ["ops-mgmt", "payments-prod"])

    def test_the_note_names_each_unread_project(self):
        self.assertIsNone(fsa.unread_note([]))
        note = fsa.unread_note([("p", "denied"), ("q", "over-cap")])
        self.assertIn("2 project(s)", note)
        self.assertIn("p (denied), q (over-cap)", note)
        self.assertIn("fleet_scope tool", note)


if __name__ == "__main__":
    unittest.main()
