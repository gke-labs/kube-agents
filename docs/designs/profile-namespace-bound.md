# A namespace bound for a team's profile

A proposal for the security approver; nothing in it is built. This is the design for the
guardrail of #3009, a part of the epic #2900. It says how the tasks of one profile are held to
the Kubernetes namespaces of one team. With row 8, it also says what more is necessary before
the install can make a claim about a person of that team.

Each row of [Decisions](#decisions) is our proposal, with its other way beside it. After the
security approval we build the proposal of each row: rows 1 to 7 first, then row 8 as a change
of its own. A row that goes the other way changes this document first.

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
- With row 8: a team profile can have a people hold, and its tier is then a held tier. Release
  R is the release that builds row 8; it can be B or a later release.
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
- With row 8: those paths are closed for the listed people of a team, under the conditions of
  [The person bound](#the-person-bound).

What the security page can say:

- With rows 1 to 7, the task claim: "A task of a team profile reads objects only in the
  namespaces of its list, on its one cluster. It can read the names and the fields of each kind
  of the cluster, and an object of the team can name things outside its namespace."
- Three conditions go with the task claim. The cluster of the install enforces NetworkPolicy. A
  change of a list is complete only after the roll of row 2. Where the web console is on, its
  admission policy is on (row 7).
- With row 8: the person claim, under the conditions of
  [The person bound](#the-person-bound). "A person who is listed at a held tier starts only
  tasks of that tier's team profiles, and the install gives that person no fleet dashboard.
  The person still reads what the agent posts in a channel that the person is in. In the home
  channel that includes the alerts, the findings and the audit reports of the fleet. The claim
  does not cover a person with `pods/portforward` or `pods/exec` in the install's namespace,
  with a door's token, or on Teams."
- With a no to row 8: the task claim, and "A person of a team can still ask the platform agent
  or a session about another namespace."

The guardrail line of #3009 names "the caller's authorized namespaces/repos (per-user tier
membership ...)". With row 8, the caller is held through the membership of a held tier, under
the conditions of [The person bound](#the-person-bound). With a no, only `allowedTiers` of
today holds the caller. The team profile refuses a task of a person at another tier, and the
person's other paths stay.

## Decisions

| Id  | Our proposal                                                                                                                                                                                                                                                                                                           | With a no                                                                                                                                                                                                            | Where                                                                    |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 1   | The kind. A team profile is a kind of its own, with one cluster and a list of namespaces. See [The kind](#the-kind).                                                                                                                                                                                                   | The other way: a field on the AgentProfile. Three states of row 6 are then open, or each needs a mechanism of its own; [The kind](#the-kind) has the table. A no to one line of that section removes that line.      | a new type and CRD, the operator, the card, `a2a/gateway/profiles.go`    |
| 2   | The hold. The identity map's entry of a team profile carries the profile's cluster and namespace list. See [The hold](#the-hold).                                                                                                                                                                                      | One of the two other forms: a file of its own, or the list in the signed credential. Rows 3, 6 and 7 and "Done when" 3 then change as [The hold](#the-hold) says.                                                    | `credential_proxy_profiles.go`, `task_credential.py`, the exec tier      |
| 3   | The target. The broker refuses a call of a team profile's task whose target is not the entry's cluster.                                                                                                                                                                                                                | The broker checks the namespace and not the cluster. The task claim then rests on the gateway, which sets the target.                                                                                                | `credential_proxy.py`                                                    |
| 4   | The `kubectl` rule of [The kubectl rule](#the-kubectl-rule).                                                                                                                                                                                                                                                           | No bound; the design ends. A no to one line of the rule removes that line.                                                                                                                                           | a new rule beside `task_confinement.py`; rule id `caller.task-namespace` |
| 5   | Each other route. A team profile's task gets `kubectl` under row 4 and the documentation relay, from a closed list. See [Each other route](#each-other-route).                                                                                                                                                         | The other way: a list of refused routes. A later grant then reaches a team profile on the day it merges. With no list at all the task claim does not hold: `gcloud logging read` returns the logs of each namespace. | `credential_proxy.py`                                                    |
| 6   | Release order and rollback: two releases, the result of each state between the components, and the rollback rule. See [Release order and rollback](#release-order-and-rollback).                                                                                                                                       | No release carries rows 1 to 5 until this document has another order.                                                                                                                                                | the upgrade table, the rollback page, the operator, `a2a/gateway`        |
| 7   | Who can change the bound. A principal that can write one of the objects of [Who can change the bound](#who-can-change-the-bound) can change it. Accept that, and say it on the security page.                                                                                                                          | The other way: an admission policy lets only named principals create or change a team profile. It needs Kubernetes 1.30 and the `admissionPolicy` values gate. The other objects of the table stay as today.         | the security page                                                        |
| 8   | The person. With row 8, a team profile can have a people hold: its listed people start only tasks of the team profiles of their tier, also from chat, under the conditions of [The person bound](#the-person-bound). We recommend a yes: the guardrail line of #3009 names the caller, and rows 1 to 7 hold no person. | Row 8 is out of this design. The install makes the task claim only, and a person of a team reaches the team profile through a session, with each other path of today.                                                | the new type, `a2a/gateway`, `a2a/lib/chattext`                          |

Rows 1 to 7 are the task bound. Row 8 makes the scope of this design wider than the profile.
Outside [The person bound](#the-person-bound), each sentence that depends on row 8 starts with
"With row 8", or is in a list below a line that starts so.

### The kind

Each line is a proposal.

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
- Three names are refused as items: `kube-system`, `kube-public` and `kube-node-lease`. The
  operator also refuses the namespace of the install, which a CRD rule cannot name. This is a
  guard against an error, and it is not a bound: row 7 trusts the writer of the list. The
  other way is no refused name.
- A later field that narrows a team task does not come as an optional field with an open
  default. It comes with a new kind, or with a version that the CRD does not serve in the
  older form. Or the CRD of an earlier release declares the field, and the operator of that
  release refuses an object that sets it. With row 8: the same rule applies to a field that
  narrows a person.
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
| `--raw`                   | The decoded path, without the query, is `<prefix>/namespaces/<name>/<resource>`, and `<name>` is on the namespace list. The namespace line does not apply; the path has the name                                                                      |
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
2. Release B. The kind and its CRD. The operator renders a team profile: its ServiceAccounts,
   its pool, its card, its line for the dispatcher and its entries. The gateway and the
   dispatcher know the forms of [The kind](#the-kind).

With row 8: release R builds row 8. The CRD of release B declares the field of the people
hold. An operator below R refuses a team profile that sets it. So no older CRD of the kind
drops the field. With row 8: R is the first release whose previous release has the check of
the rollback rule. So R can be B only if the release before B has that check and the name of
the field.

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
| The kind's CRD or the operator's role for it is absent; the operator is at B or later | A `helm upgrade` by hand, which applies no CRDs; an operator image ahead of its ClusterRole                                                                                              | The operator runs with no controller of the kind and says so in a condition on the PlatformAgent. That condition does not change the readiness of an install with no team profile. The operator looks for the CRD and for its role on its steady pass, and restarts one time when both are there                                 | Closed: no team profile runs until that restart. With row 8: open for the person, as after a rollback                                                       |
| The kind's CRD is deleted                                                             | A delete by hand                                                                                                                                                                         | The API server deletes each team profile. What the operator's cache holds after the delete is not checked. If the cache keeps the objects, the entries stay and the bound holds                                                                                                                                                  | Closed for the tasks. With row 8: each people hold ends when the operator renders the gateway with no team profile                                          |
| The gateway is below B; the operator is at B or later                                 | A fixed image for the gateway; an operator reference with a digest and no tag, or no reference at all; an old pod in a roll, with no time limit while the roll stalls                    | The gateway does not read the card of a team profile and cannot address it                                                                                                                                                                                                                                                       | Closed: the team profile does not work through that gateway                                                                                                 |
| The dispatcher is below B; the operator is at B or later                              | A fixed image for the dispatcher                                                                                                                                                         | The dispatcher has no durable on the subject root of a team profile and does not read its key of the table. It starts no team task, also with a line and a pod that are left of an AgentProfile with that name                                                                                                                   | Closed: the team profile does not run                                                                                                                       |
| The gateway or the dispatcher is at B; the operator is below B                        | A fixed image; a gateway that follows the agent image after only the operator step of a rollback. With row 8: the same for a gateway at R with an operator below R                       | The gateway of B reads a card that is left. No line and no entry exist, so the task does not start                                                                                                                                                                                                                               | Closed for the tasks. With row 8: that gateway gets no people hold, so it is open for the person                                                            |
| The verifier is below B                                                               | A fixed image; an old pod in a roll                                                                                                                                                      | It works as today. The operator of B adds no key and no new value of a key to the tiers file. The broker applies the entry                                                                                                                                                                                                       | Closed: the bound holds                                                                                                                                     |
| The auth callout is below B                                                           | A fixed image; an old pod in a roll                                                                                                                                                      | The map of B has no new key and no new value of a key, so the callout reads it and serves the other profiles. It gives no grant for the subject root of a team profile, so the dispatcher gets no team task                                                                                                                      | Closed: the team profile does not run                                                                                                                       |
| With row 8: the gateway is below R, and a team profile has a people hold              | The cases of the gateway row                                                                                                                                                             | The gateway does not know the people hold and refuses nothing                                                                                                                                                                                                                                                                    | Open for the person: each path of today. The condition `PeopleHeld` of [The person bound](#the-person-bound) is False                                       |
| The operator moves back below B                                                       | The first step of a rollback                                                                                                                                                             | As in the second state. The older operator deletes the pool. The card and the ServiceAccounts stay; no component below B reads that card, and no map from below B names those ServiceAccounts                                                                                                                                    | Closed for the tasks: no team profile runs. With row 8: open for the person, also for an operator that moves back below R; see the rollback rule            |
| A team profile takes the name of an AgentProfile                                      | A move of a pin to a team profile with the same name; an AgentProfile that a person made while the operator was below B, also one that names a ServiceAccount                            | A pod and an entry of the AgentProfile can be left. No team task gets to that pod: the subject, the line and the label of a team profile are of their own. An operator below B makes no name with a dot, and it refuses a named ServiceAccount with the prefix `agentprofile-`. So it renders no entry for a team ServiceAccount | Closed                                                                                                                                                      |
| A team profile takes the name of a deleted team profile                               | A delete and a create                                                                                                                                                                    | An old exec pod has the entry of the deleted one. With another cluster, that pod refuses the call by row 3. With the same cluster, it applies the list of the deleted one                                                                                                                                                        | The window of a changed list (row 2)                                                                                                                        |
| An entry has one key and not the other, or an empty list                              | A defect of the operator                                                                                                                                                                 | The map is malformed, and the broker does not start                                                                                                                                                                                                                                                                              | Closed                                                                                                                                                      |

The rollback rule:

- A rollback below release B takes each team profile down. It does not open the task bound, in
  the identity map form of row 2. No step before the rollback is necessary for the task claim.
- `kubectl get teamprofiles -A` lists each team profile, before and after the rollback. The
  objects stay, and the next upgrade to B runs them again as they are. A team profile that the
  chart of B renders is different: a move back to an older chart deletes it.
- Two things are left after the operator moved back: the card and the ServiceAccounts. They
  stay until the next upgrade to B, or until a person deletes them. The older operator deletes
  the pool.
- The rollback page and the upgrade table get a row with these sentences.

With row 8, the rollback rule has these lines also:

- After a rollback below release R the people of a held tier keep their membership. The older
  gateway gives them the platform agent and a session again.
- The step before that rollback is to remove those people from the ingress allowlists, if they
  must stay out. With an allowlist that is open to all, that step is not possible for one
  person.
- The `upgrade.sh` of the release before R gets a check, and each later script keeps it. Its
  operator mode and its full mode refuse a move back while a team profile has a people hold,
  until a flag says to go on. The check does not run for `helm rollback`, for a
  `helm upgrade` by hand, or with the script of an older release.
- The condition `PeopleHeld` counts only while the operator that runs is at R or later and the
  condition names that operator's version.
- The tier page gets the sentence for a held tier, beside its sentence for membership.

### Who can change the bound

| Object                                      | What it changes                                                                                                    | Who can write it today                                                                                                     | Admission control today                                                                                                                                |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The team profile                            | The list; a delete and a create change the cluster. With row 8: the people hold, which a delete ends               | New kind. For AgentProfiles today: the operator's ServiceAccount has `patch`, and the chart's pre-delete hook has `delete` | The rules of the CRD. No admission policy names AgentProfiles today                                                                                    |
| The CRD of the kind                         | The rules of each field. It is cluster-scoped. A delete of it deletes each team profile                            | A cluster admin, and the install and upgrade scripts                                                                       | None                                                                                                                                                   |
| The PlatformAgent                           | `spec.deployment.image` chooses the broker's image. With row 8: `spec.tiers.membership` and the ingress allowlists | The operator's ServiceAccount. The ServiceAccount of the chart's web console has `get` and `patch`                         | A validating webhook, where the install turns it on. A policy holds the web console to three keys of `spec.integration`, only where the policy renders |
| The operator's Deployment                   | The image values for the broker, the gateway and the dispatcher                                                    | Helm, and a principal with write on Deployments                                                                            | None                                                                                                                                                   |
| The broker's Deployment and the exec tier's | The map that each pod applies. The map is an environment value, and `spec.deployment.env` cannot set its name      | The operator's ServiceAccount                                                                                              | None. The operator renders them again                                                                                                                  |
| The profile namespace                       | The pods, the ServiceAccounts, and the NetworkPolicy that row 5 rests on                                           | The operator's Role there. The dispatcher's Role creates Jobs there                                                        | A policy holds a Job of the dispatcher to the operator's template, only where the `admissionPolicy` gate renders it                                    |
| With row 8: the gateway's Deployment        | The member list and the people hold that the gateway applies                                                       | The operator's ServiceAccount                                                                                              | None. The operator renders it again                                                                                                                    |

- A principal with the Kubernetes role to write one of these objects can change or open the
  bound. The agent's own roles have no write on them.
- We propose to accept that and to say it on the security page.
- The task claim needs the policy of the chart's web console where that console is on: its
  `patch` reaches the image field. The policy needs the `admissionPolicy` values gate and
  Kubernetes 1.30 or later.
- The other way is a ValidatingAdmissionPolicy that lets only named principals create or change
  a team profile. The chart has that pattern for another kind. Its cost is the same gate and
  Kubernetes version, and a list of principals for each install. It does not cover the other
  objects of the table.

### The person bound

Rows 1 to 7 make the task claim true. They do not make a claim about a person. These are the
paths of one person that we found, and what row 8 does with each for a person of a team:

| Path                                                                                                                                                     | What it reads or does                                                                                                                         | Gate today                                                                                                 | With row 8                                                                                                                    |
| -------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| A plain chat turn. The default route is `platform`; an install can set `session`                                                                         | The platform agent reads across the fleet                                                                                                     | The ingress allowlist                                                                                      | Goes to the team profile                                                                                                      |
| The "ask about this" message shortcut, a quoted ask, a reply under a report of the fleet                                                                 | Each is a turn. The reply carries the report, which the person already reads in that channel                                                  | The ingress allowlist                                                                                      | As a plain chat turn                                                                                                          |
| A click on a choice button, or a typed answer to an open question                                                                                        | A turn. It also marks the question as answered for each other person                                                                          | The ingress allowlist                                                                                      | Refused with one notice, if the question is not of the tier's team profiles. The card stays open                              |
| `/session` or `delegate ...`                                                                                                                             | With the session cluster view, a session's `kubectl` reads through kube-proxy with a cluster-wide read identity                               | The ingress allowlist and the session cap                                                                  | Refused                                                                                                                       |
| A session delegates to `platform`, or to a `read` profile such as `cluster`                                                                              | The fleet, or each namespace of the cluster that the model names                                                                              | The people list of `platform`; the authors' tier on the card                                               | No turn and no wake reaches a session. A session from before the people hold gets no more of them                             |
| A steer, a status question or a typed `stop` in a thread with another person's task                                                                      | What that task reads; its status; the end of the task                                                                                         | The ingress allowlist. A profile task refuses a steer from another tier                                    | Refused, but for a task of the tier's team profiles                                                                           |
| The Slack Home tab                                                                                                                                       | A fleet dashboard: the worst open finding of each cluster, the cards, the pull requests, the last audit                                       | The ingress allowlist                                                                                      | One line in place of the dashboard, at the next open                                                                          |
| The channel picker of that tab, a typed `/sethome`, and the home learn from a mention. The picker and the learn exist only while the home learn is armed | Moves the channel where the alerts and the reports of the fleet post, or shows which channel it is                                            | The ingress allowlist; for the picker also "not a guest"                                                   | Refused; no learn                                                                                                             |
| A Retry or Drop tap on a board row, and a tap on the autonomy dial of the Home tab                                                                       | Acts on another person's cards, or on the dial; reads nothing                                                                                 | On Slack the Home tab's rule; on Google Chat the ingress allowlist. The dial has an admin check also       | Refused                                                                                                                       |
| A turn on the A2A door with a Google or Entra sign-in                                                                                                    | The platform agent or a session; the door sets no profile                                                                                     | The door's sign-in and the door's allowlist                                                                | Refused. A cancel of the person's own task stays                                                                              |
| Work that the person started before the people hold: a scheduled job, an open card, a task that runs                                                     | Reports of the fleet in the person's conversation; a delegation or a wake of a session                                                        | None                                                                                                       | The gateway drops such a post, with a log line and no second post in the home channel. It refuses the delegation and the wake |
| `/feedback`, a Stop button on a task of the person, a rating tap                                                                                         | Usage counts of the install; the end of that task; a rating that the tap writes                                                               | The ingress allowlist; Stop is for the requesters of the task                                              | Stay                                                                                                                          |
| Teams, the A2A console, the MCP server, the inject door, the shared-token class of the A2A door                                                          | The platform agent, a session, or a named profile, at `read`. The A2A console and the MCP server also read the bus                            | Access to that door. For the A2A console that is `pods/portforward`                                        | Not held; see the conditions                                                                                                  |
| The web console of the chart, the Hermes dashboard, the Hermes CLI                                                                                       | The platform agent through the Hermes API, with no tier. The web console also writes                                                          | `pods/portforward` for the web console; `pods/exec` for the CLI and, on a stock install, for the dashboard | Not held; see the conditions                                                                                                  |
| A post of the agent in a shared channel or thread                                                                                                        | What another person asked for. In the home channel also the alerts, the findings and the audit reports of the fleet, which no person asks for | The chat platform shows it to each member of the channel                                                   | Not held; see the limits                                                                                                      |

Some parts of this table are not checked, and the build checks each before it relies on the
row ("Done when", the list for row 8). They are: the filters of the A2A door; the code of the
home learn past its start; the board taps, the dial, `/feedback` and the Stop button past the
comments at the top of their files; how a scheduled job and a card post; the Teams adapter.

Row 8 has five parts, and they are one decision. With the people hold and no chat route, a
person of a team has no path at all. A chat backend cannot name a profile, a session is
refused, and each door that can name a profile runs at `read`.

- The people hold. A team profile gets a field for it; the build chooses the name before the
  first CRD is out. With the field set, the team profile names exactly one tier, and that tier
  is a held tier. The built-in `read` tier cannot be held: a person who is not listed is at
  `read`. We propose a field of the team profile, and not a mark on the `Tier`. An older CRD
  of the `Tier` drops a mark.
- How the gateway gets it. We propose that the people hold gets to the gateway as a value of
  the gateway's own, with a roll. It does not go into the tiers file. Three components read
  that file strictly, and an older verifier or auth callout would then lose each custom tier
  of the install.
- The refusals. For a person at a held tier the gateway does what the last column of the table
  says. A refused turn gets one notice. The rule covers each backend that can carry a member:
  Google Chat, Slack, Discord, and the Google and Entra sign-in classes of the A2A door. On
  those two classes the member match is without case, as the door's allowlist is.
- One place for the rule, and a default. The rule is one function of the gateway. The gateway
  calls it one time for a turn, before it tells the adapter that the sender is verified and
  before the turn route heals a task. Each path that is not a turn calls it: the Home tab's
  admission, the board admission of Google Chat, the start of the home learn, the delegate
  request, the wake of a session and a conversation post of the notify route. A conversation
  of a held person is one whose record names a requester or a steer author at a held tier;
  that includes a shared thread. A test lists each entry point of each adapter, each bus
  subscription and each HTTP route of the gateway. A new one fails the test until it has a
  line in the table.
- The chat route. A plain chat turn of that person goes to the tier's team profile. If more
  than one team profile lists the tier, the turn starts with the profile's name. That is one
  new chat form; the gateway matches the first word only against the names of that tier's team
  profiles. A first word that fits no name gets one notice with the names. The A2A door gets
  no such route.

The status condition `PeopleHeld` on the team profile shows what the operator can see. It
names the operator's version. It is True only when three things hold: the gateway pod that
works runs an image that the operator derives from its own tag; the gateway's roll for the
current member list and people hold ended; and Teams is off.

`PeopleHeld` counts only while two more things are true. The operator that runs is at R or
later, and it is the one that the condition names. The condition on the PlatformAgent says that
the controller of the kind runs.

`PeopleHeld` has a note, and stays True, for these things: an allowlist that is open to all;
an allowlist entry on a backend that can carry a member, with no entry in the member list; the
A2A console and the Hermes CLI, which are on each install; and the MCP server, the inject door,
the Hermes dashboard and the shared-token class of the A2A door, where the operator renders
them on. The operator does not render the chart's web console, so that door has no note. A
delete of the team profile ends the people hold and removes the condition.

What must be true for the person claim. The install can make it only while each line is true:

| What must be true                                                                                                                                                                                                                               | What shows it                                                                                                                                          | What removes it                                                                              |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Each human of the team is listed, on each backend that the ingress allowlist admits. A person who is not listed runs at `read` and is not held                                                                                                  | The admin, by hand. `PeopleHeld` has a note                                                                                                            | Group claims (#1999)                                                                         |
| Teams is off for the gateway. A Teams user cannot be listed                                                                                                                                                                                     | `PeopleHeld` is False                                                                                                                                  | A change that lets a Teams user be listed                                                    |
| No person of the team has `pods/portforward` or `pods/exec` in the install's namespace, or `get` on the token of a door. That covers the A2A console, the web console, the Hermes dashboard, the Hermes CLI, the MCP server and the inject door | The admin, by hand. `PeopleHeld` has a note for each such door that the operator renders                                                               | A principal for each user of the A2A console (#5557); nothing is planned for the other doors |
| No person of the team has the shared token of the A2A door                                                                                                                                                                                      | The admin, by hand. `PeopleHeld` has a note where that class is on                                                                                     | Nothing is planned                                                                           |
| The controller of the kind runs: the CRD and the role are present (row 6)                                                                                                                                                                       | The condition on the PlatformAgent. `PeopleHeld` counts only while that condition says that the controller runs                                        | Nothing; it is the state that row 6 names                                                    |
| The gateway pod that works runs an image that the operator derives from its own tag (row 6). With two replicas that is the pod with the Lease                                                                                                   | `PeopleHeld` is False. The operator reads that pod's image today, for another condition                                                                | Nothing; it is the state that row 6 names                                                    |
| The gateway's roll for the current member list and people hold ended                                                                                                                                                                            | `PeopleHeld` is False                                                                                                                                  | Nothing; a stalled roll keeps the old list                                                   |
| With the web console of the chart on, its admission policy is on (row 7)                                                                                                                                                                        | The admin, by hand                                                                                                                                     | Nothing; it is a setting of the install                                                      |
| No rollback below release R took place (row 6)                                                                                                                                                                                                  | The check of the rollback rule. `PeopleHeld` counts only while the operator that runs is at R or later and the condition names that operator's version | Nothing                                                                                      |

Limits that stay with the claim:

- A person of a team still reads what the agent posts in a channel that the person is in. That
  includes the answer to another person, and the alerts, the findings and the audit reports of
  the fleet in the home channel.
- One person is in one team. One team is one tier, and one team profile has one cluster. A
  team with workloads on five clusters has five profiles. The list of members has 200 entries
  at most for the install, and a tier's name has 10 characters at most.
- In a thread with a person of a team and a person at another tier, the turn of the team's
  person gets a refusal, as a thread with two tiers does today. A turn of the other person
  goes on as today.
- A task of the platform agent that runs for the person at the time of the people hold goes on
  to its deadline. Its result is dropped.
- A cost for each person of the install: with a held tier, a turn is refused on each route if
  an author has no verified identity. The same applies if the gateway cannot read the thread's
  author list in full. Today that refusal covers a profile route only.

The other way: row 8 is out of this design. Then:

- The install makes the task claim only, and the security page says so.
- A person of a team gets to the team profile through a session's delegate tool, as a person
  gets to the `cluster` profile today.
- That person can still ask the platform agent, or a session, about another namespace.

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
- Group claims for a team (#1999). A follow-up of #3009 below the epic will hold them.
- A console for a team's people. The users of the A2A console share one principal (#5557), so
  it stays at `read`. This design does not serve the "Web Console" part of #3009.
- A team's work on more than one cluster in one profile.
- Custom kinds for a team profile.
- Logs of the team from Cloud Logging. `kubectl logs` with `--previous` is the read that stays.
- A bound on a session pod or on the platform agent. With row 8, those paths are closed for the
  listed people of a team; row 8 does not change what those agents read.
- With row 8: a default tier other than `read` for a person who is not listed.

## Done when

For the guardrail part of #3009:

1. The security approver approved this document.
2. A team task is refused outside its namespace list and outside its cluster. Unit tests cover
   each line of [The kubectl rule](#the-kubectl-rule), the text of a refusal, the target
   refusal of row 3, each refused route of [Each other route](#each-other-route) and a route
   that is not on the list.
3. A conformance row and a mutation cover each of those refusals. One more covers that a map
   with one key and not the other does not start the broker. That row needs the module that
   parses the identity map, and the suite does not import it today. That is open question 11
   of the broker-surface design. If that question gets a no, the parse of the two keys moves
   to a module that the suite imports.
4. The CRD and the operator refuse each case of [The kind](#the-kind), with a test for each;
   one covers the size refusal of the map, and one the sweep of a card. One test covers that
   each form of the table in that section is outside the pattern of today. A team profile does
   not take a task of the generic `cluster` profile and does not cap another profile. The
   tests have one and two team profiles on one cluster.
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
9. The documents follow the answers. The security page has the task claim, its conditions and
   who can change the bound. The upgrade table and the rollback page have the rows of row 6.
   The broker-surface design names the team profile in its rule 1 and in each "Each profile"
   cell.

With row 8, also:

1. A person at a held tier gets each cell of the last column of the table in
   [The person bound](#the-person-bound), also the cells that stay. Each backend that can
   carry a member has a test. One test has the default route `session`, and one has an author
   with no verified identity.
2. The test of entry points, bus subscriptions and HTTP routes is there. Tests cover a wake
   after the people hold, a click on a choice button, a member entry in another case on each
   of the two door classes, and a Slack guest.
3. A test covers each line and each note of the condition `PeopleHeld`, and one covers the
   check of the rollback rule in each mode.
4. A plain chat turn of that person starts a task of the tier's team profile, with a test for
   one profile and for two.
5. One check on an install, by hand: a person of a team is refused the platform agent and
   starts a task of the team profile.
6. The build checks each item of the "not checked" sentence in
   [The person bound](#the-person-bound).
7. The security page has the person claim and its conditions, and the tier page describes a
   held tier.
