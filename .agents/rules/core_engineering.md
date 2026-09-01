# Core engineering rules

Rules for code written in this repository. [`AGENTS.md`](../../AGENTS.md) names this file and is
where a new rule's one-line statement goes; the form each rule takes per language lives here.

These rules bind the lines you write, not the files those lines land in. Editing one function of a
file full of literals obliges you to name the ones your edit introduces or touches and nothing
else; hoisting the rest is a separate change with its own argument, and landing it alongside a
bugfix breaks "Keep changes scoped to the request" in [`AGENTS.md`](../../AGENTS.md). A file you
are not otherwise touching is not in scope because you read it.

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

No linter checks this. The repository runs `go fmt`, `go vet`, pytest, shellcheck (the three
installer scripts only), and prettier; none of them has a magic-number rule enabled, and no
`golangci-lint` or `ruff` config exists. This is a review expectation, and the pre-PR adversarial
pass is where it gets caught.
