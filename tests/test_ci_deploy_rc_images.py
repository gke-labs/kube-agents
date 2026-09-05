"""The smoke pipeline installs published images when RC_COMMIT_SHA is set.

`hack/ci-deploy.sh` builds the pull request's images and installs those. A
release-candidate eval must not: the candidate's images are already published,
and a rebuild from the same source is a different artefact — different base
digests, a different `_KUBE_AGENTS_VERSION` baked in — so grading a rebuild
would not grade what the release ships.

`RC_COMMIT_SHA` is the whole switch, and two properties matter more than the
feature itself.

The first is that the presubmit path is untouched. Every pull request in the
repository runs it, so a regression here is a repository-wide outage, and the
switch is unset on every one of those runs — which makes the unset path the
thing most worth pinning.

The second is that the release-candidate path names only tags. The published
operator image is `k8s-operator`; the one Artifact Registry carries is
`kube-agents-operator`, so a repository override copied across from the
presubmit path would 404. Dropping the override is also what carries the
credential broker: its image is not a chart value, the operator derives it from
the agent image (`resolveCredentialProxyImage`), so it follows whichever
repository the agent uses only as long as nothing overrides that repository.

The tests lift the real sections out of the real file and run them, the way
scripts/test_eval_dashboard_publish.py does, rather than grepping for flags —
a guard that greps passes for a section that has been commented out.
"""

import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

from tests.testing.common import create_mock_git_repo

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_RC_SHA = "b4ee5f3eb9c2aceb7f03460d2e573278ab9483fe"
_PRESUBMIT_TAG = "pr-1024-a3dc868"
_AR_REPO = "us-central1-docker.pkg.dev/kube-agents-evals/kube-agents"
_GHCR_PREFIX = "ghcr.io/gke-labs/kube-agents"

# The two sections under test, delimited by the banner that follows each.
_IMAGE_SOURCE_SECTION = (r"^# ─── 2a\. Image Source.*?", r"^export MODEL_PROVIDER=")
_BUILD_SECTION = (r"^# ─── 4\. Build Container Images.*?", r"^# ─── 5\. Chart Deployment")


def lifted(start: str, stop: str) -> str:
    """A run of top-level statements as written, lifted from the script."""
    src = _CI_DEPLOY.read_text(encoding="utf-8")
    match = re.search(rf"{start}(?={stop})", src, re.S | re.M)
    if match is None:  # pragma: no cover - a re-banner should say so loudly
        raise AssertionError(f"no section matching {start!r} in {_CI_DEPLOY}")
    return match.group(0)


def fake_candidate_tree(tmp: str, images_exist: bool) -> tuple[pathlib.Path, str]:
    """A stub tree standing in for a checkout at the candidate.

    Returns the `hack/` directory section 2a's SCRIPT_DIR points at, and the
    commit the tree is sitting on. Two things need it to be a real repository:
    section 2a sources `${SCRIPT_DIR}/../scripts/release/common.sh`, which is
    what lets a test choose whether the candidate's images are published
    without reaching a registry, and it then asks `git rev-parse HEAD` there to
    enforce the checkout contract.
    """
    _, repo, git = create_mock_git_repo(tmp)
    root = pathlib.Path(repo)
    (root / "hack").mkdir()
    release = root / "scripts" / "release"
    release.mkdir(parents=True)
    (release / "common.sh").write_text(
        textwrap.dedent(
            f"""\
            export REQUIRED_RELEASE_IMAGES=(k8s-operator platform-agent credential-proxy agent-sandbox)
            get_registry_prefix() {{ echo "{_GHCR_PREFIX}"; }}
            check_commit_images_exist() {{ return {0 if images_exist else 1}; }}
            """
        )
    )
    git("add", "-A")
    git("commit", "-m", "feat: a release candidate")
    return root / "hack", git("rev-parse", "HEAD").stdout.strip()


def run_image_source(
    script_dir: pathlib.Path, rc_commit_sha: str
) -> subprocess.CompletedProcess:
    """Run section 2a and print the decision it reached, one field per line."""
    script = "\n".join(
        [
            "set -euo pipefail",
            f'SCRIPT_DIR="{script_dir}"',
            f'export RC_COMMIT_SHA="{rc_commit_sha}"',
            f'export AR_REPO="{_AR_REPO}"',
            f'export TAG="{_PRESUBMIT_TAG}"',
            'export PULL_NUMBER="1024"',
            # Section 2 sets these just above the lifted section; the release
            # candidate branch overwrites them and the presubmit path must not.
            f'export IMG="{_AR_REPO}/kube-agents-operator:{_PRESUBMIT_TAG}"',
            f'export AGENT_IMAGE="{_AR_REPO}/platform-agent"',
            lifted(*_IMAGE_SOURCE_SECTION),
            'echo "TAG=${TAG}"',
            'echo "IMG=${IMG}"',
            'echo "AGENT_IMAGE=${AGENT_IMAGE}"',
            'echo "DEPLOY_SOURCE=${DEPLOY_SOURCE}"',
            'for arg in "${IMAGE_ARGS[@]}"; do echo "ARG=${arg}"; done',
        ]
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def run_build_section(rc_commit_sha: str) -> subprocess.CompletedProcess:
    """Run section 4 with `gcloud` stubbed, so a build announces itself."""
    script = "\n".join(
        [
            "set -euo pipefail",
            f'export RC_COMMIT_SHA="{rc_commit_sha}"',
            f'export AR_REPO="{_AR_REPO}"',
            f'export TAG="{_PRESUBMIT_TAG}"',
            'export PROJECT_ID="kube-agents-evals"',
            'export HERMES_AGENT_TAG="v0"',
            "BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)",
            'gcloud() { echo "BUILD SUBMITTED"; }',
            lifted(*_BUILD_SECTION),
        ]
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def fields(result: subprocess.CompletedProcess) -> dict[str, str]:
    out = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key != "ARG":
            out[key] = value
    return out


def image_args(result: subprocess.CompletedProcess) -> list[str]:
    return [
        line.partition("=")[2]
        for line in result.stdout.splitlines()
        if line.startswith("ARG=")
    ]


class PresubmitPathUnchangedTest(unittest.TestCase):
    """RC_COMMIT_SHA unset is every pull request in the repository."""

    def test_every_artifact_registry_flag_is_what_helm_gets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, _ = fake_candidate_tree(tmp, images_exist=True)
            result = run_image_source(fake_hack, rc_commit_sha="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            image_args(result),
            [
                "--set-string",
                f"operator.image.repository={_AR_REPO}/kube-agents-operator",
                "--set-string",
                f"operator.image.tag={_PRESUBMIT_TAG}",
                "--set-string",
                f"platformAgent.deployment.image.repository={_AR_REPO}/platform-agent",
                "--set-string",
                f"platformAgent.deployment.image.tag={_PRESUBMIT_TAG}",
                "--set-string",
                f"agentSandbox.image.repository={_AR_REPO}/agent-sandbox",
                "--set-string",
                f"agentSandbox.image.tag={_PRESUBMIT_TAG}",
            ],
            "the presubmit's Helm image flags must name every image the run "
            "built: every pull request runs this path, and an image left off "
            "installs whatever the chart defaults to instead of the build.",
        )

    def test_the_tag_and_image_exports_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, _ = fake_candidate_tree(tmp, images_exist=True)
            result = run_image_source(fake_hack, rc_commit_sha="")
        got = fields(result)
        self.assertEqual(got["TAG"], _PRESUBMIT_TAG)
        self.assertEqual(
            got["IMG"], f"{_AR_REPO}/kube-agents-operator:{_PRESUBMIT_TAG}"
        )
        self.assertEqual(got["AGENT_IMAGE"], f"{_AR_REPO}/platform-agent")

    def test_the_images_are_still_built(self) -> None:
        result = run_build_section(rc_commit_sha="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BUILD SUBMITTED", result.stdout)


class ReleaseCandidatePathTest(unittest.TestCase):
    def test_only_tags_are_set_and_no_repository_is_overridden(self) -> None:
        """The published operator image is `k8s-operator` and the Artifact
        Registry one is `kube-agents-operator`, so an override copied from the
        presubmit path would 404. The chart already defaults every image to the
        published path; only the tag has to be said."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, rc_sha = fake_candidate_tree(tmp, images_exist=True)
            result = run_image_source(fake_hack, rc_commit_sha=rc_sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            image_args(result),
            [
                "--set-string",
                f"operator.image.tag={rc_sha}",
                "--set-string",
                f"platformAgent.deployment.image.tag={rc_sha}",
                "--set-string",
                f"agentSandbox.image.tag={rc_sha}",
            ],
        )

    def test_no_flag_names_the_artifact_registry(self) -> None:
        """Stated separately from the equality above because this is the
        property the credential broker rides on: the operator derives the
        broker's image from the agent's repository, so overriding that
        repository moves the broker to a registry the candidate never
        published to."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, rc_sha = fake_candidate_tree(tmp, images_exist=True)
            result = run_image_source(fake_hack, rc_commit_sha=rc_sha)
        for arg in image_args(result):
            self.assertNotIn(_AR_REPO, arg)
            self.assertNotIn("image.repository", arg)

    def test_the_exported_references_name_the_published_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, rc_sha = fake_candidate_tree(tmp, images_exist=True)
            result = run_image_source(fake_hack, rc_commit_sha=rc_sha)
        got = fields(result)
        self.assertEqual(got["TAG"], rc_sha)
        self.assertEqual(got["IMG"], f"{_GHCR_PREFIX}/k8s-operator:{rc_sha}")
        self.assertEqual(got["AGENT_IMAGE"], f"{_GHCR_PREFIX}/platform-agent")
        self.assertIn(rc_sha[:7], got["DEPLOY_SOURCE"])

    def test_a_commit_with_no_published_images_stops_the_deploy(self) -> None:
        """Fifteen minutes earlier than the alternative. Without this the
        missing image surfaces as `helm --wait` timing out on
        ImagePullBackOff, which reads as a broken chart rather than as a
        commit docker-publish-ghcr.yml has not finished publishing."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, rc_sha = fake_candidate_tree(tmp, images_exist=False)
            result = run_image_source(fake_hack, rc_commit_sha=rc_sha)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no complete image set", result.stdout)
        self.assertIn(rc_sha, result.stdout)

    def test_a_tree_at_another_commit_stops_the_deploy(self) -> None:
        """Only the images come from the candidate. The chart, the CRDs, and
        bench/tasks come from whatever tree the job is sitting on, so an
        RC_COMMIT_SHA that does not match HEAD grades the candidate's images
        against another commit's everything-else — and nothing about the run
        would say so. The checkout is the caller's job, per
        hack/resolve-rc-target.sh's header, which makes "the caller forgot" a
        state this has to name rather than absorb."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, rc_sha = fake_candidate_tree(tmp, images_exist=True)
            result = run_image_source(fake_hack, rc_commit_sha=_RC_SHA)
        self.assertNotEqual(rc_sha, _RC_SHA)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(_RC_SHA, result.stdout)
        self.assertIn(rc_sha, result.stdout)
        self.assertIn("git checkout --detach", result.stdout)

    def test_nothing_is_built(self) -> None:
        result = run_build_section(rc_commit_sha=_RC_SHA)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            "BUILD SUBMITTED",
            result.stdout,
            "a release-candidate eval must grade the published images, not a "
            "rebuild of the same source.",
        )
        self.assertIn("Skipping image builds", result.stdout)


class HelmInvocationTest(unittest.TestCase):
    def test_the_release_takes_its_image_flags_from_the_array(self) -> None:
        """Section 2a decides; section 5 must not decide again. A second
        `--set-string ...image.repository=` on the helm line would win by
        being later and would silently undo the release-candidate path."""
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        # Anchored on the parameterized form #1185 introduced; the literal
        # "kube-agents" anchor this test merged with (#1170) predated that
        # rename on its branch and went green against a stale merge-ref.
        helm_call = text.partition('helm upgrade --install "${HELM_RELEASE_NAME}"')[
            2
        ].partition("--wait")[0]
        self.assertIn('"${IMAGE_ARGS[@]}"', helm_call)
        self.assertNotIn("image.repository", helm_call)
        self.assertNotIn("image.tag", helm_call)


if __name__ == "__main__":
    unittest.main()
