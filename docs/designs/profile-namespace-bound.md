# A namespace bound for a team's tasks

A proposal for the security approver; nothing in it is built. This is the design for the
guardrail of #3009, a part of the epic #2900. It says how a task that a person of a team starts
is held to one Kubernetes namespace of that team. It makes no claim about a person of that team:
the person bound gets a design of its own.

Each row of [Decisions](#decisions) is our proposal, with its other way beside it. After the
security approval we build the proposal of each row. A row that goes the other way changes this
document first.

The security approver decided four points on 2026-11-27
([the decision](https://github.com/gke-labs/kube-agents/pull/7354#issuecomment-7207672849)), on
an earlier text that put the bound in a kind of its own and in a `kubectl` rule of the broker.
This text moves the bound to the task credential and to `kube-proxy`, because a profile worker's
`kubectl` moves there (#7278). The four points were decided for the objects of that earlier
text, so this text does not take one of them as decided for a new object:

- The refused name `default` was decided for the namespace list of a team's profile. Row 1
  asks if it carries over to the namespace list of a binding.
- The admission policy was decided for the create and the change of a team's profile. Row 7
  asks if it carries over to a binding.
- The resource of a raw path was decided for the `kubectl` rule, which is gone. Row 4 asks
  again.
- The person bound is not a row of this document, as decided.

Related documents:

- [`profile-namespace-bound-facts.md`](profile-namespace-bound-facts.md) has the facts of today
  that the rows use, and what was checked in the code.
- [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) has profiles, tiers, membership and
  the task credential.
- [`kube-proxy.md`](kube-proxy.md) has the proxy, its `Authorize` hook and the task credential
  gate.
- [`profile-reads-with-no-cluster.md`](profile-reads-with-no-cluster.md) adds a target shape for
  a project. This design adds one for a namespace, in the same way.
- [`profile-broker-surface.md`](profile-broker-surface.md) has the rules for a grant to the
  profile role.

Words in this document:

- A namespace target is a target of the task credential with four keys: `project`, `location`,
  `cluster` and `namespace`. A cluster target has the first three.
- A bound task is a task with a namespace target. A bound credential is its credential.
- A bound profile is an AgentProfile with the mark of row 1.
- A binding is an object of the kind of row 1. It has the people of one team and, for each
  cluster of the team, a list of namespaces. A team is the people of one binding.
- The gate is the task credential gate of `kube-proxy` (`kube-proxy/taskgate`).
- A pin is an AgentProfile with `clusterRef`. A fixed image is an image that an install sets by
  hand for one component.
- Releases A and B are the two releases of
  [Release order and rollback](#release-order-and-rollback). A bound task can exist from
  release B.

## Summary

- Today each layer stops at the cluster. A profile task reads each namespace of its target
  cluster with the install's identity.
- Rows 1 to 7 hold a bound task to one namespace. The namespace is a key of the signed target
  of the task credential. The gate checks each request of the task against it, with the
  classification of the request that the API server itself uses.
- One ordinary AgentProfile is shared by the teams. A binding says which namespaces a team can
  ask for. The gateway checks the namespace that a task names against the bindings of the
  people who ask, and only then grants the task. Who is on a team is an open line of row 1: a
  list of people on the binding (proposed), or a tier.
- The grant carries the bound too. Its scope has the namespace as its last segment, so it is
  longer than the scope of a cluster target.
- A bound fails in the other direction than a grant. A component that does not know a grant
  refuses it. A component that does not know a bound does not apply it. So each new form here
  is one that an older component refuses or cannot produce. Row 6 has each state between the
  components. In each state a bound task has its bound, or it has no read of a cluster. One
  change is not a state between components: a principal who can write AgentProfiles makes a
  profile that was bound into a plain one. Row 7 has it.
- The bound is on the task, not on the person. Today the same person can ask the platform agent
  or open a session, and neither path has a tier check. This design does not close those
  paths.
- The design needs the last step of #7278: the operator gives a worker pod the proxy's address
  and turns the gate on. That step is not merged. Until it is, a bound task has no `kubectl`,
  and the broker refuses it (row 5).
- One profile that the teams share has a cost that a profile for one team does not have: the
  worker pods and the bus subjects of the profile are shared between the teams. Rows 8 and 9
  have the two paths that follow and what the claim needs for each.

What the security page can say:

- The task claim: "A task of a bound profile reads objects of a cluster only in one namespace,
  on one cluster. The namespace is one that a binding gives to each person who asked. The task
  can read the names and the fields of each kind of the cluster, and an object of the
  namespace can name things outside it."
- Four conditions go with the task claim. The cluster of the install enforces NetworkPolicy.
  Where the web console is on, its admission policy is on (row 7). A worker pod keeps nothing
  of one task for the next: no file, no process and no credential. No process of a task can
  use the pod's own connection to the bus. [What the teams share](#what-the-teams-share) says
  what is shown for the last two today, and what is not.
- One sentence goes with the task claim: "A person of a team can still ask the platform agent
  or a session about another namespace."

The guardrail line of #3009 names "the caller's authorized namespaces/repos (per-user tier
membership ...)". With the proposal of row 1 a binding names the caller, and a tier stays a
capability level; with its other way a binding names a tier. A hold on what a person can start
on the other paths is for the person bound's own design.

## Decisions

| Id  | Our proposal                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | With a no                                                                                                                                                                                                                                           | Where                                                                                                                   |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| 1   | The profile and the binding. A bound profile is an ordinary AgentProfile with a mark and with no cluster of its own. A binding is an object of a small kind of its own, with the people of a team and their namespaces on each cluster. See [The profile and the binding](#the-profile-and-the-binding). The approver decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7354#issuecomment-7207672849)) that `default` is a refused name on the namespace list of a team's profile. Proposed: the same on the namespace list of a binding.          | One of the other ways of that section: a kind for a team's profile; a tier as a team; a list on the PlatformAgent. A no to one line of the section removes that line. For `default`: the decision does not carry over, and the name is not refused. | the AgentProfile type and CRD, a new type and CRD, the operator, the card, `a2a/gateway/profiles.go`                    |
| 2   | The hold. The namespace is a key of the signed target, and the last segment of the grant's scope. See [The hold](#the-hold).                                                                                                                                                                                                                                                                                                                                                                                                                                                        | The other form: a list of namespaces in the target. Rows 4 and 6 then change as [The hold](#the-hold) says.                                                                                                                                         | `a2a/taskcred`, `task_credential.py`, the parity fixtures, `a2a/dispatch`, `a2a/gateway`                                |
| 3   | The target. The gateway takes the cluster of a bound task from the binding. The task names no project, location or cluster. When its namespace is on more than one cluster of the bindings, the task picks one of those by its name in the binding (`team_cluster`). The gate checks the cluster as today, and then the namespace.                                                                                                                                                                                                                                                  | The task names its cluster with the three parameters of today. A gateway below B then gives that task a cluster target with no bound ([The profile and the binding](#the-profile-and-the-binding)).                                                 | `a2a/gateway/profiles.go`, `kube-proxy/taskgate`                                                                        |
| 4   | The request rule of [The request rule](#the-request-rule), in the gate. It has no list of resources.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | No bound; the design ends. The other way for the resources: a closed list of resources by API group. A no to one line of the rule removes that line.                                                                                                | `kube-proxy/taskgate`; rule id `caller.task-namespace`                                                                  |
| 5   | Each other route. At the broker a bound credential reaches the documentation relay only, from a closed list. See [Each other route](#each-other-route).                                                                                                                                                                                                                                                                                                                                                                                                                             | The other way: a list of refused routes. A later grant then reaches a bound task on the day it merges. With no list at all the task claim does not hold: `gcloud logging read` returns the logs of each namespace.                                  | `credential_proxy.py`                                                                                                   |
| 6   | Release order and rollback: two releases, the result of each state between the components, and the rollback rule. See [Release order and rollback](#release-order-and-rollback).                                                                                                                                                                                                                                                                                                                                                                                                    | No release carries rows 1 to 5 until this document has another order.                                                                                                                                                                               | the upgrade table, the rollback page, `a2a/taskcred`, `task_credential.py`, `a2a/gateway`, `a2a/dispatch`, the operator |
| 7   | Who can change the bound. The approver decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7354#issuecomment-7207672849)) that an admission policy lets only named principals create or change a team's profile, where the `admissionPolicy` values gate renders it. That kind is not in this text. Proposed: the same policy for a binding, which holds a team's namespaces now. For each other object of [Who can change the bound](#who-can-change-the-bound), a principal that can write it can change the bound, and the security page says so. | The other way: no admission policy for the binding. The decision did not take that way for a team's profile.                                                                                                                                        | a policy in the chart, the security page                                                                                |
| 8   | The worker pod that the teams share. The container of a bound profile's worker restarts between two tasks, so that no process and no file of one team's task is left for the next. See [What the teams share](#what-the-teams-share).                                                                                                                                                                                                                                                                                                                                               | One of the other ways of that section: the build shows that the reset of today leaves no process; a pod for each task; a pool for each team. With none of them the task claim does not hold.                                                        | the worker adapter, the operator's pool                                                                                 |
| 9   | The bus subjects that the teams share. A bound profile ships only when a worker pod's own bus connection no longer reaches the tasks of other teams (#3889), or when the build shows that no process of a task can use that connection. It does not ship with only a stated limit. See [What the teams share](#what-the-teams-share).                                                                                                                                                                                                                                               | One of the other ways of that section: a bound profile ships with the limit stated on the security page; a pool and a bus identity for each team.                                                                                                   | the auth callout, the worker adapter, the security page                                                                 |

Rows 1 to 7 are the task bound. Rows 8 and 9 are what one shared profile needs beside it. The
person bound is not a row here.

### The profile and the binding

Each line is a proposal for the approver, with its other way where one exists.

The profile:

- A bound profile is an AgentProfile with the field `targetShape: namespace`. The build can
  change the name of the field.
- A bound profile can set only the fields of a closed list: `description`, `persona`,
  `harness`, `tools`, `lifecycle`, `queueTimeoutSeconds`, `concurrency`, `allowedTiers`,
  `parameters` and `resources`. The rules of the CRD refuse each other field of today:
  `clusterRef`, `yieldToPinned`, `identity`, `delegates`, `bus` topics and `scripts`. They also
  refuse the `unattended` tier and a parameter with the name `project`, `location` or
  `cluster`. Each of those makes the profile a pin, gives it a cluster target, starts a Job
  task, or gives its tasks a path that row 4 does not see.
- A later field of an AgentProfile does not reach a bound profile on the day it merges. A test
  fails when the type gets a field that is neither on the closed list nor in the rules; a
  design then gives the field a line here. The other way: a list of refused fields only.
- The other way for `delegates`: a bound task can delegate, and its child is held to the
  parent's namespace. The gateway's delegation code reads only a scope of six segments as a
  cluster scope today, so that way needs a change there and a state of its own in row 6.
- A bound profile declares the parameter `namespace` as required. It can declare the parameter
  `team_cluster`, which a task needs only when its namespace is on more than one cluster.
- The operator writes the mark on the profile's card and on the profile's line for the
  dispatcher.
- One bound profile is shared by the teams. So the model of
  [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) stays: one profile for each role, and
  no object, identity or card for each cluster. The built-in `devteam` profile of #3009 is the
  first bound profile; it is the next change of #3009.
- `allowedTiers` is the capability level, as for each profile. With `read` on the list, each
  person of the install can address a bound profile. A person with no binding gets a refusal
  and no task.
- A bound profile is not a pin for routing, and its tiers do not cap another profile. A pin on
  the same cluster caps a bound task as it caps each other profile's task; that guard of today
  stays.
- One warm pool of a bound profile serves the tasks of each team, one after the other.
  [What the teams share](#what-the-teams-share) has what follows from that.

The binding:

- The kind is `TeamBinding`, in the install's namespace. It has two fields, and each is
  required: `members` and `clusters`.
- `members` lists people in the form of `spec.tiers.membership` of today: a backend and a user.
  The gateway matches a person as it matches a member of a tier. A person can be in more than
  one binding. A tier stays a capability level.
- The other way for `members`: the binding names a tier, and a team is the people of that
  tier. The table below has the limits of that way. A step between the two is possible: the
  tier way first, and `members` or a group (#1999) later.
- `clusters` has 1 to 8 items. Each item has a `name` (a DNS label), a `cluster` (project,
  location and cluster name) and `namespaces`, with 1 to 32 items. Each namespace is a DNS
  label. The build sets the limit of `members`. The other way for the numbers: 1 to 32
  clusters, for an install whose teams work on more than 8, and 1 to 200 members. The hard
  limit in each way is the size of the file below.
- Four names are refused as namespaces: `default`, `kube-system`, `kube-public` and
  `kube-node-lease`. The operator also refuses the namespace of the install, which a CRD rule
  cannot name. The approver decided `default` for the list of a team's profile; row 1 asks if
  it carries over to this list. This is a guard against an error, and it is not a bound:
  row 7 says who can write the list.
- A list can name a namespace that other teams use. A bound task then reads each object of
  that namespace, also the objects of the other teams. The security page tells the writer to
  list only the namespaces of that one team.
- A binding has no pod, no ServiceAccount, no durable, no card and no warm pool. The operator
  of B renders all bindings into one file, in a ConfigMap of its own that no pod template
  hashes. The gateway mounts it as a directory and reads the file's state on each request, as
  it reads the scope file. A file that the gateway cannot read or parse gives no namespace
  target.
- Other ways for the gateway's read of the bindings: an environment value, as the membership
  of a tier is today, which rolls the gateway on each change; or a read of the objects from
  the API server, which gives the gateway a role that it does not have today.
- The operator refuses a binding that takes the file above its size limit, with a condition.
- A later field that narrows a bound task does not come as an optional field with an open
  default. It comes as a form that an older component refuses or cannot produce, as each form
  of this design does.

How a task gets its target, at a gateway of B:

1. The task addresses a bound profile and names a `namespace`.
2. The gateway takes the bindings that list each author of the task. In a thread with more
   than one author, a binding counts only if it lists each of them.
3. Of the clusters of those bindings, it takes the ones whose list has the namespace. With
   none, it refuses the task. With more than one, the task must name one of them with
   `team_cluster`; else the gateway refuses. It also refuses when two of them have that name.
4. The gateway checks that cluster against the install's scope and the pins, as for each
   cluster target.
5. The gateway grants the task with the scope of row 2. It writes the cluster's project,
   location and name into the task's parameters itself, and it refuses a task that sets one of
   those three.

Six lines of these steps are choices, each with its other way:

- A thread with more than one author. Proposed: a binding counts only if it lists each author
  (step 2). Other ways: a bound profile refuses each thread with more than one author; or the
  binding of one author is enough, which gives a co-author a namespace that no binding gives
  that person.
- Two clusters with one name. Proposed: the gateway refuses the task (step 3). The other way:
  the operator refuses a binding whose cluster name another binding has, so that a name is
  one cluster in an install.
- The text of a refusal. Proposed: it names the namespaces that the authors can ask for, so
  that the model can correct the task. That shows a team's namespace names to the model of a
  member's session. The other way: a fixed text with no names.
- A session's delegation. Proposed: the same steps apply, and the authors are the authors of
  the session's turn. The other way: a session cannot delegate to a bound profile; a person
  in chat then has no path to it, because chat reaches a profile only through a session.
- A profile task as the parent. Proposed: it cannot delegate to a bound profile; the gateway
  refuses it, because such a child has no author of its own. The other way: the child takes
  the authors of the first turn of its chain.
- A steer. Proposed: a steer into a bound task is refused unless a binding gives the task's
  namespace, on the task's cluster, to the author of the steer too. The other way: only an
  author of the task can steer it.

A requester that is no person gets no namespace target: no binding can list the A2A console's
shared principal, the inject door, the MCP server, the shared-token class of the A2A door, a
Teams user, an AgentSchedule or a trigger. The membership of a tier has the same limit today.

Why one profile and a binding, and not a profile for each team:

| Point                                                  | One bound profile that the teams share, and a binding for each team (proposed)                        | A kind for a team's profile (the earlier text)                                                                                                              | A tier as a team                                                                                                                                                                              |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Objects for a team                                     | One binding. No pod and no identity                                                                   | One profile for each team and cluster, with its ServiceAccounts, pool, durables and card                                                                    | One `Tier`, and its line on a profile                                                                                                                                                         |
| What it adds to the control plane                      | One kind with no controller path of a profile; one file for the gateway                               | A second path in each table that the operator renders from the profile list, and each later feature of an AgentProfile again                                | Nothing new                                                                                                                                                                                   |
| Limits of today                                        | The size of one file                                                                                  | The durables of each profile and tier are consumers of one stream with an allowance of 64; a ServiceAccount and an identity entry for each profile and tier | `allowedTiers` has 4 items at most, so one profile serves 4 teams at most. A tier's name there has 10 characters at most and no hyphen. The membership has 200 entries. A person has one tier |
| A person in two teams                                  | Yes                                                                                                   | Yes                                                                                                                                                         | No                                                                                                                                                                                            |
| The worker pods and the bus subjects of a team's tasks | Shared between the teams. [What the teams share](#what-the-teams-share) has the two paths that follow | The team's own                                                                                                                                              | Shared between the teams, with one profile                                                                                                                                                    |
| A component that does not know the form                | Refuses it or gives no target (row 6)                                                                 | Does not run the profile                                                                                                                                    | An older gateway runs the task at the tier with no bound, unless the profile is of a kind of its own                                                                                          |

#### What the teams share

A profile for one team gives a team its own worker pods and its own bus subjects. One bound
profile that the teams share does not. Two paths between teams follow, and the task claim needs
an answer for each. Rows 8 and 9 are these two answers.

The worker pod:

- A pod of the warm pool takes the tasks of each team, one after the other. The dispatch spec
  says that between two tasks each path that a task can write is reset, or the container
  restarts.
- Shown in the code: after each task the worker adapter empties each reset path, and it ends
  the worker when that fails.
- Not shown: that no process of a task is left after the task.
- A task's credential is in a file that the task's processes can read. The credential is valid
  until its expiry, which follows the task's deadline, also after the task ended. The gate
  checks a credential and not if its task still runs.
- So a process that a task of one team leaves could read the credential of the next task, of
  another team, and call the proxy with it.
- Proposed (row 8): the container of a bound profile's worker restarts between two tasks. That
  is the second form of the dispatch spec, and it leaves no process. Its cost is the start of
  a container for each task. Other ways: the build shows that the adapter ends each process
  of a task; a pod for each task; a pool for each team.

The bus:

- A worker pod's own connection to the bus reaches the task subjects of each task of its
  profile. A change that gives each task a connection of its own is planned (#3889) and not
  merged.
- With one profile that the teams share, those are the tasks of each team: their inputs and
  their events.
- Not checked: if a process of a task can use the pod's own connection.
- Proposed (row 9): a bound profile ships only when #3889 has narrowed that connection, or
  when the build shows that no process of a task can use it. It does not ship before that
  with only a stated limit.
- Other ways: a bound profile ships before that, and the security page states that a task of
  one team can reach the task subjects of another team; or a pool and a bus identity for each
  team, at the cost of a profile for each team.

What the binding kind needs in the repository:

- The type, its CRD in the operator and in the chart, the operator's role for it, and its line
  in the operator's RBAC self-check.
- A start gate. The operator of today skips the controller of a kind whose CRD or role is
  absent at its start, and does not look again. The kind needs the look on the steady pass,
  one restart and a condition on the PlatformAgent.
- The file, its mount in the gateway, and the gateway's reader.
- The field pages, the condition texts, the uninstall page and the security page's list of
  custom resources.
- No webhook: only the PlatformAgent has one.

The other way for the binding is a list on the PlatformAgent. It needs no kind and no start
gate. The policy of row 7 then names a field of the PlatformAgent, and the web console's `patch`
on that object reaches the list where its own policy does not render.

### The hold

The gate needs the namespace of the task. Three forms:

- One namespace in the signed target (proposed). The dispatcher mints `namespace` as the fourth
  key of the target. The grant of the task has the scope
  `project/<p>/location/<l>/cluster/<c>/namespace/<n>`. Both verifiers and the gate read the
  key.
- A list of namespaces in the signed target. The dispatcher mints the list, and the gate
  admits each namespace of it.
- Two keys in the identity map, for a profile that serves one team (the earlier text). Each
  strict reader of the map must know the keys first: the broker, the exec tier and the gate.

What each form costs:

| State                             | One namespace in the target                                                                                          | A list in the target                                                                                                          | Two keys in the identity map                                      |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| What changes                      | The minter in `a2a/dispatch`, both verifiers, the gate, the gateway's grant                                          | The same, and a list form in the credential                                                                                   | The operator, and three strict readers. One profile for each team |
| The grant                         | The scope of the grant has the namespace. The capability verifier holds the dispatcher to it                         | A scope is one path and cannot hold a list. Only the dispatcher's own code ties the list to the task                          | No change                                                         |
| A question about three namespaces | Three tasks                                                                                                          | One task                                                                                                                      | One task                                                          |
| A change of a team's list         | No restart. The next task has the new list. A credential that exists keeps its namespace until it expires            | The same                                                                                                                      | The broker restarts, the exec tier and `kube-proxy` roll          |
| A verifier below A                | It refuses the credential whole. Closed                                                                              | The same                                                                                                                      | It does not start on the map                                      |
| A minter below B                  | The capability verifier refuses its ask: the grant's scope is longer than the cluster scope that it asks for. Closed | The older dispatcher asks for the cluster scope and gets it, and mints no list. Open, unless the grant gets a form of its own | An older operator renders no entry                                |

Proposed: one namespace in the target. The grant then carries the bound, and each check of
today holds an older component with no mechanism of its own.

A change of a binding:

- The gateway has the new file after the kubelet's delay. No component restarts.
- A task that runs keeps its credential. A credential is valid until its expiry, which follows
  the task's deadline; the install's lifetime limit holds it (6 hours by default, 168 hours at
  most).
- The gate checks a credential when a request starts. A watch or a log follow that a task
  opened through the worker adapter streams on until the adapter closes it at the end of the
  task.
- So a namespace that a binding lost is closed when each credential that names it has expired.
  The security page says so.

### The request rule

For a request of a caller that the gate's identity map names, with a bound credential, after
the checks of today (the credential, the pod, the profile, the tier, the cluster). The rule is a
closed list. `RequestInfo` is the classification that the proxy makes with the API server's own
classifier, before the gate runs.

| The request                                                                              | Rule                                                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A resource request with the namespace of the target                                      | Passes to the proxy's policy, which admits it as for each other caller: the verbs `get`, `list` and `watch`, and a write only with a dry run                                                                                                                                                      |
| A resource request with another namespace                                                | Refused                                                                                                                                                                                                                                                                                           |
| A resource request with no namespace: a cluster-scoped kind, or a list across namespaces | Refused                                                                                                                                                                                                                                                                                           |
| A path that is no resource                                                               | `GET` or `HEAD` on the discovery paths that `kubectl` needs pass: `/api`, `/api/<version>`, `/apis`, `/apis/<group>`, `/apis/<group>/<version>`, `/version`, `/openapi/v2`, `/openapi/v3` and the paths below it. Each other path is refused, also `/metrics`, `/healthz`, `/livez` and `/readyz` |
| A path with no cluster                                                                   | Refused, as today                                                                                                                                                                                                                                                                                 |
| A credential with a cluster target, of a profile that is not bound                       | The rule of today. This rule does not apply                                                                                                                                                                                                                                                       |

Behind the gate the proxy's policy of today applies to a bound task as to each caller:

- The read verbs pass. A `create`, an `update` and a `patch` pass only with a dry run.
- `exec`, `attach`, `portforward` and `proxy` are refused, and so is an upgraded connection.
- `pods/log` is a read, and a log follow streams.
- A read of Secrets returns the names and the metadata only. The proxy asks the API server for
  a form with no values, redacts the answer, and removes an annotation that holds a copy of
  the Secret.

A refusal has the rule id `caller.task-namespace` in the audit record and a fixed message that
names the task's namespace, so that the model can correct the call.

Limits of the rule:

- The rule has no list of resources. A bound task reads each namespaced kind of its namespace,
  also a custom kind. The earlier `kubectl` rule had a closed list of kinds, because a rule on
  the command line cannot know if a kind is namespaced; the gate has the namespace of each
  request. The decision of 2026-11-27 held a raw path to that list. Its aim was that no path
  goes around the rule, and here one rule sees each request. Open for the approver: no list
  (proposed), or a closed list of resources by API group.
- The discovery paths show the names and the fields of each kind of the cluster, also the
  custom ones. They show no object.
- ConfigMaps of the namespace are readable, with what they hold. The names and the keys of its
  Secrets are readable.
- An object of the namespace can name things outside it, such as a pod's node. The rule reads
  the request, not the answer.
- A read of the namespace object itself, and what the classifier gives for it, is not checked.
  The build checks it; the rule admits no list of namespaces.
- The rule refuses each cluster-scoped read and each health path. The other way: a later design
  gives a chosen one a line in the rule, such as a node's state or `/readyz`. A read that has no
  line stays refused.
- The rule adds no verb rule: behind it the proxy's mode admits a write with a dry run in the
  task's namespace, as for each other caller. Such a write stores nothing; the admission
  webhooks of the cluster see it, under the install's identity. The other way: the gate
  admits only `get`, `list` and `watch` for a bound credential.
- A namespace's name can go to another team later. The binding is then wrong until a person
  changes it.

### Each other route

A grant to the profile role goes to each profile; that is rule 1 of the broker-surface design.
That design plans more grants. One is the Logging read of the Cloud API relay for "Each
profile", which returns the container logs of each namespace. So a bound task needs a default
that the broker holds:

- The broker has a closed list of routes for a bound credential: the documentation relay, which
  reads public documents only. A route that is not on the list is a refusal.
- So `/v1/exec` is refused, with `kubectl` and `gcloud`, and so are each version-control verb
  and the audit-report store. `logging read` and the serial console read data of each
  namespace, and the version-control reads reach each managed repository.
- The other way for the list: more routes on it, such as the version-control read verbs. Such
  a route needs a tie to the team first, and none exists today.
- That refusal of `/v1/exec` holds in each release, also while a worker's `kubectl` still goes
  to the broker. A bound task then has no `kubectl`. It gets one only through the proxy.
- A later grant to the profile role does not reach a bound task until a design gives it a row
  here. Its conformance row must see the refusal for a bound credential. The pull-request part
  of #3009 is such a row.
- A profile pod reaches only DNS, the bus, LiteLLM, the broker and, with the last step of
  #7278, the proxy. So those two are its only paths to a cluster. That rests on a network
  plugin that enforces NetworkPolicy.

### Release order and rollback

The bound comes in two releases:

1. Release A. Both verifiers accept one more key set in a target: the three keys of a cluster
   target with `namespace`. The key sets of today stay, and so does the project target of
   [`profile-reads-with-no-cluster.md`](profile-reads-with-no-cluster.md) when it is built. Each
   other set is refused, as today. The gate applies row 4 to a credential with the new set, and
   the broker applies row 5. The worker adapter takes the new set.
2. Release B. The binding kind, its CRD and the admission policy of row 7; the field of the
   AgentProfile. The operator renders the bindings' file, and the mark on the card and on the
   dispatcher's line. The gateway reads the mark and the file and grants the scope of row 2.
   The dispatcher mints the fourth key for a profile whose line has the mark.

A grant with the cluster scope contains the longer scope of row 2, because a grant permits each
scope that it is a prefix of. So under a cluster grant a dispatcher of B would mint a namespace
that no binding gave. No gateway gives a marked profile a cluster grant: below B the card gives
no target, and at B the gateway grants the long scope only. Proposed as a second check, in
release B: for an ask with the namespace segment, the capability verifier admits only a grant
with exactly that scope. The project target design has the same check for its scope. The other
way: no second check; at this point the bound then rests on the gateway's rule alone.

Release B needs one more thing that is not of this design: the last step of #7278. Before it,
no install turns the gate on, the proxy accepts no token of a profile worker, and no worker pod
has the proxy's address. A bound task then starts and has no `kubectl`. Proposed: the code of A
and B does not wait for that step, and the built-in profile of #3009 ships only in a release
that has it. Other ways: release B waits for that step; or the work of this design builds that
step, which is another owner's issue today.

Safety does not rest on this order. What each state does:

| State                                                                                    | How it comes                                                                                                                                                      | What each component does                                                                                                                                                                                                                                                      | Result                                                                                                            |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| The proxy or the broker is below A, and a bound credential exists                        | An upgrade to B on an install below A, between its steps; a harness-only rollback below A; a fixed image                                                          | The verifier refuses a target key that it does not know, and with it the whole credential                                                                                                                                                                                     | Closed: the task has no read of a cluster through that component                                                  |
| The proxy has no gate, or its gate is off                                                | A fixed image for the proxy from before the gate; an install before the last step of #7278                                                                        | The proxy accepts the token of a profile worker only with the gate on, and then only from a ServiceAccount of the gate's identity map. With no gate it refuses the worker's token                                                                                             | Closed: a bound task has no `kubectl`                                                                             |
| The dispatcher is below B; the gateway is at B                                           | A fixed image for the dispatcher                                                                                                                                  | The dispatcher asks the capability verifier for the cluster scope. The grant's scope is longer, and a grant must be a prefix of what is asked. The verifier refuses                                                                                                           | Closed: the task is not admitted                                                                                  |
| The gateway is below B                                                                   | A fixed image for the gateway; an operator reference with a digest and no tag, or no reference at all; an old pod in a roll                                       | The gateway does not know the mark. The card names no cluster, so the task gets no target, and its grant has the scope of the install's namespace. The dispatcher mints no credential for the proxy or the broker for such a task                                             | Closed: the task runs with no read of a cluster                                                                   |
| The dispatcher is at B; the mark is not on its line                                      | An operator below B                                                                                                                                               | The dispatcher asks for the cluster scope, as in the row of the older dispatcher                                                                                                                                                                                              | Closed                                                                                                            |
| The CRDs are at B or later; the operator is below B                                      | Each upgrade to B, between the CRD apply and the operator's roll; a failed operator step; `helm rollback`; newer CRDs by hand                                     | The operator writes a card with no mark and renders no file. As in the row of the older gateway                                                                                                                                                                               | Closed                                                                                                            |
| The CRDs are below B; the operator is at B                                               | A `helm upgrade` by hand, which applies no CRDs; older CRDs by hand                                                                                               | The API server drops the field of the profile, and the binding kind is absent. The operator writes no mark and has no binding                                                                                                                                                 | Closed: no task gets a namespace target                                                                           |
| The binding's CRD or the operator's role for it is absent; the operator is at B or later | The same; an operator image ahead of its ClusterRole                                                                                                              | The operator runs with no controller of the kind and says so in a condition on the PlatformAgent. It looks for the CRD and its role on its steady pass, and restarts one time when both are there. What it does with a file that it rendered before is for the build to state | Closed for a new binding. A file that is left keeps its bindings                                                  |
| The gateway cannot read or parse the file                                                | A defect; a file from a newer operator                                                                                                                            | No namespace target                                                                                                                                                                                                                                                           | Closed                                                                                                            |
| No operator runs                                                                         | A stopped operator of B                                                                                                                                           | The file keeps its content                                                                                                                                                                                                                                                    | The bound holds. A change of a binding does not apply                                                             |
| The capability verifier or the auth callout is below B                                   | A fixed image; an old pod in a roll                                                                                                                               | Each works as today. A scope with eight segments is well-formed for the verifier of today, and a bound profile is an ordinary profile for the callout                                                                                                                         | The bound holds                                                                                                   |
| The worker adapter is below A                                                            | An old worker pod                                                                                                                                                 | The adapter routes by the target's cluster. If it refuses the fourth key, the task has no `kubectl`                                                                                                                                                                           | The bound holds, or closed. The build states which                                                                |
| The operator moves back below B                                                          | The first step of a rollback                                                                                                                                      | The older operator writes the card and the dispatcher's line with no mark, and does not touch the file. As in the rows of the older gateway and of the line with no mark                                                                                                      | Closed                                                                                                            |
| A bound profile loses its mark                                                           | A rollback below B: the older CRDs drop the field. A change by a principal of row 7                                                                               | The profile is a plain profile with no cluster parameter. As in the row of the older gateway                                                                                                                                                                                  | Closed. The mark must be set again after the next upgrade to B; the chart does that for a profile that it renders |
| A profile that was bound gets a cluster parameter                                        | A change by a principal of row 7. The rules of the CRD of B refuse it for a profile with the mark; with the CRDs below B, or after the mark is gone, no rule does | The profile is a plain profile with a cluster target, under the same name                                                                                                                                                                                                     | Row 7                                                                                                             |

The rollback rule:

- A rollback below release B gives no task a namespace target. It does not open the task bound.
  No step before the rollback is necessary for the task claim.
- `kubectl get teambindings -A` lists each binding, before and after the rollback. The objects
  stay, and the next upgrade to B uses them again as they are.
- The rollback applies the older CRDs, and the API server then drops the mark of each bound
  profile. Such a profile stays a plain profile with no cluster parameter, and its tasks get no
  target. After the next upgrade to B the chart sets the mark again for a profile that it
  renders; a person sets it again for a profile that a person made.
- A chart below B has no admission policy of row 7. A write to a binding in that time is not
  held, and the next upgrade to B uses the list as it is then.
- The file of the bindings stays after the operator moved back, until the next upgrade to B or
  until a person deletes it. No component below B reads it.
- The rollback page and the upgrade table get a row with these sentences.

### Who can change the bound

| Object                                                                        | What it changes                                                                                                                                                          | Who can write it today                                                                                           | Admission control today                                                                                                                                |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The binding                                                                   | The people of a team, its clusters and its namespaces                                                                                                                    | New kind                                                                                                         | The rules of the CRD, and the policy of the next lines                                                                                                 |
| The admission policy of the kind, its binding and the `admissionPolicy` value | Who can create or change a binding                                                                                                                                       | A cluster admin, and Helm                                                                                        | None                                                                                                                                                   |
| The CRD of the kind                                                           | The rules of each field. A delete of it deletes each binding                                                                                                             | A cluster admin, and the install and upgrade scripts                                                             | None                                                                                                                                                   |
| A bound profile                                                               | A change that removes the mark and adds a cluster parameter, or a delete and a create, makes a plain profile with the same name, which has a cluster target and no bound | For AgentProfiles today: the operator's ServiceAccount has `patch`, and the chart's pre-delete hook has `delete` | The rules of the CRD of B, which hold only a profile that has the mark. No admission policy names AgentProfiles today                                  |
| The file of the bindings                                                      | The list that the gateway reads                                                                                                                                          | The operator's ServiceAccount, and a principal with write on ConfigMaps of the install's namespace               | None. The operator renders it again                                                                                                                    |
| The dispatcher's Deployment and the task-signing key                          | The minter of each credential                                                                                                                                            | The operator's ServiceAccount                                                                                    | None                                                                                                                                                   |
| The gateway's Deployment                                                      | The component that checks a task against the bindings and grants it                                                                                                      | The operator's ServiceAccount                                                                                    | None                                                                                                                                                   |
| The Deployment of `kube-proxy`                                                | The gate's identity map and its signing keys; the proxy's mode                                                                                                           | The operator's ServiceAccount                                                                                    | None. The operator renders it again                                                                                                                    |
| The PlatformAgent                                                             | `spec.deployment.image` chooses the broker's image                                                                                                                       | The operator's ServiceAccount. The ServiceAccount of the chart's web console has `get` and `patch`               | A validating webhook, where the install turns it on. A policy holds the web console to three keys of `spec.integration`, only where the policy renders |
| The operator's Deployment                                                     | The image values for the broker, the gateway, the dispatcher and the proxy                                                                                               | Helm, and a principal with write on Deployments                                                                  | None                                                                                                                                                   |
| The profile namespace                                                         | The pods, the ServiceAccounts, and the NetworkPolicy that row 5 rests on                                                                                                 | The operator's Role there                                                                                        | A policy holds a Job of the dispatcher to the operator's template, only where the `admissionPolicy` gate renders it                                    |

- A principal with the Kubernetes role to write one of these objects can change or open the
  bound. The binding is the exception, where the policy of the next line renders. The agent's
  own roles have no write on these objects.
- Proposed for the binding: a ValidatingAdmissionPolicy lets only named principals create or
  change one, where the `admissionPolicy` values gate renders it. The approver decided that
  policy on 2026-11-27 for a team's profile, a kind that this text no longer has; row 7 asks
  if the decision carries over. The chart has that pattern for another kind. Its cost is that
  gate, Kubernetes 1.30 or later, and a list of principals for each install. Where the policy
  does not render, each writer of the kind can change a binding.
- Two points of that policy are open for the approver: what an empty list of principals does,
  and if the list must name the principal that applies the chart, because a chart can render a
  binding. The rule of this document is no open default.
- That policy does not cover the other objects of the table, and the security page says so.
  Open for the approver: if the same policy also holds each write of a bound profile, so that
  only a named principal can remove the mark.
- The task claim needs the policy of the chart's web console where that console is on: its
  `patch` reaches the image field. The policy of the web console needs the `admissionPolicy`
  values gate and Kubernetes 1.30 or later.

## Options for the task bound

| Option                                                                          | What it needs                                                                                | What the facts say                                                                                                                                                             |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A. A namespace list on a profile for each team, held by the profile's name      | A kind for a team's profile, two keys in the identity map, a `kubectl` rule in the broker    | The earlier text. A profile worker's `kubectl` moves from the broker to the proxy, and each strict reader of the map stops on a new key. One profile for each team and cluster |
| B. The namespace in the signed target, checked by the gateway against a binding | A new target shape in the minter, in both verifiers and in the gate; a binding for each team | No value that the model sets reaches the credential with no check. An older verifier refuses the new key, and the capability verifier holds an older minter. Two releases      |
| C. One namespace in the signed target, from a task parameter, with no check     | A new target shape                                                                           | The model in a session can set a task parameter                                                                                                                                |
| D. A Kubernetes identity for each team, with a namespaced RoleBinding           | The proxy and the broker run each call as that identity                                      | Each runs a call as the install's identity. The command policy says that impersonation "is planned, not deployed"                                                              |
| E. Rules in the persona's text                                                  | Nothing                                                                                      | The security page says that persona rules "are guidance, not enforcement"                                                                                                      |

Proposed: B. Row 2 has its two forms.

## What stays out

- The built-in `devteam` profile and its persona. They are the next change of #3009, with its
  bench case. That case needs a requester that a binding can list; the inject door is none
  today.
- The pull-request part of #3009: the confirm from chat (#4429) and a repository bound for a
  proposal (#4428).
- Group claims for a team (#1999). #7355, a follow-up of #3009 below the epic, tracks them. A
  group then takes the place of the `members` list.
- A console for a team's people. The users of the A2A console share one principal (#5557), so
  no binding can list them. This design does not serve the "Web Console" part of #3009.
- More than one namespace in one task.
- Logs of the team from Cloud Logging. `kubectl logs` with `--previous` is the read that stays.
- A bound on a session pod or on the platform agent.
- The person bound: a hold on what a person of a team can start. It gets a design of its own.

## Done when

For the guardrail part of #3009:

1. The security approver approved this document.
2. A bound task is refused outside its namespace and outside its cluster. Unit tests of the
   gate cover each line of [The request rule](#the-request-rule), the text of a refusal, and
   what the classifier gives for a list across namespaces, a cluster-scoped read, a read of a
   namespace object and each discovery path. Unit tests of the broker cover each refused route
   of [Each other route](#each-other-route) and a route that is not on the list.
3. A conformance row and a mutation cover each of those refusals of the broker. The parity
   fixtures of the two verifiers cover the new key set of a target, the sets of today and each
   other set.
4. The CRDs and the operator refuse each case of
   [The profile and the binding](#the-profile-and-the-binding), with a test for each; one
   covers the size refusal of the file, and one fails when the AgentProfile type gets a field
   that is on neither list. Tests of the gateway cover each step of "How a task gets its
   target", a thread with two authors, a session's delegation, the refusal of a profile parent,
   the refusal of a steer, the tier cap of a pin, a requester that is no person, and a file
   that it cannot read. A test of the chart covers that the admission policy of row 7 renders
   with the gate, and a test against an API server covers that it refuses a principal that is
   not named.
5. One test for each state of row 6 whose result is "closed" or "the bound holds", by its
   name. A test of a component below A or B runs from the commit alone: a bound credential,
   the card, the dispatcher's line and a grant as B makes them are fixtures in the commit. The
   tests against a verifier, a gate, a broker, a dispatcher and a gateway of today land in
   release A, against the code of A.
6. Those tests include these. A verifier of today refuses a credential with the fourth key, in
   Go and in Python. A capability verifier of today refuses the cluster scope under a grant
   with the namespace segment. A gateway of today gives a task of a card with the mark no
   target. A dispatcher of today mints no credential for the proxy for a task with no target.
   A capability verifier of B refuses an ask with the namespace segment under a cluster grant.
7. One check on an install, by hand: a bound task reads its namespace and is refused in
   another, at the proxy; the broker refuses its `/v1/exec` call. After a move of the operator
   below B and back, a bound profile has no mark and its task gets no target; with the mark set
   again, a bound task runs again.
8. Rows 8 and 9 hold before a bound profile is in a release. A test covers that the worker of
   a bound profile restarts its container between two tasks, and that the next task finds no
   file, no process and no credential of the task before it. For row 9, a test covers that a
   worker pod's own bus connection reaches no task of another team, or that no process of a
   task can use that connection.
9. The build checks three things that this design did not check. They are the API requests of
   `kubectl describe` and `kubectl top pod` for a namespace; what a profile worker's key reads
   on LiteLLM's own endpoints, also if it keeps anything of the model's session between two
   tasks; and what the worker adapter of today does with a target key that it does not know.
10. The documents follow the answers. The security page has the task claim, its conditions,
    who can change the bound, and what a list exposes when it names a namespace that other
    teams use. The upgrade table and the rollback page have the rows of row 6. The proxy's
    design has the request rule. The broker-surface design names a bound credential in its
    rule 1 and in each "Each profile" cell.
