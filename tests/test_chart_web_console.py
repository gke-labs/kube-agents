"""The web console's chart wiring keeps it reachable only through port-forward.

The console holds the Platform Agent's API key and has no login of its own, so
its access boundary is the chart: a ClusterIP Service whose type cannot be
changed, and a NetworkPolicy that refuses ingress from every other pod. A
values knob that reopened `LoadBalancer`, or a template that dropped the
policy, would let any workload — or any host — drive the agent through it.

It also pins the two wiring mistakes the first version shipped: a Session KV
URL (that server listens on the agent pod's loopback and no other pod can reach
it) and a LiteLLM fallback that answered as the bare model, not the agent.

`helm` is not installed on the runner that executes this suite, so this reads
the templates and the schema rather than a real render, the way
test_chart_topology_spread.py does.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_web_console.py' -v
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from tests.testing.release import parse_required_release_images
CHART_DIR = REPO_ROOT / "charts" / "kube-agents"
TEMPLATE = CHART_DIR / "templates" / "web-console.yaml"
HELPERS = CHART_DIR / "templates" / "_helpers.tpl"
VALUES = CHART_DIR / "values.yaml"
SCHEMA = CHART_DIR / "values.schema.json"
IMAGES = REPO_ROOT / "images.json"
RELEASE_COMMON = REPO_ROOT / "scripts" / "release" / "common.sh"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker-publish-ghcr.yml"

IMAGE_NAME = "web-console"
DOCKERFILE = "a2a/Dockerfile.web-console"


def _documents(text: str) -> list[str]:
    """The template's YAML documents, Helm directives and comments stripped."""
    body = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.S)
    return [d for d in body.split("\n---\n") if d.strip()]


def _plain_yaml(doc: str) -> str:
    """A document parseable as YAML: directive-only lines dropped, inline ones stubbed."""
    lines = [l for l in doc.splitlines() if not re.fullmatch(r"\s*\{\{.*\}\}\s*", l)]
    return re.sub(r"\{\{.*?\}\}", "x", "\n".join(lines))


def _kind(doc: str) -> str:
    match = re.search(r"^kind:\s*(\S+)", doc, flags=re.M)
    return match.group(1) if match else ""


class WebConsoleChartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text()
        cls.docs = {_kind(d): d for d in _documents(cls.template)}

    def test_service_type_is_a_literal_cluster_ip(self) -> None:
        service = self.docs["Service"]
        self.assertRegex(service, r"(?m)^\s+type: ClusterIP$")
        self.assertNotIn(".Values.webConsole.service.type", self.template)
        self.assertNotIn("nodePort", self.template)

    def test_schema_refuses_a_service_type(self) -> None:
        schema = json.loads(SCHEMA.read_text())
        service = schema["properties"]["webConsole"]["properties"]["service"]
        self.assertFalse(service.get("additionalProperties", True))
        self.assertEqual(set(service["properties"]), {"port"})

    def test_network_policy_refuses_all_ingress(self) -> None:
        self.assertIn("NetworkPolicy", self.docs, "the console renders without its NetworkPolicy")
        policy = yaml.safe_load(_plain_yaml(self.docs["NetworkPolicy"]))
        spec = policy["spec"]
        self.assertIn("Ingress", spec["policyTypes"])
        self.assertEqual(spec.get("ingress"), [], "the policy admits some ingress")
        deployment = yaml.safe_load(_plain_yaml(self.docs["Deployment"]))
        self.assertEqual(
            spec["podSelector"]["matchLabels"],
            deployment["spec"]["selector"]["matchLabels"],
            "policy podSelector.matchLabels does not equal Deployment selector.matchLabels",
        )
        self.assertEqual(
            spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"],
            deployment["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/name"),
        )
        self.assertIn('{{- include "kube-agents.labels" . | nindent 8 }}', self.docs["Deployment"])

    def test_console_reaches_only_the_agent_gateway(self) -> None:
        deployment = self.docs["Deployment"]
        self.assertIn("HERMES_URL", deployment)
        self.assertRegex(deployment, r"svc\.cluster\.local:8642")
        for gone in ("SESSION_KV", "LITELLM", "8699", ":4000"):
            self.assertNotIn(gone, deployment, f"the console still wires {gone}")

    def test_replica_count_is_not_a_value(self) -> None:
        values = yaml.safe_load(VALUES.read_text())
        self.assertNotIn("replicaCount", values["webConsole"])
        self.assertRegex(self.docs["Deployment"], r"(?m)^\s+replicas: 1$")

    def test_quota_preflight_counts_the_console(self) -> None:
        self.assertRegex(
            HELPERS.read_text(),
            r'(?s)if \.Values\.webConsole\.enabled -\}\}(?:(?!\{\{- end).)*append \$chartWorkloads \(dict "values" \.Values\.webConsole ',
        )

    def test_off_by_default(self) -> None:
        values = yaml.safe_load(VALUES.read_text())
        self.assertIs(values["webConsole"]["enabled"], False)

    def test_refuses_to_render_without_the_agent(self) -> None:
        # A console with no Platform Agent renders a Deployment whose secret
        # ref and upstream Service do not exist; it fails at render instead.
        self.assertRegex(
            self.template,
            r'(?s)if \.Values\.webConsole\.enabled \}\}\s*\{\{- if not \.Values\.platformAgent\.enabled \}\}\s*\{\{- fail ',
        )

    def test_no_access_widening_knobs(self) -> None:
        self.assertNotIn("ALLOWED_HOSTS", self.template)
        self.assertFalse((CHART_DIR / "values-poc.yaml").exists(), "values-poc.yaml bypasses install.sh")

    def test_latest_tag_is_pulled_every_time(self) -> None:
        # A node that cached `latest` never picks up a new build under IfNotPresent,
        # and an integer tag must be cast to string before `eq $tag "latest"`.
        self.assertIn('| default .Chart.AppVersion | toString }}', self.template)
        self.assertIn('(ternary "Always" "IfNotPresent" (eq $tag "latest"))', self.template)
        self.assertEqual(yaml.safe_load(VALUES.read_text())["webConsole"]["image"]["pullPolicy"], "")

    def test_image_inventory_renders_the_console(self) -> None:
        self.assertRegex(
            (REPO_ROOT / "hack" / "check-image-inventory.sh").read_text(),
            r"(?m)^check_toggle webConsole --set webConsole\.enabled=true$",
        )


class WebConsoleImageTest(unittest.TestCase):
    """The image the chart pulls is built, signed, inventoried and released."""

    def test_inventory_entry(self) -> None:
        entries = {e["name"]: e for e in json.loads(IMAGES.read_text())["images"]}
        self.assertIn(IMAGE_NAME, entries)
        entry = entries[IMAGE_NAME]
        self.assertEqual(entry["origin"], "first-party")
        self.assertEqual(entry["repository"], f"ghcr.io/gke-labs/kube-agents/{IMAGE_NAME}")
        values = yaml.safe_load(VALUES.read_text())
        self.assertEqual(values["webConsole"]["image"]["repository"], entry["repository"])

    def test_release_requires_the_image(self) -> None:
        self.assertIn(IMAGE_NAME, parse_required_release_images(RELEASE_COMMON.read_text()))

    def test_publish_workflow_builds_and_signs_it(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text()
        self.assertIn(f"file: {DOCKERFILE}", workflow)
        self.assertIn(f"/{IMAGE_NAME}@$WEB_CONSOLE_DIGEST", workflow)
        self.assertTrue((REPO_ROOT / DOCKERFILE).is_file())


if __name__ == "__main__":
    unittest.main()
