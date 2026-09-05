---
name: inspect-repository
description: Read and analyze the source of any GitHub repository — public or one this install has a token for — without a local checkout. Clones broker-side and pulls file content back; use it to answer questions about code, not to change it.
---

# inspect-repository - Read a repository this pod has no checkout of

This pod has no `git` of its own and no `.git` anywhere it can write. To read
somebody else's code, ask the credential broker to clone it and pull the file
content back. What lands locally is source without a repository around it: files
you can open, grep and reason about, and no `.git/config` for anything to
execute out of.

The script is `./skills/inspect-repository/scripts/inspect_repository.py`. Every subcommand prints one JSON
object on stdout.

## When to Use

- **Answering a question about code you do not have.** "How does upstream
  implement this controller?", "which release added this flag?"
- **Reading a dependency or an upstream project** named in an issue, a design
  doc, or a user's question.
- **Reading the GitOps repository for context** when you are not editing it.

## When NOT to Use

- **Changing a repository.** Use **submit-suggestion** (a pull request against
  the GitOps repo) or **fleet-audit** (fixes for its own findings). This skill
  opens read-only workspaces and the broker refuses to commit from one.
- **Reading a file the GitOps workflow already handed you.** `fleet-audit` and
  `submit-suggestion` give you a workspace or a handle of their own; use theirs
  rather than opening a second view of the same repository.

## Two shapes, and which to pick

**Copy the tree** when the repository is small enough to read on disk:

```bash
python3 ./skills/inspect-repository/scripts/inspect_repository.py clone --repo kubernetes-sigs/kustomize --depth 1
```

Copies into `/opt/data/scratch/repos/<owner>__<name>` unless `--into` names
somewhere else, and prints `written`, `bytes`, `skipped`, `stopped` and
`complete`. Narrow with `--prefix api` when only part of it matters.

**Search first, take what it names** when the repository is large — which is
most of them:

```bash
H=$(python3 ./skills/inspect-repository/scripts/inspect_repository.py open --repo kubernetes-sigs/kustomize --depth 1 | jq -r .handle)
python3 ./skills/inspect-repository/scripts/inspect_repository.py grep --handle "$H" --pattern 'func NewCmdBuild'
python3 ./skills/inspect-repository/scripts/inspect_repository.py fetch --handle "$H" --into ./src kustomize/commands/build/build.go
python3 ./skills/inspect-repository/scripts/inspect_repository.py close --handle "$H"
```

The handle survives between turns; the shell does not. Keep it, and **close it
when you are done** — an open handle holds a clone on the broker's volume.

## Rules

- **`--depth 1` unless you need history.** A full clone of a large repository
  can exceed the broker's ceiling and be refused outright. A shallow workspace
  reads identically; it just cannot commit.
- **Never invent a path.** Fetch only paths that `list` or `grep` returned. A
  path you inferred is how a read comes back empty for a file that exists under
  a different name.
- **Read `truncated` and `stopped` before you conclude anything.** `list` pages:
  when `truncated` is true, call it again with `--after <next>`. `clone` reports
  `stopped: maxFiles` or `maxBytes` when a bound bit, and `complete: false`
  means you are looking at part of a repository. "The repository does not
  contain X" is not a claim a truncated result supports.
- **Read `skipped`.** `tooLarge` means that file is never coming through this
  route; `requestBudget` means ask again for the rest; `symlink` means the file
  is there and the broker will not follow a link to it, so name the target
  instead; `notAFile` means no such file in the checkout.
- **Do not run `git` against what lands.** There is no repository there, on
  purpose.
- **Say which repository and which ref** in anything you report, and treat a
  shallow read as a read of one commit on one branch.

## Reference

| Subcommand | What it does                                                           |
| ---------- | ---------------------------------------------------------------------- |
| `clone`    | Copy a repository (or a `--prefix`) into a scratch directory and close |
| `open`     | Open a broker-side workspace, print a handle                           |
| `list`     | One page of the checkout's paths; page with `--after`                  |
| `grep`     | Search tracked files; fixed-string unless `--regex`                    |
| `fetch`    | Copy named paths into `--into`                                         |
| `close`    | Drop the broker-side clone                                             |

On an install whose broker has not been armed for content-passing, `clone`
reports `"mode": "directory"` and hands back a leased checkout on the shared
volume; the other subcommands say the broker does not serve them. Read the files
in that checkout directly and do not commit in it.
