# fleet-audit — The two-command lifecycle: `start` and the inspection

Part of the `fleet-audit` skill (`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`), which says when to open this file.

## The two-command lifecycle

Run both commands by their full path, `/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py`.
A cron run starts in a directory of its own (`<profile>/cron/runs/<job>_<run>`) and a kanban worker
in its card's workspace, so `./skills/...` resolves in neither. **You are not in a git checkout, and you do not need to
be.** The harness establishes its own workspace at
`/opt/data/gitops/<audit-id>/<owner>__<name>` and resolves every `remediation.path` against it. The
workspace is keyed by audit id because the audit streams share the volume with each other and with
every kanban worker: each one gets a tree nobody else writes in, so a colliding schedule can no
longer reset another stream's working copy out from under it. The repository comes from the
`$GITOPS_STATE_CONFIGMAP` ConfigMap, which the operator manages and which is readable before any
workspace exists.

What that workspace contains depends on the install, and `start` tells you which one you have as its
`mode` field:

- **`content`** — the workspace is an empty directory. The repository lives in the credential broker,
  which owns the only checkout; you write manifests into the directory and the harness hands the
  bytes over. Nothing on your side is a git repository, so there is nothing here to read the
  repository out of: use `list` and `fetch` (below) for that.
- **`directory`** — the workspace is a clone of the GitOps repository on the shared volume, and the
  harness runs `checkout`, `add`, `commit` and `push` inside it.
- **`none`** — no forge is configured; see [No forge configured](finish.md#no-forge-configured).

Between `content` and `directory` everything else is identical, including where you write manifests
and what `finish` publishes. Where the two differ, this file says which mode it is talking about.
`none` differs throughout, and its own section says how.

### Step 1 — `start`

Before inspecting anything, claim the workspace:

```bash
/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py start \
  --audit <audit-id> \
  [--repo "<owner>/<repo>"]
```

This resolves the target repository (using `--repo` if specified, falling back to the single
configured repo in `$GITOPS_STATE_CONFIGMAP`, failing if ambiguous across multiple repos, and
running with no forge when none is registered — see [No forge configured](finish.md#no-forge-configured)), makes
the forge credential current, establishes a clean workspace, ensures the audit's labels exist, locates
the stream's open ledger issue, and clears any findings document a crashed run left behind. If the
user asked for a specific repository that is not yet registered, instruct the user or cluster
administrator to add it to `$GITOPS_STATE_CONFIGMAP`. `--repo` takes the name as the managed list
gives it: `owner/name` on github.com, `<host>/owner/name` on a GitHub Enterprise Server, and
`<host>/<path>` for a repository on another forge (`gitlab.com/acme/platform/infra`). In content mode the broker's file workspace clones a repository
from its own forge, so `fetch`, `list`, `grep`, the remediation step and the declared-intent search
work the same way on every forge, and a remediation on GitLab is a merge request. Directory mode
serves GitHub only: its local clone reaches no other forge, so `start` and `finish` on a repository
elsewhere stop at the clone, with no ledger written. Run the audit in content mode for those. It
creates **no branch** — there is no report
branch. It prints exactly one JSON line:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "mode": "content",
  "workspace": "/opt/data/gitops/compliance-audit/acme__fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"],
  "carried": [
    {
      "id": "cluster-admin-binding.acme-prod-us-east1-prod-us-east._.clusterrolebinding-debug-binding",
      "check": "cluster-admin-binding",
      "cluster": "acme-prod/us-east1/prod-us-east",
      "namespace": "",
      "object": "ClusterRoleBinding/debug-binding",
      "title": "ClusterRoleBinding debug-binding grants cluster-admin to a non-system subject"
    }
  ],
  "context_repos": ["acme/terraform-live"],
  "declared_intent_repos": ["acme/fleet", "acme/terraform-live"],
  "declared_intent_searched": [],
  "declared_intent_sources": [],
  "declared_intent_unsearched": [],
  "declarations_path": "/opt/data/scratch/declarations_compliance-audit.json",
  "sop": "/opt/data/profiles/platform/governance/compliance_audit_sop.md",
  "checks": ["privileged-container", "host-namespace", "…"],
  "checks_contract": "Run every check above against every cluster you can read. …",
  "next_steps": [
    "Read the SOP at /opt/data/profiles/platform/governance/compliance_audit_sop.md in full, then …",
    "Write the findings document at /opt/data/scratch/findings_compliance-audit.json, starting it with …/scripts/audit_report.py draft --audit compliance-audit --manifest-file /opt/data/scratch/manifest_compliance-audit.json. Its rules are in …/references/findings-document.md.",
    "Run …/scripts/audit_report.py finish --audit compliance-audit --findings-file … --manifest-file /opt/data/scratch/manifest_compliance-audit.json. Its output and exit codes are in …/references/finish.md."
  ]
}
```

Write your findings to the `findings_path` it gives you. Do not pick your own path.

`next_steps` is the rest of the run in three lines: read the SOP and run its checks, write the
document, run `finish`. Each names the full path of the file that step needs, and the `finish` line
names the manifest path the SOP's collector writes.

`checks` is your stream's full roster, handed over so coverage never depends on how far into the SOP
you read. **It is the work list, not a substitute for the SOP** — the slug says which check, the SOP
says what the check _is_ and what counts as a violation, so read the whole file before you start.
`sop` names it.

`workspace` is where your manifests go. **Every `remediation.path` is resolved against it**, so a
manifest written anywhere else is a file the harness will never find — the finding degrades to a
manual one, or, for a fix the sweep would open, `finish` refuses until it is there. `start` scrubs that directory before handing it to you;
`finish` does not, which is what lets the files you write in between survive.

`mode` is `content`, `directory` or `none`. Between the first two it changes one thing you can see:
in `content` mode the workspace is empty rather than a checkout. Read it rather than guessing from
what is on disk. `none` means no forge is configured; see [No forge configured](finish.md#no-forge-configured).

`pending_remediation_requests` lists the findings a repository writer has already asked to be fixed,
parsed from the ledger's comments. **Write those manifests during inspection** — if the finding is
still reproducing at `finish`, its pull request opens immediately instead of a week later.

`carried` lists every finding the open ledger carries — its id, the check that found it, where it
is, and its title — read off the previous run's stored report, the body `finish` will compare your
document against. **These are the findings you are answering for.** For each one, this run ends one
of four ways: you report it again; you re-ran its check on that cluster, saw it gone, and say so
under [`resolved_because`](findings-document.md#the-findings-document) with the same `check`, `cluster`, `namespace` and
`object`; it is a posture now covered by a declaration and sits under `declared`; or you did not run
that check there and your `checks_run` does not claim you did. A run whose `checks_run` says the
check ran and whose document neither reports nor explains the finding is **held** — see [The clean
run](clean-run.md#the-clean-run). On a stream that passes `--manifest-file` there is a fifth ending for a
finding the collector still emits: `resolved_because` does not release it, and only the collector no
longer emitting it or a `declared` entry does. Where the store has never held the ledger, `start`
reads the issue's hidden block once instead. Empty when there is no open ledger, or the store's
record of it is missing, unreadable, for another issue, or lists ids that differ from the issue's
hidden block, or the issue listing returned no body to check it against, or the store never held it
and the issue has no readable block (`start` says so on stderr).

`context_repos` names the repositories registered for **declared intent**: the `context_repos` key
of `$GITOPS_STATE_CONFIGMAP`, added by an administrator by hand, as `owner/name` slugs. A stream
whose SOP has a declared-intent step (`obtainability-audit` §4a, `compliance-audit` §3a, `security-patch-orchestrator` §4a, `fleet-wide-cost-analysis` §3a) searches them before it
reports a posture as a finding. They are read and nothing else: the key is separate from
`managed_repos`, the harness never merges the two, so the broker's push gate, the repository
resolver and the sweep never see them. The list is empty when nothing is registered or the key
could not be read, which `start` says on stderr; the GitOps clone is searched either way. A private
context repository is readable through the broker's content-mode clone only (`inspect_repository.py
clone` and `open`): the broker mints a `contents: read` token for it per clone and installs it
nowhere. The sandbox's own CLI credential and the directory-mode clone cover managed repositories
alone.

`declared_intent_repos` is the set that step must account for: the GitOps repository plus every
`context_repos` slug, one entry each. `start` writes the same set to a run record beside the
findings document (`/opt/data/scratch/run_<audit-id>.json`), before it prints, and `finish` measures
the document's `declared_intent_searched` against that record rather than against the ConfigMap as
it stands at finish time. Every stream prints it; only a stream with a declared-intent step is held
to it.

`declared_intent_searched`, `declared_intent_sources` and `declarations_path` are the harness's own
half of that step, already done by the time `start` prints. On a stream with a declared-intent step,
`start` reads every repository in `declared_intent_repos` it can — each `context_repos` entry
through `inspect_repository.py clone` at the entry's `ref` when it has one and at its own default
branch otherwise (a base branch the operator configures applies only to a gitops or managed
repository, so a context repository is never read at it), the GitOps repository
from the clone it just reset or through the same script in content mode — for `declares:`
frontmatter in OKF notes, within the paths each repository's `.kube-agents/intent.yaml` names
(`declared_intent_sources` lists each as `{repo, ref, paths}`, `paths` empty when the whole tree
was read). In content mode the copy is bounded the same way: `.kube-agents/` first, then only the
named paths, so the sibling script's file and byte caps count notes rather than manifests; a
repository with no usable intent file (one naming a path the broker refuses included), or whose
file names a path with nothing behind it, is copied whole under those caps. `start` files what it found
at `declarations_path` and lists each repository it read completely — every note under the
searched paths arrived and was read; one the broker withheld or the harness could not decode costs
the repository its entry, a symlink there being no note in either mode — as `owner/name@sha`. `finish` unions that list into the document's and
moves every finding a filed declaration covers to `declared` itself. A slug in `declared_intent_repos` missing
from `declared_intent_searched` is one the harness could not read; `declared_intent_unsearched`
lists each as `{repo, ref}`, stderr says why, and the SOP says what the worker does about it — its
own copy at that `ref`. An entry whose `ref` failed the branch-name check carries the value under
`refused_ref` instead of a `ref`: the harness skipped that repository rather than reading its
default branch in the pin's place, the worker copies nothing either, and the ledger names it as
not searched until the entry is corrected. On every other stream the three lists are empty and the
file at `declarations_path` holds none.

### Step 2 — Inspect the fleet (reasoning phase)

Enumerate the clusters in scope and inspect them **read-only** (`kubectl get/describe`,
`gcloud ... describe/list`). For every deviation you intend to report, capture the exact command you
ran and the output that proves it.

Keep a per-cluster tally as you go: for each check in the roster `start` printed, the slug and **the
exact command you issued for it**, appended the moment that check completes. `finish` requires it as
`checks_run` and rejects a slug with no command. Reconstructing the tally afterwards from memory is
how a check that never ran gets recorded as one that did — and now that each entry carries a command
that gets published, reconstructing it from memory is also how you end up publishing a command you
never issued.

If a remediation is a declarative file, write that file **under the `workspace` directory `start`
reported** and name its repo-relative path in the finding. The harness puts it on a branch of its
own.

**Directory mode only: do not leave unrelated uncommitted work in that tree during an audit.**
Opening a remediation pull request there requires switching branches, and the harness forces the
switch. It snapshots and restores every path you declared, and returns you to the branch you started
on — but a file it was never told about is not covered by that guarantee. Content mode switches no
branch and writes nothing back into the workspace, so this does not apply.

#### Reading the repository in content mode

The clone is gone, so three commands stand in for it. `grep` searches inside the files, `list` names
them, and `fetch` copies the ones you name into the workspace:

```bash
/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py grep  --audit <audit-id> --pattern 'namespace: payments'
/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py list  --audit <audit-id> --prefix clusters/prod-us-east
/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py fetch --audit <audit-id> --path clusters/prod-us-east/payments-netpol.yaml
```

`grep` runs broker-side and answers with matching lines; it is a fixed string unless `--regex`, and
`--prefix` narrows it. Use it when what you know is what a file says. `list` answers with paths and
sizes, never content — use it when what you know is where the file lives. The broker caps what each
returns, so read `truncated` on both and **pass `--prefix`** on a large repository. `fetch` writes
each file into the workspace at its repo-relative path, which is exactly where a remediation editing
that file has to end up; fetch it, edit it in place, and name the same path in the finding.

The collectors do not need them. Handed the scratch workspace as `--workspace`, `collect.py` and
`fleet_waste.py` copy the repository's YAML out of the broker into a private directory and index that,
so candidates carry `declaration` and `release_declaration` (and, from `collect.py` alone,
`namespace_directory`) in content mode as they do from a clone. When a collector cannot read the
tree it logs a `WARNING` naming why and attaches none of those fields. Each file the broker will not
send (over its size limit, or a symlink) costs less: no `declaration` or `namespace_directory` on a
cluster whose `clusters/<name>/` tree holds it, and no `release_declaration` or `namespace_directory`
anywhere. The exception is a too-large non-Kustomization file holding a column-0 `kind` and, by
the broker's search, no release kind: it loses only its cluster's tree, if that tree holds no release
and no Kustomization an Application names. Either way the declaration rule's own search is the
answer for what is missing.

All three print `sha`, the commit of the tree the broker answered from. There is no `git` on this
side to ask, and the declared-intent record ([`declared_intent_searched`](findings-document.md#declared_intent_searched)) names each repository
as `owner/name@sha`; take the sha from the command whose answer you searched.

All three take `--branch`, and a second round needs it. Without it they answer from the base, so a
file the remediation branch has already changed — by an earlier run or by a reviewer — comes back as
the base has it, and committing the edit onto that branch reverts the change. The revert
fast-forwards, so nothing objects. Pass the remediation branch whenever the remote already has one;
a branch it does not have falls back to the base, which is what a first round wants anyway.

All three exit 2 in directory mode, where the clone already holds the file.

`audit_report.py draft --audit <audit-id> --manifest-file <manifest> [--out <path>]` starts the
findings document from the collector's manifest: `scope.clusters` with each collected target's
`checks_run` copied from the commands it ran, `scope.skipped` for every target it could not read, and
one finding per candidate carrying its evidence, impact and severity. It leaves each finding's
`recommendation` empty and its `remediation` `manual`, because those are the judgement the collector
cannot make: write them, and `finish` has nothing else to reject.
