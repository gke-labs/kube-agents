"""The chart's shadow-mode replay proxy (litellm.replayShadow).

Off, the chart renders nothing of it. On, the proxy takes the inference-gateway
Service while LiteLLM moves behind inference-gateway-upstream, the two told
apart by one label key, and the proxy keeps `app: litellm` because the worker
pods' egress policies admit only that label. The schema refuses the proxy
without an image and a job kind. The quota preflight counts its pods.

The render tests need helm: without it they skip on a laptop and fail with CI
set, the rule tests/test_rendered_workloads_rollout_quota.py follows.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "litellm.yaml"
_CI_ENV = _ROOT / "hack" / "ci-env.sh"

_REQUIRED = (
    "platformAgent.harness.clusterName=ci-cluster",
    "platformAgent.harness.location=us-central1",
    "platformAgent.harness.projectId=ci-project",
)
_ROLE = "kubeagents.x-k8s.io/inference-role"
_IMAGE_REPOSITORY = "us-central1-docker.pkg.dev/pool/kube-agents/replay-proxy-shadow"
_IMAGE_TAG = "pr-4700-abcdef0-1790000000000000000"
_ON = (
    "litellm.replayShadow.enabled=true",
    f"litellm.replayShadow.image.repository={_IMAGE_REPOSITORY}",
    f"litellm.replayShadow.image.tag={_IMAGE_TAG}",
    "litellm.replayShadow.job.kind=presubmit",
    "litellm.replayShadow.job.project=pool",
    "litellm.replayShadow.job.build=1790000000000000000",
    "litellm.replayShadow.job.pr=4700",
)
_PROXY = "inference-replay-shadow"
_PROXY_CONTAINER = "replay-shadow-proxy"
_LITELLM_CONTAINER = "litellm-container"
_HELM_TIMEOUT_SECONDS = 120

_PROBE_TEMPLATE = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-requirements
data:
  requirements: |
    {{ include "kube-agents.quotaRequirements" . }}
"""


def _helm():
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            raise AssertionError("helm is not on PATH but CI is set: install helm rather than skipping")
        raise unittest.SkipTest("helm not installed and CI is unset")
    return "helm"


def _render(sets=(), chart=_CHART, show_only=None, typed_sets=()):
    """Render the chart; `sets` go in as strings bar true/false, `typed_sets` as Helm parses them."""
    args = [_helm(), "template", "test-release", str(chart)]
    for value in (*_REQUIRED, *sets):
        args += ["--set-string" if "=" in value and not value.endswith(("=true", "=false")) else "--set", value]
    for value in typed_sets:
        args += ["--set", value]
    if show_only:
        args += ["--show-only", show_only]
    return subprocess.run(args, capture_output=True, text=True, timeout=_HELM_TIMEOUT_SECONDS)


def _docs(text):
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _find(docs, kind, name):
    for doc in docs:
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"no {kind}/{name} rendered")


def _selects(selector, labels):
    return all(labels.get(k) == v for k, v in selector.items())


class TemplateTextTest(unittest.TestCase):
    """What the template says, without rendering it."""

    def test_callbacks_are_registered_only_through_their_classified_names(self):
        self.assertNotRegex(_TEMPLATE.read_text(), r"proxy_handler_instance|\"otel\"|\"prometheus\"")

    def test_proxy_container_is_never_the_name_the_litellm_log_collector_reads(self):
        text = _TEMPLATE.read_text()
        self.assertIn(f"- name: {_PROXY_CONTAINER}", text)
        collector = _CI_ENV.read_text()
        self.assertIn(f'readonly LITELLM_LOG_CONTAINER="{_LITELLM_CONTAINER}"', collector)
        self.assertNotEqual(_PROXY_CONTAINER, _LITELLM_CONTAINER)


class DisabledTest(unittest.TestCase):
    def test_off_by_default_renders_nothing_of_the_proxy(self):
        res = _render()
        self.assertEqual(res.returncode, 0, res.stderr)
        for absent in (_ROLE, "inference-gateway-upstream", _PROXY, _PROXY_CONTAINER, "REPLAY_"):
            self.assertNotIn(absent, res.stdout)

    def test_off_leaves_inference_gateway_selecting_app_litellm_alone(self):
        docs = _docs(_render(show_only="templates/litellm.yaml").stdout)
        self.assertEqual(_find(docs, "Service", "inference-gateway")["spec"]["selector"], {"app": "litellm"})
        labels = _find(docs, "Deployment", "litellm")["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels, {"app": "litellm"})


class EnabledTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        res = _render(_ON, show_only="templates/litellm.yaml")
        if res.returncode != 0:
            raise AssertionError(res.stderr)
        cls.docs = _docs(res.stdout)
        cls.proxy = _find(cls.docs, "Deployment", _PROXY)
        cls.litellm = _find(cls.docs, "Deployment", "litellm")
        cls.proxy_labels = cls.proxy["spec"]["template"]["metadata"]["labels"]
        cls.litellm_labels = cls.litellm["spec"]["template"]["metadata"]["labels"]
        cls.container = cls.proxy["spec"]["template"]["spec"]["containers"][0]
        cls.env = {e["name"]: e.get("value") for e in cls.container["env"]}

    def test_both_pod_kinds_keep_app_litellm_and_differ_by_role(self):
        self.assertEqual(self.proxy_labels, {"app": "litellm", _ROLE: "replay-shadow"})
        self.assertEqual(self.litellm_labels, {"app": "litellm", _ROLE: "litellm"})

    def test_inference_gateway_selects_the_proxy_and_never_litellm(self):
        selector = _find(self.docs, "Service", "inference-gateway")["spec"]["selector"]
        self.assertTrue(_selects(selector, self.proxy_labels))
        self.assertFalse(_selects(selector, self.litellm_labels))

    def test_upstream_selects_litellm_and_never_the_proxy(self):
        upstream = _find(self.docs, "Service", "inference-gateway-upstream")["spec"]
        self.assertTrue(_selects(upstream["selector"], self.litellm_labels))
        self.assertFalse(_selects(upstream["selector"], self.proxy_labels))
        self.assertEqual(upstream["ports"], [{"protocol": "TCP", "port": 80, "targetPort": 8080}])
        self.assertEqual((self.env["REPLAY_UPSTREAM_HOST"], self.env["REPLAY_UPSTREAM_PORT"]),
                         ("inference-gateway-upstream", "80"))

    def test_proxy_deployment_selects_only_its_own_pods(self):
        selector = self.proxy["spec"]["selector"]["matchLabels"]
        self.assertTrue(_selects(selector, self.proxy_labels))
        self.assertFalse(_selects(selector, self.litellm_labels))

    def test_no_selector_keys_on_pod_template_hash(self):
        selectors = [
            _find(self.docs, "Service", "inference-gateway")["spec"]["selector"],
            _find(self.docs, "Service", "inference-gateway-upstream")["spec"]["selector"],
            self.proxy["spec"]["selector"],
            _find(self.docs, "NetworkPolicy", "inference-replay-shadow-egress")["spec"],
        ]
        for selector in selectors:
            self.assertNotIn("pod-template-hash", json.dumps(selector))

    def test_proxy_spreads_over_its_own_pods_of_one_revision(self):
        [spread] = self.proxy["spec"]["template"]["spec"]["topologySpreadConstraints"]
        self.assertEqual(spread["labelSelector"]["matchLabels"], self.proxy["spec"]["selector"]["matchLabels"])
        self.assertEqual(spread["matchLabelKeys"], ["pod-template-hash"])
        self.assertEqual(spread["whenUnsatisfiable"], "ScheduleAnyway")

    def test_probes(self):
        self.assertEqual(self.container["readinessProbe"]["httpGet"], {"path": "/health/readiness", "port": 8080})
        self.assertEqual(self.container["livenessProbe"]["httpGet"], {"path": "/replay/healthz", "port": 8080})

    def test_rollout_never_drops_below_the_running_count(self):
        self.assertEqual(self.proxy["spec"]["replicas"], 2)
        self.assertEqual(self.proxy["spec"]["strategy"]["rollingUpdate"], {"maxSurge": 1, "maxUnavailable": 0})

    def test_security_context(self):
        pod = self.proxy["spec"]["template"]["spec"]
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(pod["securityContext"]["runAsUser"], 65532)
        self.assertIs(pod["securityContext"]["runAsNonRoot"], True)
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        ctx = self.container["securityContext"]
        self.assertIs(ctx["readOnlyRootFilesystem"], True)
        self.assertIs(ctx["allowPrivilegeEscalation"], False)
        self.assertEqual(ctx["capabilities"], {"drop": ["ALL"]})

    def test_container_and_image(self):
        self.assertEqual(self.container["name"], _PROXY_CONTAINER)
        self.assertEqual(self.container["image"], f"{_IMAGE_REPOSITORY}:{_IMAGE_TAG}")

    def test_fingerprint_inputs_are_what_litellm_runs(self):
        self.assertRegex(self.env["REPLAY_MODEL_BEHAVIOUR_CHECKSUM"], r"^[0-9a-f]{64}$")
        litellm_container = next(c for c in self.litellm["spec"]["template"]["spec"]["containers"]
                                 if c["name"] == _LITELLM_CONTAINER)
        self.assertEqual(self.env["REPLAY_LITELLM_IMAGE"], litellm_container["image"])

    def test_job_tags(self):
        self.assertEqual({k: self.env[k] for k in ("REPLAY_JOB_KIND", "REPLAY_JOB_PROJECT", "REPLAY_JOB_BUILD",
                                                   "REPLAY_JOB_PR")},
                         {"REPLAY_JOB_KIND": "presubmit", "REPLAY_JOB_PROJECT": "pool",
                          "REPLAY_JOB_BUILD": "1790000000000000000", "REPLAY_JOB_PR": "4700"})

    def test_data_budget_sits_under_the_volume_and_the_volume_under_ephemeral_storage(self):
        mib = 1024 ** 2
        volume = self.proxy["spec"]["template"]["spec"]["volumes"][0]
        self.assertEqual(volume["emptyDir"]["sizeLimit"], "2Gi")
        budget = int(self.env["REPLAY_DATA_MAX_BYTES"])
        self.assertLess(budget, 2048 * mib)
        for side in ("requests", "limits"):
            self.assertEqual(self.container["resources"][side]["ephemeral-storage"], "2304Mi")
        self.assertEqual((self.env["REPLAY_STORE_DIR"], self.env["REPLAY_LOG_DIR"]), ("/data/store", "/data/logs"))
        self.assertEqual(self.container["volumeMounts"], [{"name": "data", "mountPath": "/data"}])

    def test_a_pod_being_replaced_outlives_one_segment_and_one_collector_pass(self):
        segment = int(self.env["REPLAY_LOG_SEGMENT_SECONDS"])
        command = self.container["lifecycle"]["preStop"]["exec"]["command"]
        pre_stop = int(re.search(r"time\.sleep\((\d+)\)", command[-1]).group(1))
        # The collector's pace as CI sets it, not a copy that could drift.
        collector_interval = int(re.search(r"REPLAY_SHADOW_COLLECTOR_INTERVAL_SECONDS=(\d+)", _CI_ENV.read_text()).group(1))
        self.assertGreater(pre_stop, segment + collector_interval)
        self.assertGreater(self.proxy["spec"]["template"]["spec"]["terminationGracePeriodSeconds"], pre_stop)
        # The hook tells the proxy it is draining, where the proxy looks for it.
        self.assertIn(os.path.join(self.env["REPLAY_LOG_DIR"], ".draining"), command[-1])

    def test_egress_policy_allows_dns_and_litellm_only(self):
        policy = _find(self.docs, "NetworkPolicy", "inference-replay-shadow-egress")["spec"]
        self.assertEqual(policy["policyTypes"], ["Egress"])
        self.assertTrue(_selects(policy["podSelector"]["matchLabels"], self.proxy_labels))
        self.assertFalse(_selects(policy["podSelector"]["matchLabels"], self.litellm_labels))
        dns, litellm = policy["egress"]
        self.assertEqual(sorted((p["port"], p["protocol"]) for p in dns["ports"]), [(53, "TCP"), (53, "UDP")])
        self.assertEqual(litellm["ports"], [{"port": 8080, "protocol": "TCP"}])
        [peer] = litellm["to"]
        self.assertEqual(peer, {"podSelector": {"matchLabels": {"app": "litellm", _ROLE: "litellm"}}})


# The two model routes hack/ci-deploy.sh renders: presubmits on Vertex AI, the
# nightlies on the Gemini Developer API, the rest of its LiteLLM flags alike.
_EVAL_MODEL = "gemini-4-argon"
_EVAL_LITELLM = (
    f"litellm.modelDefaultName={_EVAL_MODEL}",
    "litellm.vertex.serviceAccountAnnotations.iam\\.gke\\.io/gcp-service-account=litellm@pool.iam.gserviceaccount.com",
    "litellm.vertex.sessionAffinity.enabled=true",
)
_EVAL_LITELLM_TYPED = (
    "litellm.fallback.numRetries=2",
    "litellm.fallback.retryAfterSeconds=20",
    "litellm.vertex.sessionAffinity.slots=1",
)
_VERTEX = ("litellm.modelProvider=vertex_ai", *_EVAL_LITELLM)
_GEMINI_API = ("litellm.modelProvider=gemini", *_EVAL_LITELLM)
# Not a Gemini thinking model, so the gemini render carries no thinking key and
# only the route tells it from an openai_compatible one.
_PLAIN_MODEL = "litellm.modelDefaultName=gemma-3-27b"
_SERVER_A = "litellm.openaiCompatible.apiBase=https://llm-a.example.com/v1"
_SERVER_B = "litellm.openaiCompatible.apiBase=https://llm-b.example.com/v1"

_CALLBACKS_PROBE = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-callbacks
data:
  callbacks: |
    {{ include "kube-agents.litellmCallbacks" . }}
"""
# Every provider and flag combination that turns a callback on.
_CALLBACK_RENDERS = (
    (),
    ("litellm.otel=true",),
    ("litellm.modelProvider=anthropic",),
    ("litellm.modelProvider=vertex_ai", "litellm.modelDefaultName=claude-opus-5"),
    ("litellm.modelProvider=vertex_ai", f"litellm.modelDefaultName={_EVAL_MODEL}",
     "litellm.vertex.sessionAffinity.enabled=true"),
    ("litellm.redaction.enabled=true",),
    ("litellm.traceLogEnabled=true",),
    ("litellm.modelProvider=openai_compatible", _PLAIN_MODEL, _SERVER_A, "litellm.envFromSecret=ai-gateway-oauth"),
    ("litellm.modelProvider=vertex_ai", f"litellm.modelDefaultName={_EVAL_MODEL}", "litellm.otel=true",
     "litellm.vertex.sessionAffinity.enabled=true", "litellm.redaction.enabled=true", "litellm.traceLogEnabled=true"),
)


class ReplayKeyTest(unittest.TestCase):
    """REPLAY_MODEL_BEHAVIOUR_CHECKSUM covers what decides the answer, not the route to the model."""

    def render(self, sets, chart=_CHART, typed_sets=()):
        res = _render((*_ON, *sets), chart=chart, show_only="templates/litellm.yaml",
                      typed_sets=(*_EVAL_LITELLM_TYPED, *typed_sets))
        self.assertEqual(res.returncode, 0, res.stderr)
        docs = _docs(res.stdout)
        container = _find(docs, "Deployment", _PROXY)["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        rollout = _find(docs, "Deployment", "litellm")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        return env["REPLAY_MODEL_BEHAVIOUR_CHECKSUM"], rollout

    def test_vertex_and_gemini_api_routes_to_one_model_share_the_key(self):
        vertex_key, _ = self.render(_VERTEX)
        gemini_key, _ = self.render(_GEMINI_API)
        self.assertEqual(vertex_key, gemini_key)

    def test_the_pods_still_roll_when_the_route_changes(self):
        _, vertex_rollout = self.render(_VERTEX)
        _, gemini_rollout = self.render(_GEMINI_API)
        self.assertNotEqual(vertex_rollout, gemini_rollout)

    def test_openai_compatible_and_gemini_api_serving_one_model_name_keep_apart(self):
        gemini_key, _ = self.render(("litellm.modelProvider=gemini", _PLAIN_MODEL))
        server_key, _ = self.render(("litellm.modelProvider=openai_compatible", _PLAIN_MODEL, _SERVER_A))
        self.assertNotEqual(gemini_key, server_key)

    def test_two_openai_compatible_servers_keep_apart(self):
        server_a, _ = self.render(("litellm.modelProvider=openai_compatible", _PLAIN_MODEL, _SERVER_A))
        server_b, _ = self.render(("litellm.modelProvider=openai_compatible", _PLAIN_MODEL, _SERVER_B))
        self.assertNotEqual(server_a, server_b)

    def test_retry_settings_leave_the_key_alone(self):
        base, _ = self.render(_VERTEX)
        retried, _ = self.render(_VERTEX, typed_sets=("litellm.fallback.numRetries=1",
                                                      "litellm.fallback.retryAfterSeconds=30"))
        self.assertEqual(base, retried)

    def test_what_changes_the_answer_changes_the_key(self):
        base, _ = self.render(_VERTEX)
        for name, extra, typed in (
            ("model", ("litellm.modelDefaultName=gemini-3.5-flash",), ()),
            ("max tokens", (), ("litellm.maxTokens=4096",)),
            ("thinking", ("litellm.reasoningEffort=high",), ()),
            ("fallback alias", ("litellm.fallback.reasoningEffort=low",), ()),
            ("redaction", ("litellm.redaction.enabled=true",), ()),
        ):
            with self.subTest(name):
                changed, _ = self.render((*_VERTEX, *extra), typed_sets=typed)
                self.assertNotEqual(base, changed)

    def test_an_edit_to_a_request_rewriting_hook_changes_the_key(self):
        base, _ = self.render(_VERTEX)
        with tempfile.TemporaryDirectory() as tmp:
            chart = pathlib.Path(tmp) / "kube-agents"
            shutil.copytree(_CHART, chart)
            hook = chart / "files" / "litellm_thinking_replay_callback.py"
            hook.write_text(hook.read_text() + "\n# edited\n")
            changed, _ = self.render(_VERTEX, chart=chart)
        self.assertNotEqual(base, changed)

    def test_an_edit_to_the_replica_routing_hook_leaves_the_key_alone(self):
        base, _ = self.render(_VERTEX)
        with tempfile.TemporaryDirectory() as tmp:
            chart = pathlib.Path(tmp) / "kube-agents"
            shutil.copytree(_CHART, chart)
            hook = chart / "files" / "litellm_session_affinity_callback.py"
            hook.write_text(hook.read_text() + "\n# edited\n")
            changed, _ = self.render(_VERTEX, chart=chart)
        self.assertEqual(base, changed)


class CallbackClassesTest(unittest.TestCase):
    """Every rendered callback is classified as in the replay key or out of it."""

    @classmethod
    def setUpClass(cls):
        _helm()
        cls.tmp = tempfile.mkdtemp()
        cls.chart = pathlib.Path(cls.tmp) / "kube-agents"
        shutil.copytree(_CHART, cls.chart)
        (cls.chart / "templates" / "zz-probe.yaml").write_text(_CALLBACKS_PROBE)
        res = _render(chart=cls.chart, show_only="templates/zz-probe.yaml")
        if res.returncode != 0:
            raise AssertionError(res.stderr)
        classes = json.loads(_docs(res.stdout)[0]["data"]["callbacks"])
        cls.keyed = set(classes["replayKeyed"].values())
        cls.unkeyed = set(classes["replayUnkeyed"].values())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_callback_is_in_both_sets(self):
        self.assertEqual(self.keyed & self.unkeyed, set())

    def test_every_rendered_callback_is_classified(self):
        seen = set()
        for sets in _CALLBACK_RENDERS:
            with self.subTest(sets=sets):
                res = _render(sets, chart=self.chart, show_only="templates/litellm.yaml")
                self.assertEqual(res.returncode, 0, res.stderr)
                config = yaml.safe_load(_find(_docs(res.stdout), "ConfigMap", "litellm-config")["data"]["config.yaml"])
                callbacks = config["litellm_settings"]["callbacks"]
                self.assertEqual(set(callbacks) - self.keyed - self.unkeyed, set())
                seen.update(callbacks)
        # The renders above reach every classified callback, so none is a dead name.
        self.assertEqual(seen, self.keyed | self.unkeyed)


class PdbTest(unittest.TestCase):
    """The LiteLLM budget never counts the proxy; the proxy has its own."""

    def _pdbs(self, sets=()):
        res = _render(sets, show_only="templates/pdb.yaml")
        self.assertEqual(res.returncode, 0, res.stderr)
        return {d["metadata"]["name"]: d["spec"] for d in _docs(res.stdout) if d.get("kind") == "PodDisruptionBudget"}

    def test_off_the_litellm_budget_selects_app_litellm_and_there_is_no_proxy_budget(self):
        pdbs = self._pdbs()
        self.assertEqual(pdbs["litellm"]["selector"]["matchLabels"], {"app": "litellm"})
        self.assertNotIn(_PROXY, pdbs)

    def test_on_each_budget_selects_one_pod_kind(self):
        docs = _docs(_render(_ON, show_only="templates/litellm.yaml").stdout)
        proxy_labels = _find(docs, "Deployment", _PROXY)["spec"]["template"]["metadata"]["labels"]
        litellm_labels = _find(docs, "Deployment", "litellm")["spec"]["template"]["metadata"]["labels"]
        pdbs = self._pdbs(_ON)
        litellm, proxy = pdbs["litellm"], pdbs[_PROXY]
        self.assertTrue(_selects(litellm["selector"]["matchLabels"], litellm_labels))
        self.assertFalse(_selects(litellm["selector"]["matchLabels"], proxy_labels))
        self.assertTrue(_selects(proxy["selector"]["matchLabels"], proxy_labels))
        self.assertFalse(_selects(proxy["selector"]["matchLabels"], litellm_labels))
        self.assertEqual((litellm["maxUnavailable"], proxy["maxUnavailable"]), (1, 1))
        self.assertNotIn("minAvailable", proxy)


class SchemaTest(unittest.TestCase):
    def test_on_without_a_job_kind_is_refused(self):
        sets = [s for s in _ON if not s.startswith("litellm.replayShadow.job.kind")]
        res = _render(sets)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("/litellm/replayShadow/job/kind", res.stderr)

    def test_on_with_an_unknown_job_kind_is_refused(self):
        res = _render([*_ON, "litellm.replayShadow.job.kind=weekly"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("/litellm/replayShadow/job/kind", res.stderr)

    def test_on_without_an_image_is_refused(self):
        sets = [s for s in _ON if not s.startswith("litellm.replayShadow.image.")]
        res = _render(sets)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("/litellm/replayShadow/image/repository", res.stderr)
        self.assertIn("/litellm/replayShadow/image/tag", res.stderr)

    def test_off_needs_neither(self):
        res = _render(["litellm.replayShadow.enabled=false"])
        self.assertEqual(res.returncode, 0, res.stderr)


_RECORD = tuple(s for s in _ON if not s.startswith("litellm.replayShadow.job.kind")) + (
    "litellm.replayShadow.job.kind=nightly", "litellm.replayShadow.mode=record")
_REPLAY = tuple(s for s in _ON if not s.startswith("litellm.replayShadow.job.kind")) + (
    "litellm.replayShadow.job.kind=replay-lane", "litellm.replayShadow.mode=replay",
    "litellm.replayShadow.allowServe=true")
_MODE_VARS = ("REPLAY_MODE", "REPLAY_CACHE_DIR", "REPLAY_ALLOW_SERVE")


def _proxy_env(sets):
    res = _render(sets, show_only="templates/litellm.yaml")
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    container = _find(_docs(res.stdout), "Deployment", _PROXY)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


class ModesTest(unittest.TestCase):
    """record and replay (values.yaml, replayShadow.mode): replay takes two switches; shadow is unchanged."""

    def test_the_default_mode_renders_what_shadow_rendered_before_the_modes(self):
        env = _proxy_env(_ON)
        for name in _MODE_VARS:
            self.assertNotIn(name, env)
        self.assertEqual(env["REPLAY_DATA_MAX_BYTES"], "1610612736")
        self.assertEqual(_proxy_env([*_ON, "litellm.replayShadow.mode=shadow"]), env)

    def test_record_saves_under_the_cache_dir_and_never_serves(self):
        env = _proxy_env(_RECORD)
        self.assertEqual((env["REPLAY_MODE"], env["REPLAY_CACHE_DIR"]), ("record", "/data/replay-cache"))
        self.assertNotIn("REPLAY_ALLOW_SERVE", env)
        self.assertEqual(env["REPLAY_DATA_MAX_BYTES"], "1610612736", "the proxy counts the cache in the budget")

    def test_replay_with_both_switches_serves(self):
        env = _proxy_env(_REPLAY)
        self.assertEqual((env["REPLAY_MODE"], env["REPLAY_ALLOW_SERVE"], env["REPLAY_JOB_KIND"]),
                         ("replay", "yes", "replay-lane"))
        self.assertEqual(env["REPLAY_DATA_MAX_BYTES"], "1073741824", "the seeded cache sits outside the budget")

    def test_replay_without_allow_serve_or_allow_serve_without_replay_is_refused(self):
        without = [s for s in _REPLAY if "allowServe" not in s]
        alone = [*_ON, "litellm.replayShadow.allowServe=true"]
        for sets in (without, [*without, "litellm.replayShadow.allowServe=false"], alone,
                     [*_RECORD, "litellm.replayShadow.allowServe=true"]):
            with self.subTest(sets=sets[-2:]):
                res = _render(sets)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("/litellm/replayShadow", res.stderr)

    def test_each_mode_is_held_to_its_job_kind(self):
        for sets in ([*_RECORD, "litellm.replayShadow.job.kind=presubmit"],
                     [*_REPLAY, "litellm.replayShadow.job.kind=nightly"],
                     [*_REPLAY, "litellm.replayShadow.job.kind=presubmit"],
                     [*_ON, "litellm.replayShadow.job.kind=replay-lane"]):
            with self.subTest(sets=sets[-1]):
                res = _render(sets)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("/litellm/replayShadow", res.stderr)

    def test_the_cache_dir_is_one_directory_beside_logs_and_store(self):
        for cache_dir in ("/data/logs", "/data/store", "/etc/cache", "/data/a/b", "/data/../x"):
            with self.subTest(cache_dir=cache_dir):
                res = _render([*_RECORD, f"litellm.replayShadow.cacheDir={cache_dir}"])
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("/litellm/replayShadow/cacheDir", res.stderr)
        self.assertEqual(_proxy_env([*_RECORD, "litellm.replayShadow.cacheDir=/data/answers"])["REPLAY_CACHE_DIR"],
                         "/data/answers")

    def test_an_unknown_mode_is_refused(self):
        res = _render([*_ON, "litellm.replayShadow.mode=serve"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("/litellm/replayShadow/mode", res.stderr)


class ModeValuesTextTest(unittest.TestCase):
    """What values.yaml ships, read without helm: the default never serves."""

    def test_the_defaults_are_shadow_and_no_serving(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())["litellm"]["replayShadow"]
        self.assertEqual((values["mode"], values["allowServe"], values["cacheDir"]),
                         ("shadow", False, "/data/replay-cache"))
        cache_dir = re.search(r'readonly REPLAY_CACHE_POD_DIR="/data/\$\{REPLAY_CACHE_SUBDIR\}"', _CI_ENV.read_text())
        self.assertIsNotNone(cache_dir, "hack/ci-env.sh sets the same cache directory the chart defaults to")
        self.assertIn('readonly REPLAY_CACHE_SUBDIR="replay-cache"', _CI_ENV.read_text())


class QuotaTest(unittest.TestCase):
    """kube-agents.quotaRequirements counts the proxy's two pods, through a probe template."""

    @classmethod
    def setUpClass(cls):
        _helm()
        cls.tmp = tempfile.mkdtemp()
        cls.chart = pathlib.Path(cls.tmp) / "kube-agents"
        shutil.copytree(_CHART, cls.chart)
        (cls.chart / "templates" / "zz-probe.yaml").write_text(_PROBE_TEMPLATE)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def requirements(self, sets=()):
        res = _render(sets, chart=self.chart, show_only="templates/zz-probe.yaml")
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(_docs(res.stdout)[0]["data"]["requirements"])

    def test_two_proxy_pods_are_counted(self):
        off, on = self.requirements(), self.requirements(_ON)
        mib = 1024 ** 2
        self.assertEqual(on["pods"] - off["pods"], 2)
        self.assertEqual(on["requestsCpu"] - off["requestsCpu"], 2 * 100)
        self.assertEqual(on["limitsCpu"] - off["limitsCpu"], 2 * 1000)
        self.assertEqual(on["requestsMemory"] - off["requestsMemory"], 2 * 512 * mib)
        self.assertEqual(on["limitsMemory"] - off["limitsMemory"], 2 * 2048 * mib)
        self.assertEqual(on["requestsEphemeral"] - off["requestsEphemeral"], 2 * 2304 * mib)

    def test_not_counted_without_litellm(self):
        self.assertEqual(self.requirements(["litellm.enabled=false"])["pods"],
                         self.requirements(["litellm.enabled=false", *_ON])["pods"])


if __name__ == "__main__":
    unittest.main()
