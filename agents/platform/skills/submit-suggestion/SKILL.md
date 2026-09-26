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

Never run the plain `git` on this machine and never work from wherever your
shell happens to start. That `git` is a different program: it reaches the
network with a credential, and it is not the one your working copy answers to.
`prepare` brings the repository down and stands you on the branch this change
goes on.

The script path is spelled out from `$HERMES_HOME` rather than as `./skills/…`
because this skill is reached from a kanban card as well as from a cron turn,
and a card dispatch starts you in the task's workspace, not the profile
directory. `$HERMES_HOME` is the profile directory in both. Use that form
everywhere below, including for `vcs.py` in Step 5.

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

It prints one JSON line. **Keep it — Step 2 works inside its `workspace`.** The
`workspace` is named for your branch as well as the repository, because
`/opt/data/scratch` is shared with every other card: another card suggesting a
change to the same repository right now derives a different branch name for it,
so it works in a different directory. Different, not protected — every card here
runs as the same account and the whole tree is readable and writable from all of
them. What keeps two suggestions apart is that no two of them share a branch
name, which is why the name has to describe the change.

```json
{
  "workspace": "/opt/data/scratch/vcs/github__acme__fleet__platform-agent__provision-mercury-09",
  "repo": "acme/fleet",
  "branch": "platform-agent/provision-mercury-09",
  "base": "main",
  "started_from": "main",
  "proposal": ""
}
```

`base` is what the change merges into: the repository's own default branch, not
a hardcoded `main` — or, when a pull request for this branch is already open,
whatever that one is already targeting.

`started_from` and `proposal` are two halves of one answer — whether this is new
work or another round on a change already under review. `prepare` asks the forge
rather than taking your word for it. When a pull request is open for the branch,
its URL is in `proposal`, the copy is taken **of that branch** so every reviewed
revision comes down with it, and `started_from` is the branch itself. Otherwise
`proposal` is empty, the copy is taken of the base, and `started_from` is the
base.

There is one working copy per repository **and branch**, so preparing a second
change to the same repository does not disturb the first. If the copy for this
branch holds revisions that were never published, `prepare` refuses to replace
it. Finishing them is the way past, not the flag: go to the copy the refusal
names, publish what is in it and open the proposal, and this `prepare` becomes a
second round on that branch instead of a replacement. `--force` deletes those
revisions; it is the answer only once you have read them and decided they should
not exist.

`prepare` refuses the name for a second reason: the proposal it was last used
for is closed, and its revisions are not in the history you just cloned. That is
what a squash merge leaves behind — the change is in the trunk under a different
revision, the branch is still on the forge, and building on it again would
re-propose work that has already landed. Choose a different name; the derived
one is a default, not a requirement.

That refusal holds only while the forge still has the branch, and many
repositories delete it as they merge it. Nothing here can tell the two apart:
no read verb reports whether a branch exists, so a name whose branch is gone
looks exactly like a name whose branch is in the way. If you know the
repository deletes merged branches, `--allow-reused-branch` says so and
`prepare` proceeds on the name. It is a claim, not a check — get it wrong and
`submit` is refused as `BRANCH_DIVERGED` at the end of the turn, after the
change is written.

### Step 2: Make the Changes

Generate or edit the files **inside the returned `workspace`**.

The local version control binary is `/opt/vcs/libexec/git`. It holds no
credential and cannot reach a forge, which is exactly why it is the one to use
on the working copy. Export it once and call it through the variable:

```bash
export G=/opt/vcs/libexec/git
cd <workspace>
# create or edit the declarative files here
$G add <file_path_1> <file_path_2>
$G commit -m "<conventional_commit_message>"
```

Do **not** define a shell alias for it. Each command you run arrives in a fresh
non-interactive shell, which never expands aliases, so an aliased `git`
followed by `git commit` silently runs the credentialed program instead.

**CRITICAL SECURITY RULE:** explicitly stage only the targeted declarative files
you generated or modified. **Never use `git add .` or `git add -A`** — this is a
real clone on a filesystem you also scratch in, and a blanket add sweeps
transient debugging output, logs and anything else that landed there into a
public pull request.

_(Example: `$G add config/manifest.yaml && $G commit -m "feat(fleet): provision GKE operator for mercury-09"`)_

Committing here is optional. Uncommitted changes **to files the copy already
tracks** are recorded as a single revision under the `--title` you pass when you
run Step 3, which is what a single-purpose change wants. Commit yourself when
the change deserves more than one revision, or a message that is not the pull
request's headline.

The rule above still holds at Step 3: a file the copy has never seen is not
swept in for you. `submit` refuses and names it, because it cannot tell a
manifest you generated from a log you left behind. Stage the ones that belong
(`$G add <path>`) and delete the rest.

### Step 3: Call the Secure Submit Suggestion Script

The same helper with `submit` publishes the branch and opens the pull request —
or updates the one already open. It finds the working copy Step 1 made, so the
only thing to pass back is the branch name.

Write the description to a file first and pass the path. Inside double quotes
bash expands backticks and `$(...)`, and a pull request body is full of
backticks — through `--body` a benign one silently deletes its own text and a
hostile one runs in the working copy you are about to publish:

```bash
BODY=$(mktemp -p /opt/data/scratch pr_body.XXXXXX.md)
cat > "$BODY" <<'EOF'
This Pull Request was generated automatically by the **Platform Agent** control plane.

### 🚀 Functional Impact:
<detailed_markdown_bulleted_impact_description>

Please review the code diffs and merge this PR to trigger the GitOps CI/CD rollout!
EOF
```

The quoted `<<'EOF'` matters as much as `--body-file`: unquoted, the heredoc
expands the same constructs the argument would have. `mktemp` matters because
`/opt/data/scratch` is shared with every other card running right now, and a
fixed name there is two cards writing one file — one card's description on the
other's pull request. Keep the file directly in `/opt/data/scratch`, the only
directory `--body-file` reads from, and **not** inside the `workspace`: a file
written there is untracked, and `commit` refuses rather than guess — it names
the file and tells you to name the paths that belong in the change — so a
description left in the working copy stops the submit instead of riding along
in it.

```bash
python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py submit \
  --repo "<owner>/<repo>" \
  --branch "platform-agent/<change_type>-<target_id>" \
  --title "<pr_title>" \
  --body-file "$BODY"
```

`--base <branch>` names what the change merges into, for the rare case where it
is not the `base` Step 1 reported. It may not name the branch you are
submitting: a head branch that is its own base carries nothing for anyone to
review, and `prepare`, `submit` and the broker each refuse it. That covers the
repository whose trunk is called something other than `main` — the name is read
from the remote, not from a list.

The script returns the clean, live pull request URL. If a pull request for this
branch is already open, it updates that one's title and body in place and
returns its URL — resubmitting is not an error. `--keep-description` (Step 5) is
the one exception: it leaves the open pull request's title and body as their
author wrote them.

Older invocations carried `--workspace`, `--lease`, `--handle`, `--from`,
`--delete` and `--base-sha`, and had `list` and `fetch` as commands of their
own. The flags are still accepted, and read and ignored with a line saying so;
the two commands refuse with a line saying where the files are now. That is all
any of it buys: a command written against the old shape fails on what is
actually wrong with it — there is no working copy here, take the branch with
`prepare` — rather than on "unrecognized arguments" or "invalid choice", which
say nothing and hide the real cause. A card that prepared before an upgrade
cannot submit after one; its clone was on a volume this script no longer has.
Prepare again. Do not write new commands with these flags.

### Step 4: Confirm Suggestion

Record the PR link returned by the script, update the pending status inside your local state registry (if applicable), and present a clean, human-readable confirmation containing the PR URL link back to the user.

### Step 5: Addressing Review Feedback on an Existing PR

When you are asked to **address review comments / reviewer feedback** on an
existing PR, **read the comments yourself — never expect them pasted into the
task.** The `version-control` skill reads and writes them through the same
broker this skill publishes through; there is no forge CLI to authenticate and
no token to refresh.

1. **Read the pull request and all its feedback** — the conversation and the
   inline review comments arrive together, each carrying the file and line it
   sits on where it has one:

   ```bash
   V="$HERMES_HOME"/skills/version-control/scripts/vcs.py
   python3 "$V" proposal view <PR_NUMBER> --repo "<owner>/<repo>" --comments
   ```

   Add `--diff` for the change under discussion. The `source` field of that
   answer is the branch to work on in the next step.

2. **Apply the requested changes on the PR's own branch.** Run Step 1 against
   that branch — `prepare --repo "<owner>/<repo>" --branch <source>` — then
   Steps 2 and 3 exactly as written. Two things differ from a first submission
   and both are handled for you: the copy comes down with the revisions already
   under review on it and yours go on top, and `submit` adds to the branch
   rather than replacing it, refusing outright if somebody else has moved it in
   the meantime.

   Edit the files the reviewer commented on in the `workspace` — what is on the
   branch is what you are being asked to change, and rewriting a file from
   memory loses the rest of it. Stage only those specific files
   (**never `git add .` / `-A`**).

   Pass `--title` and `--body-file` again so the description matches the
   revisions now on the branch — or `--keep-description` and no body-file, when
   what you were asked for does not alter what the pull request is for. That
   flag keeps the title along with the body, so a `--title` passed beside it
   does **not** reach the pull request, and the run says so. It is not inert,
   though: it is still the message any uncommitted edits are recorded under, so
   pass the one you would want on that revision and not a placeholder. Pass one
   whenever you left edits uncommitted at Step 2 — without it the run is refused
   for having changes and no message to record them under. `--keep-description`
   also needs the pull request to still be open: a merged or closed one is not a
   description to keep, and the script refuses before publishing anything rather
   than opening a fresh pull request with no description at all.

3. **Reply on the PR** summarizing what changed, then relay a clean
   confirmation (PR URL + what you changed) back through your kanban result.

   Through a file, for the reason Step 3 gives. A reply is a summary of edits
   you just made and often quotes the comment you are answering — backticks in
   your own text, and `$(...)` in somebody else's. `proposal comment` takes no
   `--body-file`, so the file is read by a command substitution you write
   yourself; what it expands to is an argument and is not expanded again.

   ```bash
   REPLY=$(mktemp -p /opt/data/scratch pr_reply.XXXXXX.md)
   cat > "$REPLY" <<'EOF'
   <what changed>
   EOF
   python3 "$V" proposal comment <PR_NUMBER> --repo "<owner>/<repo>" --body "$(cat "$REPLY")"
   ```

Never ask the requester to paste the comment text — fetching it and addressing it is your job.
