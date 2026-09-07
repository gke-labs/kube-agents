# Supporting a Second Forge

> **STATUS — design of record; steps 1 and 2 of §9 are in, step 3 is nearly in, step 4 is in as far
> as the broker's own surface, and the rest is not.** No second forge works today. One provider
> exists (`GitHubProvider`), four consumers use it, one more shells `gh` directly. Repository
> identity now runs on `repo_ref.py` in Python and `repo_ref.go` in Go; the CRD declares the forge in
> `spec.integration.git` with validation dispatched per provider; and the broker's executable
> allowlist and refresh route follow the declared provider rather than naming GitHub. But only
> GitHub is registered, and the layers that remain GitHub-shaped are the egress allowlist, the
> installer and Terraform inputs, and the prompts. This document is the plan for the rest, and the
> order it has to happen in. Each section says what is true on `main` now and what the design
> changes.

**Scope:** What it would take for a kube-agents install to drive a forge that is not GitHub, and how
to get there without a flag day. GitLab is the worked example throughout because it is the one asked
for; nothing here is specific to it.
**Owns:** the repository-identity model, the provider contract as it grows past its first feature,
the per-provider token and git-credential shapes, the declarative surface and the vocabulary the
prompts use, where MCP fits, and the sequencing of all of it. The provider protocol's original
seven operations and the reasoning behind their normalisations belong to
[`pr-comment-conversation.md`](pr-comment-conversation.md) §3; credential containment belongs to
[`../credential-isolation-design.md`](../credential-isolation-design.md).

---

## 1. The problem

The Platform Agent opens pull requests, resolves issues, publishes audit ledgers and answers review
comments. All of it goes to GitHub, and most of it says so in code. An install whose GitOps
repository lives on GitLab cannot use any of it.

The coupling runs through five layers, each with a different owner and a different cost to unwind:

1. **The consumers.** Five scripts call the forge's API to get work done. Four go through a
   provider abstraction; `resolver.py` still shells `gh` behind a private runner of its own.
   (`github_token_refresh.py` and `credential_proxy.py` also run `gh`, but for credentials rather
   than for forge work; they are layer 3.)
2. **Repository identity.** `owner/repo` — exactly two path segments — was asserted in seven places
   across Python and Go, one regex expressing it copy-pasted into six modules. The widest assumption
   and the one least visible from any single file. Both halves now run through one parser each —
   `repo_ref.py` and `repo_ref.go` (§3) — and the CRD's admission check is one of the Go parser's
   callers.
3. **The credential plane.** The sandbox may hold no token, so every forge call is brokered. The
   allowlist and the refresh route now follow the declared provider (§5): the executables an install
   may run are derived from it, and `/v1/forge/refresh` dispatches to a per-provider refresher. What
   is still written for GitHub specifically is the git credential shape the broker writes, the
   token-minting pipeline behind it, and the FQDN network policy that decides where the pod may
   reach at all.
4. **The declarative surface.** The CRD now declares the forge — `spec.integration.git` carries a
   provider, validation dispatches on it, and the state ConfigMap records the declared provider
   rather than a constant — but only GitHub is registered, and the installer and Terraform
   composition still carry GitHub App inputs unconditionally.
5. **The prompts.** Four `SKILL.md` files instruct the model in `gh` spellings; seven governance
   SOPs name `gh` to forbid it and call the artefact a pull request throughout.

Layers 1, 2 and 3 are worth changing whether or not a second forge ever arrives — each one removes a
duplicated parser, a silent fallback, or a hardcoded host. Layer 2 is done: the duplicated parser
and `provider_for`'s silent fallback both went with §3, and the Go half followed. Layer 5 is only
worth changing for a second forge, and layer 4 almost was: its one standalone defect was that the CR
silently rewrote non-GitHub URLs into GitHub ones (§3), which was worth fixing on its own and did
not need any of the rest. That split says what is worth doing; it does not decide the order, which
§9 derives from three sequencing constraints instead — and one of those pulled part of layer 4
forward ahead of layers 1 and 3.

## 2. What already generalises

Two things exist and do not need designing again.

**The provider protocol.** `agents/platform/scripts/forge.py` defines `ForgeProvider` in three
groups — the original set that reading and answering a pull-request conversation needs, the ones §4
added for opening a change, and the ones the fleet audit's ledger needs — normalises three
GitHub-isms behind them (`can_write` as a boolean rather than `author_association`,
`supports_acknowledge` as a capability rather than an assumption, `normalise_login` folding the
spellings one account gets), and funnels every provider call through one `_call()` override point. `pr-comment-conversation.md` §3 explains each of those and why live
validation forced two of them; this document does not restate it.

**Provider selection.** `PROVIDERS` is a host-keyed table and `provider_for` reads it. Adding a
forge is a registration rather than a branch in a sweep.

A third is half-built, and the missing half is the one this design turns on. **A place to record
which forge a repository belongs to now exists.** The `managed_repos` state ConfigMap carries a list
of `{"type", "url"}` entries — `ManagedRepoEntry` in Go, `get_managed_repo_entries()` in Python — so
the discriminator is already per repository rather than per install, and already crosses the
operator-to-agent boundary.

Nothing dispatches on it, and the gap is already costing something. The operator now writes the
declared provider into `Type` rather than a constant, but with one provider registered every entry
it authors still reads `{Type: "github", URL: …}` — and it does not confine the field to that:
`parseManagedRepoEntries`
unmarshals whatever type string the ConfigMap holds, the merge writes existing entries back
verbatim, and `GitHubSpec.GitRepo`'s own comment invites a cluster administrator to register
repositories in that ConfigMap directly. A `{"type": "gitlab", …}` entry therefore survives
reconciliation intact and reaches the agent — where `get_managed_github_repos()` keeps the `github`
entries and returns bare slugs. It logs the ones it skips rather than dropping them in silence, so a
repository an administrator registered is visible as unsupported instead of indistinguishable from
one that was never registered; it is still skipped, because there is one provider to skip it in
favour of. The `pr_comments` sweep then calls `forge.provider_for()` with no argument at all, having
just discovered its repositories through that function, so it gets `GitHubProvider` from the default
rather than from the data.

`provider_for` takes a repository and parses its host, so a host the table does not know is a
rejection rather than a silent fallback — §3 covers how. What is still missing is the other
direction: the entry's declared `type` reaching the selection at all. That needs a second provider
to select, and lands with one (§9).

## 3. Repository identity

`owner/repo` was asserted across five modules and two languages, with no module able to see what
another was asserting. Every Python assertion now runs through one parser,
`agents/platform/scripts/repo_ref.py`, which returns a `RepoRef` carrying a host and an opaque,
arbitrary-depth path. It imports nothing outside the standard library, because the credential sidecar
validates across a trust boundary and must not pull in a module that shells out to `kubectl`.

- `repo_ref.parse` reads a host only where the syntax states one — a scheme, or the SCP `host:path`
  form. The one exception is a schemeless value whose first segment is a known forge host, which does
  name that host: that keeps `github.com/owner/repo` working without misreading `my.org/repo`, a
  legal bare slug, as a host and a one-segment path. Every segment is checked for traversal and
  leading dashes, so the component rules that used to sit beside three of the callers are applied to
  all of them — including `credential_proxy`, the one that had none, which is where the behaviour
  actually changes.
- `repo_ref.github_slug` is where the two-segment rule now lives: a per-provider validation on
  GitHub, applied to a ref whose host has already been parsed. `gitops_workspace.extract_github_slug`
  calls it against `github.com` alone, because it reads a URL the operator registered rather than a
  git remote, and `github_token_refresh.github_repo_from_remote` parses and then requires a host,
  because git cannot produce an `origin` of `acme/repo` and accepting one would let a stray config
  value stand in for a clone URL.
- `repo_ref.is_github_slug` is the predicate form, and it is stricter than the depth check alone: the
  value must already _be_ the slug rather than merely normalise to one.
  `gitops_workspace.is_valid_repo_slug`, `credential_proxy.is_valid_repository` and the inline check
  in `github_token_refresh.refresh_git_credentials` are calls to it, and all three answer about a
  string their caller then passes on verbatim — so a predicate that said yes about a value it had
  quietly trimmed would be answering about a string nobody holds.
- `forge._parse_repo` was the sixth. #504 removed the `SETTINGS.md` path that called it, so it is
  deleted rather than converted; `provider_for` calls `repo_ref.parse` directly.
- `CleanRepoSlugWithOrg` in the operator was the Go one. It stripped the scheme, a `user@` prefix,
  an SCP `host:` prefix and a `github.com/` prefix, then required exactly one slash in what was
  left, and `ValidateGitRepoURLWithOrg` — the CRD's admission check — was a call to it. Both are now
  deprecated shims over `repo_ref.go` and `GitProvider.Resolve`, and admission calls
  `validateGitIntegration` instead (§6).

The regex behind the bare-slug form used to be copy-pasted under its own name into `forge.py`,
`gitops_workspace.py`, `resolver.py`, `pr_conversation.py`, `audit_report.py` and
`submit_suggestion.py`, two of the copies already dead. All of them are gone.

GitLab projects live at arbitrary depth — `group/subgroup/project` is ordinary, not exotic. Both
parsers now carry one; what refuses it is the GitHub provider's two-segment rule, at the points
where GitHub is the provider. The difference is that the refusal is a provider's, and states a
reason, instead of being an invariant of the whole stack expressed in four dialects.

**The non-GitHub input that was not refused.** `CleanRepoSlugWithOrg` counted slashes _after_
discarding the host, so an SCP-style URL whose path held exactly one slash survived — and that is
the form GitLab's clone button hands you for a project sitting directly under its group.
`git@gitlab.com:group/project` was admitted by the CRD, reduced to `group/project`, and then
`CleanRepoURLWithOrg`, which prefixed a literal `https://github.com/` to any shorthand, wrote it
into the state ConfigMap as `{"type": "github", "url": "https://github.com/group/project"}`. The
GitLab repository was not rejected; it was rewritten into a GitHub one. The https spelling took a
different route to the same place — `CleanRepoURLWithOrg` returned http(s) URLs verbatim without
that reduction, so `https://gitlab.com/group/project` seeded an entry whose `type` and `url` named
different forges, and a test row pinned it. This was the layer-4 defect §1 says was worth fixing on
its own; both spellings are refused from step 2, by the host check in `GitProvider.Resolve`.

**What remains.** Both sides have one parser and one set of rules, and the host survives the parse
instead of being discarded before the slashes are counted. What the Python side does not yet have is
`RepoRef` as the currency between modules: every caller parses at its own boundary and hands on a
string, so the ref is a local variable rather than something passed. Making it the parameter type is
step 3 of §9, alongside the consumers that would carry it. One other thing is outstanding: the
entry's declared `type` reaching provider selection, which §2 describes and which needs a second
provider before it selects anything.

**Where #1085 now stands.** [#1085](https://github.com/gke-labs/kube-agents/issues/1085) reported
that `repo_from_settings` resolved `https://evil.example/victim-org/victim-repo` to
`victim-org/victim-repo` with no host check, pointing the token refresher at a repository the URL did
not name. That function is gone, which closed the host confusion the issue reported. The half the
issue deferred — "decide separately whether `ValidateGitRepoURL` should reject a non-GitHub host at
admission" — is answered by step 2: it does, and a host the declared provider does not serve is now
refused rather than discarded. #1085 closes there.

**A latent defect this removed.** `provider_for` used to have two ways of choosing wrong. It selected
by asking whether any key of the host table appeared anywhere in the repository string — a substring
test rather than a parsed host, so `https://example.invalid/github.com/o/r` selected
`GitHubProvider` — and it fell back to `GitHubProvider` for anything it did not match. Neither picked
the wrong provider, because no caller hands it a host: the sweep passes nothing at all, and both of
`pr-conversation`'s sources yield bare `owner/repo` slugs — `extract_github_slug` for the discovered
ones, `is_valid_repo_slug` for `--repo`. The fallback was the only branch taken, and the host table
was reached only from tests.

That was sound while there is one provider and a bare slug means GitHub. It stops being sound at the
second, because the table becomes load-bearing at exactly the moment a caller starts passing hosts —
and §4 adds three callers that resolve repositories their own way, of which `submit_suggestion.py`
and `audit_report.py` have since arrived. So selection parses
the host now, an unparseable repository raises `RepoUnparseable`, and a host the table does not know
raises `UnknownForgeHost` with a reason code, the way every other unresolvable input in this stack
does. A repository with no host still selects GitHub, which is what the shorthand means until §6
gives the CR somewhere else to point.

## 4. The provider contract past its first feature

`forge.py` began with two consumers — the `pr_comments` sweep in `github_scan_gate.py` and the
`pr-conversation` worker skill — and both are the same feature seen from its two ends. Everything
else was outside it: `resolver.py` and `audit_report.py` each carry a private `gh` runner of their
own, and `submit_suggestion.py` did not even have that, shelling `["gh", …]` inline at three call
sites. A second forge implemented against that `forge.py` therefore bought a reviewer conversation
and no issue resolution and no audit ledger.

`submit_suggestion.py` migrated first: the protocol gained `create_pull_request`,
`update_pull_request`, and `pull_request_url`, and opening a change became provider work.
`audit_report.py` followed, moving twenty call sites and adding the ledger group — labels, issues,
and the bulk pull-request reads the ledger reconciles against. It also
settled how a provider is wired to a caller that has its own runner: `audit_report.py` injects
`run_cmd`, so the command log, the FAILED line, and its test suite all keep seeing every forge
call. `resolver.py` is the one left, and it is its own change because its runner is under
active rewrite for unrelated reasons.

One thing `audit_report.py` could not take with it. `is_machine_author` — the gate that stops the
automation issuing itself a `/remediate` — reads `authorAssociation` and `viewerDidAuthor`, and
`forge.Comment` carries neither, so `issue_comments` and `pull_request_comments` return the forge's
own dictionaries. Until that dataclass widens, a second provider has to produce GitHub's comment
shape there. Widening it is a change to a security control and belongs on its own.

Migrating those onto the provider is the step that makes a forge a class rather than four
rewrites. It is anticipated rather than planned:
[`pr-comment-conversation.md`](pr-comment-conversation.md) §7 names `resolver.py` as the module's
obvious next consumer while holding the migration itself out of scope. Half of that has since
happened by another route — `resolver.py` dropped its own repository parser and imports
`gitops_workspace` for one instead — but it still runs its own `gh`, which is the half this section
is about. This design puts the rest on a schedule, and §9 says why that schedule puts it before any
GitLab code.

The protocol grows to the union of what the four need, and the list fell out of the migrations
rather than being guessed: every method on it is there because a caller in this repository had a
`gh` invocation that had to stop being one. What matters to this design is that it is decided by the
callers, not by GitHub's API surface, and that harness policy stays above the provider. The existing
split is the precedent: the provider answers
"what is open" and the caller answers "which of those are mine", so the branch-prefix and
`agent:ignore` rules are written once instead of once per forge.

Two provider shapes come out of the credential plane rather than out of this section, and §5 explains
why: a CLI-backed provider that shells a brokered binary, and a proxy-backed provider that speaks
REST through a sidecar route. Both implement the same protocol; `_call()` is where they differ.

## 5. The credential plane

The governing constraint is [`../credential-isolation-design.md`](../credential-isolation-design.md):
the agent sandbox receives no API keys or access tokens through its environment or filesystem, and
no ServiceAccount token either. Every forge call is therefore brokered by the credential broker,
which runs in a Pod of its own, and a second forge is a change to the broker before it is a change
to the agent.

Five things named GitHub: three inside the broker, the minting pipeline behind it, and the network
policy that lets the pod out at all. The first two are done and the rest are not; each paragraph
below says which it is.

**The executable allowlist.** `ALLOWED_EXECUTABLES` was `("gcloud", "kubectl", "gh", "git")`, a
class attribute of `CommandExecutor` read from three places — and `credential_proxy_client.py`
carried the same set again as `SUPPORTED_EXECUTABLES`, so the step was two files rather than one.
GitLab has `glab`, so a GitLab install wants that entry and a GitHub install must not have it: an
allowlist that is the union of every supported forge grants every install more than it uses.

`agents/platform/scripts/forge_clis.py` now derives it. The base tools every install runs are
constant; the forge tool is looked up per configured provider, and the operator renders
`CREDENTIAL_PROXY_FORGE_PROVIDERS` from `spec.integration.git.provider`.

Closing the two-files gap took both halves: the readers import the one module, and the operator
renders the variable onto both containers. Sharing the module alone would not have done it. The two
readers sit on opposite sides of the credential boundary and run in different containers, so a
variable rendered into the sidecar alone leaves the shim deriving from an unset value — silently the
GitHub set, which on a GitLab install is both failure modes at once: narrower than the enforcer, so
it refuses `glab`, and wider, so it offers `gh`. A shim narrower than the enforcer refuses a tool
the install is entitled to, and a wider one turns a clear local refusal into a confusing remote one.

The variable is safe in the sandbox because it carries provider names and the name-to-binary table
it selects from is compiled into the image, so the sandbox cannot widen its own allowlist by editing
it — and the sidecar enforces against its own copy either way.

The naive reading of "install configuration" would be to put the allowlist itself in the
environment, and that would be a regression: anything able to set one variable on the sidecar could
then run any binary on it. So the table of provider-name-to-executable is compiled in and the
variable only selects from it. The operator configures provider names, never binaries, and
`mergeCredentialProxyEnv` reserves the variable so a plugin cannot set it either.

Not every forge has a CLI. Bitbucket Cloud has none, which is why `forge.py` was built with the
`_call()` seam in the first place: a provider with no binary to shell needs a `/v1/<forge>/…` route
on the sidecar and reaches it through that one method. Both shapes are supported and neither is
preferred; the choice is a property of the forge.

**The refresh route.** `/v1/github/refresh` was a path, not a parameter. It is now
`/v1/forge/refresh` with the provider in the body. A request on the old path names no provider and
means GitHub, so both converge on one handler rather than two. Being in the sidecar's refresher
table is not permission to use it: the handler also checks the provider against the configured set,
which is what stops an agent asking for a forge the administrator never declared.

The two images are versioned independently — an operator may pin `CREDENTIAL_PROXY_IMAGE` to a
mirrored digest while the agent tracks a tag — so a pair one release apart is a supported shape
rather than a moment during a rollout, and each direction of the skew needs its own answer. The
sidecar keeps serving the old path, which covers an old agent against a new sidecar. The client
retries the old path on a 404, which covers the reverse; without it that pair loses every credential
refresh, and so every git write the agent makes. The retry is conditioned on 404 alone, and only for
GitHub: a sidecar that serves the route answers 200, 400 or 502 and never 404, so no genuine refusal
is sent twice, and a sidecar old enough to lack the route is old enough to serve GitHub alone, so
retrying another provider there would mint a GitHub credential for a repository that is not on
GitHub.

**Git credentials.** `refresh_git_credentials` writes no credential line of its own. It runs
`gh auth login --with-token` and then `gh auth setup-git`, which installs the GitHub CLI itself as
git's credential helper — `credential.helper = !gh auth git-credential`. The forge coupling here is
therefore not a username or a URL template that a provider could supply; it is that the helper is a
forge's CLI. `glab` has an equivalent and would work the same way, but a forge with no CLI has no
binary to install as a helper at all, so the sidecar has to serve one. That is the same split as the
paragraph above: a CLI-backed provider configures a helper, a proxy-backed provider needs one
written for it.

That is why the dispatch is a refresher script per provider, chosen by the sidecar, rather than a
provider branch inside one script. `github_token_refresh.py` is GitHub's strategy — Minty, a signed
JWT, `gh auth setup-git` — and it refuses a provider it does not serve rather than proceeding, since
proceeding would ask Minty for a GitHub token in the name of a repository that is not on GitHub.

**Token acquisition, which is where the two forges genuinely diverge.** GitHub App installation
tokens must be minted from a JWT signed by the App's private key and expire hourly, so the install
runs Minty — the workload in `charts/kube-agents/templates/github-minter.yaml`, with the KMS key and
service accounts behind it provisioned by `terraform/modules/github-minter`. That apparatus follows
from App tokens being short-lived and signature-derived. A GitLab group or project access token is a
long-lived string with no minting step at all, so the broker's job for GitLab is storage and scoping
rather than signing, and the KMS key, the signing service and the `github-token-minter-config`
policy ConfigMap have no analogue to build.

That asymmetry decides the shape: token acquisition is a **strategy selected per provider**, not a
pipeline every forge is fitted into. GitHub keeps Minty. GitLab starts with a group access token in a
Secret mounted **into the credential broker** — never into the agent sandbox, which is what the
first paragraph of this section forbids — which is the smallest thing that works and the one an
operator can rotate without new infrastructure. OIDC token exchange is the better long-run answer
for GitLab and is deliberately deferred: it is a second design, and the first working install does
not need it.

**Egress.** `github.com`, `*.github.com` and `*.githubusercontent.com` are literals in the FQDN
network policy, written twice: the operator renders them in
`k8s-operator/internal/controller/platformagent_manifests.go`, which is what a real install gets,
and `deploy/kustomize/gke-dataplane-v2/fqdn-networkpolicy.yaml` carries the same three for the dev
path. Both derive from the configured forge hosts instead — changing only the kustomize copy leaves
every shipped install unchanged. A self-managed GitLab is at a customer-chosen hostname, so no
literal could have covered it.

## 6. The declarative surface

`IntegrationSpec` used to hold exactly one forge field, `GitHub *GitHubSpec`, and `GitHubSpec` two:
`GitRepo` and `Org`. (`PlatformAgentIntegrationSpec` embeds it alongside `GoogleChat` and `Slack`, so
GitHub is the only _forge_ integration rather than the only integration.) `Org` carried GitHub's
namespace grammar in a CRD pattern — alphanumerics and hyphens, at most 39 characters — which is not
GitLab's: a group path admits dots and underscores, and a project can sit several groups deep, so no
value of `org` names a nested GitLab namespace. `GitRepo`'s validation, `ValidateGitRepoURLWithOrg`,
checked length and non-graphic runes and then deferred to `CleanRepoSlugWithOrg`, so what the CR
enforced about the repository was "exactly one slash once the host has been discarded" — and nothing
at all about the host. The declarative surface named GitHub in the field path and nowhere in the
validation, and the check that did fire was a shape check standing in for the host check §3 shows is
the one that matters.

The operator writes the repository into the `managed_repos` state ConfigMap as a `ManagedRepoEntry`.
§2 covers what the agent does with that; what belongs here is that the discriminator this design
needs already had a field, a schema and a transport, and that the only thing missing at this layer
was a way to declare it — which is why an administrator who writes one straight into the ConfigMap
gets an entry the operator preserves and the agent discards.

Step 2 made that declaration `spec.integration.git`, carrying a provider, a host, a repository and a
namespace, with `spec.integration.github` kept as a deprecated alias that resolves to the same
thing. Setting both is refused rather than given a precedence rule. Validation is
provider-dispatched — each provider asserting its own namespace grammar, and each rejecting a host
that is not its own — rather than one host-blind shape check standing in for all of them, and
`ManagedRepoEntry.Type` carries the declared provider instead of a constant, which is how the
discriminator reaches the agent: written down by the operator, rather than inferred from the URL's
text. Only GitHub is registered; the CRD enum widens with the agent-side provider that honours it
(§9 step 5), because a provider the CRD accepts and the agent discards is the worse failure.

`install.sh` and `terraform/examples/full-install` carry the GitHub App inputs as
`github_app_id`, `enable_github_minter` and `github_minter_kms_*`; the chart spells the same
settings `githubMinter.appId`, `githubMinter.enabled` and `githubMinter.kms.*`. All of them become
provider-conditional: an install that declares GitLab provisions no KMS key and no minter.

The chart is guarded from step 2, because that is where the forge declaration lives — but guarded
rather than conditional: `githubMinter.enabled` alongside a non-GitHub provider fails the render.
That is the right shape while GitLab has no credential path of its own, since silently rendering no
minter would leave the agent with no token source at all; step 5 turns the guard into the condition
the paragraph above describes, once there is something to render instead. The installer and
Terraform halves wait for step 5 too, when there is a second provider to select. A `terraform`
variable whose only legal value is its default is configuration nobody can set, and the branch it
would guard could not be exercised.

## 7. Vocabulary

Two kinds of prompt name the forge, and they need different work.

Four `SKILL.md` files instruct the model in `gh` spellings and call the artefact a pull request:
`fleet-audit`, `pr-conversation` and `submit-suggestion` under `agents/platform/skills/`, and
`gke-stockout-investigator` under `agentplugins/`, which reaches an install through the
`AgentPlugin` CRD rather than through the agent image and so is easy to miss. These want the command
behind a wrapper and the noun taken from configuration.

The seven governance SOPs name `gh` only to forbid it — "never run `gh issue create`", "the helper
owns every `git`/`gh` operation" — because `audit_report.py` owns their write path. A prohibition has
no command to wrap, so the SOP work is smaller and different: the nouns ("pull request", "PR body")
come from configuration, and the prohibitions get reworded to match a helper that now reaches a
provider rather than `gh`. §4 has already moved the helper; nothing here has been reworded yet.

`github-issue-resolver`, the skill a reader would expect on the first list, is not on it: its prompt
names no forge command — only its own `resolver.py` subcommands — and its coupling is entirely in
that script, which §4 also moves.

`pr-comment-conversation.md` §6 already prescribes this for the worker skill, which is told to take
`forge` and `noun` from the card "so one prompt serves a forge whose users call them merge requests".
This design extends the same rule to the SOPs rather than inventing a second convention.

## 8. Why an MCP server is an addition, not the mechanism

A GitLab MCP server is a reasonable thing to want, and it cannot carry this design. Two reasons, and
they are independent.

**Half the forge work has no model in the loop.** `github_scan_gate.py` is a `no_agent` cron script
by deliberate design: an idle tick costs a handful of `gh` calls, no model turn and no tokens, which
is the whole reason the earlier prompt-driven poller was retired. MCP is a model-facing tool
surface, so it cannot serve that sweep, and it cannot serve the audit publish path or the
guardrails in
`submit_suggestion.py` either. Those need an importable library whatever else exists.

**An MCP server in the sandbox would hold a token.** MCP servers are spawned as child processes of
the agent container. A `GITLAB_TOKEN` in that process's environment is precisely what
`credential-isolation-design.md` guarantees against. The two remote MCP servers that ship today are
not a counterexample: they authenticate with a forked `mcp-remote` that mints Google ADC tokens per
call, and ADC is ambient to the pod rather than injected as a secret. A forge PAT has no ambient
equivalent.

The second is answered by running the MCP server as a trusted sidecar beside the credential proxy,
with the agent reaching it over loopback and the sidecar attaching the token — the same containment
the `_call()` seam already provides for a proxy-backed provider, expressed at the MCP layer instead
of the REST one. The first has no such answer, because no arrangement of MCP servers puts a model in
a cron loop that deliberately has none. The library stays either way.

Where it pays off is the interactive path — an agent asked in chat to read a merge request, or a
worker turn answering a review comment. There, typed MCP tools are better than teaching a model
`glab` spellings in SOP prose. So the provider is the mechanism and MCP is an optional surface on top
of it, added last and depended on by nothing.

## 9. Implementation order

The order is not preference. Three constraints fix most of it.

**Reader before writer.** The provider discriminator §2 calls for crosses a process boundary: the Go
operator declares it, the Python agent acts on it. Widening what the writer may emit before the
reader accepts it produces a release where a valid CR is rejected inside the pod, and the operator
sees a reconcile that succeeded and an agent that will not start. Repository identity therefore
lands in Python first and in Go second — and the reader here is not only the parser. It is
`get_managed_github_repos()`, which skips every entry whose `type` is not `github`. Teaching the
operator to emit `type: gitlab` while that skip still runs is exactly the failure this constraint
describes: a reconcile that succeeds and a repository the agent never sees. A reader cannot accept a
type it has no provider for, so what step 1 does is make the skip visible in the log — which covers
the one path that produces such an entry today, an administrator editing the ConfigMap by hand. The
dispatch itself has to arrive no later than the provider it dispatches to, which is step 5.

That pairing is also why the Go half of layer 4 runs ahead of layers 1 and 3, which §1 ranks as the
work worth doing regardless. Both of those dispatch on the provider — the consumer migration decides
which provider a caller gets, the credential plane decides which token and which binary — so the
discriminator has to be declared before either has anything to dispatch on. Sequencing it later
means building both against the inference §2 describes and then rewriting them.

**No-behaviour-change before behaviour change.** The consumer migration (§4) is a large diff with no
functional delta, verifiable against the GitHub install that already exists. Landing it before any
GitLab code means a reviewer reads one thing at a time, and a regression has one candidate cause.

**Everything provable on GitHub, before anything that needs GitLab.** Steps 1 to 4 can each be
exercised against the running GitHub install by showing the existing behaviour unchanged — including
the CRD step, where the evidence is a GitHub CR still admitting and reconciling through the new
`spec.integration.git` shape and its alias. The first change that cannot is the `GitLabProvider`
itself, which needs a real GitLab project to validate against. That
environment does not exist here today — see §10 — so the design puts every step that does not need it
first, and none of that work is stranded if the environment question takes a while to answer.

The resulting sequence:

1. **Repository identity** (§3): `RepoRef` in Python, with the host parsed and carried rather than
   discarded and unknown hosts raising. The Python assertions become callers, the duplicated regex
   goes, and `get_managed_github_repos()` logs what it skips instead of dropping it in silence.
   **Landed.**
2. **The declarative surface, Go half** (§6): `spec.integration.git`, the deprecated alias,
   provider-dispatched validation, and `ManagedRepoEntry.Type` carrying the declared provider for
   the agent to read. §3's SCP rewrite is fixed here and #1085 closes here, because this step is
   already rewriting that admission path — either could land ahead of the sequence instead. The
   chart gains the guard §6 describes here; making its GitHub App inputs, the installer's and
   Terraform's actually conditional waits for step 5, for the reason §6 gives. **Landed.**
3. **The consumer migration** (§4): protocol widened, the three remaining scripts moved onto it.
   Splits naturally by consumer, and does: `submit_suggestion.py` and the three pull-request
   methods it needed have **landed**, and so has `audit_report.py` with the ledger group.
   `resolver.py` has not.
4. **The credential plane** (§5): route, executable allowlist and egress allowlist parameterised,
   and the git credential helper selected per provider — for a CLI-backed forge that is its own
   `setup-git` equivalent, for a proxy-backed one a helper the sidecar serves. Still one provider.
   Splits by layer: the broker's own surface — the allowlist, the refresh route, the per-provider
   refresher — has **landed**. The egress allowlist has not, and is the whole of what remains here.
   The installer's and Terraform's GitHub App inputs are step 5's, with the provider that gives
   their second branch a value to take.
5. **`GitLabProvider`** (§4, §5): the first new forge, with a Secret-backed token. `install.sh` and
   `terraform/examples/full-install` become provider-conditional here, alongside the provider that
   gives their second branch a value to take.
6. **Vocabulary** (§7): the four skills, then the SOPs' nouns and prohibitions.
7. **MCP sidecar** (§8), if wanted. Depended on by nothing above.

## 10. Open questions

- **Where a GitLab install gets validated.** No environment here has a GitLab, and step 5 cannot fill
  in a Live validation section without one — nor can steps 6 and 7 be shown doing what they are for,
  though both can at least be shown not to regress GitHub. The choice is a gitlab.com project with
  a group access token, or a self-managed GitLab in the development cluster. Self-managed also
  exercises the
  customer-chosen-hostname path that no literal egress rule could cover, which argues for it, at the
  cost of standing infrastructure.
- **Whether gitlab.com and self-managed GitLab are one provider or two.**
  `pr-comment-conversation.md` §3 records that "Bitbucket" is two providers sharing almost nothing.
  GitLab is better off than that — the API is the same — but the token model, the host and the
  network path all differ, and a single class that branches on "is this gitlab.com" is how the
  Bitbucket mistake would be repeated in a smaller way.
- **Whether one field can name the token's scope boundary on both forges** (§6). On GitHub that
  boundary is an App installation and it lines up with the first path component. On GitLab it is a
  group, a project's namespace can sit several segments below it, and "the first path component" is
  therefore false. Either the field generalises to "scope boundary" and each provider says how to
  derive it, or the two forges want different fields. The token-scoping code depends on the answer,
  and so does whether a per-repository permission policy has anywhere to hang.
- **Whether the Minty policy ConfigMap has an analogue.** GitHub's per-repository permission policy
  is enforced at mint time. A long-lived GitLab token carries its scope from creation, so the
  equivalent enforcement — if it is wanted — has to live somewhere else, most plausibly as a check in
  the broker before it brokers.
