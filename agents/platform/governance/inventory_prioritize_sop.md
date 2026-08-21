# Onboarding Report Prioritization (`bootstrap-inventory-prioritize`)

**Purpose:** Turns the raw findings produced by the discovery sweep into two things — the durable
findings queue that later publishers read, and the short ranked report the user receives now. Reads
`/opt/data/INVENTORY.raw.md`, registers every finding through
`/opt/data/scripts/inventory_findings.py`, writes `/opt/data/INVENTORY.md`.

**You do not decide what the findings are, and you do not make the registration call.** The sweep
wrote a machine-readable block into the raw file; `inventory_findings.py extract` turns it into a
numbered list, `register` refuses to send anything until every number on that list carries a score,
and `ranked` reads the order back. Your job between those commands is judgement — scoring, and
choosing what the report shows — and nothing else.

This is the last stage before delivery. The file you write is posted to the user **verbatim** by the
`bootstrap-inventory-delivery` job — no agent edits or reformats it afterward.

The report shows at most five items and the queue holds all of them, so a finding that does not
reach the report is deferred rather than discarded.

---

## Pre-Execution Check

1. If `/opt/data/INVENTORY.md` already exists, the report has already been written. Return strictly
   `[SILENT]` immediately and do nothing.
2. If `/opt/data/INVENTORY.raw.md` is absent **or empty**, the sweep has not finished or has failed.
   Do **not** run discovery yourself, do **not** go looking for the findings elsewhere, do **not**
   register anything, and do **not** write a report. Block the card with `kanban_block` saying
   whether the file was missing or empty, and stop.

   An empty findings file is not the same as a clean cluster. A clean cluster still produces a
   header and a `scanned=…` summary; zero bytes means the sweep did not write anything, and a report
   generated from it would be invented. This has been observed: given a zero-byte file, this stage
   made 51 tool calls hunting for the findings and then wrote a 554-byte report describing a cluster
   it had never read.

---

## Step 1: Read the Raw Findings — and Only Those

Read `/opt/data/INVENTORY.raw.md`, **in one read, in full**. That file is your entire input.

**This task reads exactly two files for its findings: this SOP and `/opt/data/INVENTORY.raw.md`.**
Nothing else.
That means no `search_files`, no `grep`, no reading source code, scripts, configs, logs, kanban
records or other reports, and no `gcloud`, `kubectl`, `lookout` or any other tooling. There is no
context to gather. The findings file plus the rules below are sufficient, and anything else you read
is either irrelevant or will tempt you into reporting something the sweep did not find.

`inventory_findings.py` is the exception, and it runs the other direction: it reads the raw file
you already have, records it, and reads back the order the queue computed. It is not a place to
look for findings the raw file does not contain, and it is not a licence to run anything else.

**The findings file is complete, however short it looks.** Read it once, whole. Do not page through
it in line ranges, and do not read past its end to check for more: a read that returns nothing means
you have reached the end of a complete file, not that content is missing or truncated. A cluster in
good shape produces a short findings file, and that is a normal result.

This matters more than it looks. A measured run of this stage made **116 tool calls** and spent five
minutes on it, because an empty read past the end of an 87-line file read as missing data and sent
the worker hunting through the repository for it. The report it eventually wrote was fine. The four
minutes it wasted getting there were not, and they are paid by a user sitting in a chat window
waiting for their first answer.

The raw file's format varies by deployment — it may be prose and Markdown tables, or it may be
line-oriented `key=value` findings with a `severity=` field and a trailing
`scanned=… findings=… elapsed=…` summary. Treat either as a list of findings.

**The severity the file states is evidence, not the answer.** A block line's `severity_hint` counts,
and so does the prose plan's own grouping: `Priority 1 / 2 / 3` headings, or sections named Critical
/ High / Medium / Low. That is the sweep's judgement made while it had the whole cluster in view, and
it is the best evidence you have for the likelihood and blast-radius measures in Step 3. Read it,
then classify against the anchors. Do not carry the word through unchanged — the queue orders
findings from several sources on one scale, and a sweep's `Priority 1`, an audit stream's `critical`
and a watcher event carrying no severity at all are three bars set by three authors.

Classifying against the anchors is not the holistic re-derivation that used to make this stage
unstable. Measured over three runs on identical input, re-judging severity freehand produced 3, 6
and 6 items with only two findings common to all three. Each measure in Step 3 is a lookup against a
short table, so the same finding classifies the same way twice — which is the property the queue
needs, because a list that reshuffles every morning from a fleet that has not changed is worse than
no list.

**Category or summary records are not findings.** Some tools emit per-category rollup lines
(`kind=health.category …  status=degraded total=3`) alongside the individual findings they count.
Those lines describe the same problems a second time. Use them for the posture sentence in the
report — how much was scanned, what is healthy — never as items in the ranked list, and never
register them.

**But a category that could not be checked must still be reported.** A record marking a category
`unavailable`, `skipped`, or `error` is not a finding either, and it is not a clean result: it means
that area was never examined. Say so in the posture sentence, naming the category and the reason
("control-plane checks did not run: no cloud-provider metrics available"). Dropping it leaves the
user reading a report that looks like full coverage. A silent gap reads as "clean" — the same reason
the discovery sweep is required to name a cluster whose scan failed rather than omit it.

---

## Step 2: Extract the Findings

```
python3 /opt/data/scripts/inventory_findings.py extract
```

It reads the raw file's ```findings block and prints a numbered list — `f001`, `f002`, … — with each
finding's check, cluster, namespace, object and title. **That list is the complete set, and it is the
only set.** Do not add an item you noticed in the prose and the block missed, do not drop one that
reads like a duplicate, and do not merge two into one. The block's lines were written per affected
object on purpose: a missing `readinessProbe` on three Deployments is three rows in the queue,
because each has its own manifest to change and its own life — one gets fixed next week and the other
two do not. Step 5 gathers them back into a single report line ("3 Deployments in `payments`").

`extract` writes the list to `/opt/data/INVENTORY.items.json`, which is what Step 4's `register`
reads. Leave both at their defaults.

**Cross-check against the prose plan you read in Step 1 — by problem, not by count.** The two are the
same findings said twice, but at different grain: the plan groups a recommendation over several
workloads where the block writes one line each, so the counts are expected to differ and a difference
is not a defect. What you are looking for is a problem the plan describes that no block line covers
at all. If you find one, the sweep dropped it on the way out and this stage cannot recover it: block
the card with `kanban_block` naming the problem, and stop. Registering the rest would put a short
list in the queue and label it complete.

Two exit codes mean you cannot proceed:

- **10 — no block.** The sweep predates the block requirement, or omitted it. Block the card saying
  so. Do not fall back to reading the findings out of the prose: that is the enumeration this stage
  was rebuilt to stop doing, and it lost findings every time.
- **11 — malformed lines.** The output names every bad line and why. The raw file is the sweep's
  output and not yours to edit, so block the card and quote the errors.

Any other non-zero exit is the command itself being wrong — a mistyped flag, a missing argument.
Read the error and fix the command; it says nothing about the sweep.

Zero findings is a success, not one of those cases: a clean fleet writes an empty block, `extract`
prints `extracted 0 findings`, and you go to Step 5 with nothing to register.

---

## Step 3: Score Every Extracted Finding

One score per id Step 2 printed — all of them, including the ones nobody will be asked to fix. A
report is a list and can set things aside; the queue is an order, and a row with no score has no
place in one. Step 4 will not send a partial set, so an id you skip here stops the whole
registration rather than quietly dropping one row. The two gates below are flags on a scored row,
not filters before scoring.

Each measure is a lookup against its table. Pick the row that matches; do not interpolate.

**B — blast radius: what fails.**

|     |                                                                            |
| --- | -------------------------------------------------------------------------- |
| 8   | cluster-wide or control-plane                                              |
| 5   | an entire serving workload                                                 |
| 3   | degraded capacity — some replicas, or a load-bearing non-serving component |
| 2   | one pod, a batch job, or a non-production workload                         |
| 1   | no runtime consequence; hygiene only                                       |

**For a credential or permission finding, B is the scope of what it grants, not the workload holding
it.** A project-level service-account key mounted in one pod is an 8, not a 2. The natural reading
gets this wrong.

Where two findings are otherwise equal, the one on the cluster this agent itself runs on ranks above
the same finding elsewhere.

**L — likelihood: when it bites.**

|     |                                                                                                                                                |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 10  | failing now — CrashLoopBackOff, OOMKilled, expired certificate, PVC at capacity, a credential known to be exposed                              |
| 6   | fires on the next ordinary event — a rollout, node drain, autoscale, preemption; also a long-lived broad-scope credential reachable from a pod |
| 4   | dated — a real deadline exists: an API removed in the next minor, a key or certificate expiry, a quota trend crossing                          |
| 2   | needs an abnormal event — zone loss, traffic past the current peak                                                                             |
| 1   | no failure mode; posture only                                                                                                                  |

**detect and recover: how long it hurts.** Score each 1–3.

|     | detect                                                                  | recover                                                                                                      |
| --- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 3   | silent — no probe, no alert, no telemetry on the path; a user tells you | state at risk or manual multi-step recovery — no backup, an RWO StatefulSet, a rotation plus an access audit |
| 2   | visible if someone looks                                                | an ordinary rollout or rollback                                                                              |
| 1   | something alerts on it today                                            | self-healing                                                                                                 |

**C — confidence**, a multiplier: `1.0` measured, where the fault or object was observed directly;
`0.9` read from live object state; `0.6` inferred from absence or a heuristic. A sweep that read
`kubectl get deployments -o json` and saw no `readinessProbe` is 0.9. A sweep concluding "probably no
HPA, nothing mentioned one" is 0.6.

**`rank_score = B × L × (detect + recover) × C`**, giving 1 to 480. You do not compute it and you do
not send it: register the vector, and the queue computes the score and the severity word from it.
Sending either is rejected.

**Severity is derived:** `critical` at 150 and above, `major` from 40 to 149, `minor` below 40. One
floor overrides those thresholds — **`L = 10` together with `B ≥ 3` is `critical` whatever it
scores.** Without it a live outage gets labelled by its blast radius, and a CrashLoopBackOff taking
out a third of a three-replica serving Deployment arrives as `major`, the same word as the missing
`readinessProbe` beside it that also scores 90. `B ≥ 3` is where the floor stops on purpose: an
OOMKilled nightly Job in a dev namespace is failing now and is genuinely not a `critical`.

### Worked examples

| finding                                                        | B   | L   | detect | recover | C   | score | severity           |
| -------------------------------------------------------------- | --- | --- | ------ | ------- | --- | ----- | ------------------ |
| static project-editor SA key in a Secret, no Workload Identity | 8   | 6   | 3      | 3       | 1.0 | 288   | critical           |
| CrashLoopBackOff, single-replica serving Deployment            | 5   | 10  | 1      | 2       | 1.0 | 150   | critical           |
| CrashLoopBackOff, one of three serving replicas                | 3   | 10  | 1      | 2       | 1.0 | 90    | critical — floored |
| no `readinessProbe`, 3-replica serving Deployment              | 3   | 6   | 3      | 2       | 1.0 | 90    | major              |
| Shielded Nodes disabled, Standard node pool                    | 8   | 2   | 3      | 3       | 0.9 | 86    | major              |
| no resource requests, BestEffort QoS                           | 3   | 6   | 2      | 2       | 1.0 | 72    | major              |
| OOMKilled, nightly batch Job in a dev namespace                | 2   | 10  | 1      | 2       | 1.0 | 60    | major              |
| Managed Service for Prometheus not enabled                     | 1   | 1   | 2      | 1       | 1.0 | 3     | minor              |

Two of those pairings are the point. The service-account key outranks the live CrashLoopBackOff
deliberately: one workload is down and someone can see it, while the other is a project-scoped
credential nobody is watching and nothing would report. And Shielded Nodes shares the key's blast
radius but scores a third of it, because L separates "is handing out access right now" from "would
be catastrophic if someone first compromised a node".

### The two gates

**Provider-managed.** An object in `kube-system`, `kube-public`, `kube-node-lease`, or any namespace
matching `gke-*` or `gmp-*` is not the operator's to fix. They do not own the manifest, cannot change
it, and a recommendation to do so is not weak advice, it is impossible advice. Score and register
these rows like any other and set `provider_managed: true`; the queue never opens a pull request
against one. In the report they do not compete for a slot — see Step 5.

The queue derives this flag from the namespace as well, so it is set whether or not you pass it. Pass
it anyway when you know it, and pass it for a provider-managed object that sits outside those
namespaces.

**The exception is a fault, not a gap.** A provider-managed workload that is actively broken —
crash-looping, not ready, OOMKilled, a node not registering — is `actionable: true` and reported
normally. The operator cannot patch the spec, but the action is real: a support case or an upgrade.
The line is whether the finding says "this is failing" or "this is configured in a way we would not
have chosen".

**Actionable.** A finding with no concrete next step gets `actionable: false` and sorts after every
actionable finding whatever it scored. "Adopt SLO-based alerting" has no next step; "enable Shielded
Nodes on node pool `default-pool`" has one. This is a flag rather than a score penalty on purpose — a
penalty would let a B=8 observation float back above work someone could actually do.

---

## Step 4: Register Everything

Write one score entry per extracted id to `/opt/data/INVENTORY.scores.json`, then hand the file to
the script:

```json
{
  "complete_clusters": ["prod-eu"],
  "scores": {
    "f001": {
      "rubric": { "B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0 },
      "recommendation": { "action": "…", "rationale": "…", "risk": "…" },
      "remediation": {
        "kind": "manifest",
        "path": "k8s/checkout.yaml",
        "note": "…"
      },
      "verification": {
        "kind": "kubectl",
        "command": "…",
        "still_failing_when": "…"
      },
      "actionable": true,
      "provider_managed": false
    }
  }
}
```

```
python3 /opt/data/scripts/inventory_findings.py register --scores /opt/data/INVENTORY.scores.json
```

**It validates all of it before it sends any of it.** Every id, every required field and every rubric
is checked before a connection is opened, and every problem is printed in one list — so a rejected
run costs you one edit, not one round trip per field. If it exits 12, fix everything it named and run
it again. Do not register a subset by hand and do not fall back to calling the queue directly; a
partial batch is the silent drop this stage exists to prevent, and two instrumented runs of the old
hand-rolled call registered seven and three of the same nine findings.

Sending is per cluster, so it is not all-or-nothing on the wire. If one cluster's POST fails the
others stay registered, the script names which failed, and `registered N of M` will genuinely differ.
Treat that as what it says — some of it landed — and follow the exit 13 branch below.

The identity of a row is `check` + `cluster` + `namespace` + `object`, which the script takes from
the extracted item — you cannot set them, and a re-run of this card updates the same rows instead of
duplicating them.

`complete_clusters` lists the clusters the raw file says were scanned in full. Leave a cluster out
when the file records a gap, a skipped category, a failed credential mint, or a cluster in `ERROR`.
Being listed lowers the confidence of queued rows this run did not re-report, which re-ranks them
down; a sweep that died halfway produces the same silence as a fleet that got healthier overnight,
and asserting the second from the first announces fixes that did not happen.

### The score fields

| field                            | what to send                                                                     |
| -------------------------------- | -------------------------------------------------------------------------------- |
| `rubric`                         | `{"B": …, "L": …, "detect": …, "recover": …, "C": …}` from Step 3                |
| `recommendation`                 | `{"action": …, "rationale": …, "risk": …}` — what to do, why, what breaks if not |
| `remediation`                    | `{"kind": …, "path": …, "note": …}`                                              |
| `verification`                   | `{"kind": …, "command": …, "still_failing_when": …}`                             |
| `provider_managed`, `actionable` | the Step 3 gates; a `provider_managed` line in the block stays true either way   |
| `root_cause`                     | optional; one line, where the finding has an identified cause                    |

Nothing else is accepted — a score entry carrying a `title` or a `cluster` is rejected with the list
of fields that are yours to set.

`remediation.kind` is `manifest` when the fix is a YAML edit — with `path` naming the file where the
raw file gives one — `gcloud` when it is a cluster or node-pool setting, and `manual` otherwise.
`path` is only accepted alongside `manifest`.

`verification.kind` is `kubectl`, `gcloud`, or `manual`. The command must be **read-only**, and the
narrowest thing that answers whether this one finding still holds: it is re-run later by a publisher
that has only the row. `still_failing_when` says how to read the output.

```json
{
  "kind": "kubectl",
  "command": "kubectl get deployment checkout -n payments -o jsonpath='{.spec.template.spec.containers[*].readinessProbe}'",
  "still_failing_when": "output is empty"
}
```

A `manual` verification may omit the command; nothing else may.

### Checks with no owning stream

Three of the checks the sweep emits have no audit stream behind them yet: `probes-startup`,
`readonly-root-fs`, and `no-resourcequota`. Set `remediation.kind` to `manual` for those even though
each is a YAML edit — there is no stream to route a pull request through until they get one.

The vocabulary the slugs come from is the sweep's to choose from; it lives in
[`inventory.md`](inventory.md) Step 4 beside the block that carries them.

### If the script fails

Its exit code says which of two failures you are looking at, and they want opposite things.

**Exit 12 — your scores are incomplete or malformed, or the scores file is not valid JSON.** Nothing
was sent. Fix every problem it listed and run it again; the run is repeatable and registering twice
is safe.

**Exit 13 — one or more clusters could not be sent.** **Write the report anyway.** The clusters it
did not name are registered; the ones it named are not. Say which in the card's completion summary,
do not block the card, do not retry more than once, and do not skip Step 5. A user waiting on their
first report is not served by a stage that stops because a background queue was unavailable.

On success the script prints each cluster's outcomes and a final `registered N of N`. It also names
any finding that came back **`suppressed`**, meaning the user has already dismissed it permanently: a
suppressed finding must not appear in the report or in the roll-up count.

---

## Step 5: Select What to Show

```
python3 /opt/data/scripts/inventory_findings.py ranked
```

**Take the order it gives you.** It is computed from the vectors you just registered, by the same
rule for every source, and it is the reason this stage is reproducible. Do not re-sort it, do not
second-guess a placement, and do not promote a finding because it reads worse than the one above it.
Each row carries its score, severity, check, object and the `provider_managed` and `not_actionable`
flags the rules below turn on, and the `total:` line is what the roll-up counts from.

If it exits 13 the queue is unreachable: rank by the scores you computed in Step 3 instead —
actionable before unactionable, then highest score first — and say so in the card summary.

From the top of that order:

- **The list holds at most 5 items in total**, counting everything — criticals included. The one
  exception is when critical findings alone exceed 5: those are never capped and never rolled up, so
  the list is exactly those criticals and nothing else.
- **Gather rows that share a condition into one line.** The same missing probe on three Deployments
  was registered as three rows and appears once, naming the count and the objects: "3 Deployments in
  `payments`", listing names where there are few enough to be useful and a count where there are not.
  A gathered line takes one slot and sits at its highest-scoring member's place.
- **Rows flagged `provider_managed` do not take a slot.** All of them together become at most one
  informational item, phrased as an observation rather than an instruction ("14 GKE-managed workloads
  in `kube-system` and `gmp-system` run without explicit resource limits; these are managed by GKE
  and not yours to change"), or are folded into the roll-up when there is no room. A provider-managed
  workload that is actively failing is the exception and is reported normally, at its rank.
- **All informational findings share a single item between them.** However many `minor` and
  unactionable findings survive, they get one slot in the list, not one each. Write that item at the
  level of the shared risk, name the worst instance concretely, and name or count the others inside
  it. Informational findings never occupy more than one slot while any `major` finding exists.
- **Anything not shown is rolled up**, not dropped: one line giving the count and where it lives,
  e.g. `Also found: 14 more items, tracked in the findings queue — ask for the full list.` The count
  is the `total:` line `ranked` printed minus the rows you showed or gathered into a shown line —
  count it off that list, not off what you remember registering. A run that
  reported "6 more items" against a queue holding three, all three of them shown, is what this
  sentence is here to stop. **Omit
  this line entirely when nothing remains** — printing `Also found: 0 items` is noise, and it is a
  sign the selection was padded to a target.

**Five is a ceiling, not a quota.** Report the number of distinct problems the cluster actually has.
If that number is two, the report has two items and is a better report for it. Never pad toward five
by splitting a gathered line back into its objects, by giving informational findings a slot each, or
by listing a category rollup as though it were a finding. Padding is the failure this stage was built
to fix; a short report is the success case, not an incomplete one.

Grouping informational findings is deliberate even when they have genuinely different owners and
different fixes. A first report exists to tell someone what to look at first. A list where one item
is a real problem and four are low-severity latent risks reads as five problems, and buries the one
that matters — which is the same failure as listing the same finding five times, arrived at honestly.

If there are no `critical` or `major` findings at all, say that plainly and show at most the top 3
informational items. A quiet cluster is a good result and should read like one.

**Nothing is dropped, and now nothing needs to be.** Every finding is registered, and every
registered finding is either shown, gathered into a shown line, or counted in the roll-up.

---

## Step 6: Write the Report (`/opt/data/INVENTORY.md`)

Write clean Markdown that reads well in a chat client. Structure:

1. **One-line heading**, e.g. `# GKE Environment Scan`.
2. **One or two sentences of posture:** what was scanned (clusters, nodes, workloads) and the
   headline judgement. Give the reader the shape of their environment before the problems.
3. **The selected findings**, in the queue's order, as a numbered list. **Two lines each, no
   sub-bullets:** a bold one-line headline naming the problem and where it is (cluster, namespace and
   object, or for a gathered line the count and affected objects), then one sentence covering what
   breaks if it is left alone and the action to take, in that order.

   Resist expanding this into a labelled block per finding. The reader is deciding what to look at
   first, not executing the fix from a chat window. Config snippets, exact field paths and
   step-by-step remediation belong in the full inventory or in a remediation pull request, not here.
   A report that takes a screen to skim has failed even if every word in it is correct.

4. **The roll-up line** for everything not shown.
5. **A closing line** telling the user the full inventory is available on request.

### Hard constraints

- **Aim for 2000 characters; 4000 is the hard ceiling.** The delivery router truncates longer messages with
  a `... [truncated]` footer on adapters that do not declare `splits_long_messages`. Check the length
  before you finish; if it is over, tighten the prose — do not drop a finding to fit.
- **Report only what came back from the queue.** The raw file is the only thing you may register
  from, and the ranked list is the only thing you may report from. Do not add findings, infer
  problems the sweep did not record, or supplement from your own knowledge of the environment. If the
  raw file is thin, the report is short.
- **Do not reproduce the raw file's tables.** The full fleet and workload tables stay in
  `INVENTORY.raw.md`; this report is the ranked summary, and duplicating the tables defeats it.
- **Write the report atomically.** Write the finished text to `/opt/data/INVENTORY.md.tmp`, then
  move it into place with `mv /opt/data/INVENTORY.md.tmp /opt/data/INVENTORY.md`. Do not write
  `INVENTORY.md` directly. Its existence is the signal the delivery job waits on, and that job ticks
  every 60 seconds — writing in place gives it a window in which it can read, claim, and deliver a
  half-finished report to the user. A rename on the same filesystem is atomic, so the file either
  is not there or is complete. This has been observed: a report read mid-write came back at 60% of
  its final length, with no error anywhere.

---

## Step 7: Silent Exit

Once `/opt/data/INVENTORY.md` is confirmed on disk, return strictly `[SILENT]` without running any
further commands. Delivery is handled by the `bootstrap-inventory-delivery` job — do not message the
user yourself.
