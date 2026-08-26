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

Each PlatformAgent runs as one long-lived Pod with these managed containers:

1. `platform-agent`: the untrusted agent sandbox.
2. `platform-agent-dashboard`: the optional local dashboard.
3. `fluent-bit`: log forwarding.
4. `envoy-credential-proxy`: Envoy, the credentialed command and chat runtime,
   and the `k8s-event-watcher`, which forwards cluster events using a
   non-secret internal key.

The proxy is a **native sidecar** -- an entry in `initContainers` carrying
`restartPolicy: Always` -- and not an ordinary container, so
`kubectl get pod -o jsonpath='{.spec.containers[*].name}'` will not list it. It
is shaped that way because it owns port 8643, which the Service targets, and it
shares a network namespace with the sandbox: as two ordinary containers they
started together and raced for the bind, and the sandbox could take the port
external callers reach. A native sidecar starts before any app container, which
narrows that race but does not close it: the kubelet waits for the sidecar to have
**started**, plus its `startupProbe` if it declares one, and this container declares
only a `readinessProbe`. So the ordering guaranteed is of process creation, not of
Envoy having bound 8643. Giving the container a `startupProbe` on that port would
close it; see `buildPodTemplateSpec` in the operator.

Requires Kubernetes 1.29+, where `SidecarContainers` is beta and enabled by
default. It is alpha and off in 1.28, and GA in 1.33.

On 1.28 the install fails rather than degrading. The API server strips
`restartPolicy` from the init container, which leaves it declaring a readiness
probe that a non-restartable init container may not have, so the pod template is
rejected and the apply fails. There is no configuration in which the proxy runs
as an ordinary init container with credential isolation quietly weakened. The
chart's `kubeVersion` refuses the install before that point.

The sandbox calls wrappers for `gcloud`, `kubectl`, `gh`, and `git`. Wrappers
send a structured argument vector to Envoy at `127.0.0.1:8765`. Envoy forwards
requests over a private Unix socket to the credential runtime. Slack and Google
Chat use the same local relay.

Only trusted sidecars receive projected Kubernetes ServiceAccount (KSA) tokens.
The credential sidecar receives secret environment variables, credential state,
and its identity token. It also receives a second, separately-audienced
Kubernetes-API token, CA, and namespace projection, which the event watcher it
hosts uses to reach the management cluster. Neither token is mounted in the
agent or dashboard containers. The credential sidecar also authenticates callers of the
PlatformAgent API before forwarding requests with a non-secret internal
sentinel. Pod-wide automatic KSA token mounting is disabled.

### Guarantee

The operator does not place managed credentials in the sandbox container's:

- environment;
- root filesystem;
- persistent agent volume; or
- mounted ServiceAccount token path.

`spec.deployment.env` is applied to the credential sidecar because it may
contain credentials. A short allowlist may also be copied to the sandbox — the
four OpenTelemetry settings and the three `ALERT_DAILY_LIMIT_*` alert ceilings —
but only as literal values; all `valueFrom` sources are rejected. A name earns a
place on that list only if an arbitrary value for it cannot redirect state,
grant access, or change what code runs; `safeSandboxEnvOverrides` in
`k8s-operator/internal/controller/platformagent_manifests.go` is the list.
Reserved proxy, runtime-loader, and shell-startup variables cannot override the
operator's managed values.

### Limitation

Containers in one Pod share a network namespace and one Pod identity. The
sandbox does not have the KSA token file, but it can technically reach the GKE
metadata server used by the credential sidecar. A Pod-level NetworkPolicy
cannot block metadata for one container while allowing it for another.

This design therefore meets the scoped filesystem-and-environment goal, but it
does not provide the stronger identity boundary of separate Pods. It assumes
the agent does not deliberately request credentials from the metadata server.

The shared workspace is the other way in, and it is not closed by the UID split
either. Both containers mount the agent PVC and both write there with
`umask 0002`, which they must: each has to be able to change what the other
created. So a file the credential sidecar writes into a clone — a `.git/config`
in a repository it cloned, say — is group-writable, and the sandbox is in that
group. Configuration the sandbox edits there is configuration a later proxied
command reads, and some of it names programs to run. What the UID split removes
is the sandbox reading the sidecar's process state and private volumes by
identity; what it does not remove is the sandbox reaching the sidecar through
bytes the sidecar itself agreed to read. This predates the UID split — before
it, those bytes were the sandbox's own — so nothing here made it worse, and
nothing here closes it. What would close it is refusing the configuration keys
that select a program to run, and that belongs to the command policy rather
than to the Pod spec.

## Scope

### In scope

- PlatformAgent only.
- Credentials managed by the operator.
- CLI forwarding for `gcloud`, `kubectl`, `gh`, and `git`.
- Slack and Google Chat credentialed relays.
- PlatformAgent API bearer-key termination in the credential sidecar.
- GitHub installation tokens minted through Minty.
- Sidecar health, lifecycle, rollout, and migration from the former proxy Pod.

### Out of scope

- Preventing credentials from appearing in approved command or tool output.
- Preventing an approved credentialed command from deliberately copying a
  credential into the shared workspace. Such credential disclosure and tool
  side effects require a separate approval/policy design.
- Preventing deliberate access to the shared Pod identity or metadata server.
- Arbitrary user-supplied init containers, sidecars, volumes, and mounts. These
  are trusted configuration and may intentionally weaken isolation.
- OperatorAgent and DevTeamAgent.
- General data-exfiltration prevention.

## Architecture

```text
PlatformAgent Pod

  platform-agent
    credential-free env and mounts
    CLI wrappers / chat adapters
              |
              | HTTP on 127.0.0.1:8765
              v
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
credential runtime listens on a Unix socket mounted only in the sidecar, so the
sandbox cannot bypass Envoy by calling the runtime directly. A separate sidecar
listener authenticates the existing PlatformAgent API on port 8643 and forwards
to the sandbox API on loopback using a non-secret sentinel.

The containers share a Pod, but not the same lifecycle: the credential proxy is
a native sidecar, so the kubelet starts it before every app container and stops
it after them. If the sidecar is not ready, the Pod is not ready -- a restartable
init container counts toward Pod readiness. If either Envoy or the credential
runtime exits, the sidecar exits and Kubernetes restarts it; during startup that
means the app containers have not begun yet, so a bootstrap failure shows up as
`Init:CrashLoopBackOff` rather than a running Pod with one bad container.

## Credential Placement

| Data                             | Sandbox                | Credential sidecar        |
| -------------------------------- | ---------------------- | ------------------------- |
| `spec.deployment.env`            | No                     | Yes                       |
| Slack tokens                     | No                     | Yes, Secret-backed env    |
| PlatformAgent external API key   | No                     | Yes, Secret-backed env    |
| Session KV API key and HMAC salt | Yes, Secret-backed env | Yes, API key only         |
| Automatic KSA token mount        | Disabled               | Disabled                  |
| Explicit projected KSA token     | Not mounted            | Read-only, one-hour token |
| gcloud/kubectl configuration     | No                     | Private `emptyDir`        |
| GitHub installation token/cache  | No                     | Private `emptyDir`        |
| Agent workspace                  | Yes                    | Yes, for proxied commands |

### The loopback-only exception

`SESSION_KV_API_KEY` and `SESSION_KV_SALT` are the only Secret-backed values the
sandbox receives, and they are the exception that proves the rule rather than a
relaxation of it. Both are pod-scoped: they authenticate and pseudonymise
nothing outside this Pod, and neither grants access to any external system, so
an agent that reads them out of its own environment gains nothing it did not
already have.

They cannot go behind the proxy, because the sandbox is not the client — it is
the server. `session_kv_server.py` runs in the sandbox and binds
`127.0.0.1:8699`; its callers are the event watcher in the credential sidecar,
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
not a degradation: the `k8s-event-watcher` in the credential sidecar
authenticates to the Session KV server with `SESSION_KV_API_KEY` and treats an
empty value as fatal, so it exits on every start and **no cluster events are
watched at all** — silently, since the container stays Ready and no probe covers
the watcher. The other two are degradations: the Session KV server refuses every
authenticated request with a 503 and says why, and identity hashing falls back
to a per-process random salt with one warning.

The projected token uses the audience `kubeagents-credential-proxy`, expires
after one hour, and is mounted only at
`/var/run/secrets/kubeagents/serviceaccount/token` in the credential sidecar.
The event watcher has a separate one-hour token with the Kubernetes API's
default audience, plus the cluster CA and Pod namespace, mounted at the
conventional in-cluster path in the same credential sidecar. Two differently
audienced tokens therefore sit side by side there: the proxy's own, which the
Kubernetes API will not accept, and the watcher's, which it will. Neither is
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
credential sidecar. Requests that cannot be represented safely fail closed,
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
files, and it reads those from the shared workspace volume as well as from the
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

**Limitation.** Two gaps remain and both are consequences of the same thing —
the runtime executing inside a directory the sandbox owns.

The pins do not extend to configuration stored in a repository's own
`.git/config`. That file is inside the shared workspace, so the sandbox can write
it directly, with no proxied command involved and nothing for the argument-vector
checks to inspect; the runtime then reads it on the next request. Where every way
of reaching such a key is itself nameable, those triggers are refused instead —
that is what the `help` and `interpret-trailers` subcommand entries and the
`--help` and `--trailer` option entries above are for. This is weaker than a pin
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

Every key that needs `.gitattributes` is no harder to reach for it, because that
file is in the workspace as well. The two `filter.*` rows need the tracked file
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
the sandbox rather than operate inside a directory the sandbox controls; until
that lands, a cloned working tree is sandbox-controlled input and not a trust
boundary.

### Agent-supplied kubeconfigs

A Cluster Agent profile pins itself to one cluster through `KUBECONFIG`, and
that file lives on the shared workspace volume, which the credential sidecar
also mounts. The sandbox can therefore choose the document a credentialed
`kubectl` opens.

A kubeconfig is executable configuration rather than data. Left unconstrained it
offers the sandbox several ways past the boundary this design establishes:

- `users[].user.exec.command` and `users[].user.auth-provider.config.cmd-path`
  run a program inside the credential sidecar, next to the credentials;
- `clusters[].cluster.server` and `proxy-url` choose where the access token
  minted by `gke-gcloud-auth-plugin` is sent, with `certificate-authority-data`
  supplied by the same author so TLS still validates;
- `users[].user.tokenFile` reads a sidecar file of the author's choosing and
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
get-credentials`, which already runs in the sidecar. The proxy reads exactly one
string out of the caller's file — `current-context` — accepts it only if it is a
well-formed `gke_<project>_<location>_<cluster>` name, and regenerates the
kubeconfig itself into a directory backed by a sidecar-only `emptyDir`. That
regenerated file is what every proxied command runs against. No field the
sandbox wrote is ever interpreted, and there is no check-then-open window,
because the sandbox never had a handle on the document that is opened.

The same substitution is applied to a `--kubeconfig` flag in the argument
vector, which `kubectl` prefers over the environment; covering only the
environment would leave the flag as an equivalent path. `get-credentials` is
handled as the one command permitted to author a kubeconfig: it writes into the
sidecar's own directory, the result is filed under the context it selects, and a
copy is then written to the workspace path the caller asked for. The visible pin
that profile scaffolding records and the Cluster Agent preflight inspects
therefore still exists, without being what a later command opens.

Consequences:

- Naming a cluster is not additional authority. `get-credentials` is bound by
  the same Workload Identity the sidecar already holds, so the sandbox can only
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
  sidecar: the endpoint can be opened or closed on a running cluster, and the
  proxy is a daemon that would otherwise keep acting on the configuration it
  first saw.
- `current-context` is read with a real YAML parser, so a valid kubeconfig in
  any legal spelling is recognized, but deliberately with PyYAML's pure-Python
  `safe_load`. The C loader recurses in C and terminates the sidecar with
  `SIGSEGV` on a deeply nested document, where the Python loader raises a
  catchable error. The input is chosen by the sandbox, so this is a
  denial-of-service boundary rather than a performance choice.

### Chat

Slack and Google Chat adapters send credential-free request payloads to Envoy.
The credential runtime owns platform tokens and performs the external API call.
Allowlisted users, payload size limits, and file size limits are enforced by the
relay.

### PlatformAgent API

The Kubernetes Service sends API traffic to port 8643 on the credential
sidecar. The sidecar validates the configured external bearer key and replaces
it with the sandbox's fixed, non-secret sentinel before forwarding to port 8642.
Existing API clients retain bearer-key authentication without placing the real
key in the sandbox.

### GitHub and Minty

The credential sidecar obtains a Google OIDC identity token and calls Minty.
Minty validates CEL authorization rules for the authenticated agent identity and
requested repository, then brokers a repository-scoped GitHub installation
token with a maximum one-hour lifetime. The GitHub App private key remains in
Cloud KMS and signing uses `AsymmetricSign`.

The workspace is mounted at the same path in both containers so proxied Git
commands operate on the agent's repository. Git authentication, CLI config, and
token caches remain on a separate sidecar-only volume. The sandbox receives
only command output, never a mounted Git credential file.

## Kubernetes Details

- The Pod uses the configured PlatformAgent KSA for the credential sidecar's
  Workload Identity.
- `automountServiceAccountToken: false` applies to the Pod.
- Two separately projected ServiceAccount token volumes are mounted only by the
  credential sidecar — its own, and the event watcher's; neither is mounted by
  the agent or dashboard containers.
- Secret and credential-state volumes are mounted only by the credential
  sidecar.
- The sandbox and sidecar run non-root, drop all Linux capabilities, disallow
  privilege escalation, and use the runtime-default seccomp profile.
- The Pod never sets `shareProcessNamespace`, and the two containers run as
  different users: the sandbox as UID 10000, the `hermes` user the agent image's
  files belong to, and the sidecar as UID 10001. Neither `/proc` nor a file mode
  hands the sandbox the sidecar's environment.
- Both keep GID 10000, which is also the Pod `fsGroup`. The workspace PVC is
  mounted in both and each writes files the other has to change — the sandbox
  creates the leased GitOps directory the sidecar clones into, the sidecar writes
  the kubeconfig pin into a profile home the sandbox created — so both
  entrypoints run with `umask 0002`. Files that predate the UID split are made
  group-writable by the kubelet's `fsGroup` pass at every mount.
- Every container the operator builds — the credential-cleanup init container,
  sandbox, dashboard, sidecar, log shipper — has a read-only root filesystem;
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
- The operator reports Ready only when the combined Pod is ready.

## Deployment and Migration

The operator always creates the sandbox and Envoy credential sidecar together
in the existing `<agent>-gateway` Deployment and retains the existing
`<agent>-data` PVC. Before the sandbox starts, a managed init container removes
legacy gcloud, GitHub, Git, Kubernetes, AWS, Azure, Docker, npm, and Python
credential files from that PVC. This preserves agent state without carrying
credentials forward from an older deployment. It deletes operator-owned
resources from the abandoned two-Pod design:

- `<agent>-credential-proxy` Deployment;
- `<agent>-sandbox` Deployment;
- `<agent>-credential-proxy` Service;
- `<agent>-sandbox` ServiceAccount; and
- `<agent>-sandbox-metadata-deny` NetworkPolicy.

Deletion refuses to remove resources not owned by the PlatformAgent.

The credential-sidecar image contains Envoy, the real credential-aware CLIs,
and the credential runtime. The sandbox image contains only the wrappers for
those CLI names.

## Tradeoffs

Benefits:

- one Deployment and one Pod lifecycle;
- no managed credential env or files in the sandbox container;
- no separate proxy Service or cross-Pod availability coordination;
- a single local surface for CLI and chat policy; and
- credentials remain usable without adding real cloud CLIs to the sandbox.

Costs:

- no hard network or identity boundary between the sandbox and sidecar;
- a custom command-forwarding protocol must be maintained;
- interactive, streaming, and file-based CLI behavior is limited;
- configuration files the sandbox supplies to a credentialed command must be
  regenerated rather than read, which bounds them to what the sidecar can
  reproduce — for kubeconfigs, GKE contexts only; and
- each new service needs an explicit proxy integration and policy.

If deliberate metadata or Pod-identity access becomes in scope, this design
must return to separate Pods or use a node/runtime mechanism that enforces
per-container network identity.

## Verification

CI and deployment tests should assert that:

1. the sandbox has no `spec.deployment.env`, secret volume, credential-state
   volume, or ServiceAccount token mount, and no Secret-backed env other than
   the two pod-scoped Session KV values named above — the assertion enumerates
   them, so a third one cannot be added without amending this list;
2. only the credential sidecar mounts proxy identity/state, and only the event
   watcher mounts its Kubernetes-API token projection;
3. only the credential sidecar receives Slack tokens and deployment env;
4. wrapper URLs resolve to `127.0.0.1:8765`;
5. Envoy can reach the Unix-socket backend and `/healthz` reflects both;
6. unsupported executables, raw shell requests, and blocked disclosure commands
   fail closed;
7. a command given an agent-authored kubeconfig runs against a regenerated one,
   with no `exec`, `server`, `proxy-url`, or `tokenFile` value from the supplied
   document reaching it, whether it arrives through `KUBECONFIG` or
   `--kubeconfig`;
8. the old proxy Deployment and Service are absent after reconciliation;
9. the external PlatformAgent API key is accepted by the sidecar and replaced
   before forwarding to the loopback-only sandbox API; and
10. Pod readiness fails when either Envoy or the credential runtime fails.
