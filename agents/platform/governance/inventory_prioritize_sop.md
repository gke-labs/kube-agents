# Onboarding Report Prioritization (`bootstrap-inventory-prioritize`)

**Purpose:** Turns the raw findings produced by the discovery sweep into the short, ranked report the
user actually receives. Reads `/opt/data/INVENTORY.raw.md`, writes `/opt/data/INVENTORY.md`.

This is the last stage before delivery. The file you write is posted to the user **verbatim** by the
`bootstrap-inventory-delivery` job — no agent edits or reformats it afterward.

---

## Pre-Execution Check

1. If `/opt/data/INVENTORY.md` already exists, the report has already been written. Return strictly
   `[SILENT]` immediately and do nothing.
2. If `/opt/data/INVENTORY.raw.md` is absent, the sweep has not finished or has failed. Do **not**
   run discovery yourself and do **not** write a placeholder report. Block the card with
   `kanban_block` stating that the raw findings file is missing, and stop.

---

## Step 1: Read the Raw Findings — and Only Those

Read `/opt/data/INVENTORY.raw.md`. That file is your entire input.

**Do not run any discovery commands.** No `gcloud`, no `kubectl`, no `lookout`, no other tooling.
Everything you need is in the file. Re-scanning here would duplicate work the sweep already did, add
minutes to the user's wait, and produce a report whose contents no longer match the sweep it claims
to summarize.

The raw file's format varies by deployment — it may be prose and Markdown tables, or it may be
line-oriented `key=value` findings with a `severity=` field and a trailing
`scanned=… findings=… elapsed=…` summary. Treat either as a list of findings. Where an explicit
`severity` is present, use it. Where it is not, infer severity from the rubric below.

---

## Step 2: Rank the Findings

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

## Step 3: Select What to Show

Do not report a fixed number of items. Select by severity, with a cap:

- **Every critical / actively-failing finding is shown**, however many there are. These are never
  capped and never rolled up.
- **Then warnings and latent risks, highest-ranked first, until the report shows 5 items total.**
- **Everything remaining is rolled up**, not dropped: a single line giving the count by severity or
  category, e.g. `Also found: 14 informational items (probes, resource limits, labelling).`

If there are no critical or warning findings at all, say that plainly and show the top 3
informational items. Do not promote informational findings to manufacture urgency — a quiet cluster
is a good result and should read like one.

**Never drop a finding silently.** Every finding in the raw file is either shown or counted in the
roll-up.

---

## Step 4: Write the Report (`/opt/data/INVENTORY.md`)

Write clean Markdown that reads well in a chat client. Structure:

1. **One-line heading**, e.g. `# GKE Environment Scan`.
2. **One or two sentences of posture:** what was scanned (clusters, nodes, workloads) and the
   headline judgement. Give the reader the shape of their environment before the problems.
3. **The selected findings**, ranked, as a numbered list. For each, in one or two sentences:
   - **what** the finding is,
   - **where** — cluster, namespace, and object name,
   - **why it matters**, in terms of what breaks or degrades,
   - **what to do** — one concrete action.
4. **The roll-up line** for everything not shown.
5. **A closing line** telling the user the full inventory is available on request.

### Hard constraints

- **Keep the whole file under 4000 characters.** The delivery router truncates longer messages with
  a `... [truncated]` footer on adapters that do not declare `splits_long_messages`. Check the length
  before you finish; if it is over, tighten the prose — do not drop a finding to fit.
- **Report only what is in the raw file.** Do not add findings, infer problems the sweep did not
  record, or supplement from your own knowledge of the environment. If the raw file is thin, the
  report is short.
- **Do not reproduce the raw file's tables.** The full fleet and workload tables stay in
  `INVENTORY.raw.md`; this report is the ranked summary, and duplicating the tables defeats it.
- **Write `/opt/data/INVENTORY.md` last**, once the content is final. Its existence is the signal the
  delivery job waits on, so a partially-written file can be delivered as-is.

---

## Step 5: Silent Exit

Once `/opt/data/INVENTORY.md` is confirmed on disk, return strictly `[SILENT]` without running any
further commands. Delivery is handled by the `bootstrap-inventory-delivery` job — do not message the
user yourself.
