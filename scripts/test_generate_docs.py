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
from unittest import mock

import generate_docs

FENCE_RE = re.compile(r"\A```json\n(.*)\n```\Z", re.S)


def roster_entry(job_id: str) -> dict:
    jobs = generate_docs.load_roster(generate_docs.PLATFORM_CRON_ROSTER)
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
                blocks={"example": (tuple(pages), generator)}
            )
        self.assertEqual(len(calls), 1, "the generator ran once per block, not per page")
        self.assertEqual([path for path, _, _, _ in targets], pages)
        for path, new_text, changed, block_id in targets:
            self.assertTrue(changed, path)
            self.assertEqual(block_id, "example")
            self.assertIn("GENERATED BODY", new_text)
            self.assertNotIn("stale", new_text)
            self.assertTrue(new_text.startswith("# Page\n\n"), new_text)

    def test_two_blocks_on_one_page_accumulate(self):
        # `reference/cron-jobs.md` carries the jobs table and the job example.
        # Spliced each from a fresh read of the disk, the second block's text
        # still held the first block's stale region, and writing the targets
        # in order put that stale region back: one run of the generator left
        # `--check` red on the table. Each block splices into what the block
        # before it produced, so the page's last target carries both.
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.md"
            page.write_text(
                "<!-- BEGIN GENERATED: first -->\n"
                "stale first\n"
                "<!-- END GENERATED: first -->\n\n"
                "<!-- BEGIN GENERATED: second -->\n"
                "stale second\n"
                "<!-- END GENERATED: second -->\n",
                encoding="utf-8",
            )
            targets = generate_docs.collect_targets(
                blocks={
                    "first": ((page,), lambda: "NEW FIRST"),
                    "second": ((page,), lambda: "NEW SECOND"),
                },
            )
        self.assertEqual([block for _, _, _, block in targets], ["first", "second"])
        self.assertTrue(all(changed for _, _, changed, _ in targets))
        _, last_text, _, _ = targets[-1]
        self.assertIn("NEW FIRST", last_text)
        self.assertIn("NEW SECOND", last_text)
        self.assertNotIn("stale", last_text)

    def test_an_unchanged_block_beside_a_changed_one_is_reported_unchanged(self):
        # `changed` stays per block: the report names the region that moved,
        # and a block whose region is already current is not written as new
        # because the other block on the page changed.
        current = (
            "<!-- BEGIN GENERATED: first -->\n"
            "<!-- Regenerate with: make docs-generate -- do not edit by hand. -->\n"
            "<!-- prettier-ignore-start -->\n\n"
            "SAME\n\n"
            "<!-- prettier-ignore-end -->\n"
            "<!-- END GENERATED: first -->"
        )
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.md"
            page.write_text(
                f"{current}\n\n"
                "<!-- BEGIN GENERATED: second -->\n"
                "stale second\n"
                "<!-- END GENERATED: second -->\n",
                encoding="utf-8",
            )
            targets = generate_docs.collect_targets(
                blocks={
                    "first": ((page,), lambda: "SAME"),
                    "second": ((page,), lambda: "NEW SECOND"),
                },
            )
        self.assertEqual(
            [(block, changed) for _, _, changed, block in targets],
            [("first", False), ("second", True)],
        )


def audit_job(job_id: str, schedule: dict, **extra) -> dict:
    job = {
        "id": job_id,
        "name": job_id,
        "schedule": schedule,
        "prompt": f"Read the SOP at 'governance/{job_id}_sop.md'.",
        "skills": [generate_docs.AUDIT_SKILL],
        "enabled": True,
    }
    job.update(extra)
    return job


class CapabilitiesTest(unittest.TestCase):
    """The README's capabilities matrix lists every audit and every skill."""

    def test_the_block_is_registered_for_the_readme(self):
        # Without this entry `--check` stops reading the README region and the
        # table goes stale with every other test still green.
        self.assertEqual(
            generate_docs.BLOCKS["capabilities"],
            ((generate_docs.README,), generate_docs.gen_capabilities),
        )

    def test_each_enabled_audit_is_listed_with_a_named_cadence(self):
        jobs = [
            job
            for job in generate_docs.load_roster(generate_docs.PLATFORM_CRON_ROSTER)
            if job.get("enabled") and generate_docs.AUDIT_SKILL in job.get("skills", [])
        ]
        audits = generate_docs.governance_audits()
        self.assertEqual(sorted(a["id"] for a in audits), sorted(j["id"] for j in jobs))
        body = generate_docs.gen_capabilities()
        for audit in audits:
            cadence = generate_docs.CRON_CADENCE[audit["expr"]]
            self.assertIn(f"| {generate_docs.md_escape(audit['name'])} | {cadence} |", body)

    def test_script_jobs_are_left_out(self):
        ids = {a["id"] for a in generate_docs.governance_audits()}
        for job in generate_docs.load_roster(generate_docs.PLATFORM_CRON_ROSTER):
            if job.get("no_agent"):
                self.assertNotIn(job["id"], ids)

    def test_every_skill_is_listed(self):
        skills = generate_docs.gen_capabilities().split("### Skills by area")[1]
        names = {
            *generate_docs._read_skills(generate_docs.SKILLS_DIR),
            *generate_docs._read_skills(generate_docs.CLUSTER_SKILLS_DIR),
        }
        for name in names:
            self.assertIn(f"`{name}`", skills)

    def test_daily_audits_come_first_and_untimed_schedules_last(self):
        roster = [
            audit_job("step", {"expr": "0 */6 * * *"}),
            audit_job("interval", {"kind": "interval", "minutes": 30, "display": "every 30m"}),
            audit_job("null-expr", {"expr": None}),
            audit_job("unicode-digit", {"expr": "0 \u00b2 * * *"}),
            audit_job("weekly", {"expr": "0 7 * * 1"}),
            audit_job("january", {"expr": "0 8 * 1 *"}),
            audit_job("monthly", {"expr": "0 9 1 * *"}),
            audit_job("daily-late", {"expr": "0 10 * * *"}),
            audit_job("daily-early", {"expr": "30 6 * * *"}),
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            order = [a["id"] for a in generate_docs.governance_audits()]
            body = generate_docs.gen_capabilities()
        self.assertEqual(
            order,
            [
                "daily-early",
                "daily-late",
                "weekly",
                "january",
                "monthly",
                "step",
                "interval",
                "null-expr",
                "unicode-digit",
            ],
        )
        self.assertIn("| interval | `every 30m` |", body)

    def test_a_cell_is_escaped_and_a_missing_name_falls_back_to_the_id(self):
        roster = [
            audit_job("piped", {"expr": "20 6 * * *"}, name="A | B <x>"),
            audit_job("nameless", {"expr": "0 7 * * *"}, name=None),
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            body = generate_docs.gen_capabilities()
        self.assertIn("| A \\| B &lt;x&gt; | Daily 06:20 |", body)
        self.assertIn("| nameless |", body)

    def test_disabled_and_skill_less_jobs_are_left_out(self):
        roster = [
            audit_job("off", {"expr": "0 6 * * *"}, enabled=False),
            audit_job("no-skill", {"expr": "0 6 * * *"}, skills=[]),
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            self.assertEqual(generate_docs.governance_audits(), [])

    def test_an_audit_whose_prompt_names_no_sop_fails_the_run(self):
        roster = [audit_job("lost", {"expr": "0 6 * * *"}, prompt="Run the audit.")]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            with self.assertRaises(SystemExit) as caught:
                generate_docs.governance_audits()
        self.assertIn("lost", str(caught.exception))

    def test_a_hyphenated_or_capitalised_sop_name_is_linked(self):
        roster = [
            audit_job("odd-name", {"expr": "0 6 * * *"}, prompt="Read 'governance/Odd-Name_SOP.md'.")
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            body = generate_docs.gen_capabilities()
        self.assertIn(
            "[`Odd-Name_SOP.md`](agents/platform/governance/Odd-Name_SOP.md)", body
        )


if __name__ == "__main__":
    unittest.main()
