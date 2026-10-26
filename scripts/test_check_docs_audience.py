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
import re
import tempfile
import time
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
RUN_BUDGET_SECONDS = 2.0


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
        """Every entry names a page the site has and a shape the check reports; none is a free pass."""
        entries, _ = check_docs_audience.load_allowlist()
        pages = {p.relative_to(check_docs_audience.SITE_ROOT).as_posix() for p in check_docs_audience.site_pages()}
        self.assertTrue(entries)
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
        """CPU time, not wall clock, so a loaded runner does not fail an unrelated pull request."""
        started = time.process_time()
        with contextlib.redirect_stdout(io.StringIO()):
            check_docs_audience.main()
        self.assertLess(time.process_time() - started, RUN_BUDGET_SECONDS)


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
        body = "#2801. #2802, #2803: (#2804) #2805\n"
        self.assertEqual([t for _, t in self.citations_for(body)], ["#2801", "#2802", "#2803", "#2804", "#2805"])

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

    def test_number_inside_an_html_comment_passes(self):
        body = "a <!-- #1234 --> b\n<!--\n#5678\n-->\n#2801 <!-- open #4321\nstill #8765 --> #3333\n"
        self.assertEqual(self.citations_for(body), [(5, "#2801"), (6, "#3333")])

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

    def test_an_exact_allowlist_match_passes(self):
        (self.site / "page.md").write_text("see #2801\n", encoding="utf-8")
        self.allowlist.write_text(f"page.md 1 {CITATION}\n", encoding="utf-8")
        code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("1 allowlist entr(y/ies), each matched exactly", out)


if __name__ == "__main__":
    unittest.main()
