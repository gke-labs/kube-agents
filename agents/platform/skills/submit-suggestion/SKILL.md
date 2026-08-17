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

### Step 1: Lease a Private Workspace and Branch

Never run `git` from wherever your shell happens to be. You share one volume with
every other agent in this pod — the fleet audits, the other kanban workers — and
a bare `git checkout` there lands inside a clone somebody else is mid-way
through. `prepare` hands you a clone that is yours alone:

```bash
./skills/submit-suggestion/scripts/submit_suggestion.py prepare \
  --branch "platform-agent/<change_type>-<target_id>"
```

_(Example: `platform-agent/provision-mercury-09` or `platform-agent/upgrade-policy-baseline`)_

It clones and refreshes the GitOps repository, takes the branch, and prints one
JSON line:

```json
{
  "workspace": "/opt/data/gitops/t_9f3c1e07/acme__fleet",
  "lease": "t_9f3c1e07",
  "branch": "platform-agent/provision-mercury-09",
  "base": "main",
  "repo": "acme/fleet",
  "started_from": "origin/main"
}
```

**Keep that whole line. Step 3 needs `workspace` and `lease` back.** The
credential proxy refuses `git add`, `commit`, `checkout`, `push` and every other
tree-mutating verb outside a leased workspace, so a command run anywhere else
comes back as a security refusal rather than quietly damaging another agent's
work.

`base` is the repository's own default branch, not a hardcoded `main` —
`started_from` records what the branch was actually cut from. When the branch
already exists on the remote (Step 5, addressing feedback on an open PR),
`started_from` is `origin/<branch>` and your commits land **on top of** the ones
already under review. When it does not, the branch is cut fresh from
`origin/<base>`.

### Step 2: Make and Commit the Changes

1.  Generate or edit the required declarative files **inside the returned
    `workspace`**.
2.  Stage and commit the changes locally following Conventional Commit standards. **CRITICAL SECURITY RULE:** You **must** explicitly stage only the targeted declarative manifest files you generated or modified. **Never use `git add .` or `git add -A`** to prevent committing transient debugging files, volatile local credentials, or workspace logs:
    ```bash
    cd <workspace>
    git add <file_path_1> <file_path_2>
    git commit -m "<conventional_commit_message>"
    ```
    _(Example: `git add config/manifest.yaml && git commit -m "feat(fleet): provision GKE operator for mercury-09"` or `git add policies/baseline.yaml && git commit -m "fix(policy): restrict baseline network policy ingress"`)_

### Step 3: Call the Secure Submit Suggestion Script

Invoke the same helper with `submit` to handle the GitHub App token exchange, git
credential configuration, branch push, and Pull Request creation. Pass **both**
the `workspace` and the `lease` from Step 1 — the script verifies the lease on
that tree is still yours and refuses outright if it belongs to another agent:

```bash
./skills/submit-suggestion/scripts/submit_suggestion.py submit \
  --workspace "<workspace>" \
  --lease "<lease>" \
  --branch "platform-agent/<change_type>-<target_id>" \
  --title "<pr_title>" \
  --body "This Pull Request was generated automatically by the **Platform Agent** control plane.

### 🚀 Functional Impact:
<detailed_markdown_bulleted_impact_description>

Please review the code diffs and merge this PR to trigger the GitOps CI/CD rollout!"
```

`--lease` is not optional bookkeeping. `prepare` and `submit` are separate
processes, and outside a kanban card there is no session identity for `submit`
to re-derive the lease from — so without it the script stops and tells you to
pass it, rather than inventing an id that could never match the workspace.

The script returns the clean, live GitHub PR URL. If a Pull Request for this
branch is already open, it updates that one's title and body in place and
returns its URL — resubmitting is not an error.

### Step 4: Confirm Suggestion

Record the PR link returned by the script, update the pending status inside your local state registry (if applicable), and present a clean, human-readable confirmation containing the PR URL link back to the user.

### Step 5: Addressing Review Feedback on an Existing PR

When you are asked to **address review comments / reviewer feedback** on an existing PR, **read the comments yourself — never expect them pasted into the task.** You have GitHub access via the minted, repo-scoped App token (cached into `gh` and the git credential store by `scripts/github_token_refresh.py`).

1. **Refresh auth** if a call is unauthorized: `./scripts/github_token_refresh.py`.
2. **Read the PR and all its feedback** — both the conversation and inline (diff) review comments:
   ```bash
   gh pr view <PR_NUMBER> --repo <owner/repo> --json title,url,headRefName,body,comments,reviews
   gh api repos/<owner/repo>/pulls/<PR_NUMBER>/comments   # inline review-thread comments
   ```
3. **Apply the requested changes on the PR's own branch.** Lease a workspace for
   that branch the same way Step 1 does — `prepare --branch <headRefName>` — and
   work inside the `workspace` it prints. Because the branch already exists on
   the remote, `prepare` bases it on `origin/<headRefName>`, so the commits
   already under review are still there and yours go on top;
   `started_from` in its JSON says which. Make the targeted edits, then —
   following the **same CRITICAL security rule as Step 2** — stage only the
   specific files (**never `git add .` / `-A`**), commit with a Conventional
   Commit message, and run `submit` with the same `--workspace`, `--lease` and
   `--branch <headRefName>` so the existing PR updates in place. `submit` pushes
   with `--force-with-lease`: it will update the branch your workspace fetched,
   and refuse rather than overwrite one somebody else has moved in the meantime.
4. **Reply on the PR** summarizing what changed (`gh pr comment <PR_NUMBER> --repo <owner/repo> --body "..."`), then relay a clean confirmation (PR URL + what you changed) back through your kanban result.

Never ask the requester to paste the comment text — fetching it from GitHub and addressing it is your job.
