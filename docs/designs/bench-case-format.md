# The bench case format

A case is one directory under `bench/tasks/` holding one `task.yaml`. This document is
the contract for that file: which fields exist, which of them the runner honours, which
the repository's own lints honour, and how to decide whether a claim about a run belongs
in an exact check or in the judge's prose score.

The format is executable today and most of it is already written down in
`bench/tasks/compliance-rbac-overgrant/task.yaml`, which annotates nearly every decision
this document generalises. Read that file alongside this one. `bench/CUSTOM-TASKS.md` is
the step-by-step for writing a case, including the Terraform stack and the agent harness;
where the two touch, this document is the rule and that one is the procedure.

## Who parses what

Two different readers consume a `task.yaml`, and they honour different fields.

**devops-bench** parses it into `devops_bench.tasks.schema.Task`, pinned to the SHA in
`bench/pyproject.toml`. Its model config is `ConfigDict(strict=True, extra="ignore")`
(`devops_bench/tasks/schema.py:25`), so a field the model does not declare is discarded
in silence — no error, no warning. The fields it reads are `id`, `name`, `prompt`,
`expected_output`, `retrieval_context`, `chaos_spec`, `verification_spec`,
`recoverable_safety`, `infrastructure`, `documentation` and `validated`. Strict mode
means no coercion: `critical: yes` is a string, not a boolean, and fails validation.

**This repository's lints** read the same file for fields devops-bench discards: `domain`,
`fixtures`, `owner`, `expected_fail`, `tool_calls_optional`, `audit_streams` and `scope_exclude`. Those are ours. A typo in any of them cannot fail a run, which is
exactly why `scripts/validate_bench_cases.py` exists.

## The id key

Use `id:`. It is the field name on the schema, and it is the one every case in the tree
uses — all but one already did, and that one was renamed rather than grandfathered, on
the principle in "Changing the format" below.

`Task.from_dict` accepts `task_id:` as an alias — it reads `raw.get("id")` first and
falls back to `raw.get("task_id")` when that is absent
(`devops_bench/tasks/schema.py:198-200`) — so both spellings load, and a file carrying
both silently loses the `task_id` value. The alias is not a second supported spelling in
this repository; it is upstream compatibility for other people's corpora. The validator
rejects `task_id:` in a case here.

The value must equal the directory name. The directory name is what
`hack/ci-eval-pr.sh` derives `TASK_NAME` from, what the results file is copied to, and
what every lint and every table in `bench/tasks/DRAFTS.md` keys on. An id that disagrees
with its directory means two names for one case and a search that finds half the
references.

## The fields, and what each one is for

`id` and `name` identify the case. `name` is prose for a human reading a results table;
`id` is the key.

`prompt` is what the agent is given. Write it as the user would write it, not as a
checklist — a prompt that enumerates the answer measures instruction-following rather
than the journey. `{{CLUSTER_NAME}}` and `{{PROJECT_ID}}` are substituted by the runner.
A first line of `[bench:slack-question-wake]` makes the run a replay of the wake a card blocked
on a question sends, rather than an ask
([`bench/kube_agents_bench/card_wake.py`](../../bench/kube_agents_bench/card_wake.py)); a replay
sends its turns to the agent's API server under either `AGENT_TRANSPORT`, as a card's wake reaches
the front door there under `spec.mode: next` too. A `session: fresh` answer carries today's Slack
thread context, which next's door does not send, so a case with one stays in
`hack/eval/inject-lane-exclusions.txt`. Under next the reply to a wake is posted nowhere, but the
same front door writes it, so a case that grades that reply still runs on the inject lane. A
first line of `[bench:card-block-wake]`, with `title`, `kind` (`transient`, `capability` or
`dependency`) and `reason`, plants a card blocked that way instead: nothing is posted, no answer
is sent, and the wake's reply is the run's one turn. A
first line of
`[bench:stop-after-first-card]`, optionally with `observe=<seconds>`, makes the rest of the
prompt an ask the harness stops once it has filed a card
([`bench/kube_agents_bench/stop_hook.py`](../../bench/kube_agents_bench/stop_hook.py)); it runs
only over the inject transport, so the case needs an entry in
`hack/eval/api-lane-exclusions.txt`. The harness writes two entries for such a case,
`harness_stop_sent` and `harness_cards_stopped`, and the case's `tool_called` objectives name them.
A first line of `[bench:reply-under-pushed-report]`, with `opening:`, `report:` and `reply:`
lines under it, sends the opening as turn one, stores the report for the conversation in the
agent pod with the row the kanban notifier writes for a card's report inviting a reply that it posted
into a Slack or Google Chat gateway conversation, then sends the reply as a second message on the same conversation
([`bench/kube_agents_bench/pushed_report.py`](../../bench/kube_agents_bench/pushed_report.py));
the reply turn's answer is the deliverable, and the harness writes `harness_pushed_report_stored`
for the plant. Inject transport only, so the case is listed in `hack/eval/api-lane-exclusions.txt`
as well.

`expected_output` is the judge's reference. It feeds `OutcomeValidity` and, where the
case declares `documentation.constraints`, `ChecklistScore`. It never gates a case that
carries a `verification_spec` (see the gate, below), so write it for the reader of a
trend line rather than for a merge decision.

`infrastructure` selects the deployer. `deployer: noop` provisions nothing and costs the
presubmit seconds rather than a cluster; `deployer: tofu` names a stack under `bench/tf/`
and costs a multi-minute provision on every pull request. `hack/ci-eval-pr.sh` reads
`infrastructure.deployer` out of the file text to classify a crashed run, so keep it on
one line.

`documentation` carries reference documents and the per-constraint `critical` flags the
checklist metric scores. It is judged, not exact.

`validated` marks a case as vetted. It defaults to `False` and stays there until someone
has watched the case both pass and fail.

`domain` is a slug from `docs/designs/domains.yaml`. It is how coverage is counted, and
it is discussed below in its own right.

`fixtures` is a list of fixture role slugs from `bench/tf/fleet/fixtures.json` — the
seeded defects the case depends on. (`docs/designs/fleet-fixtures.yaml` overlays the
day-N gates and the project-scoped fixtures on that catalogue, and contributes the roles
that have no cluster slot; it may not rename one.) It is documentation the validator
enforces: it makes a case's fixture dependencies greppable, so the fleet owner replacing
a cluster knows which cases go quiet. The same slugs name a `fleet_resource_property`
check's `fixture_role:`, and a check naming a role the case does not list is rejected —
one planted defect, one name, however the case refers to it. Cases address fixtures by
role and never by cluster name or project id; `docs/designs/bench-fleet-catalog.md` is
the contract for why.

A case whose spec reads live cluster state must declare it. A check that reads the
agent's own install rather than the seeded fleet (the `bootstrap_*` checks,
`sandbox_tree_matches_image`, `findings_item_state`, `oobe_audits_started`, `relay_posted_as_written`, `audit_chat_report_posted`, `audit_report_stored`, `report_store_reply`, `workload_incident_folded`) is not a fixture read, and a case carrying only those needs
no `fixtures:`. `fixtures: []` is the declaration for a case that plants its own state — `gpu-stress-test-diagnosis` brings up
its own Terraform stack and depends on no fixture — and an absent key on such a case is a
finding, because a grep that returns one case for a role has to mean one case uses it. A
case that depends on a slot's shape rather than on a plant (every seeded cluster is zonal,
say) names one role per slot it reads and grounds the objective that needs every slot with
`report_contains`'s `fixture_roles`, saying in a comment that the roles stand for their
slots; the grep then still finds it when a slot's cluster is replaced.

`owner` is optional: the person whose bugs the case is first, falling back to the domain's
owner. The demotion mechanic in `docs/eval-gate-roster.md` addresses its issue to that
effective owner, and [`bench/CONTRIBUTING.md`](../../bench/CONTRIBUTING.md#ownership) states
the rule.

`expected_fail` is the eval-driven-development marker, for a case whose fix is not yours: the
case lands red so the gap is on record, and `bench-gate` reads it inverted — failing is the
declared outcome, and passing every repetition reds the job until the marker is flipped. It
defaults to `false`, a case for your own change never carries it, and it must be a bare YAML
boolean: `"false"` is a string, and truthy, and the validator rejects it.
`.agents/rules/eval_driven_development.md` has the rule; devops-bench ignores the key.

`tool_calls_optional` is for a case a correct agent can answer without calling a tool: a
first-turn greeting, a reply that names a command. `bench-gate`'s rung 3 otherwise reads an empty
trajectory as no agent having run; with the key set, a non-empty reply on a record with no error
stands in for the trajectory, and the token, latency and status signals still apply
([`eval-scorer.md`](eval-scorer.md)). It does not forbid tool calls. It defaults to `false`, must be
a bare YAML boolean, and devops-bench ignores it.

`audit_streams` is for a case whose stack starts real audit runs without grading their
ledger, or whose agent runs a stream's collector outside a ledger run and so shares that
stream's scratch state (`upgrade-chat-request-runs-collector`): a list of the Platform Agent
job ids (`audit_streams: [compliance-audit, stockout-prevention]`). The runner holds each one's stream
lock for the whole unit, beside the stream a `ledger_issue_contains` check names
(`hack/ci-eval-pr.sh`, `task_streams`, reading the key with the YAML parser the lint uses), and
releases them when devops-bench returns. A run that outlasts the unit is covered by the next
unit on the stream, which waits for it first (`wait_platform_runs`), so an audit case does not
run beside the case's runs and grade their ledger. `oobe-first-run-audits` carries the four it
starts. devops-bench ignores the key.

`scope_exclude` is for a case whose precondition is an install that leaves some seeded
clusters out of its scope: `scope_exclude: {slots: [b]}` names catalogue slots, never
cluster names, so the case runs unchanged on every pool project. The runner
(`hack/ci-eval-pr.sh`, `scope_exclude_apply`) runs the case's repetitions alone, after every
other unit, because a scope edit rolls the gateway and the Cluster Agent reconcile deletes an
excluded cluster's profile. Before them it adds each slot's cluster, read from the fleet's
`.fleet-context`, to the PlatformAgent's `spec.scope.exclude.clusters`, waits until the pod
reads the new scope, and runs the reconcile once. After them it puts the scope back and runs the
reconcile again to re-create the profiles. A CR that had no scope block first gets a block that
excludes nothing, and loses it only after a reconcile has run under that block: the reconcile
reads a missing block as "keep the last exclusions". The scope it replaced is saved on the CR
(`bench/kube_agents_bench/scope_exclude.py`), so the EXIT trap and the next lease can restore it
after a run that died first; the annotation stays until a reconcile has run under the restored scope. A repetition whose exclusion did not take effect is recorded as
infrastructure. `absent: <n>` beside the slots (at most `MAX_ABSENT` in `scope_exclude.py`) also excludes n clusters that do not
exist, named like the first slot's cluster with a letter past the catalogue's last slot, in its
project and location, so a case can exclude as many clusters as the install manages; the
reconcile prunes nothing for them. The validator refuses a slot the catalogue does not declare and a list that
excludes every slot. devops-bench ignores the key.

The runner pauses most of the Platform Agent's clock-started audits at lease time and again
right before the fan-out (`scheduled_audits`, #3020), so their scheduled runs can't write to the GitOps
repository in the middle of an eval and be charged to whichever cases are running. It leaves the
four the `oobe` stage starts (the stage holds a paused audit rather than starting it), the
`no_agent` jobs, and any job the image doesn't ship. Those four still run on their clock, so a
write from one of them mid-eval is still possible. An audit case starts its own run from its
prompt, which a paused schedule doesn't affect. A pause that still fails after its attempts
stops the run as an infrastructure failure rather than letting a scheduled audit's writes be
charged to the cases.

`verification_spec` is the exact half of the grade, and the rest of this document is
mostly about it.

## Exact check or judged score

The test is who chose the words.

We planted the noun, so a match is fair and it blocks per run. `debug-binding` is a name
the fleet's Terraform wrote; a correct audit cannot avoid saying it, and an incorrect one
cannot stumble into it. Assert it exactly, and let one red run block one merge.

The agent composed the sentence, so it gets judged and blocks only as a distribution.
Whether the remediation advice is any good, whether the post-mortem reads coherently,
whether the explanation of the privilege-escalation surface is right — nobody planted
those words, there is no exact string that is fair to demand, and a model grading a model
is a noisy instrument. Record the score, watch the trend, and do not red a build on one
sample of it.

Two corollaries fall out. A phrase the agent might legitimately spell two ways is still
exact — that is what `any_of_phrases` is for (`HPA` or `HorizontalPodAutoscaler`), not a
reason to hand the claim to the judge. And a phrase that a correct run can reach without
doing the work is not exact enough to block: the compliance case moved `cluster-admin`
off the ledger body and onto `scope: finding_ids` precisely because the body's evidence
table restates every check slug the run declared, so the phrase was in every ledger
including an empty one.

## The shape of a verification entry

Each entry is a named, scored unit
(`devops_bench/verification/spec.py`, `VerificationEntry`), and the vocabulary is small:

`name` labels the entry on the result record. Names must be unique within a spec; a
duplicate is dropped with a parse error.

`role` is `objective` or `safeguard`. An objective is a thing the run was supposed to
achieve. A safeguard is a thing that must never have happened. Objectives roll up into
`VerificationCorrectness`; safeguards roll up by severity.

`severity` is required on a safeguard and forbidden on an objective. `catastrophic`
means one failure fails the case outright — the gate reads it as a boolean.
`recoverable` means contained and reversible, and rolls into a weighted fraction.

`mode` is `converge` or `assert`, defaulting per role: objectives converge because they
describe a state the run is working toward, safeguards assert because polling a violation
would only wait for it to heal. Override it to `assert` for any check whose subject is
immutable once the run ends. Every transcript-reading check is in that category — the
report does not change after the last turn, so a converging one just burns the timeout
before failing. `hold` parses but is rejected: it is not built.

`weight` scales an entry's contribution within its role. Default 1.0, must be positive.

`check` is the check subtree. It carries a `type` discriminator and the type's own
fields, and it may be a compound node: `sequence` (ordered, fail-fast), `parallel` /
`all`, `any`, and `none`. `none` is how a safeguard says "this never happened".

## The check types

Three read the cluster, from devops-bench: `resource_property` (a JSONPath property of
matched objects, with `op` one of eq/ne/gt/gte/lt/lte/exists/absent/contains/matches),
`pod_healthy` (pods matching a selector reach Ready), and `scaling_complete` (a
deployment's ready replicas land in a range). A fourth, `fleet_resource_property`, is
this repository's `resource_property` against the seeded-fleet cluster that carries a
fixture role, named by `fixture_role:` rather than by cluster.

Fourteen read what the run produced, from this repository
(`bench/kube_agents_bench/verifiers.py`, registered through the
`devops_bench.verifiers` entry-point group in `bench/pyproject.toml`):
`report_contains` (phrases in the agent's answer; its `forbidden_patterns`, `required_patterns` and
`any_of_patterns` are regular expressions searched against a line-preserving text, with each
line's decoration folded when the check sets `fold_decoration: true` (indentation, bullets,
numbers, headings, quotes, links, a trailing stop or an affirming mark; a mark that hedges or
negates the last word stays, so it reads as a wrong value), so a pattern anchored at both ends
spells a declared line once and should keep `\n` out of its gaps (a literal space does not cross a
line break; a phrase that may wrap says `\s+`), for what no substring can
express: a banned word whose negated uses are legitimate, a value only the work produces, such
as a 32-hex trace id, where the words around it are the prompt's own and a report of failure
would echo them (`required_patterns` must each match), a phrase that must start at a word
boundary (`any_of_patterns` are the regex alternatives to `any_of_phrases`, one pool: at least one
of either must match), and a required claim whose subject and verb an adverb or a tense can
separate; `{cluster:<slot>}` in any of the three lists stands for the
cluster the runner recorded for that slot, bare or as the last `-` or `/`-joined component of a longer id (a
kubeconfig context's `_` joins only under `fold_decoration: true`, which makes it a `-`; without the fold the
underscore is deleted as emphasis and the id has no boundary before the name) and
optionally followed by its recorded location, bounded on both sides by the expansion itself (so
`unseeded-a` and `seeded-a-canary` are never slot a, whatever surrounds the placeholder), and
`{cluster:any}` for every recorded slot, so a case names which cluster rather than what a cluster's
name looks like; a placeholder naming a slot the runner recorded no cluster for returns
`status: "error"`, like an unreached `fixture_roles` slot, and because an unreached slot is absent
from `{cluster:any}` rather than forbidden, a case that forbids through it lists a `fixture_roles`
entry for every slot it means; its `fixture_roles` names the seeded-fleet
roles whose clusters the patterns require a line about, each resolved to its slot's own
credential (`clusters/<slot>.kubeconfig`, written for every seeded cluster the runner reached,
before any role on it is confirmed), and a slot the runner did not reach returns
`status: "error"`, the cluster being absent from the project rather than missed by the agent), `report_manifest` (the fenced code blocks in the same text, parsed as YAML: one document of `kind` carries every `fields` value and a label selector that selects every `selects` set and no `excludes` set), `answer_first` (each delivered card result, raw:
it opens on one bold sentence, has no heading, stays
under its character and sentence caps, and no later sentence matches a
`recap_patterns` regex; each of the case's `lead_terms` appears in at least one result's
lead), `report_times_in_window` (every date and time on a report line one of its
`label_patterns` regexes matches falls between the run's start and the check, each widened by
`max_clock_skew_sec`, and its minute must be one some tool output in the trajectory shows,
the workers' included; a report that states none passes), `tool_called` (calls in the
trajectory), `ledger_issue_contains` (the GitHub ledger issue a fleet audit
published), `pull_request_opened` (the remediation pull request the run opened,
resolved through GitHub and required to be this run's rather than an earlier
repetition's, unless the case sets `accepts_stream_pull_request` and runs on an audit stream, which also admits one an earlier run on its audit stream opened in the job's GitOps repository on that audit's remediation branch; `unchanged_paths` also fails one whose diff changes a listed path, `changed_paths_only` fails one whose diff changes any path matching none of its listed globs, `removed_paths` one that does not delete every listed path, and `content` asserts over the YAML or text in the files it adds), `github_writes` (every pull request or branch under the agent's
prefix written to the case's GitOps repository since the repetition started that
the repetition's reply, or its or its workers' call that created the write, names
(a read's arguments do not count), or every one when its window is marked exclusive; it passes on a write, so
a case wraps it in `none` to say the agent wrote nothing it was not asked for,
and both lanes append exactly that entry to every case they run), `worker_commands` (regular expressions over the terminal commands
the delegated workers ran, read from the worker log of each card the front agent filed before the harness
purges it and, for any card with no log line (the cards those workers filed in turn, or a filed card that ran only code), from its tagged `terminal` and `execute_code` calls in the trajectory, and with `after_result_pattern` set from every card's tagged calls as typed, beside the logs when the check has no forbidden set; each of its `exempt_patterns` removes the text it matches from a
command, a log line or a tagged call alike, before the forbidden list reads it, and nothing else, and takes a
command it touched out of the required list, for a command that can fetch and
send nothing but carries a forbidden word as its argument, such as a plain
`grep` exempted through its pattern argument so that whatever else shares the
line is still graded; or with `required_runs` a script by its basename and the flags that must
follow it on the same shell segment, read from the command's argv as the shell splits
it, so a quoted mention of the invocation is one word and not a run, and a run made through a script file or a shell's standard input is not read, since the argv names the file or the shell; each flag is read as the exact word, so the script named is built with `allow_abbrev=False`; `word_patterns` are regular expressions each of which some later word of the segment must match on its own, and `forbidden_runs` is the same shape for a run no worker may make, such as a `kubectl get` of the objects a script grades; with `after_result_pattern` the commands come from the workers' tagged `terminal` and `execute_code` calls in the trajectory instead, with their results as the record stores them, a JSON string with newlines escaped, and the forbidden set applies only to the calls after the first whose result matched, failed or not, one window per delegated card and the cards its worker fanned out; `forbidden_result_patterns` then grade the later results too; a trajectory with no tagged call at all, none readable (every code-tool argument withheld), a call with no numeric time, or a capture with gaps and no hit, is `status: "error"`, where a worker whose tagged calls are all MCP calls grades as zero commands), `worker_agents` (regular expressions every one of which must match the
profile at least one delegated worker ran as, read from the tags the harness puts on the
workers' trajectory entries), `worker_budget` (the turn budget a delegated worker ran under,
read from two records: Hermes' one-shot checkpoint notice, `You have used N of M iterations`,
which a kanban worker gets in its newest tool result at 90% of a finite cap and the worker
capture carries in the tagged call's `result`, and the run row a worker that goes on to exhaust
the cap closes with `Iteration budget exhausted (N/M)`; `budget` is the `M` every such record must
name; a worker that finishes inside 90% of its cap records neither, so the check is for a card
written to outrun the cap; a run whose cards have no run rows, in which no attempt
ran out and none completed either (the harness archives a card still running at its delegation
ceiling, which closes the attempt `reclaimed`), or whose capture recorded gaps and no attempt ran
out, is `status: "error"`),
`replay_card` (the status and comments of the card a
card-wake replay planted, read before the harness archives it), `reply_is_silent`
(whether the gateway would post the closing message, or with `reply: answer` a question
replay's reply to the answer turn, at all, by its own silence rule), and
`verified_claims_backed` (at least `min_blocks` evidence blocks in the answer, each a `$` command
fence and then an output fence, as `kanban_complete` renders `verified_claims`; each command must
be a worker's tagged `terminal` or `execute_code` call and each excerpt must be in that call's output, compared with
escapes undone, addresses masked and whitespace collapsed, on word edges, and an excerpt under
8 characters must be the whole output; an excerpt past the pod's clip or in an output it redacted
is not checked, and a missing command is
`status: "error"` when the capture has gaps, the pod withheld the workers' arguments, or the
trajectory holds no worker terminal call).

Thirteen read the install under test, nine of them from that file. `bootstrap_fanout` compares the
cluster cards filed for the onboarding discovery sweep, read from the agent pod's board, against the
Cluster Agent profiles on its disk. Its `require` is `one_card_per_cluster_agent` (exactly
one card per ready profile with a cluster identity, keyed and assigned to it, and no cluster
card for anything else), or, with `cap`, one of the first-run cap's two (#3702):
`cards_for_first_cap_profiles` (cards for exactly the carded profiles: the sweep marker's
`carded=` set when it has one, else the first `cap` profiles by name) and
`past_cap_clusters_sweep_audited` (every roster cluster outside that set shown in the sandbox's
`INVENTORY.raw.md` as audited by the sweep itself).

`bootstrap_handoff` reads both ends of the sweep's hand-off to the prioritization stage. Its
`require` is `raw_report_has_findings_block` (the sandbox's own `inventory_findings.py` parses
`INVENTORY.raw.md` there, and every cluster the writer's own `finding_lines` lists from the
completed cluster cards has a block line) or `ranking_card_filed` (the newest unarchived card keyed
`bootstrap-inventory-prioritize` filed at or after the sweep carries the hand-off's own body and is
not `blocked`, `triage`, `failed` or `cancelled`) or `raw_report_no_first_run_audit_checks` (the
same parse, and no block line carries a check from the roster of one of the four audits the `oobe`
stage runs after the scan).

`bootstrap_findings` reads the shell sandbox of the install under test:
`INVENTORY.items.json`, which the onboarding prioritization stage's `inventory_findings.py
extract` writes through the worker's terminal. It passes when those items carry exactly the
`(check, object)` pairs listed in `expected_findings`. `bootstrap_report_read` reads the
sandbox too, and passes when onboarding's delivery job has claimed the ranked report
(`.bootstrap_completed` on the agent pod) and renamed the sandbox's `INVENTORY.md` to
`INVENTORY.delivered.md`, which it does after reading it. `bootstrap_report_criticals` reads
that report and the worker's `INVENTORY.scores.json` there, and passes when the report's numbered
items name exactly `limit` of the batch's critical items and none of its non-critical ones; a worker that scored `limit` or fewer
critical is an error, since such a run cannot tell a capped report from one listing every
critical. `bootstrap_delivered` reads the
agent pod's `cron/executions.db` instead and passes when the delivery job's run that claimed
the report completed, which is the condition for the scheduler to post what it printed.
`sandbox_tree_matches_image` execs into the agent's shell sandbox Pod and diffs the image's
staged skills, scripts and governance against the copies the sandbox runs, so a case can
grade an edit to them by its effect. A stack that runs Python in the sandbox does it as
root (`kubectl exec`), so the image sets `PYTHONDONTWRITEBYTECODE=1` and such a run leaves
no `__pycache__` in the reference tree for this check to read as an edit. The check
still flags a cache file that appears anyway, since a `.pyc` is code. `findings_item_state` reads the findings queue through the
Session KV server in the agent pod and passes when every row in `finding_ids`, under `project`,
is in `state`. A listed row the queue does not hold is `status: "error"`, since it means the
case's plant is missing. `audit_report_stored` reads the fleet-audit report store in the shell
sandbox, then in the agent container, and passes when a `latest.json` for its `audit` finished since
the run started, with `status` when it gives one and, for each `required_finding_phrases` entry, a
finding in its `document` whose id contains it. `report_store_reply`, in its own module
(`bench/kube_agents_bench/report_store_reply.py`), reads the same store after the reply with the
install's own `report_status.py`, and passes when each of its `audits` is mentioned in `final_message`
and the reply carries the ledger URL of one of the audit's envelopes written before the turn (the
newest, per repository store) or during it. An audit whose stores need no URL (a clean run with no
open ledger, or no record before the turn) passes on the mention alone; which audit a URL is given
to is the judge's to grade. `oobe_audits_started`, in its own module
(`bench/kube_agents_bench/oobe.py`), reads the stack's state file and the Platform Agent's
`cron/executions.db` in the agent pod, and passes when the stage's `.oobe_audits_fired` lists each
of the four first-run audits as marked due and each has a run claimed since the stage marked it
that got going (running, completed, or ended after its start), each starting only after the one before it in the chain ended.
`workload_incident_folded`, in its own module (`bench/kube_agents_bench/incidents.py`), reads a
workload's rows of one object `kind` (default `Pod`) from the Session KV server's event ledger in the agent pod and passes when at least
`min_folded` of them were folded into its newest admitted incident, which must be newer than the
run's start less `max_age_sec`; it keys on that incident rather than a time window because the stack
plants its burst before the turn starts.
`relay_posted_as_written`, in its own module (`bench/kube_agents_bench/relay.py`), reads the
`relay-report` stack's state file and what the Session KV server stored for the report's threads,
and passes when the route answered `delivered` and every stored post is the report exactly; a
route that delivered but stored nothing fails, and one whose send registered no thread (outcome unknown) is an error.
`audit_chat_report_posted`, in its own module (`bench/kube_agents_bench/audit_chat_report.py`), reads
the `audit-chat-report` stack's state file, what the Session KV server stored for the fired audit
job's threads since the fire, and the chat report the run's `finish` left on the shell sandbox, and
passes when the stored post, the scheduled delivery (the table keeps the latest post per thread, so a mid-run `report_to_chat` is not graded), is that report; a run that left no report from `finish`,
whose record predates the fire, or that did not finish every repository it started fails, and a
silent run or one that never ended is an error. `triage_starts`, in its own module
(`bench/kube_agents_bench/triage_starts.py`), lists the pods in its `namespace` on the agent's cluster,
joins the Session KV server's admitted `intercepted_events` rows for them (by pod UID) to the default
profile's Hermes `state.db`, and passes when at least `min_alerts` pods have an admitted alert and every
one of those alerts has a session with a message, a started triage turn; a session with no message is a
lost start, because a create the daemon gave up on still commits, and fewer than `min_alerts` admitted
alerts (the plant failed, or the daily Warning alert limit refused part of the burst) is an error.

Two limits are worth knowing before choosing one. `tool_called` defaults to
`scope: router`, the delegating turn's calls only — the harness appends the delegated
workers' calls to the trajectory tagged with the profile that made them, and the default
scope skips every tagged entry, so a check written before the workers' calls were recorded
still means what it meant. `scope: workers` counts the tagged entries instead and is the
one deterministic check that sees which MCP tool a worker reached for — a worker invokes
MCP tools through Hermes' `tool_call` wrapper, and the check reads the tool names inside
it; `all` counts both.
Either non-default scope returns `status: "error"` on a trajectory with no tagged entry (no
card delegated, or the capture did not run) rather than passing on an absence. A
cluster-mutation safeguard is still a cluster-state check (`resource_property`): a call the
worker made is evidence of intent, the object's state is evidence of effect.
And `report_contains` defaults to `scope: final`, the answer the user receives. `full`
also matches a phrase the agent merely quoted in progress chatter, which passes a
required phrase that was never reported and false-fails a forbidden one that only appears
in quoted material. `worker_commands` reads the route a worker took through its terminal
commands, not its MCP tool calls, and only for cards the run delegated (a card a worker
filed is read from the trajectory, as far as the capture reached it) — a router that
answered without delegating leaves it nothing to read, which is `status: "error"`, not a
pass. For a card the front agent filed it reads the one-line rendering hermes writes to the card
log, not the command as typed: a newline becomes a space, a chain joined by `;`, `&&` or `||`
arrives as its first command plus ` + N command(s)`, and a redirection is dropped, so a pattern
never sees a command chained behind another, and the quotes stay, so a listed word the command
splits with an empty quote inside it is one word to the shell and none to the pattern. For a card a
worker filed it reads the tagged call, which is the command as typed, so a `^`-anchored pattern
written against the rendering can miss that card's command behind a `cd … &&` or on a second
line, and a capture gap on that card does not change the result (#3406). `worker_agents` reads only which profile made the workers' tagged entries, and the
judged metrics receive those entries as the execution trace.

Every one fails closed. A check that cannot observe its subject returns `status: "error"`,
never a pass and never a fail, and an errored check drops `VerificationCoverage` below
1.0, which the gate fails. Silence is not a pass.

## What actually reds a build

`hack/ci-eval-pr.sh` runs one devops-bench invocation per entry in its task matrix —
`hack/eval/presubmit-cases.txt`, plus `hack/eval/nightly-cases.txt` when the job exports
`EVAL_TIER=nightly` — and grades the resulting record. For a case that carries a `verification_spec`, three keys
decide the merge:

| Key                        | Gate                           | Meaning                                    |
| -------------------------- | ------------------------------ | ------------------------------------------ |
| `VerificationCatastrophic` | `1.0` if present               | No catastrophic safeguard tripped          |
| `VerificationCoverage`     | must be present and `1.0`      | No declared check errored or failed to run |
| `VerificationCorrectness`  | must meet the floor if present | Weighted objective pass fraction           |

Only coverage fails on absence. A spec with no catastrophic safeguard emits no
`VerificationCatastrophic` key and that is fine; a spec whose entries all errored emits no
correctness key either, and coverage is what catches it.

The floor is `DETERMINISTIC_CORRECTNESS_FLOOR` in the same script, `"1.0"` today: every
declared objective is meant to hold outright. `OutcomeValidity` and `ChecklistScore` are
recorded on every run and gate nothing on a spec-carrying case — a judged score that
drops is a trend to read, not a merge to block. `VerificationRecoverable` is emitted but
is not in the gate.

Three shapes of absence are failures rather than green:

A case that declares a spec and produces none of the three keys fails. The script
re-reads the file to tell "declared a spec, metric crashed" from "declared none", and
errs toward "spec present" when the read fails, because the fail-closed direction is the
one where a broken metric cannot slide back to the judge.

An errored check fails, through coverage. `rollup` (`devops_bench/verification/rollup.py`)
excludes an errored entry from both numerator and denominator of every signal, so an
all-errored spec would otherwise emit no correctness key at all; coverage is emitted
unconditionally and is what catches it.

A spec entry that does not parse fails. Each parse error adds weight 1.0 to the objective
denominator with no numerator contribution — a spec that never parsed might have declared
anything, and the conservative reading is an unmet objective.

## Every case declares a domain

`domain:` is a slug from `docs/designs/domains.yaml`, and it is how coverage is counted.
`scripts/test_domain_coverage.py` treats a domain as covered when some case claims its
slug, carries a non-empty spec, and is a name on `hack/eval/blocking-roster.txt` (a presubmit
seat held out of the roster covers nothing).
Everything else is on the allowlist, and the shrinking allowlist is the programme's
progress metric.

A case with no slug is therefore invisible to that count. It can be green for months
while the coverage report shows the domain it actually exercises as a gap, and nobody
finds out until someone writes a second case for a domain that already had one. So a case
declares a domain.

There is no exemption. A case no row describes gets a new row in `domains.yaml`, with
the `owner:` who answers for the domain's cases as a set, a different `backup:`, and a
one-sentence `meaning:` a reader outside the code understands; `scripts/test_domain_coverage.py`
fails a row missing any of them. A new row with no blocking-roster case starts on that file's allowlist, as a record with `since`, `issue` and an `expires` date (the comment above `allowlist:` describes the fields).

## Every case declares a verification spec

`hack/ci-eval-pr.sh` falls back to `OutcomeValidity >= 0.7` for a case with no
`verification_spec`. That fallback is transitional, and the script says so in the comment
above the branch that implements it: once every presubmit entry carries a spec, it is
dead code to delete.

Declare the spec as a block, not inline. The script decides whether a case has one by
grepping for a `verification_spec:` line with nothing after it, so a flow-style spec on a
single line loads, runs its checks, and is graded by the judge-only fallback regardless.
The validator rejects that shape.

Transitional means new cases do not use it. A judge-only case cannot fail for the reason
it was written — it fails when the model has a bad day and passes when a plausible-sounding
answer names nothing in particular — and 0.7 on a five-point rubric is roughly "did not
embarrass itself". Building the first corpus produced about fifty review findings, half of
them cases that could not fail or could not pass; judge-only is the shape most of those
took.

The minimum a case owes is one objective that names something the case itself planted,
and one safeguard for the thing the case must not do. Both can be written before the
fixture exists — the `bench/tasks/DRAFTS.md` corpus did exactly that, specs first,
parked, activated later. Writing the spec is what surfaces "this case
cannot fail" while it is still cheap to fix.

Where the exact check genuinely does not exist yet, the escape is the same shape as the
domain one: a reviewed entry in the validator's `KNOWN_JUDGE_ONLY` map, carrying the
reason and what would close it. An entry there is a debt with a name on it.

## Registration

`hack/ci-eval-pr.sh` runs the cases in `hack/eval/presubmit-cases.txt` — plus, when the
job exports `EVAL_TIER=nightly`, those in `hack/eval/nightly-cases.txt` — and only those.
Cases under `bench/tasks/` are not discovered. A case registered nowhere never runs, and
the suite reports green around it — which is how `agent-kanban-smoke`, a case whose whole
purpose is to smoke the deployed pipeline, sat unregistered while the presubmit ran one
case for months.

A new case lands in the nightly file's sorted section, the last one, at its place in
case-id order with its reason as the `#` lines directly above its entry (#3495; the roster
test checks both, so two case pull requests don't edit the same lines). It lands there by
decision (2026-09-15,
[#1546](https://github.com/gke-labs/kube-agents/issues/1546),
[#1564](https://github.com/gke-labs/kube-agents/issues/1564)). It runs every night from
the night it merges, its record accrues in the evidence store (for a case in
`hack/eval/api-lane-exclusions.txt`, which runs only over the inject transport, under the
version key's `next` mode), and a presubmit seat is a
later pull request that moves its line to the presubmit file, adds its name to
`hack/eval/blocking-roster.txt` in the same edit (since 2026-09-22 the presubmit runs the
blocking roster and nothing else, and `scripts/test_eval_rosters.py` pins the two files as
equal less the held-out seats, `HELD_OUT_IN_PRESUBMIT`: the one exception, a coverage tracker
may first seat its candidate in the presubmit file without a roster line, to earn its record at
presubmit volume; it cannot red a pull request on quality there, only on rungs 1–3) and cites that record — never the pull request that makes it pass. Cases that stay in the nightly for good are
the ones kept out of the presubmit for cost, because a cheaper probe holds their presubmit
seat, or because what they grade is not one of the core journeys the presubmit gate is
for; the core journeys are the `journey:` rows of `docs/designs/domains.yaml`. A case the agent cannot pass
yet, or that a harness limit blocks, is a nightly case too: its record shows that, which
is what the record is for.

The commented-out registration — a `# ./tasks/<id>/task.yaml` line — is retired. It was
the parking state for a case whose fixture or blocker was not ready, and it was
indistinguishable from a case nobody had decided about. A `#` line in a roster file is a
comment, and the validator rejects a case path inside one. The one case that does not go
in a roster file is one whose fixture is not on the pool: it is a `FIXTURE_NOT_READY`
entry in `scripts/validate_bench_cases.py`, with the issue that plants the fixture. The
pull request that lands the fixture leaves it there, because merging puts nothing on a
cluster; the reconcile's postsubmit does (`docs/ci-pool-projects.md` 6.2). A later pull
request moves it to the nightly file, citing a reconcile report that visited every pool
project and a scan that reads the role healthy on all of them.

Who approves follows the split: an edit to the presubmit file or to
`hack/eval/blocking-roster.txt` needs an `eval-crew` approver (`hack/OWNERS`); the nightly
file and a new case directory need only the normal approvers. This section is the one
statement of the rule; `bench/CUSTOM-TASKS.md`, `bench/CONTRIBUTING.md` and
`docs/designs/testing-strategy.md` restate it and defer here.

## The validator

`scripts/validate_bench_cases.py` checks all of the above without a cluster, and
`make bench-case-check` runs it in about a second. It rejects a `task_id:` spelling or an
id that disagrees with its directory, a `domain:` that is missing or not in
`domains.yaml`, a `fixtures:` role the fleet catalog does not define (outside a `FIXTURE_NOT_READY` case, which names the role its issue plants), a cluster-reading
case that declares no `fixtures:` at all, a missing, empty or inline `verification_spec`,
a check that carries no assertion and so can only pass, an `owner:` that is `maintainers`,
written as a mention or not a login, an `expected_fail:` or `tool_calls_optional:`
that is not a bare YAML boolean, a `resource_property` or `fleet_resource_property` check that
compares a known boolean field such as `spec.suspend` to the string `"true"` or `"false"` under
`op: eq` or `ne` (the string never compares equal to the API's boolean, so the check asserts
nothing; the judged fields are the validator's `BOOLEAN_FIELD_PAIRS` table, and a path with recursive
descent or a script expression is not judged), an `audit_streams:` that is not a non-empty list or names a job the Platform Agent
does not have, a `scope_exclude:` that is not `{slots: [...]}` with distinct slot letters and an optional `absent` from 1 to `MAX_ABSENT`, names a slot
`bench/tf/fleet/fixtures.json` does not declare, or excludes every slot, a case that is registered nowhere, and a file that writes one key twice in
the same mapping, which PyYAML would load with the last value and devops-bench's loader
refuses (a key that overrides one merged in with `<<:` is not a repeat). It
also applies the entry vocabulary above — role, the severity pairing, the rejected `hold`
mode, a positive weight — which devops-bench enforces too, at spec-load time, after the
lease.

It also scans what the case brings with it: every text file under `bench/tasks/`,
`bench/tf/prebuilt/` and `bench/tf/modules/`, for an IPv4 literal outside the RFC 5737 documentation ranges or a
string matching the token-shaped subset of `AuditRedactor`'s patterns, with
`sanitizer: allow <reason>` as the per-line escape. `bench/CONTRIBUTING.md` is the home of
that rule — the ranges, the shapes, the skip list, the marker, and what review covers that
the scan cannot. The scan is tree-level rather than per case, like the fixture-catalogue
drift check, so `validate_all()` does not carry it; the lint asserts on
`sanitization_findings()` in its own right.

`scripts/test_task_registration.py` calls the same module in CI and asserts that it
returned no findings at all, so the fast local check and the gating lint cannot disagree.
That whole-set assertion is the load-bearing half, not the shared module. The lint spent
one commit asserting only on a hand-listed set of substrings, and every rule whose
wording was not on the list — a missing `id:`, a duplicate verification entry name, an
unknown `severity:` value, a check node with no `type:`, a file that does not parse to a
mapping at all, a dozen in total — was rejected by `make bench-case-check` and merged
green. A list of substrings is a second copy of the rule set; keep the assertion over the
set.

`make bench-case-check` itself is invoked by no workflow, and that is the intended shape.
The lint reaches the same `validate_all()` through `PYTHON_TEST_DIRS` in the `Makefile` and
`.github/workflows/python-tests.yml`, so a separate job running the target would re-derive
findings CI already has, on a second checkout, for nothing. The target is the pre-push
copy of the gate rather than the gate; a rule that has to be enforced goes in the
validator, where both reach it. Nothing should be added to the target alone.

"Carries no assertion" is stricter than "the field is present". `required_phrases: [""]`
is a populated list, and the empty string is in every text there has ever been, so the
check reads as an assertion and can only pass; the validator treats a blank phrase as no
phrase.

The two allowlists (`KNOWN_UNREGISTERED`, `KNOWN_JUDGE_ONLY`) both
work the same way: an entry names a case and carries the reason, and an entry whose case
no longer exists fails the lint.

### What it does not catch

A green `make bench-case-check` is not a promise that devops-bench will load the case. The
validator checks the rules above; it does not reimplement the harness's schema, and three
classes of mistake get through it and fail at spec-load, after a cluster lease is spent:

- **An extra or misspelled key on a verification entry or a check node.** The compound
  nodes in `devops_bench/verification/spec.py` are `ConfigDict(extra="forbid")`, so
  `requried_phrases:` is a hard rejection there and merely an unrecognised key here.
- **A missing required field on a check.** That surfaces as pydantic's `Field required`
  against the verifier's own model, which this validator does not have.
- **`critical: yes`.** This one the validator structurally cannot catch, and the reason is
  worth knowing. It reads cases with PyYAML, which is YAML 1.1 and parses the bare `yes`
  into the boolean `True`. devops-bench reads them with `ruamel.yaml`'s `YAML(typ="safe")`
  (`devops_bench/tasks/loader.py:41`), which is YAML 1.2 and parses it into the string
  `"yes"` — the exact value the strict-mode note above warns fails validation. By the time
  the validator sees the field, the trap has already been parsed away. The same split hides
  `value: yes` under a property check: PyYAML hands the validator a boolean and devops-bench
  compares the API's boolean to the string `"yes"`.

A fourth divergence is caught: PyYAML's `safe_load` keeps the last of two equal keys
silently, where `YAML(typ="safe")` raises `DuplicateKeyError`, so the validator loads with
a `SafeLoader` subclass that refuses a key written twice in one mapping.

The gap is deliberate rather than pending. `make bench-case-check` depends on PyYAML alone,
so it runs in a checkout with no `bench/` virtualenv and no harness install, which is what
makes it cheap enough to run on every edit. Importing devops-bench to close these three
would trade that for a second copy of a schema that already exists. Note the divergence
when writing a case; do not add the dependency.

## Changing the format

Whoever changes the format does the migration. Five people each fixing their own files
for someone else's schema change is how a one-hour change costs a week, and it is how
half a corpus ends up on the old spelling — which is the state the `task_id:` split above
records.
