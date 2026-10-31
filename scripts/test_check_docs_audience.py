#!/usr/bin/env python3
"""Unit tests for the site-audience guard.

Run: cd scripts && python3 -m unittest test_check_docs_audience

The guard fails green: a shape that stops matching lets the next maintainer
identifier onto the public site with no signal, and a placeholder handling that
stops excusing `<project>` turns every install page red. Both directions are
pinned here, along with the derivation from hack/ci-env.sh that the denylist
relies on instead of repeating the values, the two preconditions that stop an
empty run from reporting clean, the citation shape's prose masking, and the
shrinking allowlist that holds the backlog at its count and nowhere above it.
"""

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_docs_audience

# Literals below are the shapes under test, not values the site may carry.
FAKE_DENYLIST = "# comment\n\nsecrets\\.[A-Z_]+\nvars\\.[A-Z_]+\n\\bapp_id\\b\nexample-prow-project\n"
FAKE_CI_ENV = (
    '#!/bin/bash\n'
    'export PROJECT_ID="${PROJECT_ID:-example-evals-pool}"\n'
    'export GCP_PROJECT_ID="${PROJECT_ID}"\n'
    'export OTHER_PROJECT_ID="example-literal-project"\n'
    'export PROJECT_DIR="hack"\n'
    'export HOST_CLUSTER_NAME="platform-agent-host"\n'
)

# The regex metacharacters a denylist line uses when it is a shape rather than
# a literal; a line without any of them is asserted to match its own text.
REGEX_METACHARACTERS = set("\\[](){}?*+|^$")

CITATION = check_docs_audience.CITATION_SHAPE
ISSUE_URL = "https://github.com/gke-labs/kube-agents/issues/3072"
PULL_URL = "https://github.com/gke-labs/kube-agents/pull/2801"
# The acceptance criterion: standard library only, under two seconds on the committed site.
# Measured on the script's own run, `python3 scripts/check_docs_audience.py`, in a child
# process: `make coverage` traces this process, and a tracer inflates CPU time by more than
# the budget's headroom on a loaded runner, while the child runs untraced as a user runs it.
RUN_BUDGET_SECONDS = 2.0
SCRIPT = Path(check_docs_audience.__file__)


class SiteFixture(unittest.TestCase):
    """A throwaway site root, denylist and ci-env per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.site = root / "site"
        self.site.mkdir()
        self.denylist = root / "denylist.txt"
        self.denylist.write_text(FAKE_DENYLIST, encoding="utf-8")
        self.ci_env = root / "ci-env.sh"
        self.ci_env.write_text(FAKE_CI_ENV, encoding="utf-8")
        self.allowlist = root / "allowlist.txt"
        self.allowlist.write_text("# empty\n", encoding="utf-8")

    def write_page(self, body: str, name: str = "page.md") -> None:
        page = self.site / name
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(body, encoding="utf-8")

    def findings_for(self, body: str, name: str = "page.md") -> list[check_docs_audience.Finding]:
        self.write_page(body, name)
        return check_docs_audience.scan(self.site, self.denylist, self.ci_env)

    def shapes_for(self, body: str, name: str = "page.md") -> list[str]:
        return [f.shape for f in self.findings_for(body, name)]

    def citations_for(self, body: str, name: str = "page.md") -> list[tuple[int, str]]:
        return [(f.line, f.text) for f in self.findings_for(body, name) if f.shape == CITATION]

    def mdx_citations_for(self, body: str) -> list[tuple[int, str]]:
        """The same body as the one `.mdx` page of the site, the default `.md` page removed first."""
        (self.site / "page.md").unlink(missing_ok=True)
        return self.citations_for(body, name="page.mdx")


class DenylistShapesTest(SiteFixture):
    def test_workflow_secret_reference_fails(self):
        self.assertTrue(any("secrets" in s for s in self.shapes_for("uses ${{ secrets.DEPLOY_KEY }}")))

    def test_workflow_variable_reference_fails(self):
        self.assertTrue(any("vars" in s for s in self.shapes_for("reads vars.GCP_PROJECT")))

    def test_app_id_key_fails(self):
        self.assertTrue(any("app_id" in s for s in self.shapes_for("app_id = 123")))

    def test_literal_denylist_entry_fails(self):
        self.assertTrue(any("example-prow-project" in s for s in self.shapes_for("runs in example-prow-project")))

    def test_ci_env_default_project_fails(self):
        shapes = self.shapes_for("the pool is example-evals-pool")
        self.assertEqual(shapes, [check_docs_audience.CI_ENV_SHAPE])

    def test_ci_env_literal_project_fails(self):
        shapes = self.shapes_for("see example-literal-project")
        self.assertEqual(shapes, [check_docs_audience.CI_ENV_SHAPE])

    def test_pool_member_suffix_still_fails(self):
        """A numbered pool project contains the derived ID at a word boundary."""
        self.assertEqual(self.shapes_for("leased example-evals-pool-3"), [check_docs_audience.CI_ENV_SHAPE])

    def test_derived_id_inside_another_word_passes(self):
        self.assertEqual(self.shapes_for("myexample-evals-pool"), [])

    def test_mdx_pages_are_scanned(self):
        self.assertTrue(self.shapes_for("app_id: 1", name="page.mdx"))

    def test_uppercase_placeholders_pass(self):
        """`<APP_ID>` and `GITHUB_APP_ID` are what a reader fills in, not a value."""
        self.assertEqual(self.shapes_for("--from-literal=githubAppID=<APP_ID>\n`GITHUB_APP_ID` env\n"), [])

    def test_cluster_name_and_non_project_exports_pass(self):
        """Only *PROJECT_ID exports are read: the cluster default and PROJECT_DIR are not identifiers."""
        self.assertEqual(self.shapes_for("--cluster-name=platform-agent-host in hack/"), [])

    def test_clean_page_passes(self):
        self.assertEqual(self.shapes_for("# Install\n\nRun `install.sh` in your project.\n"), [])


class ServiceAccountTest(SiteFixture):
    def test_real_project_email_fails(self):
        shapes = self.shapes_for("bind sa@internal-team-prod.iam.gserviceaccount.com")
        self.assertEqual(shapes, [check_docs_audience.SERVICE_ACCOUNT_SHAPE])

    def test_placeholder_project_emails_pass(self):
        body = "\n".join(
            [
                "gsa@<project>.iam.gserviceaccount.com",
                "gsa@<PROJECT_ID>.iam.gserviceaccount.com",
                "gsa@${PROJECT_ID}.iam.gserviceaccount.com",
                "gsa@PROJECT_ID.iam.gserviceaccount.com",
                "gsa@your-project.iam.gserviceaccount.com",
                "gsa@my-project.iam.gserviceaccount.com",
                "gsa@example-project.iam.gserviceaccount.com",
            ]
        )
        self.assertEqual(self.shapes_for(body), [])

    def test_google_service_agent_emails_pass(self):
        """A GKE prerequisite page has to be able to show Google's own service agents."""
        body = "\n".join(
            [
                "service-<PROJECT_NUMBER>@gcp-sa-gkehub.iam.gserviceaccount.com",
                "service-123456789@gcp-sa-gkehub.iam.gserviceaccount.com",
                "service-123456789@container-engine-robot.iam.gserviceaccount.com",
                "service-123456789@compute-system.iam.gserviceaccount.com",
                "123456789@cloudservices.iam.gserviceaccount.com",
                "123456789@cloudbuild.iam.gserviceaccount.com",
                "service-123456789@containerregistry.iam.gserviceaccount.com",
            ]
        )
        self.assertEqual(self.shapes_for(body), [])


class CiEnvDerivationTest(unittest.TestCase):
    def _ids(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ci-env.sh"
            path.write_text(text, encoding="utf-8")
            return check_docs_audience.ci_env_project_ids(path)

    def test_reads_default_and_literal_but_not_indirection_or_other_names(self):
        self.assertEqual(self._ids(FAKE_CI_ENV), ["example-evals-pool", "example-literal-project"])

    def test_survives_quoting_and_comment_variants(self):
        for line in (
            'export PROJECT_ID="${PROJECT_ID:-example-evals-pool}"  # the pool',
            'export PROJECT_ID=${PROJECT_ID:-"example-evals-pool"}',
            "export PROJECT_ID='example-evals-pool'",
            "export PROJECT_ID=example-evals-pool",
        ):
            with self.subTest(line=line):
                self.assertEqual(self._ids(line + "\n"), ["example-evals-pool"])

    def test_the_real_ci_env_yields_a_project(self):
        """The derivation is the denylist's only source for these values; it must keep finding one."""
        self.assertTrue(check_docs_audience.ci_env_project_ids())


class PreconditionsTest(unittest.TestCase):
    """An empty run is an error, never a clean report."""

    def test_missing_site_root_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ci_env = Path(tmp) / "ci-env.sh"
            ci_env.write_text(FAKE_CI_ENV, encoding="utf-8")
            errors = check_docs_audience.preconditions(Path(tmp) / "nowhere", ci_env)
        self.assertEqual(len(errors), 1)
        self.assertIn("no .md/.mdx page", errors[0])

    def test_ci_env_without_a_project_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site"
            site.mkdir()
            (site / "page.md").write_text("hello\n", encoding="utf-8")
            ci_env = Path(tmp) / "ci-env.sh"
            ci_env.write_text('export PROJECT_ID="${PROJECT_ID}"\n', encoding="utf-8")
            errors = check_docs_audience.preconditions(site, ci_env)
        self.assertEqual(len(errors), 1)
        self.assertIn("no project ID derived", errors[0])

    def test_the_committed_tree_meets_both(self):
        self.assertEqual(check_docs_audience.preconditions(), [])


class CommittedDenylistTest(unittest.TestCase):
    """The shapes the committed denylist claims, both directions, against the real file."""

    @classmethod
    def setUpClass(cls):
        cls.patterns = check_docs_audience.load_denylist()

    def _hits(self, text: str) -> list[str]:
        return [p.pattern for p in self.patterns if p.search(text)]

    def test_compiles_and_is_not_empty(self):
        self.assertTrue(self.patterns)

    def test_does_not_repeat_a_project_id_ci_env_exports(self):
        """The derivation owns those values; a copy here goes stale when the export changes."""
        for project in check_docs_audience.ci_env_project_ids():
            self.assertEqual([p for p in self.patterns if p.pattern == re.escape(project) or p.pattern == project], [])

    def test_every_literal_line_matches_itself(self):
        literals = []
        for pattern in self.patterns:
            text = pattern.pattern.replace("\\.", ".")
            if not set(text) & REGEX_METACHARACTERS:
                literals.append((pattern, text))
        self.assertTrue(literals, "no literal lines; the fixture assumption broke")
        for pattern, text in literals:
            with self.subTest(pattern=pattern.pattern):
                self.assertTrue(pattern.search(text))

    def test_numeric_app_and_installation_ids_are_caught(self):
        for text in ("App ID: 123456", "the installation ID 98765432", "GitHub App ID is 1234567"):
            with self.subTest(text=text):
                self.assertTrue(self._hits(text))

    def test_snake_case_keys_are_caught(self):
        for text in ("app_id = 1", "installation_id: 2"):
            with self.subTest(text=text):
                self.assertTrue(self._hits(text))

    def test_placeholders_and_user_facing_names_pass(self):
        for text in (
            "--from-literal=githubAppID=<APP_ID>",
            "`GITHUB_APP_ID` — numeric App ID.",
            "APP_ID=123456",
            "githubAppID: 123456",
            "the App ID you copied",
            "set `github_app_id` in terraform.tfvars",
            "terraform.tfvars",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._hits(text), [])


class CommittedSiteTest(unittest.TestCase):
    """The committed site against the committed allowlist: nothing outside it, every entry exact."""

    def test_the_current_site_has_nothing_outside_the_allowlist(self):
        entries, errors = check_docs_audience.load_allowlist()
        self.assertEqual(errors, [])
        findings, problems = check_docs_audience.reconcile(check_docs_audience.scan(), entries)
        self.assertEqual([repr(f) for f in findings], [])
        self.assertEqual(problems, [])

    def test_the_committed_allowlist_only_pins_the_backlog(self):
        """Every entry names a page the site has and a shape the check reports; none is a free pass.

        An empty list passes vacuously: the cleanup ends with no entry left.
        """
        entries, _ = check_docs_audience.load_allowlist()
        pages = {p.relative_to(check_docs_audience.SITE_ROOT).as_posix() for p in check_docs_audience.site_pages()}
        for entry in entries:
            with self.subTest(entry=repr(entry)):
                self.assertIn(entry.page, pages)
                self.assertIn(entry.shape, check_docs_audience.PINNABLE_SHAPES)

    def test_pinnable_shapes_are_the_citation_shape_and_one_denylist_line(self):
        """Widening this tuple is how a maintainer identifier would first pass the gate; pin it."""
        denylist_shapes = {
            check_docs_audience.DENYLIST_SHAPE.format(pattern=p.pattern) for p in check_docs_audience.load_denylist()
        }
        self.assertEqual(check_docs_audience.PINNABLE_SHAPES, (CITATION, "denylist: evalcand_"))
        self.assertIn("denylist: evalcand_", denylist_shapes)

    def test_main_reports_success(self):
        self.assertEqual(check_docs_audience.main(), 0)

    def test_main_runs_inside_the_budget(self):
        """The script's own run as a child: its CPU time, not wall clock, and not this process's tracer."""
        before = os.times()
        run = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
        after = os.times()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        spent = (after.children_user - before.children_user) + (after.children_system - before.children_system)
        self.assertLess(spent, RUN_BUDGET_SECONDS)


class CitationShapeTest(SiteFixture):
    """A pull-request or issue number in prose, and the places it is allowed to be."""

    def test_bare_number_in_prose_fails(self):
        self.assertEqual(self.citations_for("Fixed in #2801 for good.\n"), [(1, "#2801")])

    def test_url_form_fails_once(self):
        self.assertEqual(self.citations_for(f"tracked at {ISSUE_URL} today\n"), [(1, ISSUE_URL)])
        self.assertEqual(self.citations_for("at github.com/gke-labs/kube-agents/pull/2801\n"), [(1, "github.com/gke-labs/kube-agents/pull/2801")])

    def test_link_form_fails_once_not_twice(self):
        """`[#N](url)` carries the number and the URL; it is one citation."""
        self.assertEqual(self.citations_for(f"a release with [#2801]({PULL_URL}) in it\n"), [(1, f"[#2801]({PULL_URL})")])

    def test_link_text_wording_and_url_fragment_do_not_change_the_count(self):
        for body in (
            f"[PR #2801]({PULL_URL})",
            f"[#2801]({PULL_URL}#issuecomment-5)",
            f"[pull request #2801, merged]({PULL_URL}#event-1)",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.citations_for(body + "\n"), [(1, body)])

    def test_link_target_path_query_and_title_do_not_change_the_count(self):
        """Whatever Markdown admits between the URL and the closing parenthesis stays inside the one link."""
        for body in (
            f"[#2801]({PULL_URL}/files)",
            f"[#2801]({PULL_URL}/commits?diff=split)",
            f'[#2801]({PULL_URL} "merged in 0.19")',
            f"[#2801]({PULL_URL}#discussion_r1 'title')",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.citations_for(body + " and (#2802)\n"), [(1, body), (1, "#2802")])

    def test_link_text_number_to_another_target_is_one_bare_citation(self):
        self.assertEqual(self.citations_for("[PR #2801](/kube-agents/install/)\n"), [(1, "#2801")])

    def test_short_link_text_is_caught_through_its_url(self):
        self.assertEqual(self.citations_for(f"([#12]({ISSUE_URL}))\n"), [(1, ISSUE_URL)])

    def test_two_numbers_on_a_line_are_two_findings(self):
        self.assertEqual(self.citations_for("| 0.8.0 | #2010, #2042: more consumers |\n"), [(1, "#2010"), (1, "#2042")])

    def test_heading_anchors_pass(self):
        body = "See [refused](#when-an-upgrade-is-refused) and [status](/crd/#status) and [step 2](#2-install).\n"
        self.assertEqual(self.citations_for(body), [])

    def test_anchors_and_entities_starting_with_digits_pass(self):
        body = "[404 errors](#404-errors), [2026](#2026-roadmap), a space&#160;here, #2801v2\n"
        self.assertEqual(self.citations_for(body), [])

    def test_punctuation_after_a_number_still_counts(self):
        body = "#2801. #2802, #2803: (#2804) #2805; #2806-ish\n"
        self.assertEqual([t for _, t in self.citations_for(body)], ["#2801", "#2802", "#2803", "#2804", "#2805", "#2806"])

    def test_digits_only_anchor_targets_pass(self):
        """The slug of a version or year heading is digits only; a link to it is not a citation."""
        self.assertEqual(self.citations_for("[0.14.0](#0140) and [2026](#2026) and [#1](#2026)\n"), [])

    def test_anchor_targets_in_an_attribute_or_angle_brackets_pass(self):
        """The same target spelled as an `href` attribute or an angle-bracket destination is a link target too."""
        body = '<a href="#404-errors">errors</a>, <a href=\'#2026\'>year</a>, [0.14.0](<#0140>), see #2801\n'
        self.assertEqual(self.citations_for(body), [(1, "#2801")])
        self.assertEqual(self.mdx_citations_for('<LinkCard href="#2026-roadmap" title="Roadmap" />\n'), [])

    def test_link_forms_the_link_branch_cannot_see_count_once(self):
        """A wrapped link, an angle-bracket destination and an HTML or MDX attribute cite one number once."""
        self.assertEqual(self.citations_for(f"see [pull request\n#2801]({PULL_URL}) for why\n"), [(2, "#2801")])
        for body in (
            f"[#2801](<{PULL_URL}>)",
            f'<a href="{PULL_URL}">#2801</a>',
            f'<LinkCard href="{PULL_URL}" title="#2801" />',
            "#2801 and #2801 again",
        ):
            with self.subTest(body=body):
                self.assertEqual(len(self.citations_for(body + "\n")), 1)
        self.assertEqual(len(self.citations_for(f'<a href="{PULL_URL}">#2802</a>\n')), 2)

    def test_a_link_is_keyed_on_its_citation_not_a_shorter_number_before_it(self):
        """A link's text may carry `#2` before `#2801`; the link and a bare `#2801` on the line are one citation."""
        body = f"[step #2 of #2801]({PULL_URL}), see #2801 below\n"
        self.assertEqual(len(self.citations_for(body)), 1)
        self.assertEqual(len(self.citations_for(f"[step #2 of #2801]({PULL_URL}) and #2802\n")), 2)

    def test_a_link_wrapped_after_its_number_counts_twice(self):
        """The stated limit: the bare number and its target sit on two lines, as a reference-style link's do."""
        body = f"[pull request #2801 was\nmerged]({PULL_URL}) and [other][ref]\n\n[ref]: {ISSUE_URL}\n"
        self.assertEqual(self.citations_for(body), [(1, "#2801"), (2, PULL_URL), (4, ISSUE_URL)])

    def test_fewer_than_three_digits_pass(self):
        self.assertEqual(self.citations_for("item #12 and #99\n"), [])

    def test_cross_repository_reference_passes(self):
        body = "This is [kubernetes/kubernetes#68078](https://github.com/kubernetes/kubernetes/issues/68078).\n"
        self.assertEqual(self.citations_for(body), [])

    def test_other_repository_urls_pass(self):
        self.assertEqual(self.citations_for("https://github.com/other/repo/pull/123 and https://github.com/gke-labs/kube-agents/blob/main/README.md\n"), [])

    def test_number_inside_a_fenced_block_passes_and_lines_stay_true(self):
        body = "intro\n```bash\n# see #1234\nkubectl get pods  # #5678\n```\nafter #2801\n"
        self.assertEqual(self.citations_for(body), [(6, "#2801")])

    def test_tilde_fence_and_indented_fence_pass(self):
        body = "~~~\n#1234\n~~~\n   ```\n   #5678\n   ```\n#2801\n"
        self.assertEqual(self.citations_for(body), [(7, "#2801")])

    def test_fence_inside_a_list_item_passes(self):
        """A fence at the item's content column, five spaces in on the upgrade page, is still a fence."""
        body = "1. Step one.\n\n     ```bash\n     echo hi # #1234\n     ```\n\n     then #2801\n"
        self.assertEqual(self.citations_for(body), [(7, "#2801")])

    def test_longer_closing_fence_closes_shorter_opening_but_not_the_reverse(self):
        body = "````md\n```\n#1234\n```\n#5678\n````\n#2801\n"
        self.assertEqual(self.citations_for(body), [(7, "#2801")])

    def test_fence_on_a_list_marker_or_quote_marker_line_is_a_fence(self):
        """CommonMark opens a fence on the marker line too; a list item's closer is indented, a quote's keeps its marker."""
        body = (
            "- ```bash\n  # see #1234\n  ```\n- then #2801\n\n"
            "> ```yaml\n> # #5678\n> ```\n\n"
            "after #2802\n```yaml\n#9999\n```\n#2803\n"
        )
        self.assertEqual(self.citations_for(body), [(4, "#2801"), (10, "#2802"), (14, "#2803")])

    def test_a_fence_opened_on_a_marker_line_closes_when_its_container_does(self):
        """CommonMark ends a quoted fence at the first line with fewer `>`, and an item's at a less-indented line."""
        self.assertEqual(self.citations_for("> ```yaml\n> a: #1234\n\nprose #2801\n"), [(4, "#2801")])
        self.assertEqual(self.citations_for("> ```yaml\n> a: #1234\nprose #2801\n```\n#5678\n```\n#2802\n"), [(3, "#2801"), (7, "#2802")])
        self.assertEqual(self.citations_for("- ```bash\n  echo #1234\n\nprose #2801\n"), [(4, "#2801")])
        self.assertEqual(self.citations_for("1. ```bash\n   echo #1234\n\n   #5678\nprose #2801\n"), [(5, "#2801")])
        self.assertEqual(self.citations_for("- ```bash\n  echo #1234\n- next #2801\n"), [(3, "#2801")])

    def test_a_fence_behind_indentation_alone_waits_for_its_closer(self):
        """An indented fence is not known to be in an item; a less-indented line inside it is still code."""
        self.assertEqual(self.citations_for("   ```\n#5678\n   ```\n#2801\n"), [(4, "#2801")])

    def test_a_fence_closes_at_its_own_blockquote_depth(self):
        """A quoted fence line inside an unquoted block is content, not the block's closer."""
        self.assertEqual(self.citations_for("```md\n> ```\n```\n#2801\n"), [(4, "#2801")])

    def test_backtick_fence_info_string_with_a_backtick_is_a_code_span_not_a_fence(self):
        """CommonMark: a backtick fence's info string may not hold a backtick; a tilde fence's may."""
        body = "```yaml``` is the fence you want, see #2801\n```bash\n# see #1234\n```\n#5678\n~~~a`b\n#4321\n~~~\n#3333\n"
        self.assertEqual(self.citations_for(body), [(1, "#2801"), (5, "#5678"), (9, "#3333")])

    def test_number_inside_an_html_comment_passes(self):
        body = "a <!-- #1234 --> b\n<!--\n#5678\n-->\n#2801 <!-- open #4321\nstill #8765 --> #3333\n"
        self.assertEqual(self.citations_for(body), [(5, "#2801"), (6, "#3333")])

    def test_prose_on_the_closer_line_of_a_block_html_comment_is_not_read(self):
        """The shared reader's limit, pinned: a browser shows the text after `-->`, this check does not read it; an MDX comment ends at its closer."""
        body = "<!-- reviewer note\n--> the proxy listens on `#8080`; see #2803\nlater #2801\n"
        self.assertEqual(self.citations_for(body), [(3, "#2801")])
        body = "{/* note\n*/} port `#8080` and `{/*` quoted, see #2803\nlater #2801\n"
        self.assertEqual(self.mdx_citations_for(body), [(2, "#2803"), (3, "#2801")])

    def test_an_opener_nothing_closes_is_text(self):
        """A stray `<!--` or `{/*` hides nothing after it, as the links checker reads the same page."""
        self.assertEqual(self.citations_for("a <!-- b\n#2801\n"), [(2, "#2801")])
        body = "a <!-- b #1234\n#5678 --> #2801\nc <!-- d\n#2802\n"
        self.assertEqual(self.citations_for(body), [(2, "#2801"), (4, "#2802")])
        self.assertEqual(self.mdx_citations_for("a {/* b #2801\n#2802\n"), [(1, "#2801"), (2, "#2802")])

    def test_a_mid_line_opener_is_believed_only_within_its_paragraph(self):
        """A `-->` in a later paragraph, another comment's or one in a code span, is not a stray opener's closer."""
        self.assertEqual(self.citations_for("a <!-- b\n#2801\n\n<!-- note -->\n#2802\n"), [(2, "#2801"), (5, "#2802")])
        self.assertEqual(self.citations_for("a <!-- b\n#2801\n\nthe `-->` closer\n#2802\n"), [(2, "#2801"), (5, "#2802")])
        self.assertEqual(self.citations_for("<!--\n#2801\n\n#2802 -->\n#2803\n"), [(5, "#2803")])

    def test_a_code_span_that_wraps_is_still_a_span(self):
        """A span may wrap onto the next line; its contents are code and the pairing after it is unchanged."""
        body = "the `platform-agent\n#2801` pod and `x` then #2802\n"
        self.assertEqual(self.citations_for(body), [(2, "#2802")])
        body = "wrap the `<!--\nmarker` in code, see #2801\n\n<!-- prettier-ignore -->\n"
        self.assertEqual(self.citations_for(body), [(2, "#2801")])

    def test_mdx_comment_delimiters_are_text_in_a_markdown_page(self):
        """A `.md` page renders `{/* */}` as text, so a number inside counts there and not in `.mdx`."""
        body = "{/* see #2801 */}\n{/*\n#2802\n*/}\n"
        self.assertEqual(self.citations_for(body), [(1, "#2801"), (3, "#2802")])
        self.assertEqual(self.mdx_citations_for(body), [])

    def test_a_closer_inside_a_fence_does_not_close_a_comment(self):
        """Fences are read first, so a closer in code is not the one an opener waits for."""
        body = "a <!-- b\n```\n-->\n```\n#2801\n"
        self.assertEqual(self.citations_for(body), [(5, "#2801")])

    def test_blanked_comment_does_not_fuse_its_neighbours(self):
        self.assertEqual(self.citations_for("word<!-- c -->#2801\n"), [(1, "#2801")])

    def test_mdx_comment_passes(self):
        body = "{/* BEGIN GENERATED: #1234 */}\n{/*\n#5678\n*/}\n#2801\n"
        self.assertEqual(self.citations_for(body, name="page.mdx"), [(5, "#2801")])

    def test_inline_code_passes_and_a_quoted_opener_opens_nothing(self):
        body = "the `<!--` marker opens a comment, as does `{/*`; port `#8080` and ``a `#1234` b``\nlater prose #2801\n"
        self.assertEqual(self.citations_for(body), [(2, "#2801")])

    def test_unbalanced_backtick_does_not_hide_the_line(self):
        self.assertEqual(self.citations_for("a stray ` here and #2801\n"), [(1, "#2801")])

    def test_contributing_page_is_exempt_from_citations_only(self):
        self.assertEqual(self.citations_for("thanks to #2801\n", name="contributing.md"), [])
        self.assertEqual(self.shapes_for("app_id = 1 and #2801\n", name="contributing.md"), ["denylist: \\bapp_id\\b"])

    def test_nested_contributing_page_is_not_exempt(self):
        self.assertEqual(self.citations_for("see #2801\n", name="deploy/contributing.md"), [(1, "#2801")])

    def test_denylist_lines_still_read_fenced_code(self):
        """Only the citation shape masks prose; a denylist shape inside a fence still fails."""
        self.assertEqual(self.shapes_for("```\nuses secrets.API_TOKEN\n```\n"), ["denylist: secrets\\.[A-Z_]+"])

    def test_denylist_lines_count_every_occurrence(self):
        """Two hits on one line are two findings, so an allowlist count is occurrences for every shape."""
        texts = [f.text for f in self.findings_for("secrets.A and secrets.B\n")]
        self.assertEqual(texts, ["secrets.A", "secrets.B"])


class ReleaseMarkerTest(unittest.TestCase):
    """The release-pipeline shapes the committed denylist carries."""

    @classmethod
    def setUpClass(cls):
        cls.patterns = check_docs_audience.load_denylist()

    def _hits(self, text: str) -> list[str]:
        return [p.pattern for p in self.patterns if p.search(text)]

    def test_release_markers_are_caught(self):
        for text in (
            "push an `evalcand_YYMMDDHHMM_<SHORT_SHA>` tag",
            "the rc_2501010101_abc1234 build",
            "`.github/workflows/release-publish.yml` runs it",
        ):
            with self.subTest(text=text):
                self.assertTrue(self._hits(text))

    def test_user_facing_release_words_pass(self):
        for text in (
            "the src_1 volume",
            "a release candidate",
            "pin the `staging` stage",
            "Release 0.19.0 is out",
            "see .github/workflows/docs-check.yml",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._hits(text), [])


class AllowlistTest(SiteFixture):
    """The shrinking rule: equal passes, a rise fails, a drop fails until the entry follows, nothing else is excused."""

    def _reconcile(self, allowlist_text: str) -> tuple[list[check_docs_audience.Finding], list[str], list[str]]:
        self.allowlist.write_text(allowlist_text, encoding="utf-8")
        entries, errors = check_docs_audience.load_allowlist(self.allowlist)
        findings = check_docs_audience.scan(self.site, self.denylist, self.ci_env)
        uncovered, problems = check_docs_audience.reconcile(findings, entries, self.site, self.allowlist)
        return uncovered, problems, errors

    def test_an_equal_count_passes(self):
        self.write_page("#2801 and #2802\n")
        uncovered, problems, errors = self._reconcile(f"page.md 2 {CITATION}\n")
        self.assertEqual((uncovered, problems, errors), ([], [], []))

    def test_a_rise_fails_and_reports_the_page_findings(self):
        self.write_page("#2801 and #2802\n#2803\n")
        uncovered, problems, errors = self._reconcile(f"page.md 2 {CITATION}\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(problems), 1)
        self.assertIn(f"page.md: 3 finding(s) of shape {CITATION!r}, the allowlist entry at {self.allowlist}:1 allows 2", problems[0])
        self.assertIn("remove enough of the 3 listed below to return to 2", problems[0])
        self.assertEqual([f.text for f in uncovered], ["#2801", "#2802", "#2803"])

    def test_a_drop_fails_naming_the_entry_to_lower(self):
        self.write_page("#2801\n")
        uncovered, problems, errors = self._reconcile(f"# header\n\npage.md 2 {CITATION}\n")
        self.assertEqual((uncovered, errors), ([], []))
        self.assertEqual(
            problems,
            [f"page.md: 1 finding(s) of shape {CITATION!r}, the allowlist entry at {self.allowlist}:3 allows 2; lower the entry to 1"],
        )

    def test_a_clean_page_fails_naming_the_entry_to_remove(self):
        self.write_page("clean\n")
        _, problems, _ = self._reconcile(f"page.md 1 {CITATION}\n")
        self.assertEqual(
            problems,
            [f"page.md: no finding of shape {CITATION!r} left, the allowlist entry at {self.allowlist}:1 allows 1; remove the entry"],
        )

    def test_a_missing_page_fails(self):
        self.write_page("clean\n")
        _, problems, _ = self._reconcile(f"gone.md 1 {CITATION}\n")
        self.assertEqual(problems, [f"{self.allowlist}:1: gone.md is not a site page; remove the entry"])

    def test_a_malformed_or_zero_line_is_an_error(self):
        self.write_page("clean\n")
        _, _, errors = self._reconcile(f"page.md 0 {CITATION}\npage.md\npage.md two {CITATION}\n")
        self.assertEqual(len(errors), 3)
        for number, error in enumerate(errors, start=1):
            self.assertIn(f"{self.allowlist}:{number}: expected `<page> <count> <shape>` with a count above zero", error)

    def test_a_non_decimal_digit_count_is_malformed_not_a_traceback(self):
        """`str.isdigit` accepts a superscript two that `int` refuses; the line is reported, not raised."""
        self.write_page("#2801\n")
        _, _, errors = self._reconcile(f"page.md \u00b2 {CITATION}\n")
        self.assertEqual(len(errors), 1)
        self.assertIn(f"{self.allowlist}:1: expected `<page> <count> <shape>` with a count above zero", errors[0])

    def test_only_the_backlog_shapes_can_be_pinned(self):
        """An entry for a workflow secret or an App ID is refused: those shapes have always held at zero."""
        self.write_page("secrets.API_TOKEN and app_id = 1\n")
        _, _, errors = self._reconcile("page.md 1 denylist: secrets\\.[A-Z_]+\npage.md 1 denylist: \\bapp_id\\b\n")
        self.assertEqual(len(errors), 2)
        self.assertIn(f"{self.allowlist}:1: shape 'denylist: secrets\\\\.[A-Z_]+' cannot be pinned", errors[0])
        self.assertIn(f"{self.allowlist}:2: shape 'denylist: \\\\bapp_id\\\\b' cannot be pinned", errors[1])

    def test_an_unpinnable_entry_fails_main_even_when_the_page_matches_it(self):
        self.write_page("uses secrets.API_TOKEN\n")
        self.allowlist.write_text("page.md 1 denylist: secrets\\.[A-Z_]+\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_docs_audience.main(self.site, self.denylist, self.ci_env, self.allowlist), 1)

    def test_a_repeated_page_and_shape_is_an_error(self):
        self.write_page("#2801\n")
        _, _, errors = self._reconcile(f"page.md 1 {CITATION}\npage.md 1 {CITATION}\n")
        self.assertEqual(errors, [f"{self.allowlist}:2: page.md already has an entry of shape {CITATION!r} at line 1"])

    def test_the_evalcand_line_is_pinnable_by_occurrence(self):
        self.denylist.write_text("evalcand_\n", encoding="utf-8")
        self.write_page("push evalcand_a then evalcand_b\n")
        uncovered, problems, errors = self._reconcile("page.md 2 denylist: evalcand_\n")
        self.assertEqual((uncovered, problems, errors), ([], [], []))

    def test_an_entry_covers_its_shape_only(self):
        self.write_page("#2801 and secrets.API_TOKEN\n")
        uncovered, problems, _ = self._reconcile(f"page.md 1 {CITATION}\n")
        self.assertEqual(problems, [])
        self.assertEqual([f.shape for f in uncovered], ["denylist: secrets\\.[A-Z_]+"])

    def test_an_entry_covers_its_page_only(self):
        self.write_page("#2801\n")
        self.write_page("#2802\n", name="other.md")
        uncovered, problems, _ = self._reconcile(f"page.md 1 {CITATION}\n")
        self.assertEqual(problems, [])
        self.assertEqual([(f.path.name, f.text) for f in uncovered], [("other.md", "#2802")])

    def test_a_nested_page_is_keyed_by_its_site_relative_path(self):
        self.write_page("#2801\n", name="install/upgrade.md")
        uncovered, problems, _ = self._reconcile(f"install/upgrade.md 1 {CITATION}\n")
        self.assertEqual((uncovered, problems), ([], []))

    def test_no_entry_means_zero(self):
        self.write_page("#2801\n")
        uncovered, problems, _ = self._reconcile("# nothing pinned\n")
        self.assertEqual(problems, [])
        self.assertEqual([f.text for f in uncovered], ["#2801"])


class MainRedPathTest(SiteFixture):
    """`main()` on a failing site: the exit code and the file:line an author has to act on."""

    def _run_main(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_docs_audience.main(self.site, self.denylist, self.ci_env, self.allowlist)
        return code, out.getvalue()

    def test_a_finding_fails_and_names_file_line_and_shape(self):
        (self.site / "page.md").write_text("clean line\nuses secrets.API_TOKEN here\n", encoding="utf-8")
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("1 maintainer identifier(s)", out)
        self.assertIn(f"{self.site / 'page.md'}:2: ", out)
        self.assertIn("'secrets.API_TOKEN'", out)

    def test_a_failed_precondition_fails_without_scanning(self):
        code, out = self._run_main()  # the fixture site has no page yet
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", out)
        self.assertNotIn("maintainer identifier(s)", out)

    def test_a_malformed_allowlist_fails_without_scanning(self):
        (self.site / "page.md").write_text("clean\n", encoding="utf-8")
        self.allowlist.write_text("page.md one citation\n", encoding="utf-8")
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"ERROR: {self.allowlist}:1: expected `<page> <count> <shape>`", out)
        self.assertNotIn("maintainer identifier(s)", out)

    def test_an_allowlist_problem_fails_and_names_the_entry(self):
        (self.site / "page.md").write_text("clean\n", encoding="utf-8")
        self.allowlist.write_text(f"page.md 1 {CITATION}\n", encoding="utf-8")
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("1 allowlist entr(y/ies) out of step", out)
        self.assertIn(f"page.md: no finding of shape {CITATION!r} left, the allowlist entry at {self.allowlist}:1", out)
        self.assertIn("remove the entry", out)

    def test_a_rise_header_counts_the_listed_findings_as_above_the_allowlist(self):
        """The header and the entry line agree: the pinned finding is listed, not reported as uncovered."""
        (self.site / "page.md").write_text("see #2801 and #2802\n", encoding="utf-8")
        self.allowlist.write_text(f"page.md 1 {CITATION}\n", encoding="utf-8")
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"2 maintainer identifier(s) or pull-request citation(s) on the published site outside or above what {self.allowlist} allows", out)
        self.assertNotIn("does not cover", out)
        self.assertIn("remove enough of the 2 listed below to return to 1", out)

    def test_an_empty_allowlist_passes_a_clean_site(self):
        """The cleanup's end state: a list of comments only, a clean site, exit 0."""
        (self.site / "page.md").write_text("clean\n", encoding="utf-8")
        code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("0 allowlist entr(y/ies)", out)

    def test_an_exact_allowlist_match_passes(self):
        (self.site / "page.md").write_text("see #2801\n", encoding="utf-8")
        self.allowlist.write_text(f"page.md 1 {CITATION}\n", encoding="utf-8")
        code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("1 allowlist entr(y/ies), each matched exactly", out)


if __name__ == "__main__":
    unittest.main()
