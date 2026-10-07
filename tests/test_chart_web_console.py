"""The web console's chart wiring keeps it reachable only through port-forward.

The console holds the Platform Agent's API key and has no login of its own, so
its access boundary is the chart: a ClusterIP Service whose type cannot be
changed, and a NetworkPolicy that refuses ingress from every other pod. A
values knob that reopened `LoadBalancer`, or a template that dropped the
policy, would let any workload — or any host — drive the agent through it.

It also pins the two wiring mistakes the first version shipped: a Session KV
URL (that server listens on the agent pod's loopback and no other pod can reach
it) and a LiteLLM fallback that answered as the bare model, not the agent.

The insights banner adds three things checked here: env that carries the model
and the install-time identity, a headless Service the console resolves to find
every LiteLLM replica, and the model helper both templates share so the banner
names the model LiteLLM serves.

`helm` is not installed on the runner that executes this suite, so this reads
the templates and the schema rather than a real render, the way
test_chart_topology_spread.py does. The render tests at the end run only where
helm is installed.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_web_console.py' -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "kube-agents"
TEMPLATE = CHART_DIR / "templates" / "web-console.yaml"
LITELLM_TEMPLATE = CHART_DIR / "templates" / "litellm.yaml"
HELPERS = CHART_DIR / "templates" / "_helpers.tpl"
VALUES = CHART_DIR / "values.yaml"
SCHEMA = CHART_DIR / "values.schema.json"
IMAGES = REPO_ROOT / "images.json"
RELEASE_COMMON = REPO_ROOT / "scripts" / "release" / "common.sh"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker-publish-ghcr.yml"

IMAGE_NAME = "web-console"
DOCKERFILE = "a2a/Dockerfile.web-console"
MODEL_HELPER = 'include "kube-agents.litellmModel" .'
INSIGHTS_ENV = ("MODEL_NAME", "MODEL_PROVIDER", "LITELLM_PEERS_HOST", "AGENT_KSA", "AGENT_GSA", "AGENT_ROLES")
HELM_REQUIRED = (
    "--set", "platformAgent.harness.clusterName=c1",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=p",
)


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
        cls.docs = {}
        cls.headless = None
        for doc in _documents(cls.template):
            if "clusterIP: None" in doc:
                cls.headless = doc
                continue
            cls.docs.setdefault(_kind(doc), doc)

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
        deployment_labels = re.search(
            r"selector:\s*\n\s*matchLabels:\s*\n((?:\s+\S+: .*\n)+)", self.docs["Deployment"]
        ).group(1)
        for key in spec["podSelector"]["matchLabels"]:
            self.assertIn(key, deployment_labels, f"policy selects on {key}, which the Deployment does not carry")

    def test_console_chats_only_through_the_agent_gateway(self) -> None:
        # LITELLM_PEERS_HOST reads LiteLLM's metrics, never its chat API: the
        # inference-gateway Service and the proxy port stay out.
        deployment = self.docs["Deployment"]
        self.assertIn("HERMES_URL", deployment)
        self.assertRegex(deployment, r"svc\.cluster\.local:8642")
        for gone in ("SESSION_KV", "8699", ":4000", "inference-gateway", "LITELLM_URL", "LITELLM_BASE"):
            self.assertNotIn(gone, deployment, f"the console still wires {gone}")

    def test_insights_env_is_wired(self) -> None:
        deployment = self.docs["Deployment"]
        for name in INSIGHTS_ENV:
            self.assertRegex(deployment, rf"(?m)^\s+- name: {name}$", f"{name} is not set on the console")
        litellm_only = re.search(
            r"\{\{- if \.Values\.litellm\.enabled \}\}(.*?)\{\{- end \}\}", deployment, flags=re.S
        )
        self.assertIsNotNone(litellm_only, "the LiteLLM env is not guarded by litellm.enabled")
        for name in ("MODEL_NAME", "MODEL_PROVIDER", "LITELLM_PEERS_HOST"):
            self.assertIn(f"name: {name}", litellm_only.group(1))
        self.assertIn(".Values.platformAgent.security.serviceAccountName", deployment)
        self.assertIn('join "," ($identity.roles | default list)', deployment)

    def test_model_helper_is_shared_with_litellm(self) -> None:
        helpers = HELPERS.read_text()
        self.assertIn('define "kube-agents.litellmModel"', helpers)
        self.assertIn("$defaultModels := dict", helpers)
        litellm = LITELLM_TEMPLATE.read_text()
        self.assertIn(MODEL_HELPER, litellm)
        self.assertNotIn("$defaultModels := dict", litellm, "litellm.yaml keeps a second copy of the defaults")
        self.assertIn(MODEL_HELPER, self.docs["Deployment"])

    def test_headless_service_finds_the_litellm_pods(self) -> None:
        self.assertIsNotNone(self.headless, "no headless Service for the LiteLLM replicas")
        service = yaml.safe_load(_plain_yaml(self.headless))
        self.assertEqual(service["kind"], "Service")
        self.assertEqual(service["spec"]["clusterIP"], "None")
        self.assertEqual([p["port"] for p in service["spec"]["ports"]], [8080])
        litellm = LITELLM_TEMPLATE.read_text()
        for key, value in service["spec"]["selector"].items():
            self.assertRegex(
                litellm,
                rf"(?s)kind: Deployment.*?selector:\s*\n\s*matchLabels:\s*\n\s*{key}: {value}\n",
                f"the LiteLLM Deployment does not select on {key}: {value}",
            )
        # Rendered only inside the console's own guard and LiteLLM's.
        self.assertRegex(
            self.template,
            r"(?s)\{\{- if \.Values\.litellm\.enabled \}\}\s*---\s*\{\{- /\*.*?\*/\}\}\s*apiVersion: v1\s*kind: Service",
        )

    def test_litellm_policy_admits_the_console(self) -> None:
        # The metrics fetch crosses litellm-policy. Both copies (this chart's
        # and the operator's) admit every pod in the namespace on 8080.
        litellm = LITELLM_TEMPLATE.read_text()
        self.assertRegex(litellm, r"(?s)ingress:\s*\n\s*- from:\s*\n\s*- podSelector: \{\}\s*\n\s*ports:\s*\n\s*- port: 8080")
        operator = (REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_litellm_policy.go").read_text()
        self.assertRegex(operator, r"(?s)tcpPort\(litellmPort\),.*?PodSelector: &metav1\.LabelSelector\{\}")
        policy = yaml.safe_load(_plain_yaml(self.docs["NetworkPolicy"]))
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"], "the console's policy now restricts egress")

    def test_schema_bounds_the_agent_identity(self) -> None:
        schema = json.loads(SCHEMA.read_text())
        identity = schema["properties"]["webConsole"]["properties"]["agentIdentity"]
        self.assertFalse(identity.get("additionalProperties", True))
        self.assertEqual(identity["properties"]["gcpServiceAccount"]["type"], "string")
        self.assertEqual(identity["properties"]["roles"]["type"], "array")
        values = yaml.safe_load(VALUES.read_text())["webConsole"]["agentIdentity"]
        self.assertEqual(values, {"gcpServiceAccount": "", "roles": []})
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        whole = json.loads(SCHEMA.read_text())
        ok = {"webConsole": {"agentIdentity": {"gcpServiceAccount": "a@p.iam.gserviceaccount.com", "roles": ["roles/x"]}}}
        jsonschema.validate(ok, whole)
        for bad in (
            {"gcpServiceAccount": 1},
            {"roles": "roles/x"},
            {"roles": ["a,b"]},
            {"roles": [""]},
            {"user": "someone"},
        ):
            with self.subTest(bad=bad), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({"webConsole": {"agentIdentity": bad}}, whole)

    def test_replica_count_is_not_a_value(self) -> None:
        values = yaml.safe_load(VALUES.read_text())
        self.assertNotIn("replicaCount", values["webConsole"])
        self.assertRegex(self.docs["Deployment"], r"(?m)^\s+replicas: 1$")

    def test_quota_preflight_counts_the_console(self) -> None:
        self.assertRegex(HELPERS.read_text(), r'(?s)if \.Values\.webConsole\.enabled.*?append \$chartWorkloads')

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
        # A node that cached `latest` never picks up a new build under IfNotPresent.
        self.assertIn('(ternary "Always" "IfNotPresent" (eq $tag "latest"))', self.template)
        self.assertEqual(yaml.safe_load(VALUES.read_text())["webConsole"]["image"]["pullPolicy"], "")

    def test_image_inventory_renders_the_console(self) -> None:
        self.assertRegex(
            (REPO_ROOT / "hack" / "check-image-inventory.sh").read_text(),
            r"(?m)^check_toggle webConsole --set webConsole\.enabled=true$",
        )


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class WebConsoleRenderTest(unittest.TestCase):
    """A real render, where helm is installed."""

    def _render(self, *values: str, release: str = "t") -> list[dict]:
        out = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-n", "ns", *HELM_REQUIRED, *values],
            capture_output=True, text=True, check=True,
        ).stdout
        return [d for d in yaml.safe_load_all(out) if d]

    def _headless(self, docs: list[dict]) -> list[dict]:
        return [d for d in docs if d["kind"] == "Service" and d["spec"].get("clusterIP") == "None"]

    def _console_env(self, docs: list[dict]) -> dict[str, str]:
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "t-web-console")
        return {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}

    def test_headless_service_only_with_both_toggles(self) -> None:
        self.assertEqual(self._headless(self._render()), [])
        self.assertEqual(self._headless(self._render("--set", "webConsole.enabled=true", "--set", "litellm.enabled=false")), [])
        both = self._headless(self._render("--set", "webConsole.enabled=true"))
        self.assertEqual(len(both), 1)
        self.assertEqual(both[0]["spec"]["selector"], {"app": "litellm"})

    def test_env_carries_model_and_identity(self) -> None:
        env = self._console_env(self._render(
            "--set", "webConsole.enabled=true",
            "--set", "litellm.modelProvider=anthropic",
            "--set", "webConsole.agentIdentity.gcpServiceAccount=a@p.iam.gserviceaccount.com",
            "--set", "webConsole.agentIdentity.roles={roles/container.viewer,roles/logging.viewer}",
        ))
        self.assertEqual(env["MODEL_NAME"], "claude-opus-5")
        self.assertEqual(env["MODEL_PROVIDER"], "anthropic")
        self.assertEqual(env["LITELLM_PEERS_HOST"], "t-web-console-litellm-peers.ns.svc.cluster.local")
        self.assertEqual(env["AGENT_KSA"], "kubeagents-platform-agent")
        self.assertEqual(env["AGENT_GSA"], "a@p.iam.gserviceaccount.com")
        self.assertEqual(env["AGENT_ROLES"], "roles/container.viewer,roles/logging.viewer")

        without = self._console_env(self._render("--set", "webConsole.enabled=true", "--set", "litellm.enabled=false"))
        for name in ("MODEL_NAME", "MODEL_PROVIDER", "LITELLM_PEERS_HOST"):
            self.assertNotIn(name, without)

    def test_long_release_name_keeps_the_peers_service_a_dns_label(self) -> None:
        # 50 characters plus "-web-console-litellm-peers" is 76, over the
        # 63-character limit. The cut lands after "-web-console-", and the
        # trailing "-" is trimmed.
        release = "r" * 47 + "-ab"
        docs = self._render("--set", "webConsole.enabled=true", release=release)
        (service,) = self._headless(docs)
        name = service["metadata"]["name"]
        self.assertEqual(name, f"{release}-web-console")
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == f"{release}-web-console")
        env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["LITELLM_PEERS_HOST"], f"{name}.ns.svc.cluster.local")

    def test_console_and_litellm_name_the_same_model(self) -> None:
        docs = self._render("--set", "webConsole.enabled=true", "--set", "litellm.modelDefaultName=my-model")
        config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
        self.assertIn("model: gemini/my-model", config["data"]["config.yaml"])
        self.assertEqual(self._console_env(docs)["MODEL_NAME"], "my-model")


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
        self.assertRegex(RELEASE_COMMON.read_text(), rf'(?m)^\s+"{IMAGE_NAME}"$')

    def test_publish_workflow_builds_and_signs_it(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text()
        self.assertIn(f"file: {DOCKERFILE}", workflow)
        self.assertIn(f"/{IMAGE_NAME}@$WEB_CONSOLE_DIGEST", workflow)
        self.assertTrue((REPO_ROOT / DOCKERFILE).is_file())


if __name__ == "__main__":
    unittest.main()
