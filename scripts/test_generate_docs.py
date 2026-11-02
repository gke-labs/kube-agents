"""The cron-job example region, a region spliced into more than one page, and the CRD field pages.

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

import contextlib
import io
import json
import re
import shutil
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
            if job.get("enabled") and generate_docs.is_audit(job)
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
            *generate_docs._read_skills(generate_docs.TSE_SKILLS_DIR),
        }
        self.assertTrue(names)
        for name in names:
            self.assertIn(f"`{name}`", skills)

    def test_the_catalog_lists_the_support_agent_skills_under_their_own_group(self):
        tse_skills = generate_docs._read_skills(generate_docs.TSE_SKILLS_DIR)
        self.assertTrue(tse_skills, "agents/tse/skills holds no SKILL.md")
        body = generate_docs.gen_skill_catalog()
        group = body.split(f"### {generate_docs.TSE_SKILL_GROUP}\n")[1]
        for name in tse_skills:
            self.assertIn(
                f"[`{name}`]({generate_docs.GITHUB_BLOB}/agents/tse/skills/{name}/SKILL.md)",
                group,
            )
        self.assertTrue(body.rstrip().endswith(group.rstrip()))

    def test_a_name_shared_across_personas_keeps_each_directory_s_description(self):
        shared = next(iter(generate_docs.SKILL_GROUPS.values()))[0]
        by_dir = {
            generate_docs.SKILLS_DIR: {shared: "platform description"},
            generate_docs.CLUSTER_SKILLS_DIR: {shared: "cluster description"},
            generate_docs.TSE_SKILLS_DIR: {},
        }
        with mock.patch.object(generate_docs, "_read_skills", side_effect=by_dir.__getitem__):
            body = generate_docs.gen_skill_catalog()
        platform_rows, cluster_rows = body.split(f"### {generate_docs.CLUSTER_SKILL_GROUP}\n")
        blob = generate_docs.GITHUB_BLOB
        self.assertIn(
            f"| [`{shared}`]({blob}/agents/platform/skills/{shared}/SKILL.md) | platform description |",
            platform_rows,
        )
        self.assertIn(
            f"| [`{shared}`]({blob}/agents/cluster/skills/{shared}/SKILL.md) | cluster description |",
            cluster_rows,
        )
        self.assertNotIn("cluster description", platform_rows)

    def test_a_persona_group_with_no_skills_is_left_out(self):
        grouped = generate_docs.group_skills({"fleet-audit": "d"}, {})
        for group, _ in generate_docs.PERSONA_SKILL_GROUPS:
            self.assertEqual(grouped[group], [])
        with mock.patch.object(generate_docs, "read_persona_skills", return_value={}):
            body = generate_docs.gen_skill_catalog()
        self.assertNotIn(generate_docs.TSE_SKILL_GROUP, body)
        self.assertNotIn(generate_docs.CLUSTER_SKILL_GROUP, body)

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


def helper_job(job_id: str, expr: str, **overrides) -> dict:
    job = {
        "id": job_id,
        "name": job_id.replace("-", " ").title(),
        "schedule": {"kind": "cron", "expr": expr, "display": expr},
        "skills": [],
        "no_agent": True,
        "script": f"{job_id.replace('-', '_')}.py",
        "enabled": True,
    }
    job.update(overrides)
    return job


class CronHelperJobsTest(unittest.TestCase):
    """The watchdogs page's helper-job table is the roster's non-audit entries."""

    def test_the_block_is_registered_for_the_watchdogs_page(self):
        self.assertEqual(
            generate_docs.BLOCKS["cron-helper-jobs"],
            ((generate_docs.WATCHDOGS_PAGE,), generate_docs.gen_cron_helper_jobs),
        )

    def test_exactly_the_enabled_non_audit_jobs_are_listed_in_roster_order(self):
        roster = generate_docs.load_roster(generate_docs.PLATFORM_CRON_ROSTER)
        expected = [job["id"] for job in roster if job.get("enabled") and not generate_docs.is_audit(job)]
        self.assertTrue(expected)
        body = generate_docs.gen_cron_helper_jobs()
        listed = re.findall(r"^\| `([^`]+)` \|", body, re.M)
        self.assertEqual(listed, expected)
        for job in roster:
            if generate_docs.is_audit(job):
                self.assertNotIn(f"`{job['id']}`", body)

    def test_helpers_and_audits_partition_the_enabled_roster(self):
        # The two regions share `is_audit`; this holds them to the roster from
        # the other side, so neither can gain or lose an entry on its own.
        enabled = [
            job["id"]
            for job in generate_docs.load_roster(generate_docs.PLATFORM_CRON_ROSTER)
            if job.get("enabled")
        ]
        helpers = [job["id"] for job in generate_docs.helper_jobs()]
        audits = [audit["id"] for audit in generate_docs.governance_audits()]
        self.assertEqual(sorted(helpers + audits), sorted(enabled))
        self.assertFalse(set(helpers) & set(audits))

    def test_a_null_skills_entry_is_a_helper_in_both_regions(self):
        # `jobs.json` readers elsewhere in the tree accept `"skills": null`;
        # the helper filter used to raise `TypeError` on it while the audit
        # filter read it as no skills.
        roster = [
            helper_job("no-skills", "0 6 * * *", skills=None),
            audit_job("audit", {"expr": "0 6 * * *"}),
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            body = generate_docs.gen_cron_helper_jobs()
            audits = [audit["id"] for audit in generate_docs.governance_audits()]
        self.assertIn("| `no-skills` | script, `no_skills.py` |", body)
        self.assertNotIn("`audit`", body)
        self.assertEqual(audits, ["audit"])

    def test_disabled_and_audit_entries_are_left_out(self):
        roster = [
            helper_job("off", "0 6 * * *", enabled=False),
            helper_job("audit", "0 6 * * *", no_agent=None, skills=[generate_docs.AUDIT_SKILL]),
            helper_job("kept", "0 6 * * *"),
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            body = generate_docs.gen_cron_helper_jobs()
        self.assertNotIn("`off`", body)
        self.assertNotIn("`audit`", body)
        self.assertIn("| `kept` | script, `kept.py` |", body)

    def test_the_type_column_tells_a_script_from_a_prompt_job(self):
        roster = [
            helper_job("plain-prompt", "0 7 * * 6", no_agent=None, script=None),
            helper_job("prompt-after", "0 7 * * 6", no_agent=None, script="queue.py"),
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            body = generate_docs.gen_cron_helper_jobs()
        self.assertIn("| `plain-prompt` | prompt | Weekly, Saturday 07:00 |", body)
        self.assertIn("| `prompt-after` | prompt, after `queue.py` |", body)

    def test_an_unglossed_schedule_shows_its_expression_and_a_name_is_escaped(self):
        roster = [
            helper_job("odd", "7 3 * * 2", name="A | B <x>"),
            helper_job("interval", "", schedule={"kind": "interval", "minutes": 5, "display": "every 5m"}),
        ]
        with mock.patch.object(generate_docs, "load_roster", return_value=roster):
            body = generate_docs.gen_cron_helper_jobs()
        self.assertIn("| `odd` | script, `odd.py` | `7 3 * * 2` | A \\| B &lt;x&gt; |", body)
        self.assertIn("| `every 5m` |", body)


class GovernanceAuditsTest(unittest.TestCase):
    """The governance page's matrix is the roster's audits, in README order."""

    def test_the_block_is_registered_for_the_governance_page(self):
        self.assertEqual(
            generate_docs.BLOCKS["governance-audits"],
            ((generate_docs.GOVERNANCE_PAGE,), generate_docs.gen_governance_audits),
        )

    def test_each_audit_is_a_row_with_its_sop_watchdog_cadence_and_name(self):
        audits = generate_docs.governance_audits()
        self.assertTrue(audits)
        body = generate_docs.gen_governance_audits()
        rows = [line for line in body.splitlines()[2:]]
        self.assertEqual(len(rows), len(audits))
        for audit, row in zip(audits, rows):
            self.assertEqual(
                row,
                "| `{sop}` | `{id}` | {cadence} | {name} |".format(
                    sop=Path(audit["sop"]).name,
                    id=audit["id"],
                    cadence=generate_docs.CRON_CADENCE[audit["expr"]],
                    name=generate_docs.md_escape(audit["name"]),
                ),
            )

    def test_script_jobs_are_not_in_the_matrix(self):
        body = generate_docs.gen_governance_audits()
        for job in generate_docs.load_roster(generate_docs.PLATFORM_CRON_ROSTER):
            if job.get("no_agent"):
                self.assertNotIn(f"`{job['id']}`", body)


class CadenceCellTest(unittest.TestCase):
    """One helper decides what every generated cadence cell shows."""

    def test_a_glossed_expression_shows_its_gloss(self):
        self.assertEqual(generate_docs.cadence_cell({"expr": "20 6 * * *"}), "Daily 06:20")

    def test_an_unglossed_expression_shows_itself_or_the_fallback(self):
        self.assertEqual(generate_docs.cadence_cell({"expr": "7 3 * * 2"}), "`7 3 * * 2`")
        self.assertEqual(generate_docs.cadence_cell({"expr": "7 3 * * 2"}, "—"), "—")

    def test_an_interval_schedule_shows_its_display(self):
        schedule = {"kind": "interval", "minutes": 5, "display": "every 5m"}
        self.assertEqual(generate_docs.cadence_cell(schedule), "`every 5m`")
        self.assertEqual(generate_docs.schedule_expr(schedule), ("", "every 5m"))


class CRDFieldBlocksTest(unittest.TestCase):
    """The three CRD field pages are registered, rendered as tables, and selectable."""

    def test_each_kind_has_a_block_on_its_own_page(self):
        for kind, page in generate_docs.CRD_KINDS.items():
            block_id = f"{generate_docs.CRD_BLOCK_PREFIX}{kind.lower()}"
            paths, generator = generate_docs.BLOCKS[block_id]
            self.assertEqual(paths, (page,))
            self.assertEqual(generator.func, generate_docs.gen_crd_fields)
            self.assertEqual(generator.args, (kind,))
            self.assertTrue(page.exists(), page)

    def test_select_blocks_keeps_registry_order_and_rejects_unknown_ids(self):
        blocks = {"a": 1, "b": 2, "crd-fields-x": 3, "crd-fields-y": 4}
        self.assertEqual(generate_docs.select_blocks([], [], blocks), blocks)
        self.assertEqual(list(generate_docs.select_blocks(["crd-fields-x", "a"], [], blocks)), ["a", "crd-fields-x"])
        # A prefix covers a family, so the operator Makefile names it once.
        self.assertEqual(
            list(generate_docs.select_blocks([], ["crd-fields-"], blocks)), ["crd-fields-x", "crd-fields-y"]
        )
        self.assertEqual(list(generate_docs.select_blocks(["a"], ["crd-"], blocks)), ["a", "crd-fields-x", "crd-fields-y"])
        with self.assertRaises(SystemExit) as caught:
            generate_docs.select_blocks(["a", "nope"], [], blocks)
        self.assertIn("nope", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            generate_docs.select_blocks([], ["zzz-"], blocks)
        self.assertIn("zzz-", str(caught.exception))

    def test_without_go_a_write_run_skips_the_crd_pages_and_a_check_fails(self):
        def crd_generator() -> str:
            return generate_docs.run_crd_generator(generate_docs.CRD_API_DIR, ("AgentPlugin",))

        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.md"
            crd = Path(tmp) / "crd.md"
            for page, block_id in ((plain, "plain"), (crd, "crd-fields-x")):
                page.write_text(
                    f"<!-- BEGIN GENERATED: {block_id} -->\nstale\n<!-- END GENERATED: {block_id} -->\n",
                    encoding="utf-8",
                )
            blocks = {"plain": ((plain,), lambda: "FRESH"), "crd-fields-x": ((crd,), crd_generator)}
            out = io.StringIO()
            with mock.patch.object(generate_docs.shutil, "which", return_value=None):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(generate_docs.main([], blocks), 0)
                    self.assertIn("FRESH", plain.read_text(encoding="utf-8"))
                    self.assertIn("stale", crd.read_text(encoding="utf-8"))
                    self.assertIn("skipped  [crd-fields-x]", out.getvalue())
                    with self.assertRaises(SystemExit):
                        generate_docs.main(["--check"], blocks)
                    with self.assertRaises(SystemExit):
                        generate_docs.main(["crd-fields-x"], blocks)

    @unittest.skipIf(shutil.which(generate_docs.GO_BINARY) is None, "needs a Go toolchain")
    def test_the_page_body_is_a_spec_and_a_status_table(self):
        body = generate_docs.gen_crd_fields("AgentPlugin")
        intro, tables = body.split("\n\n", 1)
        self.assertTrue(intro.startswith("Every field of the `AgentPlugin` resource's"), intro)
        self.assertIn("[AgentPlugin CRD](/kube-agents/operator/agentplugin-crd/)", intro)
        self.assertIn("`kubectl explain agentplugin.spec.<field>`", intro)
        self.assertIn("(`agentplugin.status.<field>` for a row of the Status table)", intro)
        # The Constraints column leaves the CEL rules out, and the intro says so
        # and where they are listed.
        self.assertIn("`kubectl get crd agentplugins.kubeagents.x-k8s.io -o yaml`", intro)
        self.assertIn("`x-kubernetes-validations`", intro)
        self.assertTrue(tables.startswith("## Spec\n\n" + "\n".join(generate_docs.CRD_TABLE_HEADER)))
        self.assertIn("\n## Status\n", body)
        self.assertIn("| `agentRef` | `string` | yes |  |  | ", body)
        self.assertIn("one of `Always`, `Never`, `IfNotPresent`", body)
        self.assertIn("| `env` | `[]corev1.EnvVar` |", body)
        self.assertNotIn("| `env[]", body)

    def test_a_row_is_rendered_from_the_model(self):
        with mock.patch.object(
            generate_docs,
            "crd_model",
            return_value={
                "AgentPlugin": {
                    "spec": [
                        {
                            "path": "parts[].name",
                            "name": "name",
                            "type": "string",
                            "kind": "scalar",
                            "external": False,
                            "required": True,
                            "default": "a|b",
                            "constraints": [
                                {"name": "MaxLength", "value": "63"},
                                {"name": "items:Pattern", "value": "^(a|b)$"},
                                {"name": "Enum", "value": "x;y"},
                                {"name": "MultipleOf", "value": "5"},
                                {"name": "values:Enum", "value": "p;q"},
                            ],
                            "description": "Name of the <part>, see `kind: <k>`.",
                        }
                    ],
                    "status": [],
                }
            },
        ):
            body = generate_docs.gen_crd_fields("AgentPlugin")
        self.assertIn(
            "| `parts[].name` | `string` | yes | `a\\|b` | max length `63`; items pattern `^(a\\|b)$`; "
            "one of `x`, `y`; multiple of `5`; values one of `p`, `q` | Name of the &lt;part&gt;, see `kind: <k>`. |",
            body,
        )
        self.assertTrue(body.endswith("## Status\n\n" + generate_docs.CRD_EMPTY_TREE))

    def test_escape_cell_leaves_code_spans_alone(self):
        self.assertEqual(generate_docs.escape_cell("a <b> `<c>` d|e `f|g`"), "a &lt;b&gt; `<c>` d\\|e `f\\|g`")

    def test_an_edited_region_makes_check_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.md"
            page.write_text(
                "<!-- BEGIN GENERATED: example -->\n"
                "edited by hand\n"
                "<!-- END GENERATED: example -->\n",
                encoding="utf-8",
            )
            blocks = {"example": ((page,), lambda: "FRESH")}
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(generate_docs.main(["--check"], blocks), 1)
                self.assertIn("edited by hand", page.read_text(encoding="utf-8"))
                self.assertEqual(generate_docs.main([], blocks), 0)
                self.assertIn("FRESH", page.read_text(encoding="utf-8"))
                self.assertEqual(generate_docs.main(["--check"], blocks), 0)
                self.assertEqual(generate_docs.main(["--check", "example"], blocks), 0)


if __name__ == "__main__":
    unittest.main()
