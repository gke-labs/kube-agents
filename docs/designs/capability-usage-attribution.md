# Memory and model tokens by capability

**Status:** design, not built.

## High level summary

A capability is one job the agent does: a fleet audit, the stall watch, a chat question, and
similar jobs. This design measures the memory and the model tokens of each capability. It then
estimates the use and the cost of each capability for one month.

Today the install reports memory for each container and tokens for the whole install. No
measurement gives these figures for each capability. This design adds a capability name to each
model call and to each process. It samples both during a test run. It then divides the totals among
the capabilities. The division covers 100 % of the measured memory and tokens. The report lists the
five largest consumers and one line, Other, for the remainder.

Use is not even over time. The first-run flow of a new install and the weekly audits make a short,
heavy load. A normal weekday makes a light load. The report therefore measures two windows of one
test run:

- the burst window: about 1 hour from the install's start. The first-run flow and every audit
  run once.
- the soak window: the next 1 hour. The watchers run, and a few scripted chat questions are
  asked.

The test run is the nightly run on the sized test fleet, which the sized test fleet design
describes. This design adds the measurement to that run.

## Success criteria

Ten questions define success for this design and the sized test fleet design together. They are
numbered C1 to C10 in both. This design owns the seven below; the sized test fleet design owns C5,
C6 and C7 (container headroom, every audit finishing with no crash, and the time to a new install's
first reports). A criterion is met when the report answers it for M on 7 nights in a row and for L
on 4 weeks in a row, from runs the two checks under The report mark sound.

| #   | Question                                                           | The report shows                                                                                                                                                                                            |
| --- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C1  | What does kube-agents cost a month on an S, M or L fleet?          | For each size: the monthly model-token cost and memory cost from the monthly estimate, beside the test fleet's own infrastructure cost from the run's cost record, kept apart.                              |
| C2  | What does each capability cost, a run and a month, at each size?   | The table for each capability: memory and tokens a run, the cost of each unit of work it starts, the month, and the cost a cluster, with a row for each size.                                               |
| C3  | Which five capabilities use most of the memory and the tokens?     | The overview tables for each size and window: the five largest, Other, the platform base and Unattributed, adding up to 100 %.                                                                              |
| C4  | How does a new install's first hours compare with a normal day?    | The burst window's and the soak window's totals and rates an hour, side by side for each size, and the first-run flow's one-time cost.                                                                      |
| C8  | Which capability grows faster than the fleet?                      | The flag on each capability whose cost a cluster rises by more than 20 % from M to L, with both figures.                                                                                                    |
| C9  | What does one chat question cost, and how long does the user wait? | For each scripted chat question and size: its tokens and cost, and its opening turn's time (`turn1_s`) and the full answer's time (`latency`), as the eval harness records them.                            |
| C10 | How much model quota does each size need at its peak?              | For each size and window: the highest token rate over any one minute (input, cached input and output tokens a minute), the install's and each of the five largest capabilities', and the requests a minute. |

## Questions the report answers

For each fleet size and each window:

1. Which capabilities use the most memory, and how much: the share of memory-time (GiB-hours) and
   the peak.
2. Which capabilities use the most model tokens, and how many: input, output and cached input.
3. For one month, how much memory-time and how many tokens each capability uses, and what that
   costs.
4. For one named capability, for example the stall watch: what one run of it costs on a fleet of
   size S, M or L, what each piece of work it starts costs, and how both change with the fleet's
   size.

Questions 1 to 3 have an overview table whose rows are the five largest capabilities, Other, the
platform base and Unattributed; the rows add up to 100 % of the measured total. Question 4 has a
table for every capability, so a capability that is too small for the overview is not hidden in
Other.

## Terms

| Term           | Meaning                                                                                                                                                   |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Capability     | A named job of the agent. The list is fixed, below.                                                                                                       |
| Capability tag | The capability's name, carried on a model call (as request metadata) and on a process (as an environment variable).                                       |
| Platform base  | Each install container's idle memory, measured once at the start of the run before any capability runs, and held fixed for the run. A row of its own.     |
| Unattributed   | Memory or tokens that no tag and no rule below assigns to a capability. A row of its own. A tagging gap shows here; the target is below 5 % of the total. |
| Memory-time    | Memory used, integrated over time, in GiB-hours. It drives cost. The peak drives the container limit.                                                     |
| Input tokens   | Prompt tokens not read from the model's cache. Cached input tokens are counted apart, so no token is counted twice.                                       |

## The capability list

The list follows what starts the work, because a tag can name that with certainty. Each job in
either cron roster is a row by its id, whatever kind of job it is: an agent job, a script, or a
script that also calls the model (`upgrade-readiness-weekly-brief` has a script and a turn limit).
The two rosters are `agents/platform/cron/jobs.json`, the Platform Agent's audits and watchers, and
`agents/chat/defaults/cron/jobs.json`, the Chat Agent's jobs: `profile-cron-tick`, which ticks the
other profiles' rosters every minute, `cluster-agent-reconcile`, the inventory jobs and `oobe`.

Beyond the rosters, five rows:

| Capability      | Started by                                                                                                                       |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| First-run flow  | The `oobe` job's inventory scan and its cards, before the first-run audits start. Each first-run audit is its audit's row.       |
| Incident triage | A session the event watcher opens.                                                                                               |
| Chat            | A chat turn. Which skills a turn loaded is read after the run from the session record and reported inside this row.              |
| Memory service  | The Hindsight memory service's own model calls and memory, when the install enables it.                                          |
| Session Pods    | The Pods that `delegate` and `/session` start under `spec.mode: next`, each under the capability of the session that started it. |

A capability's row holds the work it starts, not only its own run. Three rules carry the tag
forward:

- **An audit counts under the audit** whatever started it, a schedule, the first-run flow or a chat
  request. A kanban card that runs an audit's procedure carries that audit's tag, by the audit id
  the card names.
- **An inject counts under the capability that raised it.** A script watcher calls no model, but it
  can start agent work through the Session KV server's inject route. The stall watch
  (`agents/platform/scripts/stall_watch.py`) hands each new stall episode to that route as a
  `controller-stall` inject; the server posts the alert and starts a Planning Agent turn, which
  files one card to the cluster's Cluster Agent, which runs `gke-stall-detection`. Each inject
  carries its origin's tag (`stall-watch`, `incident-triage` for the event watcher, the drift
  detector's id), and the turn and the card it starts inherit it. So the stall watch's row holds
  its sweeps and every investigation they start.
- **Any other card** carries the tag of the session that created it.

## Tokens

Every model call carries its capability tag to the LiteLLM gateway. The harness keeps the tag in a
context variable that each session sets when it starts, so calls from several sessions in one
process each carry their own tag. A patch under `deploy/docker/patches` adds the tag to each
request's metadata (`extra_body`). The cron ticker sets it from the job id, the kanban worker from
the card, the chat gateway as `chat`, and the Session KV server from the inject's origin.

The install's LiteLLM has no database, so its spend tracking by tag is not available. Two outputs
can carry the tag instead. LiteLLM's `custom_prometheus_metadata_labels` setting adds a label from
request metadata to its token metrics, which the existing scrape
(`bench/kube_agents_bench/scale/collect.py`) reads. The setting was read in LiteLLM 1.103; the
pinned image is 1.106 (`images.json`), so the first step confirms it there. The other output is
`charts/kube-agents/files/llm_trace_logger.py`, which already writes one JSON line per model call with its token usage; it
writes to `/tmp`, and would write to the container log instead.

The first implementation step needs no runtime change. Hermes stores token counts on each session
row in each profile's `state.db`: `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens` and `reasoning_tokens`, which `bench/kube_agents_bench/worker_trajectory.py`
already reads. A cron session's id names its job (`cron_<job>_<date>_<time>`), and the row has a
`source` column, so the report joins each session to its capability after the run. Model calls made
outside a Hermes session, such as the memory service's, land in Unattributed in this form. The
first nightly runs measure this form against LiteLLM's own totals; the per-call tag follows if
Unattributed is above 5 %.

## Memory

Memory is sampled every 15 seconds in each install container. The containers are every container
of every Pod in the install's namespace: the gateway Pod, the shell sandbox, the credential broker,
the kube-proxy, LiteLLM, the event watcher, the A2A bus, the operator, the token minter, the web
console when enabled, and the session Pods. A container that serves no single capability (the
operator, the minter, the console, the A2A bus) contributes only platform base and Unattributed.

The container total comes from Cloud Monitoring, the series the runner already reads:
`kubernetes.io/container/memory/used_bytes` with `memory_type=non-evictable`. Within a container,
memory above the platform base is divided in three ways.

**Separate processes** carry a tag in their environment, `KUBE_AGENTS_CAPABILITY`, set when the
harness starts them. This holds for kanban workers, script jobs and the commands the agent runs in
the shell sandbox. A child process inherits the tag. A sampler sums the memory of each tag's
processes: `RssAnon` plus `RssShmem`, or `VmRSS` under gVisor, which reports no `RssAnon`.

**Shared processes** run several capabilities at once as threads. The gateway's
`hermes gateway run` holds every chat turn and the default profile's cron jobs, and each profile's
`hermes cron tick` runs its due jobs on a thread pool. An environment variable cannot separate them:
it is global to the process. Their memory above their idle size is divided among the capabilities
that had a live session in that process during the sample:

- in equal shares among the live sessions, from the sessions' start and end times and their
  messages' times in `state.db`. This needs no runtime change, so it is the rule from the first
  build step;
- in proportion to each session's tokens in the interval, once the per-call tag gives a timestamped
  token series. An interval in which no live session finished a model call (a long tool run, a wait
  on a card) falls back to equal shares;
- a sample with no live session in the process leaves its memory above idle as Unattributed.

This is an estimate, not a measurement, and the report shows it as a separate share inside each
capability's row, so a reader can see how much of a figure is estimated.

**Shared services**, the credential broker, the kube-proxy and LiteLLM, serve every capability.
Their memory above idle is divided by each capability's share of their requests in the same minute.
The broker and the kube-proxy log each request; the shell's command wrappers add the caller's tag
to it as a header.

Process sizes count shared pages in each process that maps them, and include page cache in
proportion, so their sum does not equal the container's figure. Each sample fits them as follows:

1. The target is the container's figure less its platform base, or zero if the figure is below the
   base. A figure below the base is recorded, because it means the base measured at the start was
   too high.
2. The shares are the tagged processes' sizes, the estimated shares of the shared processes, and
   the untagged share: the size of every other process of a non-root user, including a shell
   command whose tag was removed or is not on the list.
3. All shares are scaled by one factor so that they add up to the target. The untagged share,
   after scaling, is Unattributed. When the container runs no non-root process at all, the whole
   target is Unattributed.

Unattributed memory is therefore the scaled size of what no tag explains, and it is not spread over
the tagged capabilities.

**Where the samplers run.** In the shell sandbox, the root memory guard proposed for the sandbox
(`deploy/sandbox/shell-memory-guard.py`) walks `/proc` every 15 seconds and writes one log line
with each tag's memory. This is a new walk beside the guard's own, not a reuse: the guard reads
process sizes only near its watermark. In the gateway Pod, the sampler runs inside the
`platform-agent` container itself, beside the existing cgroup memory sample of the gateway's
lifecycle ledger (`deploy/docker/patches/apply_cgroup_memory_sample.py`). A separate container
would need `shareProcessNamespace`, which the operator leaves unset on purpose.

**Tags are data, and environments hold secrets.** A sampler reads `/proc/<pid>/environ` and takes
the one key `KUBE_AGENTS_CAPABILITY`; it never logs or keeps any other variable. It caches the tag
by pid and process start time, so a reused pid does not keep an old tag. In the shell sandbox the
model runs the commands and can set or remove the variable, so shell attribution is best effort: a
value that is not on the fixed capability list counts as Unattributed. The agent's SSH client sends
the variable (`SendEnv`, in `deploy/docker/ssh_config.d/`) and the sandbox's `sshd` accepts it on
the `AcceptEnv` line inside the `Match User` block of `deploy/sandbox/sshd_config`, where a global
line does not apply.

## The two windows

The windows come from the sized test fleet's run. It starts at 00:00 UTC and ends before the first
scheduled audit of the day, so each window holds the same work every night.

| Window | Length                                                       | Holds                                                                                                |
| ------ | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Burst  | 1 hour nominal: until the last audit ends, from 1 to 3 hours | The first-run flow, and beside it the other audits on a 5-minute stagger, the weekly ones included.  |
| Soak   | 1 hour, after the burst window                               | The script watchers on their schedules, the event watcher, the churn, three scripted chat questions. |

Each window reports its own totals, its rate an hour and its peaks.

## The monthly estimate

Scaling a window by hours would count the first-run flow every hour. The estimate instead
counts each capability's runs in a month, from its cron expression: a daily job about 30.4 runs, a
weekly one about 4.3, an hourly one 730, one every 30 minutes 1,460, one every 10 minutes 4,380, a
weekday job about 21.7.

- **A scheduled capability:** its use a run, times its runs a month. Each audit's run is in the
  burst window, and the install's state is reset each night, so every audit run is a first run,
  which reads every cluster as new. The estimate marks the audit rows as first-run figures until a
  run without the reset measures typical runs. A watcher's use a run comes from its runs in the
  soak window.
- **An event or chat capability:** its rate an hour in the soak window, times 730 hours. The
  chat rate comes from the three scripted questions, not from people, so the report marks the chat
  row as a scripted load.
- **The first-run flow:** once an install, a line of its own, outside the monthly total.
- **The platform base:** its memory, times 730 hours.

The cost of each row:

- **Tokens:** input, output and cached input tokens, each times the price of the model the install
  uses. The price table is one file in the repository, with its source and date.
- **Memory:** on GKE Autopilot, the module's default (`terraform/modules/gke-cluster/`), a Pod is
  billed for the memory it requests. The install's memory cost is the cost of its requests,
  divided by each row's share of the memory-time. On a Standard cluster the nodes are billed
  instead, and the same division applies to the share of the nodes the install's requests take.
  Dividing by use undercharges the capability whose peak sets a request; the report also shows
  each row's share of the peak.

## The report

Each run writes `capability-usage.json`, one record for each size, window and capability, every
capability included, with the tokens, the memory-time, the estimated share, the peak, the runs and
units counted, and the cost. A Markdown report from the records has three parts.

**The overview.** For each size and window, a memory table and a token table, each with the five
largest capabilities, Other, the platform base and Unattributed, adding up to 100 %, and a table of
the monthly estimate and its cost.

**A table for each capability.** One table a capability, with a row for each size. Its columns:

| Column       | Meaning                                                                                                      |
| ------------ | ------------------------------------------------------------------------------------------------------------ |
| Runs         | The runs measured, over the nights the figure averages.                                                      |
| Memory a run | The peak, and the memory-time of one run.                                                                    |
| Tokens a run | Input, cached input and output tokens of one run, without the work it started.                               |
| Units        | The pieces of work the runs started, and how many: for the stall watch, stall episodes; for an audit, cards. |
| Cost a unit  | The memory-time and tokens of one unit: one stall investigation, from the inject to the card's end.          |
| A month      | Runs a month times the cost a run, plus the units a month at the measured rate times the cost a unit.        |
| A cluster    | The cost a run divided by the size's clusters, to compare sizes and to extrapolate past L.                   |

The two parts matter because a capability's cost has two sources that scale differently. The
stall watch's sweep reads every cluster, so its cost a run grows with the fleet. Its investigations
grow with the stalls the fleet has, not with its size; the sized fleet plants 5 failing Pods at M,
so the measured unit count is the test's, not a real fleet's. The table therefore keeps the cost a
unit apart from the count, and a reader multiplies the cost a unit by their own fleet's rate.

A figure averages the runs of the last 7 nights at M and the last 4 at L, because a run of a
30-minute watcher happens about four times a night and an audit once. S has no scheduled run, so
its row stays empty until a run by hand fills it.

**Response time.** The eval harness records each scripted chat question's opening turn's wall-clock
time (`turn1_s`, from the moment the agent accepts the question to the end of that turn) and the
whole answer's time (`latency`). The report keeps both with the question's tokens and cost (C9).

**Peak rate.** The sampler reads LiteLLM's token and request counters every 15 seconds, so the report
gives the highest rate over any one minute in each window (C10). With the per-call tag, the counters
carry the capability as a label and the peak is per capability too. Without it, a capability's peak
comes from its sessions' message times and token counts, at the resolution those records have.

**Flags.** The report marks three kinds of row:

- a capability whose cost a cluster rises from M to L by more than 20 %, which means it grows faster
  than the fleet;
- a capability whose peak memory in L is above half of its container's limit;
- a capability whose figure rests on fewer than three runs.

Two checks guard the numbers. A run that fails either is marked unsound rather than reported:

- the rows add up to the measured total: the token rows to LiteLLM's counters for the window, with
  cached tokens counted once, and the memory rows to the containers' figures from Cloud Monitoring;
- the Unattributed share is below 5 % for tokens and for memory.

## What is built, in order

Each item is its own pull request, after this design merges and the sized test fleet runs.

1. **Tokens from the session records.** `bench/kube_agents_bench/scale/attribution.py` joins each
   profile's sessions to their capabilities and checks the sum against LiteLLM's counters. No
   runtime change. A session that an inject started is joined through the inject's record, which
   names its kind (`controller-stall`, an event, a drift), to the capability that raised it.
2. **The process tags and samplers.** The Session KV server's tag on each inject-started turn and
   its card; `KUBE_AGENTS_CAPABILITY` at each separate process's start,
   carried over SSH and checked against the list; the shell sampler and the gateway sampler; the
   broker's and kube-proxy's tag on each request log line.
3. **The memory report.** The fit to the container figures, the shared-process estimate, the two
   checks, the monthly estimate and the cost, with unit tests on recorded samples. Its shared-process
   estimate uses equal shares among live sessions, which needs only the session records.
4. **The per-call token tag**, if the first step's Unattributed share needs it: the context
   variable, the request metadata patch, and the LiteLLM label or log line.
5. **The nightly.** The sized test fleet's run calls the report after each window and keeps the
   records beside its verdict.

## Open questions

- **Chat load.** Three scripted questions an hour are a guess at an install's chat traffic. The
  number of questions in the soak window is one setting, and real installs' chat rates would set
  it better.
- **Typical runs.** A weekly run without the nightly reset would replace the first-run figures of
  the audits.
- **Retention.** Whether the per-capability series stay on in production installs, so a user can
  see the same table for their own install. The install-level counters of
  [`usage-counters-producer.md`](usage-counters-producer.md) are the place such a series would
  join.
