# PlatformAgent Credential Isolation

## Summary

### Goal

The PlatformAgent sandbox container must not receive API keys, access tokens,
refresh tokens, or Kubernetes ServiceAccount tokens through its environment or
filesystem.

Credential values returned by an approved tool call are an accepted risk. For
example, an approved `gcloud auth print-access-token` response may expose a
token to the agent. Preventing that disclosure is not part of this design.

### Design

Each PlatformAgent runs as three Pods.

The **gateway Pod** holds the harness and nothing credentialed:

1. `platform-agent`: the untrusted agent sandbox.
2. `platform-agent-dashboard`: the optional local dashboard.
3. `fluent-bit`: log forwarding.
4. `agent-api-auth`: the PlatformAgent API authenticator and the
   `k8s-event-watcher`, which forwards cluster events using a non-secret
   internal key. It holds no credential path.

The **shell sandbox Pod**, `<agent>-shell`, runs `sshd`, the agent's own tools, a
durable `/opt/data`, and the shims that stand in for `gcloud`, `kubectl`, `gh`, and
`git`. This is the Pod that executes anything the model wrote. Its ServiceAccount
carries no `iam.gke.io/gcp-service-account` annotation, so the metadata server hands it
an unbound principal that IAM grants nothing.

The **credential broker Pod**, `<agent>-credential-proxy`, runs Envoy, the real CLIs,
and the Slack and Google Chat relays, and is reached over a ClusterIP Service on port
8765 rather than loopback. Nothing in it executes anything the model wrote. The broker
is always its own Deployment; there is no configuration that puts it back in the gateway
Pod or in the sandbox Pod, and none that turns the sandbox off.
[`designs/agent-shell-sandboxing.md`](designs/agent-shell-sandboxing.md) is canonical for
why the three-Pod shape is what it is.

`agent-api-auth` is a **native sidecar** — an entry in `initContainers` carrying
`restartPolicy: Always` — and not an ordinary container, so
`kubectl get pod -o jsonpath='{.spec.containers[*].name}'` will not list it. It is shaped
that way because it owns port 8643, which the PlatformAgent API Service targets, and it
shares a network namespace with the sandbox container: as two ordinary containers they
started together and raced for the bind, and the sandbox could take the port external
callers reach. A native sidecar starts before any app container, which narrows that race
but does not close it: the kubelet waits for the sidecar to have **started**, plus its
`startupProbe` if it declares one, and this container declares only a `readinessProbe`.
So the ordering guaranteed is of process creation, not of the listener having bound 8643.
Giving the container a `startupProbe` on that port would close it; see
`buildPodTemplateSpec` in the operator.

Requires Kubernetes 1.29+, where `SidecarContainers` is beta and enabled by
default. It is alpha and off in 1.28, and GA in 1.33.

On 1.28 the install fails rather than degrading. The API server strips
`restartPolicy` from the init container, which leaves it declaring a readiness
probe that a non-restartable init container may not have, so the pod template is
rejected and the apply fails. The chart's `kubeVersion` refuses the install before
that point.

A shim in the sandbox sends a structured argument vector to Envoy at
`CREDENTIAL_PROXY_URL`. Envoy forwards it over a private Unix socket to the credential
runtime in the same Pod. Slack and Google Chat use the same relay.

Only trusted containers receive projected Kubernetes ServiceAccount (KSA) tokens. The
credential runtime receives secret environment variables, credential state, and its
identity token. The `agent-api-auth` sidecar receives a separately-audienced
Kubernetes-API token, CA, and namespace projection, which the event watcher it hosts uses
to reach the management cluster. Neither is mounted in the sandbox or dashboard. The
`platform-agent` container does hold one credential — a third, audience-bound projected
token it presents to the broker across the network, which buys nothing anywhere else.
`agent-api-auth` also authenticates callers of the PlatformAgent API before forwarding
requests with a non-secret internal sentinel. Pod-wide automatic KSA token mounting is
disabled.

### Guarantee

The operator does not place managed credentials in the sandbox Pod's:

- environment;
- root filesystem;
- persistent data volume; or
- Pod identity — its ServiceAccount is bound to no Google service account, so the
  metadata server has nothing to give it.

Two containers mount a projected ServiceAccount token on purpose, and both projections
carry the broker's audience rather than the Kubernetes API's: the gateway's
`platform-agent` container and the sandbox's `shell` container. Each presents it to the
broker to be let past the listener's authentication, which is what
`CREDENTIAL_PROXY_TOKEN_FILE` names. The API server rejects a token minted for another
audience, so neither is a Kubernetes credential and neither undoes the Pod's
`automountServiceAccountToken: false`. The sandbox's is projected 0444 because uid 1000
reads it; `buildShellSandboxCredentialProxyTokenVolume` says why that gives away nothing
the container is not already holding.

The shared agent volume used to be the live gap here: a `core.fsmonitor` entry the
sandbox wrote under a workspace root ran in the credential holder on the next
`git status`, with no lease taken and no mutating verb, so neither the workspace-lease
floor nor the argument-level deny policy reached it. Separate Pods close it. There is no
volume both sides mount, the broker owns the only checkout, and the skills that write to
a forge hand it file content and a commit message rather than a directory.

`spec.deployment.env` is applied to the credential runtime because it may
contain credentials. A short allowlist may also be copied to the sandbox — the
four OpenTelemetry settings and the three `ALERT_DAILY_LIMIT_*` alert ceilings —
but only as literal values; all `valueFrom` sources are rejected. A name earns a
place on that list only if an arbitrary value for it cannot redirect state,
grant access, or change what code runs; `safeSandboxEnvOverrides` in
`k8s-operator/internal/controller/platformagent_manifests.go` is the list.
Reserved proxy, runtime-loader, and shell-startup variables cannot override the
operator's managed values.

### Limitation

The gateway Pod keeps a cloud identity. It and the broker Pod both run as
`kubeagents-platform-agent`, whose KSA carries the `iam.gke.io/gcp-service-account`
annotation, so anything with execution in the gateway can mint the Google service
account's token from `169.254.169.254`. The Pod that runs code the model wrote cannot,
which is the property this design is about; what is left is trusted code holding more
than it needs. Two ways to close it: configure
`spec.security.workloadIdentityFederation`, which gives the broker a credential source
that is a file in its own Pod, or give the broker a ServiceAccount of its own and take
the annotation off the gateway's. Neither ships today.

`spec.security.egressPolicy: Allowlist` renders a default-deny egress NetworkPolicy on
the gateway Pod that leaves the metadata server's credential API off its allowlist — the
address itself is permitted on port 53, where under Cloud DNS for GKE it is the Pod's
resolver — and it does not close that path either. Adding a NetworkPolicy is monotone —
policies selecting one Pod are unioned and the API has no deny rule — and the gateway Pod
is already selected for egress
by the `<agent>-gateway-netpol` this same operator renders (unless
`spec.networkPolicy.enabled: false` withholds it — on a Helm install the one shape where
the allowlist stands alone and enforces; a Kustomize install's static
`platform-agent-core-egress` still selects the same Pod), which permits the metadata
path. So enabling the allowlist widens what the Pod may send and narrows nothing; it is
an auditable object rather than a control until that gateway policy is narrowed. It would
in any case do nothing on a cluster whose CNI does not enforce NetworkPolicy. See
[Denying the sandbox the metadata server](site/src/content/docs/reference/credential-isolation.md#denying-the-sandbox-the-metadata-server).

Nothing tells the gateway from the sandbox. The broker authenticates every caller with a
`TokenReview` over an audience-bound projected token, but `CREDENTIAL_PROXY_ALLOWED_CALLERS`
names both ServiceAccounts and no policy varies on which one presented it. The broker
records the principal; nothing reads it yet.

## Scope

### In scope

- PlatformAgent only.
- Credentials managed by the operator.
- CLI forwarding for `gcloud`, `kubectl`, `gh`, and `git`.
- Slack and Google Chat credentialed relays.
- PlatformAgent API bearer-key termination in the `agent-api-auth` sidecar.
- GitHub installation tokens minted through Minty.
- Broker health, lifecycle, rollout, and migration from the former in-Pod sidecar.

### Out of scope

- Preventing credentials from appearing in approved command or tool output.
- Preventing an approved credentialed command from deliberately copying a
  credential into content it returns. Such credential disclosure and tool side
  effects require a separate approval/policy design.
- Preventing the gateway Pod from reaching the Pod identity it still shares with
  the broker, or the metadata server behind it.
- Arbitrary user-supplied init containers, sidecars, volumes, and mounts. These
  are trusted configuration and may intentionally weaken isolation.
- OperatorAgent and DevTeamAgent.
- General data-exfiltration prevention.

## Architecture

```text
PlatformAgent gateway Pod

  platform-agent
    credential-free env and mounts
    CLI wrappers / chat adapters
              |
              | HTTP to the credential-proxy Service on :8765
              v
credential-proxy Pod

  envoy-credential-proxy
    Envoy listener
              |
              | private Unix socket
              v
    credential runtime
    real CLIs, Slack/Chat clients, Minty client
    secret env, KSA token, private temporary state
```

Envoy is the only listener for credentialed tool and chat requests. The
credential runtime listens on a Unix socket mounted only in its own Pod, so no
caller can bypass Envoy by reaching the runtime directly. Envoy authenticates
every caller that is not asking for `/healthz`: the caller presents an
audience-bound projected ServiceAccount token (audience
`kubeagents-credential-proxy`, one hour) as a bearer header, and the runtime
verifies it with a `TokenReview` against `CREDENTIAL_PROXY_ALLOWED_CALLERS`. That
list names the gateway's ServiceAccount and the sandbox's, and no policy varies
on which one presented the token, so the check keeps other workloads out rather
than telling those two apart. The token crosses the cluster network in cleartext;
a NetworkPolicy is what keeps it off the wire elsewhere.

The `agent-api-auth` sidecar authenticates the existing PlatformAgent API on port
8643 and forwards to the agent on loopback using a non-secret sentinel. It is a
native sidecar in the gateway Pod, so the kubelet starts it before every app
container and stops it after them, and an unready sidecar makes the Pod unready.
It also hosts the `k8s-event-watcher`, which is why splitting the broker out
strands nothing.

The broker has a lifecycle of its own now — a Deployment, its own rollout, and
its own readiness. A broker that is down does not hold the agent Pod in
`Init:CrashLoopBackOff`; it makes every proxied command report the credential
proxy as unavailable, which is a failure mode to read in the agent's logs rather
than in the Pod's status.

## Credential Placement

| Data                             | Sandbox                | Credential runtime          |
| -------------------------------- | ---------------------- | --------------------------- |
| `spec.deployment.env`            | No                     | Yes                         |
| Slack tokens                     | No                     | Yes, Secret-backed env      |
| PlatformAgent external API key   | No                     | Yes, Secret-backed env      |
| Session KV API key and HMAC salt | Yes, Secret-backed env | Yes, API key only           |
| Automatic KSA token mount        | Disabled               | Disabled                    |
| Explicit projected KSA token     | Broker audience only   | Read-only, one-hour token   |
| gcloud/kubectl configuration     | No                     | Private `emptyDir`          |
| GitHub installation token/cache  | No                     | Private `emptyDir`          |
| Agent workspace                  | Yes                    | No, the broker owns its own |

### The loopback-only exception

`SESSION_KV_API_KEY` and `SESSION_KV_SALT` are the only Secret-backed values the
sandbox receives, and they are the exception that proves the rule rather than a
relaxation of it. Both are pod-scoped: they authenticate and pseudonymise
nothing outside this Pod, and neither grants access to any external system, so
an agent that reads them out of its own environment gains nothing it did not
already have.

They cannot go behind the proxy, because the sandbox is not the client — it is
the server. `session_kv_server.py` runs in the sandbox and binds
`127.0.0.1:8699`; its callers are the event watcher in `agent-api-auth`,
the Platform MCP server, the `incident_context` plugin, and the gateway's
kanban notifier, which keys a delivered triage report to the thread it went
into. The key exists so
that the server can reject a request that did not come from one of them, which
means the server has to hold it. The salt is read by the Chat Agent plugins,
which also run in the sandbox, before any identity is written to disk; hashing
it anywhere else would mean shipping the plaintext address out of the sandbox
first, which is exactly what it exists to prevent.

Deliberately _not_ `API_SERVER_KEY`: that value is the non-secret loopback
sentinel `cluster-internal-trusted`, so reusing it here would authenticate
nothing. Both keys are optional in the CRD, so a Secret without them yields
containers without the variables rather than a pod that will not start. What
that costs is worth stating precisely, because one of the three consequences is
not a degradation: the `k8s-event-watcher` in `agent-api-auth`
authenticates to the Session KV server with `SESSION_KV_API_KEY` and treats an
empty value as fatal, so it exits on every start and **no cluster events are
watched at all** — silently, since the container stays Ready and no probe covers
the watcher. The other two are degradations: the Session KV server refuses every
authenticated request with a 503 and says why, and identity hashing falls back
to a per-process random salt with one warning.

The projected token uses the audience `kubeagents-credential-proxy`, expires
after one hour, and is mounted at
`/var/run/secrets/kubeagents/serviceaccount/token` in the credential runtime. A token
with the same audience is also mounted read-only in the `platform-agent` container,
because that is how the agent authenticates to a broker that is not on loopback.
The event watcher has a separate one-hour token with the Kubernetes API's
default audience, plus the cluster CA and Pod namespace, mounted at the
conventional in-cluster path in `agent-api-auth`. Two differently
audienced tokens therefore sit side by side in the gateway Pod: the broker-audience one,
which the Kubernetes API will not accept, and the watcher's, which it will. Neither is
shared with the sandbox or dashboard.
Deleting a default token during startup is intentionally not used: projected
tokens rotate, and mount-time exclusion is reliable.

## Request Paths

### CLI commands

The sandbox image contains wrapper binaries instead of credential-aware CLI
binaries. A wrapper sends the executable name and argument array to the local
proxy. The credential runtime directly executes the corresponding real CLI and
returns output and exit status. It never evaluates an agent-supplied shell
command.

Only `gcloud`, `kubectl`, `gh`, and `git` are accepted. The proxy also rejects
known credential-disclosure, credential-replacement, and self-modification
operations, and the GitHub **write** path: merging a pull request
(`github.merge`), approving a review (`github.assent`), mutating through the
REST API (`github.api-mutation`), triggering workflows or releases
(`github.pipeline-trigger`), and repository administration — secrets,
variables, and repository deletion, archiving or editing
(`github.repo-administration`). Rulesets are not in that last list because
`gh ruleset` cannot change one: it has only `check`, `list` and `view`.
Reshaping a ruleset goes through `gh api`, so `github.api-mutation` is the
rule that refuses it and the id an operator will see.
Those five exist because the agent is the proposer: the review gate is only a
gate if the thing that opens a pull request cannot also merge it. Pipelines and redirections are interpreted by the sandbox shell
around an individual wrapper invocation, so they cannot execute inside the
credential broker. Requests that cannot be represented safely fail closed,
including:

- interactive TTY programs and password prompts;
- arbitrary binary or unbounded streaming input/output;
- file paths that refer to sandbox-only files;
- background processes or commands that outlive the request; and
- commands exceeding request, output, or timeout limits.

Standard input and full-duplex streaming require a future bounded protocol; the
wrapper does not silently consume an inherited protocol stream.

The current deny policy applies regular expressions to a normalised rendering of
the argument vector and permits flags before or between subcommands. Three
normalisations, and all of them matter for predicting what a rule will match:

- **Free-text flag values are dropped**, so prose the agent wrote is not searched
  as though it were a command path. Without this a pull request body containing
  the word "merge" tripped `github.merge`, which refused the product's own
  GitOps suggestions. The flag names remain, since a rule may key on one.
- **Attached shorthand values are split**, so `-XPUT` reads as `-X PUT`. gh,
  kubectl and gcloud are all Cobra/pflag, which accepts a shorthand's value with
  no separator, and a rule written for the separated spelling would otherwise
  miss it.
- **A shorthand buried in a cluster keeps its dash**, so `-iX PUT` reads as
  `-i -X PUT`. pflag also accepts a boolean shorthand and a value-taking one in
  the same token, and splitting only the first one off left the `-X` a rule
  matches on as a bare letter — enough for `gh api -iX PUT …/merge` and
  `gh auth status -at` to get through. Only the shorthands a shipped rule keys
  on are re-dashed, and the walk stops at the first non-letter, since
  everything from there is somebody's value rather than another flag.

A value that looks like a flag is never dropped: the free-text set is applied
without knowing the subcommand, and a name on it is not always value-taking.

This is an interim policy mechanism, not a general shell parser. If the policy grows
beyond these narrowly defined commands, it should use tool-specific argument
parsers over the structured argument vector.

### git configuration

A kubeconfig is not the only executable configuration the agent can author.
`git` selects both its transport and several helper programs from configuration
files, and it reads those from the working tree it runs in as well as from the
credential runtime's own home directory. Left at its defaults, a `git` the
sandbox requested can therefore name a program for the credential runtime to
execute, without the argument vector containing anything the deny policy would
match — the argv is only ever `git commit`.

The runtime consequently overrides those defaults for every command it runs,
through the environment rather than through a configuration file, so that the
settings cannot be edited by anything holding the volume:

- the transport allowlist is restricted to `https`, which git honors above the
  equivalent setting from every configuration file, including one supplied on
  the command line;
- the system configuration file is suppressed and the global configuration file
  is pinned to a path inside the runtime's private `emptyDir`. It is pinned
  rather than disabled because `gh auth setup-git` installs the GitHub
  credential helper by writing that file;
- the hooks directory is pinned to an empty, non-writable directory, which also
  neutralizes hooks installed into a fresh clone from a template directory;
- the filesystem monitor, which names a program and is invoked by a read-only
  verb, is disabled;
- commit and tag signing are disabled, and the signing program is set to a
  command that fails — for every signature format, not just the default one.
  Signing runs a program named in configuration, and the trigger is an ordinary
  `git commit`, so like the hooks pin above it its absence is reachable with no
  unusual argument at all. One pin is not enough here: git supports three
  signature formats, `gpg.format` is settable from the repository's own
  configuration, and each format reads its own program key, so pinning only the
  openpgp one leaves `[gpg] format = ssh` with `gpg.ssh.program` — and
  `gpg.ssh.defaultKeyCommand`, which needs no signing key configured at all —
  reaching a command through `git commit -S` and `git tag -s`. All four keys
  are pinned. The set is closed, which is what separates this from the
  arbitrary-name keys in the limitation below: three formats, three fixed key
  names. The `-S`/`-s` flags themselves are not refused, and need not be;
- subcommand autocorrection is disabled. This one is not defence in depth but a
  precondition for the refusal list below: with autocorrection enabled from a
  repository's configuration, a misspelled subcommand resolves to the real one,
  and a list that compares whole tokens matches nothing; and
- both editors git launches — the message editor and the rebase sequence editor
  — are set to a command that does nothing and fails. They are set through the
  environment for the same reason as the transport allowlist: those two
  variables outrank the equivalent configuration setting from every file and
  from the command line. Nothing is lost, because the runtime has no terminal
  and so a command that needs an editor could never have succeeded.

Only settings whose disabled value is a working value are pinned this way. There
is no value of `diff.external` that means "no external diff" — git executes an
empty value — so pinning it replaces one code-execution setting with a `git diff`
that always fails, and it is deliberately not pinned.

The runtime additionally refuses, in the argument vector, the global options that
would undo the above: `-c` and `--config-env`, which set configuration ranking
above the pinned values; `--exec-path`, which selects the directory git executes
`git-<subcommand>` from; `--git-dir` and `--work-tree`, which identify a
repository directly and so bypass the containment check applied to the request's
working directory; and `--global`, `--system` and `--file`, which write the
configuration files being pinned. `--file` names its target explicitly and the
target is not a secret — `git config --list --show-origin` prints it — so
refusing the first two without the third would have closed nothing; it is also
an unrestricted write to any path, since the containment check inspects the
request's working directory and not this. Its short spelling `-f` is refused
only when `config` appears in the same argument vector, because on every other
subcommand `-f` is `--force`, which the skills issue. `-C` remains accepted
because the containment check resolves
it, and repository-local `git config` remains accepted because that is how a
clone's commit identity is set. Also refused are the subcommands whose function
is to execute a caller-named command — `bisect` (`bisect run`), `difftool`
(`--extcmd`), `mergetool`, `filter-branch` (`--tree-filter`), `send-email`
(`--smtp-server`), `instaweb`, `web--browse`, `help`, `fast-import`, the `p4`
and `svn` bridges, `interpret-trailers`, and `submodule foreach`.

The documentation viewer takes two entries rather than one, because it has two
triggers. `git help -m <page>` executes `man.<man.viewer>.cmd` through a shell
and `git help -w` does the same through `web.browser` and `browser.<tool>.cmd`,
which the `help` subcommand entry covers. But `git <any-verb> --help` is
dispatched to that same viewer with the verb still in the subcommand slot, so
`git status --help` reaches it while nothing in the argument vector is `help`.
The `--help` option is therefore refused as well, and both are needed: refusing
either alone leaves the other open. The keys carry an arbitrary name and so
cannot be pinned, and the cheapest sequence that reaches them is three ordinary
requests taking no lease at all — two repository-local `git config` writes and a
read verb. `-h` is not refused: git answers it from the subcommand's own option
table and prints usage without dispatching to a viewer. `web--browse` stays on
the subcommand list on its own account, because it is directly invocable and
runs the configured browser command; it never covered the `git help -w` route,
which reaches that code internally without the token appearing in the argument
vector.

The same category appears as options on subcommands the product has no reason to
refuse outright, so those options are refused instead: `--exec` and `-x`, which
run a caller-named command once per commit during a rebase; `-O` and
`--open-files-in-pager`, which run one over the matches of a search — reachable
by a read-only verb, needing neither a lease nor a file on the volume;
`--help`, described above; `--trailer`, which runs `trailer.<name>.cmd` to
compute a trailer's value and so puts a caller-named command on `git commit -m`,
the argument vector the skills already send; and
`--upload-pack` and `--receive-pack`, which name a program to run for the remote
end of a transfer. The last two are unreachable while the transport allowlist
excludes local paths, and are refused so that widening the allowlist does not
silently reintroduce them. `--upload-pack` has a short spelling and it is
deliberately not refused: `-u` means `--upload-pack` on `git clone`, but the
same two characters mean `--set-upstream`, `--update` and `--update-head-ok` on
other subcommands, and refusing it would refuse all four everywhere.
(`--receive-pack` has no short form.)

Every entry is matched against the whole argument vector rather than only the
region where git would honour it; against any abbreviation of it that git would
accept, since git's subcommand options take unambiguous prefixes; and, for short
options, anywhere inside a cluster, since git lets short options group into one
argument and carry a value attached to the last of them. Each of those three is
a spelling git honours, and a checker that recognises fewer spellings than the
executor accepts is the one defect this codebase keeps producing. Deciding where
a subcommand's options end, or which prefixes it leaves unambiguous, would mean
agreeing with git's parser indefinitely; the checker is instead strictly more
conservative than git. The cost is a refusal the argument's position would
otherwise excuse — a commit message that is the bare word `foreach` or `help`,
or `git clean -x` — which is the direction this is meant to fail in.

Matching the subcommand by position instead would remove that last cost, and it
is deliberately not done. Resolving the subcommand slot means knowing which of
git's global options take a separated value, and the list is not one this
runtime can keep complete: `git --attr-source HEAD help -m <page>` runs the
configured viewer while a position-aware reading of the same argument vector
sees the subcommand as `HEAD`. Scanning every argument cannot disagree with git
about where the subcommand is.

Refusals are reported as `SECURITY_POLICY_BLOCKED` with rule
`git.argument.refused`. No shipped skill uses any of them; every skill clone,
fetch and push uses an `https` URL built from a fixed prefix.

**Limitation.** Two gaps remain, and both are consequences of the same thing —
the runtime executing inside a tree whose contents the sandbox chooses.

The pins do not extend to configuration stored in a repository's own
`.git/config`. The sandbox no longer mounts that tree, so it cannot write the file
directly; what remains is the write routes, which is why they refuse every
spelling of `.git` a filesystem treats as `.git`. That refusal is a check rather
than a structural impossibility, and where every way of reaching such a key is
itself nameable the triggers are refused too — that is what the `help` and
`interpret-trailers` subcommand entries and the `--help` and `--trailer` option
entries above are for. This is weaker than a pin
and should be read that way. The key stays settable, so the refusals hold only
while the set of triggers is complete, and completeness is an empirical claim
about a program that changes: `man.<tool>.cmd` turned out to have two triggers
rather than one, and the second was found after the first had been closed and
documented as closed.

Measured against git 2.55 under the pinned environment, driving the runtime's own
executor, these repository-local keys still reach a command:

| Key                      | Reached by                                                          | Lease | Why it is not pinned            |
| ------------------------ | ------------------------------------------------------------------- | ----- | ------------------------------- |
| `diff.external`          | `git diff`, `git log --ext-diff`                                    | no    | no value disables it; see above |
| `diff.<driver>.command`  | `git diff`                                                          | no    | arbitrary name                  |
| `diff.<driver>.textconv` | `git diff`, `git log -p`, `git blame`, `git show`                   | no    | arbitrary name                  |
| `filter.<name>.clean`    | `git status`, `git diff`; also `git add`, `git stash`, `git commit` | no    | arbitrary name                  |
| `filter.<name>.process`  | `git status`, `git diff`; also `git add`, `git stash`               | no    | arbitrary name                  |
| `filter.<name>.smudge`   | `git checkout`                                                      | yes   | arbitrary name                  |
| `merge.<driver>.driver`  | `git merge`, `git cherry-pick`, `git rebase`                        | yes   | arbitrary name                  |
| `alias.<name>`           | `git <name>`                                                        | no    | arbitrary name                  |

Every key that needs `.gitattributes` is no harder to reach for it, because it is
an ordinary tracked file. The two `filter.*` rows need the tracked file
to be modified as well as the attribute to be set; with both in place a bare
`git status` runs the configured command.

The lease column is measured against the runtime's own check, not assumed. Six of
the eight are reachable without one, `git status` and `git diff` being the widest
of those routes; the two that do need a lease are only bounded by it in the weak
sense noted above, since the agent creates its own leases. Extending the pinned layer
does not close any of the eight: seven take an arbitrary name inside the key, so
there is nothing to enumerate, and the eighth has no value that means "off".
This is what separates them from the signature-format keys pinned earlier, where
the set of key names is fixed and finite and pinning therefore works.

The same measurement found several keys that do **not** reach a command here, and
each is worth knowing because it is a control rather than an accident, except the
last: `core.editor` and `sequence.editor` are outranked by `GIT_EDITOR` and
`GIT_SEQUENCE_EDITOR`; `core.sshCommand` is unreachable while the transport
allowlist is `https` only; `init.templateDir` installs hooks that the
`core.hooksPath` pin then ignores; and `core.hooksPath` and `core.fsmonitor` are
pinned directly.

`core.pager` is the exception, and it is closed by accident. It executes on a
read verb — `git log`, `git diff`, `git show`, `git branch` — whenever git has a
terminal on stdout. It does not execute here only because the runtime captures
output through a pipe, so git never starts a pager; `--paginate` does not change
that. Nothing declares this, so a change to how the runtime captures output would
turn a repository-local config value into arbitrary code execution. Pinning
`core.pager` would not help, because `pager.<command>` reaches the same place with
an arbitrary name in the key. The pipe is the control, and there is a test that
fails if it goes away.

`include.path` and `includeIf` are honoured from repository-local configuration,
so any of the keys above can be pulled in from an absolute path outside the
workspace rather than written into `.git/config` directly. That widens where the
value may sit; it adds no capability, and an included setting is still subject to
the pinned layer — an included `core.hooksPath` loses to the pin exactly as a
direct one does.

That file reaches the credential as well as the program search. A credential
helper configured there is itself a command, and it runs for any host the
installed GitHub helper declines to answer for — so it both executes and is
handed the credential being requested; a URL rewrite configured there changes
which host a fetch or push contacts, and the transport allowlist does not help
because the substituted host is `https` too. Neither is closed today. The
credential-helper half is closable — resetting the helper list in the pinned
layer and reinstating the runtime's own helper immediately after it discards a
repository's helper while leaving authenticated push working — but that couples
the runtime to the value `gh auth setup-git` writes, and it has not been done.

The refused-subcommand list is likewise a denylist over a set that is not closed:
git holds a command in configuration for several tools, and a future release may
add another. Allowlisting the subcommands the product issues, and failing closed
on the rest, is the structurally correct form and is the recommended follow-up.

Reducing both is the motivation for having the runtime receive file content from
the sandbox rather than operate inside a directory the sandbox controls. The
mechanism for that is described below; until the skills are moved onto it, a
cloned working tree is sandbox-controlled input and not a trust boundary.

### Broker-owned working trees

The controls above all work on the argument vector, and every one of them shares
a premise: the runtime executes inside a directory the sandbox owns. That premise
is what makes repository-local configuration reachable at all, and it is the
reason enumeration cannot finish — the dangerous keys carry an arbitrary name
inside the key, so there is no finite set to deny.

The alternative is to stop passing a directory. The sandbox sends `{path, bytes}`
pairs, a branch and a message; the runtime writes them into a tree on its own
volume, commits and pushes; the sandbox never names a path and never sees one
come back. `CREDENTIAL_PROXY_CONTENT_WORKSPACE` arms it and it is off by default.
While it is off the `/v1/workspace/*` routes do not exist — they answer 404, which
is also what an older runtime answers, so a client can detect support by asking.

Four things hold it together, and they are not equally strong:

- **Disjoint roots, checked.** The tree root is under the runtime's own state
  directory and the check that it does not overlap any other configured path runs
  at construction. A mount rearrangement that collapses the two is a runtime that
  refuses to start rather than one that starts without the property.
- **One path validator.** The same function decides what a path may name on
  reads and on writes, refuses rather than normalises, and refuses every spelling
  of `.git` that a filesystem treats as `.git`. A checker that disagrees with
  itself about what `manifests/../.git/config` means is the defect class this
  document keeps describing.
- **A separate door for the runtime's own git.** Broker-internal git does not go
  through `/v1/exec`. It accepts a literal allowlist of the subcommands this
  mechanism issues (`WORKSPACE_GIT_SUBCOMMANDS` in
  `agents/platform/scripts/content_workspace.py`), refuses `-C`, and is contained
  to the tree root. Keeping the two apart is what lets the sandbox-facing
  answer stay "git is not reachable" rather than "git is reachable, narrowly".
- **Geometry, not checked here.** The tree root is on
  the runtime's own volume, which the sandbox container does not mount. Nothing in
  the process can verify that; it is the same argument this document already makes
  about `$HOME/.gitconfig` and `KUBECTL_KUBERC`, and it is exactly as weak. The
  structural form of it is the separate broker Pod, where there is no shared mount
  to name.

Resource notes, because the trees are on the runtime's own emptyDir and that is
node ephemeral storage rather than a disk of its own.

The number of open workspaces is capped, and a clone that comes in over
`CREDENTIAL_PROXY_MAX_CLONE_BYTES` (256 MiB by default) is removed rather than
kept, so a repository the runtime cannot afford does not sit half-cloned on the
node. Read those two together rather than separately: the ceiling is **per
clone**, so the two defaults multiply out to about 2 GiB retained across eight
open workspaces, by design. Size the node's ephemeral storage against the
product, not against either number.

The ceiling is also measured after the clone finishes, so it bounds what is
_retained_ and not the peak: a repository far over the limit still lands on the
disk before it is removed, and the only thing bounding that is the runtime's
per-invocation timeout. A shallow clone plus the ceiling would bound both and is
the right follow-up; neither alone does.

Separately, the content routes raise the request-body cap to twice the
total-payload limit, and the listener is threaded with no connection cap, so peak
heap is roughly concurrency times that figure. None of this is reachable from
outside the Pod, but it is worth knowing before the flag is armed anywhere it
matters.

Every verb takes a lock for its whole duration. The handler is threaded, so two
requests naming one handle genuinely interleave, and each verb is a read-then-act
on a working tree the other is entitled to delete or reset underneath it. Without
it a `close` landing inside a `commit` leaves the commit re-creating the tree the
close just removed, with no handle pointing at it.

Be precise about what arming this does and does not do. It does not change what
`git` executes: a repository-local `.git/config` planted in the runtime's own tree
still runs what it names, measured both ways. What changes is who can write that
file. And `/v1/exec` is unchanged while the flag is on — both mechanisms run side
by side deliberately, so that landing this does not have to be reverted to fix the
migration. The finding class above therefore closes for work that goes through the
content routes and stays open for work that does not, which today is all of it.

### Agent-supplied kubeconfigs

A Cluster Agent profile pins itself to one cluster through `KUBECONFIG`, so the
sandbox names the document a credentialed `kubectl` would otherwise open. The two
no longer share a filesystem, but a name the broker resolves on the sandbox's
behalf raises the same question a shared volume did.

A kubeconfig is executable configuration rather than data. Left unconstrained it
offers the sandbox several ways past the boundary this design establishes:

- `users[].user.exec.command` and `users[].user.auth-provider.config.cmd-path`
  run a program inside the credential broker, next to the credentials;
- `clusters[].cluster.server` and `proxy-url` choose where the access token
  minted by `gke-gcloud-auth-plugin` is sent, with `certificate-authority-data`
  supplied by the same author so TLS still validates;
- `users[].user.tokenFile` reads a broker-side file of the author's choosing and
  sends its contents as the bearer token; and
- `insecure-skip-tls-verify` removes the remaining obstacle to the above.

None of this is visible to the deny policy described above, which matches
against the argument vector — the argv is only ever `kubectl get pods`.
Validating the document instead would mean maintaining a denylist over a format
that keeps growing, and would not hold regardless: the sandbox can rewrite the
file between the check and the open.

The proxy therefore treats an agent-supplied kubeconfig as a **name, not as
content**. This is possible because the sandbox never legitimately authors one.
Every kubeconfig the system uses is produced by `gcloud container clusters
get-credentials`, which already runs in the broker. The proxy reads exactly one
string out of the caller's file — `current-context` — accepts it only if it is a
well-formed `gke_<project>_<location>_<cluster>` name, and regenerates the
kubeconfig itself into a directory backed by a broker-only `emptyDir`. That
regenerated file is what every proxied command runs against. No field the
sandbox wrote is ever interpreted, and there is no check-then-open window,
because the sandbox never had a handle on the document that is opened.

The same substitution is applied to a `--kubeconfig` flag in the argument
vector, which `kubectl` prefers over the environment; covering only the
environment would leave the flag as an equivalent path. `get-credentials` is
handled as the one command permitted to author a kubeconfig: it writes into the
broker's own directory, the result is filed under the context it selects, and the
context name is what the caller gets back. The visible pin
that profile scaffolding records and the Cluster Agent preflight inspects
therefore still exists, without being what a later command opens.

Consequences:

- Naming a cluster is not additional authority. `get-credentials` is bound by
  the same Workload Identity the broker already holds, so the sandbox can only
  name clusters that identity could already reach.
- Only GKE contexts are supported, because the context name is what makes
  regeneration possible. A pin the proxy cannot regenerate from — no
  `current-context`, a non-GKE context name, or a merged `path1:path2` list — is
  rejected with `400` rather than honored.
- A cache miss costs one `get-credentials`, preceded by one
  `clusters describe` to decide whether the control plane should be reached
  over its DNS endpoint. The common paths warm the cache themselves, since
  profile scaffolding and context switching both begin with that command. That
  describe is memoised per cluster for a minute rather than for the life of the
  broker: the endpoint can be opened or closed on a running cluster, and the
  proxy is a daemon that would otherwise keep acting on the configuration it
  first saw.
- `current-context` is read with a real YAML parser, so a valid kubeconfig in
  any legal spelling is recognized, but deliberately with PyYAML's pure-Python
  `safe_load`. The C loader recurses in C and terminates the broker with
  `SIGSEGV` on a deeply nested document, where the Python loader raises a
  catchable error. The input is chosen by the sandbox, so this is a
  denial-of-service boundary rather than a performance choice.

### Chat

Slack and Google Chat adapters send credential-free request payloads to Envoy.
The credential runtime owns platform tokens and performs the external API call.
Allowlisted users, payload size limits, and file size limits are enforced by the
relay.

### PlatformAgent API

The Kubernetes Service sends API traffic to port 8643 on `agent-api-auth`, the
native sidecar in the gateway Pod. It validates the configured external bearer
key and replaces it with the fixed, non-secret sentinel before forwarding to port
8642 on loopback.
Existing API clients retain bearer-key authentication without placing the real
key in the sandbox.

### GitHub and Minty

The credential broker obtains a Google OIDC identity token and calls Minty.
Minty validates CEL authorization rules for the authenticated agent identity and
requested repository, then brokers a repository-scoped GitHub installation
token with a maximum one-hour lifetime. The GitHub App private key remains in
Cloud KMS and signing uses `AsymmetricSign`.

The broker owns the only checkout. Proxied Git operations take file content and a
commit message rather than a directory both sides mount, so Git authentication,
CLI config, token caches, and the working tree all stay on the broker's own
volume. The sandbox receives only command output, never a mounted Git credential
file and never a `.git` it can write into.

## Kubernetes Details

- The broker Pod uses the configured PlatformAgent KSA — `spec.security.serviceAccountName`,
  defaulting to the agent's name — for its Workload Identity. The gateway Pod
  runs under the same one, which is the boundary's remaining gap; the sandbox Pod
  runs under `<agent>-shell`, annotated with nothing.
- `automountServiceAccountToken: false` applies to the Pod.
- The event watcher's projected ServiceAccount token is mounted only by
  `agent-api-auth`. The `platform-agent` container mounts one of its own — the
  broker-audience token it presents to the broker Service — and the dashboard
  mounts none.
- Secret and credential-state volumes are mounted only in the broker Pod.
- Every container runs non-root, drops all Linux capabilities, disallows
  privilege escalation, and uses the runtime-default seccomp profile.
- No Pod sets `shareProcessNamespace`. The credential runtime is not in a Pod with
  anything that runs model-authored code, so the UID split that used to separate
  the sandbox from the credential runtime inside one Pod is no longer what holds
  the boundary: a Pod boundary does. The workspace PVC is likewise no longer
  shared with the credential runtime, which owns its own volume and takes file
  content rather than a directory.
- Every container the operator builds — the credential-cleanup init container,
  the agent, the dashboard, `agent-api-auth`, the log shipper — has a read-only
  root filesystem;
  writable state uses bounded `emptyDir` volumes. The sandbox and dashboard
  share a 2Gi `/tmp` scratch volume: the entrypoint runs several hermes
  invocations with `HOME=/tmp` before the agent starts, and those two
  containers already share the data PVC, so the shared volume is not a new
  channel between them. Note the lifetime this changes: `/tmp` used to be each
  container's own writable layer, discarded whenever that container restarted.
  An `emptyDir` is scoped to the Pod, so its contents now survive a container
  crash or restart and are visible to both containers' next boot. The log
  shipper gets no `/tmp`: it buffers in memory
  and keeps its tail database on its own volume. Containers supplied through
  `spec.deployment.sidecars`/`initContainers` are appended to the Pod as
  written; the webhook does not require a read-only root of them, so a CR can
  still add a writable container to this Pod.
- A policy ConfigMap hash is placed on the Pod template to trigger rollout when
  command policy changes.
- The operator reports Ready only when every workload it renders is ready.

## Deployment and Migration

The operator renders the `<agent>-gateway` Deployment, the `<agent>-shell`
StatefulSet, and the `<agent>-credential-proxy` Deployment and Service, and retains
the existing `<agent>-data` PVC. Before the agent starts, a
managed init container removes legacy gcloud, GitHub, Git, Kubernetes, AWS,
Azure, Docker, npm, and Python credential files from that PVC. This preserves
agent state without carrying credentials forward from an older deployment. It
deletes operator-owned resources from the abandoned two-Pod design:

- `<agent>-sandbox` Deployment; and
- `<agent>-sandbox` ServiceAccount.

Deletion refuses to remove resources not owned by the PlatformAgent.

Three names that used to be on that list are no longer deleted, and the
difference is load-bearing. The `<agent>-credential-proxy` Deployment and
Service are not legacy any more: they are exactly what the operator renders on
every reconcile, so deleting them each pass would tear down the Pod the same
reconcile applied.

The `<agent>-sandbox-metadata-deny` NetworkPolicy is left alone for a different
reason. It is a guardrail rather than a workload, and this controller does not
delete a guardrail it did not create, so a cluster operator who applies that
policy by hand can rely on it surviving a reconcile. A stale
NetworkPolicy fails closed; a stale Deployment does not. Leaving it on the list
was also a live bug: nothing owns a hand-applied copy, so the ownership check
above refused it and failed every reconcile before `updateStatusReady`, which
left the CR's status silently not tracking the agent.

The credential-proxy image contains Envoy, the real credential-aware CLIs,
and the credential runtime. The sandbox image contains only the wrappers for
those CLI names.

## Tradeoffs

Benefits:

- no managed credential env or files in the sandbox;
- a network and identity boundary between the sandbox and the credential runtime,
  not a container boundary;
- a single surface for CLI and chat policy; and
- credentials remain usable without adding real cloud CLIs to the sandbox.

Costs:

- three Pod lifecycles and cross-Pod availability to coordinate: a broker that is
  down is a broker the sandbox cannot reach, and the command fails rather than
  degrading;
- a custom command-forwarding protocol must be maintained;
- interactive, streaming, and file-based CLI behavior is limited;
- nothing crosses as a path, because no filesystem is common to caller and broker:
  a document travels on stdin, a kubeconfig as a GKE context name the broker
  regenerates, and a commit as file content plus a message; and
- each new service needs an explicit proxy integration and policy.

The gateway Pod's own cloud identity is the boundary still to be drawn; see
[Limitation](#limitation).

## Verification

CI and deployment tests should assert that:

1. the sandbox has no `spec.deployment.env`, secret volume, credential-state
   volume, and no Secret-backed env other than the two pod-scoped Session KV
   values named above — the assertion enumerates them, so a third one cannot
   be added without amending this list. The `platform-agent` container and the
   sandbox's `shell` container each mount exactly one ServiceAccount token, the
   broker-audience projection, and still none of the rest; no container in
   either Pod mounts a Kubernetes-API-audience token except `agent-api-auth`;
2. only the credential runtime mounts proxy identity/state, and only the event
   watcher mounts its Kubernetes-API token projection;
3. only the credential runtime receives Slack tokens and deployment env;
4. wrapper URLs resolve to
   `http://<agent>-credential-proxy.<namespace>.svc.cluster.local:8765`;
5. Envoy can reach the Unix-socket backend and `/healthz` reflects both;
6. unsupported executables, raw shell requests, and blocked disclosure commands
   fail closed;
7. a command given an agent-authored kubeconfig runs against a regenerated one,
   with no `exec`, `server`, `proxy-url`, or `tokenFile` value from the supplied
   document reaching it, whether it arrives through `KUBECONFIG` or
   `--kubeconfig`;
8. the `<agent>-credential-proxy` Deployment and Service are present after every
   reconciliation and are never on the legacy-cleanup list. The `<agent>-sandbox`
   Deployment and ServiceAccount are absent, and
   `<agent>-sandbox-metadata-deny` survives a reconcile that did not create
   it, whether or not it carries an owner reference;
9. `spec.harness.experimental.shellSandbox.enabled: false` is refused with
   `Degraded`/`ShellSandboxCannotBeDisabled` and changes nothing about the running
   workload;
10. the external PlatformAgent API key is accepted by `agent-api-auth` and replaced
    before forwarding to the loopback-only sandbox API; and
11. broker Pod readiness fails when either Envoy or the credential runtime fails.
