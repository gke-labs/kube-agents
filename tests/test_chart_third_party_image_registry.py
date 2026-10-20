"""Third-party images in the Helm chart and full-install Terraform composition
remain on upstream registries when thirdPartyImageRegistry is unset/empty, even
when imageRegistry is set (#3503).

A custom first-party imageRegistry must not rewrite third-party images (LiteLLM,
fluent-bit, cleanup hook, GitHub token minter, Hindsight) onto the first-party
prefix, where an operator who only mirrored kube-agents images never mirrored them.

When global.imageRegistry is set without global.thirdPartyImageRegistry, the chart's
NOTES.txt warns operators that third-party images will still pull from upstream public
registries.

Run: python3 -m unittest discover -s tests -p 'test_chart_third_party_image_registry.py' -v
"""

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_COMPOSITION = _REPO_ROOT / "terraform" / "examples" / "full-install"
_MAIN_TF = _COMPOSITION / "main.tf"
_HELM = shutil.which("helm")

_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]


def _render_chart(*args) -> list[dict]:
    cmd = [_HELM, "template", "test-release", str(_CHART), *_REQUIRED, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(proc.stdout) if doc is not None]


@unittest.skipIf(_HELM is None, "helm is required for chart template rendering tests")
class ChartThirdPartyImageRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        chart = pathlib.Path(cls._tmp.name) / "kube-agents"
        shutil.copytree(_CHART, chart)
        templates = chart / "templates"
        notes = (templates / "NOTES.txt").read_text()
        (templates / "NOTES.txt").unlink()
        (templates / "_notes_under_test.tpl").write_text(
            '{{- define "notes-under-test" -}}\n' + notes + "\n{{- end -}}\n"
        )
        (templates / "notes-under-test.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: notes-under-test\n"
            'data:\n  notes: {{ include "notes-under-test" . | toJson }}\n'
        )
        cls.chart = chart

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _render_notes(self, *args) -> str:
        cmd = [
            _HELM, "template", "test-release", str(self.chart),
            "--show-only", "templates/notes-under-test.yaml",
            *_REQUIRED,
            *args,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        doc = yaml.safe_load(proc.stdout)
        return doc["data"]["notes"]
    def test_default_render_leaves_images_on_default_registries(self):
        docs = _render_chart()
        rendered_images = self._extract_images(docs)

        # First party should default to ghcr.io/gke-labs/kube-agents
        self.assertTrue(any("ghcr.io/gke-labs/kube-agents/k8s-operator" in img for img in rendered_images))
        # Third party should be upstream
        self.assertTrue(any("ghcr.io/berriai/litellm" in img for img in rendered_images))
        self.assertTrue(any("docker.io/fluent/fluent-bit" in img for img in rendered_images))
        self.assertTrue(any("docker.io/alpine/k8s" in img for img in rendered_images))

    def test_image_registry_only_does_not_rewrite_third_party_images(self):
        mirror = "us-east4-docker.pkg.dev/my-proj/kube-agents"
        docs = _render_chart(
            "--set", f"global.imageRegistry={mirror}",
            "--set", "githubMinter.enabled=true",
            "--set", "githubMinter.org=foo",
            "--set", "githubMinter.repo=bar",
            "--set", "platformAgent.integration.github.gitRepo=foo/bar",
            "--set", "platformAgent.integration.github.org=foo",
            "--set", "hindsight.enabled=true",
        )
        rendered_images = self._extract_images(docs)

        # First party images must be rewritten to mirror
        self.assertTrue(
            any(img.startswith(f"{mirror}/k8s-operator") for img in rendered_images),
            f"Expected k8s-operator under mirror {mirror}, got: {rendered_images}",
        )
        self.assertTrue(
            any(img.startswith(f"{mirror}/platform-agent") for img in rendered_images),
            f"Expected platform-agent under mirror {mirror}, got: {rendered_images}",
        )

        # Third-party images must REMAIN upstream, NOT under mirror
        self.assertFalse(
            any(img.startswith(f"{mirror}/litellm") for img in rendered_images),
            f"LiteLLM should not be rewritten to first-party mirror, got: {rendered_images}",
        )
        self.assertFalse(
            any(img.startswith(f"{mirror}/fluent-bit") for img in rendered_images),
            f"fluent-bit should not be rewritten to first-party mirror, got: {rendered_images}",
        )
        self.assertFalse(
            any(img.startswith(f"{mirror}/k8s:") for img in rendered_images),
            f"cleanup hook (k8s) should not be rewritten to first-party mirror, got: {rendered_images}",
        )
        self.assertFalse(
            any(img.startswith(f"{mirror}/github-token-minter") for img in rendered_images),
            f"minter should not be rewritten to first-party mirror, got: {rendered_images}",
        )
        self.assertFalse(
            any(img.startswith(f"{mirror}/hindsight") for img in rendered_images),
            f"hindsight should not be rewritten to first-party mirror, got: {rendered_images}",
        )
        self.assertFalse(
            any(img.startswith(f"{mirror}/pgvector") for img in rendered_images),
            f"pgvector should not be rewritten to first-party mirror, got: {rendered_images}",
        )

        self.assertTrue(any("ghcr.io/berriai/litellm" in img for img in rendered_images))
        self.assertTrue(any("docker.io/fluent/fluent-bit" in img for img in rendered_images))
        self.assertTrue(any("docker.io/alpine/k8s" in img for img in rendered_images))
        self.assertTrue(any("github-token-minter-server" in img for img in rendered_images))
        self.assertTrue(any("hindsight-api" in img for img in rendered_images))
        self.assertTrue(any("pgvector" in img for img in rendered_images))

    def test_both_registries_rewrites_first_and_third_party(self):
        first_mirror = "us-east4-docker.pkg.dev/my-proj/kube-agents"
        third_mirror = "us-east4-docker.pkg.dev/my-proj/third-party"
        docs = _render_chart(
            "--set", f"global.imageRegistry={first_mirror}",
            "--set", f"global.thirdPartyImageRegistry={third_mirror}",
        )
        rendered_images = self._extract_images(docs)

        # First party on first_mirror
        self.assertTrue(any(img.startswith(f"{first_mirror}/k8s-operator") for img in rendered_images))
        # Third party on third_mirror
        self.assertTrue(any(img.startswith(f"{third_mirror}/litellm") for img in rendered_images))
        self.assertTrue(any(img.startswith(f"{third_mirror}/fluent-bit") for img in rendered_images))
        self.assertTrue(any(img.startswith(f"{third_mirror}/k8s") for img in rendered_images))

    def test_notes_warns_when_first_party_registry_set_and_third_party_empty(self):
        mirror = "us-east4-docker.pkg.dev/my-proj/kube-agents"
        notes = self._render_notes("--set", f"global.imageRegistry={mirror}")
        self.assertIn("WARNING: global.imageRegistry is set", notes)
        self.assertIn("global.thirdPartyImageRegistry is empty", notes)
        self.assertIn("will still be pulled from their upstream public registries", notes)
        self.assertIn("cert-manager is not rendered by this chart", notes)

    def test_notes_silent_when_both_registries_set(self):
        mirror = "us-east4-docker.pkg.dev/my-proj/kube-agents"
        notes = self._render_notes(
            "--set", f"global.imageRegistry={mirror}",
            "--set", f"global.thirdPartyImageRegistry={mirror}",
        )
        self.assertNotIn("WARNING: global.imageRegistry is set", notes)

    def test_notes_silent_when_both_registries_empty(self):
        notes = self._render_notes()
        self.assertNotIn("WARNING: global.imageRegistry is set", notes)

    def _extract_images(self, docs: list[dict]) -> list[str]:
        images = []
        for doc in docs:
            kind = doc.get("kind", "")
            # Check containers
            spec = doc.get("spec", {})
            template_spec = spec.get("template", {}).get("spec", {})
            containers = template_spec.get("containers", []) + template_spec.get("initContainers", [])
            for c in containers:
                if "image" in c:
                    images.append(c["image"])
                for env in c.get("env", []):
                    if env.get("name") in ("PLATFORM_AGENT_IMAGE", "FLUENT_BIT_IMAGE", "AGENT_SANDBOX_IMAGE", "OPERATOR_IMAGE"):
                        if "value" in env:
                            images.append(env["value"])
            if kind == "PlatformAgent":
                dep = spec.get("deployment", {})
                if "image" in dep:
                    images.append(dep["image"])
        return images


class CompositionWiringTest(unittest.TestCase):
    def test_composition_third_party_registry_does_not_fall_back_to_image_registry(self):
        main_tf = _MAIN_TF.read_text()
        # third_party_registry must be trimsuffix(var.third_party_image_registry, "/")
        # and NOT fall back to var.image_registry
        self.assertNotIn("var.third_party_image_registry != \"\" ? var.third_party_image_registry : var.image_registry", main_tf)
        match = re.search(r'third_party_registry\s*=\s*trimsuffix\(\s*var\.third_party_image_registry,\s*"/"\s*\)', main_tf)
        self.assertIsNotNone(match, "third_party_registry should trim var.third_party_image_registry without falling back to var.image_registry")

    def test_composition_warns_when_image_registry_set_and_third_party_empty(self):
        main_tf = _MAIN_TF.read_text()
        self.assertIn('check "unmirrored_third_party_images"', main_tf)
        self.assertIn(
            'trimsuffix(var.image_registry, "/") == "" || local.third_party_registry != ""',
            main_tf,
        )
        self.assertIn('var.image_registry is set', main_tf)
        self.assertIn('var.third_party_image_registry is empty', main_tf)
