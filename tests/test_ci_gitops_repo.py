"""Tests for the GitOps-repository resolution in hack/ci-deploy.sh.

The presubmit eval's GitHub-writing scenarios read the `Git Repo:` line out of
/opt/data/SETTINGS.md, which the operator renders from the PlatformAgent CR's
spec.integration.github.gitRepo. CI has to supply that value, and the whole
point of how it supplies it is the *failure* behaviour:

* one GitOps repository per leasable project, never a shared default -- two
  Boskos leases must not write to the same ledger issue;
* an unmapped project stops the deploy, in Prow and on a laptop alike, rather
  than silently installing an agent with an empty gitRepo (every scenario then
  fails at step 0 for a reason no log explains) or, worse, one pointed at some
  other project's repository;
* under Prow the in-repo table is the only source, because the project is
  leased per run and a value pinned in the job environment would eventually
  outlive the lease it was written for.

None of that is observable from a successful run, which is exactly why it
regresses quietly. The block is extracted from the script by its section
markers and executed, so these assertions are against the code that ships
rather than a copy.
"""

import base64
import pathlib
import re
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

# The section this suite exercises, and the marker that ends it. Both are
# asserted below, so renaming a section fails here loudly instead of silently
# shrinking what is tested.
_SECTION_START = "# ─── 2b. GitOps Repository for This Run"
_SECTION_END_RE = re.compile(r"^# ─── (?!2b\.)", re.MULTILINE)

# Repeated here on purpose rather than parsed out of the script: a test that
# derives the expected mapping from the mapping under test asserts nothing.
# docs/ci-pool-projects.md is the onboarding runbook that
# points here; it carries no copy of the pairs.
_EXPECTED_MAPPING = {
    "kube-agents-evals": "gke-agentic/kube-agents-evals-infra",
    "kube-agents-evals-2": "gke-agentic/kube-agents-evals-2-infra",
    "kube-agents-evals-3": "gke-agentic/kube-agents-evals-3-infra",
    "kube-agents-evals-4": "gke-agentic/kube-agents-evals-4-infra",
    "kube-agents-evals-5": "gke-agentic/kube-agents-evals-5-infra",
    "kube-agents-evals-6": "gke-agentic/kube-agents-evals-6-infra",
    "kube-agents-evals-7": "gke-agentic/kube-agents-evals-7-infra",
    "kube-agents-evals-8": "gke-agentic/kube-agents-evals-8-infra",
    "kube-agents-evals-9": "gke-agentic/kube-agents-evals-9-infra",
    "kube-agents-evals-10": "gke-agentic/kube-agents-evals-10-infra",
    "kube-agents-evals-11": "gke-agentic/kube-agents-evals-11-infra",
    "kube-agents-evals-12": "gke-agentic/kube-agents-evals-12-infra",
    "kube-agents-evals-13": "gke-agentic/kube-agents-evals-13-infra",
    "kube-agents-evals-14": "gke-agentic/kube-agents-evals-14-infra",
    "kube-agents-evals-15": "gke-agentic/kube-agents-evals-15-infra",
    "kube-agents-evals-16": "gke-agentic/kube-agents-evals-16-infra",
    "kube-agents-evals-17": "gke-agentic/kube-agents-evals-17-infra",
    "kube-agents-evals-18": "gke-agentic/kube-agents-evals-18-infra",
    "kube-agents-evals-19": "gke-agentic/kube-agents-evals-19-infra",
    "kube-agents-evals-20": "gke-agentic/kube-agents-evals-20-infra",
    "kube-agents-evals-21": "gke-agentic/kube-agents-evals-21-infra",
    "kube-agents-evals-22": "gke-agentic/kube-agents-evals-22-infra",
    "kube-agents-evals-23": "gke-agentic/kube-agents-evals-23-infra",
    "kube-agents-evals-24": "gke-agentic/kube-agents-evals-24-infra",
    "kube-agents-evals-25": "gke-agentic/kube-agents-evals-25-infra",
    "kube-agents-evals-26": "gke-agentic/kube-agents-evals-26-infra",
    "kube-agents-evals-27": "gke-agentic/kube-agents-evals-27-infra",
    "kube-agents-evals-28": "gke-agentic/kube-agents-evals-28-infra",
    "kube-agents-evals-29": "gke-agentic/kube-agents-evals-29-infra",
    "kube-agents-evals-30": "gke-agentic/kube-agents-evals-30-infra",
    "kube-agents-evals-31": "gke-agentic/kube-agents-evals-31-infra",
    "kube-agents-evals-32": "gke-agentic/kube-agents-evals-32-infra",
    "kube-agents-evals-33": "gke-agentic/kube-agents-evals-33-infra",
    "kube-agents-evals-34": "gke-agentic/kube-agents-evals-34-infra",
    "kube-agents-evals-35": "gke-agentic/kube-agents-evals-35-infra",
}

# The fail-closed tests need a project the mapping will never contain, and for
# a while that was "kube-agents-evals-3" — the obvious next name in the
# sequence, picked because nothing could plausibly claim it. The pool claimed
# it: the project was added to Boskos on 2026-08-21 and every presubmit that
# leased it died on the unmapped-project refusal. Mapping it turned this suite
# red, which is the test doing its job, but it also showed the fixture was
# wrong to begin with. A placeholder that is a plausible future value of the
# thing it stands outside of is a placeholder with an expiry date on it, and
# the next name in the sequence has the same date on it.
_NEVER_MAPPED_PROJECT = "not-a-pool-project-fixture"

# EVAL_FORGE=gitlab (issue #2394): the pool's GitLab projects carry the same
# names as the GitHub repositories, under the gitlab.com group gke-agentic, so
# the pinned pairs are the same table. What is GitLab-specific is pinned
# separately below: the host, the credential Secret, and the forge/repository
# values the chart receives instead of the github.gitRepo alias.
_EXPECTED_GITLAB_MAPPING = dict(_EXPECTED_MAPPING)
_GITLAB_HOST = "gitlab.com"
_GITLAB_FORGE_SECRET = "gitlab-forge-token"
# The label hack/ci-teardown.sh sweeps by; pinned as a literal here and
# compared to both scripts below.
_TEARDOWN_SELECTOR = "app.kubernetes.io/part-of=kube-agents"
# One pair for the pool, read from where the runner identities live, never
# from the leased project: pinned here so a copy-per-project design cannot
# creep back in without the test saying so.
_GITLAB_SECRETS_PROJECT = "kube-agents-prow"
_CRD_RELATIVE = "charts/kube-agents/crds/kubeagents.x-k8s.io_platformagents.yaml"
_HELPERS_RELATIVE = "charts/kube-agents/templates/_helpers.tpl"
_CRD_LISTS_GITLAB_RE = re.compile(r"^\s+- gitlab$", re.MULTILINE)
_HELPERS_REGISTER_GITLAB_RE = re.compile(r'\$registered := list .*"gitlab"')


def _chart_registers_gitlab(root):
    """Both hand-mirrored provider lists name gitlab: the CRD enum and $registered."""
    crd = (root / _CRD_RELATIVE).read_text(encoding="utf-8")
    helpers = (root / _HELPERS_RELATIVE).read_text(encoding="utf-8")
    return bool(_CRD_LISTS_GITLAB_RE.search(crd)) and bool(_HELPERS_REGISTER_GITLAB_RE.search(helpers))


def _script_dir_with_chart(tmp_path, crd_lists_gitlab, helpers_register_gitlab=None):
    """A SCRIPT_DIR whose ../charts registers provider gitlab, or does not.

    The block reads both lists through SCRIPT_DIR, so the gate is exercised
    against files this test writes rather than the checkout's, which change
    the day the provider lands. The helper list follows the CRD unless a case
    splits them.
    """
    if helpers_register_gitlab is None:
        helpers_register_gitlab = crd_lists_gitlab
    crd = tmp_path / _CRD_RELATIVE
    crd.parent.mkdir(parents=True)
    providers = "                - github\n" + ("                - gitlab\n" if crd_lists_gitlab else "")
    crd.write_text("            provider:\n              enum:\n" + providers, encoding="utf-8")
    helpers = tmp_path / _HELPERS_RELATIVE
    helpers.parent.mkdir(parents=True)
    registered = '"github" "gitlab"' if helpers_register_gitlab else '"github"'
    helpers.write_text("{{- $registered := list " + registered + " -}}\n", encoding="utf-8")
    hack = tmp_path / "hack"
    hack.mkdir()
    return str(hack)


def _resolution_block():
    text = _CI_DEPLOY.read_text(encoding="utf-8")
    start = text.find(_SECTION_START)
    assert start != -1, f"{_SECTION_START!r} not found in hack/ci-deploy.sh"
    end_match = _SECTION_END_RE.search(text, start + len(_SECTION_START))
    assert end_match, "no section marker follows the GitOps-repository block"
    return text[start : end_match.start()]


class CiDeployGitopsRepoTest(unittest.TestCase):
    maxDiff = None

    def _resolve(self, project_id, **env):
        """Run the resolution block and report what it decided.

        Returns (returncode, stdout, stderr). On success stdout's last line is
        `RESOLVED <gitRepo>|<helm minter args>|<helm forge args>`, with an
        empty gitRepo meaning the GitHub integration is deliberately off.
        """
        script = _resolution_block() + (
            '\nprintf "RESOLVED %s|%s|%s\\n" "${GITOPS_REPO}" "${GITHUB_MINTER_ARGS[*]}" "${FORGE_ARGS[*]}"\n'
        )
        # set -euo pipefail mirrors the script's own header: a resolution that
        # only "fails" by leaving a variable unset must show up as a failure.
        proc = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "PROJECT_ID": project_id,
                    # Cleared unless a case sets them: the ambient shell must
                    # not decide whether this looks like a Prow run.
                    "PULL_NUMBER": "",
                    "JOB_NAME": "",
                    "EVAL_GITOPS_REPO": "",
                    "EVAL_GITHUB_APP_ID": "",
                    # github unless a case says otherwise; the gate reads the
                    # CRD through SCRIPT_DIR, the checkout's by default.
                    "EVAL_FORGE": "",
                    "SCRIPT_DIR": str(_REPO_ROOT / "hack"),
                    **env,
                }
            ),
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _resolved_repo(self, stdout):
        line = [ln for ln in stdout.splitlines() if ln.startswith("RESOLVED ")][-1]
        return line[len("RESOLVED ") :].split("|", 1)[0]

    def _minter_args(self, stdout):
        line = [ln for ln in stdout.splitlines() if ln.startswith("RESOLVED ")][-1]
        return line.split("|")[1]

    def _forge_args(self, stdout):
        line = [ln for ln in stdout.splitlines() if ln.startswith("RESOLVED ")][-1]
        return line.split("|")[2]

    # --- the mapping ------------------------------------------------------

    def test_each_pool_project_maps_to_its_own_repository(self):
        for project, repo in _EXPECTED_MAPPING.items():
            with self.subTest(project=project):
                rc, out, err = self._resolve(project, PULL_NUMBER="123", JOB_NAME="pull-eval")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._resolved_repo(out), repo)

    def test_no_two_projects_share_a_repository(self):
        repos = list(_EXPECTED_MAPPING.values())
        self.assertEqual(len(repos), len(set(repos)))

    def test_the_fail_closed_fixture_is_not_a_mapped_project(self):
        """Keeps the fail-closed tests honest about what they prove.

        If the fixture ever becomes a real mapping, `test_unmapped_project_*`
        starts asserting that a *mapped* project is refused — the opposite of
        its name. It would fail loudly here rather than quietly inverting its
        own meaning, which is the failure the kube-agents-evals-3 fixture was
        one onboarding away from.
        """
        self.assertNotIn(_NEVER_MAPPED_PROJECT, _EXPECTED_MAPPING)
        self.assertNotIn(_NEVER_MAPPED_PROJECT, _resolution_block())

    # --- fail-closed ------------------------------------------------------

    def test_unmapped_project_fails_the_prow_deploy(self):
        rc, out, err = self._resolve(
            _NEVER_MAPPED_PROJECT, PULL_NUMBER="123", JOB_NAME="pull-eval"
        )
        self.assertNotEqual(rc, 0)
        self.assertNotIn("RESOLVED", out)
        # The message has to name the edit, or the next person onboarding a
        # Boskos project has a red job and no lead.
        self.assertIn("gitops_repo_for_project", err)

    def test_unmapped_project_fails_a_local_run_with_the_escape_hatches(self):
        rc, out, err = self._resolve("some-developer-project")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("RESOLVED", out)
        self.assertIn("EVAL_GITOPS_REPO=owner/repo", err)
        self.assertIn("EVAL_GITOPS_REPO=none", err)

    def test_prow_run_refuses_a_pinned_override(self):
        rc, _, err = self._resolve(
            "kube-agents-evals",
            PULL_NUMBER="123",
            JOB_NAME="pull-eval",
            EVAL_GITOPS_REPO="gke-agentic/some-other-infra",
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("EVAL_GITOPS_REPO", err)

    def test_malformed_override_is_rejected(self):
        for value in (
            "https://github.com/acme/fleet",
            "git@github.com:acme/fleet.git",
            "acme",
            "acme/fleet/extra",
            "acme/fleet; rm -rf /",
        ):
            with self.subTest(value=value):
                rc, out, _ = self._resolve("dev-project", EVAL_GITOPS_REPO=value)
                self.assertNotEqual(rc, 0)
                self.assertNotIn("RESOLVED", out)

    # --- the explicit opt-outs -------------------------------------------

    def test_local_override_is_honoured(self):
        rc, out, err = self._resolve("dev-project", EVAL_GITOPS_REPO="gke-agentic/scratch-infra")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resolved_repo(out), "gke-agentic/scratch-infra")

    def test_none_is_the_only_route_to_an_empty_gitrepo(self):
        rc, out, err = self._resolve("dev-project", EVAL_GITOPS_REPO="none")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resolved_repo(out), "")

    # --- the minter half --------------------------------------------------

    def test_minter_stays_off_until_the_app_id_is_supplied(self):
        # The minter Deployment is in the release `helm --wait` gates on, and
        # it cannot pass readiness before a human has imported the GitHub App
        # key into the project's KMS key. Defaulting it on would fail every
        # presubmit in a project that has not been through that step.
        rc, out, err = self._resolve("kube-agents-evals", PULL_NUMBER="123")
        self.assertEqual(rc, 0, err)
        self.assertIn("githubMinter.enabled=false", self._minter_args(out))

    def test_minter_is_scoped_to_the_leased_project_repository(self):
        rc, out, err = self._resolve(
            "kube-agents-evals-2", PULL_NUMBER="123", EVAL_GITHUB_APP_ID="123456"
        )
        self.assertEqual(rc, 0, err)
        args = self._minter_args(out)
        self.assertIn("githubMinter.enabled=true", args)
        self.assertIn("githubMinter.org=gke-agentic", args)
        self.assertIn("githubMinter.repo=kube-agents-evals-2-infra", args)
        self.assertIn("githubMinter.appId=123456", args)

    def test_minter_stays_off_when_the_github_integration_is_off(self):
        rc, out, err = self._resolve(
            "dev-project", EVAL_GITOPS_REPO="none", EVAL_GITHUB_APP_ID="123456"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("githubMinter.enabled=false", self._minter_args(out))

    # --- the forge switch (EVAL_FORGE) ------------------------------------

    def test_github_is_the_default_and_keeps_the_alias(self):
        rc, out, err = self._resolve("kube-agents-evals-2", PULL_NUMBER="123")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            self._forge_args(out),
            "--set-string platformAgent.integration.github.gitRepo=gke-agentic/kube-agents-evals-2-infra",
        )

    def test_unknown_forge_is_refused(self):
        rc, out, err = self._resolve("kube-agents-evals", PULL_NUMBER="123", EVAL_FORGE="bitbucket")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("RESOLVED", out)
        self.assertIn("EVAL_FORGE", err)

    def test_gitlab_maps_each_pool_project_and_declares_one_gitlab_forge(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_dir = _script_dir_with_chart(pathlib.Path(tmp), crd_lists_gitlab=True)
            for project, path in _EXPECTED_GITLAB_MAPPING.items():
                with self.subTest(project=project):
                    rc, out, err = self._resolve(
                        project, PULL_NUMBER="123", JOB_NAME="pull-eval",
                        EVAL_FORGE="gitlab", EVAL_GITHUB_APP_ID="123456", SCRIPT_DIR=script_dir,
                    )
                    self.assertEqual(rc, 0, err)
                    # GitHub is off: no gitRepo, and the minter cannot be on
                    # (the chart refuses githubMinter with no GitHub forge).
                    self.assertEqual(self._resolved_repo(out), "")
                    self.assertIn("githubMinter.enabled=false", self._minter_args(out))
                    args = self._forge_args(out)
                    self.assertIn("platformAgent.integration.github.gitRepo=", args)
                    self.assertIn("platformAgent.integration.forges[0].provider=gitlab", args)
                    self.assertIn(f"platformAgent.integration.forges[0].host={_GITLAB_HOST}", args)
                    self.assertIn("platformAgent.integration.forges[0].namespace=gke-agentic", args)
                    self.assertIn(
                        f"platformAgent.integration.forges[0].credentialsRef.name={_GITLAB_FORGE_SECRET}", args
                    )
                    self.assertIn("platformAgent.integration.repositories[0].forge=gitlab", args)
                    self.assertIn(
                        f"platformAgent.integration.repositories[0].repository=https://{_GITLAB_HOST}/{path}", args
                    )
                    self.assertIn("platformAgent.integration.repositories[0].role=gitops", args)

    def test_gitlab_unmapped_project_is_refused_naming_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_dir = _script_dir_with_chart(pathlib.Path(tmp), crd_lists_gitlab=True)
            rc, out, err = self._resolve(
                _NEVER_MAPPED_PROJECT, PULL_NUMBER="123", JOB_NAME="pull-eval",
                EVAL_FORGE="gitlab", SCRIPT_DIR=script_dir,
            )
        self.assertNotEqual(rc, 0)
        self.assertNotIn("RESOLVED", out)
        self.assertIn("gitlab_project_for_project", err)

    def test_gitlab_is_gated_on_a_chart_that_registers_the_provider(self):
        # The chart would refuse `provider: gitlab` at helm time, after the
        # image build. Its registry is two hand-mirrored lists, the CRD enum
        # and $registered in _helpers.tpl; the block reads both first and
        # fails in seconds unless both name gitlab, naming the files.
        for crd, helpers in ((False, False), (True, False), (False, True)):
            with self.subTest(crd_lists_gitlab=crd, helpers_register_gitlab=helpers):
                with tempfile.TemporaryDirectory() as tmp:
                    script_dir = _script_dir_with_chart(
                        pathlib.Path(tmp), crd_lists_gitlab=crd, helpers_register_gitlab=helpers
                    )
                    rc, out, err = self._resolve(
                        "kube-agents-evals", PULL_NUMBER="123", EVAL_FORGE="gitlab", SCRIPT_DIR=script_dir
                    )
                self.assertNotEqual(rc, 0)
                self.assertNotIn("RESOLVED", out)
                self.assertIn("register provider", err)
                self.assertIn(_CRD_RELATIVE, err)
                self.assertIn(_HELPERS_RELATIVE, err)

    def test_gitlab_gate_reads_the_checkout_chart(self):
        # Against the real chart the answer follows its registry: refused
        # until the GitLab provider lands, resolved afterwards. Either way the
        # gate's verdict and the files agree, which is what keeps the fast
        # refusal from outliving the reason for it.
        rc, out, err = self._resolve("kube-agents-evals", PULL_NUMBER="123", EVAL_FORGE="gitlab")
        if _chart_registers_gitlab(_REPO_ROOT):
            self.assertEqual(rc, 0, err)
            self.assertIn("forges[0].provider=gitlab", self._forge_args(out))
        else:
            self.assertNotEqual(rc, 0)
            self.assertIn("register provider", err)

    def test_gitlab_turns_the_minter_off_without_claiming_it_was_on(self):
        # With EVAL_GITHUB_APP_ID in the environment the GitHub path would
        # enable the minter; under gitlab it is off, and the log says why.
        with tempfile.TemporaryDirectory() as tmp:
            script_dir = _script_dir_with_chart(pathlib.Path(tmp), crd_lists_gitlab=True)
            rc, out, err = self._resolve(
                "kube-agents-evals", PULL_NUMBER="123", EVAL_FORGE="gitlab",
                EVAL_GITHUB_APP_ID="123456", SCRIPT_DIR=script_dir,
            )
        self.assertEqual(rc, 0, err)
        self.assertIn("githubMinter.enabled=false", self._minter_args(out))
        self.assertNotIn("GitHub token minter: enabled", out)
        self.assertIn("GitHub token minter: off (EVAL_FORGE=gitlab)", out)

    def test_gitlab_prow_run_still_refuses_a_pinned_github_override(self):
        # Past the gate, the GitHub resolution still runs and still refuses a
        # pinned override under Prow: the forge switch removes no refusal.
        with tempfile.TemporaryDirectory() as tmp:
            script_dir = _script_dir_with_chart(pathlib.Path(tmp), crd_lists_gitlab=True)
            rc, _, err = self._resolve(
                "kube-agents-evals", PULL_NUMBER="123", JOB_NAME="pull-eval",
                EVAL_FORGE="gitlab", EVAL_GITOPS_REPO="gke-agentic/some-other-infra", SCRIPT_DIR=script_dir,
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("EVAL_GITOPS_REPO", err)


class CiDeployWiringTest(unittest.TestCase):
    """The resolved value has to reach helm, and reach it early enough."""

    def setUp(self):
        self.text = _CI_DEPLOY.read_text(encoding="utf-8")

    def test_helm_receives_the_resolved_repository(self):
        # The alias is set once, inside FORGE_ARGS, and helm expands the array:
        # a second literal on the helm line would set the alias beside the
        # forges list, which the chart refuses.
        self.assertEqual(
            self.text.count('--set-string "platformAgent.integration.github.gitRepo=${GITOPS_REPO}"'), 1
        )
        self.assertIn('"${FORGE_ARGS[@]}"', self.text)
        self.assertIn('"${GITHUB_MINTER_ARGS[@]}"', self.text)

    def test_gitlab_secret_is_materialised_after_cluster_auth_and_before_the_chart(self):
        auth = self.text.find("# ─── 3. Cluster Auth")
        secret = self.text.find("materialize_gitlab_forge_secret\n")
        chart = self.text.find("# ─── 5c. Deploy the chart")
        self.assertNotEqual(auth, -1)
        self.assertNotEqual(secret, -1)
        self.assertNotEqual(chart, -1)
        self.assertLess(auth, secret)
        self.assertLess(secret, chart)

    def test_resolution_runs_before_the_image_build(self):
        # A ~20-minute Cloud Build submit sits between the two. Resolving
        # after it would burn the whole build to report a one-line
        # configuration error.
        resolution = self.text.find(_SECTION_START)
        build = self.text.find("gcloud builds submit")
        self.assertNotEqual(resolution, -1)
        self.assertNotEqual(build, -1)
        self.assertLess(resolution, build)

    def test_ci_deploy_parses(self):
        subprocess.run(["bash", "-n", str(_CI_DEPLOY)], check=True)



def _materialize_function():
    text = _CI_DEPLOY.read_text(encoding="utf-8")
    start = text.find("materialize_gitlab_forge_secret() {")
    assert start != -1, "materialize_gitlab_forge_secret() not found in hack/ci-deploy.sh"
    end = text.find("\n}\n", start)
    return text[start : end + len("\n}\n")]


_FAKE_TOKEN = "glpat-fake-token-for-the-test"


class CiDeployGitlabSecretTest(unittest.TestCase):
    """The agent token goes Secret Manager -> kubectl over a pipe.

    Lifted from the script and run with stubbed gcloud and kubectl that record
    what they were asked. What is pinned: which secret is read from which
    project, the Secret and key the chart's credentialsRef will name, that the
    value travels on stdin and appears in no argument, and that a read that
    fails applies nothing.
    """

    _NS = "kubeagents-system-test"
    _PID = "kube-agents-evals-7"

    def _run(self, tmp_path, gcloud_exit="0"):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "log"
        log.mkdir()
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{log}/gcloud.argv"\n'
            f'[ "{gcloud_exit}" = "0" ] || exit "{gcloud_exit}"\n'
            # With a trailing newline, as `echo token | gcloud secrets create
            # --data-file=-` stores it; the Secret must carry the token alone.
            f'printf "%s\\n" "{_FAKE_TOKEN}"\n',
            encoding="utf-8",
        )
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{log}/kubectl.argv"\n'
            'case "$*" in\n'
            # The real kubectl reads --from-file=<key>=/dev/stdin; so does this.
            '  "create secret generic"*) printf "kind: Secret\\ndata:\\n  token: %s\\n" "$(base64 < /dev/stdin | tr -d \'\\n\')" ;;\n'
            '  "create namespace"*) printf "kind: Namespace\\n" ;;\n'
            # kubectl label --local rewrites the manifest on stdin with the label added.
            '  "label --local"*) cat; printf "  labels:\\n    %s\\n" "$(printf "%s" "$5" | sed "s/=/: /")" ;;\n'
            f'  "apply -f -") cat >> "{log}/applied.yaml" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        for stub in (gcloud, kubectl):
            stub.chmod(stub.stat().st_mode | 0o111)
        script = (
            "set -euo pipefail\n"
            f'NAMESPACE="{self._NS}"; PROJECT_ID="{self._PID}"\n'
            'GITLAB_FORGE_SECRET_NAME="gitlab-forge-token"; GITLAB_FORGE_SECRET_KEY="token"\n'
            f'GITLAB_AGENT_SM_SECRET="gitlab-agent-token"; GITLAB_SECRETS_PROJECT="{_GITLAB_SECRETS_PROJECT}"\n'
            f'GITLAB_FORGE_SECRET_LABEL="{_TEARDOWN_SELECTOR}"\n'
            + _materialize_function()
            + "materialize_gitlab_forge_secret\n"
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env=get_isolated_test_env(bin_dir=bin_dir),
        )
        read = lambda name: (log / name).read_text(encoding="utf-8") if (log / name).exists() else ""
        return proc, read("gcloud.argv"), read("kubectl.argv"), read("applied.yaml")

    def test_the_token_is_read_from_the_pools_home_and_applied_under_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, gcloud_argv, kubectl_argv, applied = self._run(pathlib.Path(tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            f"secrets versions access latest --secret=gitlab-agent-token --project={_GITLAB_SECRETS_PROJECT}",
            gcloud_argv,
        )
        self.assertNotIn(f"--project={self._PID}", gcloud_argv)
        self.assertIn(
            f"create secret generic gitlab-forge-token -n {self._NS} --from-file=token=/dev/stdin --dry-run=client -o yaml",
            kubectl_argv,
        )
        self.assertIn("kind: Namespace", applied)
        self.assertIn("kind: Secret", applied)
        self.assertIn("token: " + base64.b64encode(_FAKE_TOKEN.encode()).decode() + "\n", applied)
        # Labelled for hack/ci-teardown.sh's namespaced sweep, so the token
        # leaves the host cluster with the lease.
        self.assertIn("app.kubernetes.io/part-of: kube-agents\n", applied)
        # Pinned separately from the apply above: the value crossed on stdin only.
        for where, text in (("gcloud argv", gcloud_argv), ("kubectl argv", kubectl_argv), ("stdout", proc.stdout)):
            with self.subTest(where=where):
                self.assertNotIn(_FAKE_TOKEN, text)

    def test_the_secret_label_is_the_teardown_sweeps_selector(self):
        deploy = re.search(r'^GITLAB_FORGE_SECRET_LABEL="([^"]+)"$', _CI_DEPLOY.read_text(encoding="utf-8"), re.MULTILINE)
        teardown = re.search(r'^SWEEP_SELECTOR="([^"]+)"$', (_REPO_ROOT / "hack" / "ci-teardown.sh").read_text(encoding="utf-8"), re.MULTILINE)
        self.assertIsNotNone(deploy)
        self.assertIsNotNone(teardown)
        self.assertEqual(deploy.group(1), teardown.group(1))
        self.assertEqual(deploy.group(1), _TEARDOWN_SELECTOR)

    def test_a_failed_read_applies_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, _, kubectl_argv, applied = self._run(pathlib.Path(tmp), gcloud_exit="1")
        self.assertNotEqual(proc.returncode, 0)
        # The client-side render is in the same pipe as the read and may run;
        # what must not run is any apply, Secret or Namespace.
        self.assertEqual(applied, "")
        self.assertNotIn("apply -f -", kubectl_argv)
        self.assertIn("could not render the GitLab forge Secret from Secret Manager", proc.stderr)



def _preflight_function():
    text = _CI_DEPLOY.read_text(encoding="utf-8")
    start = text.find("preflight_gitlab_forge_secret() {")
    assert start != -1, "preflight_gitlab_forge_secret() not found in hack/ci-deploy.sh"
    end = text.find("\n}\n", start)
    return text[start : end + len("\n}\n")]


class CiDeployGitlabPreflightTest(unittest.TestCase):
    """The token is read once before the image build, and a failed read stops the run there."""

    def _run(self, tmp_path, gcloud_exit):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gcloud.argv"
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{log}"\n'
            f'[ "{gcloud_exit}" = "0" ] || exit "{gcloud_exit}"\n'
            f'printf "%s" "{_FAKE_TOKEN}"\n',
            encoding="utf-8",
        )
        gcloud.chmod(gcloud.stat().st_mode | 0o111)
        script = (
            "set -euo pipefail\n"
            'PROJECT_ID="kube-agents-evals-9"; GITLAB_AGENT_SM_SECRET="gitlab-agent-token"\n'
            f'GITLAB_SECRETS_PROJECT="{_GITLAB_SECRETS_PROJECT}"\n'
            + _preflight_function()
            + "preflight_gitlab_forge_secret\n"
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=get_isolated_test_env(bin_dir=bin_dir)
        )
        return proc, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_a_readable_secret_passes_and_the_value_is_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, argv = self._run(pathlib.Path(tmp), "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            f"secrets versions access latest --secret=gitlab-agent-token --project={_GITLAB_SECRETS_PROJECT}", argv
        )
        self.assertNotIn("--project=kube-agents-evals-9", argv)
        self.assertNotIn(_FAKE_TOKEN, proc.stdout + proc.stderr)

    def test_an_unreadable_secret_stops_before_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, _ = self._run(pathlib.Path(tmp), "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stopping before the build", proc.stderr)
        self.assertIn("gitlab-agent-token", proc.stderr)

    def test_the_pair_has_one_home(self):
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        self.assertIn(f'GITLAB_SECRETS_PROJECT="{_GITLAB_SECRETS_PROJECT}"', text)
        # Every Secret Manager read of the pair names that home, none the lease.
        reads = [ln for ln in text.splitlines() if "secrets versions access latest" in ln]
        self.assertEqual(len(reads), 2, reads)
        for ln in reads:
            self.assertIn('--project="${GITLAB_SECRETS_PROJECT}"', ln)

    def test_the_preflight_runs_before_the_image_build(self):
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        call = text.find("\n  preflight_gitlab_forge_secret\n")
        build = text.find("gcloud builds submit")
        self.assertNotEqual(call, -1)
        self.assertLess(call, build)


if __name__ == "__main__":
    unittest.main()
