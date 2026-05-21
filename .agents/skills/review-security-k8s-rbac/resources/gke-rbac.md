2026-05-21 copy of Best practices for GKE RBAC from https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac

This document is about good practices for planning your role-based access
control (RBAC) policies in Google Kubernetes Engine (GKE) (GKE). This
document assumes that you know about the following:

- [RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [Kubernetes API groups](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.30/#-strong-api-groups-strong-)
- [Kubernetes API resources](https://kubernetes.io/docs/reference/kubernetes-api/)
- [Kubernetes API verbs](https://kubernetes.io/docs/reference/using-api/api-concepts/#api-verbs)

RBAC is a core security feature in Kubernetes that lets you create fine-grained
permissions to manage what actions users and workloads can perform on resources
in your clusters. You create RBAC *roles* and *bind* those roles to *subjects*,
which are authenticated users such as service accounts or Google Groups.

This document is for Security specialists and Operators who plan and
implement RBAC policies for their organization. To learn more about common roles
and example tasks that we reference in Google Cloud content, see
[Common GKE user roles and tasks](https://docs.cloud.google.com/kubernetes-engine/enterprise/docs/concepts/roles-tasks).

For a checklist of the guidance in this document, see
[Checklist summary](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#checklist-summary).
For a consolidated overview of all GKE best practices, see [Best practices for GKE](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices).

To learn how to implement RBAC in Google Kubernetes Engine (GKE), see
[Configure role-based access control](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control).

## How RBAC works

RBAC supports the following types of roles and bindings:

- **ClusterRole:** a set of permissions that can be applied to any namespace, or to the entire cluster.
- **Role:** a set of permissions that is limited to a single namespace.
- **ClusterRoleBinding:** bind a `ClusterRole` to a user or a group for all namespaces in the cluster.
- **RoleBinding:** bind a `Role` or a `ClusterRole` to a user or a group within a specific namespace.

You define permissions as `rules` in a `Role` or a `ClusterRole`. Each `rules`
field in a role consists of an API group, the API resources within that API
group, and the verbs (actions) allowed on those resources. Optionally, you
can scope verbs to named instances of API resources by using the `resourceNames`
field. For an example, see
[Restrict access to specific resource instances](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#named-resources).

After defining a role, you use a `RoleBinding` or a `ClusterRoleBinding` to bind
the role to a subject. Choose the type of binding based on whether you want to
grant permissions in a single namespace or in multiple namespaces.

## RBAC role design

### Use the principle of least privilege

When assigning permissions in an RBAC role, use the principle of least privilege
and grant the minimum permissions needed to perform a task. Using the principle
of least privilege reduces the potential for privilege escalation if your
cluster is compromised, and reduces the likelihood that excessive access results
in a security incident.

When designing your roles, carefully consider common privilege escalation risks,
such as `escalate` or `bind` verbs, `create` access for PersistentVolumes, or
`create` access for Certificate Signing Requests. For a list of risks, refer to
[Kubernetes RBAC - privilege escalation risks](https://kubernetes.io/docs/concepts/security/rbac-good-practices/#privilege-escalation-risks).

### Don't delete system RBAC roles and bindings

Kubernetes creates several RBAC resources that have the `system:` prefix, such
as the `system:basic-user`, `system:discovery`, and `system:public-info-viewer`
ClusterRoleBindings. These resources are required for correct cluster
functionality. Avoid deleting system roles and bindings because this can cause
cluster instability. The Kubernetes API server tries to automatically reconcile
these resources on startup, but if reconciliation fails, your cluster might
become inaccessible.

To help ensure that system roles and bindings aren't deleted or modified, review
your RBAC policies and confirm/verify that subjects don't have `delete` or
`update` permissions on RoleBindings and ClusterRoleBindings that have `system:`
prefixes.

### Avoid default roles and groups

Kubernetes creates a set of default ClusterRoles and ClusterRoleBindings that
you can use for API discovery and to enable managed component functionality. The
permissions granted by these default roles might be extensive depending on the
role. Kubernetes also
has a set of default users and user groups, identified by the `system:` prefix.
By default, Kubernetes and GKE automatically bind these roles to
the default groups and to various subjects. For a full list of the default roles
and bindings that Kubernetes creates, refer to
[Default roles and role bindings](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#default-roles-and-role-bindings).

The following table describes some default roles, users, and groups. We
recommend that you avoid interacting with these roles, users, and groups unless
you've carefully evaluated them, because interacting with these resources can have
unintended consequences to your cluster's security posture.

| Name | Type | Description |
|---|---|---|
| `cluster-admin` | ClusterRole | Grants a subject permission to do anything on any resource in the cluster. |
| `system:anonymous` | User | Kubernetes assigns this user to API server requests that have no authentication information provided. Binding a role to this user gives any unauthenticated user the permissions granted by that role. |
| `system:unauthenticated` | Group | Kubernetes assigns this group to API server requests that have no authentication information provided. Binding a role to this group gives any unauthenticated user the permissions granted by that role. |
| `system:authenticated` | Group | GKE assigns this group to API server requests made by any user who is signed in with a Google Account, including all Gmail accounts. In practice, this isn't meaningfully different from `system:unauthenticated` because anyone can create a Google Account. Binding a role to this group gives any user with a Google Account, including all Gmail accounts, the permissions granted by that role. |
| `system:masters` | Group | Kubernetes assigns the `cluster-admin` ClusterRole to this group by default to enable system functionality. Adding your own subjects to this group gives those subjects access to do anything to any resource in your cluster. |

**If possible, avoid creating bindings that involve the default users, roles,
and groups.** This can have unintended consequences to your cluster's security
posture. For example:

- Binding the default `cluster-admin` ClusterRole to the `system:unauthenticated` group gives any unauthenticated users access to all resources in the cluster (including Secrets). These highly-privileged bindings are actively targeted by attacks such as mass malware campaigns.
- Binding a custom Role to the `system:unauthenticated` group gives unauthenticated users the permissions granted by that Role.

When possible, use the following guidelines:

- Don't add your own subjects to the `system:masters` group.
- Don't bind the `system:unauthenticated` group to any RBAC roles.
- Don't bind the `system:authenticated` group to any RBAC roles.
- Don't bind the `system:anonymous` user to any RBAC roles.
- Don't bind the `cluster-admin` ClusterRole to your own subjects or to any of the default users and groups. If your application requires many permissions, determine the exact permissions required and create a specific role for that purpose.
- Evaluate the permissions granted by other default roles before binding subjects.
- Evaluate the roles bound to default groups before modifying the members of those groups.

#### Prevent usage of default groups

You can use the gcloud CLI to disable non-default RBAC bindings in a
cluster that reference the `system:unauthenticated` and `system:authenticated`
groups or the `system:anonymous` user. Use one or both of the following flags
when you create a new GKE cluster or update an existing cluster.
Using these flags doesn't disable the default Kubernetes bindings that reference
these groups. These flags require GKE version 1.30.1-gke.1283000
or later.

> [!CAUTION]
> **Caution:** Updating an existing cluster with these flags doesn't delete existing non-default bindings that reference these groups. After updating your cluster, find and remove non-default bindings. For details, see [Detect and remove usage of default roles and groups](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#detect-prevent-default).

- [`--no-enable-insecure-binding-system-authenticated`](https://docs.cloud.google.com/sdk/gcloud/reference/container/clusters/create#--enable-insecure-binding-system-authenticated): Disable non-default bindings that reference `system:authenticated`.
- [`--no-enable-insecure-binding-system-unauthenticated`](https://docs.cloud.google.com/sdk/gcloud/reference/container/clusters/create#--enable-insecure-binding-system-unauthenticated): Disable non-default bindings that reference `system:unauthenticated` and `system:anonymous`.

#### Detect and remove usage of default roles and groups

> [!NOTE]
> **Note:** To help secure your clusters against mass malware attacks that exploit `cluster-admin` access misconfigurations, GKE clusters running version 1.28 and later won't allow you to bind the `cluster-admin` ClusterRole to the `system:anonymous` user or to the `system:unauthenticated` or `system:authenticated` groups.

To check whether your clusters reference these users and groups in RBAC
bindings, enable the standard tier of Kubernetes security posture scanning for
your clusters or fleet so that GKE can show you results in the
security posture dashboard in the Google Cloud console. For instructions,
see
[Enable workload configuration auditing](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/protect-workload-configuration#enable-security-posture).

The following sections show you how to find the specific RoleBindings or
ClusterRoleBindings that reference default users and groups, and how to delete
those resources.

##### ClusterRoleBindings

1. List the names of any ClusterRoleBindings with the subject
   `system:anonymous`, `system:unauthenticated`, or `system:authenticated`:

       kubectl get clusterrolebindings -o json \
         | jq -r '["Name"], ["---"], (.items[] | select((.subjects | length) > 0) | select(any(.subjects[]; .name == "system:anonymous" or .name == "system:unauthenticated" or .name == "system:authenticated")) | [.metadata.namespace, .metadata.name]) | @tsv'

   The output should list only the following ClusterRoleBindings:

       Name
       ---
       "system:basic-user"
       "system:discovery"
       "system:public-info-viewer"

   If the output contains additional non-default bindings, do the following
   for *each additional binding*. If your output doesn't contain non-default
   bindings, skip the following steps.

   > [!CAUTION]
   > **Caution:** Don't delete the default system ClusterRoleBindings listed in the previous output. For details, see [Don't delete system RBAC roles and bindings](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#dont-delete-system-rbac).

2. List the permissions of the role associated with the binding:

       kubectl get clusterrolebinding CLUSTER_ROLE_BINDING_NAME -o json \
           | jq ' .roleRef.name +" " + .roleRef.kind' \
           | sed -e 's/"//g' \
           | xargs -l bash -c 'kubectl get $1 $0 -o yaml'

   Replace `CLUSTER_ROLE_BINDING_NAME` with the name of
   the non-default ClusterRoleBinding.

   The output is similar to the following:

       apiVersion: rbac.authorization.k8s.io/v1
       kind: ClusterRole
       metadata:
       ...
       rules:
       - apiGroups:
         - ""
         resources:
         - secrets
         verbs:
         - get
         - watch
         - list

   If you determine that the permissions in the output are safe to grant to the
   default users or groups, no further action is required. If you determine
   that the permissions granted by the binding are unsafe, proceed to the next
   step.
3. Delete an unsafe binding from your cluster:

       kubectl delete clusterrolebinding CLUSTER_ROLE_BINDING_NAME

   Replace `CLUSTER_ROLE_BINDING_NAME` with the name of
   the ClusterRoleBinding to delete.

##### RoleBindings

1. List the namespace and name of any RoleBindings with the subject
   `system:anonymous`, `system:unauthenticated`, or `system:authenticated`:

       kubectl get rolebindings -A -o json \
         | jq -r '["Namespace", "Name"], ["---", "---"], (.items[] | select((.subjects | length) > 0) | select(any(.subjects[]; .name == "system:anonymous" or .name == "system:unauthenticated" or .name == "system:authenticated")) | [.metadata.namespace, .metadata.name]) | @tsv'

   If your cluster is configured correctly, the output should be **blank** .
   If the output contains any non-default bindings, do the following
   steps for *each additional binding*. If your output is blank, skip the
   following steps.

   If you only know the name of the RoleBinding then you can use the
   following command to find matching rolebindings across all namespaces:

       kubectl get rolebindings -A -o json \
         | jq -r '["Namespace", "Name"], ["---", "---"], (.items[] | select((.subjects | length) > 0) | select(.metadata.name == "ROLE_BINDING_NAME") | [.metadata.namespace, .metadata.name]) | @tsv'

   Replace `ROLE_BINDING_NAME` with the name of the
   non-default RoleBinding.
2. List the permissions of the Role associated with the binding:

       kubectl get rolebinding ROLE_BINDING_NAME --namespace ROLE_BINDING_NAMESPACE -o json \
           | jq ' .roleRef.name +" " + .roleRef.kind' \
           | sed -e 's/"//g' \
           | xargs -l bash -c 'kubectl get $1 $0 -o yaml --namespace ROLE_BINDING_NAMESPACE'

   Replace the following:
   - `ROLE_BINDING_NAME`: the name of the non-default RoleBinding.
   - `ROLE_BINDING_NAMESPACE`: the namespace of the non-default RoleBinding.

   The output is similar to the following:

       apiVersion: rbac.authorization.k8s.io/v1
       kind: Role
       metadata:
       ...
       rules:
       - apiGroups:
         - ""
         resources:
         - secrets
         verbs:
         - get
         - watch
         - list

   If you determine that the permissions in the output are safe to grant to the
   default users or groups, no further action is required. If you determine
   that the permissions granted by the binding are unsafe, proceed to the next
   step.
3. Delete an unsafe binding from your cluster:

       kubectl delete rolebinding ROLE_BINDING_NAME --namespace ROLE_BINDING_NAMESPACE

   Replace the following:
   - `ROLE_BINDING_NAME`: the name of the RoleBinding to delete.
   - `ROLE_BINDING_NAMESPACE`: the namespace of the RoleBinding to delete.

### Scope permissions to the namespace level

Use bindings and roles as follows, depending on the needs of your workload or
user:

- To grant access to resources in **one** namespace, use a `Role` with a `RoleBinding`.
- To grant access to resources in **more than one** namespace, use a `ClusterRole` with a `RoleBinding` for each namespace.
- To grant access to resources in **every** namespace, use a `ClusterRole` with a `ClusterRoleBinding`.

Grant permissions in as few namespaces as possible.

### Don't use wildcards

The `*` character is a *wildcard* that applies to everything. Avoid using
wildcards in your rules. Explicitly specify API groups, resources, and verbs in
RBAC rules. For example, specifying `*` in the `verbs` field would grant `get`,
`list`, `watch`, `patch`, `update`, `deletecollection`, and `delete` permissions
on the resources. The following table shows examples of avoiding wildcards in
your rules:

| Recommended | Not recommended |
|---|---|
| ```yaml - rules: apiGroups: ["apps","extensions"] resources: ["deployments"] verbs: ["get","list","watch"] ``` Grants `get`, `list`, and `watch` verbs specifically to the `apps` and `extensions` API groups. | ```yaml - rules: apiGroups: ["*"] resources: ["deployments"] verbs: ["get","list","watch"] ``` Grants the verbs to `deployments` in any API group. |
| ```yaml - rules: apiGroups: ["apps", "extensions"] resources: ["deployments"] verbs: ["get", "list", "watch"] ``` Grants only `get`, `list`, and `watch` verbs to deployments in the `apps` and `extensions` API groups. | ```yaml - rules: apiGroups: ["apps", "extensions"] resources: ["deployments"] verbs: ["*"] ``` Grants all verbs, including `patch` or `delete`. |

### Use separate rules to grant least-privilege access to specific resources

When planning your rules, try the following high-level steps for a more
efficient least-privilege rule design in each role:

1. Draft separate RBAC rules for each verb on each resource that a subject needs to access.
2. After drafting the rules, analyze the rules to check whether multiple rules have the same `verbs` list. Combine those rules into a single rule.
3. Keep all the remaining rules separate from each other.

This approach results in a more organized rule design, where rules that grant
the same verbs to multiple resources are combined, and rules that grant
different verbs to resources are separate.

For example, if your workload needs get permissions for the `deployments`
resource, but needs `list` and `watch` on the `daemonsets` resources, you should
use separate rules when creating a role. When you bind the RBAC role to your
workload, it won't be able to use `watch` on `deployments`.

As another example, if your workload needs `get` and `watch` on both the `pods`
resource and the `daemonsets` resource, you can combine those into a single
rule, because the workload needs the same verbs on both resources.

In the following table, both rule designs work, but the split rules more
granularly restrict resource access based on your needs:

| Recommended | Not recommended |
|---|---|
| ```yaml - rules: apiGroups: ["apps"] resources: ["deployments"] verbs: ["get"] - rules: apiGroups: ["apps"] resources: ["daemonsets"] verbs: ["list", "watch"] ``` Grants `get` access for Deployments and `watch` and `list` access for DaemonSets. Subjects can't list Deployments. | ```yaml - rules: apiGroups: ["apps"] resources: ["deployments", "daemonsets"] verbs: ["get","list","watch"] ``` Grants the verbs to both Deployments and DaemonSets. A subject who might not require `list` access on `deployments` objects would still get that access. |
| ```yaml - rules: apiGroups: ["apps"] resources: ["daemonsets", "deployments"] verbs: ["list", "watch"] ``` Combines two rules because the subject needs the same verbs for both the `daemonsets` and `deployments` resources. | ```yaml - rules: apiGroups: ["apps"] resources: ["daemonsets"] verbs: ["list", "watch"] - rules: apiGroups: ["apps"] resources: ["deployments"] verbs: ["list", "watch"] ``` These split rules would have the same result as the combined rule, but would create unnecessary clutter in your role manifest |

### Restrict access to specific resource instances

RBAC lets you use the `resourceNames` field in your rules to restrict access to
a specific named instance of a resource. For example, if you're writing an RBAC
role that needs to `update` the `seccomp-high` ConfigMap and nothing else, you
can use `resourceNames` to specify only that ConfigMap. Use `resourceNames`
whenever possible.

> [!NOTE]
> **Note:** You can't use `resourceNames` for `list` and `create` verbs. For example, if you're writing a role that needs to `list` all ConfigMaps in addition to updating the `seccomp-high` ConfigMap, you need to split the rules.

| Recommended | Not recommended |
|---|---|
| ```yaml - rules: apiGroups: [""] resources: ["configmaps"] resourceNames: ["seccomp-high"] verbs: ["update"] ``` Restricts the subject to only update the `seccomp-high` ConfigMap. The subject can't update any other ConfigMaps in the namespace. | ```yaml - rules: apiGroups: [""] resources: ["configmaps"] verbs: ["update"] ``` The subject can update the `seccomp-high` ConfigMap and any other ConfigMap in the namespace. |
| ```yaml - rules: apiGroups: [""] resources: ["configmaps"] verbs: ["list"] - rules: apiGroups: [""] resources: ["configmaps"] resourceNames: ["seccomp-high"] verbs: ["update"] ``` Grants `list` access to all ConfigMaps in the namespace, including `seccomp-high`. Restricts `update` access to only the `seccomp-high` ConfigMap. The rules are split because you can't grant `list` for named resources. | ```yaml - rules: apiGroups: [""] resources: ["configmaps"] verbs: ["update", "list"] ``` Grants `update` access for all ConfigMaps, along with `list` access. |

### Don't let service accounts modify RBAC resources

Do not bind `Role` or `ClusterRole` resources that have `bind`, `escalate`,
`create`, `update`, or `patch` permissions on the `rbac.authorization.k8s.io`
API group to service accounts in any namespace. `escalate` and `bind` in
particular can let an attacker bypass the
[escalation prevention mechanisms built into RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#privilege-escalation-prevention-and-bootstrapping).

### Restrict the ability for workloads to self-modify

Certain Kubernetes workloads, especially system workloads, have permission to
self-modify. For example, some workloads vertically autoscale themselves.
Self-modification can allow an attacker who has already compromised a node to
escalate their access in the cluster. For example, an attacker could have
a workload in a namespace change itself to run as a more privileged
ServiceAccount in the same namespace.

Unless necessary, don't give Pods permission to self-modify. If some Pods must
self-modify, use Policy Controller to limit what the workloads can
change. For example, you can use the
[NoUpdateServiceAccount](https://docs.cloud.google.com/kubernetes-engine/enterprise/policy-controller/docs/latest/reference/constraint-template-library#noupdateserviceaccount)
constraint template to prevent Pods from changing their ServiceAccount.
When you create a policy, exclude any cluster management components from your
constraints, like in the following example:

    parameters:
      allowedGroups:
      - system:masters
      allowedUsers:
      - system:addon-manager

## Kubernetes service accounts

### Create a Kubernetes service account for each workload

Create a separate Kubernetes service account for each workload. Bind a
least-privilege `Role` or `ClusterRole` to that service account.

### Don't use the default service account

Kubernetes creates a service account named `default` in every namespace. The
`default` service account is automatically assigned to Pods that don't
explicitly specify a service account in the manifest. Avoid binding a `Role` or
`ClusterRole` to the `default` service account. Kubernetes might assign the `default`
service account to a Pod that doesn't need the access granted in those roles.

### Don't automatically mount service account tokens

The `automountServiceAccountToken` field in the Pod specification tells
Kubernetes to inject a credential token for a Kubernetes service account into
the Pod. The Pod can use this token to make authenticated requests to the
Kubernetes API server. The default value for this field is `true`.

In all GKE versions, set `automountServiceAccountToken=false` in
the Pod specification if your Pods don't need to communicate with the API
server.

### Prefer ephemeral tokens over Secret-based tokens

By default, the kubelet process on the node retrieves a short-lived,
automatically rotating service account token for each Pod. The kubelet mounts
this token on the Pod as a
[projected volume](https://kubernetes.io/docs/concepts/storage/projected-volumes/)
unless you set the `automountServiceAccountToken` field to `false` in the Pod
specification. Any calls to the Kubernetes API from the Pod use this token to
authenticate to the API server.

If you're manually retrieving service account tokens, avoid using Kubernetes
Secrets to store the token. Secret-based service account tokens are legacy
credentials that don't expire and aren't rotated automatically. If you need
credentials for service accounts, use the
[`TokenRequest` API](https://kubernetes.io/docs/reference/kubernetes-api/authentication-resources/token-request-v1/)
to obtain short-lived tokens that are automatically rotated.

## Continuously review RBAC permissions

Review your RBAC roles and access regularly to identify potential escalation
pathways and redundant rules. For example, consider a situation where you don't
delete a `RoleBinding` that binds a `Role` with special privileges to a deleted
user. If an attacker creates a user account in that namespace with the same name
as the deleted user, they'd be bound to that `Role` and would inherit the same
access. Periodic reviews minimize this risk.

## Checklist summary

[Use the principle of least privilege](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#least-privilege) [Avoid default roles and groups](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#default-roles-groups) [Don't delete system RBAC roles and bindings](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#dont-delete-system-rbac) [Scope permissions to the namespace level](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#namespace-level-permissions) [Don't use wildcards](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#no-wildcards) [Use separate rules to grant least-privilege access to specific resources](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#split-rules) [Restrict access to specific resource instances](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#named-resources) [Don't let service accounts modify RBAC resources](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#service-accounts-rbac-modify) [Restrict the ability for workloads to self-modify](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#restrict-self-modify) [Create a Kubernetes service account for each workload](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#service-account-per-app) [Don't use the default service account](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#avoid-default-service-account) [Don't automatically mount service account tokens](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#avoid-token-automount) [Prefer ephemeral tokens over Secret-based tokens](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#ephemeral-tokens) [Continuously review RBAC permissions](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac#review-permissions)

## What's next

- [Read the GKE hardening advice](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/hardening-your-cluster).
- [Read Kubernetes RBAC good practices](https://kubernetes.io/docs/concepts/security/rbac-good-practices/).
- [Explore our other best practices](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices).
- [View sample manifests for common cluster roles](https://github.com/GoogleCloudPlatform/gke-rbac-best-practices)