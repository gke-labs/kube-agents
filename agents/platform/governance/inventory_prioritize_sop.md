# Onboarding Report Prioritization (`bootstrap-inventory-prioritize`)

**Purpose:** Turns the raw findings produced by the discovery sweep into the short, ranked report the
user actually receives. Reads `/opt/data/INVENTORY.raw.md`, writes `/opt/data/INVENTORY.md`.

This is the last stage before delivery. The file you write is posted to the user **verbatim** by the
`bootstrap-inventory-delivery` job — no agent edits or reformats it afterward.

---

## Pre-Execution Check

1. If `/opt/data/INVENTORY.md` already exists, the report has already been written. Return strictly
   `[SILENT]` immediately and do nothing.
2. If `/opt/data/INVENTORY.raw.md` is absent **or empty**, the sweep has not finished or has failed.
   Do **not** run discovery yourself, do **not** go looking for the findings elsewhere, and do
   **not** write a report. Block the card with `kanban_block` saying whether the file was missing or
   empty, and stop.

   An empty findings file is not the same as a clean cluster. A clean cluster still produces a
   header and a `scanned=…` summary; zero bytes means the sweep did not write anything, and a report
   generated from it would be invented. This has been observed: given a zero-byte file, this stage
   made 51 tool calls hunting for the findings and then wrote a 554-byte report describing a cluster
   it had never read.

---

## Step 1: Read the Raw Findings — and Only Those

Read `/opt/data/INVENTORY.raw.md`, **in one read, in full**. That file is your entire input.

**This task reads exactly two files: this SOP and `/opt/data/INVENTORY.raw.md`.** Nothing else.
That means no `search_files`, no `grep`, no reading source code, scripts, configs, logs, kanban
records or other reports, and no `gcloud`, `kubectl`, `lookout` or any other tooling. There is no
context to gather. The findings file plus the rules below are sufficient to write the report, and
anything else you read is either irrelevant or will tempt you into reporting something the sweep did
not find.

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
`scanned=… findings=… elapsed=…` summary. Treat either as a list of findings. Where an explicit
`severity` is present, use it. Where it is not, infer severity from the rubric below.

**Category or summary records are not findings.** Some tools emit per-category rollup lines
(`kind=health.category …  status=degraded total=3`) alongside the individual findings they count.
Those lines describe the same problems a second time. Use them for the posture sentence in the
report — how much was scanned, what is healthy — and never as items in the ranked list.

**But a category that could not be checked must still be reported.** A record marking a category
`unavailable`, `skipped`, or `error` is not a finding either, and it is not a clean result: it means
that area was never examined. Say so in the posture sentence, naming the category and the reason
("control-plane checks did not run: no cloud-provider metrics available"). Dropping it leaves the
user reading a report that looks like full coverage. A silent gap reads as "clean" — the same reason
the discovery sweep is required to name a cluster whose scan failed rather than omit it.

---

## Step 2: Collapse Duplicates

**Do this before ranking anything.** Raw findings are emitted per affected object, so one
misconfiguration repeated across a fleet arrives as many near-identical records. Ranking them
individually fills the report with the same problem wearing different names, which is the exact
outcome this stage exists to prevent.

Merge records into a single finding when any of these hold:

- They share a `fingerprint` (tools that emit one have already decided these are the same issue).
- They are the same condition on different objects of the same kind — one sysctl change across
  three nodes is **one** finding affecting three nodes, not three findings.
- They are the same underlying misconfiguration reached by different routes — a webhook registered
  as both a Validating and a Mutating configuration, backed by the same service with the same
  timeout and the same remedy, is one finding.

A merged finding names the count and the affected objects: "3 nodes", "10 webhooks across 5
services", listing names where there are few enough to be useful and a count where there are not.
Merge only what shares a remedy. If fixing one instance would not fix the others, they are separate
findings however similar they look.

---

## Step 3: Rank the Findings

Score every finding on three dimensions, in this order of precedence:

1. **Is it failing now, or could it fail later?** An active fault — a workload not running, a probe
   failing, a quota exhausted, a node not ready — outranks a latent risk, which outranks a
   best-practice gap. "Configured in a way that will hurt during an incident" is a latent risk.
   "Not following a recommended pattern" is a gap.
2. **Blast radius.** Cluster-wide or control-plane scope outranks a whole workload, which outranks a
   single pod or a single namespace. A finding that affects the cluster the agent itself runs on
   ranks above the same finding elsewhere.
3. **Is the action concrete?** A finding with a specific, nameable next step outranks one whose
   remedy is "consider adopting X". Rank an unactionable observation last regardless of severity.

Ties break toward the finding whose affected object the user is most likely to recognize by name.

---

## Step 4: Select What to Show

Select by severity, working from the collapsed findings of Step 2:

- **Every critical / actively-failing finding is shown**, however many there are. These are never
  capped and never rolled up.
- **Then warnings and latent risks**, highest-ranked first, up to a ceiling of 5 items total.
- **All informational findings share a single item between them.** However many distinct ones
  survive Step 2, they get one slot in the list, not one each. Write that item at the level of the
  shared risk, name the worst instance concretely, and name or count the others inside it: "10
  admission webhooks across 5 services use `failurePolicy: Fail` with 10–30s timeouts; the worst is
  cert-manager at 30s, and gmp-operator, kubeagents and warden are also affected." Informational
  findings never occupy more than one slot while any warning exists.
- **Anything still not shown is rolled up**, not dropped: one line giving the count by severity or
  category, e.g. `Also found: 14 informational items (probes, resource limits, labelling).` **Omit
  this line entirely when nothing remains** — printing `Also found: 0 items` is noise, and it is a
  sign the selection was padded to a target.

**Five is a ceiling, not a quota.** Report the number of distinct problems the cluster actually has.
If that number is two, the report has two items and is a better report for it. Never pad toward five
by splitting one finding back into its instances, by giving informational findings a slot each, or
by listing a category rollup as though it were a finding. Padding is the failure this stage was
built to fix; a short report is the success case, not an incomplete one.

Grouping informational findings is deliberate even when they have genuinely different owners and
different fixes. A first report exists to tell someone what to look at first. A list where one item
is a real problem and four are low-severity latent risks reads as five problems, and buries the one
that matters — which is the same failure as listing the same finding five times, arrived at
honestly.

If there are no critical or warning findings at all, say that plainly and show at most the top 3
informational items. A quiet cluster is a good result and should read like one.

**Never drop a finding silently.** Every finding in the raw file is either shown, merged into a
shown finding, or counted in the roll-up.

---

## Step 5: Write the Report (`/opt/data/INVENTORY.md`)

Write clean Markdown that reads well in a chat client. Structure:

1. **One-line heading**, e.g. `# GKE Environment Scan`.
2. **One or two sentences of posture:** what was scanned (clusters, nodes, workloads) and the
   headline judgement. Give the reader the shape of their environment before the problems.
3. **The selected findings**, ranked, as a numbered list. **Two lines each, no sub-bullets:**
   a bold one-line headline naming the problem and where it is (cluster, namespace and object, or
   for a merged finding the count and affected objects - "all 3 nodes", "10 webhooks across
   cert-manager, gmp-operator and warden"), then one sentence covering what breaks if it is left
   alone and the action to take, in that order.

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
- **Report only what is in the raw file.** Do not add findings, infer problems the sweep did not
  record, or supplement from your own knowledge of the environment. If the raw file is thin, the
  report is short.
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

## Step 6: Silent Exit

Once `/opt/data/INVENTORY.md` is confirmed on disk, return strictly `[SILENT]` without running any
further commands. Delivery is handled by the `bootstrap-inventory-delivery` job — do not message the
user yourself.
