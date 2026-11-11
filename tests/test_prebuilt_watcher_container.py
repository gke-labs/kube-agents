"""Every prebuilt stack that reads k8s-event-watcher's log names the container the operator runs it in.

The watcher is not a container of its own: it runs inside the gateway Pod's
agent-api-auth sidecar (buildAgentAPIAuthSidecar in the operator). A stack whose
``agent_container`` default names another container reads an empty log with
kubectl's error discarded, counts zero fires and times out its wait on every
apply: #3681 for autoops-incident, #4223 for triage-start-burst.
"""

from __future__ import annotations

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PREBUILT = _REPO_ROOT / "bench" / "tf" / "prebuilt"
_OPERATOR_MANIFESTS = (
    _REPO_ROOT
    / "k8s-operator"
    / "internal"
    / "controller"
    / "platformagent_manifests.go"
)
_OPERATOR_CONTAINER_RE = re.compile(
    r'^\s*agentAPIAuthContainerName\s*=\s*"([^"]+)"', re.MULTILINE
)
#: A stack reads the watcher's log when it runs `kubectl logs ... -c "${var.agent_container}"`.
#: Only backslash-continued lines may sit between the two, so a `kubectl logs`
#: followed some lines later by a `kubectl exec -c` does not class the stack.
_WATCHER_LOG_READ_RE = re.compile(
    r'kubectl logs\b(?:[^\n]|\\\n)*?-c "\$\{var\.agent_container\}"'
)
_AGENT_CONTAINER_DEFAULT_RE = re.compile(
    r'variable "agent_container" \{.*?^\s*default\s*=\s*"([^"]+)"',
    re.DOTALL | re.MULTILINE,
)


class WatcherContainerDefaultTest(unittest.TestCase):
    def test_every_stack_that_reads_the_watcher_log_defaults_to_the_operator_container(
        self,
    ):
        operator = _OPERATOR_CONTAINER_RE.search(_OPERATOR_MANIFESTS.read_text())
        self.assertIsNotNone(
            operator, f"agentAPIAuthContainerName not found in {_OPERATOR_MANIFESTS}"
        )
        readers = {}
        for main_tf in sorted(_PREBUILT.glob("*/main.tf")):
            if _WATCHER_LOG_READ_RE.search(main_tf.read_text()):
                default = _AGENT_CONTAINER_DEFAULT_RE.search(
                    main_tf.with_name("variables.tf").read_text()
                )
                readers[main_tf.parent.name] = default.group(1) if default else None
        self.assertIn(
            "triage-start-burst",
            readers,
            "the read pattern no longer matches the stack this test was written for",
        )
        wrong = {
            name: value for name, value in readers.items() if value != operator.group(1)
        }
        self.assertEqual(
            wrong,
            {},
            f"these stacks read the watcher's log from a container the gateway Pod does not run it in (operator: {operator.group(1)!r})",
        )


if __name__ == "__main__":
    unittest.main()
