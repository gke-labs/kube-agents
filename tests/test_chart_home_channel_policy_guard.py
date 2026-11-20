"""The chart's `homeChannelPolicy` rendering, for Slack and for Google Chat,
is guarded against an installed PlatformAgent CRD that predates the field, the
way every other new CRD field is: `lookup` the CRD, dig to the platform under
`spec.integration`, and `fail` with the convention's message when the key is
absent. `lookup` is empty under
`helm template`, so the guard can only be pinned as text."""

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "charts" / "kube-agents" / "templates" / "platform-agent-cr.yaml"
_CRD_LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"'
_CRD_DIG = '"spec" "properties" "integration" "properties" "{platform}" "properties"'
# Each platform's values key, and the template variable its guard digs into.
_PLATFORMS = (("slack", "$slackProps"), ("googleChat", "$googleChatProps"))
_FAIL_MESSAGE = "helm upgrade does not update CRDs"


class HomeChannelPolicyGuardTest(unittest.TestCase):
    def setUp(self):
        self.template = _TEMPLATE.read_text(encoding="utf-8")

    def _policy_block(self, platform):
        block = re.search(
            r"\{\{- with \$integ\." + platform + r"\.homeChannelPolicy \}\}\n(.*?)homeChannelPolicy: \{\{ \. \| quote \}\}\n",
            self.template, re.DOTALL)
        self.assertIsNotNone(block, f"the {platform} homeChannelPolicy block is missing from the CR template")
        return block.group(1)

    def test_the_policy_looks_up_the_installed_crd_before_it_renders(self):
        for platform, props in _PLATFORMS:
            with self.subTest(platform=platform):
                guard = self._policy_block(platform)
                self.assertIn(_CRD_LOOKUP, guard)
                self.assertIn(_CRD_DIG.format(platform=platform), guard)
                self.assertIn(f'hasKey {props} "homeChannelPolicy"', guard)

    def test_the_guard_fails_with_the_conventions_message(self):
        for platform, _ in _PLATFORMS:
            with self.subTest(platform=platform):
                guard = self._policy_block(platform)
                fail = re.search(r'\{\{- fail "([^"]*)" \}\}', guard)
                self.assertIsNotNone(fail, "the guard has no fail")
                self.assertIn(f"platformAgent.integration.{platform}.homeChannelPolicy", fail.group(1))
                self.assertIn(_FAIL_MESSAGE, fail.group(1))
                self.assertIn("charts/kube-agents/crds/", fail.group(1))


if __name__ == "__main__":
    unittest.main()
