#!/usr/bin/env python3
"""Unit tests for the site prose ratchet.

Run: cd scripts && python3 -m unittest test_check_docs_prose

The check fails green: a shape that stops matching lets the next `simply`
onto the site with no signal, an extraction that stops blanking fences turns
every page with a code block red, and a ratchet that accepts an unrecorded
improvement stops being a record. Each direction is pinned here on a
throwaway site root and ratchet file; two tests run against the real tree to
pin the committed ratchet to today's counts and its canonical form. Nothing
here times the scan: CPU time on a shared runner, with the sweep's other
workers on its cores, is not a property of the script, so a bound on it reds
the job on the runner's load alone.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import check_docs_prose as prose

# Literals below are the shapes under test, not values the site may carry.
FRONTMATTER = "---\ntitle: Page\ndescription: simply a test\n---\n\n"
SHORT_SENTENCE_WORDS = 10


def one_sentence(n: int) -> str:
    """``n`` words with no sentence end inside them."""
    return " ".join(["word"] * n)


def words(n: int) -> str:
    """``n`` words in short sentences, so only the count under test can trip a ceiling."""
    out = []
    while n > 0:
        take = min(n, SHORT_SENTENCE_WORDS)
        out.append(" ".join(["Word"] + ["word"] * (take - 1)) + ".")
        n -= take
    return " ".join(out)


class SiteFixture(unittest.TestCase):
    """A throwaway site root and ratchet file per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.site = root / "site"
        self.site.mkdir()
        self.ratchet = root / "ratchet.json"

    def write_page(self, body: str, name: str = "page.md") -> Path:
        page = self.site / name
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(FRONTMATTER + body, encoding="utf-8")
        return page

    def findings(self, body: str, name: str = "page.md") -> list[prose.Finding]:
        page = self.write_page(body, name)
        return prose.page_findings(page, page.read_text(encoding="utf-8"))

    def kinds(self, body: str) -> list[str]:
        return [f.kind for f in self.findings(body)]

    def run_main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = prose.main(list(argv), self.site, self.ratchet)
        return code, out.getvalue()


class ExtractionTest(SiteFixture):
    def test_frontmatter_is_not_prose(self):
        self.assertEqual(self.kinds("A plain sentence.\n"), [])

    def test_backticked_identifier_is_not_a_banned_word(self):
        self.assertEqual(self.kinds("Run `simply` and `--robust` here.\n"), [])

    def test_link_url_is_not_prose(self):
        self.assertEqual(self.kinds("See [the page](https://example.com/simply/robust.html).\n"), [])

    def test_backtick_fence_is_not_prose(self):
        self.assertEqual(self.kinds("```sh\nsimply robust\n```\n"), [])

    def test_tilde_fence_is_not_prose(self):
        self.assertEqual(self.kinds("~~~\nsimply robust\n~~~\n"), [])

    def test_html_comment_is_not_prose(self):
        self.assertEqual(self.kinds("<!-- simply -->\n<!--\nrobust\n-->\n"), [])

    def test_generated_region_is_not_prose(self):
        body = "<!-- BEGIN GENERATED: x -->\nsimply robust\n<!-- END GENERATED: x -->\n"
        self.assertEqual(self.kinds(body), [])

    def test_one_line_generated_region_blanks_only_its_line(self):
        body = "<!-- BEGIN GENERATED: x --> robust <!-- END GENERATED: x -->\n\nThis is simply text.\n"
        findings = self.findings(body)
        self.assertEqual([f.detail for f in findings], ["simply"])
        self.assertEqual(findings[0].line, FRONTMATTER.count("\n") + 3)

    def test_mdx_import_and_tag_lines_are_not_prose(self):
        body = 'import { Aside } from "@astrojs/starlight/components";\n\n<Aside type="note">\n  Fine text.\n</Aside>\n'
        self.assertEqual(self.kinds(body), [])

    def test_prose_inside_a_component_is_still_read(self):
        body = '<Aside type="note">\n  This is simply text.\n</Aside>\n'
        self.assertEqual(self.kinds(body), [prose.KIND_BANNED_WORD])

    def test_multi_line_component_tag_is_not_prose(self):
        body = (
            "<CardGrid>\n  <LinkCard\n    title=\"Seamless\"\n    description=\"A robust card, simply put.\"\n"
            "    href=\"/x/\"\n  />\n</CardGrid>\n\nAfter the grid.\n"
        )
        self.assertEqual(self.kinds(body), [])

    def test_one_line_component_tag_reads_the_same_as_the_wrapped_one(self):
        body = '<LinkCard title="Seamless" description="A robust card, simply put." href="/x/" />\n'
        self.assertEqual(self.kinds(body), [])

    def test_wrapped_opening_tag_closed_by_a_bare_bracket_keeps_its_body(self):
        """prettier's JSX layout puts the `>` on its own line; the body after it is prose."""
        body = '<Aside\n  type="caution"\n  title="Robust"\n>\n  This is simply text.\n</Aside>\n'
        findings = self.findings(body)
        self.assertEqual([f.kind for f in findings], [prose.KIND_BANNED_WORD])
        self.assertEqual(findings[0].line, FRONTMATTER.count("\n") + 5)

    def test_blockquote_separator_ends_a_paragraph(self):
        """Two quoted paragraphs are two, and a sentence does not run across the `>` line."""
        half = words(prose.PARAGRAPH_WORD_LIMIT // 2 + 1)
        self.assertEqual(self.kinds(f"> {half}\n>\n> {half}\n"), [])
        quoted = f"> {one_sentence(prose.SENTENCE_WORD_LIMIT // 2)}.\n>\n> {one_sentence(prose.SENTENCE_WORD_LIMIT // 2 + 1)}.\n"
        self.assertEqual(self.kinds(quoted), [])

    def test_quoted_table_row_is_a_table_row(self):
        body = f"> Lead-in.\n>\n> | a | b |\n> | --- | --- |\n> | x | {words(prose.CELL_WORD_LIMIT + 1)} |\n"
        self.assertEqual(self.kinds(body), [prose.KIND_CELL])

    def test_wrapped_code_span_is_one_word_and_the_prose_after_it_is_read(self):
        self.assertEqual(self.kinds("Set `--robust\nflag` here.\n"), [])
        finding = self.findings("Set `a\nb` simply `c` here.\n")[0]
        self.assertEqual(finding.kind, prose.KIND_BANNED_WORD)
        self.assertEqual(finding.line, FRONTMATTER.count("\n") + 2)

    def test_comment_opening_after_prose_is_not_prose(self):
        for body in (
            "Rollback is manual today. <!-- TODO: simply\nautomate -->\nAfter.\n",
            "Rollback is manual today. {/* TODO: simply\nautomate */}\nAfter.\n",
            "Rollback is manual today. <!-- simply --> After.\n",
        ):
            with self.subTest(body=body[:50]):
                self.assertEqual(self.kinds(body), [])

    def test_comment_and_span_continuation_lines_do_not_split_a_paragraph(self):
        """A line a comment or a wrapped span emptied is the same paragraph, not a break."""
        half = words(prose.PARAGRAPH_WORD_LIMIT // 2 + 1)
        for body in (
            f"{half} <!-- TODO: note\nsomething -->\n{half}\n",
            f"{half} {{/* TODO: note\nsomething */}}\n{half}\n",
            f"{half} `a\nb\nc` {half}\n",
        ):
            with self.subTest(body=body[-40:]):
                self.assertEqual(self.kinds(body), [prose.KIND_PARAGRAPH])
        straddling = f"{one_sentence(prose.SENTENCE_WORD_LIMIT // 2)} <!-- a\nb -->\n{one_sentence(prose.SENTENCE_WORD_LIMIT // 2 + 1)}.\n"
        self.assertEqual(self.kinds(straddling), [prose.KIND_SENTENCE])

    def test_comment_opener_in_a_code_span_is_not_an_opener(self):
        body = "The `<!--` opener is documented here.\nThis is simply text.\n"
        self.assertEqual(self.kinds(body), [prose.KIND_BANNED_WORD])

    def test_line_numbers_are_one_based_source_lines(self):
        body = "First line.\n\n```\ncode\n```\n\nThis is simply text.\n"
        finding = self.findings(body)[0]
        frontmatter_lines = FRONTMATTER.count("\n")
        self.assertEqual(finding.line, frontmatter_lines + 7)


class BannedWordTest(SiteFixture):
    def test_every_banned_word_hits(self):
        for word in prose.BANNED_WORDS:
            with self.subTest(word=word):
                self.assertEqual(self.kinds(f"This is {word} text.\n"), [prose.KIND_BANNED_WORD])

    def test_every_banned_phrase_hits(self):
        for phrase in prose.BANNED_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.kinds(f"And {phrase} the rest.\n"), [prose.KIND_BANNED_WORD])

    def test_match_is_case_insensitive(self):
        self.assertEqual(self.kinds("Simply put.\n"), [prose.KIND_BANNED_WORD])

    def test_match_is_whole_word(self):
        self.assertEqual(self.kinds("A robustness test with delvers and synergistic facet.\n"), [])

    def test_phrase_match_is_whole_word(self):
        self.assertEqual(self.kinds("The limit is set within today's release.\n"), [])

    def test_phrase_wrapped_across_lines_hits_at_its_first_line(self):
        for body in (
            "It is worth\nnoting that.\n",
            "- We can leverage\n  the controller.\n",
            "| a | b |\n| --- | --- |\n| it's worth\nnoting | x |\n",
        ):
            with self.subTest(body=body[:20]):
                findings = self.findings(body)
                self.assertEqual([f.kind for f in findings], [prose.KIND_BANNED_WORD])
                self.assertNotIn("\n", findings[0].detail)
        finding = self.findings("Lead-in.\nIt is worth\nnoting that.\n")[0]
        self.assertEqual(finding.line, FRONTMATTER.count("\n") + 2)
        self.assertEqual(finding.detail, "It is worth noting")

    def test_phrase_does_not_cross_a_blank_line(self):
        self.assertEqual(self.kinds("It is worth\n\nnoting that.\n"), [])
        self.assertEqual(self.kinds("We can leverage\n\nthe controller.\n"), [])

    def test_phrase_with_a_typographic_apostrophe_hits(self):
        for body in ("It\u2019s worth noting this.\n", "In today\u2019s release.\n"):
            with self.subTest(body=body):
                self.assertEqual(self.kinds(body), [prose.KIND_BANNED_WORD])

    def test_leverage_as_a_verb_hits(self):
        for text in ("It leverages the cache.", "Leverage the cache.", "We are leveraging it."):
            with self.subTest(text=text):
                self.assertEqual(self.kinds(text + "\n"), [prose.KIND_BANNED_WORD])

    def test_leverage_as_a_noun_does_not(self):
        self.assertEqual(self.kinds("The lease gives the operator leverage.\n"), [])

    def test_finding_carries_the_matched_text(self):
        finding = self.findings("A seamless install.\n")[0]
        self.assertEqual(finding.detail, "seamless")


class AntithesisTest(SiteFixture):
    def test_is_not_a_x_it_is_y_hits(self):
        for sep in (",", ":", ";", " —", " -"):
            with self.subTest(sep=sep):
                body = f"The lease is not a lock{sep} it is a note.\n"
                self.assertEqual(self.kinds(body), [prose.KIND_ANTITHESIS])

    def test_not_merely_x_but_y_hits(self):
        for qualifier in ("merely", "just", "simply"):
            with self.subTest(qualifier=qualifier):
                body = f"It is not {qualifier} a cache but a record.\n"
                self.assertIn(prose.KIND_ANTITHESIS, self.kinds(body))

    def test_plain_technical_contrast_does_not(self):
        body = "AgentProfile is a namespaced resource, not cluster-scoped.\n"
        self.assertEqual(self.kinds(body), [])

    def test_plain_is_not_does_not(self):
        body = "The flag is not required. It is read once.\n"
        self.assertEqual(self.kinds(body), [])

    def test_antithesis_in_a_table_cell_hits(self):
        body = "| a | b |\n| --- | --- |\n| x | The lease is not a lock, it is a note. |\n"
        self.assertEqual(self.kinds(body), [prose.KIND_ANTITHESIS])


class StructuralCeilingTest(SiteFixture):
    def test_paragraph_at_the_ceiling_passes(self):
        self.assertEqual(self.kinds(words(prose.PARAGRAPH_WORD_LIMIT) + "\n"), [])

    def test_paragraph_over_the_ceiling_fails(self):
        self.assertEqual(self.kinds(words(prose.PARAGRAPH_WORD_LIMIT + 1) + "\n"), [prose.KIND_PARAGRAPH])

    def test_blank_line_heading_list_item_and_table_row_end_a_paragraph(self):
        half = words(prose.PARAGRAPH_WORD_LIMIT // 2 + 1)
        for body in (
            f"{half}\n\n{half}\n",
            f"{half}\n## Heading\n{half}\n",
            f"{half}\n- {half}\n",
            f"{half}\n* {half}\n",
            f"{half}\n1. {half}\n",
            f"{half}\n| a |\n| --- |\n{half}\n",
        ):
            with self.subTest(body=body[:40]):
                self.assertEqual(self.kinds(body), [])

    def test_directive_line_ends_a_paragraph_and_adds_no_words_to_the_aside(self):
        """A Starlight `:::note[Title]` line and its `:::` closer bound the aside like a heading."""
        half = words(prose.PARAGRAPH_WORD_LIMIT // 2 + 1)
        at_limit = one_sentence(prose.SENTENCE_WORD_LIMIT)
        for body in (
            f":::note\n{at_limit}.\n:::\n",
            f":::caution[Grants nothing today]\n{at_limit}.\n:::\n",
            f"{half}\n:::note\n{half}\n:::\n{half}\n",
            f"- item\n  :::note[Upgrade note: a title]\n  {at_limit}.\n  :::\n",
        ):
            with self.subTest(body=body[:40]):
                self.assertEqual(self.kinds(body), [])
        self.assertEqual(self.kinds(":::note[Simply put]\nText.\n:::\n"), [prose.KIND_BANNED_WORD])

    def test_wrapped_lines_are_one_paragraph(self):
        half = words(prose.PARAGRAPH_WORD_LIMIT // 2 + 1)
        self.assertEqual(self.kinds(f"{half}\n{half}\n"), [prose.KIND_PARAGRAPH])

    def test_table_cell_at_the_ceiling_passes(self):
        body = f"| a | b |\n| --- | --- |\n| x | {words(prose.CELL_WORD_LIMIT)} |\n"
        self.assertEqual(self.kinds(body), [])

    def test_table_cell_over_the_ceiling_fails(self):
        body = f"| a | b |\n| --- | --- |\n| x | {words(prose.CELL_WORD_LIMIT + 1)} |\n"
        self.assertEqual(self.kinds(body), [prose.KIND_CELL])

    def test_sentence_at_the_ceiling_passes(self):
        self.assertEqual(self.kinds(one_sentence(prose.SENTENCE_WORD_LIMIT) + ". Short.\n"), [])

    def test_sentence_over_the_ceiling_fails(self):
        body = one_sentence(prose.SENTENCE_WORD_LIMIT + 1) + ". Short.\n"
        self.assertEqual(self.kinds(body), [prose.KIND_SENTENCE])

    def test_long_sentence_names_its_own_line_and_opening_words(self):
        body = "First line is short.\nSecond line is short too.\nThird starts here " + one_sentence(prose.SENTENCE_WORD_LIMIT) + ".\n"
        finding = self.findings(body)[0]
        frontmatter_lines = FRONTMATTER.count("\n")
        self.assertEqual(finding.kind, prose.KIND_SENTENCE)
        self.assertEqual(finding.line, frontmatter_lines + 3)
        self.assertIn("Third starts here word word word", finding.detail)

    def test_abbreviation_and_version_do_not_split_a_sentence(self):
        text = "Use e.g. `flag` and v1.2 (i.e. `two`). " + one_sentence(prose.SENTENCE_WORD_LIMIT - 5) + "."
        self.assertEqual(self.kinds(text + "\n"), [prose.KIND_SENTENCE])

    def test_bare_url_keeps_the_sentence_end(self):
        for url in ("https://example.com/docs", "<https://example.com/>"):
            with self.subTest(url=url):
                body = f"Read {url}. Then " + one_sentence(prose.SENTENCE_WORD_LIMIT - 1) + ".\n"
                self.assertEqual(self.kinds(body), [])

    def test_code_span_counts_as_one_word(self):
        spans = " ".join(["`a.very.long.identifier/with-parts`"] * prose.SENTENCE_WORD_LIMIT)
        self.assertEqual(self.kinds(spans + ".\n"), [])

    def test_counts_are_per_kind(self):
        body = words(prose.PARAGRAPH_WORD_LIMIT) + " " + one_sentence(prose.SENTENCE_WORD_LIMIT + 1) + ".\n"
        counts = prose.counts_of(self.findings(body))
        self.assertEqual(counts, {prose.KIND_PARAGRAPH: 1, prose.KIND_SENTENCE: 1})


class RatchetTest(SiteFixture):
    def test_clean_site_with_no_ratchet_file_passes(self):
        self.write_page("Fine.\n")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)

    def test_page_without_an_entry_must_be_clean(self):
        self.write_page("This is simply text.\n", "new.md")
        self.ratchet.write_text("{}\n", encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("new.md: banned-word 1 > ceiling 0", out)
        self.assertIn("fix the prose", out)
        self.assertIn("simply", out)

    def test_recorded_count_passes(self):
        self.write_page("This is simply text.\n")
        self.ratchet.write_text(json.dumps({"page.md": {prose.KIND_BANNED_WORD: 1}}), encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)

    def test_regression_above_the_ceiling_fails_and_lists_the_findings(self):
        self.write_page("This is simply text.\nA robust one.\n")
        self.ratchet.write_text(json.dumps({"page.md": {prose.KIND_BANNED_WORD: 1}}), encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("banned-word 2 > ceiling 1", out)
        self.assertIn("simply", out)
        self.assertIn("robust", out)

    def test_unrecorded_improvement_fails(self):
        self.write_page("Fine.\n")
        self.ratchet.write_text(json.dumps({"page.md": {prose.KIND_BANNED_WORD: 1}}), encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("banned-word 0 < ceiling 1", out)
        self.assertIn("lower the entry", out)

    def test_ceiling_is_per_kind_not_per_page(self):
        """A banned word traded for an over-long sentence is a regression and an improvement."""
        self.write_page(one_sentence(prose.SENTENCE_WORD_LIMIT + 1) + ".\n")
        self.ratchet.write_text(json.dumps({"page.md": {prose.KIND_BANNED_WORD: 1}}), encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("banned-word 0 < ceiling 1", out)
        self.assertIn("long-sentence 1 > ceiling 0", out)

    def test_entry_for_a_deleted_page_fails(self):
        self.write_page("Fine.\n")
        self.ratchet.write_text(json.dumps({"gone.md": {prose.KIND_BANNED_WORD: 1}}), encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("gone.md", out)
        self.assertIn("no such page", out)

    def test_malformed_ratchet_fails(self):
        self.write_page("Fine.\n")
        for text in ("[]", '{"page.md": {"no-such-kind": 1}}', '{"page.md": {"banned-word": "1"}}'):
            with self.subTest(text=text):
                self.ratchet.write_text(text, encoding="utf-8")
                code, out = self.run_main()
                self.assertEqual(code, 1)
                self.assertIn("ERROR", out)

    def test_write_ratchet_on_a_malformed_ratchet_reports_and_leaves_it(self):
        self.write_page("Fine.\n")
        for text in ("{not json", "[]"):
            with self.subTest(text=text):
                self.ratchet.write_text(text, encoding="utf-8")
                code, out = self.run_main(prose.WRITE_RATCHET_FLAG)
                self.assertEqual(code, 1)
                self.assertIn("ERROR", out)
                self.assertEqual(self.ratchet.read_text(encoding="utf-8"), text)

    def test_write_ratchet_lowers_and_drops(self):
        self.write_page("This is simply text.\n")
        self.write_page("Fine.\n", "clean.md")
        self.ratchet.write_text(
            json.dumps({"page.md": {prose.KIND_BANNED_WORD: 3, prose.KIND_SENTENCE: 2}, "clean.md": {prose.KIND_BANNED_WORD: 1}}),
            encoding="utf-8",
        )
        code, _ = self.run_main(prose.WRITE_RATCHET_FLAG)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.ratchet.read_text(encoding="utf-8")), {"page.md": {prose.KIND_BANNED_WORD: 1}})
        code, out = self.run_main()
        self.assertEqual(code, 0, out)

    def test_write_ratchet_never_raises_or_adds(self):
        self.write_page("This is simply text. A robust one.\n")
        self.write_page("Also simply.\n", "new.md")
        self.ratchet.write_text(json.dumps({"page.md": {prose.KIND_BANNED_WORD: 1}}), encoding="utf-8")
        code, _ = self.run_main(prose.WRITE_RATCHET_FLAG)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.ratchet.read_text(encoding="utf-8")), {"page.md": {prose.KIND_BANNED_WORD: 1}})
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("page.md: banned-word 2 > ceiling 1", out)
        self.assertIn("new.md: banned-word 1 > ceiling 0", out)

    def test_write_ratchet_drops_a_deleted_page(self):
        self.write_page("Fine.\n")
        self.ratchet.write_text(json.dumps({"gone.md": {prose.KIND_BANNED_WORD: 1}}), encoding="utf-8")
        self.run_main(prose.WRITE_RATCHET_FLAG)
        self.assertEqual(json.loads(self.ratchet.read_text(encoding="utf-8")), {})

    def test_no_pages_is_an_error_not_a_pass(self):
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)
        self.assertIn("no .md/.mdx page", out)

    def test_page_key_is_site_relative_posix(self):
        self.write_page("Fine.\n", "deep/dir/page.mdx")
        self.assertEqual(list(prose.scan(self.site)), ["deep/dir/page.mdx"])


class RealSiteTest(unittest.TestCase):
    """The committed ratchet against the tree, and its canonical form."""

    def test_committed_ratchet_matches_the_site(self):
        """What the check in CI sees: every page at its ceiling, none above or below."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = prose.main([])
        self.assertEqual(code, 0, out.getvalue())

    def test_ratchet_file_is_in_canonical_form(self):
        """Sorted pages, kinds in report order, zero entries omitted, so a rewrite is a clean diff."""
        text = prose.RATCHET.read_text(encoding="utf-8")
        self.assertEqual(text, prose.dump_ratchet(json.loads(text)))
        for page, kinds in json.loads(text).items():
            self.assertTrue(kinds, page)
            self.assertNotIn(0, kinds.values(), page)


if __name__ == "__main__":
    unittest.main()
