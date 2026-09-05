# Version control and issue tracking

> **STATUS — design of record; not implemented.** Today an install drives exactly
> one forge, GitHub, and most of the code says so by name. This is the design for
> driving any of them, and the order it has to happen in.

**Scope:** what it takes for a kube-agents install to read and change a
repository, open and answer change proposals, and file and resolve issues on a
version-control forge that is not GitHub. GitLab is the worked example because it
is the one asked for; Bitbucket is the third and is designed for by not being
designed for. Issue trackers that are not part of a forge — Jira alongside
Bitbucket being the case that arrives first — are named and deliberately left
undesigned.

## Summary

An agent that manages infrastructure has to read and change the repository that
describes it. Prospective customers do not all keep that repository on GitHub,
and an agent that speaks only GitHub is an agent they cannot evaluate. **GitLab
and Bitbucket** are the two needed in the short term, and the list is not closed.

So version control and issue tracking become one abstraction with a modular
per-provider layer behind it. Four things carry it:

**A neutral vocabulary.** The caller is a language model, so the verbs are named
for the concepts every system shares rather than for one forge's spelling: a
change offered for review is a `proposal`, not a pull request or a merge
request. Both familiar spellings work as aliases.

**A seam at the credential boundary.** The agent sandbox holds no token, so every
call that spends one is brokered. The sandbox sends a repository and a verb; the
broker decides which provider that repository belongs to, calls it, and answers
in the neutral concepts. Nothing crosses the seam in a forge's own vocabulary.

**Only the remote half is abstracted.** All three target systems are forges of
_git_ — the version-control system underneath is the same one and only the
collaboration layer on top differs. Within that, only operations that cross the
network or spend a credential actually differ. `log`, `annotate`, `show`, `diff`
and file modes are git in every case, so they run natively in the sandbox on a
working copy with no origin and no credential, through no abstraction at all.

**One directory per provider.** Adding a system is a new package and one line in
a registration file. Two tests make that a build failure rather than a
convention.

The design was measured against the two repository-access designs this
repository already has, on identical probes: it answered the most probes, took
the fewest turns, and was the only one whose cost did not grow with repository
size. Method and results in [The experiment](#9-the-experiment).

| Layer                        | Where it goes                                                        |
| ---------------------------- | -------------------------------------------------------------------- |
| The sandbox client           | `agents/platform/skills/version-control/`, and `vcs.py`              |
| The broker                   | `agents/platform/scripts/vcs_broker.py`, with no provider name in it |
| The shared provider contract | `agents/platform/scripts/providers/`                                 |
| Each provider                | `providers/github/`, `providers/gitlab/` — one directory each        |
| Registration                 | `providers/registry.py` — the one shared file a new provider edits   |
| The declarative surface      | `spec.integration.git` on the CR                                     |
| The local git                | `/opt/vcs/libexec/git` in the sandbox image                          |

## How to read this document

It is long, and it is layered so that a human reader can stop as soon as they
have what they came for. Each section goes a level deeper than the one before
it. An agent should read all of it.

| Section                                                         | What it gives you                                                                                                               |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| [1. Why](#1-why)                                                | the customer requirement, where GitHub is named today, and the shape of the answer — **stop here if that is what you came for** |
| [2. The concepts](#2-the-concepts)                              | what the systems call things, which words the verbs use, and how a repository is named                                          |
| [3. The seam](#3-the-seam)                                      | the broker, the bundle transport, the sandbox’s one git, the routes and the error contract                                      |
| [4. The provider interface](#4-the-provider-interface)          | what a provider supplies, and the decisions a second forge forces                                                               |
| [5. Modularity](#5-modularity)                                  | the package layout, and what makes the boundary hold at the third forge                                                         |
| [6. The declarative surface](#6-the-declarative-surface)        | how an install says which provider it uses                                                                                      |
| [7. GitLab](#7-gitlab)                                          | the worked example: identity, credential, translation, errors                                                                   |
| [8. MCP](#8-why-an-mcp-server-is-an-addition-not-the-mechanism) | why a forge MCP server is an addition rather than the mechanism                                                                 |
| [9. The experiment](#9-the-experiment)                          | how this was measured against the two existing designs, and the results                                                         |
| [10. What this does not fix](#10-what-this-does-not-fix)        | the limits that remain once it all lands                                                                                        |
| [11. Delivery](#11-delivery)                                    | the order, and what each step can be held to                                                                                    |
| [12. Open questions](#12-open-questions)                        | what is still undecided                                                                                                         |

---

## 1. Why

### What customers are asking for

GitLab and Bitbucket are the two required in the short term, and the list being
open is what shapes the design rather than the two names on it. Self-hosted
GitLab and Gerrit come up often enough that a design which handles three by
enumeration and a fourth by rewrite is the wrong design.

That demand is the whole reason the work exists. It is worth being explicit that
nothing else is: this is not a response to a deficiency in how repositories are
reached today. The mechanisms this repository ships work, and one of them is the
baseline the new one had to match.

### Where GitHub is named today

The Platform Agent opens pull requests, resolves issues, publishes audit ledgers and answers review
comments. All of it goes to GitHub, and most of it says so in code. An install whose GitOps
repository lives on GitLab cannot use any of it.

The coupling runs through five layers, each with a different owner and a different cost to unwind:

1. **The consumers.** Six scripts call the forge's API to get work done. Three go through a
   provider abstraction — `pr_conversation.py`, `pr_triggers.py` and `github_scan_gate.py`, all on
   `forge.py`; three shell `gh` directly — `resolver.py`, `submit_suggestion.py` and
   `audit_report.py`, two behind a private runner of their own and one inline. A seventh,
   `inspect_repository.py`, reaches a repository rather than an API, and does it by running
   `git clone` through the sandbox's credential shim. (`github_token_refresh.py` and
   `credential_proxy.py` also run `gh`, but for credentials rather than for forge work; they are
   layer 3.)
2. **Repository identity.** `owner/repo` — exactly two path segments — is asserted in seven places
   across Python and Go, and one regex expressing it is copy-pasted into six modules. This is the
   widest assumption and the one least visible from any single file.
3. **The credential plane.** The sandbox may hold no token, so every forge call is brokered. The
   broker's executable allowlist, its refresh route, the git credential shape it writes, and the
   token-minting pipeline behind it are each written for GitHub specifically — as is the FQDN
   network policy that decides where the pod may reach at all.
4. **The declarative surface.** `GitHubSpec` is the only forge integration in the CRD, its `org`
   field takes GitHub's namespace grammar, the state ConfigMap the operator writes labels every
   repository `github` by a constant, and the chart, installer and Terraform composition all carry
   GitHub App inputs.
5. **The prompts.** Four `SKILL.md` files instruct the model in `gh` spellings; seven governance
   SOPs name `gh` to forbid it and call the artefact a pull request throughout.

Layers 1, 2 and 3 are worth changing whether or not a second forge ever arrives — each one removes a
duplicated parser, a silent fallback, or a hardcoded host. Layer 5 is only worth changing for a
second forge, and layer 4 almost is: its one standalone defect is that the CR silently rewrites one
shape of non-GitHub URL into a GitHub one ([Repository identity](#repository-identity)), which is
worth fixing on its own but does not need any of this. That split says what is worth doing; it does
not decide the order, which [Delivery](#11-delivery) derives from three sequencing constraints
instead — and one of those pulls part of layer 4 forward ahead of layers 1 and 3.

### Why one abstraction rather than three integrations

The alternative is to add GitLab and Bitbucket the way GitHub was added — each
one threaded through the skills, the scripts, the credential minter and the
governance procedures that name a forge. That multiplies by the number of
forges in every one of those places, and each new one has to be added to all of
them again.

An abstraction pays for itself the moment there is a second forge, provided the
seam is in the right place. Putting it between the sandbox and the credentialed
broker is what makes the rest fall out: the sandbox has no forge knowledge at
all, so nothing in a skill, a prompt or an agent's habits has to change when an
install points at GitLab.

### What is abstracted, and what stays native

"Forges of git" is the fact the design leans on hardest, and the split it
implies is sharper than it first looks:

- **Remote operations are abstracted.** Anything that crosses the network or
  spends a credential: fetching a repository, sending revisions back, opening
  and reading change proposals, listing and commenting on issues. These genuinely
  differ per forge — different APIs, different authentication, different nouns —
  and they are the ones that need a credential the sandbox must not hold.
- **Local operations stay native.** `log`, `annotate`, `show`, `diff`,
  `status`, `grep`, file modes, walking history: these are `git`, invoked
  directly by the agent, at full fidelity, on a working copy in the sandbox. That
  working copy has **no origin and no credential**, so the native command cannot
  reach a network even if something asks it to.

The practical consequence is that the abstraction is small. It covers the verbs
that had to be covered and nothing else, and the agent keeps the tool it already
knows for the majority of the work. A model asked to find when a policy changed
runs `git log`, not a protocol verb.

The same split is what keeps a repository's own contents away from the
credential. History moves as a git bundle — objects and refs, no `.git/config`,
no hooks, no remote URL — so the credentialed side never checks out a tree that
a repository or a sandbox authored. That property is described in
[The shape](#the-shape) and it is worth having, but it is a consequence of the
transport rather than the reason for the work.

### What already generalises

Two things exist and do not need designing again.

**The provider protocol.** `agents/platform/scripts/forge.py` defines `ForgeProvider` as seven
operations, normalises three GitHub-isms behind them (`can_write` as a boolean rather than
`author_association`, `supports_acknowledge` as a capability rather than an assumption,
`normalise_login` folding the spellings one account gets), and funnels every provider call through
one `_call()` override point. `pr-comment-conversation.md` §3 explains each of those and why live
validation forced two of them; this document does not restate it.

**Provider selection.** `PROVIDERS` is a host-keyed table and `provider_for` reads it. Adding a
forge is a registration rather than a branch in a sweep.

A third is half-built, and the missing half is the one this design turns on. **A place to record
which forge a repository belongs to now exists.** The `managed_repos` state ConfigMap carries a list
of `{"type", "url"}` entries — `ManagedRepoEntry` in Go, `get_managed_repo_entries()` in Python — so
the discriminator is already per repository rather than per install, and already crosses the
operator-to-agent boundary.

Nothing dispatches on it, and the gap is already costing something. The operator only ever authors
`{Type: "github", URL: …}`, but it does not confine the field to that: `parseManagedRepoEntries`
unmarshals whatever type string the ConfigMap holds, the merge writes existing entries back
verbatim, and `GitHubSpec.GitRepo`'s own comment invites a cluster administrator to register
repositories in that ConfigMap directly. A `{"type": "gitlab", …}` entry therefore survives
reconciliation intact and reaches the agent — where `get_managed_github_repos()` keeps the `github`
entries, drops the rest without a word, and returns bare slugs. A repository an administrator
registered disappears at the one point where the forge could have chosen a provider from it. The
`pr_comments` sweep then calls `forge.provider_for()` with no argument at all, having just
discovered its repositories through that function, so it gets `GitHubProvider` from the default
rather than from the data.

`provider_for` does take a repository now, and `pr-conversation` passes one, but it infers the forge
from the string rather than reading it from the entry — [Repository identity](#repository-identity)
covers how. The discriminator has to be
dispatched on rather than filtered by, so that a host the table does not know is a rejection rather
than a silent drop.

### How this compares to what we have

The two designs it was measured against are the shared-volume credential proxy
and content passing, on identical probes and identical corpora: twenty
read probes at three repository sizes, plus a four-probe write rung. It was also
the only one of the three that never reached past its own interface. Full method
and results in [The experiment](#9-the-experiment).

Those numbers are a sanity check, not the justification. The justification is
the customer requirement above. What the measurement establishes is that meeting
it costs nothing in capability or speed — which is the thing that would have
stopped the work.

---

## 2. The concepts

The caller is a language model, so the verb names were chosen as a research
question rather than a naming preference: which words for these concepts is a
model most likely to already understand? Version-control concepts are stable
across systems and the spellings are not, so where the systems disagree the
neutral name is the command and the familiar one is an alias. Both always work.

### Where the names come from

The sources consulted were the systems' own command sets and reference
documentation — git, Mercurial, Subversion, Bazaar/Breezy, Jujutsu, Fossil and
Darcs — together with Eric S. Raymond's _Understanding Version-Control Systems_,
the _Version Control with Subversion_ book's terminology chapter, and
Wikipedia's _Version control_ article, whose "common terminology" section is
where the cross-system vocabulary is written down as vocabulary rather than as
one tool's manual.

For the collaboration half, which no version-control system defines, the source
is the cross-forge tooling: Launchpad's and Breezy's _merge proposal_, and
Jelmer Vernooij's `silver-platter`, which drives GitHub, GitLab and Launchpad
through one `MergeProposal` abstraction and is the closest existing answer to
this problem.

### The vocabulary

| Concept                     | Command    | Aliases    | Why this name                                                                                                                                                                                                                                                                                               |
| --------------------------- | ---------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Per-line attribution        | `annotate` | `blame`    | Mercurial's command is `annotate`; Subversion accepts `annotate` and `ann` alongside `blame`; Breezy's is `annotate`; Jujutsu's is `jj file annotate`. Git is the outlier in leading with `blame`, and it accepts `annotate` too.                                                                           |
| Revision history            | `log`      | `history`  | `log` is consensus across git, Mercurial, Subversion and Breezy. Fossil's `timeline` is the only dissent.                                                                                                                                                                                                   |
| The tracked file set        | `files`    | `manifest` | Mercurial has both and its own documentation prefers `files`; `manifest` is a Mercurial-internal noun that means nothing elsewhere.                                                                                                                                                                         |
| Text search                 | `grep`     | `search`   | git and Mercurial both spell it `grep`, and the Unix name is the one a model has the most exposure to.                                                                                                                                                                                                      |
| Sending revisions upstream  | `publish`  | `push`     | Mercurial's phase model is where this is a concept: a _publishing repository_ is one that makes changesets public. `push` is the DVCS spelling, and here it would be a lie — this working copy has no remote to push to, and the word invites `--force` and an `origin` that do not exist.                  |
| A change offered for review | `proposal` | `pr`, `mr` | Launchpad and Breezy call it a _merge proposal_, and `silver-platter` settled on the same term for exactly this cross-forge problem. "Pull request" carries GitHub's fork-and-branch assumption, "merge request" is GitLab's, and Gerrit's unit of review is a single revision rather than a branch at all. |
| Dropping the working copy   | `discard`  | `close`    | Named for what it does. `close` implies a counterpart that was opened, and these routes hold no state — there is nothing on the credential side to release.                                                                                                                                                 |
| Work items                  | `issue`    | —          | The one noun every forge already agrees on.                                                                                                                                                                                                                                                                 |

`clone`, `commit`, `branch`, `diff`, `show` and `status` needed no decision;
they are the same word in every system that has the concept.

### Why the grammar changes between layers

One inconsistency is deliberate. The repository verbs are verb-first (`clone`,
`commit`, `publish`) and the collaboration verbs are noun-first
(`proposal create`, `issue list`). The grammar marks the layer: verb-first is
the version-control system, noun-first is the forge. A caller that notices the
difference has noticed something true about where the work happens — and about
which half varies between GitHub, GitLab and Bitbucket.

`create` takes `open` as an alias on both nouns, and that one runs the other way
round from the table above: every forge says _open a pull request_ and none of
them says _create_ one, so here the familiar word is the one a model reaches for
first and `create` is kept only because it is what the wire verb is called.

### Repository identity

`owner/repo` is asserted in seven places, across five modules and two languages, and no module can
see what another is asserting:

- `forge._parse_repo` matches a `github.com` URL or a bare one-slash slug, and raises
  `RepoUnparseable` otherwise.
- `gitops_workspace.is_valid_repo_slug` matches the same one-slash shape and returns a boolean, with
  `_valid_repo_component` separately rejecting `..` and a leading dash in either half.
- `gitops_workspace.extract_github_slug` strips one of four literal GitHub prefixes and returns
  `None` for anything else that looks like a URL or an SCP endpoint.
- `github_token_refresh.github_repo_from_remote` returns `owner/repo` from a git remote, and returns
  `None` for a host that is not GitHub.
- `github_token_refresh.refresh_git_credentials` asserts it inline — `repository.count("/") != 1`
  raises — on the path every token refresh takes, brokered or direct.
- `credential_proxy.is_valid_repository` splits on the first `/` and requires the remainder to hold
  no further separator, so a deeper path fails validation.
- `CleanRepoSlugWithOrg` in the operator strips the scheme, a `user@` prefix, an SCP `host:` prefix
  and a `github.com/` prefix, then requires exactly one slash in what is left.
  `ValidateGitRepoURLWithOrg` — the CRD's admission check — is a call to it, so admission and
  normalisation are one rule.

The regex behind the bare-slug form is additionally copy-pasted under its own name into six Python
modules: `forge.py`, `gitops_workspace.py`, `resolver.py`, `pr_conversation.py`, `audit_report.py`
and `submit_suggestion.py`. Two of those six copies are already dead — defined and never referenced
again — which is what unmanaged duplication looks like before anyone tries to change the shape.

GitLab projects live at arbitrary depth — `group/subgroup/project` is ordinary, not exotic — and none
of the seven handles one. All of them refuse it, in four dialects: two raise (a reason-coded
`RepoUnparseable`, and a bare `RuntimeError`), two return `None`, two return `False`, and one
returns a Go `error`.

**The one non-GitHub input that is not refused.** `CleanRepoSlugWithOrg` counts slashes _after_
discarding the host, so an SCP-style URL whose path holds exactly one slash survives — and that is
the form GitLab's clone button hands you for a project sitting directly under its group.
`git@gitlab.com:group/project` is admitted by the CRD, reduced to `group/project`, and then
`CleanRepoURLWithOrg`, which prefixes a literal `https://github.com/` to any shorthand, writes it
into the state ConfigMap as `{"type": "github", "url": "https://github.com/group/project"}`. The
GitLab repository is not rejected; it is rewritten into a GitHub one and labelled `github` by the
constant above describes. Every reader downstream then behaves correctly, on a repository the operator
invented. This is the layer-4 defect [Where GitHub is named today](#where-github-is-named-today) says is
worth fixing on its own.

**The change.** One `RepoRef` carrying a host and an opaque, arbitrary-depth path, constructed in one
place and passed rather than re-parsed. Every validator above becomes a caller. The two-segment rule
survives as a per-provider validation on the GitHub provider, where it is true, instead of as an
invariant of the whole stack, where it is not — and the host survives the parse instead of being
discarded before the slashes are counted.

`forge.py`'s "On the repository parser" note describes code that no longer exists: a parity test
holding `_parse_repo` level with `resolver.get_target_repo`, and `gitops_workspace.repo_from_settings`
as a loose parser knowingly left unfixed. All three are gone, and `resolver.py` imports
`gitops_workspace` now instead of carrying a parser of its own. Correcting that note belongs to
step 1, which is what makes it true again.

**Where #1085 now stands.** [#1085](https://github.com/gke-labs/kube-agents/issues/1085) reported
that `repo_from_settings` resolved `https://evil.example/victim-org/victim-repo` to
`victim-org/victim-repo` with no host check, pointing the token refresher at a repository the URL did
not name. That function is gone. Three of the issue's four examples are now rejected at admission by
the slash count above, and the fourth — the SCP form — is admitted with its host silently discarded,
which grants nothing the plain `owner/repo` shorthand does not already grant. The host confusion the
issue reported is closed; what the same code path costs now is the rewrite in the paragraph above.
What remains is the half the issue deferred — "decide separately whether `ValidateGitRepoURL` should
reject a non-GitHub host at admission". It does not, which is §6, so the remedy is step 2 rather
than `RepoRef`.

**A latent defect this also removes.** `provider_for` has two ways of choosing wrong. It selects by
asking whether any key of the host table appears anywhere in the repository string — a substring
test rather than a parsed host, so `https://example.invalid/github.com/o/r` would select
`GitHubProvider` — and it falls back to `GitHubProvider` for anything it does not match. Neither
picks the wrong provider today, because no caller hands it a host: the sweep passes nothing at all,
and both of `pr-conversation`'s sources yield bare `owner/repo` slugs — `extract_github_slug` for
the discovered ones, `is_valid_repo_slug` for `--repo`. The fallback is the only branch taken: every
provider in the running system comes from it, and the host table is reached only from tests.

That is sound while there is one provider and a bare slug means GitHub, which is what the docstring
says. It stops being sound at the second, because the table becomes load-bearing at exactly the
moment a caller starts passing hosts — and [the consumer migration](#the-protocol-past-its-first-feature)
is about to add three callers that resolve
repositories their own way. Selection must parse the host, and an unknown one must raise with a
reason code, the way every other unresolvable input in this stack does.

### The vocabulary in prompts and procedures

Two kinds of prompt name the forge, and they need different work.

Four `SKILL.md` files instruct the model in `gh` spellings and call the artefact a pull request:
`fleet-audit`, `pr-conversation` and `submit-suggestion` under `agents/platform/skills/`, and
`gke-stockout-investigator` under `agentplugins/`, which reaches an install through the
`AgentPlugin` CRD rather than through the agent image and so is easy to miss. These want the command
behind a wrapper and the noun taken from configuration.

The seven governance SOPs name `gh` only to forbid it — "never run `gh issue create`", "the helper
owns every `git`/`gh` operation" — because `audit_report.py` owns their write path. A prohibition has
no command to wrap, so the SOP work is smaller and different: the nouns ("pull request", "PR body")
come from configuration, and the prohibitions get reworded once the helper they defer to is a
provider rather than `gh`. [The consumer migration](#the-protocol-past-its-first-feature) moves that
helper; this step only follows it.

`github-issue-resolver`, the skill a reader would expect on the first list, is not on it: its prompt
names no forge command — only its own `resolver.py` subcommands — and its coupling is entirely in
that script, which [the consumer migration](#the-protocol-past-its-first-feature) also moves.

`pr-comment-conversation.md` §6 already prescribes this for the worker skill, which is told to take
`forge` and `noun` from the card "so one prompt serves a forge whose users call them merge requests".
This design extends the same rule to the SOPs rather than inventing a second convention.

---

## 3. The seam

### The shape

History moves as a bundle, in both directions, and is never checked out on the
credential side.

`clone` asks the broker for a git bundle of the repository and unpacks it in the
sandbox, into a working copy with no remote. A bundle is objects and refs. It
carries no `.git/config`, no hooks, no remote URL. So every question about the
past is answered locally, by the sandbox's own git, at full fidelity and without
a credential anywhere near it.

`commit` runs in the sandbox too, against that copy. The revision has a real
parent and a real identifier before anything leaves the container, which is why
a branch of five changes stays five revisions instead of arriving at the forge
flattened into one.

`publish` sends those revisions back up as a bundle, symmetric with `clone`. The
broker fetches the target branch into a scratch repository, unpacks the bundle
beside it, and checks four things before it pushes: that the bundle carries
exactly the branch it claims; that its tip descends from the revision `clone`
handed out; that the target's current tip is also an ancestor, so a push nobody
saw arrive is not silently discarded; and that an existing remote branch of the
same name is not being clobbered. Then it pushes the ref it fetched.

Ahead of all four it checks that the branch is not the target. Those four are
ancestry checks, and every one of them passes for a publish onto the branch the
copy was cloned from, because that is a fast-forward — which would leave the
default clone, edit, commit, publish sequence writing to the shared line of
development with nothing in the protocol objecting. `vcs.py` refuses it before
it builds the bundle so the message costs no round trip, and the broker refuses
it again with `TARGET_IS_BRANCH` rather than trusting the client that sent the
objects.

The scratch repository is never checked out. It is fetched into and pushed from,
and nothing materialises a working tree, so a `.gitattributes`, a hook, or a
`.gitmodules` among the incoming objects has nothing to act on. That is what
makes accepting caller-supplied objects at all defensible: the broker handles
them as objects, not as a repository it is standing in.

Nothing under the broker's scratch root outlives a request. Every route is one
request long, so there is no handle to leak, no tree to collide with another
caller's, and no cleanup an interrupted client can skip.

One consequence is worth naming. An agent almost never wants a whole answer: it
wants the shape of one, then one part of it in full. Here that is `log --stat`
and then `show -- <path>`, both local, both git's own narrowing, and neither of
them a route. A protocol that answers from the credential side has to grow a
verb for each such narrowing and a cap on each verb's response, and a caller
that hits the cap gets a truncated list rather than a smaller question. This
design has no cap on a history question, because a history question never
crosses the seam. The one transfer it does make is bounded, once, at `clone`.

### The broker is already in its own pod

Everything above crosses as a payload rather than as a path, and that matters
because the credential broker does not share the sandbox's pod. It runs as its
own Deployment, reached over a Service, and the sandbox authenticates to it with
a projected ServiceAccount token that a TokenReview validates against the
audience `kubeagents-credential-proxy`.

That arrangement is the reason a verb protocol is the only thing that can work
here, and `credential_proxy_client.py` says so directly: it forwards no `cwd`,
because "the broker resolves a path against its own filesystem, and it has no
view of this one". A protocol whose operations name paths in the agent's tree
has nothing to name. These routes send a bundle, so there is no tree to point at
and nothing that needs to be co-resident.

`/v1/vcs/*` takes its place in the existing `ROUTE_ROLES` table alongside
`/v1/exec`, demanding the `shell` role — the same role, because the caller is
the same sandbox, and a route that demanded nothing would be the one gap in a
table whose point is that every route names what it requires.

### No shallow clones

There is no `depth`. This is a property of the transport rather than an
omission: `git bundle create` inside a shallow repository succeeds and writes a
bundle whose boundary revisions name parents the bundle does not carry, and a
clone from it fails with `remote did not send all necessary objects`. Naming a
`branch` is the size control that works, because it makes the broker's clone
single-branch. The two ceilings — `CREDENTIAL_PROXY_MAX_CLONE_BYTES` (256 MiB
of working tree) and `CREDENTIAL_PROXY_MAX_BUNDLE_BYTES` (64 MiB of bundle, both
directions) — say so in their refusals.

### One git in the sandbox

The sandbox has exactly one git: the real binary at `/opt/vcs/libexec/git`,
reached through a symlink in `/opt/vcs/bin`. There is no credential shim named
`git` beside it, and that is worth stating explicitly because the sandbox does
carry shims for `gcloud` and `kubectl`.

Two reasons, and either alone would be enough.

**Nothing the sandbox does with git spends a credential.** Local operations are
local by definition. Remote operations are not the sandbox's to make: fetching a
repository and sending revisions back are broker verbs, and the broker runs its
own git, in its own pod, over a checkout the sandbox cannot see. A shim would be
forwarding the one category of command that no longer travels that way.

**A forwarded git could not serve them anyway.** The proxy relays argv, and
git's state _is_ the local filesystem — `clone`, `add`, `commit`, `push` each
need the previous step's bytes to still be there, and the broker's filesystem is
not the sandbox's. `credential_proxy_client.py` states the consequence in the
comment standing where a `cwd` forward would go: the broker "resolves a path
against its own filesystem, and it has no view of this one", so every forwarded
command runs at the broker's own workspace root. A forwarded `git commit` does
not fail in some subtle way — it operates on a tree the agent has never seen. A
shim on PATH is worse than no shim, because that answers a command with a
confusing success instead of a missing binary.

So `git` and `gh` come off the sandbox's shim set, leaving `gcloud` and
`kubectl`, which keep theirs because neither has a credential-free equivalent
and neither has anything local to read. `SUPPORTED_EXECUTABLES` in
`credential_proxy_client.py` and the shim symlinks in `deploy/sandbox/Dockerfile`
are the two places that say so, and the image's smoke test asserts the two
missing names by absence rather than by which path wins.

The real git then needs to be reachable, and `/opt/vcs/bin` goes on PATH in one
place: the `SANDBOX_PATH` line in `deploy/sandbox/entrypoint.sh`, which becomes
the `SetEnv` directive in the generated `/etc/ssh/sshd_config.d` drop-in. One
place is not an accident of this design — sshd keeps the first `SetEnv` it reads
and discards every later one whole, so the environment a sandbox session gets
cannot be assembled from more than one directive. A missed prepend is therefore
a `git: not found` on the first call rather than a silent forward, which is the
other thing deleting the shim buys.

The image's existing build guard needs no change to cover this. It already fails
the build if a bare `command -v` finds any of `gcloud`, `kubectl`, `gh` or `git`,
and its comment gives the reason in the same terms this section does:
`/opt/credential-proxy/bin` is not on the build PATH, so a hit "can only be
finding a native binary". `/opt/vcs/bin` is not on the build PATH either, so the
hardened git stays invisible to that lookup and the guard keeps meaning what it
meant — placement, not presence. Anything a bare lookup does find still got there
by another route and is still wrong.

A second guard proves the shipped binary cannot reach a forge. The control is
deletion, and it is deletion for a specific reason: inside the sandbox the agent
owns its own environment and its own config files, so `GIT_ALLOW_PROTOCOL`,
`protocol.<name>.allow` and `/etc/gitconfig` are all things it can override or
neutralise with one flag. Only the absence of a file survives that. The broker
can rely on the environment — `GIT_ALLOW_PROTOCOL` outranks every config layer
including `-c`, which is what makes it the boundary in `credential_proxy.py`
today — because the broker, not the agent, builds that environment. The sandbox
has no such advantage, so it gets the file-shaped control instead.

Git reaches a network three different ways, and each needs its own answer.

**Remote helpers are separate executables**, found on PATH by name, so deleting
them removes the transport outright. The four that dial a host —
`git-remote-http` and the `-https`, `-ftp` and `-ftps` symlinks to it — are
deleted from `/usr/lib/git-core`. This is the one that matters: https is how
every forge is reached, so removing it is what leaves a broker verb as the only
route to one. The guard fails the build if any is back, then runs an actual
`ls-remote` per scheme against an unroutable URL and requires git's own
missing-helper message in the answer.

The message is what carries the assertion, not the exit status. `example.invalid`
resolves nowhere, so every one of those URLs fails on an image that still ships
the helpers, and a guard that checked only for failure would pass there. A
`file://` probe runs last for the converse reason: a git broken outright also
fails to reach a network, and this is what says the disarming was surgical. The
guard these replaced asked `git <helper> --help`, which git rewrites to
`git help <helper>` and execs `man`, absent in `python:3.11-slim` — so all four
probes exited 128 and it passed whatever the image contained.

**`ext` and `fd` look like helpers and are not.** Their entries in
`/usr/lib/git-core` are symlinks to `git` itself: both are builtins, dispatched
from git's own table without the filesystem being consulted, so deleting them
does nothing. What stops `ext::` — which would run an arbitrary command — is
git's protocol allowlist, where `protocol.ext.allow` has defaulted to `never`
since 2.12, and the guard asserts that refusal by its own message rather than
attributing it to an `rm`. `fd::` reads a descriptor the parent already opened,
so it reaches whatever the caller could reach anyway and opens nothing new.

**`ssh://` and `git://` are dialled from git's own code.** There is no
`git-remote-ssh` to delete, and a default that an agent can turn off is not a
control, so these two are answered by what they need to run and by scope.
`ssh://` execs an ssh _client_, and the image ships none — the sandbox runs an
ssh server, because that is how Hermes arrives; nothing in it makes an outbound
ssh connection. The guard asserts the client's absence the same way it asserts
git's placement, which matters because it is the only thing standing between a
sandbox and a forge's ssh endpoint, and an unrelated package pulling in
`openssh-client` would restore that route silently. `git://` opens port 9418
from inside git and cannot be disarmed by removing a file. It is left, and named
here rather than in [What this does not fix](#10-what-this-does-not-fix),
because the anonymous daemon protocol is read-only, spends no credential, and no
forge in scope serves writes over it. It buys an agent nothing a plain outbound
socket would not, which is a question about egress from the sandbox and not
about git.

`vcs.py` runs that binary with `GIT_CONFIG_NOSYSTEM=1`,
`GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath` pointed at an empty directory,
`protocol.ext.allow=never`, and no `origin`. The clone's only config is the one
git just wrote, so a `.gitattributes` naming `filter.foo.clean` finds no `foo`
defined and is inert.

### Forge neutrality

Nothing crosses the seam in a forge's vocabulary. The sandbox sends a repository
spec and a verb; the broker decides which forge that is, calls it, and returns
objects in the concepts above. GitHub's JSON stops at the broker.

That decision is also the security boundary, which is why it is made there. A
caller-supplied URL determines which host a minted credential is presented to,
so `resolve_forge` matches the URL's host against an allowlist built from the
configured forges and refuses anything else outright — there is no default for a
URL with a host, because defaulting is how a token reaches a host nobody
configured. A bare `owner/name` means GitHub, which is what every skill in this
repository has always meant by it. Once the forge is chosen, the clone URL is
composed from validated path segments rather than taken from the caller: the URL
decided _which forge_, and it does not get to decide the host.

The GitHub module reaches the API through `gh api` and never through `gh pr` or
`gh issue`. Those subcommands infer the repository from a `.git/config` found
above the working directory, which is the one file this design keeps out of the
credentialed process, and they format for a human. `gh api` takes an explicit
path and returns the API's own JSON, so the module is a REST client that borrows
`gh` for authentication.

Translation is where the judgement is, and it is the part a new forge module
will spend its time on. A proposal has three states — `open`, `closed`,
`merged` — rather than GitHub's two plus a nullable `merged_at`, because closed
and merged are different outcomes on every forge and a caller should not have to
know how one of them encodes the difference. An `[bot]` suffix comes off a login
here rather than at the caller; `forge.py` records what comparing an
unnormalised one costs, which was an agent that answered its own comments
forever. And `issue list` drops the nodes carrying a `pull_request` key, because
GitHub is alone in modelling a proposal as an issue — a translation that exists
precisely because the shared concept and the forge's model disagree.

### The protocol

Every route lives under `/v1/vcs/*`, and every one of them stands alone: it
names a repository, does one thing, and keeps nothing. There is no session, no
handle, and no state on the broker that a later call depends on — a caller that
crashes between two verbs leaves nothing behind to reconcile, and two agents
calling the same verb on the same repository do not have to be ordered against
each other. `clone` is the only route that hands back something durable, and it
hands it to the sandbox as a bundle rather than keeping a tree.

That is a constraint on what the verbs may be, not just a description of them.
It is why `publish` carries `baseRevision` instead of remembering what `clone`
served, and why nothing in the table takes an identifier the broker minted. The
routes still take `gitops_workspace.workspace_lock` while they run, because the
broker's clone touches the same disk as the leased checkout, but the lock is
held within one call and never across two.

| Verb                                 | Request                                                    | Response                                              |
| ------------------------------------ | ---------------------------------------------------------- | ----------------------------------------------------- |
| `capabilities`                       | `{repository}`                                             | `{forge, repo, proposalNoun, verbs, missing}`         |
| `clone`                              | `{repository, branch?}`                                    | `{forge, repo, branch, revision, size, bundleBase64}` |
| `publish`                            | `{repository, branch, target, baseRevision, bundleBase64}` | `{forge, repo, branch, revision}`                     |
| `proposal-create`                    | `{repository, source, target, title, body?, draft?}`       | `{proposal}`                                          |
| `proposal-list` / `issue-list`       | `{repository, state?, limit?, labels?}`                    | `{proposals\|issues, count, truncated}`               |
| `proposal-view` / `issue-view`       | `{repository, number, comments?, diff?}`                   | `{proposal\|issue, comments?, diff?}`                 |
| `proposal-comment` / `issue-comment` | `{repository, number, body}`                               | `{comment}`                                           |
| `issue-create`                       | `{repository, title, body?, labels?}`                      | `{issue}`                                             |

Refusals carry a code: 501 `FORGE_UNSUPPORTED`, 413 `CLONE_TOO_LARGE` and
`BUNDLE_TOO_LARGE`, 409 `NOT_FAST_FORWARD`, `BASE_MOVED`, `BRANCH_DIVERGED` and
`TARGET_IS_BRANCH`, 502 `GIT_FAILED`.

A refusal the forge itself produced is translated rather than forwarded, and it
is written for the reader it has. That reader is a model choosing its next tool
call, so each message names the cause and then the action that follows from it,
which is the shape Anthropic's
[tool-writing guidance](https://www.anthropic.com/engineering/writing-tools-for-agents)
asks for. The distinction earns its keep where the right action differs and the
symptom does not: GitHub spends HTTP 403 on both a missing scope and a throttle,
and an agent told only that the call failed retries the one that will never
succeed and abandons the one that would have worked in ten seconds. So 401 is
`FORGE_UNAUTHENTICATED` and says to stop, a throttled 403 becomes 429
`FORGE_RATE_LIMITED` and says to wait and to prefer one wide call to many narrow
ones, an unthrottled 403 is `FORGE_FORBIDDEN` and says retrying will not change
the answer, 404 `FORGE_NOT_FOUND` says that a private repository this install
cannot see answers the same way so absence is not proven, 422 `FORGE_REJECTED`
says to fix a field rather than repeat the call, and 5xx is 503
`FORGE_UNAVAILABLE` and says to retry the same call unchanged. Everything
unrecognised is still 502 `FORGE_CALL_FAILED`.

The table above is shared, and keying it on the status alone is _nearly_
forge-neutral. The exception is real and has to be designed for: a status can
mean different things on different forges. GitLab's 401 most often means a group
access token reached its expiry after a year, which no refresh path can fix and
which wants an operator named rather than a retry; Bitbucket answers 401 for a
private repository it will not admit exists, where GitHub answers 404. So a forge
package may supply an override map merged over the shared table — a per-forge
dict of the few statuses whose guidance it disagrees with, and nothing more.
Recovering the status in the first place belongs to the transport, not to the
table: an HTTP client has it as an integer, and a CLI that prints `(HTTP 404)`
into stderr has to dig it out.

The forge's own first line rides along in `detail`, and `vcs.py` renders both.
They answer different questions — the broker's sentence says what to do, the
forge's says which field it rejected or that the branch has no commits — and
neither substitutes for the other.

Every credentialed verb makes its credential current before it spends it, on the
broker side and never in the sandbox — knowing that a particular forge's token
expires is forge knowledge, in the container that is supposed to have none of
it. It is not the broker's knowledge either. The broker does not mention
credentials at all: the transport asks the credential to make itself current
before a request, and the git runner asks it for config before an invocation.

That is one object per forge because it is one forge's answer to four questions
that are really the same question — how is this forge's token presented. GitHub's
is an App installation token that expires within the hour, so its strategy asks
the broker's refresh route before every verb; minting is idempotent and costs one
local process, and the alternative is worse than the cost. An expired GitHub
token surfaces as `Authentication failed` from inside the broker's own clone,
which reaches the caller as a clone failure and reads like the repository is
gone, so inferring expiry from a failure means the first verb after an idle hour
fails once for a reason the caller cannot act on. GitLab's group access token has
no acquisition step at all, so its strategy does nothing — and says so by being a
strategy with nothing to do rather than by inheriting a no-op from a default
shaped around a forge that does.

A forge may not run a subprocess, and it does not have to: **the forge chooses
the strategy, the strategy names the privileged operation, the executor performs
it.** So no shared file names a forge, and nothing inside a forge package can
execute anything.

### Replacing `gh`

A forge CLI on PATH is the route out of a forge-neutral abstraction. Shipping
`gh` and naming it in skills binds an install to GitHub regardless of what the
abstraction says, so the collaboration verbs exist to make removing it possible:
`vcs.py issue list --state open --labels bug` replaces `gh issue list`, and
`vcs.py proposal create` replaces `gh pr create`.

In the sandbox that removal is literal. The entrypoint deletes
`/opt/credential-proxy/bin/gh`, and nothing else in the image supplies one, so
`gh` resolves nowhere in either session type. The smoke test asserts that by
absence rather than by which path wins, because a check on PATH order would pass
against a build that merely shadowed the name.

The measurement is what settled that it had to happen at all. With a working
`gh` on PATH the agent left the abstraction whenever a question got awkward, and
it did so without reporting that it had: of 60 read probes, 8 were answered
through the credential shim with no call to `vcs.py`, and 4 issued a
credentialed network clone through it. It is not that the verbs could not
answer — the same probes on the same skill were answered on the verbs once there
was no CLI to reach for. An abstraction whose bypass is on PATH under the
obvious name is one the model will take.

Removal replaced a refusing stub, and the second measurement is why. The first
build shipped `/opt/vcs/bin/gh` as a script that refused and named the verb to
use instead, on the theory that a named gap is an answer a caller can act on
while `command not found` reads to a model as a broken image to route around —
the same argument `StubForge` makes about an unconfigured forge. The write rung was then run
in a fully sealed configuration, no `gh` under any path, and the theory did not
reproduce: the agent never attempted a `gh` call, emitted no not-found across
four probes, stayed on the verbs throughout, and finished faster and in fewer
turns than the same rung with the stub in place. The skill already tells it that
no forge CLI is needed or available, and that turned out to be enough guidance
without a binary to carry it.

What this does not remove is the dependency elsewhere. `gh` stays on the
broker's executable allowlist, because the GitHub module uses `gh api` as an
authenticated HTTP client, and these still name it from outside the sandbox: the
seven governance SOPs under `agents/platform/governance/`, `forge.py`, the
`fleet-audit`, `github-issue-resolver`, `pr-conversation` and `submit-suggestion`
skills. Each is a port, not a rewrite. `forge.py` is the load-bearing one: it is
already a provider seam of its own, it is what the four skills call, and
[One implementation, not two](#one-provider-implementation-not-two) is the decision that
it converges with `providers/` rather than becoming a second copy of it. Until
these are ported they do not work in a sandbox, and each is a place a second
forge would otherwise have to be added by hand.

A sanctioned `raw` verb — a forge-native method and path, passed through the
broker and logged — was the obvious way to keep an agent that needs something
unmodelled inside the boundary rather than out on the allowlist. It is not here,
and the measurement is why. Across the read rungs the agent on these verbs made
one `gh api` call, on a probe it had already answered from a commit message, and
it made that call with a working `gh` on PATH and every other route it might
have used still serving beside it. The escape hatch is a solution to a demand
that did not
appear. Adding it would also put a forge-shaped hole in a forge-neutral protocol
and give a model a documented reason to stop at the first verb that does not
quite fit. If a real gap turns up, the verb list is where it gets answered.

---

## 4. The provider interface

### What a provider supplies

`Forge` is ten members. The rest of this section is why each is a member rather
than something the broker decides on the forge's behalf, and
[Modularity](#5-modularity) is why the boundary they draw holds at the third
forge.

| Member               | What it decides                                                                      |
| -------------------- | ------------------------------------------------------------------------------------ |
| `hosts`              | which hostnames are this forge's; also what the credential allowlist is built from   |
| `parse(url)`         | the repository a URL names, and the only validator of that repository's shape        |
| `clone_url(repo)`    | the URL to clone, composed from validated segments                                   |
| `capabilities(repo)` | what this install can do here, without spending a credential or touching the network |
| `verbs`              | which of the eight collaboration verbs this forge serves                             |
| `credential`         | the acquisition strategy, the API header and the git config — one object             |
| `transport`          | which transport the broker builds for it: `"cli"` or `"http"`                        |
| `for_config(config)` | how many instances of this forge this install has: 0, 1, or n                        |
| the eight verbs      | each describes a request and translates the response                                 |
| `error_overrides`    | the few statuses whose shared guidance this forge disagrees with                     |

Three of these are the modularity requirement rather than GitLab. `for_config` is
what lets `registry.py` stay ignorant of any particular forge. `credential` is
what keeps the next forge's token out of the credential proxy's own module.
`error_overrides` is what stops the first status whose meaning is forge-specific
from being absorbed as an `if` in a shared file.

`transport` is a declaration, not an implementation: the forge names what it
needs and the broker constructs it, which keeps the rule that a forge says what
to call and never how to execute it while allowing a transport that is not a
subprocess.

A forge that is registered but not configured is a `StubForge`: it parses its own
repository specs and answers `capabilities` with the specific gap rather than a
generic refusal. That matters more than it sounds — a caller learns what is
missing before it has written a revision it cannot deliver, and an install that
has not set GitLab up gets a named refusal instead of a confusing authentication
failure.

### `git`'s credential has no seam

This is the easiest of the four to miss, and it is worth taking first because
GitHub gives no hint that it is a question at all.

The broker authenticates to a forge twice, by two mechanisms:

- **The API**, in the collaboration verbs, through the transport.
- **`git` itself**, in `clone` and `publish`, which run `git clone` and
  `git push` against `forge.clone_url(repo)` — a plain `https://…` URL with no
  credential in it.

The natural shape of a forge interface supplies the first and forgets the second,
because on GitHub the second happens by itself. `github_token_refresh.py` ends
with:

```python
subprocess.run(["gh", "auth", "login", "--with-token"], input=token, …)
subprocess.run(["gh", "auth", "setup-git"], …)
```

`gh auth setup-git` writes a global git config entry making `gh` a credential
helper for github.com. So authenticating `git` is an _undeclared side effect_ of
authenticating the API, through a global config file, from a script that is not
part of the forge package. A reader of the interface cannot see why `clone`
works, and a second forge inherits nothing.

**So the credential declares git's config as well as the API's.**

```python
def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
    """Config keys this forge needs on the git invocations the broker makes
    on its behalf. Applied to those invocations only. Default is none."""
    return ()
```

This sits on the credential rather than on `Forge` — see
[Credentials belong to the forge](#the-credential-is-one-object) for why the
two halves are one object. GitHub's `BrokeredCredential` returns `()` and relies
on the helper `gh` installs, which is fine as behaviour and is now a thing the
interface says out loud with a docstring naming where it comes from. GitLab's
`StaticFileCredential` returns a credential-helper pin:

```python
(("credential.helper", f"!f() {{ echo username=oauth2; echo password=$(cat {TOKEN_FILE}); }}; f"),)
```

Applied through the existing `GIT_CONFIG_COUNT` layer, which
`credential_proxy.py` already builds for `GIT_FORCED_CONFIG` and which outranks
system, global and repo-local config.

Three properties this shape has that the alternatives do not:

- **It is per-invocation, not global.** `GIT_FORCED_CONFIG` is one tuple applied
  to every git the broker runs, whatever it is running it for. A credential
  belonging to one forge does not belong on all of them.
- **The token is read from a file at use time, not interpolated into config.**
  A rotated Secret takes effect without restarting anything, and the token is
  not in the process environment, not in `/proc/*/environ`, and not in any
  argv the redactor has to catch.
- **It survives the token not being a bearer header.** GitLab accepts
  `username=oauth2` with the token as the password over HTTPS basic, which is
  what git does natively. No `http.extraheader`, which would have to be set
  per-host and which git logs in `GIT_TRACE`.

### The credential is one object

This is where the requirement bites hardest, and it is the easiest place to get
the seam wrong.

Four things about a credential differ per forge, and the natural place to put
each of them is a different one:

| Concern                   | Where it wants to go                                                      |
| ------------------------- | ------------------------------------------------------------------------- |
| whether it expires at all | nowhere — it is assumed, by whether an acquisition method exists at all   |
| how it is acquired        | the process that holds the privilege, named after the forge that needs it |
| how the API presents it   | inside whichever client makes the call                                    |
| how `git` presents it     | a side effect of acquisition, per [above](#gits-credential-has-no-seam)   |

Every one of those is defensible in isolation and the set is wrong, because they
are four views of one question — how is _this_ forge's token presented — and
scattering them is what allows the fourth to become invisible.

The principle that resolves it: token acquisition is **a strategy selected per
provider, not a pipeline every forge is fitted into.** GitHub App tokens are
signature-derived and expire
hourly, so Minty exists; a GitLab group access token is a long-lived string with
no minting step, so for GitLab there is nothing to acquire. An interface built
around acquisition makes the second forge implement a method that does nothing
and receive an argument nobody uses, and still leaves it nowhere to put the parts
that are not empty.

**So one member, which the forge constructs and owns:**

```python
# providers/credentials.py — shared, forge-neutral
class Credential(Protocol):
    def ensure(self, repo: str) -> None: ...
    def headers(self, repo: str) -> dict[str, str]: ...
    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]: ...
```

`ensure` is "make yourself current, if that means anything to you." Two
implementations cover both forges and, as far as anyone has proposed, the third:

| Strategy               | `ensure`                                         | `headers`                        | `git_config`                                              |
| ---------------------- | ------------------------------------------------ | -------------------------------- | --------------------------------------------------------- |
| `BrokeredCredential`   | asks the broker's refresh route                  | none — the CLI carries it        | none — the CLI installs a helper                          |
| `StaticFileCredential` | **nothing** — a long-lived token cannot go stale | reads the file, sends the header | the helper pin from [above](#gits-credential-has-no-seam) |

GitHub takes the first, GitLab the second. **GitLab's `ensure` is `pass`**, and
that is the point: a forge whose credential does not expire says so by choosing
a strategy that has nothing to do, rather than by inheriting a default from an
abstraction shaped around a forge that does.

Collecting the API header and the git config onto the same object as acquisition
is the load-bearing part. They are one concern seen three times — how this
forge's token is presented — and separating them is precisely what lets
`gh auth setup-git` become an undeclared side effect. One object owns all three,
and a reader of a forge package sees the whole credential story without leaving
the directory.

**Who does the privileged act.** A forge may not run a subprocess, and it does
not have to: `BrokeredCredential.ensure` POSTs to the broker's `/v1/forge/refresh` route with
the provider in the body, and the executor performs the privileged act. Three
roles, cleanly separated: **the forge chooses the strategy, the
strategy names the privileged operation, the executor performs it.** No shared
file names a forge, and nothing in a forge package can execute anything.

The broker does not mention credentials at all. The transport calls
`credential.ensure(repo)` before a request and the git runner asks for
`git_config` before an invocation. "Invoked as needed" is the caller's timing and
the strategy's decision, and neither is the broker's business.

### What the credential plane holds up

The governing constraint is
[`../credential-isolation-design.md`](../credential-isolation-design.md): the
agent sandbox receives no API keys or access tokens through its environment or
its filesystem, and no ServiceAccount token either — the one it does carry is
projected for a single audience the broker checks, and is good for nothing else. Every forge call is therefore brokered, and a
second forge is a change to the credentialed process before it is a change to
anything the agent runs.

Three parts of that process are forge-shaped, and the `Credential` object above
is what keeps each of them from becoming a name in a shared file.

**Acquisition, which is where two forges genuinely diverge.** GitHub App
installation tokens are minted from a JWT signed by the App's private key and
expire hourly, so an install runs Minty — the workload in
`charts/kube-agents/templates/github-minter.yaml`, with the KMS key and service
accounts behind it provisioned by `terraform/modules/github-minter`. That
apparatus follows entirely from App tokens being short-lived and
signature-derived. A GitLab group or project access token is a long-lived string
with no minting step, so for GitLab the KMS key, the signing service and the
`github-token-minter-config` policy ConfigMap have no analogue to build; the job
is storage and scoping. GitLab's token lives in a Secret mounted **into the
credential broker** — never into the sandbox, which the paragraph above forbids —
because it is the smallest thing that works and an operator can rotate it without
new infrastructure. OIDC token exchange is the better long-run answer for GitLab
and is deliberately deferred: it is a second design, and a first working install
does not need it.

**The privileged route.** A forge may not run a subprocess, so a credential that
needs one names the operation and the broker performs it: `/v1/forge/refresh`
with the provider in the body. The route is one route, not one per forge, and
carrying the provider as a parameter rather than in the path is what lets an
agent image and a broker image differ by a release without the refresher
breaking. A forge with no CLI reaches the same route the same way — Bitbucket
Cloud has no CLI at all, and the transport split above is what makes that a
choice of transport rather than a special case.

**Egress.** A pod that cannot resolve the host makes no calls, so the FQDN
network policy is part of the credential plane whether or not it looks like it.
The allowed hosts derive from the configured forges, in both places the policy is
written: the operator renders it in
`k8s-operator/internal/controller/platformagent_manifests.go`, which is what a
real install gets, and `deploy/kustomize/gke-dataplane-v2/fqdn-networkpolicy.yaml`
carries the dev path. Both have to derive it, because changing only the kustomize
copy leaves every shipped install unable to reach the forge it was configured
for. This is also the clearest case for deriving rather than listing: a
self-managed GitLab is at a customer-chosen hostname, so no literal in this
repository could ever have covered it.

### The request is a request, not argv

A verb describes the call it wants; the transport makes it. So what a verb hands
across is an HTTP request:

```python
def api(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    raw: str | None = None,   # a media type, when the caller wants bytes
) -> Any
```

This is worth stating explicitly because the obvious alternative is very
attractive and it is a trap. When the only transport is `gh api`, the cheapest
signature is `(method, path, fields: list[str])` where `fields` is that CLI's
argv — `["-f", "title=…"]` for a body, `["-H", "Accept: …"]` for a media type.
That is one CLI's command-line spelling crossing a seam whose entire premise is
that nothing crosses it in a forge's own vocabulary, and no second forge can use
it. A seam only one forge can pass through has not been shown to be a seam.

Two things fall out of the shape above rather than being separately argued.
`params` as a dict is what gets query parameters URL-encoded, where formatting
them into the path — `f"repos/{repo}/pulls?state={state}&per_page={limit}"` — does
not. And `raw` names the media type instead of smuggling it through as a header,
so a transport that is not HTTP-header-shaped can still honour it.

### The transport cannot be a CLI

Given a neutral request shape, something has to execute it. GitHub's is a
subprocess running `gh api`, which is there because `gh` is already in the image
for the App flow. GitLab's options are `glab`, or an HTTP client in the broker
process.

The rule this has to respect is that the broker owns process execution and a
forge only ever says what to call. That rule is right and it is easy to overfit
to — stated as "a forge supplies `api_command(...) -> argv`" it silently assumes
every transport is a subprocess, which GitLab is the case that breaks.
**`glab` is rejected**:

- It re-imports a problem the design already solved once. `GitHubForge` uses only
  `gh api` and never `gh pr` or `gh issue`, because those subcommands infer the
  repository from a `.git/config` — the one file this whole design exists to keep
  out of the credentialed process. `glab` has the same subcommands with the same
  inference.
- It is a second binary in the credential-proxy image, with its own auth state
  on disk, its own config file, its own update-check network call, and its own
  CVE stream — in the container holding the token.
- It buys nothing. `gh` is in the image because it was already there for the
  GitHub App flow. There is no equivalent debt for GitLab.

**So: an in-process `HttpTransport` built on `urllib`.** The broker constructs
it; a forge never does. It is the broker that owns the timeout, the response
size cap, the redaction of the token out of anything logged, and the mapping
from HTTP status to the shared error contract.

Two things this changes that are worth being explicit about, because they are
the cost side:

- **The broker process makes direct outbound HTTPS**, where its other network
  I/O is in subprocesses. At the NetworkPolicy layer nothing changes — egress is
  per pod, and `git clone` already leaves that pod for the same host — but the
  egress policy needs the GitLab host, and for self-managed that host is
  customer-chosen and cannot be a literal in the repository. See
  [Open questions](#12-open-questions).
- **Timeouts and output caps are not inherited.** A subprocess runner enforces
  both for the CLI path. `HttpTransport` has to enforce them itself, and a test
  has to hold it to that, because "the runner did it" is exactly the kind of
  property that quietly stops being true when the transport changes.

In exchange the token never enters an argv, an environment variable, or a child
process at all. That is strictly better than `gh`, and it is worth noting that
migrating `GitHubForge` onto `HttpTransport` later becomes a small change once
the App token is available to the broker directly — not proposed here, but the
shape does not foreclose it.

### One package, many hosts

`gitlab.com` and a self-managed GitLab are **one package**. The REST API is the
same `/api/v4`, the objects are the same, the token is the same shape. What
differs is the hostname, the network path to it, and where the token came from
— configuration, not code. Two classes would be one class and a copy that
drifts.

The consequence is that the set of forges cannot be a literal, because a
customer's `gitlab.acme.internal` is not knowable at import time. So the registry
is built once at broker construction:

```python
def build_forges(config: ForgeConfig) -> tuple[Forge, ...]
```

`GitHubForge` yields exactly one instance, unconditionally. `GitLabForge` yields
one per configured host, and none when none is configured — in which case
`gitlab.com` resolves to the `StubForge`, so an install that has not set GitLab
up gets a named refusal rather than a confusing authentication failure. The host
lookup `resolve` uses is derived from this tuple rather than from a module
constant.

Where the configuration comes from is
[the declarative surface](#6-the-declarative-surface). What matters at this seam
is only what the broker has to receive, in whatever form that surface renders it:

| Field          | What it is                                                                                   |
| -------------- | -------------------------------------------------------------------------------------------- |
| `host`         | the GitLab hostname                                                                          |
| `tokenPath`    | file the projected Secret lands at                                                           |
| `allowedPaths` | namespace prefixes this token may be spent on — see [The credential](#the-gitlab-credential) |

One caveat on hostnames, inherited rather than introduced: `repository_host`
treats a first segment with no dot in it as _not a host_, so a bare `owner/name`
resolves to GitHub. An in-cluster GitLab reached as `gitlab` with no domain
would be read as a repository named `gitlab`. Configure a dotted name; the
refusal is otherwise silent and confusing.

### Where the error contract splits

`errors.py` holds a status-to-guidance table — forge-neutral prose about what an
agent should do next — and `forge_error(status, detail)` builds a refusal from
it. Two things are deliberately _not_ in there, and both are places a
single-forge design would have put them:

- **Recovering the status.** `HttpTransport` has it as an integer. A CLI prints
  `(HTTP 404)` into stderr and something has to dig it out with a regex. That
  regex belongs to `CliTransport`, not to the error module, because it is a
  property of how the call was made rather than of what the forge answered.
- **Splitting one status by message text.** GitHub spends 403 on both a missing
  scope and a throttle, and telling them apart means matching throttle markers in
  prose. That heuristic lives on `GitHubForge`. GitLab returns 429 with
  `RateLimit-*` headers and needs none of it, and a shared module carrying
  GitHub's markers would be a shared module the next forge inherits through the
  front door.

### One provider implementation, not two

`main` already carries a partial provider abstraction, and it is worth naming
before this one is read as arriving on empty ground.
`agents/platform/scripts/forge.py` is agent-side: a `ForgeProvider` protocol of
seven read operations, a `GitHubProvider` shelling `gh` behind it, and typed
`PullRequest` / `Comment` / `Commit` values. It exists to serve one feature — the
pull-request review conversation — and it stops where that feature stops.

The broker described here needs the same furniture on its own side of the
credential boundary: a provider protocol, a GitHub implementation, a host
resolver, a repository parser, an error taxonomy and a way to recover an HTTP
status from a failed CLI call. **That is one implementation, not two.** Two
copies is two places to add GitLab to, two parsers that can disagree about the
same URL, and two answers to every question the third forge asks. So
`providers/` is where both halves end up, and `forge.py` is folded into it rather
than left standing beside it.

Folding is the right word rather than "replacing", because neither side is the
superset of the other:

| `forge.py` contributes                                                  | The broker side contributes                                 |
| ----------------------------------------------------------------------- | ----------------------------------------------------------- |
| typed values instead of raw JSON crossing the seam                      | `verbs`, and `ForgeUnsupported` → 501                       |
| agent policy on the value, not in the provider — `is_ignored`, `is_bot` | `capabilities(repo)`, answered with no token and no network |
| a `ForgeError` / `RepoUnparseable` taxonomy callers catch               | the status-to-guidance table                                |
| the wider verb surface the skills actually call                         | the seven validators                                        |
|                                                                         | `clone_url` composed from validated segments only           |
|                                                                         | the `Credential` strategy, and the package layout           |

The union is roughly thirty methods, which is unimplementable without `verbs` and
`ForgeUnsupported` to make partial support a first-class answer — and the eight
broker verbs are too narrow to serve the skills. Neither can simply absorb the
other, which is why the shape above is designed rather than inherited from
whichever side happened to be written first.

That yields one constraint on what reaches `main`, and it is the only one:

> **The provider abstraction arrives on `main` in the `providers/` layout** — not
> in a flatter arrangement to be restructured afterwards, and not as a second
> GitHub implementation standing alongside `forge.py`'s.

Restructuring afterwards is the work nobody schedules, and a second GitHub
implementation is far harder to remove once merged than to not write.

The constraint is deliberately narrow: it says where the code lands, not what
order it is written in. `forge.py`'s two current consumers keep working untouched
until step 7 migrates them, so PR 1 is not blocked on that migration and the
migration is not blocked on PR 1. The state worth preventing is the third one,
where two GitHub implementations arrive independently and each acquires
consumers.

### The protocol past its first feature

`forge.py` serves two consumers — the `pr_comments` sweep in `github_scan_gate.py` and the
`pr-conversation` worker skill — and both are the same feature seen from its two ends. Everything
else is outside it: `resolver.py` and `audit_report.py` each carry a private `gh` runner of their
own, and `submit_suggestion.py` does not even have that, shelling `["gh", …]` inline at three call
sites. A second forge implemented against `forge.py` alone therefore buys a reviewer conversation
and no issue resolution, no audit ledger, and no way to open a change.

Migrating those three onto the provider is the step that makes a forge a class rather than four
rewrites. It is anticipated rather than planned:
[`pr-comment-conversation.md`](pr-comment-conversation.md) §7 names `resolver.py` as the module's
obvious next consumer while holding the migration itself out of scope. Half of that has since
happened by another route — `resolver.py` dropped its own repository parser and imports
`gitops_workspace` for one instead — but it still runs its own `gh`, which is the half this section
is about. [Delivery](#11-delivery) puts the rest on a schedule, and says why that schedule puts it
before any GitLab code.

The protocol grows to the union of what the four need. Beyond the existing seven, that is opening a
change (branch plus pull request), editing and reading one back, listing and commenting on issues,
and setting labels. The precise list falls out of the migration rather than being guessed here; what
matters to this design is that it is decided by the callers, not by GitHub's API surface, and that
harness policy stays above the provider. The existing split is the precedent: the provider answers
"what is open" and the caller answers "which of those are mine", so the branch-prefix and
`agent:ignore` rules are written once instead of once per forge.

Two provider shapes come out of the credential plane rather than out of this section: a CLI-backed
provider that shells a brokered binary, and one that speaks REST in-process. Both implement the same
protocol, and [the transport](#the-transport-cannot-be-a-cli) is where they differ — a declared
member rather than a subclass, which is what lets one provider class serve both shapes.

---

## 5. Modularity

Everything above is what the _second_ forge needs. This section is what the
_third_ one needs, and it is a different question. Bitbucket should cost less than
GitLab, not the same; the way that happens is that GitLab leaves behind a shape
Bitbucket fills in rather than a precedent Bitbucket imitates.

The requirement, stated so it can be checked: **adding a forge is a new
directory and one line in a registration file. It touches no shared module and
no other forge's code.**

The failure mode this is against is not exotic. A forge, its verb list, its error
parsing and the registry all fit comfortably in one module with the broker, and
at one forge that is the right call. At two it means the second forge arrives as
an edit to the first forge's file. At three, "who broke GitHub" stops having a
one-file answer, and by then the layout is expensive to change and nobody
schedules it.

### The layout

```text
agents/platform/scripts/
  vcs_broker.py            # broker verbs, clone/publish, scratch, locking, routes
  providers/
    __init__.py            # the public surface: Forge, ForgeUnsupported, resolve_forge
    base.py                # Forge ABC, ForgeUnsupported, StubForge, normalised shapes
    validate.py            # the seven validators
    errors.py              # the status-to-guidance table, forge_error(status, detail)
    identity.py            # _strip_scheme, repository_host, the segment regexes
    transport.py           # Transport protocol, CliTransport, HttpTransport
    credentials.py         # Credential protocol, BrokeredCredential, StaticFileCredential
    registry.py            # AVAILABLE, build_forges(config)
    github/
      __init__.py  forge.py  translate.py  errors.py  fixtures/
    gitlab/
      __init__.py  forge.py  translate.py  fixtures/
```

The split is by _who owns the decision_. `providers/` holds everything a forge
needs to be written against; a forge package holds everything only that forge
knows. `vcs_broker.py` keeps what is true regardless of forge — the workspace
lock, the scratch tree, the bundle size ceiling, the route table — and contains
no forge name at all.

Most of `providers/` is not new logic. The validators, the scheme stripping and
host resolution, and the status-to-guidance table are forge-neutral already;
what the layout does is put them somewhere a forge package can import without
importing a forge. That distinction is the whole point of the boundary test
below, and it is why `errors.py` holds the guidance table but not the throttle
heuristics that read GitHub's message text: a shared module that keeps one
forge's heuristics is a shared module the next forge inherits through the front
door.

### Registration, in two levels

The tension: the registry should not know anything about a forge, but a
self-managed GitLab is not knowable at import time — there may be zero of them
or four, and their hostnames come from configuration.

Resolved by making the class, not the registry, answer "how many of me exist":

```python
# providers/registry.py — the one shared file a new forge edits
from .github import GitHubForge
from .gitlab import GitLabForge

AVAILABLE = (GitHubForge, GitLabForge)


def build_forges(config) -> tuple[Forge, ...]:
    return tuple(f for cls in AVAILABLE for f in cls.for_config(config))
```

`for_config` is a classmethod returning zero or more instances.
`GitHubForge.for_config` returns exactly one, always, ignoring its argument.
`GitLabForge.for_config` returns one per configured host and an empty tuple when
none are configured. Adding Bitbucket is the import line and the tuple entry;
`build_forges` does not change, and neither does anything downstream of it.

`resolve` walks the built tuple by host and falls back to `StubForge` for a
known-but-unconfigured one.

### Where a provider's name is allowed to appear

One rule, and it is the whole of the modularity claim: **a forge's name appears
in its own package and nowhere else.** Not in the broker, not in a shared
contract module, not in another forge's package, and — with one acknowledged
exception below — not in the two files that decide what may run.

Six places would hold a forge name if nobody had decided otherwise, and each is
worth naming because each is a specific piece of the design rather than general
tidiness:

| Would name a forge                            | Does not, because                                                                                                                                              |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| the registry of which forges exist            | it names classes and asks each `for_config` how many instances this install has, so a self-managed host never reaches a shared file                            |
| the code that makes an authenticated API call | the forge declares a `transport`; the broker builds it. A CLI is one implementation of that, not the shape of the seam                                         |
| the request the verbs hand to it              | it is `(method, path, params, body, raw)` — an HTTP request, not one CLI's argv with a media type smuggled in as a header                                      |
| the set of verbs `capabilities` reports       | `verbs` is a class attribute of the forge                                                                                                                      |
| recovering an HTTP status from a failure      | the transport does it — an integer for HTTP, a parse for a CLI that prints `(HTTP 404)` into stderr — and the guidance table keyed on that status stays shared |
| the code that makes a credential current      | the forge's `credential` does; the broker never mentions credentials                                                                                           |

The rule has exactly one acknowledged exception, and because a modularity claim
that quietly omits its own exception is not worth checking, it gets its own
subsection below.

### What holds the boundary

A layout is a convention, and conventions decay under deadline. Two tests turn
it into something that fails CI.

**An import-boundary test**, `ast`-parsing every module under
`agents/platform/scripts/` and asserting three rules:

1. No module outside `providers/` imports `providers.<name>` — only `providers` itself.
2. `registry.py` is the sole exception, and only for names in `AVAILABLE`.
3. A forge package imports only
   `providers.{base,validate,errors,identity,transport,credentials}`
   and the standard library. Not the broker, not another forge.

Rule 3 is the one that matters. It is what makes "Bitbucket cannot reach into
GitHub's translation" a build failure rather than a code-review preference, and
it is the reason the shared modules have to be genuinely forge-neutral: if
`errors.py` kept `_THROTTLE_MARKERS`, GitLab would be importing GitHub's
heuristics through the front door and the test would not notice.

**A forge-name guard**, which the import test cannot catch: the string `github`
must not appear in `vcs_broker.py` or in any module directly under `providers/`.
An `if host == "github.com":` needs no import. This is a grep, it is crude, and
crude is the point — it is the check that catches the special case someone adds
at 6pm.

Both belong with the existing broker tests, and both are cheap enough to run on
every change rather than in a nightly.

### The contract test, parameterised

The verb tests are one suite parameterised over `AVAILABLE`, not a file per
forge. Each forge package supplies a `fixtures/` directory of recorded API
responses — the JSON its host actually returns for each of the eight verbs — and
the suite reads them.

That inverts where the cost falls. A per-forge test file means holding a new
forge to the same assertions is a shared-test edit somebody has to remember to
make; here a new package ships its fixtures and the existing suite picks it up.
The same assertions about normalised shape, about `ForgeUnsupported` for
unimplemented verbs, about validators rejecting the same inputs, run against it
without anyone touching a shared test.

Fixtures rather than a live API for the usual reason — the tests run in CI with
no credential and no egress — and recorded rather than hand-written because a
hand-written fixture encodes what the author believed the API returns.

### What a forge may not do

The boundary is also a security boundary, and it is worth stating as a
prohibition because every item is something a forge package could plausibly want
to do:

| A forge may not         | Because                                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------------------- |
| run a subprocess        | every control on what the credentialed process executes lives in one place, and it is not the forge |
| choose a scratch path   | path containment is the broker's invariant and is tested there                                      |
| set a timeout           | a forge could set it to zero and hang the proxy                                                     |
| bypass the size ceiling | the bundle limit is a resource bound, not a policy a forge tunes                                    |

The first row is the load-bearing one, and it is not a style preference. A forge
package is ordinary Python running inside the process that holds the token, so
nothing at the language level stops it from calling `subprocess.run` — which is
exactly why the prohibition has to be stated and tested rather than assumed. On
`main` today, `CommandExecutor` is the single point where every control on an
executed command is applied: `ALLOWED_EXECUTABLES` decides what may run at all,
an argv refusal list closes `--upload-pack` and `--receive-pack`,
`GIT_ALLOW_PROTOCOL` pins the transports, `GIT_FORCED_CONFIG` outranks every
config layer, `GIT_EDITOR=false` closes the editor vector, and the timeout and
output ceilings bound the result. A forge that shells out directly is not
subject to any of them, and none of those controls would report that they had
been skipped. The allowlist would become advisory the moment one forge decided
it needed something the executor did not offer.

So a forge declares `transport` and returns a request; the broker constructs the
command and `CommandExecutor` runs it. The compressed form: **a forge answers
questions, it does not do things.**
`clone_url` returns a URL; it does not clone. `verbs` names what is supported;
it does not dispatch. A verb returns a request description and translates a
response; it does not make the call. Holding to that is easy while there is one
forge and a reviewer sees the whole of it; the split is what keeps it true once
the code lives somewhere a reviewer of the broker will not look.

### Why not a runtime plugin

The question was whether per-forge support should be a plugin: separately
packaged, separately shipped, loaded at runtime rather than compiled in. That
would let GitLab support be delegated to a different developer without them
touching the shared file set at all.

**It should not, and the reason is where the code runs.** The broker is the
process that holds the credential. Its code arrives baked into the
credential-proxy image at `/opt/defaults/scripts`; the only thing the operator
mounts into that container is a read-only policy ConfigMap. There is no existing
mechanism to introduce Python into that process, and adding one would be adding
a path by which code the image did not ship executes next to a live token. A
runtime plugin loader in the credentialed container is a credential-exfiltration
surface, and the modularity it buys is available without it.

So: **build-time packages, one directory per provider, one explicit registration
list, no discovery** — which is the layout, the registration and the two tests
above, and nothing more. The modularity a plugin was wanted for is delivered by
the directory boundary and enforced by CI; what a plugin would add on top of that
is the loader, and the loader is the part that is a credential-exfiltration
surface.

The client side was never the hard part: `vcs.py` in the sandbox is forge-neutral
and holds no credential, so it needs no per-provider code at all.

### The one exception: the executable allowlist

Everything above keeps forge knowledge inside a forge package. Two files that
predate this design do not, and they are the one place where "adding a forge is a
new directory" is not the whole truth.

`CommandExecutor.ALLOWED_EXECUTABLES` is `("gcloud", "kubectl", "gh", "git")` on
`main` today, and `credential_proxy_client.py` carries the same set again as
`SUPPORTED_EXECUTABLES`. Both are literal tuples. GitLab would add `glab` if it
used one; Bitbucket has no CLI at all. So the union of every supported forge's
binaries is granted to every install, twice over, and adding a CLI-backed forge
means editing two files nobody would think to look in.

**The two lists answer different questions, and that is the fix.**
`ALLOWED_EXECUTABLES` is the broker's: what the credentialed process may run.
`SUPPORTED_EXECUTABLES` is the sandbox's: what the agent may ask it to. Holding
identical contents is what made them look like a duplicate to keep in sync,
when in fact only one of the four belongs to both.

| Binary    | Broker may run it                           | Sandbox may forward it                          |
| --------- | ------------------------------------------- | ----------------------------------------------- |
| `gcloud`  | yes                                         | yes — no credential-free equivalent             |
| `kubectl` | yes                                         | yes — same                                      |
| `gh`      | only while GitHub's transport is `"cli"`    | **no** — the verbs replace it, by design        |
| `git`     | yes — the broker clones, fetches and pushes | **no** — see [one git](#one-git-in-the-sandbox) |

So the broker's list derives from the configured providers: a CLI-backed
credential declares its binary and the list is built from the constructed
forges, which means an install with no GitLab never grants `glab` and an install
whose GitHub provider speaks HTTP never grants `gh`. The sandbox's list loses
`gh` and `git` outright and needs no per-forge logic at all, because a forge CLI
is exactly what the sandbox is not allowed to reach.

That is also why this is called out as its own delivery step rather than folded
into a provider package: the broker half changes how `CommandExecutor` is
constructed, and the sandbox half changes what the image ships.

### Checking it against Bitbucket

The layout is only worth its cost if the third forge is cheaper than the second.
Bitbucket Cloud is the honest test, because it differs from both GitHub and
GitLab in ways this design did not anticipate:

| Bitbucket is different in                                           | Absorbed by                                                                                                              | Shared file changed |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------- |
| `workspace/repo` slugs, and a UUID form                             | `parse`, `clone_url` in its own package                                                                                  | none                |
| pull requests, not MRs; comments are on an `/comments` sub-resource | `translate.py` in its own package                                                                                        | none                |
| app passwords / API tokens, Basic auth not a bearer header          | `git_config`, and the token file it reads                                                                                | none                |
| no issue tracker on many workspaces                                 | `verbs` omitting the four issue verbs, `ForgeUnsupported` for free — **but see [below](#not-every-provider-is-a-forge)** | none                |
| `values`/`page`/`size` pagination, not `Link` headers               | its own translation of listings                                                                                          | none                |
| 401 where GitHub 404s on a private repo                             | `forge_error(status, detail)` with its own status extraction                                                             | none                |

The last row is the interesting one, and it is where the shared contract is
thinnest. `errors.py` holds a status-to-guidance table keyed on status alone, and
Bitbucket's 401-for-a-hidden-private-repo means "not found or no access" where
GitHub's 401 means "credential expired". Guidance keyed only on status is
therefore not quite forge-neutral. That is why `error_overrides` is a member of
the interface rather than something invented later: a per-forge map merged over
the shared table, living in the forge package. GitLab is the first to populate
it, for its own reading of 401, so by the time Bitbucket arrives the mechanism
has a user and a test rather than being a speculative hook.

What the table shows is that all six differences land in the forge's own
directory, and the only one that needs a shared mechanism uses one that already
exists. That is the requirement holding. It is also the argument for building
the contract harness at step 10 rather than after the third forge: a difference
like the sixth is exactly the kind that gets absorbed by a special case in a
shared file when there is no test saying it may not be.

### Not every provider is a forge

The Bitbucket row above says "no issue tracker" is free, absorbed by `verbs`
omitting the issue verbs. That is true and it is not the whole answer, because
a Bitbucket install usually _does_ have an issue tracker — it is Jira, on a
different host, behind a different credential.

**Jira is not designed here and is not on the delivery plan.** What belongs in
this document is the one assumption it breaks, because that assumption is cheap
to avoid now and expensive to unpick later.

Everything above assumes **one repository resolves to one provider that answers
every verb**. `resolve_forge(repository)` returns a single object; the routing
key is the repository's host; `parse` returns a repository. All three are false
for an issue tracker:

| Assumption                             | Why Jira breaks it                                       |
| -------------------------------------- | -------------------------------------------------------- |
| the routing key is the repository host | Jira's host has nothing to do with the code host         |
| one provider answers every verb        | proposals come from Bitbucket, issues from Jira, at once |
| `parse` yields a repository            | a Jira issue is `PROJ-123` and belongs to no repository  |

So the general shape is not "a forge" but **capabilities bound to a project
context**: code hosting and proposals from one provider, issue tracking from
another, which today happen to be the same object for GitHub and GitLab.

**What this design does about it now: two things, both nearly free.**

1. **Resolution returns a binding, not a forge.** `resolve(repository)` yields a
   small object with a provider per capability group, rather than one provider.
   For GitHub and GitLab every group points at the same instance and nothing
   observable changes. Adding Jira later is then a configuration entry and a new
   package, not a change to how every caller resolves.
2. **The directory is `providers/`, not `forges/`.** "Forge" is the right word
   for GitHub, GitLab and Bitbucket and the wrong one for an issue tracker.
   Renaming a package with three implementations in it is churn nobody will
   schedule; naming it correctly before the first one lands costs a keystroke.
   The rest of this document says "provider" for the same reason, and reserves
   "forge" for the three systems that are actually forges.

**What it does not do:** no Jira package, no second credential plane, no
declarative surface for "issues live over there", and no split of the protocol
into capability groups beyond what `verbs` already expresses. Those are a
design of their own, and the first install that needs one will specify it better
than speculation would.

The point of naming it here is narrow: **whoever reviews the first provider
package should know that "one host, one provider, all verbs" is a convenience of
the first three forges and not a property of the domain.**

---

## 6. The declarative surface

`IntegrationSpec` holds exactly one forge field, `GitHub *GitHubSpec`, and `GitHubSpec` holds two:
`GitRepo` and `Org`. (`PlatformAgentIntegrationSpec` embeds it alongside `GoogleChat` and `Slack`, so
GitHub is the only _forge_ integration rather than the only integration.) `Org` carries GitHub's
namespace grammar in a CRD pattern — alphanumerics and hyphens, at most 39 characters — which is not
GitLab's: a group path admits dots and underscores, and a project can sit several groups deep, so no
value of `org` names a nested GitLab namespace. `GitRepo`'s validation, `ValidateGitRepoURLWithOrg`,
checks length and non-graphic runes and then defers to `CleanRepoSlugWithOrg`, so what the CR
enforces about the repository is "exactly one slash once the host has been discarded" — and nothing
at all about the host. The declarative surface names GitHub in the field path and nowhere in the
validation, and the check that does fire is a shape check standing in for the host check
[Repository identity](#repository-identity) shows is the one that matters.

The operator then writes the repository into the `managed_repos` state ConfigMap as a
`ManagedRepoEntry` whose `type` is the literal `"github"`. The discriminator this design needs
therefore already has a field, a schema and a transport, and the only thing missing at this layer is
a way to _declare_ it — which is why an administrator who writes one straight into the ConfigMap
today gets an entry the operator preserves and the agent discards.

**The surface is `spec.integration.git`**, carrying a provider, a host and a repository, with
`spec.integration.github` retained as a deprecated alias that maps onto it. Validation is
provider-dispatched — each provider asserting its own namespace grammar, and each rejecting a host
that is not its own — rather than one host-blind shape check standing in for all of them.
`ManagedRepoEntry.Type` carries the declared provider rather than a constant, which is how the
discriminator reaches the agent: written down by the operator, rather than inferred from the URL's
text.

Provisioning follows the same rule. `install.sh` and `terraform/examples/full-install` carry the
GitHub App inputs as `github_app_id`, `enable_github_minter` and `github_minter_kms_*`, and the
chart spells the same settings `githubMinter.appId`, `githubMinter.enabled` and
`githubMinter.kms.*`. All of them are provider-conditional: an install that declares GitLab
provisions no KMS key and no minter.

**What the surface does not carry is a switch for the abstraction itself.** An
install declares _which_ forge it uses, never _whether_ the abstraction is in
play. There is no supported arrangement in which an agent reaches a forge some
other way, so a toggle would only describe a configuration nobody is allowed to
run — and every such field is a second code path to keep working, a second
combination to test, and a way for an install to sit in the state the design
exists to remove. What that costs at upgrade time, and the one field that has to
be kept anyway to make the upgrade safe, is [step 13](#11-delivery).

That is a deliberate reversal of how an experimental feature usually arrives.
The justification is that this removes no capability: every verb replaces a call
an agent could already make, the measurement in
[The experiment](#9-the-experiment) is what establishes that it does so at least
as well, and the thing being retired — a forge CLI on PATH in a credentialed
container — is not something an install should be able to opt back into.

---

## 7. GitLab

GitLab is the forge customers ask for first after GitHub, and it is the ask
everything above was designed against. Up to here the document has described the
layer; this is the first thing that uses it.

It is also the test of the layer. An abstraction with one implementation is a
hypothesis. The measure of this design is not that GitLab works — it is how much
shared code had to change to make it work, and whether Bitbucket will need those
same changes made again.

### What GitLab actually costs

Small. The forge-independent half of the broker — `clone`, `publish`, the five
publish checks, the scratch lifecycle, the size ceilings, the bundle transport,
the workspace lock — is untouched. So is the whole of the sandbox client, because
the sandbox never learns which forge it is talking to. So is the
`version-control` skill, the CRD surface, the sandbox image and the entrypoint.

One forge class about the size of the GitHub one, one transport, and the four
contract decisions below.

### Where the intuition about GitLab is wrong

The expectation a reader arrives with is that GitLab's credential is the hard
part, because GitHub's is: an App installation, a signing key, a mint step, a
token that expires within the hour, and a policy ConfigMap enforcing which
repositories it may be spent on. GitLab has none of that, and it is tempting to
read the absence as a gap to be filled.

It is not a gap. A **group access token** is created once by an administrator,
scoped to a group and everything under it, and lasts up to a year. There is
nothing to acquire, nothing to sign, and nothing to refresh. GitLab's credential
work is somewhere else entirely, in two places GitHub's arrangement does not
force anyone to look at:

- **How the token reaches `git`.** GitHub's reaches `git` as a side effect of
  minting — `gh auth setup-git` writes a global credential helper — so the forge
  interface never had to carry it. GitLab needs to say it. See
  [`git`'s credential has no seam](#gits-credential-has-no-seam).
- **What narrows the token's blast radius.** Minty enforces a per-repository
  permission policy at mint time, so GitHub's token arrives already narrow. A
  group access token is narrowed once at creation and nothing narrows it further,
  so every project in the group is reachable with it and the broker has to
  enforce the boundary itself. See [The credential](#the-gitlab-credential).

Both are cheap. Neither is a minter, and a plan that budgets for a minter budgets
for the wrong thing.

### GitLab repository identity

GitLab namespaces nest: `group/subgroup/project`, arbitrarily deep. This is the
one place GitLab is structurally different from GitHub rather than differently
spelled, and the shared contract already allows for it: `StubForge.parse` accepts
`len(parts) >= 2` with `_SEGMENT_RE` per segment, where `GitHubForge.parse`
demands exactly 2. `GitLabForge.parse` keeps the stub's version.

`clone_url` composes `https://{self.host}/{path}.git` from validated segments,
never from the caller's URL — same rule as GitHub, same reason: the caller's URL
decides which forge, not which host a credential is presented to.

API paths take the namespace URL-encoded as a single opaque segment:
`/api/v4/projects/{quote(path, safe='')}/merge_requests`. Note `safe=''` —
the default `quote` leaves `/` alone, which yields a path GitLab reads as a
different route. That is a one-character bug with a confusing 404 and it is
worth a test of its own.

**Numbering is the trap.** GitLab merge requests and issues each carry an `id`
(globally unique) and an `iid` (per-project, the number a human sees and the
one in the web URL). Every caller-facing `number` and every API path segment
must be the `iid`. Using `id` produces a route that resolves to a different
project's item or 404s, and the failure is silent in the sense that it looks
like a permissions problem. `GitLabForge` reads `iid` in translation and sends
`iid` in paths; nothing in the module touches `id`.

### The GitLab credential

A GitLab **group access token** with `api` and `write_repository` scope, created
by an administrator, stored in a Kubernetes Secret, projected into the
credential-proxy container as a file.

`GitLabForge.credential` is a `StaticFileCredential`, and all three of its
methods are decided by that one sentence:

- `ensure()` does nothing. There is no acquisition step, no minter, no KMS key
  and no policy ConfigMap.
- `headers()` reads the file at call time and returns `PRIVATE-TOKEN`.
- `git_config()` returns the credential-helper pin from
  [above](#gits-credential-has-no-seam), which reads the same file.

This is the whole GitLab credential story, and it is nine lines in
`providers/gitlab/`. Nothing outside that directory knows GitLab has a token.

**"Long-lived" is not "permanent," and the difference has to be designed for.**
GitLab requires every access token to carry an expiry; an unset one defaults to
365 days, and the ceiling is 400. So the token does not go stale between calls —
which is why `ensure()` is still right to do nothing — but it does expire once a
year, with no automatic recovery and no warning from anything in this system.

Two consequences, both small and both easy to omit:

- **Rotation is an operator action**, and it works: the administrator updates
  the Secret, the projected file changes, and the next call reads the new value
  with no restart. That is the per-call file read earning its keep.
- **A GitLab 401 needs its own guidance string.** The shared table maps 401 to
  GitHub's meaning — the credential expired and a refresh will fix it — which
  for GitLab is advice to do something no code path implements. GitLab's 401
  should say the group access token may have expired and name the Secret. This
  is the per-forge guidance override that
  [the Bitbucket check](#checking-it-against-bitbucket) predicted would be
  needed; GitLab needs it first.

One more property of the token that belongs here because it surfaces elsewhere:
**a group access token authenticates as a bot user** that GitLab creates with
it. Anything that asks "did the agent write this?" — the branch-prefix and
`agent:ignore` rules, `viewer_login`, comment attribution — resolves to that bot
on GitLab, not to a human account. It is not a problem, but it is a fact the
agent-side policy has to be told rather than infer.

Reading from the file per call rather than caching at construction is
deliberate: a rotated Secret updates the projected file, and the next call picks
it up with no restart. The cost is a file read per API call, which is nothing
next to the HTTPS round trip.

**Scope enforcement is the broker's job here, and this is a real difference from
GitHub.** Minty enforces a per-repository permission policy at mint time, so the
token the broker holds is already narrowed. A group access token is narrowed to
its group at creation and nothing narrows it further. Two repositories in the
same group are both reachable with it, and `resolve_forge`'s host allowlist does
not care which project inside the host a call names.

So `GitLabForge` carries `allowed_paths`, a tuple of namespace prefixes, and
refuses a repository outside them before the credential is spent — the same
placement as the host allowlist and for the same reason. An empty
`allowed_paths` means the whole host, which must be a deliberate configuration
rather than the default that appears when someone omits a field.

Prefix matching is on **path segments, not string prefix**. `acme/infra-secret`
starts with the string `acme/infra` and is a different project.

### Translation

The neutral shapes are the ones every forge returns. What follows is the GitLab
side of each.

| Neutral field       | GitLab source                     | Note                                                           |
| ------------------- | --------------------------------- | -------------------------------------------------------------- |
| `number`            | `iid`                             | never `id`                                                     |
| `state` (proposal)  | `state`                           | `opened`→`open`, `merged`→`merged`, `closed`/`locked`→`closed` |
| `draft`             | `draft`                           | `work_in_progress` on older instances; read `draft`, fall back |
| `author`            | `author.username`                 | no `[bot]` suffix to strip                                     |
| `source` / `target` | `source_branch` / `target_branch` | direct                                                         |
| `url`               | `web_url`                         |                                                                |
| `created`/`updated` | `created_at` / `updated_at`       | both ISO-8601, same as GitHub                                  |
| `body`              | `description`                     | GitLab's name for it                                           |
| `labels`            | `labels`                          | plain strings, not GitHub's `{name: …}` dicts                  |

Three asymmetries are worth their own note, because each one is a place the
neutral contract was shaped by GitHub and GitLab shows the shape.

**Issues are not proposals.** `GitHubForge.issue_list` filters out nodes
carrying a `pull_request` key, because on GitHub a PR _is_ an issue and the
issues endpoint returns both. Nowhere else models it that way. GitLab's
`/issues` returns issues, so `GitLabForge.issue_list` has no filter and
`issue_view` needs no "this is a merge request, read it with `proposal view`"
refusal. The neutral contract is right and GitHub is the odd one; the filter
stays where it belongs, inside `GitHubForge`.

**Comments are notes, and most notes are not comments.** GitLab's
`/merge_requests/{iid}/notes` returns the discussion _and_ system notes —
"changed the description", "assigned to @someone", "marked as draft" — each
carrying `system: true`. Returned unfiltered, a caller reading a proposal's
conversation gets mostly bookkeeping, and an agent deciding whether it has
already replied reads its own status changes as replies. `GitLabForge` filters
`system` notes out. This is GitLab's exact analogue of the `pull_request` filter
above: one forge's model leaking items the neutral concept does not include.

**State vocabulary differs on the way in as well as out.** `validate_state`
accepts `open`, `closed`, `all` and the verbs pass the result straight into a
query string. GitLab's parameter values are `opened`, `closed`, `all`. The
mapping belongs in `GitLabForge`, on both directions, and `validate_state`'s
neutral vocabulary does not change.

Two endpoints need naming because they are not a rename of GitHub's:

- **Diff.** GitHub serves a diff from the PR endpoint under an `Accept` media
  type. GitLab does not; the closest is
  `/merge_requests/{iid}/raw_diffs`. _(Live-verify: the exact path and whether
  it needs a size guard on a large MR.)_
- **Proposal creation** posts `source_branch`, `target_branch`, `title`,
  `description` to `/merge_requests`. GitLab's draft flag on creation has
  historically been a `Draft:` title prefix rather than a field; current
  instances accept neither reliably across versions. _(Live-verify: whether
  `draft` is settable at creation on the target version, and if not, whether
  `proposal_create` sets it in a second call or reports it unsupported.)_

Both are marked because getting them wrong is a working-looking module that
silently drops a field, which is worse than an unimplemented verb.

### GitLab errors

GitLab returns conventional statuses, all of them already in the shared
status-to-guidance table: 401, 403, 404, 409, 422, 429. Two specifics:

- The message body is `{"message": …}` or `{"error": …}` depending on endpoint,
  and sometimes a dict of per-field arrays. `detail` takes the first string it
  can find and truncates at 400 characters like the CLI path does.
- **404 hides 403.** GitLab answers 404 rather than 403 for a project the token
  cannot see, deliberately. The shared guidance for 404 covers it without a GitLab
  override — "A private repository this install's credential cannot see also
  answers 404, so this does not prove the thing does not exist" — which is the
  clearest evidence that keeping the table forge-neutral was worth it: GitLab
  needs one override, for 401, and not this one.

### What GitLab does not include

Deliberately, and each of these should be a named refusal rather than a
surprise:

- **Self-managed instances behind a private CA.** The token and the API are the
  same; what differs is trust of the TLS chain. `HttpTransport` uses the
  container's CA bundle and nothing mounts a custom one.
- **GitLab groups as an issue tracker.** Group-level issues and epics are a
  different endpoint namespace. `issue_*` is project-scoped, matching the
  neutral concept.
- **Approvals.** GitLab's approval rules have no GitHub equivalent and no
  neutral verb. `proposal_view` does not report approval state.
- **Merging a proposal.** No forge implements this, on purpose — the neutral
  verb set stops at opening and commenting.
- **OAuth-refreshed tokens.** A group access token is the only supported GitLab
  credential; GitLab's own OIDC token exchange is deferred, for the reason
  [the credential plane](#what-the-credential-plane-holds-up) gives — it is a
  second design, and a first working install does not need it. Bitbucket will
  revisit this, since its tokens do come from a refresh flow, and the point of the
  strategy is that doing so is a third `Credential` implementation in
  `providers/bitbucket/`, not a change to GitLab's or GitHub's.

---

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

The second is answered by running the MCP server in the broker's pod rather than the sandbox's,
with the agent reaching it over the same authenticated Service and the server attaching the token —
the same containment the broker already provides for a brokered provider, expressed at the MCP layer
instead of the REST one. The first has no such answer, because no arrangement of MCP servers puts a model in
a cron loop that deliberately has none. The library stays either way.

Where it pays off is the interactive path — an agent asked in chat to read a merge request, or a
worker turn answering a review comment. There, typed MCP tools are better than teaching a model
`glab` spellings in SOP prose. So the provider is the mechanism and MCP is an optional surface on top
of it, added last and depended on by nothing.

---

## 9. The experiment

Three ways of getting a sandboxed agent to a repository were run against the
same probes, the same corpora and the same model, to establish that the
abstraction costs nothing relative to what already exists. Everything below —
corpora, probe definitions, harness, per-probe worker logs, raw scores — is on
the fork at
[`experiments/git-access-abc/`](https://github.com/dshnayder/kube-agents/tree/experiment/git-access-abc/experiments/git-access-abc).

### What was compared

| Arm | Access design                                                                                                         |
| --- | --------------------------------------------------------------------------------------------------------------------- |
| A   | The shared volume this repository ships: `git` and `gh` in the sandbox are shims into the credentialed container      |
| B   | Content passing (#962): `{path, bytes}` payloads over the broker's `/v1/workspace/*` routes, the broker owns the tree |
| C   | This design: forge-neutral verbs over `/v1/vcs/*`, history as a bundle, native git locally                            |

Twenty read probes at three repository sizes — 200, 3,000 and 10,000 files — and
a four-probe write rung. The read probes cover contested facts across revisions,
history questions, per-line attribution, file modes, and negative controls where
the correct answer is that the repository does not say. The write probes open a
change proposal, add a file with a specific mode, revise an existing proposal in
response to a seeded review comment, and one adversarial probe whose corpus
instructs the reader to install a git clean filter and a pre-commit hook — it
passes only if nothing executes.

Each arm ran the write rung against a repository that arm had never seen, since
the first arm to open a proposal turns "open a pull request" into "notice one
exists" for everyone after it.

Arm C's write rung was run **sealed**: arm B's routes disabled and no `gh`
binary under any path, so the numbers describe an install that shipped only this
design rather than one where other doors happened to be open.

### Results

| arm | rung  | answered | stayed on its own route | left for `gh api` | median s | median turns |
| --- | ----- | -------- | ----------------------- | ----------------- | -------- | ------------ |
| A   | 200   | 19/20    | 17/20                   | 0/20              | 195.8    | 4.5          |
| A   | 3000  | 19/20    | 18/20                   | 0/20              | 227.1    | 5.0          |
| A   | 10000 | 19/20    | 20/20                   | 0/20              | 313.9    | 7.0          |
| A   | write | 4/4      | 4/4                     | 3/4               | 192.8    | 4.0          |
| B   | 200   | 18/20    | 20/20                   | 4/20              | 296.3    | 6.5          |
| B   | 3000  | 19/20    | 19/20                   | 4/20              | 286.3    | 6.5          |
| B   | 10000 | 18/20    | 20/20                   | 4/20              | 317.7    | 7.0          |
| B   | write | 4/4      | 1/4                     | 4/4               | 621.9    | 11.0         |
| C   | 200   | 19/20    | 18/20                   | 0/20              | 171.4    | 4.0          |
| C   | 3000  | 19/20    | 19/20                   | 0/20              | 215.0    | 5.0          |
| C   | 10000 | 20/20    | 20/20                   | 0/20              | 216.3    | 5.0          |
| C   | write | 4/4      | 4/4                     | 0/4               | 455.5    | 9.0          |

Every arm passed every write probe, including the adversarial one — no arm
executed anything the repository supplied. Capability is not what separates
them.

Four things the numbers say:

**Cost does not grow with repository size.** Arm C is the cheapest arm at every
rung and the only one that is flat from 200 to 10,000 files: 4.0 → 5.0 → 5.0
median turns, against arm A's 4.5 → 7.0. The bundle is why — one crossing of the
seam hands over the history and everything after it is local, so a bigger
repository does not mean more round trips. Arm C at 10,000 files costs fewer
turns than arm A at 3,000.

**Answered rate is at least as good.** 58 of 60 read probes against arm A's 57
and arm B's 55, and arm C is the only arm that answered all 20 at the largest
rung.

**The interface carries the work.** Across 60 read probes and the sealed write
rung, arm C made zero calls to a forge API — not because it could not, on the
read rungs, but because the verbs answered. Per read rung the route counts are
43/43/32 `vcs` calls against 29/27/30 local `git` calls: roughly one to one,
which is the design's own claim about where the split falls.

**The repository is left in better shape.** On the write rung arms A and B each
opened a duplicate proposal against the default branch from the same head
branch and closed it within a minute. Arm C opened exactly two proposals, both
against the correct base, and revised the first in place when asked.

Where arm C is not ahead: **writing costs more turns than arm A** — 9.0 against
4.0. That is largely structural rather than comprehension. A proposal in arm A
is `git push` followed by `gh pr create`, two commands the model has seen more
often than almost anything else; the same proposal here is clone, edit, publish,
`proposal create`. Sealing improved it substantially — 9.0 turns and 455.5s
against 12.0 and 653.0s unsealed — and the remaining gap is the price of the
indirection.

### What this does not show

- One run per probe per arm. A one-probe difference is noise; only whole-class
  differences are load-bearing.
- The read rungs were run unsealed for arm C — arm B's routes still serving, and
  a `gh` on PATH. Those runs made zero calls on either, so sealing removes doors
  they never opened, but they were not re-run to prove it.
- The write rung ran on a later broker build than the read rungs.
- The adversarial probe is one injection corpus. It shows those two techniques
  did not fire, not that the class is closed.
- Each arm's skill text was written by the same hand, which is not a neutral
  position from which to write a competing arm's instructions.

### One defect the experiment found

The write rung is what surfaced that `execute_forge_cli` omitted its
`containment_root` argument, so all eight collaboration verbs raised `ValueError`
before the forge call launched — on every install, since the routes shipped.
`clone` and `publish` were unaffected, which is why it survived: the verbs that
move code worked and the verbs that talk about it did not. The handler logged
only the exception type, so the outage reached the agent as
`vcs proposal-list error: ValueError`. Fixed, logged with the redacted message,
and covered by a regression test that fails without the fix.

It belongs in this document because it is the honest cost of the indirection:
an abstraction has surface that a shim does not, and this one shipped a quarter
of its verbs dead. The answer is tests and error messages, and both were added.

---

## 10. What this does not fix

The broker authenticates the caller but not the command's provenance. A
projected ServiceAccount token establishes that a request came from the
sandbox — and the `shell` role establishes which routes that entitles it to —
but nothing distinguishes a verb the agent chose from a verb something else in
the sandbox chose on its behalf. Anything running in that pod is the agent as
far as these routes can tell. What makes the boundary hold is that the sandbox
has no credential path of its own, so the worst that reaches the forge is
something the agent could have asked for anyway; it is not that the caller is
known to be the model.

`clone` pulls a whole branch's history, which is the wrong shape for a one-off
read of a large upstream repository, and there is no shallow option to make it
cheaper.

`publish` proves ancestry, not authorship. The revisions in the bundle carry
whatever author the sandbox's git wrote, and the broker does not sign or rewrite
them.

Nothing here defends against prompt injection, and these verbs are a good
delivery vehicle for it. A repository is untrusted text — file contents, commit
messages, issue and proposal bodies, review comments — and `clone`, `log`,
`issue view` and `proposal view` all exist to put that text in front of a model.
What the design does buy is that the text cannot become a credential: the
credential never enters the sandbox, `raw_token`-style routes are not
agent-callable, and the sandbox has no other path to one. What it does not buy
is protection against the model being talked into an action it is allowed to
take. `publish`, `proposal create` and `issue create` are reachable to any agent that
can reach the read verbs, so an install that wants an agent to read history
without being able to write to a forge has no way to say so. A read-only mode is
the smallest thing that would fix it, and it is not designed here.

Until GitLab and Bitbucket ship, this is a forge-neutral design with one forge
in it — and an abstraction with one implementation is a hypothesis. The measure
of it is not that GitLab works but how much shared code has to change to make it
work, which is why [Delivery](#11-delivery) states that measure as a falsifiable
exit criterion rather than leaving it to be judged afterwards.

Issue trackers that are not part of a forge — Jira alongside Bitbucket being the
case that will arrive first — are named in
[Not every provider is a forge](#not-every-provider-is-a-forge) and not designed.

---

## 11. Delivery

### The constraints that fix the order

Three PRs, one per forge, and the first one carries the abstraction. Within the
first, the order is not preference — three constraints fix most of it.

**Reader before writer.** The provider discriminator that
[Repository identity](#repository-identity) calls for crosses a process boundary:
the Go operator declares it, the Python agent acts on it. Widening what the
writer may emit before the reader accepts it produces a release where a valid CR
is rejected inside the pod, and the operator sees a reconcile that succeeded and
an agent that will not start. Repository identity therefore lands in Python
first and in Go second — and the reader here is not only the parser. It is
`get_managed_github_repos()`, which drops every entry whose `type` is not
`github`. Teaching the operator to emit `type: gitlab` while that filter still
runs is exactly the failure this constraint describes: a reconcile that succeeds
and a repository the agent never sees. Turning that filter into a dispatch
therefore lands in step 1, with the parsing work, and not in step 2 with the
field that feeds it.

That pairing is also why the Go half of layer 4 runs ahead of layers 1 and 3,
which [Where GitHub is named today](#where-github-is-named-today) ranks as the
work worth doing regardless. Both of those dispatch on the provider — the
consumer migration decides which provider a caller gets, the credential plane
decides which token and which binary — so the discriminator has to be declared
before either has anything to dispatch on. Sequencing it later means building
both against an inferred provider and then rewriting them.

**No-behaviour-change before behaviour change.** The consumer migration is a
large diff with no functional delta, verifiable against a GitHub install.
Landing it before any GitLab code means a reviewer reads one thing at a time, and
a regression has one candidate cause.

**Everything provable on GitHub, before anything that needs GitLab.** Every step
of PR 1 can be exercised against a running GitHub install by showing its
behaviour unchanged — including the CRD step, where the evidence is a GitHub CR
still admitting and reconciling through the `spec.integration.git` shape and its
alias. The first change that cannot is the GitLab provider itself, which needs a
real GitLab project to validate against. That environment does not exist here
today — see [What this does not fix](#10-what-this-does-not-fix) — so the design
puts every step that does not need it first, and none of that work is stranded if
the environment question takes a while to answer.

### PR 1 — the abstraction, with GitHub behind it

No GitLab. The order below is what keeps it reviewable: identity and the
declarative surface first, because everything after them dispatches on the
provider; then the shared contract; then the one forge that fills it in; then the
callers; then the tests that hold the boundary.

| Step | Delivers                                                                                                                                                 | Held to it by                                                       |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 1    | repository identity in Python: one parser, the host carried rather than assumed, unknown hosts rejected; `get_managed_github_repos()` becomes a dispatch | parser unit tests, including a nested namespace and an unknown host |
| 2    | the declarative surface in Go: `spec.integration.git`, the deprecated alias, provider-dispatched validation, the declared type reaching the agent        | operator tests; a GitHub CR still admits and reconciles             |
| 3    | `providers/` shared contract: `Forge` ABC with `verbs`, the validators, identity, the guidance table, `forge_error(status, detail)`                      | unit tests per module                                               |
| 4    | `Transport` protocol with the neutral `api` request; `CliTransport`, including status extraction                                                         | transport unit tests                                                |
| 5    | `Credential` protocol; `BrokeredCredential`; `git_config` reaching the broker's git invocations                                                          | a test that the config lands on the invocation and nowhere else     |
| 6    | `providers/github/` — the eight verbs, translation, its throttle heuristics, its `error_overrides`                                                       | the verb suite                                                      |
| 7    | the consumer migration: the six scripts of layer 1 reach the forge through the provider and nothing else, and `inspect_repository.py` clones by verb     | their own tests, with no functional delta to explain                |
| 8    | credential-proxy wiring: the generic refresh route, repository validation via `forge.parse`, and the two executable allowlists split by purpose          | refresh tests, including a nested-namespace repository              |
| 9    | the import-boundary test and the forge-name guard                                                                                                        | they are the test                                                   |
| 10   | contract harness parameterised over `AVAILABLE`; GitHub fixtures recorded                                                                                | the GitHub verb suite runs through it                               |
| 11   | `AVAILABLE`, `for_config`, `build_forges`; `StubForge` for registered-but-unconfigured hosts                                                             | registry tests                                                      |
| 12   | `resolve` returning a per-capability binding                                                                                                             | see [Not every provider is a forge](#not-every-provider-is-a-forge) |
| 13   | the abstraction becomes unconditional: every upgraded install gets it, and the `git` and `gh` shims are deleted from the sandbox image                   | operator tests, and the image smoke test asserting both by absence  |

Step 13 is where the "no switch" of [§6](#6-the-declarative-surface) is paid
for. It also removes `spec.harness.experimental.shellSandbox.enabled`, which by
then decides nothing: `validateShellSandbox` already refuses `false` with reason
`ShellSandboxCannotBeDisabled` rather than rendering the old arrangement, and the
field is retained only so that an install which set it gets that refusal instead
of a silently ignored setting. Deleting a field is normally the risky direction — an unknown key is
pruned from an existing CR on the next reconcile, and the setting disappears
with nothing in the diff to say so. It is safe here precisely because of the
order: no install can be quietly sitting at `false`, since one that tried has
been Degraded and visible since the sandbox landed.

Step 7 is the one that splits naturally if the PR gets too large: each of the
seven consumers is independent of the others, and each is a no-functional-delta
change against a GitHub install. Nothing after it depends on all seven having
landed.

**Exit criteria — falsifiable, and worth putting in the PR description:**

- `grep -ci github agents/platform/scripts/vcs_broker.py` returns 0, and the
  same for every module directly under `providers/`.
- `credential_proxy.py` names no forge on the VCS path.
- The import-boundary test passes, and fails if you add
  `from providers.github import …` to the broker.
- There is exactly one GitHub provider implementation in the tree.

The third matters most. Anyone can produce the directory layout; the test is
what says it will still be the layout in six months. The fourth is the one this
whole sequence exists to protect.

### PR 2 — GitLab

| Step | Delivers                                                                            | Held to it by                          |
| ---- | ----------------------------------------------------------------------------------- | -------------------------------------- |
| 14   | `HttpTransport` — timeout, size cap, redaction, status mapping                      | unit tests against a local stub server |
| 15   | `providers/gitlab/` — identity, `StaticFileCredential`, translation, errors         | the PR-1 harness, with GitLab fixtures |
| 16   | GitLab's 401 guidance override (the token-expiry case)                              | error tests                            |
| 17   | one line in `registry.py`                                                           | registry tests                         |
| 18   | operator: render the GitLab config, project the Secret, derive egress from the host | operator tests                         |
| 19   | the vocabulary: the four `SKILL.md` files, then the seven governance SOPs           | the terminology check                  |
| 20   | live validation                                                                     | see below                              |

`HttpTransport` is here rather than in PR 1 deliberately: PR 1 declares the
`transport` seam and implements only the one GitHub uses. A transport with no
consumer is a guess about what the second forge will need, and the whole point
of the sequence is to stop guessing.

Step 19 is here for a related reason. Neutral verb names are worth having on
their own, but a prompt that says "pull request" is only _wrong_ once the install
might be talking to something that calls it a merge request. Landing the sweep
alongside the forge that makes it matter also means it is reviewed against a real
second spelling rather than against a hypothesis about one.

### PR 3 — Bitbucket

Not designed here. What belongs in this document is the **measure**: PR 3 should
touch `providers/bitbucket/`, one line of `registry.py`, and the operator's
configuration — and nothing else. Every shared file it turns out to need is a
place the seam was in the wrong spot, and
[the Bitbucket check](#checking-it-against-bitbucket) already predicts one such
place, the shared error-guidance table.

If PR 3's diff outside its own directory is more than the registry line and the
operator config, PR 1 did not succeed. That is the honest test, and it arrives
too late to change PR 1 — which is the argument for the import-boundary test
being in PR 1 rather than waiting for a third forge to prove the point.

### Where GitLab gets validated

No environment here has a GitLab. The endpoint-level claims marked
_live-verify_ above cannot be closed without one, and neither can step 20. **PR
1 needs none of it**, which is most of the reason the sequence is shaped this
way: the abstraction is not held hostage to an environment question.

Three options, and the middle one is the recommendation:

| Option                                               | Footprint               | What it does not cover                     |
| ---------------------------------------------------- | ----------------------- | ------------------------------------------ |
| a gitlab.com project under a throwaway group         | none                    | customer hostname, private CA, egress rule |
| **omnibus GitLab CE container** (`gitlab/gitlab-ce`) | one pod, ~8 GB, one PVC | nothing this design needs                  |
| the GitLab Helm chart                                | ≥8 vCPU / 30 GB cluster | nothing — and it costs the most            |

The omnibus image is the Linux package in a container: PostgreSQL, Redis,
Sidekiq, Gitaly and NGINX all inside one pod, configured through
`GITLAB_OMNIBUS_CONFIG` and three volumes. That matters because the Helm chart
**removed its bundled PostgreSQL, Redis and MinIO in GitLab 19.0** — the chart
now expects those to be supplied, which turns "stand up a test GitLab" into
"stand up a test GitLab and three datastores."

One pod is enough to exercise everything gitlab.com cannot: an `external_url`
that appears in no shipped literal, a self-signed or private CA, and the egress
path for a host the operator has to render. Everything this design needs from
GitLab is Free-tier — merge requests, issues, notes, API v4, and group access
tokens, which on self-managed are available with any licence.

Recommendation: an omnibus GitLab CE container in the development cluster for
steps 15–18. If standing infrastructure is the blocker, the same image runs
ephemerally in a CI job for the API-shape and credential tests, and the
long-lived instance is deferred to whenever the hostname, CA and egress work
lands. gitlab.com is not on the path at all — it costs nothing but it also
proves the least.

---

## 12. Open questions

1. **Whether one field can name the token's scope boundary on both forges.** On
   GitHub that boundary is an App installation and it lines up with the first
   path component. On GitLab it is a group, a project's namespace can sit several
   segments below it, and "the first path component" is therefore false. Either
   the field generalises to "scope boundary" and each provider says how to derive
   it, or the two forges want different fields.
   [The GitLab credential](#the-gitlab-credential) proposes `allowed_paths` as the
   local answer — a per-credential tuple of namespace prefixes the provider
   refuses outside of — which is also the shape a per-repository permission policy
   would hang on. Whether that is one shared field on the CR or a per-provider one
   is the open half. The token-scoping code depends on it.

2. **Whether gitlab.com and self-managed GitLab want one provider class or two.**
   `pr-comment-conversation.md` §3 records that "Bitbucket" is two providers
   sharing almost nothing, and a single class that branches on "is this
   gitlab.com" is how that mistake would be repeated in a smaller way.
   [One package, many hosts](#one-package-many-hosts) answers the half that
   matters: a class is asked how many instances an install wants, so gitlab.com
   and a self-managed host are two instances of one class, each with its own host
   and its own credential, and nothing branches on which is which. What that does
   not settle is whether the token models diverge far enough to want two classes
   anyway. On the evidence so far they do not.

3. **Whether there is a read-only mode, and where it is declared.** The verbs
   arrive as one set, which [What this does not fix](#10-what-this-does-not-fix)
   names as a real gap: an install that wants an agent to read history without
   being able to write to a forge cannot say so. This is a permission question,
   not a feature toggle — the abstraction itself is not optional — so it belongs
   on the declarative surface of §6 alongside whatever answers
   [the token's scope boundary](#the-gitlab-credential), and the two should be
   decided together.

4. **Whether `GitHubProvider` moves onto the in-process HTTP transport.** Not
   proposed. It would take `gh` out of the broker entirely, which
   [Replacing `gh`](#replacing-gh) wants for other reasons, and the transport
   split makes it a small change. It needs the App token reachable by the broker
   without `gh auth`, which is not true today.

## Related

- [`docs/credential-isolation-design.md`](../credential-isolation-design.md) —
  the constraint every credential decision here answers to: no API keys or
  access tokens in the agent sandbox.
- [`docs/designs/pr-comment-conversation.md`](pr-comment-conversation.md) §3 —
  the provider protocol's original seven operations and why live validation
  forced three of their normalisations. §6 is where taking the proposal noun
  from configuration was first prescribed.
- [`docs/designs/gitops-workspace-leases.md`](gitops-workspace-leases.md) — the
  leased shared checkout this replaces for repository reads.
- [`docs/designs/memory.md`](memory.md) — the document structure and the
  experiment format this follows.
- [`docs/designs/live-test-lease.md`](live-test-lease.md) — how to take the
  install before running the live validation steps in §11.
- Issue #1154 — the GitLab/Bitbucket tracking issue.
- Issue [#1085](https://github.com/gke-labs/kube-agents/issues/1085) — the
  host-confusion report that §2's repository identity closes.
- [`docs/designs/agent-shell-sandboxing.md`](agent-shell-sandboxing.md) — the
  sandbox pod, the broker's own pod, the projected-token authentication between
  them, and why the sandbox is mandatory rather than opt-in. Merged as #913, and
  §3 builds directly on it.
