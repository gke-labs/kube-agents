"""Every Prow run of `hack/ci-deploy.sh` tags its images with a tag no node has seen.

Pool clusters keep their nodes' image cache between Boskos leases, and the
images this script installs pull IfNotPresent. A tag a node has pulled before
therefore runs whatever it pointed at then. Before gke-labs/kube-agents#2766
every periodic tagged its build `pr-local-latest`, so the next lane installed a
mix of its own images and older ones; a presubmit re-run of the same head did
the same after main moved, because Prow builds the head merged onto the
current main but the tag named only the head.

These tests lift section 2's tag assembly out of the real file and run it
under each job type's environment, the way tests/test_ci_deploy_rc_images.py
does, rather than grepping for the expression.
"""

import json
import pathlib
import re
import subprocess
import tempfile
import unittest

from tests.testing.common import create_mock_git_repo

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_POOL_PROVISIONER = _REPO_ROOT / "scripts" / "provision_ci_pool_project.sh"

_AR_REPO = "us-central1-docker.pkg.dev/kube-agents-evals-3/kube-agents"
_PULL_NUMBER = "1024"
_PULL_SHA = "a3dc868f1e2d3c4b5a69788796a5b4c3d2e1f0a9"
_BASE_SHA = "8af1d67c5a5d69bb367ad5bb3ee2029996dcf1aa"
_BUILD_ID = "2108361692286029824"
_OTHER_BUILD_ID = "2108437444511666176"
_SHA_CHARS = 7

# Prow's decoration for each job type, as podinfo.json shows it. A periodic
# gets no PULL_* at all, even with extra_refs (EnvForSpec returns before them).
_PERIODIC_ENV = {
    "JOB_TYPE": "periodic",
    "JOB_NAME": "ci-kube-agents-eval-next",
    "BUILD_ID": _BUILD_ID,
}
_PRESUBMIT_ENV = {
    "JOB_TYPE": "presubmit",
    "JOB_NAME": "pull-kube-agents-smoke-test",
    "BUILD_ID": _BUILD_ID,
    "PULL_NUMBER": _PULL_NUMBER,
    "PULL_PULL_SHA": _PULL_SHA,
    "PULL_BASE_SHA": _BASE_SHA,
}

# The tag assembly through the image exports that read it: everything section 2
# runs after `ensure_helm` and before the A2A operator overrides.
_TAG_SECTION = (r"(?<=^ensure_helm\n).*?", r"^# The operator's A2A image overrides")
_CONSTANT_LINE = re.compile(r"^readonly CI_IMAGE_TAG_\w+=.*$", re.M)
_PROW_ENV_VARS = (
    "JOB_TYPE",
    "JOB_NAME",
    "BUILD_ID",
    "PULL_NUMBER",
    "PULL_PULL_SHA",
    "PULL_BASE_SHA",
    "PULL_REFS",
    "AR_REPO",
)


def lifted() -> str:
    src = _CI_DEPLOY.read_text(encoding="utf-8")
    start, stop = _TAG_SECTION
    match = re.search(rf"{start}(?={stop})", src, re.S | re.M)
    if match is None:  # pragma: no cover - a re-banner should say so loudly
        raise AssertionError(f"no tag section in {_CI_DEPLOY}")
    return "\n".join([*_CONSTANT_LINE.findall(src), match.group(0)])


def run_tag_section(script_dir: pathlib.Path, env: dict[str, str]) -> dict[str, str]:
    exports = [f"export {k}={json.dumps(v)}" for k, v in env.items()]
    script = "\n".join(
        [
            "set -euo pipefail",
            *(f"unset {name}" for name in _PROW_ENV_VARS),
            f'SCRIPT_DIR="{script_dir}"',
            'export PROJECT_ID="kube-agents-evals-3"',
            *exports,
            lifted(),
            'echo "TAG=${TAG}"',
            'echo "IMG=${IMG}"',
            'echo "AGENT_TAG=${AGENT_TAG}"',
            'echo "IMAGE_TAG=${IMAGE_TAG}"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"tag section failed: {result.stderr}")
    return dict(line.partition("=")[::2] for line in result.stdout.splitlines())


def checkout(tmp: str) -> tuple[pathlib.Path, str]:
    """A git checkout standing in for the one Prow clones; returns hack/ and HEAD."""
    _, repo, git = create_mock_git_repo(tmp)
    hack = pathlib.Path(repo) / "hack"
    hack.mkdir()
    return hack, git("rev-parse", "HEAD").stdout.strip()


def cleanup_tag_prefixes() -> list[str]:
    """The tag prefixes the pool repository's Delete rule for tagged images keys on."""
    src = _POOL_PROVISIONER.read_text(encoding="utf-8")
    match = re.search(r"<<'EOF'\n(.*?)\nEOF\n", src, re.S)
    if match is None:  # pragma: no cover
        raise AssertionError(f"no cleanup policy heredoc in {_POOL_PROVISIONER}")
    prefixes = [
        prefix
        for rule in json.loads(match.group(1))
        if rule["action"]["type"] == "Delete"
        and rule["condition"].get("tagState") == "tagged"
        for prefix in rule["condition"].get("tagPrefixes", [])
    ]
    if not prefixes:  # pragma: no cover
        raise AssertionError("the pool cleanup policy deletes no tagged images")
    return prefixes


class PeriodicTagTest(unittest.TestCase):
    def test_a_periodic_tag_names_the_checkout_and_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hack, head = checkout(tmp)
            got = run_tag_section(hack, _PERIODIC_ENV)
        self.assertEqual(got["TAG"], f"pr-local-{head[:_SHA_CHARS]}-{_BUILD_ID}")

    def test_two_periodics_of_one_commit_get_different_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hack, _ = checkout(tmp)
            first = run_tag_section(hack, _PERIODIC_ENV)
            second = run_tag_section(hack, {**_PERIODIC_ENV, "BUILD_ID": _OTHER_BUILD_ID})
        self.assertNotEqual(first["TAG"], second["TAG"])

    def test_no_checkout_and_no_build_keeps_the_old_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got = run_tag_section(pathlib.Path(tmp) / "hack", {})
        self.assertEqual(got["TAG"], "pr-local-latest")


class PresubmitTagTest(unittest.TestCase):
    def test_a_presubmit_tag_keeps_its_shape_and_adds_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hack, _ = checkout(tmp)
            got = run_tag_section(hack, _PRESUBMIT_ENV)
        self.assertEqual(
            got["TAG"], f"pr-{_PULL_NUMBER}-{_PULL_SHA[:_SHA_CHARS]}-{_BUILD_ID}"
        )

    def test_a_rerun_of_the_same_head_gets_a_different_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hack, _ = checkout(tmp)
            first = run_tag_section(hack, _PRESUBMIT_ENV)
            rerun = run_tag_section(hack, {**_PRESUBMIT_ENV, "BUILD_ID": _OTHER_BUILD_ID})
        self.assertNotEqual(first["TAG"], rerun["TAG"])

    def test_without_build_id_the_presubmit_shape_is_unchanged(self) -> None:
        env = {k: v for k, v in _PRESUBMIT_ENV.items() if k != "BUILD_ID"}
        with tempfile.TemporaryDirectory() as tmp:
            hack, _ = checkout(tmp)
            got = run_tag_section(hack, env)
        self.assertEqual(got["TAG"], f"pr-{_PULL_NUMBER}-{_PULL_SHA[:_SHA_CHARS]}")


class TagConsumersTest(unittest.TestCase):
    def test_every_image_export_carries_the_tag(self) -> None:
        for env in (_PERIODIC_ENV, _PRESUBMIT_ENV):
            with self.subTest(job=env["JOB_TYPE"]), tempfile.TemporaryDirectory() as tmp:
                hack, _ = checkout(tmp)
                got = run_tag_section(hack, env)
                self.assertEqual(got["IMG"], f"{_AR_REPO}/kube-agents-operator:{got['TAG']}")
                self.assertEqual(got["AGENT_TAG"], got["TAG"])
                self.assertEqual(got["IMAGE_TAG"], got["TAG"])

    def test_every_tag_is_one_the_pool_cleanup_policy_deletes(self) -> None:
        prefixes = cleanup_tag_prefixes()
        for env in (_PERIODIC_ENV, _PRESUBMIT_ENV, {}):
            with self.subTest(job=env.get("JOB_TYPE", "local")), tempfile.TemporaryDirectory() as tmp:
                hack, _ = checkout(tmp)
                tag = run_tag_section(hack, env)["TAG"]
                self.assertTrue(
                    any(tag.startswith(p) for p in prefixes),
                    f"{tag} escapes the pool repository's cleanup policy "
                    f"(Delete rule prefixes {prefixes}); a unique tag per run "
                    "that escapes it is kept forever",
                )


if __name__ == "__main__":
    unittest.main()
