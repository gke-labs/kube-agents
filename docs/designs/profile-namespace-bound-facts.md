# The namespace bound: the facts of today

The facts that [`profile-namespace-bound.md`](profile-namespace-bound.md) uses, with what was
checked in the code and what was not. Nothing here is a proposal.

## The read path of a profile task

- A profile worker's `kubectl` goes to the broker, or to the broker's exec tier where that
  renders. The broker runs it on the managed kubeconfig of the task's target cluster. The
  identity is the install's Google identity; with the scoped service account pool armed, it is
  the account that the cluster maps to. Each reaches each namespace of the cluster.
- The task credential names the target as project, location and cluster. Both verifiers refuse
  another key.
- The profile role reaches four routes: `/v1/exec`, the documentation relay, the audit-report
  store and the version-control verbs. The Cloud API relay is closed to the role today.
- On `/v1/exec` the role runs `kubectl` and `gcloud` only. A flag must be on a fixed list.
  `--namespace`, `-n`, `--all-namespaces` and `-A` are on it, and no security rule reads their
  values. The memory estimate of the next bullets reads `-A`.
- The read-only rule admits the read verbs: `get`, `describe`, `logs`, `events`, `top`,
  `explain`, `api-resources`, `cluster-info`, `auth can-i`, `wait`, `rollout status`,
  `config current-context` and others. A second table refuses some of them for a profile. A
  Secret read is refused by kind.
- The broker parses the call one time (`Confined`): the command words in one tuple, and each
  flag by its long name. So `-n X` and `--namespace=X` are the same entry. It does not tell a
  kind word from an object's name.
- The call gets `--context` for the target and no namespace. The kubeconfig of a target is
  written by `gcloud`, and no code sets a namespace in it. The broker's own kubeconfig has the
  agent's namespace as its default; a profile call does not use that file.
- A raw path is checked: it can hold a `/namespaces/<name>` part, which the check does not read,
  and a query with a field selector. The pattern admits no path below an object.
- No list in the code says which kinds are namespaced, for a security rule. One table for a
  memory estimate marks 17 kinds as namespaced and five as cluster-scoped. It is not complete,
  and its gate fails open.
- `gcloud` is held to the target project. `logging read` returns the log entries of each
  namespace, for each cluster of the project that the install does not exclude.
  `compute instances get-serial-port-output` returns a node's console.
- The version-control read verbs (`clone`, `proposal-list`, `issue-view` and others) reach each
  managed repository, with no tie to the task.
- A profile pod's network: no ingress, and egress to DNS, the bus, LiteLLM and the broker only.
  On the bus a profile worker gets the task subjects of its own profile. So a profile task has
  no route to a cluster but the broker. This rests on a network plugin that enforces
  NetworkPolicy.

## What the broker knows about a profile

- The task credential carries the profile's name. The broker also binds it to the caller: the
  attested ServiceAccount must be the one that the operator's identity map gives that profile.
  Two profiles cannot share a ServiceAccount.
- "The identity map" in this file and in the proposal is the broker's. The auth callout has a
  map of its own.
- The identity map has one entry for each ServiceAccount, that is one for each profile and tier.
  Its key is the ServiceAccount's user name, and each value has the keys `profile` and `tier`.
- The broker decides by the profile's name in one place today, the allowlist of the
  audit-report store. That list holds two fixed names.
- The identity map is an environment value that the broker reads one time, at start. It is read
  strictly: an entry with a key that the broker does not know stops the broker's start.
- Two files of the operator reach the broker with no restart and fail closed. The hold file is
  a ConfigMap of its own. The scope file is a key of the agent's config ConfigMap. Each is
  mounted as a directory, and the broker reads the file's state on each request. A file that
  it cannot read or parse refuses each cluster target. With the variable unset, the check
  refuses nothing.
- The command policy module cannot read a file; the conformance suite holds that.

## How a change reaches each component

- The broker is one pod with the `Recreate` strategy. A change of the identity map restarts it,
  and each of its routes is down for that time.
- The exec tier is off by default. Its Deployment is a copy of the broker's, with the identity
  map, so each exec pod holds the map of its own pod template. It rolls one pod at a time
  (`maxUnavailable` 1, `maxSurge` 0). With 2 or more replicas it stays up during a roll. At one
  replica, callers fall back to the broker. It has 32 replicas at most. An idle pod exits in
  about 3 seconds; a pod in a command has a grace period of 380 seconds.
- The broker's image follows the agent image, which the harness step of an upgrade moves. The
  gateway, the verifier and the auth callout are release images. By default they follow the
  operator image's tag, so they move in the operator step. The gateway can be older than the
  operator in four cases. An install pins `A2A_GATEWAY_IMAGE`. The operator reference has a
  digest and no tag. The install sets no operator reference, as a kustomize install with the
  agent image set. An old pod serves during the roll. In the second and third case the gateway
  follows the agent image.
- The stated upgrade order is the operator first, then the harness. The time between the two
  steps has no limit, and each mode can run alone. The release pipeline runs the operator mode
  and the harness mode from the previous GA release. That leg does not gate the release.
- A rollback runs the operator step first. That step applies the CRDs of the older release, and
  the API server then prunes a field that the older schema does not declare. The AgentProfile
  CRD keeps no unknown field.
- The tiers file holds each `Tier` by `name` and `mode`. The gateway, the verifier and the auth
  callout read it strictly, each 30 seconds. A file with a key that the reader does not know
  installs the compiled-in table, so each custom tier of the install is lost for that reader.
- The CRDs and the operator do not move together. An upgrade in the operator mode or the full
  mode applies the CRDs first and then moves the operator. Helm applies no CRDs on an upgrade,
  so a `helm upgrade` by hand moves the operator and leaves the CRDs. `helm rollback` moves the
  chart back and keeps the CRDs; the upgrade script runs it itself for a release in a pending
  state.
- An operator does not reconcile a kind that it does not know. If the CRD of a kind that it
  knows is absent at its start, the operator of today skips that controller, writes one log
  line and does not look again. It does the same if its ClusterRole cannot read the kind. The
  PlatformAgent pass reads a kind that is absent as "no profile".
- The identity map is built from the list of AgentProfiles. The PlatformAgent pass also deletes
  each warm pool that it does not want; a pool has no owner reference, and the pass finds it by
  its labels.
- The operator can read CRDs, and it has one such read today, with a `CRDOutdated` reason. The
  chart refuses a render when the AgentProfile CRD lacks a field that the chart needs.
- An apply of a CRD set does not prune. It does not change a CRD that the set does not have.
- The operator writes a profile's card on the bus directory. The gateway reads the card with a
  tolerant parse. A card has no field for a team profile today.
- The ServiceAccount of a profile at one tier has a fixed prefix, the profile's name and the
  tier in its name, unless `spec.identity.serviceAccountName` names one. The operator refuses a
  named one with that prefix, with a reserved name, with the name of a bus principal or of
  another profile, and for a profile with more than one tier. It does not check a named one
  in another way.
- The operator deletes only the ServiceAccounts that an AgentProfile owns. So no operator below
  a new kind deletes the ServiceAccounts of that kind.
- The dispatcher chooses a worker pod by two labels, the profile's name and the tier. It does
  not read the pod's ServiceAccount. It is one pod with the `Recreate` strategy, and its image
  can be set apart.
- The dispatcher mints no credential with a target for a task with no cluster target. For a
  task that a trigger starts at the report tier it mints a credential with no target, and the
  broker takes that one on the audit-report routes only.
- The dispatcher reads its table with a tolerant parse, from one list of profiles. It keeps the
  last good table when it refuses a new one. The auth callout reads its map strictly: it
  refuses a key that it does not know, and a value of a key that it does not know.
- The name of an AgentProfile and the name of a tier have no dot. The operator checks both
  itself.
- A dispatcher derives its durables from the lines of its table. It refuses to bind a durable
  with another filter, and it ends a delivery off another subject. Its grant is by the name of
  the durable, and the callout derives that grant from the profile entries of its map.
- The gateway reads the directory with one token after its root.
- The pool sweep finds a pool by the component label and the agent's labels.
- The submit subject, the durable and the card's subject have the profile's name and no other
  mark of the profile. The profile controller writes the card and the durables; the
  PlatformAgent pass writes the dispatcher's table and the identity map.
- The dispatcher's Role creates Jobs in the profile namespace. A policy holds such a Job to the
  operator's template; it renders only with the `admissionPolicy` gate, on Kubernetes 1.30 or
  later.
- A card with the parameters `project`, `location` and `cluster` takes its target from the
  task. A card with no `clusterRef` and no such parameter gets no target.
- A card stays on the directory after its operator moved back; nothing sweeps it.
- With the election on, the gateway runs two replicas and one holds a Lease. The operator then
  reads the image of that pod for the `GatewayElected` condition.
- An AgentProfile has a finalizer, for the tombstone of its card.
- Linux limits one environment string to 128 KiB.
- The tier page says for membership: a rollback to a release without it runs each person at
  `read` again.

## A pinned profile today

- A profile with `clusterRef` is a pin. A task for the generic `cluster` profile on that cluster
  goes to the pin, because `cluster` yields to a pin. Two pins on one cluster refuse such a task
  as ambiguous. The function that finds pins reads `clusterRef` only.
- A pin's tiers cap each other profile's task on its cluster.
- The AgentProfile type has CEL rules of its own today.

## Who can use a profile

- A person, for the gateway, is one user on one backend. The same human on two backends is two
  persons.
- A person has one tier. `spec.tiers.membership` of the PlatformAgent lists a person for a tier
  that a `Tier` resource defines. A person who is not listed runs at `read`; the code fixes
  that. A person cannot be listed two times, and the list holds 200 entries at most. The list
  reaches the gateway as an environment value, so a change of it rolls the gateway.
- The backends that can carry a member are Google Chat, Slack, Discord, and the Google and
  Entra sign-in classes of the A2A door.
- A Teams user, the A2A console's shared principal, the inject door and the shared-token class
  of the A2A door cannot be listed. Each runs at `read`. The A2A console cannot tell its users
  apart.
- The install has two consoles. The A2A console is on each install. It uses the gateway's
  console door, reads the bus, and has no login; its gate is `pods/portforward`. The web
  console of the chart is off by default. It calls the Hermes API of the platform agent, with
  no tier, and it has write routes. The Hermes dashboard and the Hermes CLI get to the platform
  agent also; the CLI needs `pods/exec`.
- A member is matched by the hash of the backend's author id. The id is in lower case only for
  Google Chat and Teams. The allowlists of the A2A door compare in lower case.
- A profile lists its tiers in `allowedTiers`, and the gateway refuses a task at another tier. A
  tier's name on that list has 10 characters at most and no hyphen. A thread whose authors are
  at two tiers gets a refusal.
- No profile has a list of people. The gateway has such a list for one target, the platform
  agent, and uses it for a session's delegation.
- A chat backend cannot address a profile; no chat form names one. The A2A console's frame, the
  MCP server and the inject door can. The A2A door sets no profile, and a delegation from the door
  is refused, because no list is rendered for it.
- In chat a person reaches a profile through a session and its delegate tool. The session
  offers each profile whose card lists the authors' tier, and the model in the session chooses
  the task's parameters.
- The default route of a plain chat turn is `platform`. An install can set it to `session`.
- The tier functions run only for a profile route and for a steer into a profile task. A session
  and the platform agent run with the install's one grant.
- With the session cluster view on, a session's `kubectl` reads through kube-proxy with a
  cluster-wide read identity.

## Paths of a person that are not a turn

- The Slack Home tab shows a fleet dashboard to each member that the ingress allowlist admits.
  It has each cluster with the title of its worst open finding, the running cards, the recent
  pull requests and the last audit. No model call is part of it, and no gate reads a tier.
- The channel picker on the same tab moves the install's home channel, where the alerts, the
  drift reports and the scheduled reports of the fleet post. The picker shows only while the
  home learn is armed: `spec.integration.slack.homeChannel` is unset and `homeChannelPolicy` is
  `auto`, the default. Its gates are the workspace, "not a guest", the ingress allowlist and a
  check of the channel. No gate reads a tier. A move posts one line in the new channel and one
  in the old channel, each with the member's name.
- While the home learn is armed, a mention in a channel can record that channel as the home, on
  Slack and on Google Chat. It does so while no home is recorded, or while the backend refuses
  the recorded home. The learn starts when the gateway has verified the sender, before the
  route.
- A Retry or Drop tap on a board row acts on the row's cards and reads nothing. On Slack the
  Home tab's rule admits it; on Google Chat the ingress allowlist does.
- A tap on the autonomy dial of the Slack Home tab is admitted by the Home tab's rule. The
  session store then checks that the presser is an admin.
- The gateway answers some forms itself. `/feedback summary` posts usage counts of the install.
  A Stop button ends a task for its requesters.
- The notify route posts the alerts, the cron findings and the audit reports of the fleet. They
  go into the home channel, or into a conversation for which the gateway holds a live session
  record. No person asks for them, and the route reads no tier.
- The entry points of the adapters. Discord has one handler, for a new message. Slack handles a
  message, a stopped agent session, a started assistant thread, an opened Home tab, and
  interactive payloads.
- Checked by a second reader only: Slack drops the envelope of a slash command, and its adapter
  answers a typed `/sethome` itself. An edit and a reaction start nothing. Google Chat handles
  a message and a card click only. A confirm comes only from the A2A console and the inject
  door. A click on a choice button, the "ask about this" message shortcut and a quoted ask are
  turns. A rating tap only writes a rating.
- A click on a choice button, or a typed answer, marks the question as answered when the
  gateway has verified the sender, before the route.
- A wake of a session, a delegate request and a conversation post of the notify route start
  with no new turn of a person. Slack keeps a published Home view until the next publish.
- A status question in a thread replays the status of the thread's running task, and a typed
  `stop` cancels it. Each shows only what the thread shows.

## Who can write the objects

- No admission policy names AgentProfiles or Tiers. The chart has such a policy for another
  kind (a built-in AgentSchedule), behind the `admissionPolicy` values gate and on Kubernetes
  1.30 or later.
- The PlatformAgent has a validating webhook. It is off by default in the chart, and the full
  install turns it on.
- The ServiceAccount of the chart's web console has `patch` on the PlatformAgent. A policy holds
  the web console to three keys of `spec.integration`. It renders only with the same gate, on
  Kubernetes 1.30 or later, with the web console and its environment page on.
- The agent's own roles read the PlatformAgent. They have no write on the PlatformAgent, on an
  AgentProfile, on a Tier or on a Deployment.
- The operator's ServiceAccount has `patch` on AgentProfiles. The chart's pre-delete hook has
  `delete` on them. The operator's roles write the Deployments of the install's namespace and
  the pods and ServiceAccounts of the profile namespace.
- `spec.deployment.image` of the PlatformAgent chooses the agent image, and the broker's image
  follows it. `spec.deployment.env` cannot set the name of the identity map; the operator
  reserves it.

## The older documents

The architecture pages describe one Developer Team agent for each namespace as "Coming soon",
with a hard boundary at the namespace edge.
[`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) replaces that model with one profile for
each role and a pinned profile for a special case; it gives a profile no scope below the
cluster. [`profile-broker-surface.md`](profile-broker-surface.md) is merged with the status
"draft for review". Its rule 1 says that a verb of the profile role is a grant to each profile,
and its rule 2 says that the hold is a fact of the task credential. It plans the Monitoring, Trace and
Logging reads of the Cloud API relay for "Each profile". Its open question 11 asks if the
conformance suite takes `cryptography` as a dependency. The module that parses the identity map
needs it, and the suite does not import that module today.

## Where to look

The facts have no line numbers, because they go stale. The main files for each section:

- The read path and the broker: `agents/platform/scripts/credential_proxy.py`,
  `task_credential.py`, `task_confinement.py`, `command_policy.py`, `mcp_policy.py`.
- The operator's tables and images: `k8s-operator/internal/controller/` with the files
  `credential_proxy_profiles.go`, `credential_proxy_exec_tier.go`, `agentprofile_identities.go`,
  `agentprofile_pool.go`, `agentprofile_card.go`, `platformagent_a2a_dispatcher.go`,
  `platformagent_a2a_manifests.go`, and `k8s-operator/cmd/main.go` for the start gate.
- The gateway: `a2a/gateway/` with `profiles.go`, `tier_membership.go`, `allowlist.go`,
  `gateway.go`, `notify.go`, `console.go`, `slack_home.go`, `slack_home_pick.go`,
  `home_channel.go`, and the adapters' files.
- The dispatcher and the bus names: `a2a/dispatch/pool.go`, `a2a/dispatch/dispatcher.go`,
  `a2a/dispatch/source.go`, `a2a/lib/dispatch.go`, `a2a/lib/client.go`, `a2a/capability/tiers.go`.
- The upgrade and the rollback: `scripts/installer/installer_common.sh`, and the upgrade page,
  the rollback page and the user-interfaces page of the site.
- The roles and the policies: `charts/kube-agents/templates/`.

## Checked, and not checked

Checked in the code on `main` at `439361639`:

- The credential: the target keys; that both verifiers refuse another key; the bind of the
  profile's name to the caller's ServiceAccount; that two profiles cannot share one; the
  identity map's key and its two value keys.
- The broker: the four routes and the two executables of the profile role; the flag list and
  that no security rule reads a namespace value; the form of `Confined`; the read verbs; the
  Secret rule; the raw path check and its query keys; the table of kinds in the memory
  estimate; `logging read` and the serial console in the confined `gcloud` commands; the
  version-control verbs; the one decision by the profile's name and its two fixed names; the
  two kubeconfig files.
- The operator's values: the identity map, its strict read and the restart; the `Recreate`
  strategy; the exec tier's copy of the environment, its roll, its replica limit and its grace
  period; the hold file and the scope file, and the unset case; that the policy module cannot
  read a file; the image that each component follows; the network policy of a profile pod and
  the bus grant of a profile worker; the card's writer; the name of a profile's ServiceAccount;
  the reserved name of the identity map; the roles of the operator and of the pre-delete hook;
  the operator's start gate for an absent CRD or role; the pool sweep; the dispatcher's choice
  of a pod; the target of a card.
- The gateway: the pin's routing and its tier cap; the tolerant read of a card; the strict read
  of the tiers file and its three readers; the tier of a person who is not listed; the list key
  and the size of the membership; the backends that can carry a member and the doors that
  cannot; the tier refusal; the tier name's form; the thread rule; the people list of
  `platform`; that a chat backend sets no profile and that no chat form names one; the default
  route; that a session command and a steer into a session have no tier check; the session
  cluster view's switch; the Home tab's admission and the gates of the channel picker; the
  start condition of the home learn; the gate of a board tap on each backend; the status
  question and the typed `stop`; the notify route; the handlers of the Discord and Slack
  adapters; the `GatewayElected` condition.
- By a second reader only: the two consoles and their gates; the tolerant parse of the
  dispatcher's table and the strict read of the callout's map; the Jobs role of the dispatcher
  and its policy; the case of the member match; the wake of a session; the durable's filter
  and its grant; the directory subject; the labels of the pool sweep.
- The documents: the two quotes of the options table; the Developer Team agent of the
  architecture pages; the sentence of the dispatch spec that replaces it; rules 1 and 2, the
  options, the relay row and open question 11 of the broker-surface design; the order and the
  CRD step of the upgrade page and the rollback page; the rollback sentence of the tier page.
- The roles: the web console's `patch` and its policy's gate; the agent's own roles; that no
  admission policy names AgentProfiles or Tiers; the default of the PlatformAgent's webhook.

Not checked: the behaviour of `gcloud` and `kubectl` for a kubeconfig with no default
namespace; what each confined `gcloud` command returns; what the API server answers to a field
selector on a namespace; the requests that `kubectl describe` makes; the Google roles of the
install's identity on each cluster; the kubelet's delay for a changed ConfigMap; the Hermes
side of the platform agent; the filters of the A2A door; the home learn's code past its start
condition; the board taps, the autonomy dial, `/feedback` and the Stop button past the comments
at the top of their files; what a LiteLLM key can read on LiteLLM's own endpoints; how a
scheduled job and an open card post to the person who started them; the Teams adapter; the
minter and both verifiers for the credential form; which clients other than `kubectl` ask for
strict field validation; what an informer does after a delete of its CRD; the
128 KiB limit on a running pod; each other design document for a scope below the cluster; a
running install.
