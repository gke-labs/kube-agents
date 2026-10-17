"""Pins the vllm-gemma example's server shape.

The example once ran vLLM's demo server (`vllm.entrypoints.api_server`), which has no
`/v1/chat/completions`, so the `openai_compatible` gateway got 404s. It also set
`LD_LIBRARY_PATH` to a `${...}` value, which Kubernetes does not expand. These tests fail
if either comes back, or if the served model name drifts from the README's connect table.
"""

import re
import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "examples" / "vllm-gemma"
_DEPLOYMENT = _EXAMPLE / "deployment.yaml"
_README = _EXAMPLE / "README.md"
_OPENAI_SERVER_IMAGE = "docker.io/vllm/vllm-openai@sha256:"


def _server_container():
    docs = [d for d in yaml.safe_load_all(_DEPLOYMENT.read_text()) if d and d.get("kind") == "Deployment"]
    containers = docs[0]["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == "inference-server")


def _arg(container, flag):
    prefix = f"{flag}="
    return next((a[len(prefix):] for a in container["args"] if a.startswith(prefix)), None)


class VllmGemmaExampleTest(unittest.TestCase):
    def test_runs_the_openai_compatible_server(self):
        container = _server_container()
        self.assertTrue(container["image"].startswith(_OPENAI_SERVER_IMAGE), container["image"])
        # The image's entrypoint is `vllm serve`; a command override replaces it.
        self.assertNotIn("command", container)
        self.assertEqual(container["args"][0], "$(MODEL_ID)")
        self.assertIn("--enable-auto-tool-choice", container["args"])
        self.assertEqual(_arg(container, "--tool-call-parser"), "gemma4")

    def test_served_name_matches_the_readme(self):
        served = _arg(_server_container(), "--served-model-name")
        row = re.search(r"^\|\s*`MODEL_DEFAULT_NAME`\s*\|\s*`([^`]+)`", _README.read_text(), re.M)
        self.assertIsNotNone(row, "README has no MODEL_DEFAULT_NAME row")
        self.assertEqual(served, row.group(1))

    def test_window_fits_a_scheduled_audit_request(self):
        # The agent sends its whole context on every request, and a scheduled
        # audit's request reaches about 83,000 tokens; the gateway asks for
        # MODEL_MAX_TOKENS (4,096 in the README) of output on top. A 32k window
        # refused most agent requests.
        window = int(_arg(_server_container(), "--max-model-len"))
        self.assertGreaterEqual(window, 83000 + 4096)

    def test_no_env_value_relies_on_shell_expansion(self):
        for env in _server_container().get("env", []):
            self.assertNotIn("${", str(env.get("value", "")), env["name"])


if __name__ == "__main__":
    unittest.main()
