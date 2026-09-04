---
name: self-investigation
description: One hour's audit of kube-agents itself — read the deployed revision's logs, traces, metrics and cluster state, find what is broken or wasteful in the harness rather than in the clusters it manages, and write graded findings to findings.json.
---

# Self-Investigation

The procedure for one run. Work the phases in order; each narrows what the next has to read.

Everything here is read-only. You have no kubectl, no gcloud, and no write path to the installation
you are auditing — see SOUL.md §1 for the two grants that exist and why neither is yours. `EVIDENCE`
below means `/opt/hermes/.venv/bin/python3 /opt/selfimprove/scripts/selfimprove_evidence.py` — the
venv interpreter, not a bare `python3`: the `kubernetes` package the `k8s` subcommands import is
installed only into that venv, and a bare `python3` on PATH resolves to the system interpreter and
fails every `k8s` call with `ModuleNotFoundError: No module named 'kubernetes'`.

## 0. Orient (2 minutes, do not skip)

- Read the brief. Note the deployed revision, the source path, the namespace, the signals in scope.
- `EVIDENCE k8s deployments` and `EVIDENCE k8s pods` — what is actually running, and is any of it
  unhealthy right now.
- Read the ledger summary in the brief. Known findings get re-reported with the same title and
  location, not renamed.
- If the brief says the image is unstamped, every finding you write says so too.
- Budget: about 90 model calls per turn, and no warning before they are gone. That is not enough to
  cover every signal class, so go deep on one or two rather than shallow on all seven — successive
  runs cover the rest, and the ledger accumulates what they find. Start writing findings.json as
  soon as phase 1 is done (§6) rather than saving it for the end.
- The brief says whether this run gets more than one turn. If it does, a turn cut off part-way is
  restarted with findings.json still on disk and your closing summary as the handoff — read the
  file first and add to it, never replace it. If the brief says this run gets one turn, there is no
  second chance and the incremental write is the only thing that survives.
- Being restarted is not a reason to defer the write. A turn that writes nothing hands its
  successor nothing, and the runner keeps only what reached the file or your final response.

## 1. Cast a wide net (cheap counts before expensive reads)

- `EVIDENCE logs-count --hours 24 --severity ERROR` — the shape of the last day in one call, already
  bucketed by container and severity, so it also answers which component owns it.
- `EVIDENCE logs-count --hours 168 --severity ERROR` — is today unusual or is this the baseline. A
  spike is a finding; a flat line at a high number is a different and often better finding.
- `EVIDENCE k8s events --hours 24` — restarts, evictions, failed mounts, image pull failures.
- `EVIDENCE metrics --filter 'metric.type="kubernetes.io/container/restart_count"'` — restarts the
  events have already aged out of. Scoped to this cluster for you: the monitoring grant is
  project-wide, and the clusters under management are on the other side of the line you audit. A
  filter naming another cluster returns nothing rather than reaching one.

Pick the two or three largest buckets. Do not read every log line; you will run out of turns
reading and have nothing left for analysis.

## 2. Read the agent's own files

The pod's own logs are the richest source and they are not on stdout — fluent-bit tails
`/opt/data/logs/*.log` and stamps them `log_source: agent-file`.

- `EVIDENCE logs --agent-files --hours 24 --limit 100`
- `EVIDENCE logs --agent-files --query 'jsonPayload.message:"Traceback"'`
- `EVIDENCE logs --agent-files --query 'jsonPayload.message:"Permission denied" OR jsonPayload.message:"403"'` —
  the highest-yield query for the `inefficiency` class.
- `EVIDENCE logs --agent-files --query 'jsonPayload.message:"No such file"'` — a tool reaching for a
  path the image does not have.

## 3. Follow one thread to the bottom

Choose the largest bucket and reconstruct what happened, in order, end to end:

- Widen the window back past the first occurrence: `EVIDENCE logs --hours N --limit 200`. The window
  always ends now — there is no `--until`, so reach further back rather than trying to bracket it.
- Get the trace for the same window: `EVIDENCE traces --hours N --limit 50`. Output is slowest
  first, and `durationMs` on each row is the number to grade against. Add `--full` on the ones that
  look slow: it adds a `slowest` list of the child spans inside the trace, which is how you name
  what consumed the time rather than reporting that something did. A span that is 80% of a request
  is a `latency` finding on its own.
- Open the source at the deployed revision and read the code that emitted the line. **This is the
  step that separates a finding from a log excerpt.** A traceback names a file and a line; go read
  it. Confirm the code path can actually be reached the way the evidence says it was.
- State the mechanism in one sentence before you write anything down. If you cannot, you have a
  symptom and not yet a finding — say so, grade it `low`, and record where the next run should look.

## 4. Sweep the classes the errors will not show you

Errors announce themselves. These do not:

- **inefficiency** — count repeats. The same tool call failing the same way forty times is one
  missing permission, not forty errors. A retry loop against a call that can never succeed is the
  canonical instance.
- **latency** — compare traces against what the code intends. A 120s connect timeout that is being
  hit is a different finding from one that is merely configured.
- **responses** — a turn that ended with no message, a reply that is a raw tool schema or a stack
  trace, a session that hit `max_turns` mid-answer.
- **delivery** — `EVIDENCE logs --query 'jsonPayload.message:"home channel" OR jsonPayload.message:"chat.spaces"'`.
  Read the surrounding turn: a delivery failure is usually silent to the user, which is what makes
  it worth finding.
- **forge** — `EVIDENCE logs --query 'jsonPayload.message:"github" AND severity>=WARNING'`. A pull
  request that was created but is wrong counts, and looks like success in the logs.

## 5. Test the hypothesis without touching anything

- Re-read the source path you blamed and check the surrounding conditions, not just the line.
- Look for the negative case in the evidence: if your explanation is right, some other input should
  have produced the opposite outcome. Query for it. Not finding it weakens the finding — say so in
  `confidence`.
- Check the value you assume is set: `EVIDENCE k8s configmaps` for which keys exist, and
  `EVIDENCE k8s deployments` for each container's env — a literal value inline, a `valueFrom` as its
  source. An assumption about configuration is checkable here and is wrong about a third of the time.
- Never construct a test that changes state. There is no state you are permitted to change, but the
  instinct to "just try it" is what the read-only grants exist to stop.
- **Check whether it is already open upstream, before you write it down.** Read access to the
  GitHub API is anonymous and needs no credential, so this works in every mode including
  report-only — where nothing else ever performs the check, because the filing turn that would
  otherwise do it never runs:

  ```
  curl -sS 'https://api.github.com/search/issues?q=repo:<the repository your brief names as the source>+is:open+<terms>'
  ```

  Use two or three distinctive terms from the mechanism, not the whole title. A hit does not delete
  the finding: record it anyway and put the issue or pull request number and URL in `evidence`,
  because a second sighting is what tells a maintainer the open item is still live. Unauthenticated
  search allows 10 requests a minute against a shared address, so spend it on findings you have
  already confirmed rather than on every hypothesis.

## 6. Write findings.json — early, and again after every finding

The file is the channel out of the run, and nothing you are still holding when the turn ends
survives it. If the turn is cut off before you write, the runner salvages a fenced JSON array out
of your final response — a backstop for a truncation, not a second place to put the answer.

- **Write it before you think you are ready.** Write `[]` at the end of phase 1, and rewrite the
  whole array each time you confirm a finding. Your iteration budget is finite, you get no warning
  as it runs out, and a turn cut off part-way is reported as a clean run that found nothing. Two
  confirmed findings on disk beat a better list you never reached.
- One object per distinct problem. Two symptoms of one cause are one finding.
- Titles and locations are stable identity — see SOUL.md §4. Get these right or the counts never
  accumulate and nothing is ever promoted.
- `evidence` is an array of verbatim strings with timestamps, plus the query that produced each.
  Paraphrased evidence is not evidence. Quote in full: identifiers are redacted for you on the way
  out, both by the evidence tools and again before the finding is stored, so hand-redacting only
  costs the reviewer detail.
- `occurrences` is how many times you saw it in the window you looked at, and it defaults to 1.
  It changes nothing about whether the finding is promoted — the gate counts runs, not this — but
  it is quoted in the pull request, so omitting it on something you saw forty times understates it
  by a factor of forty.
- Grade against the SOUL.md §3 rubric, not against how much work the finding took to find.
- When the investigation is done, confirm the file holds what you mean to hand back, then stop.

## What not to report

- Anything in a cluster under management, or in a user's GitOps repository. That is the Platform
  Agent's work.
- Your own pod's logs and traces. They are filtered by default; do not go looking. This excludes
  your telemetry, not your source: `agents/selfimprove/` is kube-agents code like any other and a
  defect in the runner, the ledger, the evidence CLI or these two skills is a finding worth writing.
  Grade it on what it costs the same way, and do not soften it because it is yours. One class is
  reportable but never fixable by this loop: a change to its own gate, ledger or grants, which the
  filing turn refuses at any severity. Such a finding's whole job is to sit in the ledger where a
  maintainer reads it, so write it anyway and write it well — that entry is the only way the
  problem travels.
- A `Warning` event that is a normal part of an operation that then succeeded.
- The Slack connect timeout at pod boot — expected, and the relay handles it.
- A style preference in the source with no evidence attached to it.
