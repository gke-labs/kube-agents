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
        cls.raw_docs = _documents(cls.template)
        cls.docs = {}
        cls.headless = None
        for doc in cls.raw_docs:
            if "clusterIP: None" in doc:
                cls.headless = doc
                continue
            cls.docs.setdefault(_kind(doc), doc)

    def test_service_type_is_a_literal_cluster_ip(self) -> None:
        # The fourth document is the headless LiteLLM peers Service the console
        # resolves to reach each LiteLLM pod; the rest are the console's
        # environment service account and RBAC bindings.
        self.assertEqual(
            [_kind(d) for d in self.raw_docs],
            ["Deployment", "Service", "NetworkPolicy", "Service", "ServiceAccount", "ClusterRole",
             "ClusterRoleBinding", "Role", "RoleBinding"],
        )
        self.assertIn("clusterIP: None", self.raw_docs[3])
        service = self.docs["Service"]
        self.assertRegex(service, r"(?m)^\s+type: ClusterIP$")
        self.assertRegex(service, r"(?m)^\s+port: 8080$")
        self.assertNotIn(".Values.webConsole.service", self.template)
        self.assertNotIn("nodePort", self.template)

    def test_schema_refuses_a_service_type(self) -> None:
        schema = json.loads(SCHEMA.read_text())
        web_console = schema["properties"]["webConsole"]
        self.assertFalse(web_console.get("additionalProperties", True))
        self.assertNotIn("service", web_console["properties"])
        values = yaml.safe_load(VALUES.read_text())
        self.assertNotIn("service", values["webConsole"])

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

    def test_console_chats_only_through_the_agent_gateway(self) -> None:
        # Chat turns go to Hermes. LITELLM_PEERS_HOST reads LiteLLM's metrics,
        # and LITELLM_URL is read by one route, POST /api/assist, which edits
        # a feedback draft's wording with a single completion; neither carries
        # a chat turn, and Session KV and the proxy port stay out.
        deployment = self.docs["Deployment"]
        self.assertIn("HERMES_URL", deployment)
        self.assertRegex(deployment, r"svc\.cluster\.local:8642")
        for gone in ("SESSION_KV", "8699", ":4000", "LITELLM_BASE", "OPENAI_"):
            self.assertNotIn(gone, deployment, f"the console still wires {gone}")
        self.assertEqual(deployment.count("inference-gateway"), 1, "the gateway URL is wired once, for /api/assist")

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

    def test_environment_role_grants_only_platformagent_and_replicaset_access(self) -> None:
        role = yaml.safe_load(_plain_yaml(self.docs["Role"]))
        resources = [tuple(r.get("resources", ())) for r in role["rules"]]
        self.assertEqual(resources, [("platformagents",), ("replicasets",)])
        cluster_role = yaml.safe_load(_plain_yaml(self.docs["ClusterRole"]))
        for rule in cluster_role["rules"] + role["rules"]:
            if "pods" in rule.get("resources", ()):
                self.assertEqual(rule.get("verbs"), ["list"])
        binding = yaml.safe_load(_plain_yaml(self.docs["RoleBinding"]))
        self.assertIn("subjects", binding)
        self.assertNotIn("subjects", binding.get("roleRef", {}))

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

    def test_image_pull_secrets_helper_is_not_double_wrapped(self) -> None:
        # kube-agents.imagePullSecrets already emits `imagePullSecrets:`, so
        # wrapping it in a second `imagePullSecrets:` key nests a map under a
        # list field and breaks Deployment rendering when pull secrets are set.
        deployment = self.docs["Deployment"]
        self.assertIn(
            '{{- with (include "kube-agents.imagePullSecrets" .) }}\n'
            "      {{- . | nindent 6 }}\n"
            "      {{- end }}",
            deployment,
        )
        self.assertNotRegex(deployment, r"(?m)^\s*imagePullSecrets:\s*$")

    def test_peers_service_truncates_release_name_before_suffix(self) -> None:
        # Truncating the full "<release>-web-console-litellm-peers" string at
        # 63 characters turns a 50- or 51-character release name into
        # "<release>-web-console", colliding with the console's own Service.
        # Both Services truncate the release prefix before appending their suffix.
        self.assertIn(
            '{{- $name := printf "%s-web-console" (.Release.Name | trunc 51 | trimSuffix "-") }}',
            self.template,
        )
        self.assertIn(
            '{{- $peersName := printf "%s-web-console-litellm-peers" (.Release.Name | trunc 37 | trimSuffix "-") }}',
            self.template,
        )

    def test_chat_platform_env_is_wired_from_integration(self) -> None:
        deployment = self.docs["Deployment"]
        self.assertIn("name: CHAT_PLATFORM", deployment)
        for platform in ("$integ.slack", "$integ.googleChat"):
            self.assertIn(platform, self.template)
        # The relay delivers to Slack and Google Chat only, so Teams alone
        # must not make the agent-session threads read-only.
        self.assertNotIn("$integ.teams", self.template)


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

    def test_chat_platform_names_only_platforms_the_relay_delivers_to(self) -> None:
        on = "--set", "webConsole.enabled=true"
        self.assertNotIn("CHAT_PLATFORM", self._console_env(self._render(*on)))
        slack = self._console_env(self._render(*on, "--set", "platformAgent.integration.slack.enabled=true"))
        self.assertEqual(slack["CHAT_PLATFORM"], "Slack")
        teams = self._console_env(self._render(*on, "--set", "platformAgent.integration.teams.enabled=true"))
        self.assertNotIn("CHAT_PLATFORM", teams)

    def test_long_release_name_keeps_the_peers_service_a_dns_label(self) -> None:
        # 50- through 53-character release names exercise both the 37-char
        # truncation on "<release>-web-console-litellm-peers" and the 51-char
        # truncation on "<release>-web-console", keeping both Services valid
        # 63-character DNS labels and distinct from each other.
        for release in ("r" * 47 + "-ab", "r" * 48 + "-ab", "r" * 49 + "-ab", "r" * 50 + "-ab"):
            with self.subTest(release=release):
                docs = self._render("--set", "webConsole.enabled=true", release=release)
                (service,) = self._headless(docs)
                name = service["metadata"]["name"]
                self.assertLessEqual(len(name), 63)
                self.assertTrue(name.endswith("-web-console-litellm-peers"))
                console_services = [
                    d for d in docs
                    if d["kind"] == "Service"
                    and d["spec"].get("clusterIP") != "None"
                    and d["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "web-console"
                ]
                self.assertEqual(len(console_services), 1)
                console_name = console_services[0]["metadata"]["name"]
                self.assertLessEqual(len(console_name), 63)
                self.assertTrue(console_name.endswith("-web-console"))
                self.assertNotEqual(name, console_name)
                deployment = next(
                    d for d in docs
                    if d["kind"] == "Deployment" and d["metadata"]["name"] == console_name
                )
                env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(env["LITELLM_PEERS_HOST"], f"{name}.ns.svc.cluster.local")

    def test_console_and_litellm_name_the_same_model(self) -> None:
        docs = self._render("--set", "webConsole.enabled=true", "--set", "litellm.modelDefaultName=my-model")
        config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
        self.assertIn("model: gemini/my-model", config["data"]["config.yaml"])
        self.assertEqual(self._console_env(docs)["MODEL_NAME"], "my-model")

    def test_assist_points_at_the_install_gateway_or_is_off(self) -> None:
        env = self._console_env(self._render("--set", "webConsole.enabled=true"))
        self.assertEqual(env["LITELLM_URL"], "http://inference-gateway.ns.svc.cluster.local")
        self.assertEqual(env["ASSIST_MODEL"], "model-default")

        for off in (("--set", "webConsole.assist.enabled=false"), ("--set", "litellm.enabled=false")):
            without = self._console_env(self._render("--set", "webConsole.enabled=true", *off))
            self.assertNotIn("LITELLM_URL", without, off)
            self.assertNotIn("ASSIST_MODEL", without, off)

        own = self._console_env(self._render(
            "--set", "webConsole.enabled=true", "--set", "litellm.enabled=false",
            "--set", "webConsole.assist.url=http://gateway.other.svc.cluster.local:4000",
            "--set", "webConsole.assist.model=editor",
        ))
        self.assertEqual(own["LITELLM_URL"], "http://gateway.other.svc.cluster.local:4000")
        self.assertEqual(own["ASSIST_MODEL"], "editor")

    def test_release_check_is_a_toggle_with_a_repo(self) -> None:
        env = self._console_env(self._render("--set", "webConsole.enabled=true"))
        self.assertEqual(env["RELEASE_CHECK"], "true")
        self.assertEqual(env["RELEASE_REPO"], "gke-labs/kube-agents")
        off = self._console_env(self._render("--set", "webConsole.enabled=true", "--set", "webConsole.releaseCheck.enabled=false"))
        self.assertEqual(off["RELEASE_CHECK"], "false")
        with self.assertRaises(subprocess.CalledProcessError):
            self._render("--set", "webConsole.enabled=true", "--set", "webConsole.releaseCheck.repo=not a repo")
        with self.assertRaises(subprocess.CalledProcessError):
            self._render("--set", "webConsole.enabled=true", "--set", "webConsole.assist.url=ftp://x")

    def test_environment_rbac_binds_console_sa_without_cluster_admin_or_pod_create(self) -> None:
        docs = self._render("--set", "webConsole.enabled=true")
        sa_names = [d["metadata"]["name"] for d in docs if d["kind"] == "ServiceAccount"]
        self.assertNotIn("kube-agents-upgrader", sa_names)
        role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "t-web-console-environment")
        resources = [tuple(r.get("resources", ())) for r in role["rules"]]
        self.assertEqual(resources, [("platformagents",), ("replicasets",)])
        cluster_role = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "t-web-console-environment")
        for r in cluster_role["rules"] + role["rules"]:
            if "pods" in (r.get("resources") or []):
                self.assertEqual(r.get("verbs"), ["list"])
            self.assertNotIn("*", r.get("verbs") or [])
        binding = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "t-web-console-environment")
        self.assertEqual(
            [(s["kind"], s["name"]) for s in binding.get("subjects") or []],
            [("ServiceAccount", "t-web-console")],
            "the console's namespace Role binds nobody",
        )
        for b in docs:
            if b["kind"] in ("ClusterRoleBinding", "RoleBinding"):
                self.assertNotEqual((b.get("roleRef") or {}).get("name"), "cluster-admin")



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
