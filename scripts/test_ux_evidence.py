"""Tests for ux_evidence.py: which paths are user-facing, and what counts as evidence."""

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ux_evidence as ux

CHAT = ("agents/*/SOUL.md", "agents/chat/defaults/**", "a2a/lib/chatmsg/**", "a2a/gateway/slack_*.go")
SHOT = "![slack-plan-row](https://raw.githubusercontent.com/me/kube-agents/pr-evidence/a.png)"


class GlobTest(unittest.TestCase):
    def test_star_stays_in_one_segment(self):
        self.assertTrue(ux.glob_to_regex("agents/*/SOUL.md").match("agents/chat/SOUL.md"))
        self.assertFalse(ux.glob_to_regex("agents/*/SOUL.md").match("agents/chat/x/SOUL.md"))

    def test_double_star_spans_directories(self):
        self.assertTrue(ux.glob_to_regex("a2a/lib/chatmsg/**").match("a2a/lib/chatmsg/sub/lint.go"))
        self.assertTrue(ux.glob_to_regex("**/tests/**").match("a2a/web/tests/page.js"))
        self.assertTrue(ux.glob_to_regex("**/test_*.py").match("test_top.py"))

    def test_unsupported_syntax_is_refused(self):
        with self.assertRaises(ValueError):
            ux.glob_to_regex("b/{x,y}.go")

    def test_whole_path_only(self):
        self.assertFalse(ux.glob_to_regex("install.sh").match("scripts/install.sh"))
        self.assertFalse(ux.glob_to_regex("install.sh").match("install.sh.bak"))


class ChatGlobsTest(unittest.TestCase):
    def test_reads_the_real_rule_file(self):
        globs = ux.chat_globs()
        self.assertIn("a2a/lib/chatmsg/**", globs)
        self.assertIn("agents/*/SOUL.md", globs)

    def test_stops_at_the_next_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = pathlib.Path(tmp) / "r.md"
            rules.write_text('---\npaths:\n  - "a/**"\n  - b.go\ntrigger: x\n---\n# body\n  - "c"\n')
            self.assertEqual(ux.chat_globs(rules), ("a/**", "b.go"))

    def test_an_unreadable_entry_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = pathlib.Path(tmp) / "r.md"
            rules.write_text('---\npaths:\n  - "a/**"\n  - "c.go"  # note\n---\n')
            with self.assertRaises(ValueError):
                ux.chat_globs(rules)

    def test_no_frontmatter_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = pathlib.Path(tmp) / "r.md"
            rules.write_text("# no frontmatter\n")
            with self.assertRaises(ValueError):
                ux.chat_globs(rules)


class ClassifyTest(unittest.TestCase):
    def test_splits_chat_from_other_surfaces(self):
        chat, other = ux.classify(
            ["a2a/gateway/slack_chat.go", "docs/site/src/components/Card.astro", "k8s-operator/main.go"],
            chat=CHAT,
        )
        self.assertEqual(chat, ["a2a/gateway/slack_chat.go"])
        self.assertEqual(other, ["docs/site/src/components/Card.astro"])

    def test_chat_markdown_counts(self):
        chat, _ = ux.classify(
            ["agents/chat/SOUL.md", "agents/platform/SOUL.md", "agents/chat/defaults/onboarding/report_filed.md"],
            chat=CHAT,
        )
        self.assertEqual(len(chat), 3)

    def test_real_rules_cover_a_soul(self):
        chat, _ = ux.classify(["agents/chat/SOUL.md"])
        self.assertEqual(chat, ["agents/chat/SOUL.md"])

    def test_tests_and_docs_prose_are_not_seen(self):
        chat, other = ux.classify(
            [
                "a2a/gateway/slack_chat_test.go",
                "a2a/lib/chatmsg/testdata/x.json",
                "a2a/cmd/web-console/static/app_test.go",
                "a2a/web/package-lock.json",
                "a2a/web/src/App.test.tsx",
                "a2a/web/dev/seed.mjs",
                "a2a/web/vite.config.ts",
                "a2a/web/README.md",
                "admin_console/ui.py",
                "docs/site/src/content/docs/install.md",
                "docs/site/package.json",
            ],
            chat=CHAT,
        )
        self.assertEqual((chat, other), ([], []))

    def test_installer_front_doors_and_consoles(self):
        files = [
            "install.sh",
            "scripts/installer/common.sh",
            "a2a/web/index.html",
            "a2a/cmd/web-console/static/app.js",
            "charts/kube-agents/templates/web-console.yaml",
            "a2a/console/console.go",
            "a2a/cmd/console/main.go",
        ]
        _, other = ux.classify(files, chat=CHAT)
        self.assertEqual(other, files)


class BodyTest(unittest.TestCase):
    def test_markdown_image(self):
        self.assertTrue(ux.has_image(f"## UX change\n\n{SHOT}\n"))

    def test_html_image(self):
        self.assertTrue(ux.has_image('<img width="600" src="https://example.com/a.png">'))

    def test_drag_and_drop_upload(self):
        self.assertTrue(ux.has_image("https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb"))

    def test_image_in_a_comment_does_not_count(self):
        self.assertFalse(ux.has_image(f"<!--\n{SHOT}\n-->\n- [ ] No user-visible change"))

    def test_image_in_an_unclosed_comment_does_not_count(self):
        self.assertFalse(ux.has_image(f"<!-- forgot to close\n{SHOT}\n"))

    def test_image_in_a_fence_does_not_count(self):
        self.assertFalse(ux.has_image(f"```\n{SHOT}\n```\n"))

    def test_image_in_an_indented_fence_does_not_count(self):
        self.assertFalse(ux.has_image(f"- item\n\n    ```\n    {SHOT}\n    ```\n"))

    def test_image_in_a_four_backtick_fence_does_not_count(self):
        self.assertFalse(ux.has_image(f"````\n```\n{SHOT}\n```\n````\n"))

    def test_image_in_inline_code_does_not_count(self):
        self.assertFalse(ux.has_image(f"use `{SHOT}` like this"))

    def test_comment_marker_in_code_does_not_hide_a_real_image(self):
        self.assertTrue(ux.has_image(f"```\n<!--\n```\n\n{SHOT}\n"))

    def test_a_fence_opened_inside_a_comment_ends_with_it(self):
        self.assertTrue(ux.has_image(f"<!--\n```bash\n-->\n\n{SHOT}\n"))

    def test_an_info_string_line_does_not_close_a_fence(self):
        self.assertFalse(ux.has_image(f"```\n```python\n{SHOT}\n```\n"))

    def test_a_comment_ends_at_its_first_close_even_inside_backticks(self):
        self.assertTrue(ux.has_image(f"<!-- a `b -->` c\n{SHOT}\n"))

    def test_a_fence_inside_a_comment_does_not_pair_with_a_later_block(self):
        self.assertTrue(ux.has_image(f"<!--\n```\n-->\n{SHOT}\n\n```\ncode\n```\n"))

    def test_a_double_backtick_span_holding_a_backtick(self):
        self.assertTrue(ux.has_image(f"`` ` `` {SHOT} `y`"))

    def test_a_stray_backtick_does_not_span_paragraphs(self):
        self.assertTrue(ux.has_image(f"a stray ` here\n\n## UX change\n\n{SHOT}\n\ncaption `path`\n"))
        self.assertTrue(ux.no_change_ticked("stray `\n\n```\ncode\n```\n\n- [x] No user-visible change `x`"))

    def test_empty_images_do_not_count(self):
        for body in ("![]()", "![](   )", '<img src="">', "<img src=>"):
            with self.subTest(body=body):
                self.assertFalse(ux.has_image(body))

    def test_bracketed_alt_text_counts(self):
        self.assertTrue(ux.has_image("![chat reply [after]](https://example.com/shot.png)"))

    def test_an_unclosed_fence_hides_the_images_after_it(self):
        self.assertFalse(ux.has_image(f"```\n{SHOT}\n"))
        self.assertFalse(ux.has_image(f"text\n\n~~~bash\nrun\n\n{SHOT}\n"))

    def test_what_is_not_an_unclosed_fence_hides_nothing(self):
        for body in (
            f"``` foo ```\n\n{SHOT}\n",
            f"```x```\n{SHOT}\n",
            f"- step\n  ```bash\n  run\n\n{SHOT}\n",
            f"    ```\n\n{SHOT}\n",
        ):
            with self.subTest(body=body):
                self.assertTrue(ux.has_image(body))

    def test_a_link_is_not_an_image(self):
        self.assertFalse(ux.has_image("[screenshot](https://example.com/a.png)"))

    def test_ticked_box(self):
        self.assertTrue(ux.no_change_ticked("- [x] No user-visible change"))
        self.assertTrue(ux.no_change_ticked("* [X] No user-visible change: a refactor"))

    def test_unticked_or_commented_box(self):
        self.assertFalse(ux.no_change_ticked("- [ ] No user-visible change"))
        self.assertFalse(ux.no_change_ticked("<!-- - [x] No user-visible change -->"))


class VerdictTest(unittest.TestCase):
    def test_no_surface_passes_with_an_empty_body(self):
        passed, chat, _ = ux.verdict(["scripts/merge_gate.py"], "", chat=CHAT)
        self.assertEqual((passed, chat), (True, False))

    def test_a_chat_case_change_runs_the_chat_job_without_a_screenshot(self):
        for path in ("tests/conformance/chat-ux/lint.json", "tests/conformance/chat/answer-markdown-detail.json"):
            with self.subTest(path=path):
                passed, chat, _ = ux.verdict([path], "", chat=CHAT)
                self.assertEqual((passed, chat), (True, True))

    def test_surface_without_evidence_fails_and_names_the_path(self):
        passed, chat, message = ux.verdict(["a2a/gateway/slack_stop.go"], "- [ ] No user-visible change", chat=CHAT)
        self.assertEqual((passed, chat), (False, True))
        self.assertIn("a2a/gateway/slack_stop.go", message)

    def test_surface_with_a_screenshot_passes(self):
        passed, chat, _ = ux.verdict(["a2a/gateway/slack_stop.go"], SHOT, chat=CHAT)
        self.assertEqual((passed, chat), (True, True))

    def test_surface_with_the_box_ticked_passes(self):
        passed, _, _ = ux.verdict(["a2a/console/console.go"], "- [x] No user-visible change", chat=CHAT)
        self.assertTrue(passed)

    def test_none_body(self):
        passed, _, _ = ux.verdict(["install.sh"], None, chat=CHAT)
        self.assertFalse(passed)


class MainTest(unittest.TestCase):
    def test_writes_the_chat_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = pathlib.Path(tmp) / "body.md"
            files = pathlib.Path(tmp) / "files.txt"
            out = pathlib.Path(tmp) / "out"
            body.write_text("no screenshot here")
            files.write_text("a2a/lib/chatmsg/lint.go\n\n")
            old = os.environ.get(ux.OUTPUT_ENV)
            os.environ[ux.OUTPUT_ENV] = str(out)
            try:
                code = ux.main(["--body-file", str(body), "--files", str(files)])
            finally:
                if old is None:
                    del os.environ[ux.OUTPUT_ENV]
                else:
                    os.environ[ux.OUTPUT_ENV] = old
            self.assertEqual(code, ux.EXIT_MISSING)
            self.assertEqual(out.read_text(), "chat=true\n")


if __name__ == "__main__":
    unittest.main()
