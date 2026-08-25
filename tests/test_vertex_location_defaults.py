"""The Vertex AI serving location must not default to a cluster location.

A Vertex model is only callable from a location that serves it, and the
cluster's region need not be one of them -- `gemini-3.5-flash`, the `vertex_ai`
default model, is not served from `us-central1`, which is `DEFAULT_REGION`. On
a zonal Standard cluster the cluster location is a zone (`us-central1-a`) and
is not a valid Vertex location at all. Every surface that establishes this
default therefore resolves to `global` rather than inheriting a cluster
location.

Five surfaces establish it: `installer_common.sh` (the constant), `install.sh`
(the install path and the `--menu` reconfigure path), `common.sh`, the
`full-install` Terraform composition, the Helm chart, and the admin console
(its gateway default and the UI prefill that is submitted verbatim). The shell
halves are covered behaviourally in `test_install_script.py` and
`test_installer_common.py`; the rest are only reachable through a rendered
artifact, so they are pinned here.
"""

import pathlib
import re
import shutil
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_LITELLM_TEMPLATE = _CHART / "templates" / "litellm.yaml"
_FULL_INSTALL_MAIN = (
    _REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
)
_ADMIN_GATEWAY = _REPO_ROOT / "admin_console" / "llm_gateway.py"
_ADMIN_PAGE = _REPO_ROOT / "admin_console" / "pages" / "llm_gateway.py"


class VertexLocationDefaultTest(unittest.TestCase):
    def test_chart_template_defaults_the_location_to_global(self):
        text = _LITELLM_TEMPLATE.read_text()
        match = re.search(r"\$vertexLocation\s*:=\s*(.+)", text)
        self.assertIsNotNone(match, "no $vertexLocation assignment in litellm.yaml")
        expression = match.group(1)
        self.assertIn('default "global"', expression)
        self.assertNotIn("harness.location", expression)

    def test_terraform_defaults_the_location_to_global(self):
        text = _FULL_INSTALL_MAIN.read_text()
        match = re.search(r"^\s*vertex_location\s*=\s*(.+)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "no vertex_location local in full-install main.tf")
        expression = match.group(1)
        self.assertIn('"global"', expression)
        self.assertNotIn("var.location", expression)

    def test_admin_console_defaults_the_location_to_global(self):
        # Both halves matter: the gateway applies the default, and the page
        # prefills the text input that is submitted verbatim. A regional
        # prefill would override the gateway default without touching it.
        for path in (_ADMIN_GATEWAY, _ADMIN_PAGE):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertIn('"global"', text)
                self.assertNotIn("region_for_location", text)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_chart_renders_global_for_a_zonal_cluster(self):
        """The rendered value, not just the template source.

        A zonal harness location is the case the old default got wrong: it is
        never a valid Vertex location, so rendering it proves the inheritance
        is gone rather than merely rephrased.
        """
        rendered = subprocess.run(
            [
                "helm", "template", "t", str(_CHART),
                "--set", "litellm.modelProvider=vertex_ai",
                "--set", "platformAgent.harness.projectId=my-proj",
                "--set", "platformAgent.harness.clusterName=my-cluster",
                "--set", "platformAgent.harness.location=us-central1-a",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        match = re.search(
            r"name: VERTEXAI_LOCATION\s*\n\s*value: \"([^\"]*)\"", rendered
        )
        self.assertIsNotNone(match, "VERTEXAI_LOCATION not rendered")
        self.assertEqual(match.group(1), "global")

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_chart_still_honours_an_explicit_location(self):
        rendered = subprocess.run(
            [
                "helm", "template", "t", str(_CHART),
                "--set", "litellm.modelProvider=vertex_ai",
                "--set", "platformAgent.harness.projectId=my-proj",
                "--set", "platformAgent.harness.clusterName=my-cluster",
                "--set", "platformAgent.harness.location=us-central1-a",
                "--set", "litellm.vertex.location=europe-west3",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        match = re.search(
            r"name: VERTEXAI_LOCATION\s*\n\s*value: \"([^\"]*)\"", rendered
        )
        self.assertIsNotNone(match, "VERTEXAI_LOCATION not rendered")
        self.assertEqual(match.group(1), "europe-west3")


if __name__ == "__main__":
    unittest.main()
