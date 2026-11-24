# Adding a fix recipe

A fix recipe is one kind of fix kube-agents can open as a pull request and, once a repository has
earned it, merge unattended. Each recipe is a directory under
[`agents/platform/scripts/fix_recipes/`](../agents/platform/scripts/fix_recipes/), named after the
fleet-audit check that finds the problem. `no-pdb`, which adds a missing PodDisruptionBudget, is
the only recipe today and the worked example for everything below. What a repository owner sees
and controls (the rules file, the dial, the labels, step-down) is on the site's
[automation rules reference](site/src/content/docs/reference/automation-rules.md).

A new recipe is five files: the four the scaffold writes into its directory and one end-to-end case
under `bench/tasks/`. (`no-pdb` predates the scaffold, so its tests live in
`agents/platform/scripts/test_no_pdb_fix.py` rather than a `test_recipe.py`.) The dial, the rules
file, the post-merge loop and the clean-merge counter are shared plumbing a recipe is meant not to
edit; the last section lists where that does not hold yet.

## 1. Scaffold it

```bash
make recipe-new KIND=<check-id>
```

`KIND` is the audit check id, such as `blocking-pdb`. The target writes `fix_recipes/<kind>/` with
`recipe.yaml`, `fix.py`, `checks.py` and `test_recipe.py`. It refuses a name not shaped like a check
id (lowercase words joined by hyphens) and a directory that already exists; it does not confirm that
an audit check of that name exists. Every line you must fill in carries
the marker `RECIPE-TEMPLATE`, and `make recipe-check` fails while any is left.

## 2. `recipe.yaml`

The registry, [`fix_recipes/registry.py`](../agents/platform/scripts/fix_recipes/registry.py),
reads this file and is the canonical list of its fields and grammar: its module docstring defines
each one. The file is a strict subset of YAML (one `key: value` per line, `#` comments, plain or
double-quoted values) because the credential proxy reads it without PyYAML. Anything else refuses
the file at import time.

Four fields are required: `kind` (the directory's name), `name` (what kube-agents calls the fixes
in text it writes), and `title` and `dial_environment` (the fix's row on the dial). `eval_case` is
optional to the registry but required by `make recipe-check`; it names the recipe's primary
end-to-end case (section 5). `recheck_kinds` and `recheck_siblings` declare the one-finding re-check
after merge, which the last section's limits apply to. `applied` (`object_present` or
`field_equals`, with the `applied_*` fields each needs) and `health` (`restart_flat`, the default,
or `none` for a fix whose object is not a workload) say how the post-merge check sees the fix land.

## 3. `fix.py`: the fix, written by code

The fix is code, so the same input always gives the same bytes. The credential
broker trusts a fix pull request only when re-running your generator on the inputs the file names
gives the file byte for byte (`automerge_eligibility.verify_added_manifest`).

- `generate(candidate, finding_id)` returns `(name, text)` for the manifest the fix writes, or
  `None` where it cannot be written exactly. `candidate` is the audit finding's candidate dict.
- `read_inputs(text)` is the broker's half: it reads a pull request's file back into
  `(candidate, finding_id)`, or returns `None` for a file `generate` could not have written. Treat
  the file as untrusted, and accept no more than `generate` could have been given.

Stdlib only, because the credential proxy loads it. [`no-pdb/fix.py`](../agents/platform/scripts/fix_recipes/no-pdb/fix.py)
puts the inputs in a header comment and holds every field to a pattern before it is used.

## 4. `checks.py`: the live facts that make it safe

The sandbox's pre-flight (`fleet-audit/scripts/fix_preflight.py`) loads this module through
`registry.checks_module` and runs it on its own read of the target namespace, once the kind is wired
in (see the last section). Stdlib only, like `fix.py`.

- `read_kinds`: the kubectl kinds that one namespace read must fetch, separated by commas.
- `checks(change, live)`: one result per check, each a dict with `name`, `passed`, `detail` and
  `status` (`pass`, `fail` or `unknown`). Name each check after the live fact it relies on.
- `unanswered(detail)`: every check `unknown` for one reason, used when the checks cannot run at
  all (the change cannot be read, the cluster is not in the manifest, nothing could be read).
  `unknown` never counts as passed.

## 5. One end-to-end case

The recipe's primary case is a bench task whose `post_merge_outcome` check carries
`recipe: <kind>`; the recipe's `eval_case` names it. The check merges the agent's pull request,
applies it to the fixture's cluster, runs the post-merge check and requires a record of that kind
with the expected outcome; until the audit attaches a new kind's finding (see the last section),
no such record is written. Its fields are in
[`bench/CUSTOM-TASKS.md`](../bench/CUSTOM-TASKS.md), and
[`post-merge-verified-fixed`](../bench/tasks/post-merge-verified-fixed/task.yaml) is `no-pdb`'s.
Other cases may grade the same recipe with other outcomes, but every case's `recipe` must name a
recipe that exists.

Write the case red first: run it once before the fix exists and confirm it fails for the reason you
expect. The eval loop, from a failing case to three green runs, is in
[Bringing domain expertise to kube-agents](contributing-domain-expertise.md), and its mechanics in
[`.agents/rules/eval_driven_development.md`](../.agents/rules/eval_driven_development.md).

## 6. `make recipe-check`

```bash
make recipe-check
make recipe-check RECIPE_CHECK_BASE=origin/main
```

It fails when a recipe does not load, has no `eval_case`, names a case that does not grade it
(or a case grades a recipe that does not exist), still carries a `RECIPE-TEMPLATE` line, or has a
`fix.py` or `checks.py` that does not define the names above. With `RECIPE_CHECK_BASE` it also
warns, without failing, when the change edits a recipe and a shared-plumbing file together (the list
is `SHARED_PLUMBING` in [`scripts/check_recipes.py`](../scripts/check_recipes.py)), so a platform
owner reviews the pull request. CI runs it on every pull request. Run the recipe's own
tests with `python3 agents/platform/scripts/fix_recipes/<kind>/test_recipe.py`.

## What a new recipe does not get yet

A merged recipe gets a dial row and a rules-file kind, and the post-merge store accepts its kind.
It gets no post-merge records and no auto-merge, and parts of it are not run, because these places
still name `no-pdb` directly:

- The broker's label gate labels only `automerge_eligibility.FIX_TYPE`.
- The audit opens a generated fix pull request only for `audit_report.GENERATED_FIX_CHECK`, and
  `audit_report.remediation_target` attaches the finding a pull request fixes only for that check.
  The `pr_opened` plugin writes a post-merge record only when a finding is attached, so a new
  kind's pull requests leave no record, and its end-to-end case fails with no record to read.
- The pre-flight runs a kind's `checks.py` only when `fix_preflight.CHANGES` can read its change
  and `recheck.RECHECKS` holds it, so a new kind's checks never run there.
- The re-check (`fleet-audit/scripts/recheck.py`) builds an entry for every recipe that sets
  `recheck_kinds`, and fails at import unless `CHECK_NEEDS` names the recipe's own check and
  `SIBLING_NARROWING` names each of its `recheck_siblings`. Add those entries in the same pull
  request as a recipe that sets `recheck_kinds`, or the pre-flight stops importing.

So a new kind that should reach auto-merge still edits, today, `CHECK_NEEDS` and
`SIBLING_NARROWING` in `recheck.py`, `CHANGES` in `fix_preflight.py`, `GENERATED_FIX_CHECK` (and
with it `remediation_target`) in `audit_report.py` and `FIX_TYPE` in `automerge_eligibility.py`, until those move into the registry.
`make recipe-check` does not stop this: it only warns, only when `RECIPE_CHECK_BASE` is set, and of
these files only `automerge_eligibility.py` is on its shared-plumbing list. Name each such edit in
the pull request so a platform owner reviews it.

The broker verifies only a fix that adds a file. A recipe that changes a field in an existing
manifest needs the `edit-manifest` verb. Its editor
([`fix_recipes/yaml_edit.py`](../agents/platform/scripts/fix_recipes/yaml_edit.py)) sets one scalar
in block-style YAML and refuses what it cannot edit exactly: anchors, aliases and tags anywhere in
the document, Helm templates anywhere in the file, and flow style or block scalars on the edited
path. The broker side of the verb is not on `main` yet, so an edit recipe cannot be verified for
auto-merge today.
