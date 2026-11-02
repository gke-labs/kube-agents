"""The two token-minter debug-pod recipes label the pod the way Minty's NetworkPolicy admits."""

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
RECIPE_DOCS = (
    REPO / "docs" / "site" / "src" / "content" / "docs" / "deploy" / "token-minter.md",
    REPO / "k8s-operator" / "config" / "integrations" / "github" / "README.md",
)
POLICY_FILES = (
    REPO / "k8s-operator" / "config" / "integrations" / "github" / "deployment.yaml.template",
    REPO / "charts" / "kube-agents" / "templates" / "github-minter.yaml",
)
RECIPE_RE = re.compile(r'kubectl run debug-box[\s\S]*?--labels="([^="]+)=([^"]+)"')
MINTER_SELECTOR = "app: github-token-minter"
REQUIRED_SENTENCE = "label is required"


def minter_ingress_block(policy: pathlib.Path) -> str:
    """The `ingress:` block of the NetworkPolicy that selects Minty, and nothing else of the file."""
    for document in policy.read_text().split("\n---"):
        if "kind: NetworkPolicy" in document and MINTER_SELECTOR in document and "ingress:" in document:
            return document.split("ingress:", 1)[1].split("egress:", 1)[0]
    raise AssertionError(f"{policy}: no NetworkPolicy selecting {MINTER_SELECTOR} with an ingress block")


class TokenMinterRecipeTest(unittest.TestCase):
    def test_each_recipe_labels_the_debug_pod_with_the_label_minty_admits(self):
        for doc in RECIPE_DOCS:
            with self.subTest(doc=doc.name):
                text = doc.read_text()
                match = RECIPE_RE.search(text)
                self.assertIsNotNone(match, f"{doc}: no `kubectl run debug-box ... --labels=key=value` recipe")
                key, value = match.groups()
                admitted = f'{key}: "{value}"'
                for policy in POLICY_FILES:
                    self.assertIn(admitted, minter_ingress_block(policy), f"{policy}: its Minty ingress does not admit {key}={value}, the label {doc.name} gives the debug pod")
                if REQUIRED_SENTENCE in text:
                    self.assertIn(f"`{key}={value}` {REQUIRED_SENTENCE}", text, f"{doc}: the sentence under the recipe names a different label")


if __name__ == "__main__":
    unittest.main()
