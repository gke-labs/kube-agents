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

## Questions the report answers

For each fleet size and each window:

1. Which capabilities use the most memory, and how much: the share of memory-time (GiB-hours) and
   the peak.
2. Which capabilities use the most model tokens, and how many: input, output and cached input.
3. For one month, how much memory-time and how many tokens each capability uses, and what that
   costs.

Every answer is a table whose rows are the five largest capabilities, Other, the platform base and
Unattributed. The rows add up to 100 % of the measured total.

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

An audit counts under the audit whatever started it, a schedule, the first-run flow or a chat
request. A kanban card that runs an audit's procedure carries that audit's tag, by the audit id the
card names. Any other card carries the tag of the session that created it.

## Tokens

Every model call carries its capability tag to the LiteLLM gateway. The harness keeps the tag in a
context variable that each session sets when it starts, so calls from several sessions in one
process each carry their own tag. A patch under `deploy/docker/patches` adds the tag to each
request's metadata (`extra_body`). The cron ticker sets it from the job id, the kanban worker from
the card, the chat gateway as `chat`, and the event watcher's sessions as `incident-triage`.

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
that had a live session in that process during the sample, in proportion to each one's tokens in
that interval. This is an estimate, not a measurement, and the report shows it as a separate share
inside each capability's row, so a reader can see how much of a figure is estimated.

**Shared services**, the credential broker, the kube-proxy and LiteLLM, serve every capability.
Their memory above idle is divided by each capability's share of their requests in the same minute.
The broker and the kube-proxy log each request; the shell's command wrappers add the caller's tag
to it as a header.

Process sizes count shared pages in each process that maps them, and include page cache in
proportion, so their sum does not equal the container's figure. Each sample scales the tagged and
estimated shares so that they and the platform base add up to the container's figure. A remainder
with no process or rule to explain it is Unattributed, not platform base.

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

Each run writes `capability-usage.json`, one record for each size, window and capability, with the
tokens, the memory-time, the estimated share, the peak and the cost. A Markdown report from the same
file shows, for each size and window, a memory table and a token table, each with the five largest
capabilities, Other, the platform base and Unattributed, adding up to 100 %. A third table shows
the monthly estimate and its cost.

Two checks guard the numbers. A run that fails either is marked unsound rather than reported:

- the rows add up to the measured total: the token rows to LiteLLM's counters for the window, with
  cached tokens counted once, and the memory rows to the containers' figures from Cloud Monitoring;
- the Unattributed share is below 5 % for tokens and for memory.

## What is built, in order

Each item is its own pull request, after this design merges and the sized test fleet runs.

1. **Tokens from the session records.** `bench/kube_agents_bench/scale/attribution.py` joins each
   profile's sessions to their capabilities and checks the sum against LiteLLM's counters. No
   runtime change.
2. **The process tags and samplers.** `KUBE_AGENTS_CAPABILITY` at each separate process's start,
   carried over SSH and checked against the list; the shell sampler and the gateway sampler; the
   broker's and kube-proxy's tag on each request log line.
3. **The memory report.** The fit to the container figures, the shared-process estimate, the two
   checks, the monthly estimate and the cost, with unit tests on recorded samples.
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
