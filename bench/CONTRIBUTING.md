# Contributing a bench case

How a devops-bench case written outside this repository becomes a case that runs in its
presubmit. [`CUSTOM-TASKS.md`](CUSTOM-TASKS.md) is how to write one, here or in a private
repository; this page is what happens after it is written, and it applies to a case from an
in-house author the same way. Every rule below names the check that enforces it, or says that
review does.

Contributed cases live in this tree. A suite that has to stay private runs where it is, using
the private-repository shape in `CUSTOM-TASKS.md`; there is no registry of external suite
repositories that CI pulls from.

## The path

1. Write the case under `bench/tasks/<name>/task.yaml`, and its OpenTofu stack, if it has
   one, under `bench/tf/prebuilt/<name>/`. Both directories are what the rules below scan.
2. Run `make bench-case-check`. It applies every rule on this page that a machine can, in
   about a second, with PyYAML alone: no cluster, no `bench/` virtualenv.
3. Register the case in `hack/ci-eval-pr.sh`: an active `TASKS` entry, a commented-out one
   for a case whose fixture is not ready, or a `NIGHTLY_TASKS` entry for a case too slow for
   the presubmit. The format document's "Registration" section says which and why.
4. Open the pull request. `scripts/test_task_registration.py` runs the same rules in CI and
   the presubmit runs an active entry three times against the branch, so a case that cannot
   pass or cannot fail shows up on its own pull request.
5. The case merges unadmitted. It runs and reports on every pull request from then on, and
   earns admission on its record, as described under "Roster admission" below.

## The task format

`task.yaml` follows [`docs/designs/bench-case-format.md`](../docs/designs/bench-case-format.md):
`id` equal to the directory name, a `domain` slug from `docs/designs/domains.yaml`, a
`fixtures` list for anything that reads cluster state, an `owner` (below), and a
`verification_spec` declared as a block. The exact half of the grade is a check on something
the case planted; the judge scores the prose the agent composed. That document is the
contract; this page does not restate it.

**Check:** `make bench-case-check` locally, `scripts/test_task_registration.py` in CI. The CI
lint asserts that the validator returned nothing at all, so every rule in it gates.

What review holds a case to beyond the validator: the objective names a thing the fixture
planted, and a run that did not do the work cannot reach it; the safeguard names the thing
the case must not do; and the stack is the cheapest one that plants the defect. A case with
its own `deployer: tofu` stack costs every pull request a multi-minute provision on top of
the agent run, which is why the two tofu-provisioned incumbents were moved to the nightly
tier ([#1218](https://github.com/gke-labs/kube-agents/pull/1218)). Prefer a seeded-fleet
fixture from `bench/tf/fleet/fixtures.json` or `deployer: noop`; a case that needs its own
stack says in the pull request why neither would do, and starts in `NIGHTLY_TASKS`.

## Fixture sanitization

A suite that encodes a real environment carries that environment's addresses, names and
credentials, and a fixture merged here is public. The rule: nothing under `bench/tasks/` or
`bench/tf/prebuilt/` names a real address, host, cluster, project or credential. Use the
RFC 5737 documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) for
addresses, `{{CLUSTER_NAME}}` and `{{PROJECT_ID}}` for the cluster the runner provisions, and
invented names for everything else.

**Check:** `make bench-case-check` and the CI lint scan every text file under those two
directories and fail on a line that carries either:

- an IPv4 literal outside the three documentation ranges. That covers every RFC 1918
  address, every public address, and also loopback and `0.0.0.0`, which take the marker
  below when a fixture needs them;
- a credential-shaped string: the token-shaped subset of `AuditRedactor`'s patterns,
  imported from `agents/chat/defaults/plugins/common/redactor.py` so the scan and the audit
  log agree on what a credential looks like. `CREDENTIAL_SHAPES` in
  `scripts/validate_bench_cases.py` is the list of record; today it is private-key blocks
  and the `AIza`, `ya29.`, `gh*_`, `github_pat_`, `xox*-`, `sk-` and JWT shapes. The
  redactor's bearer, key/value and e-mail patterns are left out because each matches
  ordinary prose in a prompt.

Paths with a component beginning with a dot, `*.tfstate*` and `*.tfvars` are skipped, so a
local `tofu apply` cannot red the check.

The escape hatch is per line: append `sanitizer: allow` followed by the reason, on the line
that carries the value, in a comment where the file format has one.

```yaml
bind: 127.0.0.1 # sanitizer: allow the kube-proxy default the prompt quotes, not a host
```

A marker with no reason after it is itself a finding. The marker exempts its own line only;
for a private-key block it goes on the `-----BEGIN` line.

What the scan does not see, and review does: IPv6 literals, hostnames, cluster and project
names, and the customer-specific workload names a prompt can carry. There is no pattern that
separates a real one from an invented one, so the reviewer asks.

## Ownership

Every case names who answers for it:

```yaml
owner: some-login
```

The value is a GitHub login without the at sign, or the literal `maintainers`, meaning the
approvers in the repository's [`OWNERS`](../OWNERS) file. Bare because a `task.yaml` gets
quoted into issues and comments, where a leading at sign pages someone. devops-bench
discards the key, so it is a repository-lint field like `domain`.

**Check:** `make bench-case-check` rejects a case with no `owner`, a value with a leading at
sign, or one that is not a login.

What the owner is on the hook for is the demotion mechanic in
[`docs/eval-gate-roster.md`](../docs/eval-gate-roster.md): the issue a demotion files goes
to the owner, who fixes the case or proposes retiring it, and a case whose owner does not
answer stays demoted. The bar there is the same for a contributed case and an in-house one.

## Roster admission

A merged case runs on every pull request, and cannot red one on a graded failure until it is
admitted: by its record in the evidence store once that holds a full window for it at the
current version key, or, until the store is armed and has filled for it, by being named in
`BOOTSTRAP_ADMITTED`. Admission, the hold-outs, how far the roster's promise reaches,
demotion and the switch-over that deletes the list are all in
[`docs/eval-gate-roster.md`](../docs/eval-gate-roster.md); the computed admission that
replaces the bootstrap list is in [`baselines/README.md`](baselines/README.md). Two things
hold whatever the roster says: the absolute rungs (a forbidden cluster mutation, an erroring
verifier, a record that is not a real run) red a pull request for every case, admitted or
not, and a case earns its seat on its record rather than on who wrote it.

Proposing admission while the bridge stands is a pull request that adds the case to
`BOOTSTRAP_ADMITTED` in `hack/ci-eval-pr.sh` and cites the record: the runs, what failed and
why each failure was the case's own regression or an infrastructure class the harness already
excludes. The roster page's hold-out entries are the shape of the evidence a reviewer expects.
Once the store governs the case, no pull request is needed: the record admits it, or turns it
away, and the verdict's **Admitted by** column says which.

**Check:** none mechanical. The roster edit is reviewed like any change to the gate.
