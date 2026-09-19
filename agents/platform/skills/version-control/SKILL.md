---
name: version-control
description: Read and change a hosted repository through forge-neutral verbs. Version control here is abstracted — remote operations go through vcs.py, local operations use a credential-free VCS binary on a working copy that vcs.py puts on disk. Covers history, file modes, proposals and issues, so no forge CLI is needed.
---

# version-control - remote operations through vcs.py, local operations with git

Version control here is **abstracted**. Everything is one of two things, and
getting an operation into the right half is most of using this skill.

**Remote operations — anything that crosses the network or spends a
credential.** These go through `scripts/vcs.py`, which speaks to a broker in
another container. Only the broker holds the credential; nothing here does.
The remote verbs are exactly these:

`capabilities`, `identity`, `clone`, `publish`,
`proposal create|list|view|comment|update|close|commits|acknowledge`,
`issue create|list|view|comment|update|close`, `label ensure`.

`update` edits a title or body and adds or removes labels; `close` closes;
`commits` lists the revisions on a proposal's source branch; `acknowledge`
reacts to a comment (its `id` and `kind` come from `view --comments`) so its
author sees it was read — `capabilities` says whether this forge supports it.
`issue list --query <text>` searches, and `--without-labels` skips issues
carrying any of them — that is how a queue is read for unclaimed work.
`label ensure` creates a label or updates it if it exists. `identity` says
who this install is on the forge and, with `--login`, whether that login may
write to the repository.

**Local operations — everything else.** `clone` unpacks a real working copy
onto this filesystem and prints its `path`. Inside it, use the local git, which
is `/opt/vcs/libexec/git`. Call it by that path, or through a variable you
export once — `export G=/opt/vcs/libexec/git`, then `$G log`, `$G show`,
`$G blame`, `$G grep`, `$G diff`, `$G status`, `$G branch`, `$G commit`,
`$G ls-files --stage` — and every read works as you expect. Do **not** define a
shell alias for it: each command you run here arrives in a fresh
non-interactive shell, which never expands aliases, so an aliased `git`
followed by `git log` silently runs the other program. An exported variable
survives from one command to the next; an alias does not. Read files in the
working copy with `cat`, `rg`, or anything else. You do not need `vcs.py` for
any of this and it is faster without it.

The full path matters: plain `git` on this machine is a different program that
runs elsewhere and holds a credential. The one named above holds none and
cannot reach a forge: its HTTP transport helpers are not in the image, so an
`https://` URL fails with `'remote-https' is not a git command`, and there is
no ssh client, so an `ssh://` URL fails with `cannot run ssh`. Seeing either
message means you used the right git and asked it for the one thing it does not
do; the answer is a `vcs.py` verb, not the other binary.

`vcs.py` also offers `log`, `show`, `annotate`, `files`, `grep`, `diff`,
`status`, `branch` and `commit` as thin wrappers over that same local git, for
when you want their JSON output. They make no network call. Use whichever you
prefer; the wrapper is a convenience, not the sanctioned path.

The reason for the split is that the history is here rather than there. `clone`
brings a repository down as a git bundle, so the full object graph — every
commit, every parent, every tree entry with its mode — is on this disk. Your
revisions go **up** the same way, as a bundle, which is why a branch of five
commits arrives as five commits. Nothing that came out of the repository is
ever executed beside the credential.

The script is `scripts/vcs.py`. Every subcommand prints one JSON object on
stdout. `--repo` takes `owner/name` or a full URL, and the broker decides which
forge that is.

Verb names are the version-control concept; the spelling you know is an alias.
`annotate`/`blame`, `log`/`history`, `files`/`manifest`, `grep`/`search`,
`publish`/`push`, `proposal`/`pr`/`mr`, `create`/`open` all work.

## When to Use

- **Anything about the past.** When a value changed, which revision removed a
  flag, who last touched a file, what a file looked like three revisions ago.
- **File modes.** Whether a script is executable is a tree-entry property;
  `files` reports it and it survives the round trip.
- **Changing a repository** and opening the change proposal for it.
- **Issues and proposals.** Use `issue` and `proposal` rather than a forge CLI.
- **A repository on a forge that is not GitHub.** Run `capabilities` first — it
  answers with what this install can and cannot do for that host, and it spends
  no credential doing it.

## When NOT to Use

- **A repository this install does not manage.** Every verb that spends the
  credential, `clone` and the list/view verbs included, is refused for a
  repository outside the install's managed list, because the credential is
  minted per repository; only `capabilities` answers for one. For "how does
  upstream implement this", use **inspect-repository**.
- **A one-off read of a large repository.** `clone` pulls a whole branch's
  history and there is no shallow option; **inspect-repository** pages a
  shallow view and is cheaper.
- **The GitOps write flow that already gave you a workspace.** `fleet-audit`
  and `submit-suggestion` own theirs; do not open a second view.

## Read

One remote call, then local work:

```bash
V="$HERMES_HOME"/skills/version-control/scripts/vcs.py
# Remote: one call, one bundle. Its JSON carries `path` — cd there.
python3 $V clone https://github.com/acme/infra

# Local, in that working copy. No network, no credential, no vcs.py.
# The path is the point: bare `git` is a different, credentialed program,
# and an alias would not survive to your next command here.
export G=/opt/vcs/libexec/git
$G log      -n 20 -- inventory/clusters.yaml
$G show     HEAD~3:inventory/clusters.yaml
$G blame    scripts/rotate-keys.sh
$G ls-files --stage
$G grep     'nodeCount:'
$G status
```

The same reads through `vcs.py`, if you want JSON instead:

```bash
python3 $V log      -n 20 -- inventory/clusters.yaml
python3 $V show     HEAD~3:inventory/clusters.yaml
python3 $V annotate scripts/rotate-keys.sh
python3 $V files
python3 $V grep     'nodeCount:'
python3 $V status
```

`clone` prints the working copy's `path`. Read files in it with ordinary tools.
Every `vcs.py` verb after the first infers the repository from the only copy
there is, or from the directory you are standing in; `--repo` says which when
there are several.

One repository can be cloned more than once here — the copy is named for its
branch as well as its repository, so a card working a _different_ branch of the
same repository gets a copy of its own rather than yours. That is naming, not a
permission boundary, and it does not separate two cards reading the _same_
branch: with no `--branch` every reader asks for the trunk and every reader
lands on the one directory. The second `clone` replaces what is there, and
refuses to when that copy holds work that was never published. When two copies
of one repository exist, `--repo` no longer picks between them: **run the verb from inside the
copy you mean.** The refusal names the paths.

## Write

```bash
python3 $V branch  fix/replicas
# edit files under the path `clone` printed, then:
python3 $V commit  inventory/clusters.yaml -m 'raise replicas to 5'
python3 $V publish
python3 $V proposal create --title 'Raise replicas' \
                           --body 'Rollout headroom for the evening peak.'
python3 $V discard
```

`branch` and `commit` are local and make no network call. `publish` sends every
revision made since the clone, and the identifiers `log` printed here are the
identifiers that land on the forge.

**Name the paths you mean.** `commit` with no paths records changes to files
the copy already tracks and nothing else — it is never `git add .`. A file the
copy has never seen is refused by name, because the working copy is also where
your scratch output lands and a log swept into a public proposal cannot be
taken back. Name it on the `commit` line to include it.

## Collaborate

```bash
python3 $V issue list --state open --labels bug
python3 $V issue list --state open --without-labels status:in-progress
python3 $V issue view 42 --comments
python3 $V issue create --title 'Cluster drift on prod-eu' --body '...'
python3 $V proposal list
python3 $V proposal view 17 --comments --diff
python3 $V proposal comment 17 --body 'Rebased on main.'
```

## Rules

- **`clone` before any other verb.** The read verbs answer from the local copy
  and say so when there is not one. The collaboration verbs do not need one if
  you pass `--repo`.
- **Do not reach for `gh`, even though it answers.** A forge CLI is reachable
  on this machine and it is not the sanctioned path: it answers a
  forge-neutral question in one forge's dialect, and the same request against
  the next forge this install adds would have to be written again. Nothing this
  skill cannot do becomes possible through it. A verb you need and cannot find
  is a gap worth reporting, not a reason to go around.
- **Do not `git push`, `git fetch`, `git clone` or `git remote add`.** The
  working copy has no remote on purpose, and the local git cannot speak the wire
  protocol in any case. Revisions go up through `publish` and come down through
  `clone`. Local git is for reading and committing, nothing else.
- **Start a branch before you commit.** `clone` leaves you on the shared branch,
  and publishing that branch is refused: revisions reach a repository through a
  branch of your own and a proposal onto the shared one. `vcs.py branch <name>`
  or `git switch -c <name>` (the local git) before the first commit.
- **`publish` can be refused, and the refusal is the answer.**
  `NOT_FAST_FORWARD` and `BRANCH_DIVERGED` mean your revisions do not build on
  what the remote has; `BASE_MOVED` means the target branch was rewritten, so
  the revision you cloned at is not on it any more and there is nothing to build
  on — clone again and reapply the change. An ordinary push to the target by
  somebody else is _not_ refused: your proposal simply opens with a base behind
  the tip, which is a rebase on the forge and not a problem here. Do not try to
  force any of them.
- **A forge refusal names the code and the next move; do what it says.**
  `FORGE_RATE_LIMITED` means wait and then use fewer, wider calls.
  `FORGE_UNAUTHENTICATED`, `FORGE_FORBIDDEN` and `FORGE_REJECTED` will answer
  the same way however many times you repeat the call — report or fix the
  argument instead. `FORGE_UNAVAILABLE` means retry unchanged in a few minutes.
  `FORGE_NOT_FOUND` does not prove the thing is missing: a private repository
  this install cannot see answers the same way.
- **`discard` when finished.** It removes the local copy. Nothing is held on the
  credential side, so there is nothing else to release.
- **Read `exitCode` and `stderr`.** The read verbs pass git's own exit status
  through; a `log` that returned nothing because the pathspec matched no file is
  not the same answer as one that returned nothing because the file has no
  history.
- **Say which repository and which branch** in anything you report.
- **`capabilities` before assuming a non-GitHub forge works.** GitLab and
  Bitbucket parse their specs and then tell you exactly what this install is
  missing. That is the answer, not a bug to work around.

## Reference

| Subcommand             | What it does                                                                                                  |
| ---------------------- | ------------------------------------------------------------------------------------------------------------- |
| `capabilities`         | What this install can do for this repository's forge, before anything is spent                                |
| `clone`                | The history down as a bundle, unpacked into a local working copy; `--branch` for one line                     |
| `log`                  | The revisions behind HEAD; `--patch` for diffs, `--format` a pretty format string, trailing args a pathspec   |
| `show`                 | One revision, or `revision:path` for a file as of that revision                                               |
| `diff`                 | Differences in the working copy, or against `--revision`                                                      |
| `annotate`             | Per-line last-change attribution for one path                                                                 |
| `files`                | Tracked paths with the mode the revision records                                                              |
| `grep`                 | Text search over the working copy; `--regex`, `--ignore-case`                                                 |
| `status`               | What the working copy has that its revision does not                                                          |
| `branch`               | List lines of development, or start one. Local                                                                |
| `commit`               | Record a revision locally, with a real parent and identifier. Paths, or tracked changes only                  |
| `publish`              | Send the revisions made since `clone` to the shared repository                                                |
| `discard`              | Remove the local copy                                                                                         |
| `proposal create`      | Open the forge's change proposal (pull request, merge request)                                                |
| `proposal list`        | Open proposals; `--state open\|closed\|all`, `--source`/`--target` to ask about one branch                    |
| `proposal view`        | One proposal; `--comments` for the discussion, `--diff` for the patch                                         |
| `proposal comment`     | Reply on a proposal                                                                                           |
| `proposal update`      | Retitle, rewrite the body, `--add-label`/`--remove-label`                                                     |
| `proposal close`       | Close it without merging                                                                                      |
| `proposal commits`     | The revisions on its source branch, **oldest first** — the last entry is the newest                           |
| `proposal acknowledge` | React to one comment so its author sees it was read; needs `--comment-id` and `--kind` from `view --comments` |
| `issue list`           | Work items; `--state`, `--labels`, `--without-labels`, `--query`                                              |
| `issue view`           | One issue; `--comments` for the discussion                                                                    |
| `issue create`         | Open an issue; `--labels`                                                                                     |
| `issue comment`        | Reply on an issue                                                                                             |
| `issue update`         | Retitle, rewrite the body, `--add-label`/`--remove-label`                                                     |
| `issue close`          | Close it; `--reason completed\|not-planned`                                                                   |
| `label ensure`         | Make the label exist, or update its `--color`/`--description` if it already does                              |
| `identity`             | Who this install is on this forge; `--login` asks whether that account may write here                         |

Every listing verb takes `-n/--limit` and answers with `count` and `truncated`.
`truncated` is the forge's word for "there was more", judged on what it sent
rather than on what survived filtering — so `count: 0` with `truncated: true` is
a real answer and means ask again, more narrowly.

`proposal view --comments` and `issue view --comments` say the same thing about
the conversation they read, as `commentCount` and `commentsTruncated`. Take
`commentsTruncated: true` seriously before you reply to anything: it means you
are looking at the oldest page of a longer thread, so the most recent word on
the subject — including an answer somebody already gave — is not in front of
you. There is no way to read the rest from here: `-n` can only make the page
smaller, and it is the same oldest page either way. Say that you could not read
the whole thread, and do not answer anything that turns on what the rest of it
says. It is the one truncation where carrying on quietly
produces a confidently wrong answer rather than an incomplete one.
