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
   one, under `bench/tf/prebuilt/<name>/`. These directories (along with shared modules under
   `bench/tf/modules/`) are what the rules below scan. A stack declares the outputs
   `cluster_name` and `cluster_location`: `devops-bench` reads them after the apply and
   fails the run without them.
2. Run `make bench-case-check`. It applies every rule on this page that a machine can, in
   about a second, with PyYAML alone: no cluster, no `bench/` virtualenv.
3. Register the case: add its `./tasks/<name>/task.yaml` line to the sorted section at the
   foot of `hack/eval/nightly-cases.txt`, at its place in case-id order, with the `#` lines
   directly above it saying why it's there and naming anything that still blocks it. That
   is where a new case lands; a presubmit seat (`hack/eval/presubmit-cases.txt`) is a later
   pull request that cites the case's nightly record, and it needs an `eval-crew` approver
   where the nightly line needs only the normal ones. A case whose fixture does not exist at
   all is not registered yet: it goes in `scripts/validate_bench_cases.py`'s
   `FIXTURE_NOT_READY` with the issue that plants the fixture. The format document's
   "Registration" section says which and why.
4. Open the pull request. `scripts/test_task_registration.py` runs the same rules in CI, and
   the nightly runs the case from the night it merges, so a case that cannot pass or cannot
   fail shows up on its record.
5. The case merges unadmitted. It runs and reports every night from then on and earns its
   presubmit seat on that record — which is admission: since 2026-09-22 the two are one edit,
   as described under "Roster admission" below.

A case for a gap whose fix is not yours takes the same path with one addition: it lands marked
`expected_fail: true`, having been seen red, and the owner's fix flips the marker in the diff
that closes the gap. [`.agents/rules/eval_driven_development.md`](../.agents/rules/eval_driven_development.md),
"When the fix is not yours", is the rule; the validator in step 2 rejects a marker that is not a
bare YAML boolean.

## The task format

`task.yaml` follows [`docs/designs/bench-case-format.md`](../docs/designs/bench-case-format.md):
`id` equal to the directory name, a `domain` slug from `docs/designs/domains.yaml`, a
`fixtures` list for anything that reads cluster state, optionally an `owner` (below), and a
`verification_spec` declared as a block. The exact half of the grade is a check on something
the case planted; the judge scores the prose the agent composed. That document is the
contract; this page does not restate it.

**Check:** `make bench-case-check` locally, `scripts/test_task_registration.py` in CI. The CI
lint asserts that the validator returned nothing at all, so every rule in it gates.

A pull request's eval jobs grade its cases with `main`'s `bench/`, so a new check type, case key
or fleet role lands before any case those jobs run uses it, including a nightly case on a lane list
such as `hack/eval/gitlab-cases.txt` ([`testing-strategy.md`](../docs/designs/testing-strategy.md#pinning-and-baselines)).

What review holds a case to beyond the validator: the objective names a thing the fixture
planted, and a run that did not do the work cannot reach it; the safeguard names the thing
the case must not do; and the stack is the cheapest one that plants the defect. A case with
its own `deployer: tofu` stack costs every pull request a multi-minute provision on top of
the agent run, which is why the two tofu-provisioned incumbents were moved to the nightly
tier ([#1218](https://github.com/gke-labs/kube-agents/pull/1218)). Prefer a seeded-fleet
fixture from `bench/tf/fleet/fixtures.json` or `deployer: noop`; a case that needs its own
stack says in the pull request why neither would do, and stays in the nightly.

## Fixture sanitization

A suite that encodes a real environment carries that environment's addresses, names and
credentials, and a fixture merged here is public. The rule: nothing under `bench/tasks/`,
`bench/tf/prebuilt/` or `bench/tf/modules/` names a real address, host, cluster, project or credential. Use the
RFC 5737 documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) for
addresses, `{{CLUSTER_NAME}}` and `{{PROJECT_ID}}` for the cluster the runner provisions, and
invented names for everything else.

**Check:** `make bench-case-check` and the CI lint scan every text file under those
directories and fail on a line that carries either:

- an IPv4 literal outside the three documentation ranges. That covers every RFC 1918
  address, every public address, and also loopback and `0.0.0.0`, which take the marker
  below when a fixture needs them;
- a credential-shaped string: the token-shaped subset of `AuditRedactor`'s patterns,
  imported from `agents/chat/defaults/plugins/common/redactor.py` so the scan and the audit
  log agree on what a credential looks like. `CREDENTIAL_SHAPES` in
  `scripts/validate_bench_cases.py` is the list of record. The redactor's bearer,
  key/value and e-mail patterns are left out because each matches ordinary prose in a
  prompt, and its env-pair and URL-password patterns because they match the sample
  manifests and connection strings a fixture carries on purpose.

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

A case's `owner` is the person whose bugs it is first. Authors are encouraged to set
themselves:

```yaml
owner: some-login
```

The field is optional and takes any GitHub login. Without it the case belongs to its
domain's `owner:` in [`docs/designs/domains.yaml`](../docs/designs/domains.yaml);
`scripts/case_owner.py` resolves that effective owner. The lint runs offline, so it cannot
tell whether GitHub will assign the login an issue; that is the bug filer's check, and a filer
that GitHub refuses the assignee for (not an organisation member or collaborator) must file to
the domain owner instead.
The login is written without the at sign, because a `task.yaml` gets quoted into issues and
comments, where a leading at sign pages someone. devops-bench discards the key, so it is a
repository-lint field like `domain`.

**Check:** `make bench-case-check` rejects `owner: maintainers`, which named nobody, a value
with a leading at sign, and one that is not a login.

What the owner is on the hook for is the demotion mechanic in
[`docs/eval-gate-roster.md`](../docs/eval-gate-roster.md): the issue a demotion files goes
to the owner, who fixes the case or proposes retiring it, and a case whose owner does not
answer stays demoted. The bar there is the same for a contributed case and an in-house one.

## Roster admission

A merged case runs every night, and on a pull request only once it is admitted: since
2026-09-22 the presubmit runs the blocking roster and nothing else, so a presubmit seat is a
roster seat; the one in-between state is the held-out seat a coverage tracker may take in the
presubmit file (`presubmit-cases.txt`'s last section), which runs on pull requests without
blocking on a graded failure until its roster line lands.
Admitted means named in `hack/eval/blocking-roster.txt` (the default of `BOOTSTRAP_ADMITTED`
in `hack/ci-eval-pr.sh`) with the case's line in `hack/eval/presubmit-cases.txt`, where an
admitted case can red a pull request on a graded failure. The evidence
store's record of the case is computed beside that and reported in every verdict, but under
the default `EVAL_ADMISSION_MODE=roster` it informs the roster edit rather than making it.
Admission, the hold-outs, how far the roster's promise reaches, demotion and what the record
would have to show before the mode ever changes are all in
[`docs/eval-gate-roster.md`](../docs/eval-gate-roster.md); how the record is computed is in
[`baselines/README.md`](baselines/README.md). Two things hold whatever the roster says: the
absolute rungs (a forbidden cluster mutation, an erroring verifier, a record that is not a
real run) red a pull request for every case, admitted or not, and a case earns its seat on
its record rather than on who wrote it.

Proposing admission is one pull request that moves the case's line from
`hack/eval/nightly-cases.txt` to `hack/eval/presubmit-cases.txt` and adds its name to
`hack/eval/blocking-roster.txt` — the three edits together, because `hack/ci-eval-pr.sh`
refuses a roster name that is not a presubmit case, refuses a case listed in both files, and
`scripts/test_eval_rosters.py` pins the two presubmit files as equal less those held-out seats —
and cites the record:
the verdict's **Record says** column once the store holds a full window (`would-admit`), or
before that the nightly runs, what failed and why each failure was the case's own regression
or an infrastructure class the harness already excludes. The roster page's hold-out entries
are the shape of the evidence a reviewer expects, and its "Admitted on the record since the
split" entries are the shape of the admission itself. A seat also meets the five rules under
"The five rules a seat meets" on that page; the pull request shows the red run and the
classified misses in the template's roster-admission block.

**Check:** `scripts/test_eval_rosters.py` checks three of those rules: a guarded fixture, an
exact objective check, and the presubmit's wall-time budget. The approver checks the rest: a
roster edit, and a presubmit-seat edit,
needs an `approved` from the `eval-crew` alias ([`hack/OWNERS`](../hack/OWNERS) scopes
`hack/eval/blocking-roster.txt` and `presubmit-cases.txt` to it), and a root approver
alone cannot merge either. The nightly file and the case directory itself need only the
normal approvers.
