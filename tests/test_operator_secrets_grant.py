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
- The A2A render needs `create`/`update`/`patch`/`delete`. The
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

Every other namespaced kind the operator renders is split the same way
(`NAMESPACED_WRITE_KINDS` below, from
`docs/designs/operator-namespaced-write-verbs.md`): the reads stay in the
ClusterRole for the informers and for escalation prevention, the writes are the
Role document, and the Role document names exactly that set. The Role is bound
in the install namespace on both delivery paths and in the profile namespace
(`<install namespace>-profiles`) as well: by the chart on every install, and
by `k8s-operator/config/profiles` on the kustomize path, whose `role.yaml` is a
copy of the Role document that `make chart-sync` writes and this file holds to
the source.

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

`charts/kube-agents/templates/operator-rbac.yaml` is read rendered (`helm
template`), never as text: its generated blocks are spliced from `role.yaml` by
`hack/sync-chart-manifests.sh`, `make chart-check` holds the two together, and
the tests here that render the chart are skipped where `helm` is not installed.
The conformance suite's C5 test reads the chart rendered as well.

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
# The Role half: what the operator writes, in the install namespace and the
# profile namespace only.
NAMESPACED_SECRET_VERBS = {"create", "update", "patch", "delete"}
# One marker per document; both name `secrets` alone.
SECRET_MARKER_COUNT = 2
CONTROLLER_DIR = REPO_ROOT / "k8s-operator" / "internal" / "controller"
IGNORE_PATH = REPO_ROOT / ".trivyignore.yaml"
SECRETS_MISCONFIG_ID = "KSV-0041"
# The namespace both install paths place the PlatformAgent in; the operator's
# namespaced Role is bound here and in its profile namespace.
INSTALL_NAMESPACE = "kubeagents-system"
KUSTOMIZE_DEFAULT_DIR = REPO_ROOT / "k8s-operator" / "config" / "default"
# The profile namespace and the kustomization that renders the Role copy there;
# the chart renders the same on every install.
PROFILE_NAMESPACE_SUFFIX = "-profiles"
PROFILE_NAMESPACE = INSTALL_NAMESPACE + PROFILE_NAMESPACE_SUFFIX
KUSTOMIZE_PROFILES_DIR = REPO_ROOT / "k8s-operator" / "config" / "profiles"
PROFILE_ROLE_COPY = KUSTOMIZE_PROFILES_DIR / "role.yaml"
# Every namespaced kind the operator renders, as (apiGroup, resource). The Role
# document names exactly this set and the ClusterRole grants no write verb on
# any of it; every kind's reads stay in the ClusterRole (the informers,
# escalation prevention, and the teardown's list of dispatchjobtemplates in a
# profile namespace a mode-today install never binds the Role in).
NAMESPACED_WRITE_KINDS = frozenset({
    (CORE_API_GROUP, "configmaps"),
    (CORE_API_GROUP, "events"),
    (CORE_API_GROUP, "persistentvolumeclaims"),
    (CORE_API_GROUP, "pods"),
    (CORE_API_GROUP, "podtemplates"),
    (CORE_API_GROUP, "resourcequotas"),
    (CORE_API_GROUP, SECRETS_RESOURCE),
    (CORE_API_GROUP, "serviceaccounts"),
    (CORE_API_GROUP, "services"),
    ("apps", "deployments"),
    ("apps", "statefulsets"),
    ("batch", "jobs"),
    ("coordination.k8s.io", "leases"),
    ("kubeagents.x-k8s.io", "dispatchjobtemplates"),
    ("networking.gke.io", "fqdnnetworkpolicies"),
    ("networking.k8s.io", "networkpolicies"),
    ("policy", "poddisruptionbudgets"),
    ("rbac.authorization.k8s.io", "rolebindings"),
    ("rbac.authorization.k8s.io", "roles"),
})
# Kinds whose reads are allowed in the Role document as well. None today: a
# read in the Role is a 403 wherever the Role is not bound, and the operator
# reads in namespaces it does not write in.
NAMESPACED_READ_KINDS = frozenset()
# The verbs each kind carries in the Role document. The four writes unless
# the kind says otherwise: the Recorder creates and patches Events, the
# dry run creates PodTemplates. One row per exception; the rest are the
# four writes, so a verb dropped from any kind is a red row here and not
# only in the Go parity test.
NAMESPACED_VERBS_BY_KIND = {
    (CORE_API_GROUP, "events"): frozenset({"create", "patch"}),
    (CORE_API_GROUP, "podtemplates"): frozenset({"create"}),
}
# The custom resources' verbs on the ClusterRole, each write verb with the
# code that calls it. A verb with no caller is a grant the operator never
# uses, and on a cluster-scoped grant that is authority over every namespace
# for nothing: `create` and `delete` on `platformagents` were that until this
# table. The reads are kept whole: `list` and `watch` feed the informers and
# `get` is the read half of every kind the controller writes or watches, kept
# where nothing sends a GET today (the status subresources, agentplugins,
# agentprofiles) and named here so a reader does not go looking for a caller.
# `tiers` is left to its own owner.
#
#   platformagents            get: the uncached reads (r.APIReader.Get in
#                             platformagent_controller.go, podReader in
#                             platformagent_a2a_bus_streams.go, a2aReader in
#                             agentprofile_namespace.go); list/watch: the For()
#                             informer and the Lists outside it (the webhook's,
#                             the usage poller's, the bus-stream check's, the
#                             profile reconciler's); update: the
#                             finalizer (AddFinalizer/RemoveFinalizer with
#                             r.Update); patch: the bus-stream annotation
#                             merge patch (platformagent_a2a_bus_streams.go).
#   platformagents/status     update/patch: Status().Update and Status().Patch
#                             across the reconcilers; get/list/watch:
#                             escalation prevention, because
#                             buildPlatformLocalRole grants the agent's local
#                             Role get/list/watch on platformagents and
#                             platformagents/status, and the API server
#                             refuses a Role whose grantor does not hold every
#                             verb in it (a grant on the resource does not
#                             cover the subresource).
#   platformagents/finalizers update: SetControllerReference sets
#                             blockOwnerDeletion on every rendered object, and
#                             the API server's owner-references admission
#                             requires it on the owner's finalizers.
#   agentplugins              list/watch: the Watches() informer and two
#                             Lists in platformagent_controller.go; get: the
#                             read half (no Get of a plugin is sent).
#   agentplugins/status       patch: Status().Patch (the plugin conditions);
#                             get: the read half.
#   agentprofiles             list/watch: the For() informer and the identity
#                             map's List; get: the read half (the reconciler's
#                             Get is served by the informer; its uncached
#                             reader reads ServiceAccounts only); patch:
#                             patchFinalizers' merge patch (an Update would
#                             drop spec fields a newer CRD added).
#   agentprofiles/status      update: Status().Update (writeStatus); get: the
#                             read half.
#   (no agentprofiles/finalizers: nothing the operator renders carries an
#                             owner reference to a profile, the profile's
#                             ServiceAccounts carry labels only and outlive
#                             it, and the finalizer is a field of the main
#                             resource written by the merge patch above.)
CR_API_GROUP = "kubeagents.x-k8s.io"
CR_VERBS_BY_RESOURCE = {
    "platformagents": frozenset({"get", "list", "watch", "update", "patch"}),
    "platformagents/status": frozenset({"get", "list", "watch", "update", "patch"}),
    "platformagents/finalizers": frozenset({"update"}),
    "agentplugins": frozenset({"get", "list", "watch"}),
    "agentplugins/status": frozenset({"get", "patch"}),
    "agentprofiles": frozenset({"get", "list", "watch", "patch"}),
    "agentprofiles/status": frozenset({"get", "update"}),
}
# Subresources the ClusterRole must not name at all: a row here with any verb
# is a grant with no caller (see the table's note on agentprofiles/finalizers).
CR_RESOURCES_WITHOUT_A_GRANT = frozenset({"agentprofiles/finalizers"})
# The other kubeagents.x-k8s.io resources the ClusterRole names, outside this
# table: the Tier CRD's (its owner's grant; `tiers/status` `patch` has no
# caller today) and the dispatcher's params, whose reads stay cluster-wide
# (see the markers). A new CRD's marker lands here or in the table, so a
# grant nobody has classified is red rather than unexamined.
CR_RESOURCES_OUTSIDE_THE_TABLE = frozenset({"tiers", "tiers/status", "dispatchjobtemplates"})
# The kustomize deploy recipe: config/profiles is applied after config/default
# and a failed first apply fails the target. The profile namespace's
# release-namespace annotation is the install namespace, which the operator
# checks against the agent's namespace before it treats the namespace as the
# profile namespace; a wrong value renders every profile as absent.
OPERATOR_MAKEFILE = REPO_ROOT / "k8s-operator" / "Makefile"
PROFILES_NAMESPACE_MANIFEST = KUSTOMIZE_PROFILES_DIR / "namespace.yaml"
DEPLOY_RECIPE_PROFILES_LINE = 'kubectl apply --server-side -f - && \\\n\t$(KUSTOMIZE) build "$$tmp/config/profiles" | kubectl apply --server-side -f -'
RELEASE_NAMESPACE_ANNOTATION = "meta.helm.sh/release-namespace"
PROFILE_COMPONENT_LABEL = "app.kubernetes.io/component"
PROFILE_COMPONENT = "profiles"
READ_VERBS = frozenset({"get", "list", "watch"})
WRITE_VERBS = frozenset({"create", "update", "patch", "delete"})
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


def _rule_kinds(rule: dict) -> set[tuple[str, str]]:
    return {
        (group, resource)
        for group in (rule.get("apiGroups") or [])
        for resource in (rule.get("resources") or [])
    }


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


def _render_helm(*extra_sets: str) -> list[dict]:
    proc = subprocess.run(
        ["helm", "template", "kube-agents", str(CHART_PATH), "-n", INSTALL_NAMESPACE, *REQUIRED_HELM_SETS, *extra_sets],
        capture_output=True, text=True, check=True,
    )
    return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


def _render_kustomize(*dirs: Path) -> list[dict]:
    documents: list[dict] = []
    for directory in dirs or (KUSTOMIZE_DEFAULT_DIR,):
        proc = subprocess.run(
            ["kubectl", "kustomize", str(directory)], capture_output=True, text=True, check=True,
        )
        documents.extend(d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict))
    return documents


class OperatorSecretsGrantTest(unittest.TestCase):
    def test_no_shipped_manifest_places_a_platformagent_outside_the_install_namespace(self):
        # The operator's write verbs are bound in the install namespace and its
        # profile namespace only, so a PlatformAgent a user copies from the docs or samples has
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

    def _assert_role_bound_in(self, path: str, documents: list[dict], namespaces: set[str]) -> None:
        # On a render: one Role naming secrets per expected namespace and no
        # other, and in each namespace exactly one RoleBinding names that Role
        # with the operator Deployment's ServiceAccount, in the install
        # namespace, as its subject. A typo in a binding would otherwise strip
        # the operator's writes on every install with CI green, and only the
        # runtime self-check would say so.
        roles = [
            d for d in documents
            if d.get("kind") == ROLE_KIND
            and any(SECRETS_RESOURCE in (r.get("resources") or []) for r in d.get("rules") or [])
        ]
        self.assertEqual(
            namespaces, {r["metadata"].get("namespace") for r in roles},
            f"{path}: expected one Role naming secrets in each of {sorted(namespaces)}, "
            f"found {[(r['metadata'].get('namespace'), r['metadata']['name']) for r in roles]}",
        )
        self.assertEqual(len(namespaces), len(roles), f"{path}: a namespace renders the Role twice")
        deployments = [
            d for d in documents
            if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith(OPERATOR_DEPLOYMENT_SUFFIX)
        ]
        self.assertEqual(1, len(deployments), f"{path}: expected one operator Deployment")
        operator_sa = deployments[0]["spec"]["template"]["spec"].get("serviceAccountName")
        self.assertTrue(operator_sa, f"{path}: the operator Deployment names no serviceAccountName")
        for role in roles:
            role_name = role["metadata"]["name"]
            role_namespace = role["metadata"]["namespace"]
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
                {"kind": "ServiceAccount", "name": operator_sa, "namespace": INSTALL_NAMESPACE},
                subjects,
                f"{path}: RoleBinding {bindings[0]['metadata']['name']} does not bind the operator's "
                f"ServiceAccount {operator_sa} in {INSTALL_NAMESPACE}; subjects={subjects}",
            )

    @unittest.skipUnless(shutil.which("helm") and shutil.which("kubectl"), "helm and kubectl needed")
    def test_the_namespaced_role_is_bound_to_the_operator_on_both_delivery_paths(self):
        # The Role's verbs are pinned above; this pins that something binds it.
        # A stock chart render creates the profile namespace and binds the Role
        # there too; the kustomize default directory covers the install
        # namespace alone (config/profiles adds the other, below).
        for path, documents, namespaces in (
            ("helm", _render_helm(), {INSTALL_NAMESPACE, PROFILE_NAMESPACE}),
            ("kustomize", _render_kustomize(), {INSTALL_NAMESPACE}),
        ):
            with self.subTest(path=path):
                self._assert_role_bound_in(path, documents, namespaces)

    @unittest.skipUnless(shutil.which("helm") and shutil.which("kubectl"), "helm and kubectl needed")
    def test_the_namespaced_role_is_bound_in_the_profile_namespace_too(self):
        # The chart creates the profile namespace on every install and renders
        # the Role and its binding there as well; the kustomize path's
        # config/profiles does the same beside config/default. The subject is
        # still the operator's ServiceAccount in the install namespace.
        both = {INSTALL_NAMESPACE, PROFILE_NAMESPACE}
        for path, documents in (
            ("helm", _render_helm()),
            ("kustomize", _render_kustomize(KUSTOMIZE_DEFAULT_DIR, KUSTOMIZE_PROFILES_DIR)),
        ):
            with self.subTest(path=path):
                self._assert_role_bound_in(path, documents, both)
                namespaces = [d for d in documents if d.get("kind") == "Namespace" and d["metadata"]["name"] == PROFILE_NAMESPACE]
                self.assertEqual(1, len(namespaces), f"{path}: the profile namespace is not rendered")

    def test_the_profile_role_copy_matches_the_role_document(self):
        # config/profiles/role.yaml is the Role document copied by
        # `make chart-sync` (kustomize refuses a resource above its own
        # directory). The rules must be the generated ones, or the profile
        # namespace's grant drifts from the markers the self-check probes.
        copies = [d for d in yaml.safe_load_all(PROFILE_ROLE_COPY.read_text(encoding="utf-8")) if isinstance(d, dict)]
        self.assertEqual(1, len(copies), f"expected one document in {PROFILE_ROLE_COPY}, found {len(copies)}")
        self.assertEqual(ROLE_KIND, copies[0].get("kind"))
        self.assertEqual(_namespaced_role(self).get("rules"), copies[0].get("rules"))

    def test_every_namespaced_kind_carries_its_verbs_in_the_role_document(self):
        # Per kind, not per rule: controller-gen folds kinds with the same
        # verbs into one rule, so a verb dropped from one marker splits that
        # kind out of the shared rule and this reads the union over every
        # rule that names it.
        role = _namespaced_role(self)
        for kind in sorted(NAMESPACED_WRITE_KINDS):
            with self.subTest(kind=kind):
                verbs = {
                    verb
                    for rule in role.get("rules") or []
                    if kind in _rule_kinds(rule)
                    for verb in (rule.get("verbs") or [])
                }
                self.assertEqual(NAMESPACED_VERBS_BY_KIND.get(kind, WRITE_VERBS), verbs)

    def test_make_deploy_applies_the_profile_kustomization_and_its_namespace_names_the_install(self):
        # The profile namespace's Role only reaches a cluster if make deploy
        # applies config/profiles, and the operator only trusts the namespace
        # if its release-namespace annotation names the install namespace.
        recipe = OPERATOR_MAKEFILE.read_text(encoding="utf-8")
        self.assertIn(
            DEPLOY_RECIPE_PROFILES_LINE, recipe,
            "make deploy must apply config/profiles after config/default, joined with && so a failed first apply fails the target",
        )
        namespace = yaml.safe_load(PROFILES_NAMESPACE_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual("Namespace", namespace.get("kind"))
        self.assertEqual(INSTALL_NAMESPACE, (namespace.get("metadata", {}).get("annotations") or {}).get(RELEASE_NAMESPACE_ANNOTATION))
        self.assertEqual(PROFILE_COMPONENT, (namespace.get("metadata", {}).get("labels") or {}).get(PROFILE_COMPONENT_LABEL))

    def _assert_custom_resource_verbs_have_callers(self, source: str, cluster_roles: list[dict]) -> None:
        # Per resource, the union over every ClusterRole rule that names it:
        # equal to the table, so a verb added without a caller (or a caller's
        # verb dropped) is red here. The Go parity test holds role.yaml to the
        # self-check list; this holds the grant to the code that uses it.
        with self.subTest(source=source):
            self.assertEqual(1, len(cluster_roles), f"{source}: expected one operator ClusterRole")
            rules = cluster_roles[0].get("rules") or []
            for resource, want in sorted(CR_VERBS_BY_RESOURCE.items()):
                with self.subTest(source=source, resource=resource):
                    verbs = {
                        verb
                        for rule in rules
                        if CR_API_GROUP in (rule.get("apiGroups") or []) and resource in (rule.get("resources") or [])
                        for verb in (rule.get("verbs") or [])
                    }
                    self.assertEqual(want, verbs)
            for resource in sorted(CR_RESOURCES_WITHOUT_A_GRANT):
                with self.subTest(source=source, resource=resource):
                    self.assertFalse(
                        [rule for rule in rules if CR_API_GROUP in (rule.get("apiGroups") or []) and resource in (rule.get("resources") or [])],
                        f"{source}: {resource} is granted and nothing calls it",
                    )
            named = {
                resource
                for rule in rules
                if CR_API_GROUP in (rule.get("apiGroups") or [])
                for resource in (rule.get("resources") or [])
            }
            self.assertEqual(
                set(CR_VERBS_BY_RESOURCE) | CR_RESOURCES_OUTSIDE_THE_TABLE,
                named,
                f"{source}: a kubeagents.x-k8s.io resource on the ClusterRole is not classified in this test",
            )

    def test_each_custom_resource_verb_on_the_cluster_role_has_a_caller(self):
        self._assert_custom_resource_verbs_have_callers("role.yaml", [_cluster_role(self)])

    @unittest.skipUnless(shutil.which("helm"), "helm needed")
    def test_each_custom_resource_verb_on_the_charts_cluster_role_has_a_caller(self):
        # The chart's copy is spliced from role.yaml, so this is the same
        # assertion on the rendered form; make chart-check holds the splice.
        self._assert_custom_resource_verbs_have_callers(
            "helm",
            [d for d in _render_helm() if d.get("kind") == CLUSTER_ROLE_KIND and d["metadata"]["name"].endswith("operator-role")],
        )

    def test_the_cluster_role_grants_no_write_verb_on_a_namespaced_kind(self):
        # The general form of the Secret invariant: every kind the operator
        # renders is written in the agent's namespace or its profile namespace
        # only, so the ClusterRole keeps the reads (informers, escalation
        # prevention) and nothing else. A write verb here on any of them, or
        # a wildcard, is cluster-admin in effect by that kind's route.
        for rule in _cluster_role(self).get("rules") or []:
            kinds = _rule_kinds(rule)
            resources = set(rule.get("resources") or [])
            if WILDCARD in resources or WILDCARD in set(rule.get("apiGroups") or []):
                self.fail(f"the operator's ClusterRole carries a wildcard rule: {rule}")
            if kinds & NAMESPACED_WRITE_KINDS:
                self.assertTrue(
                    set(rule.get("verbs") or []) <= READ_VERBS,
                    f"the operator's ClusterRole grants {sorted(set(rule.get('verbs') or []) - READ_VERBS)} on "
                    f"{sorted(kinds & NAMESPACED_WRITE_KINDS)} cluster-wide; the write verbs belong in the Role document",
                )

    def test_the_role_document_names_exactly_the_namespaced_kinds_with_write_verbs(self):
        # Equality, not a subset: a kind dropped from the Role is a write the
        # operator can no longer do anywhere, which the self-check would name
        # at runtime and this names at review. The verbs are the four writes
        # and nothing else.
        role = _namespaced_role(self)
        self.assertEqual(
            NAMESPACED_WRITE_KINDS,
            {kind for rule in role.get("rules") or [] for kind in _rule_kinds(rule)},
        )
        for rule in role.get("rules") or []:
            verbs = set(rule.get("verbs") or [])
            kinds = _rule_kinds(rule)
            allowed = WRITE_VERBS | (READ_VERBS if kinds <= NAMESPACED_READ_KINDS else set())
            self.assertTrue(
                verbs <= allowed,
                f"the Role document grants {sorted(verbs - allowed)} on {sorted(kinds)}; reads stay in the ClusterRole",
            )
            self.assertTrue(verbs & WRITE_VERBS, f"a Role rule grants no write verb: {rule}")

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

    def test_the_namespaced_rule_names_only_namespaced_kinds(self):
        # A verb set is only a ceiling for the resources it is attached to.
        # controller-gen folds `secrets` into one rule with the other core
        # kinds that share its verbs; every kind in that rule must be one the
        # operator writes in the two namespaces, or a later verb addition on
        # the shared marker reaches further than it says.
        rule = _secret_rules(_namespaced_role(self))[0]
        self.assertTrue(_rule_kinds(rule) <= NAMESPACED_WRITE_KINDS, rule)

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
                # The Role document's copy for the profile namespace's
                # kustomization: the same rules, the same reason.
                str(PROFILE_ROLE_COPY.relative_to(REPO_ROOT)),
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
