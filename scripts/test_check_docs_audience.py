#!/usr/bin/env python3
"""Unit tests for the site-audience guard.

Run: cd scripts && python3 -m unittest test_check_docs_audience

The guard fails green: a shape that stops matching lets the next maintainer
identifier onto the public site with no signal, and a placeholder handling that
stops excusing `<project>` turns every install page red. Both directions are
pinned here, along with the derivation from hack/ci-env.sh that the denylist
relies on instead of repeating the values, and the two preconditions that stop
an empty run from reporting clean.
"""

import contextlib
import io
import re
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

    def shapes_for(self, body: str, name: str = "page.md") -> list[str]:
        (self.site / name).write_text(body, encoding="utf-8")
        return [f.shape for f in check_docs_audience.scan(self.site, self.denylist, self.ci_env)]


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
    def test_the_current_site_is_clean(self):
        findings = check_docs_audience.scan()
        self.assertEqual([repr(f) for f in findings], [])

    def test_main_reports_success(self):
        self.assertEqual(check_docs_audience.main(), 0)


class MainRedPathTest(SiteFixture):
    """`main()` on a failing site: the exit code and the file:line an author has to act on."""

    def _run_main(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_docs_audience.main(self.site, self.denylist, self.ci_env)
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


if __name__ == "__main__":
    unittest.main()
