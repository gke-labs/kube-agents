# The Operator's Namespaced Write Verbs

> **STATUS — proposed; awaiting a ruling.** Pull request #5145 (open at the time of writing; it
> closes #4906) moves the Secret write verbs off the operator ClusterRole into a namespaced Role in
> the install namespace; on `main` before it merges, the ClusterRole still carries them. This
> document is the follow-up that change named: the write verbs on every other namespaced kind the
> operator renders. It lays out the two shapes the review thread raised, the guard each needs, and a
> recommendation. Nothing here ships until an owner rules; the implementation is prepared on a
> branch and opens after #5145 merges.

**Scope:** which verbs on the operator's ClusterRole (`k8s-operator/config/rbac/role.yaml`,
rendered from the `+kubebuilder:rbac` markers in
`k8s-operator/internal/controller/platformagent_controller.go`) can leave it, in what object, bound
where, and what proves the shape on every pull request.
**Owns:** the shape of the operator's own Kubernetes authority. The agent's RBAC
(`buildMinimalPlatformRole` and the per-agent Roles) is the site's
[`reference/security-and-iam.md`](../site/src/content/docs/reference/security-and-iam.md); the
self-check that reports a shortfall is `rbac_selfcheck.go`; the install procedure is
[`INSTALL.md`](../../INSTALL.md).

## 1. The problem

The operator ClusterRole grants `create`, `update`, `patch` and `delete` on Deployments, ConfigMaps,
Services, ServiceAccounts, PVCs, Pods, ResourceQuotas, Jobs, StatefulSets, Leases, NetworkPolicies,
FQDNNetworkPolicies, PodDisruptionBudgets, Roles, RoleBindings and DispatchJobTemplates, `create`
and `patch` on Events, and `create` on PodTemplates, in every namespace. The operator writes each
of them in exactly two namespaces: the namespace of the `PlatformAgent` it reconciles, and that
namespace's profile namespace (`<agent namespace>-profiles`). Both install paths place the
`PlatformAgent` in the install namespace, and the admission webhook admits one `PlatformAgent` per
cluster (`k8s-operator/internal/webhook/platformagent_webhook.go`, "only one PlatformAgent is
allowed per cluster"), so on a shipped install the two namespaces are `kubeagents-system` and
`kubeagents-system-profiles`.

An unscoped `create` on `pods` or `serviceaccounts` is cluster-admin in effect by a different route
than the Secret verbs were: a pod in `kube-system` with a projected token, or a ServiceAccount bound
by someone else, reaches past the install. The Secret change removed one route and said so
(`test_C5_the_operators_secret_grant_is_namespaced`, "one of the rules"). This change removes the
rest.

The reads cannot move, with one exception. The kinds the manager's cache serves are read through
cluster-wide informers: the drift watches in `SetupWithManager` (`For`, `Owns` and `Watches` on
Deployments, StatefulSets, ServiceAccounts, PVCs, ConfigMaps, Services, NetworkPolicies,
PodDisruptionBudgets, Leases, Roles, RoleBindings, ClusterRoles and ClusterRoleBindings) and the
cached `Get`s of Jobs, ResourceQuotas and FQDNNetworkPolicies, each of which starts an informer that
lists and watches the kind in every namespace and blocks cache sync where the grant is narrower. The
agent's `minimal` ClusterRole grants cluster-wide reads the operator must hold to apply it. Nothing
mechanical pins a read tuple cluster-scoped today: `TestRequiredPermissionsMatchTheGeneratedRole`
holds the probe list to the markers in both directions, so a read moved with its probe passes it,
and only the Secret reads have a dedicated pin (`TestTheMinimalRoleSecretsReadLabelsAreTheProbesOwn`).
The guard in §3 supplies the pin for the rest: the Role document carries write verbs only. The
exception is `dispatchjobtemplates`, which has no `watch`, is read only through the uncached reader
in the profile namespace, and is not in the `minimal` ClusterRole; its `get` and `list` move with
its writes.

## 2. Inventory, from the code

Taken from `role.yaml` at the Secret change's head and from `upstream/main`, which differ only in
the Secret rule. The marker reason is the comment above the marker. "Agent" is `agent.Namespace`,
the install namespace on both paths; "profile" is `profileNamespaceName(agent)`; "operator" is
`POD_NAMESPACE`, the install namespace on both paths.

| Kind (group)                                        | Write verbs today          | Marker reason                                                        | Code that writes                                                                                                                                                                                                                                                                              | Namespaces written |
| --------------------------------------------------- | -------------------------- | -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `configmaps`                                        | create update patch delete | render: the agent's config                                           | `platformagent_controller.go` (the agent and minter ConfigMaps, `applyManaged`), `usage_counters_poller.go` (`writeDocument`), `platformagent_a2a_manifests.go` (auth-map deletes), `agentprofile_pool.go` (`buildProfileCABundle`, `deleteProfileCABundle`), `credential_proxy_exec_tier.go` | agent, profile     |
| `serviceaccounts`                                   | create update patch delete | render: the agent's and the profiles' identities                     | `manifest_helpers.go` (server-side apply), `agentprofile_controller.go` (`ensureServiceAccount` create, apply and delete in the profile namespace; `deleteOwnServiceAccount`)                                                                                                                 | agent, profile     |
| `services`                                          | create update patch delete | render                                                               | `applyManaged` of the agent, broker, gateway, bus, MCP and exec-tier Services; deletes in `platformagent_a2a_mcp.go`, `credential_proxy_exec_tier.go`                                                                                                                                         | agent              |
| `persistentvolumeclaims`                            | create update patch delete | render: the data claims                                              | `platformagent_controller.go` (create, `growDataClaim` merge patch, the A2A claim delete)                                                                                                                                                                                                     | agent              |
| `pods`                                              | create update patch delete | render: session pods (the gateway Role grants `create`/`delete`)     | `sizing_profile.go` (a server-side dry-run create), `platformagent_a2a_nats_repair.go` (a stuck NATS pod delete); held so `buildA2AGatewayRole` can grant `create` and `delete`                                                                                                               | agent              |
| `resourcequotas`                                    | create update patch delete | render: the session-pod quota and the profile quota                  | `applyManaged` of the session quota; `agentprofile_namespace.go` (`buildProfileQuota`, `deleteProfileQuota`)                                                                                                                                                                                  | agent, profile     |
| `events`                                            | create patch               | the Recorder                                                         | `recordEvent` on the `PlatformAgent`; the leader-election recorder                                                                                                                                                                                                                            | agent, operator    |
| `podtemplates`                                      | create                     | render: the dry run that defaults a Job template                     | `agentprofile_job_template.go` (`defaultedJobTemplate`, `DryRunAll`)                                                                                                                                                                                                                          | profile            |
| `secrets`                                           | create update patch delete | moved already: a Role in the install namespace                       | `ensureKubeProxyTLSSecret`, the A2A Secret writers                                                                                                                                                                                                                                            | agent              |
| `deployments` (apps)                                | create update patch delete | render: the agent, broker, gateway, callout, MCP, exec tier, pools   | `applyManaged`; `agentprofile_pool.go` (`buildProfilePool`, `deleteProfilePools`); deletes in `platformagent_controller.go`, `platformagent_a2a_mcp.go`, `credential_proxy_exec_tier.go`                                                                                                      | agent, profile     |
| `statefulsets` (apps)                               | create update patch delete | render: the NATS bus                                                 | `applyManaged`; `platformagent_controller.go` delete                                                                                                                                                                                                                                          | agent              |
| `jobs` (batch)                                      | create update patch delete | render: the provisioning run; the dispatcher Jobs Role grants writes | `platformagent_a2a_manifests.go` (provision Job create, patch, delete), `platformagent_scope_publisher.go` (create, delete), `agentprofile_job_template.go` (`deleteDispatcherJobsAndTemplates`); held so `buildA2ADispatcherJobsRole` can grant `create` and `delete`                        | agent, profile     |
| `leases` (coordination.k8s.io)                      | create update patch delete | render: the gateway lease and the leader Role; the election lease    | `platformagent_a2a_gateway_lease.go` (create, patch); the manager's own election; held so `buildPlatformLeaderRole` can grant the verbs                                                                                                                                                       | agent, operator    |
| `networkpolicies` (networking.k8s.io)               | create update patch delete | render: egress, ingress and the profile fence                        | `applyManaged`; `agentprofile_namespace.go` (`buildProfileNetworkPolicy`, `deleteProfileFence`); deletes in `platformagent_controller.go`, `platformagent_litellm_policy.go`, `platformagent_a2a_mcp.go`, `credential_proxy_exec_tier.go`                                                     | agent, profile     |
| `fqdnnetworkpolicies` (networking.gke.io)           | create update patch delete | render: the FQDN egress policy                                       | `applyManaged`; deletes in `platformagent_controller.go`                                                                                                                                                                                                                                      | agent              |
| `poddisruptionbudgets` (policy)                     | create update patch delete | render: the agent's and the exec tier's budgets                      | `applyManaged`; `credential_proxy_exec_tier.go`                                                                                                                                                                                                                                               | agent              |
| `roles`, `rolebindings` (rbac.authorization.k8s.io) | create update patch delete | render: the agent's namespaced RBAC                                  | `applyManaged` of the local, leader, gateway, callout and scope-publisher Roles and bindings; `platformagent_a2a_dispatcher.go` and `agentprofile_job_template.go` (the dispatcher's pods and jobs Roles and bindings); deletes in `platformagent_controller.go`                              | agent, profile     |
| `dispatchjobtemplates` (kubeagents.x-k8s.io)        | create update patch delete | render: the dispatcher Job policy's params                           | `agentprofile_job_template.go` (`applyDispatchJobParam`, the stale-template delete, `deleteDispatcherJobsAndTemplates`)                                                                                                                                                                       | profile            |

Writes that stay cluster-scoped, and why:

- `platformagents` and `agentprofiles` with their `status` and `finalizers` subresources, and
  `agentplugins/status`: the custom resources. Nothing fixes the namespace a CR is created in, and
  the controller writes status wherever it finds one.
- `clusterroles`, `clusterrolebindings`: cluster-scoped kinds (`kubeagents:minimal:<ns>:<name>`,
  `kubeagents:tokenreview:<ns>:<name>`, the callout's binding). The escalation check bounds them.
- `tokenreviews` `create` and `bind` on `system:auth-delegator`: cluster-scoped.

Reads that stay cluster-wide: `get`, `list` and `watch` on every kind above except
`dispatchjobtemplates` (the informers, §1); the escalation-prevention set `buildMinimalPlatformRole`
grants (nodes, namespaces, pods, pod logs, services, endpoints, events, PersistentVolumes, PVCs,
ResourceQuotas, LimitRanges, ConfigMaps, ServiceAccounts, Secrets, metrics, the `apps` and `batch`
kinds, NetworkPolicies, Ingresses, HorizontalPodAutoscalers, PodDisruptionBudgets, CRDs, the GitOps
kinds); and the operator's own by-name reads of cluster-scoped kinds (StorageClasses,
RuntimeClasses, the dispatcher admission policy) or of other namespaces (the `gke-metadata-server`
DaemonSet in `kube-system`, the `kubernetes` Endpoints in `default`). What pins them is the guard
in §3 (no read verb in the Role document) and, for Secrets,
`TestTheMinimalRoleSecretsReadLabelsAreTheProbesOwn`; `TestRequiredPermissionsMatchTheGeneratedRole`
holds the self-check to whatever the markers say and pins no scope of its own.

The rendered Roles the operator grants stay inside the two namespaces, so escalation prevention
holds under either shape below: `buildA2AGatewayRole` (`pods` create and delete) and
`buildPlatformLeaderRole` (`leases`, `pods` get and patch) are in the agent namespace;
`buildA2ADispatcherJobsRole` (`jobs` create and delete) is in the profile namespace;
`buildScopePublisherRole` (`configmaps` patch) is in the agent namespace. The API server checks the
grantor's permissions in the Role's own namespace, and the write grant is bound in both.

## 3. Shape (a): a Role per namespace

The shape the Secret change ships, applied to the whole inventory. Every write marker carries
`namespace=kubeagents-system`; controller-gen renders the write verbs as the one `Role` document in
`role.yaml` beside the ClusterRole, and merges rules with the same verb set. The read marker for
each kind stays without `namespace=`.

Objects. In the install namespace: the Role (`kubeagents-manager-role` under kustomize,
`<release>-operator-namespaced-role` under Helm) and its RoleBinding, both existing. In the profile
namespace: a second Role with the same rules and a RoleBinding, new. The chart renders the rules
block once, inside a named template, and ranges over the two namespaces (the profile namespace only
under `mode: next`, where `profiles-namespace.yaml` creates it); `hack/sync-chart-manifests.sh`
keeps splicing one block between the `GENERATED NAMESPACED RULES` markers. Kustomize's
`config/default` rewrites every namespaced object to the install namespace, so the profile
namespace's copy cannot live there: it is a `config/profiles` kustomization (the namespace with the
markers the operator requires, the Role, the RoleBinding) that `make deploy` applies after
`config/default`. Today kustomize renders no profile namespace at all, so this is the first time
that path carries one.

Guard. `test_C5_the_operators_secret_grant_is_namespaced` becomes
`test_C5_the_operators_write_verbs_are_namespaced`: on both sources (`operator_clusterrole`,
`chart_operator_rbac`), no ClusterRole rule names a kind in the inventory, or `*`, with a verb
outside `get`/`list`/`watch`; the set of kinds the Role document names equals the inventory
(equality, not a subset, or a kind dropped from the Role passes), and its verbs are write verbs
only (plus `get` and `list` on `dispatchjobtemplates`); and the chart renders the Role in the
profile namespace as well. `hack/conformance-mutations.py` re-cuts
`C5-operator-secrets-cluster-wide`: its anchor is the adjacent pair `configmaps`,
`persistentvolumeclaims`, which controller-gen sorts apart on the ClusterRole's read rule once the
write verbs leave and keeps adjacent in the Role document, so the first-occurrence replace would
land in the Role and the mutant would survive. The re-cut anchors on the ClusterRole's core read
rule (its resource list is unique) and appends `create` to its verbs, which plants `secrets` and
`configmaps` writes cluster-wide at once; a second mutation removes `configmaps` from the Role
document. Both must be `KILLED`. `tests/test_operator_secrets_grant.py` generalises the same way
and its binding test asserts the profile-namespace RoleBinding on the chart path. Trivy's KSV-0041
exemption stays for the Secret reads; a Role is KSV-0113, under the scan's threshold.

What goes stale. A `PlatformAgent` outside the install namespace needs a hand copy of the Role and
a RoleBinding in its namespace and in `<its namespace>-profiles`; the copy is stale the next time a
marker gains a verb. The self-check reports it (`Degraded`/`RBACIncomplete` naming the namespace)
and nothing else does. The shipped shape has no such agent: both install paths place it in the
install namespace, the webhook admits one per cluster, and
`test_no_shipped_manifest_places_a_platformagent_outside_the_install_namespace` keeps every shipped
manifest there.

## 4. Shape (b): one write ClusterRole, bound per namespace

The write verbs are a second ClusterRole that no ClusterRoleBinding names, granted per namespace by
a RoleBinding whose `roleRef` is the ClusterRole. One definition; a per-namespace grant is one object
with no rules, which cannot go stale.

Objects. controller-gen renders one ClusterRole from the markers and a Role per `namespace=` value;
it cannot render a second ClusterRole. So the markers keep `namespace=` (the Role document stays the
generated source of the write rules and `TestRequiredPermissionsMatchTheGeneratedRole` keeps
parsing it), and each delivery path renders those rules under a different kind: the chart's
template changes `kind: Role` to `kind: ClusterRole` around the spliced block and names it
`<release>-<namespace>-operator-write-role`; kustomize needs a generated file, because kustomize
cannot change a kind — a step in `make manifests` writes
`config/rbac/write_clusterrole.yaml` from the Role document, and a test holds the two equal.
RoleBindings in the install namespace and the profile namespace reference the ClusterRole; the
profile namespace's binding has the same `config/profiles` home on the kustomize path as in shape
(a), but with no rules to keep in step.

Guard. The C5 test becomes `test_C5_the_operators_write_clusterrole_is_never_bound_cluster_wide`:
exactly one ClusterRole carries a write verb on an inventory kind (the write ClusterRole, by name),
the manager ClusterRole carries none, no ClusterRoleBinding's `roleRef` names the write ClusterRole,
and at least one RoleBinding does. The harness's `operator_clusterrole` source is `role.yaml` alone
and holds no binding, so the assertion needs a source that does: a new harness source over a
`kubectl kustomize config/default` render (as `tests/test_operator_secrets_grant.py` already does),
beside `chart_operator_rbac`, which carries the chart's bindings. Mutations: one changes the
RoleBinding's `kind` to `ClusterRoleBinding` in `namespaced_role_binding.yaml` and in the chart
template (the bound the test exists for), one plants a write verb on the manager ClusterRole as
today. Both `KILLED`. `tests/test_operator_secrets_grant.py`'s
`test_the_cluster_role_grants_no_secret_write_verb` inverts: the write ClusterRole must carry the
Secret verbs and must not be cluster-bound. Trivy flags the write ClusterRole under KSV-0041
regardless of its bindings; the path-scoped exemption on `k8s-operator/config/rbac/**` already
covers the generated file, so only its comment changes, and it has to say the object is unbound.

What it costs. The object exists cluster-wide under a name one `kubectl create clusterrolebinding`
away from the pre-change shape, and the guard holds shipped manifests only. An auditor reading
`role.yaml` or the chart sees a ClusterRole that grants cluster-wide writes on every rendered kind
and has to read the bindings to learn it is not bound that way; the issue this follows was filed by
an auditor reading the ClusterRole. The self-check's namespaced tuples are probed the same way as in
shape (a), since a RoleBinding to a ClusterRole answers a namespaced review and not a cluster-wide
one.

## 5. What both shapes share

The self-check. `requiredPermissions` marks every write tuple `Namespaced: true`; `probeRBAC`
reviews them in the operator's namespace, in each agent namespace it observes, and, new, in the
profile namespace of each `mode: next` agent whose profile namespace is ready
(`profileNamespaceReady`). A denial reads `create deployments.apps (in namespace
kubeagents-system-profiles)` and the cause sentence names the object to add. The probe count grows
from one namespace to two on a stock install; the reviews are concurrent and bounded by
`rbacProbeConcurrency`.

The webhook. One `PlatformAgent` per cluster, in any namespace. Neither shape changes it. A rule
that also pins the namespace to the operator's (`POD_NAMESPACE`) would remove the hand-managed case
from shape (a) entirely; it is a behaviour change and a decision for the owner, listed below.

The leader-election Role. The chart's `operator-leader-election-role` grants `events` only and its
comment says the lease verbs are in the ClusterRole; after the move they are in the install
namespace's Role, which covers the election lease because the operator runs there. The comment
changes; kustomize's `leader_election_role.yaml` already grants `leases`.

Escalation prevention. Unchanged in effect: the operator holds in each namespace what the Roles it
renders there grant, and the ClusterRoles it renders carry reads it holds cluster-wide. One case is
new under both shapes: on an install whose operator image predates its manifests, the operator
probes the write verbs cluster-wide, reports `RBACIncomplete`, and still writes, because the install
namespace's binding grants them. The reverse skew reports nothing.

The docs. `reference/security-and-iam.md` ("The operator controller is a separate identity") moves
from "the Secret write verbs are the namespaced Role, and nothing else is in it" to the inventory,
and its `kubectl auth can-i` block gains a `create deployments.apps --all-namespaces` line answering
`no`. The CRD reference's `RBACIncomplete` row names the profile namespace. The operator page's
"An image ahead of its ClusterRole" section and its per-agent-namespace probe paragraph gain the
profile namespace. The operator README's RBAC section drops "the Secret write verbs are the one such
marker". The conformance README's C5 row takes the renamed test. The header comment of
`config/rbac/namespaced_role_binding.yaml` ("carries the operator's Secret write verbs") and the
marker header in `platformagent_controller.go` describe the whole inventory.

## 6. Upgrade path

A Helm upgrade applies the shrunk ClusterRole, the grown Role, the profile namespace's objects and
the new operator image in one release. Helm orders ClusterRole before Role, so a write in flight
during the apply can be refused once; the reconcile requeues and the next pass succeeds. The
operator pod of the old image, if it outlives the RBAC apply, keeps writing in the install namespace
under the new Role and reports `RBACIncomplete` for the cluster-wide probes until its replacement
starts. A kustomize `make deploy` behaves the same, and applies `config/profiles` after
`config/default`. An install whose profile namespace was created by hand (kustomize, `mode: next`)
needs the Role and RoleBinding added there, or under shape (b) the RoleBinding; the self-check names
the namespace until then. Revert: revert the commits, `make manifests && make chart-sync`, upgrade
again.

## 7. Recommendation

Shape (a), extended to the whole inventory, with the profile namespace rendered by the chart and by
a `config/profiles` kustomization.

- It is the mechanism that already ships and that controller-gen renders natively: one marker
  attribute, one generated Role document, no second generator and no kind swap. Shape (b) adds a
  generated ClusterRole file and a parity test to hold it to the Role document.
- Its invariant is the one an auditor can check by reading one object: no ClusterRole grants a write
  verb on a namespaced kind. Shape (b)'s invariant is about bindings, which is the same bound held
  one object further from the grant, and leaves a cluster-wide write ClusterRole in the tree for the
  next audit to file again.
- The stale-copy case shape (b) removes is the `PlatformAgent` outside the install namespace, which
  no install path produces, no shipped manifest places, and the webhook does not require. For the
  profile namespace, which the chart creates, the chart renders the copy from the same generated
  block, so it cannot drift.

Switch to shape (b) if a `PlatformAgent` outside the install namespace becomes a supported shape, or
if the operator ever writes into a namespace the chart does not create. The switch is a kind change
in two templates and the guard flip in §4.

## 8. Decisions for the owner

1. Shape (a) or (b).
2. Whether the webhook should also refuse a `PlatformAgent` outside the operator's namespace. Yes
   removes the only hand-managed case from shape (a); no keeps today's behaviour and the
   `RBACIncomplete` disclosure.
3. One Role document for both namespaces, or a narrower profile-namespace document (the profile
   namespace needs ConfigMaps, ServiceAccounts, Deployments, ResourceQuotas, NetworkPolicies, Roles,
   RoleBindings, Jobs, PodTemplates and DispatchJobTemplates; not Secrets, Services, PVCs,
   StatefulSets, Leases, PodDisruptionBudgets or FQDNNetworkPolicies). One document is one marker
   set and one spliced block; two is tighter and doubles the markers of the shared kinds.
