---
title: Credential isolation
description: How the operator keeps API keys, tokens, and ServiceAccount credentials out of the agent sandbox container using an Envoy credential-proxy sidecar.
sidebar:
  order: 7
---

In the default sidecar layout the PlatformAgent sandbox container receives no API keys, access tokens, refresh tokens, or Kubernetes ServiceAccount tokens through its environment or filesystem. Credentials live exclusively in a trusted **Envoy credential-proxy sidecar** inside the same Pod, and the sandbox reaches credentialed capabilities only through a policy-enforced local proxy.

Enabling [`spec.security.splitCredentialBrokerPod`](#splitting-the-broker-into-its-own-pod) changes that in one specific way: the broker moves to a Pod of its own and the sandbox is given a projected ServiceAccount token so it can authenticate across the network. Every unqualified "the sandbox holds no token" statement on this page describes the sidecar layout; [the agent now holds a credential](#the-agent-now-holds-a-credential-and-that-was-a-choice) has the trade.

This page summarizes the architecture. The canonical design — including scope, deny-policy details, migration steps, and CI verification assertions — is [`docs/credential-isolation-design.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/credential-isolation-design.md).

## Pod anatomy

Each PlatformAgent runs as one long-lived Pod with these managed containers:

| Container                  | Trust level | Role                                                                                                                                                |
| -------------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `platform-agent`           | Untrusted   | The agent sandbox — credential-free env and mounts, CLI wrappers.                                                                                   |
| `envoy-credential-proxy`   | Trusted     | Envoy, the credentialed command and chat runtime, and the event watcher, which forwards cluster events using its own separate Kubernetes-API token. |
| `fluent-bit`               | Trusted     | Log forwarding.                                                                                                                                     |
| `platform-agent-dashboard` | Untrusted   | Optional local dashboard (also credential-free).                                                                                                    |

```mermaid
flowchart TB
    subgraph Pod["PlatformAgent Pod"]
        SANDBOX["platform-agent (sandbox)<br/>CLI wrappers / chat adapters<br/>no credentials"]
        subgraph SIDECAR["envoy-credential-proxy"]
            ENVOY["Envoy listener<br/>127.0.0.1:8765"]
            RUNTIME["Credential runtime<br/>real CLIs, Slack/Chat clients, Minty client<br/>secret env + projected KSA token"]
        end
        SANDBOX -->|"HTTP (structured argv)"| ENVOY
        ENVOY -->|private Unix socket| RUNTIME
    end
```

The sandbox image contains only **wrapper binaries** for `gcloud`, `kubectl`, `gh`, and `git`. A wrapper sends the executable name and argument array to Envoy at `127.0.0.1:8765`; the credential runtime executes the corresponding real CLI and returns output and exit status. It never evaluates an agent-supplied shell command, and the runtime's Unix socket is mounted only in the sidecar, so the sandbox cannot bypass Envoy. The real credential-aware CLIs ship in a separate `credential-proxy` image that the sandbox never runs.

**The sandbox environment does not cross the boundary.** The command runs with an environment the sidecar builds itself, so exporting a variable in the agent shell has no effect on the proxied process. Two values are carried explicitly in the request instead, and both must resolve inside the shared agent workspace or the request is rejected with `400`:

- **Working directory** — so relative paths in `git` and `kubectl` arguments mean what the agent intends.
- **`KUBECONFIG`** — how a Cluster Agent profile pins itself to one target cluster. Without a `KUBECONFIG`, commands use the context the sidecar bootstrapped for the host cluster.

**A kubeconfig names a cluster; it never supplies content.** The pin lives on the shared volume, so it is a document the agent can write — and a kubeconfig is executable configuration, not passive data. Fields such as `users[].user.exec.command`, `clusters[].cluster.server`, and `users[].user.tokenFile` would respectively run a program next to the credentials, redirect the minted access token, and disclose a sidecar file as a bearer token. None of it is visible to the [command policy](#request-paths), whose rules match on the argument array: the argv is only ever `kubectl get pods`. The design doc has the [full enumeration and the reasoning](https://github.com/gke-labs/kube-agents/blob/main/docs/credential-isolation-design.md#agent-supplied-kubeconfigs).

So the sidecar reads exactly one string out of the file the agent wrote, `current-context`, accepts it only if it is a well-formed `gke_<project>_<location>_<cluster>` name, and regenerates the kubeconfig itself with `gcloud container clusters get-credentials` into a directory mounted only in the sidecar. That regenerated file is what every proxied command runs against. The same substitution is applied to a `--kubeconfig` flag, which kubectl prefers over the environment. `get-credentials` is the one command allowed to author a kubeconfig: it writes into the sidecar's own directory and the result is copied out to the workspace afterwards, so the visible pin still exists for the agent to inspect without ever being what a later command opens.

Naming a cluster is not extra authority — `get-credentials` is bound by the same IAM the proxy already runs under, so it can only reach clusters this identity could reach anyway. A pin the proxy cannot regenerate from (no `current-context`, a non-GKE context name, a merged `path1:path2` list) is rejected with `400` rather than honored.

**Tree-mutating `git` runs only inside a leased workspace.** Containment to the shared volume keeps the agent off the sidecar's filesystem; it says nothing about keeping concurrent agents off each other, and a Pod runs six audit crons alongside every kanban worker. A skill takes a lease and works in a private clone under `/opt/data/gitops/<lease>/<owner>__<name>`; the proxy refuses `git add`, `commit`, `checkout`, `push`, `reset` and the other verbs that write a working tree or a remote ref unless the resolved directory — after any `-C` redirect — sits under one holding a `.lease` marker. Read verbs, `fetch`, and `clone` are unaffected. The refusal comes back as `SECURITY_POLICY_BLOCKED` with rule `git.workspace.lease`, and `CREDENTIAL_PROXY_REQUIRE_GIT_LEASE=0` disables the check for an unmigrated skill.

This is a floor, not an ownership check: the wrapper sends an argument array and a working directory, never a caller identity, so the sidecar can tell that a push is happening inside _some_ lease but not whose. Whether the lease is the caller's own is checked in the sandbox by the skill that holds it. [`docs/designs/gitops-workspace-leases.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/gitops-workspace-leases.md) is canonical for the layout and the reaper.

**`git` runs with its configuration pinned by the proxy.** `git` is the one permitted executable that takes both its transport and its helper programs from configuration files, and it reads several of those from the shared volume — so its defaults are overridden rather than trusted. Every proxied `git` runs with the transport allowlist restricted to `https`, the system configuration file suppressed, the global configuration file pinned to a path mounted only in the sidecar, the hooks directory pinned to an empty directory the agent cannot write to, the file-system monitor disabled, commit signing and its signing program disabled, subcommand autocorrection off, and both of the editors `git` launches set to a command that does nothing — the sidecar has no terminal, so nothing that needed one could have worked anyway.

The proxy also refuses, in the argument array, the global options that would undo any of that — `-c` and `--config-env`, which set configuration outranking the pins; `--exec-path`, which chooses where git looks for the program to run; `--git-dir` and `--work-tree`, which name a repository directly and so sidestep the workspace containment applied to the working directory; and `--global`, `--system` and `--file`, which write the configuration files the proxy pins. `-C` remains available for choosing a directory inside a leased workspace, since the containment check follows it, and repository-local `git config` remains available because that is how a clone's commit identity is set. Refused alongside them are the subcommands whose purpose is to run a caller-named command, among them `bisect`, `difftool`, `mergetool`, `filter-branch` and `send-email`, and the options that do the same job on an ordinary subcommand — running a command once per commit during a rebase, or over the matches of a search. The design doc has the [full list and the reason it is a denylist rather than an allowlist](https://github.com/gke-labs/kube-agents/blob/main/docs/credential-isolation-design.md#git-configuration). Every refusal comes back as `SECURITY_POLICY_BLOCKED` with rule `git.argument.refused`, and no shipped skill uses any of them.

These pins do not extend to configuration stored inside a repository's own `.git/config`, which the agent authors and which `git` honors both for settings that name a program to run and for settings that decide which host a fetch contacts and which credential helper answers for it. Reducing that surface is why the proxy is moving toward receiving file content from the agent rather than operating inside a directory the agent controls; until then, treat a cloned working tree as agent-controlled input rather than as a trust boundary.
**`git` runs with its configuration pinned by the proxy.** `git` is the one permitted executable that takes both its transport and its helper programs from configuration files, and it reads several of those from the shared volume — so its defaults are overridden rather than trusted. Every proxied `git` runs with the transport allowlist restricted to `https`, the system configuration file suppressed, the global configuration file pinned to a path mounted only in the sidecar, the hooks directory pinned to an empty directory nothing can write to, and the file-system monitor and external diff programs disabled. The proxy also refuses `-c`, `--config-env` and `--exec-path` in the argument array, because each would set configuration or a program search path that outranks the pins; that refusal comes back as `SECURITY_POLICY_BLOCKED` with rule `git.argument.config`. No shipped skill passes any of the three.
**`git` runs with its configuration pinned by the proxy.** `git` is the one permitted executable that takes both its transport and its helper programs from configuration files, and it reads several of those from the shared volume — so its defaults are overridden rather than trusted. Every proxied `git` runs with the transport allowlist restricted to `https`, the system configuration file suppressed, the global configuration file pinned to a path mounted only in the sidecar, the hooks directory pinned to an empty directory the agent cannot write to, and the file-system monitor and external diff programs disabled. The proxy also refuses five global options in the argument array: `-c`, `--config-env` and `--exec-path`, because each sets configuration or a program search path that outranks the pins, and `--git-dir` and `--work-tree`, because those name a repository directly and so sidestep the workspace containment that the working directory is checked against. `-C` remains available for choosing a directory inside a leased workspace, since the containment check follows it. The refusal comes back as `SECURITY_POLICY_BLOCKED` with rule `git.argument.refused`. No shipped skill passes any of the five.
**`git` runs with its configuration pinned by the proxy.** `git` is the one permitted executable that takes both its transport and its helper programs from configuration files, and it reads several of those from the shared volume — so its defaults are overridden rather than trusted. Every proxied `git` runs with the transport allowlist restricted to `https`, the system configuration file suppressed, the global configuration file pinned to a path mounted only in the sidecar, the hooks directory pinned to an empty directory the agent cannot write to, and the file-system monitor disabled.
**`git` runs with its configuration pinned by the proxy.** `git` is the one permitted executable that takes both its transport and its helper programs from configuration files, and it reads several of those from the shared volume — so its defaults are overridden rather than trusted. Every proxied `git` runs with the transport allowlist restricted to `https`, the system configuration file suppressed, the global configuration file pinned to a path mounted only in the sidecar, the hooks directory pinned to an empty directory the agent cannot write to, the file-system monitor disabled, and both of the editors `git` launches set to a command that does nothing — the sidecar has no terminal, so nothing that needed one could have worked anyway.

The proxy also refuses, in the argument array, the global options that would undo any of that — `-c` and `--config-env`, which set configuration outranking the pins; `--exec-path`, which chooses where git looks for the program to run; `--git-dir` and `--work-tree`, which name a repository directly and so sidestep the workspace containment applied to the working directory; and `--global`, `--system` and `--file`, which write the configuration files the proxy pins. `-C` remains available for choosing a directory inside a leased workspace, since the containment check follows it, and repository-local `git config` remains available because that is how a clone's commit identity is set. Refused alongside them are the subcommands whose purpose is to run a caller-named command, among them `bisect`, `difftool`, `mergetool`, `filter-branch` and `send-email`, and the options that do the same job on an ordinary subcommand — running a command once per commit during a rebase, or over the matches of a search. The design doc has the [full list and the reason it is a denylist rather than an allowlist](https://github.com/gke-labs/kube-agents/blob/main/docs/credential-isolation-design.md#git-configuration). Every refusal comes back as `SECURITY_POLICY_BLOCKED` with rule `git.argument.refused`, and no shipped skill uses any of them.

These pins do not extend to configuration stored inside a repository's own `.git/config`, which the agent authors and which `git` honors both for settings that name a program to run and for settings that decide which host a fetch contacts and which credential helper answers for it. Reducing that surface is why the proxy is moving toward receiving file content from the agent rather than operating inside a directory the agent controls; until then, treat a cloned working tree as agent-controlled input rather than as a trust boundary.

**`kubectl` and `gcloud` are read-only by default.** The proxy enforces that `kubectl` may not run mutating verbs like `delete`, `create`, `patch`, or `rollout restart`, and that `gcloud` may not run commands that change cloud resources. It refuses the flags that would change which identity a command authenticates as or which server receives the credential — `--as`, `--server`, `--token`, `--kuberc`, `--insecure-skip-tls-verify` and their `gcloud` equivalents — and the refusal comes back as `SECURITY_POLICY_BLOCKED` with a rule such as `kubernetes.read-only` or `kubernetes.identity-change-forbidden`.

**This allowlist is currently the only thing enforcing that posture.** There is no second layer beneath it today: a command it fails to refuse runs against the cluster with the sidecar's full credential. Kubernetes impersonation is planned and not yet deployed; once it ships, the API server will authorize each request as the requesting human user and this allowlist becomes the outer of two layers rather than the whole control. Note also that the current deployment shares one Google service account across every agent — that is the gap impersonation closes, not a mitigation.

A first deployment in a live environment will find read-only commands nobody anticipated. The fix is to add the verb to the allowlist in `command_policy.py` and ship it; that keeps the change reviewable and scoped to the one command that was missing. Report the blocked command to your infrastructure team with the rule id from the refusal.

## Credential placement

| Data                             | Sandbox                                                                               | Credential sidecar        |
| -------------------------------- | ------------------------------------------------------------------------------------- | ------------------------- |
| `spec.deployment.env`            | No                                                                                    | Yes                       |
| Slack tokens                     | No                                                                                    | Yes, Secret-backed env    |
| PlatformAgent external API key   | No                                                                                    | Yes, Secret-backed env    |
| Session KV API key and HMAC salt | Yes, Secret-backed env                                                                | Yes, API key only         |
| Automatic KSA token mount        | Disabled                                                                              | Disabled                  |
| Explicit projected KSA token     | Not mounted in the sidecar layout; mounted read-only under `splitCredentialBrokerPod` | Read-only, one-hour token |
| gcloud/kubectl configuration     | No                                                                                    | Private `emptyDir`        |
| GitHub installation token/cache  | No                                                                                    | Private `emptyDir`        |
| Agent workspace                  | Yes                                                                                   | Yes, for proxied commands |

`SESSION_KV_API_KEY` and `SESSION_KV_SALT` are the sandbox's only Secret-backed environment variables, and both are pod-scoped: neither opens anything outside this Pod. They cannot sit behind the proxy because the sandbox is the _server_ here — `session_kv_server.py` binds `127.0.0.1:8699` and needs the key in order to reject callers that are not the event watcher, the Platform MCP server, or the `incident_context` plugin — and because the salt hashes chat identities before they are written, which has to happen where the identity already is. The design doc has the [full reasoning](https://github.com/gke-labs/kube-agents/blob/main/docs/credential-isolation-design.md#the-loopback-only-exception). Both are optional in the sense that the pod starts without them, and one of them is not optional in practice: the `k8s-event-watcher` in the credential sidecar authenticates with `SESSION_KV_API_KEY` and treats an empty value as fatal, so it exits on every start and no cluster events are watched at all — the container stays Ready, so its log is the only place that says so. The Session KV server also answers `503`, and identity hashing falls back to a per-process salt with a warning.

Pod-wide `automountServiceAccountToken` is `false`. The sidecar's projected token uses the audience `kubeagents-credential-proxy` and expires after one hour; the event watcher gets a separate one-hour Kubernetes-API token projection, mounted in the same sidecar at the conventional in-cluster path. Neither token is mounted in the agent or dashboard containers.

## Request paths

- **CLI commands** — only `gcloud`, `kubectl`, `gh`, and `git` are accepted. The proxy rejects known credential-disclosure, credential-replacement, and self-modification operations; interactive TTY programs, unbounded streaming, sandbox-only file paths, and background processes fail closed.
- **Chat** — Slack and Google Chat adapters send credential-free payloads to Envoy; the credential runtime owns the platform tokens and performs the external API calls, enforcing user allowlists and payload limits.
- **PlatformAgent API** — the Service targets port 8643 on the sidecar, which validates the external bearer key and forwards to the sandbox API on loopback (port 8642) with a non-secret sentinel. The real key never enters the sandbox.
- **GitHub** — the sidecar obtains a Google OIDC identity token and calls [Minty](/kube-agents/deploy/token-minter/), which brokers a repository-scoped GitHub App installation token with a maximum one-hour lifetime. The App's private key stays in Cloud KMS.

## Guarantee and limitation

**Guarantee, in the sidecar layout:** the operator does not place managed credentials in the sandbox container's environment, root filesystem, persistent agent volume, or mounted ServiceAccount token path. `spec.deployment.env` is applied to the credential sidecar because it may contain credentials (only a short allowlist is copied to the sandbox — the four OpenTelemetry settings and the three `ALERT_DAILY_LIMIT_*` alert ceilings — as literal values only). The one exception is `splitCredentialBrokerPod: true`, which mounts a projected ServiceAccount token in the sandbox on purpose; everything else in this list still holds there.

**Limitation:** containers in one Pod share a network namespace and one Pod identity. The sandbox has no KSA token file in this layout, but it can technically reach the GKE metadata server used by the sidecar — a Pod-level NetworkPolicy cannot block metadata for one container while allowing it for another. The design meets the scoped filesystem-and-environment goal but does not provide the stronger identity boundary of separate Pods.

**This limitation is live in the default install.** [Denying the sandbox the metadata server](#denying-the-sandbox-the-metadata-server) is available, but only on top of the broker Pod split, which is itself off by default. A stock agent can reach `169.254.169.254` and mint the Workload Identity token directly, bypassing the broker and every policy control in front of it.

What the two containers do **not** share is a process namespace or a user. No configuration sets `shareProcessNamespace` — the dashboard-enabled one used to — and the sidecar runs as its own UID, so the sandbox cannot read the sidecar's environment out of `/proc`. [`docs/security-requirements.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/security-requirements.md) tracks both that requirement and the Pod-sharing limitation above formally.

## Splitting the broker into its own Pod

`spec.security.splitCredentialBrokerPod` renders the credential runtime as a Deployment and Service of its own instead of a sidecar. It closes the shared-network-namespace limitation above: with the broker in another Pod, "reachable on `127.0.0.1`" is no longer what decides who may spend the agent's credentials, and a NetworkPolicy can deny the sandbox the metadata server without denying it to the broker — which is what [`spec.security.egressPolicy`](#denying-the-sandbox-the-metadata-server) then does, and cannot do without this flag.

**It defaults to `false`, and it requires ReadWriteMany storage.** This is the blocker, not a footnote. The broker runs proxied commands with a working directory the agent created on the shared data volume — a leased git clone, a Cluster Agent profile home, a `.kubeconfigs` directory are all written by one process and used by the other. Both Pods therefore have to mount the `<name>-data` claim read-write at the same path and see the same files there. The default GKE persistent disk is ReadWriteOnce and cannot do that across two Pods on different nodes; the cluster needs Filestore or GCS Fuse, and a storage class such as `standard-rwx`, provisioned before the flag is set. The access mode of an existing claim cannot be changed in place, so this means new storage rather than an edit.

**What going without it actually looks like** is a scheduling failure, not a policy refusal. The broker Pod cannot attach the volume, stays `Pending` with a `Multi-Attach error for volume`, and never becomes a Service endpoint — so every proxied command in the sandbox reports `credential proxy unavailable: [Errno 111] Connection refused`. That is the same symptom an unhealthy sidecar produces, which is why [Troubleshooting](#troubleshooting) below now lists both causes. The operator logs a warning naming the claim and its access modes when it sees a non-RWX volume under an enabled flag. Note that the broker's own workspace-containment check will not catch this: it compares paths, both Pods are configured with the same workspace root, so the path always looks correct — what is missing is the data behind it. And if the scheduler happens to place both Pods on one node, it will appear to work, which makes the misconfiguration intermittent across restarts.

When the flag is on:

- The broker becomes `<name>-credential-proxy`, a single-replica Deployment with a Service on 8765. The agent's `CREDENTIAL_PROXY_URL` and the two chat relay URLs address that Service.
- **The call is authenticated.** The agent presents a projected ServiceAccount token with the audience `kubeagents-credential-proxy`; the broker verifies it with a Kubernetes `TokenReview` and refuses anything else with `401`. This is not optional plumbing — the sidecar layout's access control was the loopback listener and a `0600` socket, and both of those are properties of sharing a Pod.
- The front door for the PlatformAgent API stays in the agent Pod as an `agent-api-proxy` container. It forwards to port 8642 on loopback behind a fixed non-secret sentinel, which is only safe because it never leaves the Pod, so it does not follow the broker across the boundary.

Two things it does not do, and both are deliberate:

- **The two Pods share a ServiceAccount.** The Workload Identity IAM binding names it, so giving the agent one of its own would take the broker's cloud credentials with it. The identity the broker verifies is therefore "a Pod running as this ServiceAccount" — enough to exclude the rest of the cluster, not enough to tell the agent Pod from the broker Pod.
- **The token crosses the network in cleartext**, as the [Minty](/kube-agents/deploy/token-minter/) call already does. Anyone who can observe pod-to-pod traffic in the namespace can replay it until it expires. It is audience-bound, so it is useless against the Kubernetes API or any other service, and it is worth at most an hour. mTLS is the fix and is not deployed.

### The agent now holds a credential, and that was a choice

Under the sidecar layout the sandbox holds nothing at all — the broker's trust comes entirely from the socket. With the flag on, the projected token is mounted into the `platform-agent` container itself, so the model can read it. A prompt-injected agent gains no new authority _inside_ the Pod, because it could already call the broker by running `kubectl`. What it gains is **exportability**: the token is a file, and a file can be exfiltrated, after which an outside party has broker access — bounded by the command policy — until the token expires.

Against the credential requirements this mechanism is short-lived, audience-bound and independently revocable, but **not non-exportable**, and that last clause is the one it misses.

**An alternative was considered and deferred.** This same change already contains the pattern that would avoid it: `agent-api-proxy` is a credential-holding container in the agent Pod, on loopback, at a different UID, with no volumes the sandbox can read. A mirror image of it — an egress forwarder in the agent Pod that holds the token, listens on `127.0.0.1:8765`, and attaches the credential on its way out to the broker Service — would keep the wrappers unchanged and preserve "the sandbox holds no credential at all". It is not built here because it is a new component with its own failure modes, lifecycle and review surface, and this change was scoped to the split and its transport. It remains the obvious next step for anyone hardening this, and nothing in the current design forecloses it: the client's `authorization_headers()` would simply return nothing and the forwarder would supply the header instead.

## Denying the sandbox the metadata server

`spec.security.egressPolicy: Allowlist` renders one NetworkPolicy on the agent Pod: default-deny egress, with rules for DNS, the credential broker, LiteLLM, the managed OpenTelemetry collector, and whatever `spec.security.egressAllowlist` adds. The metadata server is denied by not appearing on that list.

### Whether this is actually closed, plainly

Inside the configuration below it is closed **structurally** — it does not depend on the agent choosing not to ask. Getting into that configuration is not structural, and **the default install is not protected at all**. Four conditions, and only the first is enforced by the operator:

1. **`splitCredentialBrokerPod` must be `true`, and it defaults to `false`.** This is the whole reason the two features are one story. A NetworkPolicy selects Pods, never containers, and the broker reaches the metadata server on purpose — minting the cloud token is its job. With the broker still a sidecar, the same policy governs both containers, so denying the sandbox means denying the broker and every proxied command fails. **Asking for `egressPolicy: Allowlist` without the split is refused**, not quietly downgraded: the agent goes `Degraded` with reason `EgressPolicyRequiresSplitBroker`, no policy object is written, and reconciliation stops before the workload. A control that appears in `kubectl get netpol` and protects nothing is worse than no control.

2. **The cluster CNI must enforce NetworkPolicy, and the operator cannot tell whether it does.** An unenforced policy is accepted, stored, and returned by `kubectl get` exactly like an enforced one; there is no field, condition or event to read. GKE Autopilot and GKE Dataplane V2 always enforce and cannot be configured not to. A GKE Standard cluster created without network policy gets a no-op.
3. **No other policy may widen it.** NetworkPolicies selecting the same Pod are additive and the API has no deny rule, so a namespace-wide `allow-all-egress` silently reopens everything this closes. Nothing detects that.
4. **The allowlist has to stay complete**, and it deliberately is not — see [What it breaks](#what-it-breaks-which-is-not-a-short-list). Every gap is pressure toward a broader rule.

### What a refusal does, and does not do

**Refused means not reconciled, not stopped.** On a new agent those are the same thing — no Deployment is created. On an agent that is already running, the existing Pods keep running exactly as they were, **with metadata access**, and every subsequent change to the resource is ignored while the operator retries every 30 seconds. The refusal protects you from believing you have the control; it does not take the workload down to make the point. `kubectl describe platformagent` is where you find out.

The two refusal reasons differ in one way that matters:

- `EgressPolicyRequiresSplitBroker` renders **no** policy. The objection is to the policy existing at all in that layout, since it would govern the credential broker in the same Pod.
- `EgressAllowlistRefused` **still renders the policy**, minus the destinations it refused. The objection is to one value, not to the control, so the guardrail keeps being reconciled — delete it and the next pass puts it back — while the status stays `Degraded` until the spec is fixed.

### Turning it back off, which has an order

**Setting `egressPolicy: None` does not delete the policy.** The operator will not remove a guardrail it may not have created, so `<name>-sandbox-metadata-deny` stays in the namespace after the field goes off. On its own that is fail-closed and fine: the Pod it selects keeps a door shut that nothing is asking to open.

It stops being fine the moment `splitCredentialBrokerPod` goes off too, and the tempting way to get there is exactly the wrong one. Reverting the split _alone_ is refused — the agent goes `Degraded` with `EgressPolicyRequiresSplitBroker` — so the obvious next move is to clear `egressPolicy` in the same edit and unstick it. Do that and the broker comes back into the agent Pod, where the leftover policy is still selecting it, and the policy has never listed the metadata server.

**What that produces is a crashloop, not a quiet failure.** The Pod template changed, and at the default single replica the deployment strategy is `Recreate`, so the running Pod is deleted before the replacement starts. The new `envoy-credential-proxy` container runs its bootstrap command before it serves anything — a `gcloud container clusters get-credentials`, rendered whenever `spec.harness` names a project, location and cluster, which the Helm chart requires of every install. That command needs the metadata server and `container.googleapis.com`, and the leftover policy denies both. The bootstrap raises, the credential runtime exits, the sidecar entrypoint's `wait -n` takes the container down with it, and the Pod lands in `CrashLoopBackOff`. `ReadyReplicas` never reaches 1, so the agent sits in phase `Provisioning` with `Ready=False` and the message `Waiting for deployment replicas to be ready`.

That status is true and unhelpful — it describes a Pod that will not start, and says nothing about a NetworkPolicy. The diagnosis is in the sidecar's own log, where `gcloud` reports that it cannot reach the metadata server. **If an agent starts crashlooping right after you turned these fields off, check for a leftover `<name>-sandbox-metadata-deny` before anything else.**

An agent whose `spec.harness` omits the cluster fields is the one case that behaves as you might expect instead: no bootstrap command, so the sidecar starts cleanly, the Pod reports `Ready`, and every proxied command fails at the network. The chart cannot produce that CR, but a hand-written one can.

Revert in three steps instead, which never leaves the broker inside a policy that denies it:

1. Set `egressPolicy: None`, leaving `splitCredentialBrokerPod: true`.
2. `kubectl -n NAMESPACE delete networkpolicy NAME-sandbox-metadata-deny`. This is safe only after step 1 — delete it while the field still says `Allowlist` and the next reconcile puts it straight back.
3. Set `splitCredentialBrokerPod: false`.

### Pre-enable checks

Two things to verify on the cluster before turning this on, neither of which the operator can check for you:

- **Does the CNI enforce NetworkPolicy?** `gcloud container clusters describe CLUSTER --format='value(networkPolicy.enabled,networkConfig.datapathProvider)'`. Autopilot and Dataplane V2 always do. A GKE Standard cluster created without network policy gets a policy object that enforces nothing.
- **Does the cluster run NodeLocal DNSCache, and does DNS still resolve after enabling?** This one can take the agent down. NodeLocal DNSCache runs with `hostNetwork`, so on Cilium and Dataplane V2 its traffic carries a host or remote-node identity — and neither the `k8s-app: node-local-dns` Pod selector nor the `169.254.20.10/32` CIDR peer in the rendered rule is guaranteed to match that, because CIDR peers do not select node identities unless `policy-cidr-match-mode` includes `nodes`, which is off by default. Both peers work on an iptables dataplane, which is why they are rendered. If neither matches, DNS is blocked outright and every destination in the allowlist becomes unreachable, because they are all reached by name. Check with `kubectl -n kube-system get ds node-local-dns` first, and after enabling confirm resolution from the agent container before trusting the policy.

### What it breaks, which is not a short list

The allowlist covers DNS, the broker, LiteLLM and the OTel collector. Everything the agent container reaches on its own goes away:

- the `web` toolset (DuckDuckGo) and the `browser` toolset (headless Chromium), both enabled on the `platform` and `cluster-*` profiles;
- the MCP servers that call `container.googleapis.com` and `developerknowledge.googleapis.com`;
- `github.com` reached directly from the sandbox;
- the GKE metadata lookups in the hourly cluster-agent reconcile. Those fail soft — the code catches the error and falls back — but the management cluster stops being excluded from profile creation. Set `RECONCILE_PROJECT` and `RECONCILE_EXCLUDE` to restore what they were for.

Credentialed `gcloud`, `kubectl`, `gh` and `git` are unaffected: they are wrappers that call the broker, and the broker is on the list.

None of that is accidental. A headless browser with unrestricted egress **is** the exfiltration path, so the capabilities this removes are the same ones that make the control worth having. Weigh it as a trade rather than a regression.

### Restoring a destination

`spec.security.egressAllowlist.extraRules` takes NetworkPolicy egress rules verbatim. Two things to know:

- **NetworkPolicy matches addresses, never DNS names.** Restoring a hosted service means naming its published address ranges and keeping them current.
- **A rule whose `ipBlock` contains a metadata address is dropped, not narrowed.** That includes `0.0.0.0/0` with an `except` clause naming the metadata server — see below for why an `except` clause is not a block. To grant broad egress you have to carve the ranges around `169.254.169.252` and `169.254.169.254` yourself, in the spec, where a reviewer can see it.

`spec.security.egressAllowlist.controlPlaneCIDRs` is separate because there is no NetworkPolicy peer for "the Kubernetes API server": on GKE the control plane is not a Pod and not in the cluster, and the in-cluster `kubernetes` Service address is translated to it before policy is evaluated. Left empty the rule is simply absent, and the `event-watcher` sidecar loses its API-server connection, so cluster events stop reaching the agent. Find the range with `gcloud container clusters describe CLUSTER --format='value(privateClusterConfig.masterIpv4CidrBlock,endpoint)'`.

### Why it is default-deny rather than "allow everything except the metadata server"

The obvious shape — one broad rule with the metadata address in an `except` clause — is what this repository shipped once before, and it is unsound twice over on GKE:

- **On GKE Dataplane V2 it is a near-total outage, not a permissive rule.** Google's documentation states that Pod traffic is never covered by an `ipBlock` rule, so a policy whose only peer is `0.0.0.0/0` permits no Pod-to-Pod traffic at all — not kube-dns, not LiteLLM, not the broker.
- **On an iptables dataplane the `except` clause names an address the policy never sees.** A request to `169.254.169.254:80` is translated to the node-local metadata server at `169.254.169.252:988` in NAT PREROUTING, before the filter rules run. This is [kubernetes/kubernetes#68078](https://github.com/kubernetes/kubernetes/issues/68078), open since 2018 and titled "Network policy not properly blocking GKE metadata IP".

Default-deny sidesteps both: it names Pods with selectors rather than CIDRs, and it does not have to predict which address the request was rewritten to, because neither address is permitted. For the same reason all three metadata addresses — `169.254.169.254`, `169.254.169.252` and the IPv6 `fd20:ce::254` — are refused in `extraRules`.

It is also why this is one policy object and not two. There is no deny rule in NetworkPolicy; a separate "everything except metadata" policy would not subtract the metadata server from an allowlist, it would add the internet to it.

### Turning it off does not delete the policy

Setting `egressPolicy` back to `None` leaves any rendered policy in place. The controller does not delete guardrails — that is the rule it broke for years by removing `<name>-sandbox-metadata-deny` on every reconcile — and a stale NetworkPolicy fails closed where a stale Deployment does not. Remove it with `kubectl delete networkpolicy <name>-sandbox-metadata-deny` when you mean to.
What the two containers do **not** share is a process namespace or a user. No configuration sets `shareProcessNamespace` — the dashboard-enabled one used to — and the sidecar runs as its own UID, so the sandbox cannot read the sidecar's environment out of `/proc`. [`docs/security-requirements.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/security-requirements.md) tracks both that requirement and the Pod-sharing limitation above formally.

## Troubleshooting

**Every CLI in the sandbox reports `credential proxy unavailable`.** The `gcloud`, `kubectl`, `gh`, and `git` commands inside `platform-agent` are wrappers that forward to the broker. When nothing is listening at the other end, all four fail the same way:

```text
credential proxy unavailable: [Errno 111] Connection refused
```

This is an availability problem rather than an authentication one — a rejected credential comes back as an HTTP error, not a refused connection. There are two causes, and which applies depends on whether the broker is a sidecar or a Pod of its own.

_Sidecar layout (the default)._ The sidecar is not listening. Inspect it rather than the CLI:

```bash
kubectl get pods -n kubeagents-system
kubectl logs -n kubeagents-system deploy/platform-agent-gateway -c envoy-credential-proxy
```

_Split layout (`spec.security.splitCredentialBrokerPod: true`)._ The wrappers cross a Service, so the same message also means the broker Pod has no ready endpoint. The most common reason is the ReadWriteMany prerequisite above being unmet: the Pod cannot attach the shared volume and sits `Pending`. Check the endpoint first and the events second — `Multi-Attach error for volume` is the tell.

```bash
kubectl get endpoints -n kubeagents-system platform-agent-credential-proxy
kubectl describe pod -n kubeagents-system -l app=platform-agent-credential-proxy
kubectl logs -n kubeagents-system deploy/platform-agent-credential-proxy
```

**Diagnostics run inside the Pod are misleading while the sidecar is down.** Those wrappers are the only `gcloud` and `kubectl` the sandbox has, so the commands you would normally reach for return the same connection error instead of describing the Pod's identity. Test that identity from a throwaway Pod using the same ServiceAccount:

```bash
kubectl run wi-check -n kubeagents-system --rm -it --restart=Never \
  --image=google/cloud-sdk:slim \
  --overrides='{"spec":{"serviceAccountName":"kubeagents-platform-agent"}}' \
  -- gcloud auth print-access-token
```

**The sidecar exits during startup.** The credential runtime runs `CREDENTIAL_PROXY_BOOTSTRAP_COMMAND` before it begins serving, and a non-zero exit stops the container — the Pod then crashloops while the other containers stay healthy. The command's stdout and stderr are written to the sidecar's log, so `kubectl logs -c envoy-credential-proxy` carries the reason. Bootstrap failures usually mean the Pod cannot reach the cluster or mint a token; see [Security & IAM](/kube-agents/reference/security-and-iam/) for the Workload Identity binding it depends on.

## Where to go next

- [Security & IAM](/kube-agents/reference/security-and-iam/) — Workload Identity, the GCP permission sets, and the read-only Kubernetes RBAC.
- [Token minter (Minty)](/kube-agents/deploy/token-minter/) — short-lived GitHub App tokens via KMS.
- [Full design doc](https://github.com/gke-labs/kube-agents/blob/main/docs/credential-isolation-design.md) — scope, deny policy, migration, and CI verification assertions.
