"""The operator's `secrets` grant, and the ceiling on it.

Five callers need a Secret verb on the manager role, and the grant is split in
two documents of `role.yaml` by what they do:

- `checkShellSandboxKeys` needs `get`. It asks whether the sandbox's
  authorized-keys Secret exists so the CR can say so, because that Secret is the
  one mount a pod cannot start without, and a missing one is otherwise fifteen
  minutes of `ContainerCreating` with the cause on an object nobody is looking
  at. It reads no value out of the Secret and creates that Secret nowhere.
- `stampSecretEnvHash` needs `get`, and unlike the caller above it **reads the
  values**: it digests the Secret keys the agent pod consumes as environment onto
  the pod template, so that rotating one rolls the pod
  (`k8s-operator/internal/controller/platformagent_secret_hash.go`). Which
  Secrets it reads is decided by the rendered pod spec, so a CR- or
  plugin-supplied `SecretKeyRef` is covered. Every read is by name and uncached.
- `buildMinimalPlatformRole` grants `get`/`list`/`watch` on `secrets` to
  `kubeagents:minimal:<ns>:<name>` so `<agent>-kube-proxy` can read `Secret`
  objects and serve redacted metadata (`KUBE_PROXY_REDACT_SECRETS=true`).
  Kubernetes RBAC escalation prevention requires the operator to hold
  `get`/`list`/`watch` on `secrets` cluster-wide before it can grant those
  verbs on a managed `ClusterRole`.
- `ensureKubeProxyTLSSecret` needs `create` and `update` to manage the
  `<agent>-kube-proxy-tls` Secret on every install.
- The `mode: next` A2A render needs `create`/`update`/`patch`/`delete`. The
  operator generates the NATS credential Secret once and repairs a missing key,
  renders the config Secret, and deletes the config Secret on the way back to
  `today`; all of them are owner-referenced to the `PlatformAgent`.

The three read verbs are the ClusterRole document: they are held cluster-wide
because the agent's audit ClusterRole grants them and the API server refuses a
grant the grantor does not hold. The four write verbs are the namespaced `Role`
document: every write names a Secret in the namespace of the PlatformAgent
being reconciled, so the `+kubebuilder:rbac` marker carries `namespace=`,
controller-gen renders it as the second document in `role.yaml`, and the
ClusterRole document carries no Secret write verb. That shape is what this file
pins. A write verb that reappears in the ClusterRole is write of every Secret on
the cluster, which is the cluster-admin-equivalent grant the namespacing exists
to remove.

Even though the ClusterRole holds `list` and `watch` for escalation prevention,
the operator itself must never list or watch `Secret` objects: every controller
Secret read goes by name through `a2aReader()`, and
`test_the_operator_never_watches_or_lists_secrets_in_controller_code` below
forbids `&corev1.SecretList`, `Owns(&corev1.Secret`, and
`Watches(&corev1.Secret` in controller Go files.

Trivy's KSV-0041 flags any Secret verb on a Role or ClusterRole as CRITICAL, and
its ignore file has no way to scope an exemption to one rule -- `paths` globs are
the whole vocabulary -- so exempting this grant exempts `k8s-operator/config/rbac/`
and the rendered golden fixtures in
`k8s-operator/internal/testing/testdata/platform/expected/`. That is the check
this file replaces: it pins the verb set of each document and the two
`+kubebuilder:rbac` markers, forbids controller `Secret` list/watch calls, and
fails if the Trivy exemption widens past those two directories.

`charts/kube-agents/templates/operator-rbac.yaml` is not read here: both of its
generated blocks are spliced from `role.yaml` by `hack/sync-chart-manifests.sh`,
and `make chart-check` is what holds the two together. The conformance suite's
C5 test reads the chart as well.

Run:
  python3 -m unittest discover -s tests -p 'test_operator_secrets_grant.py' -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_PATH = REPO_ROOT / "k8s-operator" / "config" / "rbac" / "role.yaml"
CHART_PATH = REPO_ROOT / "charts" / "kube-agents"
CR_TEMPLATE_PATH = CHART_PATH / "templates" / "platform-agent-cr.yaml"
REQUIRED_HELM_SETS = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
DISALLOW_SECRETS_GUARD = (
    'include "kube-agents.crdFieldGuard" (dict "value" "platformAgent.security.disallowSecretsRead" '
    '"path" (list "security") "field" "disallowSecretsRead")'
)
CORE_API_GROUP = ""
SECRETS_RESOURCE = "secrets"
WILDCARD = "*"
CLUSTER_ROLE_KIND = "ClusterRole"
ROLE_KIND = "Role"
# The ClusterRole half: what escalation prevention needs the operator to hold.
CLUSTER_SECRET_VERBS = {"get", "list", "watch"}
# The Role half: what the operator writes, in the install namespace only.
NAMESPACED_SECRET_VERBS = {"create", "update", "patch", "delete"}
# One marker per document; both name `secrets` alone.
SECRET_MARKER_COUNT = 2
CONTROLLER_DIR = REPO_ROOT / "k8s-operator" / "internal" / "controller"
IGNORE_PATH = REPO_ROOT / ".trivyignore.yaml"
SECRETS_MISCONFIG_ID = "KSV-0041"
# The namespace both install paths place the PlatformAgent in, and the only
# one where the operator's namespaced Role is bound.
INSTALL_NAMESPACE = "kubeagents-system"
KUSTOMIZE_DEFAULT_DIR = REPO_ROOT / "k8s-operator" / "config" / "default"
PLATFORM_AGENT_KIND = "PlatformAgent"
OPERATOR_DEPLOYMENT_SUFFIX = "controller-manager"
# Tracked paths a user copies a PlatformAgent manifest from. Test fixtures and
# the chart's templated CR are not among them.
SHIPPED_MANIFEST_ROOTS = (
    "docs", "INSTALL.md", "README.md", "examples",
    "k8s-operator/examples", "k8s-operator/config/samples",
)
MANIFEST_SUFFIXES = (".yaml", ".yml")
MARKDOWN_SUFFIXES = (".md", ".mdx")
YAML_FENCE = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.S)


def _documents() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(ROLE_PATH.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def _of_kind(kind: str) -> list[dict]:
    return [doc for doc in _documents() if doc.get("kind") == kind]


def _cluster_role(test: unittest.TestCase) -> dict:
    cluster_roles = _of_kind(CLUSTER_ROLE_KIND)
    test.assertEqual(len(cluster_roles), 1, f"expected one ClusterRole in {ROLE_PATH}, found {len(cluster_roles)}")
    return cluster_roles[0]


def _namespaced_role(test: unittest.TestCase) -> dict:
    # Asserted, not indexed: against a role.yaml with no Role document the
    # failure should read as the missing grant, not as an IndexError.
    roles = _of_kind(ROLE_KIND)
    test.assertEqual(len(roles), 1, f"expected one namespaced Role in {ROLE_PATH}, found {len(roles)}")
    return roles[0]


def _secret_rules(doc: dict) -> list[dict]:
    return [
        rule
        for rule in doc.get("rules", [])
        if SECRETS_RESOURCE in (rule.get("resources") or [])
        or WILDCARD in (rule.get("resources") or [])
    ]


def _tracked_files(*roots: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--", *roots], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [REPO_ROOT / line for line in out]


def _yaml_documents_in(text: str) -> list[dict]:
    try:
        return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]
    except yaml.YAMLError:
        # A fenced block with a placeholder or a fragment is not a manifest.
        return []


def _shipped_platform_agents() -> list[tuple[str, dict]]:
    found = []
    for path in _tracked_files(*SHIPPED_MANIFEST_ROOTS):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix in MANIFEST_SUFFIXES:
            blocks = [text]
        elif path.suffix in MARKDOWN_SUFFIXES:
            blocks = YAML_FENCE.findall(text)
        else:
            continue
        for block in blocks:
            for doc in _yaml_documents_in(block):
                if doc.get("kind") == PLATFORM_AGENT_KIND:
                    found.append((str(path.relative_to(REPO_ROOT)), doc))
    return found


def _render_helm() -> list[dict]:
    proc = subprocess.run(
        ["helm", "template", "kube-agents", str(CHART_PATH), "-n", INSTALL_NAMESPACE, *REQUIRED_HELM_SETS],
        capture_output=True, text=True, check=True,
    )
    return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


def _render_kustomize() -> list[dict]:
    proc = subprocess.run(
        ["kubectl", "kustomize", str(KUSTOMIZE_DEFAULT_DIR)], capture_output=True, text=True, check=True,
    )
    return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


class OperatorSecretsGrantTest(unittest.TestCase):
    def test_no_shipped_manifest_places_a_platformagent_outside_the_install_namespace(self):
        # The operator's Secret write verbs are bound in the install namespace
        # only, so a PlatformAgent a user copies from the docs or samples has
        # to be there: anywhere else its first kube-proxy TLS Secret write is
        # forbidden and the agent sits Degraded/RBACIncomplete. This reads
        # every tracked YAML file and every fenced YAML block under the
        # shipped roots, so a guide that names the namespace with a typo
        # (`kube-agents-system`) fails here rather than on a user's cluster.
        agents = _shipped_platform_agents()
        self.assertGreater(len(agents), 0, "no shipped PlatformAgent manifest found; the scan is broken")
        for source, doc in agents:
            namespace = (doc.get("metadata") or {}).get("namespace")
            with self.subTest(source=source, name=(doc.get("metadata") or {}).get("name")):
                self.assertIn(
                    namespace,
                    (None, INSTALL_NAMESPACE),
                    f"{source} places a PlatformAgent in {namespace!r}; the operator writes Secrets "
                    f"in {INSTALL_NAMESPACE} only",
                )

    @unittest.skipUnless(shutil.which("helm") and shutil.which("kubectl"), "helm and kubectl needed")
    def test_the_namespaced_role_is_bound_to_the_operator_on_both_delivery_paths(self):
        # The Role's verbs are pinned above; this pins that something binds
        # it. On each render: exactly one RoleBinding in the Role's namespace
        # names it, and that binding's subject is the ServiceAccount the
        # operator Deployment runs as, in the same namespace. A typo in the
        # binding would otherwise strip the operator's Secret writes on every
        # install with CI green, and only the runtime self-check would say so.
        for path, documents in (("helm", _render_helm()), ("kustomize", _render_kustomize())):
            with self.subTest(path=path):
                roles = [
                    d for d in documents
                    if d.get("kind") == ROLE_KIND
                    and any(SECRETS_RESOURCE in (r.get("resources") or []) for r in d.get("rules") or [])
                ]
                self.assertEqual(1, len(roles), f"{path}: expected one Role naming secrets, found {len(roles)}")
                role = roles[0]
                role_name = role["metadata"]["name"]
                role_namespace = role["metadata"].get("namespace")
                self.assertEqual(INSTALL_NAMESPACE, role_namespace)
                deployments = [
                    d for d in documents
                    if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith(OPERATOR_DEPLOYMENT_SUFFIX)
                ]
                self.assertEqual(1, len(deployments), f"{path}: expected one operator Deployment")
                operator_sa = deployments[0]["spec"]["template"]["spec"].get("serviceAccountName")
                self.assertTrue(operator_sa, f"{path}: the operator Deployment names no serviceAccountName")
                bindings = [
                    d for d in documents
                    if d.get("kind") == "RoleBinding"
                    and d["metadata"].get("namespace") == role_namespace
                    and d.get("roleRef", {}).get("kind") == ROLE_KIND
                    and d.get("roleRef", {}).get("name") == role_name
                ]
                self.assertEqual(
                    1, len(bindings),
                    f"{path}: expected one RoleBinding in {role_namespace} to Role {role_name}, found {len(bindings)}",
                )
                subjects = bindings[0].get("subjects") or []
                self.assertIn(
                    {"kind": "ServiceAccount", "name": operator_sa, "namespace": role_namespace},
                    subjects,
                    f"{path}: RoleBinding {bindings[0]['metadata']['name']} does not bind the operator's "
                    f"ServiceAccount {operator_sa} in {role_namespace}; subjects={subjects}",
                )

    def test_the_cluster_role_grants_no_secret_write_verb(self):
        # The invariant the namespacing exists for: a write verb on `secrets`
        # in the ClusterRole, or a wildcard that would reach it, is write of
        # every Secret on the cluster whatever namespace the operator works in.
        rules = _secret_rules(_cluster_role(self))
        self.assertEqual(
            len(rules),
            1,
            f"expected one ClusterRole rule naming {SECRETS_RESOURCE}, found {len(rules)}: {rules}",
        )
        self.assertNotIn(WILDCARD, rules[0].get("resources") or [])
        self.assertEqual(
            set(rules[0].get("verbs") or []),
            CLUSTER_SECRET_VERBS,
            "the operator's ClusterRole carries a Secret verb beyond the reads escalation "
            "prevention needs; the write verbs belong in the namespaced Role (marker with namespace=)",
        )
        self.assertEqual(rules[0].get("apiGroups"), [CORE_API_GROUP])

    def test_the_write_verbs_are_granted_in_exactly_one_namespaced_rule(self):
        role = _namespaced_role(self)
        self.assertTrue(
            role.get("metadata", {}).get("namespace"),
            "the Role document carries no namespace; controller-gen writes one from the marker",
        )
        rules = _secret_rules(role)
        self.assertEqual(
            len(rules),
            1,
            f"expected one Role rule naming {SECRETS_RESOURCE}, found {len(rules)}: {rules}",
        )
        self.assertEqual(set(rules[0].get("verbs") or []), NAMESPACED_SECRET_VERBS)
        self.assertEqual(rules[0].get("apiGroups"), [CORE_API_GROUP])

    def test_the_namespaced_rule_names_no_other_resource(self):
        # A verb set is only a ceiling for the resources it is attached to.
        # Folding `secrets` into a rule that also names, say, `configmaps` would
        # keep the verbs as they are and still widen what a later verb addition
        # reaches.
        rule = _secret_rules(_namespaced_role(self))[0]
        self.assertEqual(rule.get("resources"), [SECRETS_RESOURCE])

    def test_the_operator_never_watches_or_lists_secrets_in_controller_code(self):
        # `list` and `watch` are held on the ClusterRole only so the operator
        # passes Kubernetes RBAC escalation prevention when reconciling
        # `buildMinimalPlatformRole` (which grants `get`/`list`/`watch` on
        # `secrets` to `<agent>-kube-proxy`, where Secret payloads are stripped).
        # The operator's own Go code must never add a Secret informer watch or
        # call `SecretList`.
        for go_file in sorted(CONTROLLER_DIR.glob("*.go")):
            if go_file.name.endswith("_test.go"):
                continue
            source = go_file.read_text(encoding="utf-8")
            self.assertNotIn(
                "SecretList",
                source,
                f"{go_file.name} lists Secrets; operator Secret reads must stay by-name via APIReader/a2aReader",
            )
            self.assertNotIn(
                "Owns(&corev1.Secret",
                source,
                f"{go_file.name} watches Secrets via Owns(&corev1.Secret...); operator must not start a Secret informer",
            )
            self.assertNotIn(
                "Watches(&corev1.Secret",
                source,
                f"{go_file.name} watches Secrets via Watches(&corev1.Secret...); operator must not start a Secret informer",
            )
            self.assertNotIn(
                "WatchesMetadata(&corev1.Secret",
                source,
                f"{go_file.name} watches Secrets via WatchesMetadata(&corev1.Secret...); operator must not start a Secret informer",
            )

    def test_the_secrets_markers_name_no_other_resource(self):
        # controller-gen merges rules with identical apiGroups and verbs in
        # role.yaml (the read rule lands beside nodes and namespaces), so check
        # the +kubebuilder:rbac markers in platformagent_controller.go directly:
        # `secrets` stays on its own two marker lines, one cluster-scoped for
        # the reads and one with `namespace=` for the writes, and is never
        # folded into another resource's marker.
        controller_src = (CONTROLLER_DIR / "platformagent_controller.go").read_text(encoding="utf-8")
        secret_markers = [
            line.strip()
            for line in controller_src.splitlines()
            if "+kubebuilder:rbac:" in line and SECRETS_RESOURCE in line
        ]
        self.assertEqual(len(secret_markers), SECRET_MARKER_COUNT, secret_markers)
        for marker in secret_markers:
            self.assertIn(f"resources={SECRETS_RESOURCE},", marker, f"secrets marker shares resources: {marker}")
        namespaced = [marker for marker in secret_markers if "namespace=" in marker]
        self.assertEqual(len(namespaced), 1, f"expected one namespaced secrets marker, found {namespaced}")
        for verb in NAMESPACED_SECRET_VERBS:
            self.assertIn(verb, namespaced[0])
        cluster_wide = [marker for marker in secret_markers if "namespace=" not in marker]
        for verb in NAMESPACED_SECRET_VERBS:
            self.assertNotIn(verb, cluster_wide[0], f"the cluster-wide secrets marker carries a write verb: {cluster_wide[0]}")

    def test_the_trivy_exemption_stays_scoped_to_the_expected_directories(self):
        ignores = yaml.safe_load(IGNORE_PATH.read_text(encoding="utf-8"))
        entries = [
            entry
            for entry in ignores.get("misconfigurations", [])
            if entry.get("id") == SECRETS_MISCONFIG_ID
        ]
        self.assertEqual(
            len(entries),
            1,
            f"expected one {SECRETS_MISCONFIG_ID} exemption, found {len(entries)}",
        )
        self.assertEqual(
            entries[0].get("paths"),
            [
                "k8s-operator/config/rbac/**",
                "k8s-operator/internal/testing/testdata/platform/expected/**",
            ],
        )

    def test_chart_guards_and_defaults_disallow_secrets_read_to_null(self):
        template = CR_TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn(DISALLOW_SECRETS_GUARD, template)
        values = yaml.safe_load((CHART_PATH / "values.yaml").read_text(encoding="utf-8"))
        self.assertIsNone(values["platformAgent"]["security"]["disallowSecretsRead"])

    @unittest.skipUnless(shutil.which("helm"), "helm not installed")
    def test_chart_renders_disallow_secrets_read_when_set(self):
        def render_security(*extra: str) -> dict:
            proc = subprocess.run(
                [
                    "helm", "template", "test-release", str(CHART_PATH),
                    *REQUIRED_HELM_SETS, *extra,
                    "--show-only", "templates/platform-agent-cr.yaml",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for doc in yaml.safe_load_all(proc.stdout):
                if isinstance(doc, dict) and doc.get("kind") == "PlatformAgent":
                    return doc["spec"]["security"]
            self.fail("no PlatformAgent in rendered output")

        self.assertNotIn("disallowSecretsRead", render_security())
        self.assertIs(
            render_security("--set", "platformAgent.security.disallowSecretsRead=true").get("disallowSecretsRead"),
            True,
        )
        self.assertIs(
            render_security("--set", "platformAgent.security.disallowSecretsRead=false").get("disallowSecretsRead"),
            False,
        )

    def test_rendered_kube_proxy_enforces_secret_redaction_when_minimal_role_grants_secrets(self):
        # Every golden fixture where kubeagents:minimal grants `secrets` must
        # also render `<agent>-kube-proxy` with `KUBE_PROXY_REDACT_SECRETS=true`
        # and keep the agent pod's `automountServiceAccountToken` disabled while
        # routing `KUBE_PROXY_URL` to `<agent>-kube-proxy`.
        golden_dir = REPO_ROOT / "k8s-operator" / "internal" / "testing" / "testdata" / "platform" / "expected"
        checked = 0
        for fixture in sorted(golden_dir.glob("*.yaml")):
            docs = [d for d in yaml.safe_load_all(fixture.read_text(encoding="utf-8")) if isinstance(d, dict)]
            minimal_roles = [
                d for d in docs
                if d.get("kind") == "ClusterRole"
                and str(d.get("metadata", {}).get("name", "")).startswith("kubeagents:minimal:")
            ]
            if not minimal_roles:
                continue
            grants_secrets = any(
                SECRETS_RESOURCE in (rule.get("resources") or [])
                for rule in (minimal_roles[0].get("rules") or [])
            )
            if not grants_secrets:
                continue
            checked += 1
            deployments = {
                d["metadata"]["name"]: d
                for d in docs
                if d.get("kind") == "Deployment"
            }
            kube_proxy_deploys = [
                d for name, d in deployments.items() if name.endswith("-kube-proxy")
            ]
            self.assertEqual(
                len(kube_proxy_deploys),
                1,
                f"{fixture.name}: expected one *-kube-proxy Deployment alongside kubeagents:minimal secrets grant",
            )
            kp_containers = kube_proxy_deploys[0]["spec"]["template"]["spec"]["containers"]
            kp_env = {
                item["name"]: item.get("value")
                for item in kp_containers[0].get("env", [])
            }
            self.assertEqual(
                kp_env.get("KUBE_PROXY_REDACT_SECRETS"),
                "true",
                f"{fixture.name}: kube-proxy Deployment must set KUBE_PROXY_REDACT_SECRETS=true when secrets are granted",
            )
        self.assertGreater(checked, 0, "expected at least one golden fixture granting secrets on kubeagents:minimal")


if __name__ == "__main__":
    unittest.main()
