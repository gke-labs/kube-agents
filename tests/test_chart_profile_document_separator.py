"""Each optional AgentProfile renders as its own YAML document (#6325).

In charts/kube-agents/templates/profiles.yaml, the template directive
before each `---` separator must keep the newline before the separator.
A directive that ends with `-}}` removes that newline. The render then
glues the separator to the last content line of the document before it
(`memory: 512Mi---`). A YAML parser reads the two AgentProfiles as one
document, the later keys win, and only the last profile reaches the API
server.

Run: python3 -m unittest discover -s tests -p 'test_chart_profile_document_separator.py' -v
"""

import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_BOTH_PROFILES = [
    "--set", "profiles.tse.enabled=true",
    "--set", "profiles.cluster.enabled=true",
]
# A document separator glued to the end of a content line. YAML reads
# such a line as content, not as a separator, and merges the documents.
_GLUED_SEPARATOR = re.compile(r"\S---$")
_EXPECTED_PROFILE_NAMES = ["cluster", "tse"]


@unittest.skipUnless(_HELM, "helm not on PATH")
class ProfileDocumentSeparatorTest(unittest.TestCase):
    """Render the chart with both optional profiles enabled."""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [_HELM, "template", "test-release", str(_CHART), *_REQUIRED, *_BOTH_PROFILES],
            capture_output=True, text=True, check=True,
        )
        cls.rendered = proc.stdout

    def test_both_profiles_render_as_two_documents(self):
        profiles = [
            doc for doc in yaml.safe_load_all(self.rendered)
            if doc and doc.get("kind") == "AgentProfile"
        ]
        names = sorted(doc["metadata"]["name"] for doc in profiles)
        self.assertEqual(names, _EXPECTED_PROFILE_NAMES)

    def test_no_separator_is_glued_to_a_content_line(self):
        glued = [
            line for line in self.rendered.splitlines()
            if _GLUED_SEPARATOR.search(line)
        ]
        self.assertEqual(glued, [], "document separators glued to content lines")


if __name__ == "__main__":
    unittest.main()
