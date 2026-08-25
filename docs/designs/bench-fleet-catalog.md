# The fleet fixture catalog

The seeded fleet is three standing GKE clusters per eval project whose defects are
planted on purpose. `bench/tf/fleet/README.md` is the operator's document: how the stack
applies, how it reconciles, what each defect is made of, and which background findings a
correct audit returns alongside the planted one. This one is the case author's: how a
`task.yaml` refers to a fixture, which fixtures are assertable when, and what happens to a
case when one is not.

**The role vocabulary is owned by `bench/tf/fleet/fixtures.json`**, which sits beside the
Terraform that plants the fixtures and is what `hack/fleet-kubeconfigs.sh` resolves a role
against at run time. `docs/designs/fleet-fixtures.yaml` is the machine-readable form of
the rest of this document — the day-N gates below, and the project-scoped fixtures that
have no cluster slot — and it may not rename a role: `scripts/validate_bench_cases.py`
reads both and fails when they disagree about a slug or a slot. Object names in either are
copied from `bench/tf/fleet/`, which remains the source of truth for all of them.

Everything below was checked against the live fleet in `kube-agents-evals` and
`kube-agents-evals-2`, and where the audit and the README disagreed the audit won. Two
such disagreements are recorded in place: the version pin under
[A finding nobody declared](#a-finding-nobody-declared), and the HPA replica count in the
role table. Each is a correction the operator document still needs; recorded here so a
case author is not misled while it waits.

## Address a fixture by role

A case names a fixture by its role slug. Never by cluster name, never by project id.

Every eval project carries its own trio, so a case that hardcodes a cluster name runs in
one project and errors in the next, and the failure looks like a broken agent rather than
a broken case. Worse, the names are not even fixed within a project: they are
`${var.cluster_prefix}-a`, `-b` and `-c`, and the prefix is a variable whose default is
`seeded`.

So the addressable units are the role and the slot. `rbac-overgrant` is a role. `a` is a
slot. `seeded-a` is a rendering of slot `a` under this project's current prefix, and it
belongs in the harness that resolves the slot to a kubeconfig, not in a case file.

The selector that reaches the trio and nothing else is `resourceLabels.environment=seeded`
— confirmed live in both applied projects, where it returns exactly `seeded-a`, `seeded-b`
and `seeded-c`, all zonal in `us-central1-a`. The clusters also carry
`managed-by=kube-agents-seeded-fleet`, which is what keeps the orphan sweep (it matches
`managed-by=kube-agents-bench`) away from them.

The rule is about where a check points, not about every string in the file. Two cases
assert a rendered cluster name in a phrase list — `upgrade-readiness-lagging-cluster`
requires `seeded-b` and `consistency-drift-outlier` requires `seeded-c` — because the
claim being graded is that the audit named the right cluster, and `bench/tasks/DRAFTS.md`
records those two names as a contract with `bench/tf/fleet/`. That is a different thing
from addressing a fixture: the phrase survives the prefix being the default it has always
been, and it breaks loudly and correctly if the prefix ever changes. What must never
happen is a check _targeting_ a cluster by name, which is the harness's job and the thing
`fixtures:` exists to express.

Resolving a slot to a cluster is runner work and does not exist yet — `hack/ci-eval-pr.sh`
authenticates once, to `platform-agent-host`, and every cluster-state check in the corpus
reads that ambient context. `bench/tasks/DRAFTS.md` tracks it as activation blocker A5.
The contract here is what a case may write, and it holds whichever way the harness
eventually resolves it.

## Which projects have a fleet

Three projects sit in the Boskos pool: `kube-agents-evals`, `kube-agents-evals-2` and
`kube-agents-evals-3`. All three carry a fleet, the third applied on 2026-08-24, and each
keeps its own state bucket. `bench/tf/fleet/README.md` used to describe the fleet as one
trio across two projects, in its opening paragraph and again in its state-bucket list;
both now say three. The first two are the ones the live audit behind this document
covered; the third's facts here are its apply date and its gate dates, which follow
from it.

Boskos leases a project at random, and a fleet-dependent case that lands on a project
without a fleet does not fail — it errors, which drops `VerificationCoverage` below 1.0
and reds the presubmit with a message about a missing cluster rather than about the agent.
So the rule is that the fleet stack is applied to every project in the pool before any
fleet-dependent case activates, and a project added to the pool later is not lease-eligible
until its fleet is applied. Nothing in the harness checks it, so it belongs on the
pool-project onboarding checklist in
[CI pool project prerequisites](../site/src/content/docs/deploy/ci-pool-projects.md),
whose own rule is that the project is registered last.

## The roles

Eight fixtures: seven across the three cluster slots and one project-scoped. Every in-cluster fixture is on slot `a`, across the
four seeded namespaces `seeded-debug`, `seeded-reliability`, `seeded-security` and
`seeded-capacity`, plus both defect node pools. Slots `b` and `c` carry GKE-level defects
only and no workloads at all: `b` is the held-back control plane, `c` is the configuration
outlier. Every cluster is labelled `environment=seeded`, which is what confines the drift
cohort to these three and keeps `platform-agent-host` and transient `eval-pr*` clusters
from voting on the baseline.

| Role                 | Slot    | Day | What is planted                                                                       |
| -------------------- | ------- | --- | ------------------------------------------------------------------------------------- |
| `rbac-overgrant`     | a       | 0   | `clusterrolebinding/debug-binding`, cluster-admin to the `seeded-security` default SA |
| `no-pdb-workload`    | a       | 0   | `deployment/checkout-gateway` in `seeded-reliability`, two replicas, no PDB           |
| `crashloop-workload` | a       | 0   | `deployment/payments-api` in `seeded-debug`, 64Mi limit, deterministic OOMKilled loop |
| `hpa-saturated`      | a       | 0   | `pinned-inference-pool` at min = max = 1 under an HPA that wants more                 |
| `idle-nodepool`      | a       | 7   | `idle-batch-pool`, zero non-system pods, held by a NoSchedule taint                   |
| `orphan-disks`       | project | 30  | `orphan-pd-1` and `orphan-pd-2`, unattached, 10GB, in `var.zone`                      |
| `version-laggard`    | b       | 0   | Control plane one minor behind the REGULAR channel default                            |
| `drift-outlier`      | c       | 1   | Master authorized networks absent, where a and b carry an open block                  |

The `inference-server` HPA under `hpa-saturated` does not compute a stable desired
replica count. Read on 2026-08-24, `status.desiredReplicas` on `seeded-a` was 3 in
`kube-agents-evals`, 2 in `kube-agents-evals-2` and 3 in `kube-agents-evals-3` — same
stack, same manifests, three projects, two different answers. The fixture holds anyway,
and that is the point: desired exceeds the pinned maximum of 1 in every project, which is
the whole claim. The number it exceeds it by is a load calculation over live utilisation,
and it moves. So a case must assert the pin and the unmet demand — `maxReplicas` at 1,
`desiredReplicas` above it — and never a specific figure, because there is no figure that
is true everywhere the case might land.

Two things about this vocabulary are worth knowing before it confuses someone, because
neither is going to be obvious from a slug.

**A role slug is not the `seeded-role` label.** `bench/tf/fleet/main.tf` carries
`seeded-role=pinned-inference` on the pinned pool's node label and taint, and
`seeded-role=idle-batch` on the idle pool's taint — so two of the eight roles are called
one thing by the catalogue and another by the Terraform that plants them. They are
different mechanisms and both are load-bearing: the label and taint are scheduling
constraints that keep other workloads off those pools, and the role slug is what the
runner resolves to a kubeconfig. Nothing breaks, but do not read one as the other, and do
not "fix" either to match without changing the thing that reads it.

**`idle-nodepool` is also the cost SOP's check id.** The role names the planted fixture;
the check id names the finding an audit returns about it. They are deliberately the same
word for the same subject, but a sentence with `idle-nodepool` in it is ambiguous about
which of the two it means, so say "the `idle-nodepool` role" or "the `idle-nodepool`
check" and never the bare slug.

## Day 0, 1, 7, 30

The fleet is not fully assertable on the day it is applied, and the delay is not
provisioning — it is the SOPs' own age rules. A collector that filters on
`creationTimestamp` returns nothing for a fixture younger than its window, so the audit
correctly reports no finding and a case asserting one correctly fails.

Five of the eight are assertable on apply day: `rbac-overgrant`, `no-pdb-workload`,
`crashloop-workload`, `hpa-saturated` and `version-laggard`, covering security, reliability,
cluster debugging, remediation, capacity and upgrades between them. A corpus that leans on
these can go green the day the fleet applies.

`drift-outlier` waits a day. The drift SOP excludes a cluster whose `createTime` is under
24 hours old from every cohort, so on apply day the `(standard, seeded)` cohort has zero
members — and §2.4's floor, a cohort of fewer than three clusters produces no findings
ever, would floor it out even if only one cluster were new.

`idle-nodepool` waits seven, and no agent can satisfy that gate reliably, because the GKE
node pool has no creation timestamp to read. The cost SOP's idle-nodepool check refuses
pools created less than seven days ago, but `gcloud container node-pools describe` returns
no `createTime` and neither does the REST resource; the only age signal is the boot-disk
creation time of the pool's current nodes, which a rolling recreation resets while the
pool object is untouched. So the gate is a judgement the agent makes from a proxy, and a
node upgrade can silently close it. Treat `idle-nodepool` as the least dependable fixture in
the catalog, and do not build a blocking objective on the age gate itself.

`orphan-disks` waits thirty, and that one is real: the unattached-disk collector filters
server-side on the immutable `creationTimestamp<-P30D`. It is the longest gate in the
fleet and the one most easily lost, because `fleet-cost-idle-pool` needs both of its
fixtures — so the cost case waits 30 days, not 7.

The clock is per project, not per fleet. Each project's gates open from the day its own
stack was applied, so the earliest a fleet-wide assertion can hold is the latest project's
date. `kube-agents-evals` and `kube-agents-evals-2` applied on 2026-08-21, putting day 7
at 2026-08-28 and day 30 at 2026-09-20; `kube-agents-evals-3` applied on 2026-08-24,
putting its gates at 2026-08-31 and 2026-09-23. Because Boskos leases at random, a case
activated on the earlier date passes on two projects and fails on the third for three
days, which reads as flake. Activate against the last project's date and add a project's
own dates when it joins the pool.

Recreating a fixture restarts its clock. `creationTimestamp` is server-set and immutable;
backdating is impossible, and the README says plainly not to try. For the two node pools
that means editing in place or not at all — and for the disks, name, size, type and zone
all force replacement, so a label update is the only safe change and nothing else there is
worth changing.

## A finding nobody declared

The fleet's premise is that a correct audit returns the planted findings and the
documented background ones, and nothing else — that is what lets a case assert an exact
finding set rather than a substring. One fixture currently breaks it.

A comment in `bench/tf/fleet/main.tf` argues that branch (a) of upgrade SOP check 3.1,
"the control plane runs a version the channel does not offer", is "false by construction
here because the pin is drawn from that very list". It is drawn from the location-wide
`valid_master_versions` instead, so `seeded-b` sits on `1.34.10-gke.1106000`, a RAPID
version absent from REGULAR's `validVersions`, and branch (a) fires at critical alongside
the intended branch (b). An audit that reports it is right; a case that asserts the
documented finding set is wrong. The fleet README's background-findings table, which
promises the planted finding "plus the rows below — nothing else", does not list it
either.

Until `main.tf` derives the pin from the REGULAR channel's `validVersions` rather than the
location-wide list, treat the branch (a) critical on slot `b` as expected output. A case
touching `version-laggard` asserts branch (b) specifically and must not assert that the
critical count is one, or that branch (a) is absent.

## When a fixture goes away

Two ways a fixture stops being assertable, and both are worth designing a case around.

A cleanup sweep deletes it. One `orphan-pd-` deletion costs the cost case a month, because
the recreated disk starts its 30-day clock over. The clusters carry
`managed-by=kube-agents-seeded-fleet`, deliberately distinct from
`managed-by=kube-agents-bench` which the orphan sweep matches on, but a disk deleted by
hand is a disk deleted by hand.

A cluster is replaced. That is the documented recovery for `version-laggard` when the
maintenance exclusion lapses or the held minor reaches EOL, and it makes the replaced
cluster new for 24 hours — which takes the drift cohort from three clusters to two and
drops it under the floor. The drift audit then emits nothing for the whole fleet, so
`consistency-drift-outlier` goes red on every open pull request for a day. It is a clean,
self-clearing outage rather than a wrong answer, but it is a day of red: schedule a
replacement when the drift case can be quiet, or announce the gap.

Neither of these is a reason for a case to hedge. A case that tolerates its fixture being
absent cannot tell "the agent missed it" from "it was not there", which is the same defect
as a safeguard that cannot tell absent from unreachable. Declare the dependency in
`fixtures:`, assert the planted noun specifically, and let the case go red when its
fixture does.

## Read-only

The fleet is shared by every open pull request, and no case may mutate it. A case that
writes to a fixture spoils it for someone else, non-deterministically, ten minutes later
and in another pull request's logs.

Nothing enforces it today. The presubmit runs as `prowjob-default-sa@kube-agents-prow`,
which holds `container.admin`, `container.developer`, `storage.admin` and
`resourcemanager.projectIamAdmin` in all three eval projects (`gcloud projects
get-iam-policy` on each, filtered to that member), no RBAC narrows it inside the clusters, and
`kubectl auth can-i delete deployments -n seeded-debug` answers yes. So read-only is a
rule cases obey, not a property of the credential, and it should not be read as a
guarantee anywhere: an agent that decides remediation is helpful can delete the fixture
every other pull request depends on, and the first evidence will be somebody else's case
going red.

Making it a guarantee means a second, narrower credential for fleet-dependent runs. Seven
of the ten drafted cases already need a GitOps-repo write path — the six audit scenarios
and `rca-remediation-pr` — which is contained by pinning
it to a throwaway repository per eval project rather than by asking the agent not to; the
cluster credential wants the same treatment. Until then, a case author's assertion that
their case is read-only is the only control there is.

Asserting read-only from inside a case is `resource_property` against the fixture — "the
planted defect survived the run" — and not `tool_called`, which sees only the delegating
turn's calls and would be blind to a worker's mutation.
