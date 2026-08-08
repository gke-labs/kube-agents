"""Unit tests for patchlib, the shared machinery behind every apply_*.py here.

These appliers only protect the image because they fail loudly, and every one of
them now delegates that failure to this module. The negative cases below are
therefore the point of the file: an anchor that matches nothing, an anchor that
matches twice, and a replacement that produces source Python cannot parse must
all end in a non-zero exit with the file named, and must leave the target on
disk exactly as upstream shipped it.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import patchlib

RELATIVE = "tools/kanban_tools.py"

SOURCE = '''\
import os


def _check_kanban_mode() -> bool:
    return bool(os.environ.get("HERMES_KANBAN_TASK"))


registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)


registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="\U0001f4cb",
)
'''


def tree(source=SOURCE, relative=RELATIVE):
    """Write *source* under a fresh temp root and return (root, target path)."""
    root = Path(tempfile.mkdtemp())
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    return root, target


def patch(source=SOURCE, **kwargs):
    root, target = tree(source)
    kwargs.setdefault("prefix", "test")
    return patchlib.Patch(root, RELATIVE, **kwargs), target


class ConstructionTest(unittest.TestCase):
    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            patchlib.Patch(Path(tempfile.mkdtemp()), RELATIVE, prefix="test")
        self.assertIn("does not exist", str(ctx.exception))
        self.assertIn(RELATIVE, str(ctx.exception))

    def test_a_directory_is_not_a_file(self):
        root = Path(tempfile.mkdtemp())
        (root / RELATIVE).mkdir(parents=True)
        with self.assertRaises(SystemExit) as ctx:
            patchlib.Patch(root, RELATIVE, prefix="test")
        self.assertIn("does not exist", str(ctx.exception))

    def test_every_failure_names_the_file(self):
        # A Docker build log is the only place these are ever read, and the
        # reader has thirteen appliers to choose from.
        p, _ = patch()
        self.assertIn(RELATIVE, str(p._fail("something")))
        self.assertIn("test patch:", str(p._fail("something")))


class SubstituteTest(unittest.TestCase):
    def test_an_anchor_found_once_is_replaced(self):
        p, target = patch()
        p.substitute("import os\n", "import os\nimport sys\n")
        p.commit("1 anchor")
        self.assertIn("import sys", target.read_text())

    def test_an_absent_anchor_fails_loudly(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.substitute("import json\n", "")
        message = str(ctx.exception)
        self.assertIn("found 0", message)
        self.assertIn("expected 1 occurrence(s)", message)
        self.assertIn(RELATIVE, message)
        # The anchor itself is echoed: "found 0" without it says nothing about
        # which of an applier's fourteen anchors moved.
        self.assertIn("--- anchor ---\nimport json", message)

    def test_a_duplicated_anchor_fails_loudly(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.substitute('    toolset="kanban",\n', "")
        self.assertIn("found 2", str(ctx.exception))

    def test_an_expected_count_above_one_is_honoured(self):
        p, target = patch()
        p.substitute('    toolset="kanban",\n', '    toolset="kb",\n', expected=2)
        p.commit("2 anchors")
        self.assertEqual(target.read_text().count('toolset="kb"'), 2)

    def test_an_expected_count_above_one_still_fails_on_a_short_count(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.substitute("import os\n", "", expected=2)
        self.assertIn("expected 2 occurrence(s)", str(ctx.exception))
        self.assertIn("found 1", str(ctx.exception))

    def test_a_label_names_which_anchor_moved(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.substitute("import json\n", "", label="stdlib import")
        self.assertIn("occurrence(s) of the stdlib import anchor", str(ctx.exception))

    def test_the_drift_note_tells_the_reader_what_to_do(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.substitute("import json\n", "")
        self.assertIn("re-derive the anchor", str(ctx.exception))

    def test_the_drift_note_is_overridable(self):
        # apply_mcp_remote_forward_errors.py patches a TypeScript file, where
        # "bump the base image" is the wrong advice.
        p, _ = patch(note="mcp-remote changed.")
        with self.assertRaises(SystemExit) as ctx:
            p.substitute("import json\n", "")
        self.assertIn("mcp-remote changed.", str(ctx.exception))
        self.assertNotIn("base image", str(ctx.exception))


class RefuseIfPatchedTest(unittest.TestCase):
    def test_a_present_marker_refuses(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.refuse_if_patched("_handle_complete")
        self.assertIn("already patched", str(ctx.exception))
        self.assertIn("'_handle_complete' is present", str(ctx.exception))

    def test_an_absent_marker_is_a_no_op(self):
        p, _ = patch()
        p.refuse_if_patched("_kube_agents_marker")

    def test_any_one_of_several_markers_refuses(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.refuse_if_patched("_kube_agents_marker", "_handle_show")
        self.assertIn("_handle_show", str(ctx.exception))


class CommitTest(unittest.TestCase):
    def test_nothing_is_written_before_commit(self):
        p, target = patch()
        p.substitute("import os\n", "import sys\n")
        self.assertEqual(target.read_text(), SOURCE, "commit is the only write")

    def test_a_replacement_that_breaks_the_parse_fails_loudly(self):
        p, target = patch()
        p.substitute("import os\n", "import os\ndef (:\n")
        with self.assertRaises(SystemExit) as ctx:
            p.commit("1 anchor")
        self.assertIn("no longer parses", str(ctx.exception))
        self.assertIn(RELATIVE, str(ctx.exception))
        self.assertEqual(
            target.read_text(), SOURCE, "a file that does not parse must not be written"
        )

    def test_parse_false_writes_a_non_python_target(self):
        # The one non-Python target in this directory is TypeScript; `npm run
        # build` is its parse gate.
        p, target = patch(source="const x = 1\n")
        p.substitute("const x = 1\n", "const x = 2\n")
        p.commit("1 anchor", parse=False)
        self.assertEqual(target.read_text(), "const x = 2\n")

    def test_the_summary_is_reported_on_stdout(self):
        p, _ = patch()
        p.substitute("import os\n", "import sys\n")
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            p.commit("1 anchor")
        self.assertIn(f"test patch: {RELATIVE} (1 anchor)", printed.getvalue())


class AppendTest(unittest.TestCase):
    def test_a_trailer_lands_at_the_end(self):
        p, target = patch()
        p.append("\n\nimport late\n")
        p.commit("trailer")
        self.assertTrue(target.read_text().endswith("\n\nimport late\n"))


class FindDefTest(unittest.TestCase):
    def test_a_def_is_located_by_name(self):
        p, _ = patch()
        site = p.find_def("_check_kanban_mode", label="gate")
        self.assertEqual(
            p.source[site.start : site.end].splitlines()[0],
            "def _check_kanban_mode() -> bool:",
        )

    def test_after_is_the_line_following_the_def(self):
        p, target = patch()
        site = p.find_def("_check_kanban_mode", label="gate")
        p.insert(site.after, "\n\nimport sys\n")
        p.commit("insert")
        self.assertIn(
            'return bool(os.environ.get("HERMES_KANBAN_TASK"))\n\n\nimport sys\n',
            target.read_text(),
        )

    def test_an_absent_def_fails_loudly(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.find_def("_check_kanban_worker_mode", label="gate")
        self.assertIn("expected 1 module-level def", str(ctx.exception))
        self.assertIn("found 0", str(ctx.exception))
        self.assertIn("for the gate", str(ctx.exception))

    def test_a_duplicated_def_fails_loudly(self):
        p, _ = patch(source=SOURCE + "\n\ndef _check_kanban_mode() -> bool:\n    x = 1\n")
        with self.assertRaises(SystemExit) as ctx:
            p.find_def("_check_kanban_mode", label="gate")
        self.assertIn("found 2", str(ctx.exception))

    def test_a_nested_def_is_not_module_level(self):
        # An insert placed after a method would land inside the class body.
        p, _ = patch(source="class Gate:\n    def _check_kanban_mode(self):\n        pass\n")
        with self.assertRaises(SystemExit) as ctx:
            p.find_def("_check_kanban_mode", label="gate")
        self.assertIn("found 0", str(ctx.exception))

    def test_an_async_def_counts(self):
        p, _ = patch(source="async def _run() -> None:\n    pass\n")
        self.assertIsInstance(p.find_def("_run", label="runner"), patchlib.Definition)

    def test_a_file_that_does_not_parse_fails_loudly(self):
        p, _ = patch(source="def (:\n")
        with self.assertRaises(SystemExit) as ctx:
            p.find_def("_run", label="runner")
        self.assertIn("does not parse", str(ctx.exception))


class FindCallTest(unittest.TestCase):
    def test_a_call_is_located_by_a_keyword_argument(self):
        p, _ = patch()
        site = p.find_call(
            "registry.register", label="complete registration", name="kanban_complete"
        )
        self.assertIn("KANBAN_COMPLETE_SCHEMA", p.source[site.start : site.end])
        self.assertNotIn("KANBAN_SHOW_SCHEMA", p.source[site.start : site.end])

    def test_the_span_covers_the_whole_statement(self):
        p, _ = patch()
        site = p.find_call("registry.register", label="show", name="kanban_show")
        segment = p.source[site.start : site.end]
        self.assertTrue(segment.startswith("registry.register("))
        self.assertTrue(segment.endswith(")"))

    def test_an_absent_call_fails_loudly(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.find_call("registry.register", label="block", name="kanban_block")
        message = str(ctx.exception)
        self.assertIn("expected 1 module-level registry.register", message)
        self.assertIn("name='kanban_block'", message)
        self.assertIn("found 0", message)

    def test_a_duplicated_call_fails_loudly(self):
        p, _ = patch(
            source=SOURCE + '\n\nregistry.register(\n    name="kanban_show",\n)\n'
        )
        with self.assertRaises(SystemExit) as ctx:
            p.find_call("registry.register", label="show", name="kanban_show")
        self.assertIn("found 2", str(ctx.exception))

    def test_a_renamed_callee_fails_loudly(self):
        p, _ = patch()
        with self.assertRaises(SystemExit) as ctx:
            p.find_call("registry.add", label="show", name="kanban_show")
        self.assertIn("registry.add", str(ctx.exception))
        self.assertIn("found 0", str(ctx.exception))

    def test_a_call_that_is_not_a_bare_statement_is_not_matched(self):
        # `x = registry.register(...)` is a different thing to splice into.
        p, _ = patch(source='x = registry.register(\n    name="kanban_show",\n)\n')
        with self.assertRaises(SystemExit) as ctx:
            p.find_call("registry.register", label="show", name="kanban_show")
        self.assertIn("found 0", str(ctx.exception))

    def test_a_string_selector_does_not_match_a_bare_name(self):
        p, _ = patch(source="registry.register(\n    name=kanban_show,\n)\n")
        with self.assertRaises(SystemExit) as ctx:
            p.find_call("registry.register", label="show", name="kanban_show")
        self.assertIn("found 0", str(ctx.exception))

    def test_an_ident_selector_matches_a_bare_name(self):
        p, _ = patch(source="registry.register(\n    name=kanban_show,\n)\n")
        site = p.find_call(
            "registry.register", label="show", name=patchlib.Ident("kanban_show")
        )
        self.assertIsInstance(site, patchlib.CallSite)


class ExpectTest(unittest.TestCase):
    def site(self, source=SOURCE):
        p, _ = patch(source=source)
        return p, p.find_call(
            "registry.register", label="complete registration", name="kanban_complete"
        )

    def test_matching_arguments_pass(self):
        _, site = self.site()
        site.expect(
            toolset="kanban",
            schema=patchlib.Ident("KANBAN_COMPLETE_SCHEMA"),
            handler=patchlib.Ident("_handle_complete"),
        )

    def test_a_rewired_argument_fails_loudly(self):
        _, site = self.site()
        with self.assertRaises(SystemExit) as ctx:
            site.expect(handler=patchlib.Ident("_handle_finish"))
        message = str(ctx.exception)
        self.assertIn("the complete registration sets handler=_handle_complete", message)
        self.assertIn("where _handle_finish was expected", message)

    def test_a_string_where_a_name_was_expected_fails(self):
        # check_fn="_check_kanban_mode" would be a different program: a string
        # is not the callable the registry is about to hold onto.
        _, site = self.site(
            'registry.register(\n'
            '    name="kanban_complete",\n'
            '    check_fn="_check_kanban_mode",\n'
            ')\n'
        )
        with self.assertRaises(SystemExit) as ctx:
            site.expect(check_fn=patchlib.Ident("_check_kanban_mode"))
        self.assertIn("where _check_kanban_mode was expected", str(ctx.exception))

    def test_a_missing_argument_fails_loudly(self):
        _, site = self.site()
        with self.assertRaises(SystemExit) as ctx:
            site.expect(deprecated=True)
        self.assertIn("sets no deprecated= argument", str(ctx.exception))


class KeywordSpanTest(unittest.TestCase):
    def test_the_span_covers_only_the_value(self):
        p, _ = patch()
        site = p.find_call("registry.register", label="complete", name="kanban_complete")
        start, end = site.keyword_span("check_fn")
        self.assertEqual(p.source[start:end], "_check_kanban_mode")

    def test_a_splice_replaces_only_that_value(self):
        p, target = patch()
        site = p.find_call("registry.register", label="complete", name="kanban_complete")
        start, end = site.keyword_span("check_fn")
        p.splice(start, end, "_check_kanban_worker_mode")
        p.commit("1 registration")
        patched = target.read_text()
        self.assertIn("check_fn=_check_kanban_worker_mode,", patched)
        # The sibling registration is untouched.
        self.assertEqual(patched.count("check_fn=_check_kanban_mode,"), 1)

    def test_a_missing_argument_fails_loudly(self):
        p, _ = patch()
        site = p.find_call("registry.register", label="complete", name="kanban_complete")
        with self.assertRaises(SystemExit) as ctx:
            site.keyword_span("gate_fn")
        self.assertIn("sets no gate_fn= argument", str(ctx.exception))


class ByteColumnTest(unittest.TestCase):
    """``ast`` reports columns in UTF-8 bytes; the source being spliced is a str.

    No span the current locators return lands on a non-ASCII line, so this is a
    trap that has not sprung yet — but every registration in
    ``tools/kanban_tools.py`` carries an ``emoji=`` argument, and a byte column
    read as a character index does not raise, it splices three characters off
    target. These tests fail if ``_offset`` is ever simplified away.
    """

    def test_a_span_after_an_emoji_line_is_still_exact(self):
        p, _ = patch()
        site = p.find_call("registry.register", label="show", name="kanban_show")
        # kanban_show's registration follows kanban_complete's "✔" line.
        start, end = site.keyword_span("schema")
        self.assertEqual(p.source[start:end], "KANBAN_SHOW_SCHEMA")

    def test_a_span_on_a_line_containing_an_emoji_is_still_exact(self):
        source = 'registry.register(\n    emoji="\U0001f4cb", name="kanban_show",\n)\n'
        p, _ = patch(source=source)
        site = p.find_call("registry.register", label="show", name="kanban_show")
        start, end = site.keyword_span("name")
        self.assertEqual(p.source[start:end], '"kanban_show"')

    def test_a_def_after_an_emoji_is_located_exactly(self):
        body = "def _gate() -> bool:\n    return True"
        source = 'X = "\U0001f4cb\U0001f4cb\U0001f4cb"\n\n\n' + body + "\n"
        p, _ = patch(source=source)
        site = p.find_def("_gate", label="gate")
        self.assertEqual(p.source[site.start : site.end], body)
        self.assertEqual(site.after, len(source))


if __name__ == "__main__":
    unittest.main()
