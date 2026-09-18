"""The cron-job example region, and a region spliced into more than one page.

``generate_docs.py`` renders one Platform Agent roster entry as the fenced JSON
three site pages show as the job schema's worked example. The three used to be
hand-pasted copies, and a guard compared each to the roster; generating them
is what retired the guard, so these cases hold the generator to the properties
the guard used to check: the example is the roster's entry and nothing else, a
missing id fails the run rather than rendering nothing, and one body reaches
every page registered for the block.

Run:
  python3 -m unittest discover -s scripts -p 'test_generate_docs.py' -v
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

import generate_docs

FENCE_RE = re.compile(r"\A```json\n(.*)\n```\Z", re.S)


def roster_entry(job_id: str) -> dict:
    data = json.loads(generate_docs.PLATFORM_CRON_ROSTER.read_text(encoding="utf-8"))
    jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data
    return next(job for job in jobs if job["id"] == job_id)


class CronJobExampleTest(unittest.TestCase):
    def test_the_example_is_the_roster_entry_in_a_json_fence(self):
        body = generate_docs.gen_cron_job_example()
        fence = FENCE_RE.match(body)
        self.assertIsNotNone(fence, body)
        rendered = json.loads(fence.group(1))
        self.assertEqual(rendered, roster_entry(generate_docs.EXAMPLE_JOB_ID))
        # Key order is the roster's, not sorted: the pages present the entry
        # the way the file spells it.
        self.assertEqual(
            list(rendered), list(roster_entry(generate_docs.EXAMPLE_JOB_ID))
        )

    def test_the_prompt_is_rendered_verbatim_not_escaped(self):
        # The roster prompts carry an em dash. Rendered with the default
        # ``ensure_ascii`` the page would show ``—`` where the file shows
        # a dash, and a reader copying the example would copy the escape.
        body = generate_docs.gen_cron_job_example()
        prompt = roster_entry(generate_docs.EXAMPLE_JOB_ID)["prompt"]
        self.assertIn(prompt, body)
        self.assertNotIn("\\u", body)

    def test_an_id_the_roster_lacks_fails_the_run(self):
        with self.assertRaises(SystemExit) as caught:
            generate_docs.gen_cron_job_example("no-such-job")
        self.assertIn("no-such-job", str(caught.exception))

    def test_the_example_block_is_registered_for_the_three_pages(self):
        paths, generator = generate_docs.BLOCKS["cron-job-example"]
        self.assertIs(generator, generate_docs.gen_cron_job_example)
        self.assertEqual(
            set(paths),
            {
                generate_docs.WATCHDOGS_PAGE,
                generate_docs.SKILLS_CONCEPT_PAGE,
                generate_docs.CRON_PAGE,
            },
        )


class MultiPageBlockTest(unittest.TestCase):
    """A block registered for several files is spliced into each of them."""

    def test_one_body_reaches_every_registered_file(self):
        calls = []

        def generator() -> str:
            calls.append(1)
            return "GENERATED BODY"

        with tempfile.TemporaryDirectory() as tmp:
            pages = [Path(tmp) / f"page{index}.md" for index in range(2)]
            for page in pages:
                page.write_text(
                    "# Page\n\n"
                    "<!-- BEGIN GENERATED: example -->\n"
                    "stale\n"
                    "<!-- END GENERATED: example -->\n",
                    encoding="utf-8",
                )
            targets = generate_docs.collect_targets(
                blocks={"example": (tuple(pages), generator)}, files={}
            )
        self.assertEqual(len(calls), 1, "the generator ran once per block, not per page")
        self.assertEqual([path for path, _, _, _ in targets], pages)
        for path, new_text, changed, block_id in targets:
            self.assertTrue(changed, path)
            self.assertEqual(block_id, "example")
            self.assertIn("GENERATED BODY", new_text)
            self.assertNotIn("stale", new_text)
            self.assertTrue(new_text.startswith("# Page\n\n"), new_text)


if __name__ == "__main__":
    unittest.main()
