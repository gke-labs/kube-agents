# A namespace bound for a team's profile

A proposal for the security approver; nothing in it is built. This is the design for the
guardrail of #3009, a part of the epic #2900. It says how the tasks of one profile are held to
the Kubernetes namespaces of one team. It makes no claim about a person of that team: the
person bound gets a design of its own.

Each row of [Decisions](#decisions) is our proposal, with its other way beside it. After the
security approval we build the proposal of each row. A row that goes the other way changes this
document first.

The security approver decided four points on 2026-11-27
([the decision](https://github.com/gke-labs/kube-agents/pull/7354#issuecomment-7207672849)):
the resource of a raw path (row 4), the refused name `default` (row 1), the admission policy of
row 7, and that the person bound is not a row of this document. Each of the three rows says
"Decided" at that point.

Related documents:

- [`profile-namespace-bound-facts.md`](profile-namespace-bound-facts.md) has the facts of today
  that the rows use, and what was checked in the code.
- [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) has pinned profiles, tiers and
  membership.
- [`profile-broker-surface.md`](profile-broker-surface.md) has the rules for a grant to the
  profile role. It is merged, and its status line says "draft for review".

Words in this document:

- A team profile is an object of the new kind of row 1. It has one cluster and a list of
  namespaces.
- A team task, a team pod and a team ServiceAccount are a task, a worker pod and a
  ServiceAccount of a team profile.
- A team is the people at the tiers that a team profile lists.
- A pin is an AgentProfile with `clusterRef`. A fixed image is an image that an install sets by
  hand for one component.
- An entry is one line of the operator's table for the broker. In the identity map it is the
  line of one ServiceAccount, and a profile has one for each of its tiers.
- Releases A and B are the two releases of
  [Release order and rollback](#release-order-and-rollback). A team profile can exist from
  release B.

## Summary

- Today each layer stops at the cluster. A profile task reads each namespace of its target
  cluster with the install's identity.
- Rows 1 to 7 keep the tasks of a team profile to a fixed namespace list, at the broker. The
  broker knows the task's profile from the task credential, as rule 2 of the broker-surface
  design has it. The list itself is not in the credential. It comes from the operator's map,
  from the entry of the caller's ServiceAccount, and the broker binds the credential's profile
  name to that entry. That is option C of the broker-surface design. Only the credential form
  of row 2 makes the list a fact of the credential.
- Rule 2 is written for a grant. Here the same hold carries a bound, and a bound fails in the
  other direction. A component that does not know a grant refuses it. A component that does not
  know a bound does not apply it.
- So a team profile is a kind of its own (row 1), and each thing of it has a form of its own.
  A component that does not know the kind does not run the profile at all. Row 6 has each
  state between the components. In each state a team task has its bound or is refused.
- The bound of rows 1 to 7 is on the profile, not on the person. Today the same person can ask
  the platform agent or open a session, and neither path has a tier check.
- This design does not close those paths. A person of a team gets to the team profile through
  a session's delegate tool, as a person gets to the `cluster` profile today, and can still ask
  the platform agent, or a session, about another namespace.

What the security page can say:

- The task claim: "A task of a team profile reads objects only in the namespaces of its list,
  on its one cluster. It can read the names and the fields of each kind of the cluster, and an
  object of the team can name things outside its namespace."
- Three conditions go with the task claim. The cluster of the install enforces NetworkPolicy. A
  change of a list is complete only after the roll of row 2. Where the web console is on, its
  admission policy is on (row 7).
- One sentence goes with the task claim: "A person of a team can still ask the platform agent
  or a session about another namespace."

The guardrail line of #3009 names "the caller's authorized namespaces/repos (per-user tier
membership ...)". In this design only `allowedTiers` of today holds the caller. The team
profile refuses a task of a person at another tier, and the person's other paths stay. A hold
on the caller is for the person bound's own design.

## Decisions

| Id  | Our proposal                                                                                                                                                                                                                                                                                                                                                                                                                                        | With a no                                                                                                                                                                                                            | Where                                                                    |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 1   | The kind. A team profile is a kind of its own, with one cluster and a list of namespaces. See [The kind](#the-kind). Decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7354#issuecomment-7207672849)): `default` is a refused name.                                                                                                                                                                                | The other way: a field on the AgentProfile. Three states of row 6 are then open, or each needs a mechanism of its own; [The kind](#the-kind) has the table. A no to one line of that section removes that line.      | a new type and CRD, the operator, the card, `a2a/gateway/profiles.go`    |
| 2   | The hold. The identity map's entry of a team profile carries the profile's cluster and namespace list. See [The hold](#the-hold).                                                                                                                                                                                                                                                                                                                   | One of the two other forms: a file of its own, or the list in the signed credential. Rows 3, 6 and 7 and "Done when" 3 then change as [The hold](#the-hold) says.                                                    | `credential_proxy_profiles.go`, `task_credential.py`, the exec tier      |
| 3   | The target. The broker refuses a call of a team profile's task whose target is not the entry's cluster.                                                                                                                                                                                                                                                                                                                                             | The broker checks the namespace and not the cluster. The task claim then rests on the gateway, which sets the target.                                                                                                | `credential_proxy.py`                                                    |
| 4   | The `kubectl` rule of [The kubectl rule](#the-kubectl-rule). Decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7354#issuecomment-7207672849)): the resource of a raw path is on the kind list.                                                                                                                                                                                                                     | No bound; the design ends. A no to one line of the rule removes that line.                                                                                                                                           | a new rule beside `task_confinement.py`; rule id `caller.task-namespace` |
| 5   | Each other route. A team profile's task gets `kubectl` under row 4 and the documentation relay, from a closed list. See [Each other route](#each-other-route).                                                                                                                                                                                                                                                                                      | The other way: a list of refused routes. A later grant then reaches a team profile on the day it merges. With no list at all the task claim does not hold: `gcloud logging read` returns the logs of each namespace. | `credential_proxy.py`                                                    |
| 6   | Release order and rollback: two releases, the result of each state between the components, and the rollback rule. See [Release order and rollback](#release-order-and-rollback).                                                                                                                                                                                                                                                                    | No release carries rows 1 to 5 until this document has another order.                                                                                                                                                | the upgrade table, the rollback page, the operator, `a2a/gateway`        |
| 7   | Who can change the bound. Decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7354#issuecomment-7207672849)): an admission policy lets only named principals create or change a team profile, where the `admissionPolicy` values gate renders it. For each other object of [Who can change the bound](#who-can-change-the-bound), a principal that can write it can change the bound, and the security page says so. | The other way: no admission policy for the kind. The decision did not take it.                                                                                                                                       | a policy in the chart, the security page                                 |

Rows 1 to 7 are the task bound. The person bound was row 8 of an earlier text of this
document; it is not a row here.

### The kind

Each line is a proposal; a decided line says so.

- The kind is `TeamProfile`. It keeps these fields of an AgentProfile: `description`,
  `persona`, `harness`, `tools`, `lifecycle`, `queueTimeoutSeconds`, `concurrency`,
  `allowedTiers`, `parameters` and `resources`. It has two more fields, and each is required:
  `cluster` and `namespaces`.
- `namespaces` has 1 to 32 items. Each item is a DNS label.
- The kind has no `clusterRef`, no `yieldToPinned`, no `identity`, no `delegates`, no `bus`
  topics and no `scripts`. Each makes the profile a pin, or gives its tasks a path that row 4
  does not see.
- `allowedTiers` lists one or more interactive tiers. With `read` on the list, each person of
  the install can use the team profile.
- A team profile runs no Job task: it has no `unattended` tier, no schedule and no confirmed
  change. It is not a target of an AgentSchedule or of the MCP bridge.
- A team profile is not a pin for routing. It does not stand in for the generic `cluster`
  profile, and its tiers do not cap another profile. Two teams can have a profile on one
  cluster. A pin on the same cluster caps a team task as it caps each other profile's task;
  that guard of today stays.
- The gateway gives a team task the profile's own cluster as the target. No parameter of a
  task sets the target or a namespace: the kind refuses the parameter names `project`,
  `location` and `cluster`. `cluster` cannot change after the create.
- A team profile and an AgentProfile cannot have the same name. The operator of B refuses the
  newer one. With the same creation time it refuses the team profile.
- The kind has no finalizer. The operator removes the card of a team profile that is gone on
  its steady pass.
- Four names are refused as items (`default` by the decision of 2026-11-27): `default`,
  `kube-system`, `kube-public` and `kube-node-lease`. The operator also refuses the namespace
  of the install, which a CRD rule cannot name. This is a guard against an error, and it is
  not a bound: row 7 says who can write the list.
- A list can name a namespace that other teams use. A team task then reads each object of
  that namespace, also the objects of the other teams. The security page tells the writer to
  list only the namespaces of that one team.
- A later field that narrows a team task does not come as an optional field with an open
  default. It comes with a new kind, or with a version that the CRD does not serve in the
  older form. Or the CRD of an earlier release declares the field, and the operator of that
  release refuses an object that sets it.
- The operator of B enforces each refusal of this list, with a rule of the CRD or a status
  condition. The build names each condition.

One rule for the things of a team profile. One class of open state exists: a thing of a team
profile shares a set of names with a thing of an AgentProfile. One rule closes the class. Each
thing that can carry a team task or admit a team pod has a form that a component below B cannot
produce and does not read. So a team task gets only to a team pod, and a team pod runs only as
a team ServiceAccount. Only the operator of B renders an entry for a team ServiceAccount, and
that entry has the two keys.

Each form is outside the pattern that a component of today reads. The label key is not one of
the two keys of today, and a team pod does not carry those two. The subject root is not below
`a2a.submit.`. The directory subject is not `a2a.agents.` with one token. The key of the table
is not `profiles`. Three things stay shared, each for the reason in its row: the pool, the
callout's map and the tiers file.

| Thing                                | Its form                                                                               | Why a component below B cannot produce or read it                                                                                                                                                                                                    |
| ------------------------------------ | -------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The name of a ServiceAccount         | The prefix `agentprofile-`, and a part with a dot                                      | An operator below B makes a name with no dot, and it refuses a named ServiceAccount with that prefix. So no map from below B has an entry for that name                                                                                              |
| The label of a worker pod            | A label key of its own                                                                 | A dispatcher below B chooses a pod by the two labels of today                                                                                                                                                                                        |
| The line for the dispatcher          | A key of the table of its own                                                          | A dispatcher below B reads the table with a tolerant parse and does not read that key                                                                                                                                                                |
| The submit subject and the durable   | A subject root of its own, and a durable with a name of its own that filters that root | A dispatcher below B does not ask for that durable: it derives its durables from the lines that it reads. It also refuses a durable with another filter, and it ends a delivery off another subject. A gateway below B does not publish on that root |
| The card                             | A directory subject of its own                                                         | A gateway below B does not read it. With a card of both kinds for one name, the gateway of B takes the team card                                                                                                                                     |
| The Job template                     | None                                                                                   | A team profile runs no Job task. The gateway of B refuses a confirm for a team task                                                                                                                                                                  |
| The callout's map and the tiers file | No new key, and no new value of a key                                                  | Each has a strict reader. The callout of B knows a team entry by the name form of its ServiceAccount and gives the grants for the subject root of a team profile. A callout below B gives no such grant                                              |
| The warm pool                        | The component label and the agent's labels of today                                    | This one thing is shared on purpose: an operator below B then deletes a pool that it does not want                                                                                                                                                   |

Why a kind, and not a field on the AgentProfile:

| State                                      | How it comes                                                                                     | A kind of its own                                                                                                               | A field on the AgentProfile                                                                                                           |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| The CRDs are at B; the operator is below B | Each upgrade to B, for a short time; a failed operator step; `helm rollback`; newer CRDs by hand | No controller knows the kind. The profile does not run                                                                          | The older operator renders a plain pin and no bound, and the object still shows its list. Open                                        |
| The CRDs are below B; the operator is at B | A `helm upgrade` by hand, which applies no CRDs; older CRDs by hand                              | The older CRD sets do not have the kind. Its CRD stays, or a create fails                                                       | The API server drops the list, and the operator renders no bound. Open, unless the operator also reads the CRDs                       |
| A rollback below B                         | The two steps of the rollback page                                                               | Each team profile is down                                                                                                       | Each team profile is a plain pin with cluster-wide read. A person must delete each one first                                          |
| A gateway below B                          | A fixed image for the gateway; an old pod in a roll                                              | The gateway does not read the card                                                                                              | The gateway reads the profile as a pin: it takes each `cluster` task of that cluster                                                  |
| What it costs                              |                                                                                                  | A second kind, and a form of its own for each thing of the table above; a look for the kind on each steady pass; the list below | One optional field, and then an annotation, a create-only rule, a read of the CRDs on each pass and a third release for a missing key |

What the kind needs in the repository:

- The type, its CRD in the operator and in the chart, the operator's role for it, and its line
  in the operator's RBAC self-check.
- A start gate. The operator of today skips the controller of a kind whose CRD or role is
  absent at its start, and does not look again. The kind needs the look on the steady pass,
  one restart and a condition on the PlatformAgent.
- A second path in each table that the operator renders from the profile list: the identity
  map, the callout's map, the dispatcher's table, the tiers file, the hold file, the quota,
  the durables, the pools and their sweep, and the tier keys.
- The bus side: the card and a sweep for a card with no object, the gateway's read of the
  card, and the dispatcher's label rule, line and subject.
- The field pages, the condition texts, the uninstall page and the security page's list of
  custom resources.
- The conformance rows that name the identity builders.
- No webhook: only the PlatformAgent has one.

### The hold

The broker needs, for the caller's entry, the cluster and the namespace list. Three forms:

- In the identity map (proposed). Each entry of a team profile gets two keys: `cluster`, with
  the three keys of a target, and `namespaces`. Each entry of one team profile carries the
  same values. The entry of another profile does not change. The broker takes the two keys
  from the entry of the caller's ServiceAccount. A map is malformed, and the broker does not
  start on it, if an entry has one key and not the other, or an empty list.
- In a file of its own. A ConfigMap that no pod template hashes, mounted as a directory and read
  on each request, as the hold file is. Each team profile has an entry. A file that the broker
  cannot read or parse refuses each `/v1/exec` call of each profile task.
- In the signed credential (option B of [Options](#options-for-the-task-bound)). The dispatcher
  mints the list into a team profile's credential, and the verifiers check the key.

What each form costs:

| State                                       | The identity map                                                                                                                                                      | A file of its own                                                               | The list in the credential                                                        |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| What changes                                | The operator renders two keys; the broker and the exec tier read them. No new reader                                                                                  | A new ConfigMap, a mount, and a reader in the broker and the exec tier          | The minter in `a2a/dispatch` and both verifiers; the shape of the credential      |
| Size                                        | The map is one environment value, and Linux limits one such string to 128 KiB. The operator refuses a team profile that takes the map above a limit, with a condition | A ConfigMap, 1 MiB                                                              | The list is in each credential of the profile                                     |
| A change of a list                          | The broker restarts, and the exec tier rolls. See below                                                                                                               | No restart. Each pod has the new list after the kubelet's delay                 | No restart. The next task has the new list; a task that runs keeps its list       |
| A broker below A, and a team profile exists | The broker does not start, and it is down until it is at A. Closed. The old pods of an exec tier still serve the other profiles                                       | The broker does not read the file and runs the team profile with no bound. Open | The verifier refuses the team profile's credential. Closed, for that profile only |
| An operator or a minter below B             | The operator does not know the kind and renders no entry. A dispatcher below B has no line for a team profile. The team profile does not run                          | The same                                                                        | An older minter mints no list, and the broker runs the task with no bound. Open   |
| A rollback below release B                  | The team profile does not run                                                                                                                                         | The same                                                                        | The same                                                                          |

A change of a list in the identity map:

- The broker restarts with the `Recreate` strategy. Its routes are down for that time.
- With no exec tier, or with one replica, that is the whole change.
- An exec tier with 2 or more replicas rolls one pod at a time and stays up. Until the last pod
  rolls, a pod with the old map can answer a call. If a list got shorter, that pod still
  admits the removed namespace.
- A new team profile is refused on such a pod: its ServiceAccounts are not in the old map. One
  with the name of a deleted team profile is the same case as a changed list.
- An idle pod exits in about 3 seconds. A pod in a command has a grace period of 380 seconds.
  So the window is short as a rule. Its upper limit is the replica count times that period,
  plus the start of each new pod: about 3 hours 23 minutes for 32 replicas. A roll that stalls
  has no limit.
- We propose to accept the window and to show it, with a condition `BoundApplied` on the team
  profile. The controller of the kind writes it.
- The condition is True only when three things hold. The entry in the pod template of the
  broker's Deployment and of the exec tier's Deployment has the profile's list. Each roll is
  complete for that template. No pod of an older template is left, also no pod that
  terminates.
- The condition names the profile's generation and the operator's version. A stopped operator
  does not update it; it is then the last word of that operator.

Proposed: the identity map. It adds no reader, a team's list changes seldom, and it fails
closed against an older broker. The cost of that closed state is a stop of the broker, on an
install with a team profile.

Rows 3 to 7 are written for the identity map. With another form:

- A file of its own. Row 3 uses the cluster of the file's entry. In row 6 the state "a broker
  below A" is open. So the operator must render a team profile only while the broker's image is
  at release A or later. Row 7 gets the ConfigMap as one more object. "Done when" 3 holds the
  file's refusals.
- The list in the credential. No entry exists. Row 3 is a check at the minter: the target of a
  team profile's credential is the profile's cluster. Row 6 gets one more step: both verifiers
  know the key one release before the minter mints it. Row 7 names the minter's Deployment and
  its signing key in place of the identity map. "Done when" 3 holds the refusal of a team
  profile's credential by an older verifier.

The command policy module cannot read a file. So the rule of row 4 takes the list as an
argument and lives beside the confinement code. It runs in the broker and in the exec tier.

### The kubectl rule

For a call of a team profile's task, after today's checks. The rule is a closed list.

| Part of the call          | Rule                                                                                                                                                                                                                                                  |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Verbs that read objects   | `get`, `describe`, `logs`, `events`, `top pod` (also as `top pods` and `top po`), `rollout history`. Each needs the namespace of the next line                                                                                                        |
| The namespace             | Exactly one `--namespace` flag, in its long or short form, with a value of the namespace list. No flag, two flags or an empty value is a refusal                                                                                                      |
| `--all-namespaces`        | Refused in each form, also with `=false`                                                                                                                                                                                                              |
| The kinds                 | Each kind word is on the kind list: a closed list of built-in namespaced kinds in the code, each with its plural, short and group forms. Another word is a refusal                                                                                    |
| `--raw`                   | The decoded path, without the query, is `<prefix>/namespaces/<name>/<resource>`; `<name>` is on the namespace list, and `<resource>` is on the kind list. The namespace line does not apply; the path has the name                                    |
| Verbs that read no object | `explain`, `api-resources`, `api-versions`, `version`, and the local reads `--help`, `config get-contexts` and `config current-context`. No namespace rule and no kind rule                                                                           |
| Each other verb           | Refused. Some are read verbs of the policy: `top node`, `cluster-info`, `auth can-i`, `auth whoami`. A profile is refused `wait` and `rollout status` today, and that stays. A read verb that a later change adds is refused until it has a line here |
| Words after `--`          | The same rules                                                                                                                                                                                                                                        |
| Secrets                   | Today's rule stays                                                                                                                                                                                                                                    |

The kind words of a call:

| Verb                                 | Kind words                                                                                   |
| ------------------------------------ | -------------------------------------------------------------------------------------------- |
| `get`, `describe`, `rollout history` | Each comma part of the first resource word, and the part before `/` of each `kind/name` word |
| `logs`                               | The part before `/` of a `kind/name` word. A bare word is a pod's name                       |
| `top pod`                            | None. The words after it are names                                                           |
| `events`                             | The part before `/` of the `--for` value                                                     |

A refusal names the namespaces that the task can read, so that the model can correct the call.

Limits of the rule:

- The kind list holds built-in kinds only. A custom kind needs a row of its own: its scope is
  set on each cluster, and the broker reads no API discovery.
- A raw path names its API group in `<prefix>`, and the rule does not check `<prefix>`. A
  custom kind with the plural of a built-in kind passes the kind list on a raw path. Its
  namespace is still on the namespace list. Open for the approver: hold `<prefix>` to the API
  path of the built-in kind, or accept this limit.
- `kubectl get all` is refused: `all` is a category, and it is not a kind on the list.
- `api-resources` and `explain` show the names and the fields of each kind of the cluster, also
  the custom ones. They show no object.
- ConfigMaps of the team's namespaces are readable, with what they hold.
- An object of the team can name things outside the namespace, such as a pod's node. The rule
  reads the call, not the answer.
- A namespace's name can go to another team later. The profile's list is then wrong until a
  person changes it.

### Each other route

A grant to the profile role goes to each profile; that is rule 1 of the broker-surface design.
That design plans more grants. One is the Logging read of the Cloud API relay for "Each
profile", which returns the container logs of each namespace. So a team profile needs a default
that the broker holds:

- The broker has a closed list of broker routes for a team profile: `/v1/exec` with `kubectl`
  under row 4, and the documentation relay, which reads public documents only. A broker route
  that is not on the list is a refusal.
- So `gcloud`, each version-control verb and the audit-report store are refused. `logging read`
  and the serial console read data of each namespace, and the version-control reads reach each
  managed repository.
- A later grant to the profile role does not reach a team profile until a design gives it a
  row here. Its conformance row must see the refusal for a team profile. The pull-request part
  of #3009 is such a row.
- A profile pod reaches only DNS, the bus, LiteLLM and the broker. So the broker's routes are
  its only path to a cluster. That rests on a network plugin that enforces NetworkPolicy.

### Release order and rollback

The bound comes in two releases:

1. Release A. The broker and the exec tier accept exactly two key sets in an entry: `profile`
   and `tier`, or those two with `cluster` and `namespaces`. Each other key, and each value
   that is not of the stated form, stops the start, as today. They apply rows 3, 4 and 5 to a
   call whose entry has the second set.
2. Release B. The kind, its CRD and the admission policy of row 7. The operator renders a team
   profile: its ServiceAccounts, its pool, its card, its line for the dispatcher and its
   entries. The gateway and the dispatcher know the forms of [The kind](#the-kind).

Safety does not rest on this order. The operator of B is the only component that renders the
two keys, and it renders them only for a team profile. The order keeps the broker up: with the
broker below A, the first team profile stops the broker. We propose to accept that stop: it
is closed, and only an install with a team profile and a broker below A has it. An install with
no team profile has the map of today, in each release.

What each state does, for the identity map of row 2:

| State                                                                                 | How it comes                                                                                                                                                                             | What each component does                                                                                                                                                                                                                                                                                                         | Result                                                                                                                                                      |
| ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The broker is below A, and a team profile exists                                      | An upgrade to B on an install below A, between its two steps; a harness-only rollback below A; a fixed image for the broker                                                              | The map gets the two keys, the broker restarts, and it does not start on that map. A new exec pod fails in the same way. An old exec pod has no entry for a team ServiceAccount and refuses it                                                                                                                                   | Closed. The broker is down until it is at A or later. An exec tier with 2 or more replicas stalls its roll, and its old pods still serve the other profiles |
| The CRDs are at B or later; the operator is below B                                   | Each upgrade to B, between the CRD apply and the operator's roll; a failed operator step; `helm rollback`, also the one that `upgrade.sh` runs for a pending release; newer CRDs by hand | The API server admits a team profile, and no controller knows the kind. A new team profile gets no ServiceAccount, no card and no entry. For one that ran before, the older operator renders the map with no entry of it, and the broker refuses a team ServiceAccount                                                           | Closed: the team profile does not run. Its status is the last word of the operator of B                                                                     |
| No operator runs                                                                      | A stopped operator of B                                                                                                                                                                  | The map keeps the entries of each team profile, with the two keys                                                                                                                                                                                                                                                                | The bound holds. The status is not updated                                                                                                                  |
| The kind's CRD or the operator's role for it is absent; the operator is at B or later | A `helm upgrade` by hand, which applies no CRDs; an operator image ahead of its ClusterRole                                                                                              | The operator runs with no controller of the kind and says so in a condition on the PlatformAgent. That condition does not change the readiness of an install with no team profile. The operator looks for the CRD and for its role on its steady pass, and restarts one time when both are there                                 | Closed: no team profile runs until that restart                                                                                                             |
| The kind's CRD is deleted                                                             | A delete by hand                                                                                                                                                                         | The API server deletes each team profile. What the operator's cache holds after the delete is not checked. If the cache keeps the objects, the entries stay and the bound holds                                                                                                                                                  | Closed                                                                                                                                                      |
| The gateway is below B; the operator is at B or later                                 | A fixed image for the gateway; an operator reference with a digest and no tag, or no reference at all; an old pod in a roll, with no time limit while the roll stalls                    | The gateway does not read the card of a team profile and cannot address it                                                                                                                                                                                                                                                       | Closed: the team profile does not work through that gateway                                                                                                 |
| The dispatcher is below B; the operator is at B or later                              | A fixed image for the dispatcher                                                                                                                                                         | The dispatcher has no durable on the subject root of a team profile and does not read its key of the table. It starts no team task, also with a line and a pod that are left of an AgentProfile with that name                                                                                                                   | Closed: the team profile does not run                                                                                                                       |
| The gateway or the dispatcher is at B; the operator is below B                        | A fixed image; a gateway that follows the agent image after only the operator step of a rollback                                                                                         | The gateway of B reads a card that is left. No line and no entry exist, so the task does not start                                                                                                                                                                                                                               | Closed                                                                                                                                                      |
| The verifier is below B                                                               | A fixed image; an old pod in a roll                                                                                                                                                      | It works as today. The operator of B adds no key and no new value of a key to the tiers file. The broker applies the entry                                                                                                                                                                                                       | Closed: the bound holds                                                                                                                                     |
| The auth callout is below B                                                           | A fixed image; an old pod in a roll                                                                                                                                                      | The map of B has no new key and no new value of a key, so the callout reads it and serves the other profiles. It gives no grant for the subject root of a team profile, so the dispatcher gets no team task                                                                                                                      | Closed: the team profile does not run                                                                                                                       |
| The operator moves back below B                                                       | The first step of a rollback                                                                                                                                                             | As in the second state. The older operator deletes the pool. The card and the ServiceAccounts stay; no component below B reads that card, and no map from below B names those ServiceAccounts                                                                                                                                    | Closed: no team profile runs                                                                                                                                |
| A team profile takes the name of an AgentProfile                                      | A move of a pin to a team profile with the same name; an AgentProfile that a person made while the operator was below B, also one that names a ServiceAccount                            | A pod and an entry of the AgentProfile can be left. No team task gets to that pod: the subject, the line and the label of a team profile are of their own. An operator below B makes no name with a dot, and it refuses a named ServiceAccount with the prefix `agentprofile-`. So it renders no entry for a team ServiceAccount | Closed                                                                                                                                                      |
| A team profile takes the name of a deleted team profile                               | A delete and a create                                                                                                                                                                    | An old exec pod has the entry of the deleted one. With another cluster, that pod refuses the call by row 3. With the same cluster, it applies the list of the deleted one                                                                                                                                                        | The window of a changed list (row 2)                                                                                                                        |
| An entry has one key and not the other, or an empty list                              | A defect of the operator                                                                                                                                                                 | The map is malformed, and the broker does not start                                                                                                                                                                                                                                                                              | Closed                                                                                                                                                      |

The rollback rule:

- A rollback below release B takes each team profile down. It does not open the task bound, in
  the identity map form of row 2. No step before the rollback is necessary for the task claim.
- `kubectl get teamprofiles -A` lists each team profile, before and after the rollback. The
  objects stay, and the next upgrade to B runs them again as they are. A team profile that the
  chart of B renders is different: a move back to an older chart deletes it.
- A chart below B has no admission policy of row 7. A write to a team profile in that time is
  not held, and the next upgrade to B runs the list as it is then.
- Two things are left after the operator moved back: the card and the ServiceAccounts. They
  stay until the next upgrade to B, or until a person deletes them. The older operator deletes
  the pool.
- The rollback page and the upgrade table get a row with these sentences.

### Who can change the bound

| Object                                                                        | What it changes                                                                                               | Who can write it today                                                                                                     | Admission control today                                                                                                                                |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The team profile                                                              | The list; a delete and a create change the cluster                                                            | New kind. For AgentProfiles today: the operator's ServiceAccount has `patch`, and the chart's pre-delete hook has `delete` | The rules of the CRD. No admission policy names AgentProfiles today                                                                                    |
| The admission policy of the kind, its binding and the `admissionPolicy` value | Who can create or change a team profile                                                                       | A cluster admin, and Helm                                                                                                  | None                                                                                                                                                   |
| The CRD of the kind                                                           | The rules of each field. It is cluster-scoped. A delete of it deletes each team profile                       | A cluster admin, and the install and upgrade scripts                                                                       | None                                                                                                                                                   |
| The PlatformAgent                                                             | `spec.deployment.image` chooses the broker's image                                                            | The operator's ServiceAccount. The ServiceAccount of the chart's web console has `get` and `patch`                         | A validating webhook, where the install turns it on. A policy holds the web console to three keys of `spec.integration`, only where the policy renders |
| The operator's Deployment                                                     | The image values for the broker, the gateway and the dispatcher                                               | Helm, and a principal with write on Deployments                                                                            | None                                                                                                                                                   |
| The broker's Deployment and the exec tier's                                   | The map that each pod applies. The map is an environment value, and `spec.deployment.env` cannot set its name | The operator's ServiceAccount                                                                                              | None. The operator renders them again                                                                                                                  |
| The profile namespace                                                         | The pods, the ServiceAccounts, and the NetworkPolicy that row 5 rests on                                      | The operator's Role there. The dispatcher's Role creates Jobs there                                                        | A policy holds a Job of the dispatcher to the operator's template, only where the `admissionPolicy` gate renders it                                    |

- A principal with the Kubernetes role to write one of these objects can change or open the
  bound. The team profile is the exception, where the policy of the next line renders. The
  agent's own roles have no write on these objects.
- For the team profile, a ValidatingAdmissionPolicy lets only named principals create or
  change one, where the `admissionPolicy` values gate renders it (decided on 2026-11-27). The
  chart has that pattern for another kind. Its cost is that gate, Kubernetes 1.30 or later,
  and a list of principals for each install. Where the policy does not render, each writer of
  the kind can change the list.
- Two points of that policy are open for the approver: what an empty list of principals does,
  and if the list must name the principal that applies the chart, because the chart of B can
  render a team profile. The rule of this document is no open default.
- That policy does not cover the other objects of the table, and the security page says so.
- The task claim needs the policy of the chart's web console where that console is on: its
  `patch` reaches the image field. The policy of the web console needs the `admissionPolicy`
  values gate and Kubernetes 1.30 or later.

## Options for the task bound

| Option                                                                   | What it needs                                                                       | What the facts say                                                                                                                         |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| A. A namespace list on the profile, held by the profile's name           | A kind for a team profile, an entry for the broker, the broker rules of rows 3 to 5 | The broker has the verified profile name on each call. The task credential does not change. An older broker does not know the rule (row 6) |
| B. The namespace list in the signed credential, minted from the profile  | A new key in the minter and in both verifiers                                       | No value comes from the model. An older verifier refuses the new key. It changes the credential's shape, with one more release step        |
| C. One namespace in the signed target, from a task parameter             | A new target shape and a rule for who chooses the namespace                         | The model in a session can set a task parameter                                                                                            |
| D. A Kubernetes identity for each profile, with a namespaced RoleBinding | The broker runs each call as that identity                                          | The broker runs each call as the install's identity. The command policy says that impersonation "is planned, not deployed"                 |
| E. Rules in the persona's text                                           | Nothing                                                                             | The security page says that persona rules "are guidance, not enforcement"                                                                  |

Proposed: A. B is the other form that holds; row 2 has both.

## What stays out

- The built-in `devteam` profile and its persona. They are the next change of #3009, with its
  bench case.
- The pull-request part of #3009: the confirm from chat (#4429) and a repository bound for a
  proposal (#4428).
- Group claims for a team (#1999). #7355, a follow-up of #3009 below the epic, tracks them.
- A console for a team's people. The users of the A2A console share one principal (#5557), so
  it stays at `read`. This design does not serve the "Web Console" part of #3009.
- A team's work on more than one cluster in one profile.
- Custom kinds for a team profile.
- Logs of the team from Cloud Logging. `kubectl logs` with `--previous` is the read that stays.
- A bound on a session pod or on the platform agent.
- The person bound: a hold on what a person of a team can start. It gets a design of its own.

## Done when

For the guardrail part of #3009:

1. The security approver approved this document.
2. A team task is refused outside its namespace list and outside its cluster. Unit tests cover
   each line of [The kubectl rule](#the-kubectl-rule) (also the resource of a raw path), the
   text of a refusal, the target refusal of row 3, each refused route of
   [Each other route](#each-other-route) and a route that is not on the list.
3. A conformance row and a mutation cover each of those refusals. One more covers that a map
   with one key and not the other does not start the broker. That row needs the module that
   parses the identity map, and the suite does not import it today. That is open question 11
   of the broker-surface design. If that question gets a no, the parse of the two keys moves
   to a module that the suite imports.
4. The CRD and the operator refuse each case of [The kind](#the-kind), with a test for each;
   one covers the size refusal of the map, and one the sweep of a card. One test covers that
   each form of the table in that section is outside the pattern of today. A team profile does
   not take a task of the generic `cluster` profile and does not cap another profile. The
   tests have one and two team profiles on one cluster. A test of the chart covers that the
   admission policy of row 7 renders with the gate, and a test against an API server covers
   that it refuses a principal that is not named.
5. One test for each state of row 6, by its name. A test of a component below A or B runs from
   the commit alone: the map, the card, the callout's map and the dispatcher's table as B
   renders them are fixtures in the commit. The tests against an operator, a broker, a
   dispatcher, a gateway, a verifier and a callout of today land in release A, against the
   code of A.
6. Those tests include these. A broker of today does not start on the map of B. An operator of
   today renders no entry for a ServiceAccount with the team name form, also for an
   AgentProfile that names it. A dispatcher of today starts no team task, also with a line and
   a pod that are left of an AgentProfile with that name. A dispatcher of today takes the table
   of B and serves the other profiles. The operator of B restarts one time
   when the CRD and its role are there. A test covers each line of the condition
   `BoundApplied`.
7. One check on an install, by hand: a team task reads its namespace and is refused in
   another. After a move of the operator below B and back, the team profile runs again.
8. The build checks four things that this design did not check. They are the API requests of
   `kubectl describe` for each kind on the kind list; what a profile worker's key reads on
   LiteLLM's own endpoints; what the operator's cache holds after a delete of the kind's CRD;
   and the size limit of an environment value on a running pod.
9. The documents follow the answers. The security page has the task claim, its conditions,
   who can change the bound, and what a list exposes when it names a namespace that other
   teams use. The upgrade table and the rollback page have the rows of row 6.
   The broker-surface design names the team profile in its rule 1 and in each "Each profile"
   cell.
