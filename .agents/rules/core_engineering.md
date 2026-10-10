---
# Claude Code loads this rule only beside files matching `paths`; Antigravity uses `trigger` and `description`.
paths:
  - "**/*.go"
  - "**/*.py"
  - "**/*.sh"
trigger: model_decision
description: "Core engineering rules for Go, Python, and Bash code: no magic constants and credential-aware identifier naming for CodeQL."
---

# Core engineering rules

Rules for code written in this repository. [`AGENTS.md`](../../AGENTS.md) names this file and is
where a new rule's one-line statement goes; the form each rule takes per language lives here.

These rules bind the lines you write, not the files those lines land in. Editing one function of a
file full of literals obliges you to name the ones your edit introduces or touches and nothing
else; hoisting the rest is a separate change with its own argument, and landing it alongside a
bugfix breaks "Keep changes scoped to the request" in [`AGENTS.md`](../../AGENTS.md). A file you
are not otherwise touching is not in scope because you read it. The naming rule at the end reaches
an existing identifier only through an alert that names it.

## No magic constants

Every hardcoded value gets a name, and the name is declared at the top of the file — after the
imports, before the first function. This covers numbers, strings, durations, timeouts, retry
counts, size limits, file paths, URLs, and resource names.

A literal buried mid-function cannot be found by search, needs a comment at every use site to be
understood, and drifts out of step with the other copies of itself. Naming it at the top makes the
value reviewable on its own: a reader who wants to know what this file assumes about the world
reads the top of it rather than all of it.

`scripts/check_context_budget.py` is the pattern to copy — `BUDGET`, `FILES`, and `IMPORT_RE` sit
above the first function, each with the reasoning for its value beside it.

### Where "the top of the file" is

- **Go** — a `const (...)` block immediately after the import block, or `var (...)` for values a
  constant cannot hold. Unexported unless another package needs them.
- **Python** — module-level `UPPER_SNAKE_CASE` after the imports.
- **Bash** — `readonly NAME=...` after the shebang and the `set` line.

Terraform, YAML, and Helm values are out of scope: `locals` and `values.yaml` already are the
top-of-file declaration, and there is no second place for a literal to hide.

### Exceptions

- `0`, `1`, `-1`, `""`, and empty collections.
- A literal that is the subject of the line it appears on — an array index, a version comparison,
  an exit code on the line that documents it.
- Test files, where the literal is the expected value. Naming it moves the assertion away from
  what it asserts.

### Enforcement

No linter checks this. The repository runs `go fmt`, `go vet`, pytest, shellcheck at warning
severity over every tracked script (`make shellcheck`, the upstream copies in
`third_party/google-skills/` excepted), ruff's error-only rules (`make lint-python`), and prettier;
none of them has a magic-number rule enabled, and no `golangci-lint` config exists. This is a review
expectation, and the pre-PR adversarial pass is where it gets caught.

## Name identifiers for what they hold

CodeQL's clear-text-storage and clear-text-logging queries decide what is sensitive by the name of
the identifier that holds or produces it (a variable, parameter, attribute, key or function), or of
a string literal used as a lookup key, as in `os.environ.get("SESSION_KV_API_KEY")`, not by the
value. The Python queries take their words from `SensitiveDataHeuristics.qll` in the
`codeql/concepts` library (0.0.32 as bundled with `codeql/python-queries` 1.8.11; the file is the
current list). A name is a secret source when it contains `secret`, `trusted` or `confidential`; a
password source when it contains `password`, `passwd`, `passcode`, `passphrase`, `oauth`, a
delimited `mfa`, or `auth`, `authentication`, `authorization` or `api` followed within one character
by `key` (`tok` as well, after `api`), so `API_KEY` and `AUTH_KEY` match and `API_SERVER_KEY`,
`AUTH_SIGNING_KEY` and `AUTH_TOKEN` do not; and a private-data source for words such as `salary` or
`zip_code`. The account and certificate classes exist in the library, but the two clear-text queries
drop them. A name is exempt again when it also contains `path`, `file` (not `profile`), a delimited
`url`, `hash`, `sha`, `md5`, `random`, `redact`, `censor`, `obfuscate`, `crypt` or `encode` (though
`unencrypted` and `unencoded` are not exempt), or any character outside `[A-Za-z0-9_$.-]`, and the
lookbehinds exclude `untrusted`, `is_trusted` and a `secret` that directly follows `is` or `is_`,
which takes `REDIS_SECRET` with it. These lists are abridged; the file is complete. Go is different:
its one clear-text query, `go/clear-text-logging`, takes a name only when it is password-shaped
(`pass(wd|word|code|phrase)`, or `auth`, `authentication`, `authorization`, `api` or `secret`
directly followed by `key`, from `semmle/go/security/SensitiveActions.qll`) or is a callee named
`getPassword`, never a constant string, and drops names containing `test`, `mask` (not `unmask`),
`hash`, `sha`, `md5`, `code`, `crypt`, `redact`, `censor`, `obfuscate` or `err`; its other sources
are not names at all (`net/http` request headers, client-go secret reads), and no rename clears
those. `secret` and `trusted` in a Go name reach its weak-hashing query instead. Recent clear-text
alerts here have been Python names like these on values that were not credentials: a directory the
sandbox guard checks, a tuple of public apex domains, the name of an environment variable, a test
fixture whose manifest happens to be `kind: Secret`.

Two obligations follow:

- **A value that is not a credential does not carry a name that says it is.** Name the guard's
  directory for its owner, not `TRUSTED`; the apex list `PROTECTED_APEX_DOMAINS`, not
  `TRUSTED_APEX`; a fixture that registers a cluster for the registration, not `secret`; and
  `SESSION_KV_AUTH_ENV`, not `SESSION_KV_API_KEY`, when what it holds is the variable's name.
- **A value that is a credential keeps a name the regex sees**, so the same queries can watch it
  reach a log line or a file. In Python the exemptions cut both ways: `SHARED_SECRET` contains
  `sha` and `REDIS_SECRET` ends in `is_secret`, so neither is watched, while `WEBHOOK_SECRET` is;
  `API_SERVER_KEY` and `SLACK_BOT_TOKEN`, the install's own credentials, match nothing, so log
  redaction rather than CodeQL is what keeps those out of logs. Renaming a real key to clear an
  alert defeats the one check that reads this code for that failure.

When one of these alerts is a false positive, read the SARIF code flow first: the flagged line is
the sink, and the source is the identifier, or the lookup string, one or more hops back, sometimes a
function's name rather than a variable's. Rename at the definition and leave a comment naming the
heuristic there, or at the one sink several definitions share, so the next reader sees why the name
avoids the word. Dismissing the alert records nothing in the tree, and a suppression comment on the
sink, where one is honoured at all, hides the next real finding at that line.

### How CodeQL enforces it

No linter checks this either; CodeQL does, through GitHub's default setup, and
[`docs/pull-request-workflow.md`](../../docs/pull-request-workflow.md#local-validation-before-committing)
is canonical for when that runs, why a pull request here cannot count on showing an alert closing,
and the CLI commands that reproduce one before the pull request opens, from a database over the
flagged file alone, before and after the change. A pull request that closes an alert cites both
runs.
