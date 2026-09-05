"""Tests for the k8s-operator Makefile deploy contract (#526).

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

The deploy targets promise two things: they apply the manifests as committed
(no regeneration during install/deploy), and `make deploy` pins the image in a
throwaway copy of config/ rather than rewriting the tracked
config/manager/kustomization.yaml. Both revert silently — re-adding a
`manifests` dependency or simplifying the recipe back to an in-tree
`kustomize edit` fails no build, and the symptom is a dirty working tree
noticed some time later. `make -n` prints the recipe without a cluster, so
assert on that.
"""

import pathlib
import shutil
import subprocess
import unittest

_OPERATOR_DIR = pathlib.Path(__file__).resolve().parents[1] / "k8s-operator"


def _make_n(target):
    result = subprocess.run(
        ["make", "-n", target, "IMG=example.com/operator:test"],
        cwd=_OPERATOR_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"make -n {target} failed ({result.returncode}):\n{result.stderr}"
        )
    return result.stdout


class DeployContractTest(unittest.TestCase):
    def test_deploy_does_not_rewrite_tracked_kustomization(self):
        recipe = _make_n("deploy")
        # The old recipe was `cd config/manager && kustomize edit set image`,
        # which dirties the tracked kustomization.yaml on every deploy. The
        # temp-copy recipe cds into "$tmp/config/manager" instead, which this
        # assertion deliberately does not match.
        self.assertNotRegex(recipe, r"cd config/manager")
        self.assertIn("mktemp", recipe, "deploy should pin the image in a throwaway copy of config/")

    def test_deploy_does_not_regenerate_manifests(self):
        self.assertNotIn("controller-gen", _make_n("deploy"))

    def test_install_does_not_regenerate_manifests(self):
        recipe = _make_n("install")
        self.assertNotIn("controller-gen", recipe)
        self.assertNotIn("prettier --write", recipe)

    def test_uninstall_does_not_regenerate_manifests(self):
        self.assertNotIn("controller-gen", _make_n("uninstall"))

    def test_dev_rebuild_agent_points_at_a_script_that_exists(self):
        """The helpers moved to the repository root's scripts/dev/, and this
        target ran ./scripts/dev/... relative to k8s-operator/ — a path that
        stopped existing with the move. `make -n` still prints a recipe for a
        missing script, so resolve what it names against the filesystem.
        """
        recipe = _make_n("dev-rebuild-agent")
        script = "scripts/dev/dev_rebuild_agent.sh"
        self.assertIn(script, recipe)
        repo_root = _OPERATOR_DIR.parent
        self.assertTrue(
            (repo_root / script).is_file(),
            f"{script} must resolve from the repository root, not {_OPERATOR_DIR}",
        )
        self.assertFalse(
            (_OPERATOR_DIR / script).exists(),
            "k8s-operator/scripts/ is gone; the recipe must not resolve there",
        )

    def test_deploy_litellm_syntax_valid(self):
        for provider in ("", "gemini", "vertex_ai", "gemma4", "vllm"):
            with self.subTest(provider=provider):
                args = ["make", "-n", "deploy-litellm", "KUSTOMIZE=/bin/true"]
                if provider:
                    args.append(f"MODEL_PROVIDER={provider}")
                result = subprocess.run(
                    args,
                    cwd=_OPERATOR_DIR,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                syntax_check = subprocess.run(
                    ["sh", "-n"],
                    input=result.stdout,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(
                    syntax_check.returncode,
                    0,
                    f"Shell syntax error in make deploy-litellm (provider={provider}):\n{syntax_check.stderr}",
                )

    def test_kustomize_build_vllm_and_gemma4_packages(self):
        tool = shutil.which("kustomize")
        cmd_prefix = [tool, "build"] if tool else [shutil.which("kubectl"), "kustomize"]
        if not cmd_prefix[0]:
            self.skipTest("neither kustomize nor kubectl is available in PATH")
        for pkg in (
            "config/integrations/vllm-gemma",
            "config/integrations/litellm/overlays/gemma4",
        ):
            with self.subTest(package=pkg):
                res = subprocess.run(
                    cmd_prefix + [str(_OPERATOR_DIR / pkg)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"kustomize build failed for {pkg}:\n{res.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
