---
name: submit-suggestion
description: Propose declarative configuration updates securely by committing file changes and submitting GitHub Pull Requests (PRs) for SRE review. Not for fleet-audit finding fixes — the fleet-audit skill opens and tracks those PRs itself.
---

# submit-suggestion - Secure GitOps Pull Request Orchestrator

This skill equips the Platform Agent to propose declarative file updates, GKE infrastructure adjustments, or configuration changes securely by committing local repository changes and submitting GitHub Pull Requests (PRs) for human review.

## When to Use

- **Declarative File Provisioning:** Triggered when new GKE manifests or configs are requested.
- **Configuration Upgrades:** Triggered when upgrading version configurations, security patches, or network policies.
- **Governance Policy Syncs:** Triggered when compliance playbooks or settings require updates.

_Crucially, you are strictly forbidden from executing direct, manual mutations. All changes must flow through a secure PR path — this skill, or the **fleet-audit** skill for fixes of its findings (below)._

## When NOT to Use

- **Fixing a fleet-audit finding.** The bullets above match audit fixes too — a
  security patch, a policy update — which is exactly why this warning exists. If
  the change addresses a fleet-audit finding (it carries a finding id, or an
  `[audit]` ledger issue lists that exact deviation as a finding), the
  **fleet-audit** skill opens that pull request itself, through its `remediate`
  subcommand — see `skills/fleet-audit/SKILL.md` for the invocation. That path
  keys the branch on the files the fix touches, so a rerun cannot open a
  duplicate: a live PR is left untouched rather than force-pushed over, and one
  the harness withdrew as stale is re-proposed on the same branch. It applies
  the audit labels, links the ledger, and closes the PR when the finding stops
  reproducing. A PR opened through _this_ skill gets none of that — nothing
  dedupes it and nothing ever closes it, which is how one workload's findings
  once became five near-duplicate PRs.

  Two cautions when you take that path. `remediate` consumes the findings
  document the audit run wrote (`start` prints its path); if it no longer
  exists, stop and say the fix should be requested as `/remediate <finding-id>`
  on the ledger — do **not** run `start` yourself to mint a fresh document (it
  scrubs that stream's workspace, possibly under a scheduled run), and never
  hand-write one. And a change a user asked for on its own terms is not an
  audit fix, even when the same file appears in a ledger — this section is
  about fixes _of findings_, not about files findings happen to mention.

## Execution Instructions

Follow these steps to make, commit, and submit your GitOps suggestions asynchronously:

### Step 1: Prepare

Never run `git` from wherever your shell happens to be. You share one volume with
every other agent in this pod — the fleet audits, the other kanban workers — and
a bare `git checkout` there lands inside a clone somebody else is mid-way
through. `prepare` gives you somewhere of your own to work.

The script path is spelled out from `$HERMES_HOME` rather than as `./skills/…`
because this skill is reached from a kanban card as well as from a cron turn,
and a card dispatch starts you in the task's workspace, not the profile
directory. `$HERMES_HOME` is the profile directory in both. Use that form
everywhere below, including for `github_token_refresh.py` in Step 5.

If you do meet a `No such file or directory` on one of these scripts, do **not**
recover by writing the absolute path out: `/opt/data/profiles/platform/…` is
refused by the gateway lifecycle guard, under an error about restarting the
gateway that has nothing to do with what you ran. Observed live — the refusal
sent one worker on to report a change it had not made.

```bash
python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \
  --repo "<owner>/<repo>" \
  --branch "platform-agent/<change_type>-<target_id>"
```

_(Example: `--repo "acme/fleet" --branch "platform-agent/provision-mercury-09"` or `--repo "acme/fleet" --branch "platform-agent/upgrade-policy-baseline"`)_

In a multi-repository environment, pass `--repo "<owner>/<repo>"` for the repository your task targets (identified from cluster annotations or task context per SOUL.md §3.4).

It refreshes credentials, opens the GitOps repository, and prints one JSON line.
**Keep that whole line — Step 3 needs it back.** Its `mode` field says which of
two ways Steps 2 and 3 work, and you follow that field rather than choosing:

**`"mode": "content"`** — the repository is checked out on the credential
broker's side, where you have no path to it. There is no `.git` for you to touch
and no `git` for you to run.

```json
{
  "mode": "content",
  "handle": "4f1c…",
  "branch": "platform-agent/provision-mercury-09",
  "base": "main",
  "baseSha": "9a3d…",
  "repo": "acme/fleet"
}
```

**`"mode": "directory"`** — a clone leased to you alone on the shared volume.

```json
{
  "mode": "directory",
  "workspace": "/opt/data/gitops/t_9f3c1e07/acme__fleet",
  "lease": "t_9f3c1e07",
  "branch": "platform-agent/provision-mercury-09",
  "base": "main",
  "repo": "acme/fleet",
  "started_from": "origin/main"
}
```

In directory mode the credential proxy refuses `git add`, `commit`, `checkout`,
`push` and every other tree-mutating verb outside a leased workspace, so a
command run anywhere else comes back as a security refusal rather than quietly
damaging another agent's work.

`base` is the repository's own default branch, not a hardcoded `main`. In
directory mode `started_from` records what the branch was actually cut from:
when the branch already exists on the remote (Step 5, addressing feedback on an
open PR) it is `origin/<branch>` and your commits land **on top of** the ones
already under review; when it does not, the branch is cut fresh from
`origin/<base>`. Content mode always commits onto `origin/<base>` and carries the
earlier commits by pushing on top of the same branch.

### Step 2: Make the Changes

**Content mode.** Work in a scratch directory of your own — `mktemp -d` is
fine — laid out the way the repository is. A file at `<scratch>/policies/baseline.yaml`
becomes `policies/baseline.yaml` in the commit.

Editing a file that already exists means fetching it first; there is no checkout
here to `cat`. Fetch into the same scratch directory you will submit from:

```bash
S="$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py
SCRATCH=$(mktemp -d)
"$S" list --handle "<handle>" --prefix policies      # what is there
"$S" fetch --handle "<handle>" --path policies/baseline.yaml --to "$SCRATCH"
# edit "$SCRATCH"/policies/baseline.yaml, and write any new files under $SCRATCH
```

**CRITICAL SECURITY RULE:** the scratch directory _is_ the change set — there is
no staging step and nothing to un-stage. Put only the declarative files you mean
to propose in it. Never point `--from` at a directory holding transient
debugging output, local credentials or logs, and never at a directory you did
not create for this purpose. Symlinks in it are skipped rather than followed.

**Directory mode.** Generate or edit the files **inside the returned
`workspace`**, then stage and commit following Conventional Commit standards.
**CRITICAL SECURITY RULE:** explicitly stage only the targeted declarative files
you generated or modified. **Never use `git add .` or `git add -A`** — the same
reason as above.

```bash
cd <workspace>
git add <file_path_1> <file_path_2>
git commit -m "<conventional_commit_message>"
```

_(Example: `git add config/manifest.yaml && git commit -m "feat(fleet): provision GKE operator for mercury-09"`)_

### Step 3: Call the Secure Submit Suggestion Script

The same helper with `submit` handles the GitHub App token exchange, git
credential configuration, the push, and Pull Request creation. Pass back what
Step 1 printed for the mode you are in.

**Content mode** — the `handle`, the scratch directory, and the `baseSha`:

```bash
"$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py submit \
  --handle "<handle>" \
  --from "$SCRATCH" \
  --base-sha "<baseSha>" \
  --branch "platform-agent/<change_type>-<target_id>" \
  --title "<pr_title>" \
  --body "<pr_body>"
```

Add `--delete <path>` (repeatable) to remove a file the repository has.
`--base-sha` is what makes the broker refuse rather than overwrite when somebody
else changed one of these same files while you were working; without it the last
writer wins. Drop it only when you are deliberately replacing whatever is there.

**Directory mode** — the `workspace` and the `lease`, which the script checks is
still yours and refuses outright if it belongs to another agent:

```bash
python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py submit \
  --workspace "<workspace>" \
  --lease "<lease>" \
  --branch "platform-agent/<change_type>-<target_id>" \
  --title "<pr_title>" \
  --body "<pr_body>"
```

For `--body`, use a description of this shape:

```
This Pull Request was generated automatically by the **Platform Agent** control plane.

### 🚀 Functional Impact:
<detailed_markdown_bulleted_impact_description>

Please review the code diffs and merge this PR to trigger the GitOps CI/CD rollout!
```

`--lease` is not optional bookkeeping. `prepare` and `submit` are separate
processes, and outside a kanban card there is no session identity for `submit`
to re-derive the lease from — so without it the script stops and tells you to
pass it, rather than inventing an id that could never match the workspace.

Finish in the mode you prepared in. A session that got a `handle` has no leased
directory to fall back to, and one that got a `workspace` has no handle to
present.

The script returns the clean, live GitHub PR URL. If a Pull Request for this
branch is already open, it updates that one's title and body in place and
returns its URL — resubmitting is not an error.

### Step 4: Confirm Suggestion

Record the PR link returned by the script, update the pending status inside your local state registry (if applicable), and present a clean, human-readable confirmation containing the PR URL link back to the user.

### Step 5: Addressing Review Feedback on an Existing PR

When you are asked to **address review comments / reviewer feedback** on an existing PR, **read the comments yourself — never expect them pasted into the task.** You have GitHub access via the minted, repo-scoped App token (cached into `gh` and the git credential store by `scripts/github_token_refresh.py`).

1. **Refresh auth** if a call is unauthorized: `python3 "$HERMES_HOME"/scripts/github_token_refresh.py <owner/repo>`.
2. **Read the PR and all its feedback** — both the conversation and inline (diff) review comments:
   ```bash
   gh pr view <PR_NUMBER> --repo <owner/repo> --json title,url,headRefName,body,comments,reviews
   gh api repos/<owner/repo>/pulls/<PR_NUMBER>/comments   # inline review-thread comments
   ```
3. **Apply the requested changes on the PR's own branch.** Run Step 1 against
   that branch — `prepare --repo "<owner>/<repo>" --branch <headRefName>` — and
   follow the `mode` it prints, exactly as Steps 2 and 3 describe. Two things differ from a first
   submission, and both are handled for you: the commits already under review
   stay on the branch and yours go on top, and `submit` pushes with
   `--force-with-lease`, so it updates the branch it fetched and refuses rather
   than overwrites one somebody else has moved in the meantime.

   In content mode, `fetch` the files the reviewer commented on into your
   scratch directory before editing them — what is on the branch is what you are
   being asked to change, and rewriting it from memory loses the rest of the
   file. Directory mode is unchanged: edit in the `workspace`, stage only the
   specific files (**never `git add .` / `-A`**), and commit.

4. **Reply on the PR** summarizing what changed (`gh pr comment <PR_NUMBER> --repo <owner/repo> --body "..."`), then relay a clean confirmation (PR URL + what you changed) back through your kanban result.

Never ask the requester to paste the comment text — fetching it from GitHub and addressing it is your job.
