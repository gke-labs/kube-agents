---
name: inspect-repository
description: Read and analyze the source of any repository on GitHub or on another forge this install serves (GitLab) — public or one this install has a token for — without a local checkout. The broker sends one revision's files; use it to answer questions about code, not to change it.
---

# inspect-repository - Read a repository this pod has no checkout of

This pod has no `git` of its own and no `.git` anywhere it can write. To read
somebody else's code, ask the credential broker for a snapshot: the files of one
revision, sent back as a tar. What lands locally is source without a repository
around it: files you can open, grep and reason about, and no `.git/config` for
anything to execute out of.

The script is `/opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py`. It prints one JSON
object on stdout.

## When to Use

- **Answering a question about code you do not have.** "How does upstream
  implement this controller?", "which release added this flag?"
- **Reading a dependency or an upstream project** named in an issue, a design
  doc, or a user's question.
- **Reading the GitOps repository or a context repository** when you are not
  editing it.

## When NOT to Use

- **Changing a repository.** Use **submit-suggestion** (a pull request against
  the GitOps repo) or **fleet-audit** (fixes for its own findings). A snapshot
  has no history and cannot be committed from.
- **Reading a file the GitOps workflow already handed you.** `fleet-audit` gives
  you a leased clone and `submit-suggestion` `prepare` gives you the copy it
  cloned; read theirs rather than taking a second copy of the same repository.

## Copy the tree

```bash
python3 /opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py clone --repo kubernetes-sigs/kustomize
```

Copies into `/opt/data/scratch/repos/<owner>__<name>` unless `--into` names
somewhere else, and prints `written`, `bytes`, `skipped`, `complete` and `sha`,
the commit the copy was taken from. `--ref` names a branch or a tag; without
it, the copy is of the branch the broker pins the repository to, else the
remote's default branch.

**A large repository is refused whole** with `SNAPSHOT_TOO_LARGE`. Name the
part you need with `--prefix`, a directory or a file, and repeat with `--force`
into the same `--into` for each further part:

```bash
python3 /opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py clone --repo kubernetes-sigs/kustomize --prefix api --into ./src
python3 /opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py clone --repo kubernetes-sigs/kustomize --prefix kustomize/commands/build --into ./src --force
```

Compare the `sha` of each part. If they differ, the branch moved between the
copies; take the parts again.

## Rules

- **Read a repository only through `clone`.** Do not download its files from
  the forge's raw, codeload or API hosts (`raw.githubusercontent.com`,
  `api.github.com`), with `curl` or a script, and do not run `git clone`. A
  direct download skips the install's host allowlist and the checks `clone`
  makes on every file, and it can read a different revision than the one you
  report. If `clone` fails, report its error.
- **Never invent a path.** Name a `--prefix` that the repository's layout, a
  README, or an earlier copy showed you. A prefix that names nothing at that
  commit gives `written: 0` and `missing`; it is not proof that the code does
  not exist under another name.
- **Read `complete` and `skipped` before you conclude anything.** `skipped`
  lists what was not written, each with the reason: a symbolic link (the file it
  names is not copied; if it is in the repository, take it with its own
  `--prefix`), or a directory laid out as a bare repository. A copy of one `--prefix`
  is a copy of part of a repository: "the repository does not contain X" is not
  a claim it supports.
- **Do not run `git` against what lands.** There is no repository there, on
  purpose.
- **Say which repository, ref and `sha`** in anything you report. A snapshot is
  one commit on one branch.

## Reference

| Flag                        | What it does                                                      |
| --------------------------- | ----------------------------------------------------------------- |
| `--repo`                    | `owner/name` on GitHub, or `<host>/<path>` on another forge       |
| `--ref`                     | Branch or tag to read                                             |
| `--prefix`                  | Copy only this directory or file                                  |
| `--into`                    | Destination directory; must be empty unless `--force`             |
| `--force`                   | Write into a non-empty directory, adding to what is there         |
| `--max-files`/`--max-bytes` | Refuse a copy over this many files or bytes; nothing is written   |
| `--depth`                   | Accepted and not needed: a snapshot is always one revision        |
| `--lease`                   | Directory mode only: the lease the leased checkout is taken under |

On an install with no broker, `clone`
reports `"mode": "directory"` and hands back a leased checkout on the shared
volume. Read the files in that checkout directly and do not commit in it.
