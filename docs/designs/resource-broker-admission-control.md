# Resource Broker: Fair, Cost-Aware Admission Control

## High level summary

The resource broker is the set of components that run tool calls for the agents. It runs every
`kubectl`, `gcloud` and Git command, every Cloud API call, and every skill script. Today three
components make up the resource broker: the credential proxy, the Kubernetes API proxy and the
sandbox shell. Each one runs in a container with a fixed memory limit.

An OOMKill is the kernel stopping a container that uses more memory than its limit. When agents
send more work than the resource broker can hold, a container is OOMKilled. All work in that
container stops, and the agents often send the same work again. A reproduction on a fleet of two
clusters with 45,000 Pods each OOMKilled the credential proxy five times in five minutes. Two
commands caused each kill. One command can use more than 2 GiB.

This document specifies admission control for the resource broker. Admission control decides,
before a tool call starts, whether the resource broker accepts it. It has three layers:

1. A memory guard in each container that runs tool calls. The guard stops the largest tool
   process before the container reaches its memory limit. The container stays up, and only one
   tool call fails.
2. A fair share per capability. A capability is the unit of agent work that sends tool calls, for
   example one audit job or one chat turn. Each capability gets an equal share of the slots and a
   rate limit. A capability that sends too much gets an immediate refusal with a wait time. Other
   capabilities continue.
3. A cost check. Before a large read starts, the resource broker estimates its memory. A read that
   does not fit gets a refusal that tells the agent how to make the read smaller.

The memory guard prevents the OOMKill. The fair share prevents one capability from blocking the
others. The cost check gives the agent a useful answer before work starts. The number of slots and
the rates change with the memory and CPU that the containers have.

## 1. Scope

**Owns:** the admission rules of the resource broker: the memory guard, the per-capability fair
share and rate, the cost check on reads, and how slot counts and rates follow the containers'
resources.

**Does not own:**

- The credential proxy's existing slot cap, child memory budget and watermark gate
  ([`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md),
  [`credential-broker-exec-tier.md`](credential-broker-exec-tier.md) §2.1 and §4). This design keeps
  them and adds to them.
- The Kubernetes API proxy's request policy ([`kube-proxy.md`](kube-proxy.md)).
- How the event watcher pages its Event lists. Section 7 states the bound it must meet.

**Names.** "Resource broker" is the role. The components that fill it are the credential proxy
(`envoy-credential-proxy` container, `agents/platform/scripts/credential_proxy.py`), the Kubernetes
API proxy (`k8s-api-proxy`, [`kube-proxy.md`](kube-proxy.md)), and the sandbox shell
(`platform-agent-shell`), where `kubectl` and the skill scripts run.

## 2. The problem, measured

The OOM reproduction harness (`bench/oom-repro/`, a manual test in the bench harness) builds a fleet
of KWOK nodes: Node objects in real GKE clusters with no VM behind them. On two clusters of 1,500
nodes and 45,000 Pods each, at the credential proxy's default limit of 2 GiB, the following held
the memory in the last sample before each OOMKill:

| Process class                                    | Share of the container's memory |
| ------------------------------------------------ | ------------------------------- |
| `kubectl` children (one or two fleet-wide lists) | 85 to 92 %                      |
| Envoy                                            | 4 %                             |
| the broker process (`credential_proxy.py`)       | 3 to 4 %                        |
| `gcloud` children                                | 0 to 7 %                        |

Single `kubectl` children measured at the XL size, where nothing was killed:

| Command                                                                             | Pods listed | Resident memory |
| ----------------------------------------------------------------------------------- | ----------- | --------------- |
| `get nodes,pods,pvc,pv,svc,jobs,cronjobs,pdb,ns,resourcequota,…,ingress -A -o json` | 45,000      | 4,402 MiB       |
| `get deploy,sts,ds,cronjob,pod -A -o json`                                          | 45,000      | 3,314 MiB       |
| `get pods -A -o jsonpath={.status.phase}`                                           | 45,000      | 3,236 MiB       |
| `get pods -A -o json`                                                               | 90,000      | 5,610 MiB       |
| `gcloud logging read …`                                                             | —           | 289 MiB         |

Five facts follow:

1. **The cost of one call varies by a factor of 100.** A `kubectl get node X` uses about 45 MiB;
   a fleet-wide list uses 1 to 5.6 GiB. A limit on calls per minute does not bound memory: two
   calls killed the container.
2. **The output format does not reduce the cost.** `kubectl` decodes the whole list before it
   applies `jsonpath` or `custom-columns`.
3. **The existing child memory budget estimates 176 MiB per call.** That estimate is right for
   small reads and wrong by a factor of up to 30 for fleet-wide lists, so the budget admitted the
   calls that killed the container.
4. **A killed container takes all its work with it, and the work comes back.** `compliance-audit`
   sent the same two listings after each restart, until `CrashLoopBackOff`.
5. **The resource broker cannot tell capabilities apart.** Every command in the reproduction came
   from the shell's ServiceAccount. An audit, a chat question and a triage card look the same.

Since the Kubernetes API proxy landed, stock `kubectl` runs inside the sandbox shell and reads
through the Kubernetes API proxy, which streams responses. The memory of fact 2 therefore moves
from the credential proxy to the shell, whose limit (2 CPU, 2 GiB) the operator fixes. The same
two listings that killed the credential proxy are expected to kill the shell. The harness measures
that case before this design is built (§10, step 0).

## 3. Goals and non-goals

**Goals.**

- G1. No container of the resource broker is OOMKilled by tool calls, at any fleet size, at the
  default limits.
- G2. A capability that sends more than its share cannot delay another capability by more than
  one share's worth of work.
- G3. A refused call fails fast (under 1 s), and the refusal says why and what to do.
- G4. Slot counts and rates follow the containers' memory and CPU without a code change.
- G5. Simple: each layer is small, testable alone, and uses mechanisms that run on very large
  numbers of machines.

**Non-goals.**

- Making every large read succeed. A read that cannot fit fails; the agent narrows it.
- Security isolation between capabilities. The fair share is a scheduling key, not an identity
  claim (§5.1).
- Replacing the credential proxy's slot cap or watermark gate.

## 4. Layer 1: the memory guard

### 4.1 The rule

A thread in each container that runs tool calls (the credential proxy's broker process, and a
small process in the sandbox shell) does this every Δ = 100 ms:

1. Read the container's cgroup: `memory.max` (the limit L) and, from `memory.stat`, the anonymous
   memory plus the unreclaimable kernel memory. Call that sum M. Page cache is left out because
   the kernel reclaims it before it OOMKills.
2. If M ≥ H, the high watermark, send SIGKILL to the tool process with the largest resident
   memory. A tool process is a child the resource broker started for a call. The broker, Envoy,
   `sshd` and the shell's init are never candidates.
3. Answer the killed call with a refusal (§6) that names the process, its memory and the limit.
4. Repeat from step 1 without waiting while M ≥ H, at most once per Δ per kill.

H = L − headroom. The default headroom is max(256 MiB, 12.5 % of L).

### 4.2 Why this prevents the OOMKill

Definitions, per container:

- L: the memory limit. K: the memory of the processes that are never killed (broker, Envoy, init).
- N: the maximum number of concurrent tool processes (the slot cap).
- r: the maximum rate at which one tool process grows its memory. `kubectl` decoding a list grew
  at 45 to 60 MiB/s in the reproduction; the design uses r = 100 MiB/s.
- R = N · r: the maximum rate at which the container's memory grows.
- Δ: the guard's interval. τ: the time from SIGKILL until the kernel frees the process's memory
  (under 10 ms for anonymous memory).

**Claim.** If K < H and headroom > R · (Δ + τ), then M stays below L, and the kernel never
OOMKills the container because of tool processes.

**Proof.**

1. While M < H the guard does nothing, and the container is below its limit.
2. Take the first sample at which M ≥ H, at time t₀. The previous sample, at t₀ − Δ, was below H.
   So at t₀, M < H + R · Δ.
3. At t₀ the guard kills the largest tool process. Until its memory is free, at t₀ + τ, the
   container grows by at most R · τ. So the peak is below H + R · (Δ + τ) < L.
4. The killed process held at least (M − K) / N, because it was the largest of at most N. After
   the kill, the growth rate falls to at most (N − 1) · r.
5. If M is still at least H at the next sample, step 3 repeats with fewer processes. Each round
   removes one process. After at most N rounds, no tool process is left, and M ≤ K < H.
6. In every round the peak stays below H + R · (Δ + τ) < L. So M never reaches L. ∎

**The numbers at the default.** L = 2 GiB, N = 8, r = 100 MiB/s, so R = 800 MiB/s. With Δ = 100
ms and τ = 10 ms, R · (Δ + τ) = 88 MiB. The headroom is 256 MiB, nearly three times the bound.
K is about 250 MiB (broker and Envoy at their highest in the reproduction), well below H = 1,792 MiB.

**What the proof assumes, and what breaks it.**

- A tool process grows no faster than r. A process that allocates and touches a gigabyte in one
  step breaks this. `kubectl`, `gcloud` and Python scripts grow as they decode, which the
  measurements show. A guard at Δ = 50 ms halves the risk if a faster grower appears.
- The guard thread itself runs. A guard that is starved of CPU does not sample. The guard runs at
  a higher CPU priority than the tool processes (§4.3).
- K is bounded. The broker's own memory is bounded by its output cap and slot count
  ([`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md) §2.1).
  Envoy's buffers are bounded by its configuration.

### 4.3 Implementation

- **Where.** The credential proxy: a thread in the broker process, which already starts the
  children and knows their PIDs. The sandbox shell: a small process started by its entrypoint,
  which reads `/proc` for the processes under the session tree.
- **Libraries.** The Python standard library reads the cgroup files and `/proc`, and
  `os.kill` sends the signal. `psutil` is an option for the process tree.
- **Priority.** The guard sets its own `nice` value lower than the tool processes. The broker
  already proposes a higher `nice` for background children ([`credential-broker-exec-tier.md`](credential-broker-exec-tier.md) §4.2).
- **No privileges.** Reading the container's own cgroup files and killing its own children need
  no capability. A per-child cgroup with its own `memory.max` would be stronger, but a container
  cannot create sub-cgroups without a writable cgroup filesystem, which a default Pod does not
  have. Section 9 compares the two.
- **Signals.** A counter `tool_process_killed_total{container, class}` and a log line per kill with
  the command, its memory and M.

## 5. Layer 2: a fair share per capability

### 5.1 The capability key

The key says which unit of agent work sent a call. It is used for scheduling only. A caller that
lies about its key gains no access; at most it moves its calls to another share.

- **Now:** the role (shell, profile, session, watcher) and the profile (the Platform Agent or one
  Cluster Agent), which the resource broker already learns from the caller's token and from
  `HERMES_PROFILE_HOME`, which the sandbox already forwards.
- **Next:** the card or job. The kanban worker, the cron runner and the no-agent watches set
  `KUBEAGENTS_CAPABILITY` (for example `cron:compliance-audit` or `card:t_c2b04354`) in the
  session, the sandbox forwards it as it forwards `HERMES_PROFILE_HOME`, and the shims send it as a
  header. The turn token of the exec-tier design (§4.1 there, open question 9) can carry it later.

### 5.2 The rule

Each resource broker component holds N slots (§8 computes N). Requests wait in one queue per
capability key.

- **Rate.** Each capability has a token bucket: rate ρ calls per minute, burst b. A call with no
  token is refused at once with `429` and a `Retry-After` that says when a token returns. It does
  not wait and does not reach a tool.
- **Slots.** When a slot is free, the scheduler takes the next call by deficit round robin across
  the capabilities that have calls waiting: each capability gets one turn per round.
- **Wait.** A call that waits longer than W = 10 s for a slot is refused with `429`. Today's 60 s
  wait (`COMMAND_SLOT_WAIT_SECONDS`) holds a caller for a minute and then answers `503`; a short
  wait answers sooner and with a reason the agent can act on.
- **Classes.** The interactive and background classes of the exec-tier design (§4.2 there) stay.
  Deficit round robin runs inside each class, and the interactive reserve stays.

### 5.3 What this guarantees

With k capabilities that have calls waiting, each gets at least ⌊N / k⌋ slot-turns per round, and
at least one per k turns (the property of deficit round robin). A capability that floods the
broker fills only its own queue and its own bucket. It cannot take another capability's turn. Its
excess is refused at the bucket, before any tool runs. This is G2.

The fair share bounds concurrency and rate, not memory. One admitted call can still be a 5 GiB
read. Layer 1 bounds memory; layer 3 refuses most such reads before they start.

### 5.4 Implementation

- **Credential proxy, HTTP routes:** Envoy's local rate-limit filter
  (`envoy.filters.http.local_ratelimit`), with a token bucket per descriptor keyed on the
  capability header. Envoy answers `429` before the request reaches Python. Envoy already fronts
  the broker.
- **Credential proxy, exec admission:** the existing `_admit` queue, changed from one arrival-order
  queue to one queue per key with deficit round robin. The standard library (`collections.deque`,
  `threading.Condition`) is enough.
- **Kubernetes API proxy:** a Go middleware in front of the reverse proxy, with
  `golang.org/x/time/rate` (one limiter per key) and a weighted semaphore
  (`golang.org/x/sync/semaphore`) for the slots.
- **Agents:** the refusal is a normal tool result (§6), not a transport failure, so the agent does
  not retry it as "the broker is down".

## 6. Layer 3: the cost check

### 6.1 The rule

Before the resource broker forwards a list of a collection with no `limit` and no selector, it
asks the API server for one item: `?limit=1` returns `remainingItemCount`. It estimates the memory
the caller's `kubectl` will need as `items × bytes per item`, where bytes per item is measured per
resource (about 74 KiB per Pod for `kubectl` in the reproduction) and kept up to date from the
guard's kills and the completed calls.

- If the estimate is above the cost limit C (default: half of H in the container that will decode
  the list), the read is refused with `422` and a message:
  "this read would need about 3.2 GiB in the shell; narrow it: add `-l`, `--field-selector` or
  `-n`, or use `-o name`, or page it with `--chunk-size` and process each page".
- The count is cached per (cluster, resource) for 60 s, so one estimate covers a burst of reads.

This is the same idea as Kubernetes API Priority and Fairness, which charges a list request more
seats in proportion to the number of objects it returns.

### 6.2 What this guarantees

Nothing on its own: an estimate can be wrong, and a read the check misses (a selector that matches
most objects) still runs. Layer 1 is the guarantee. The cost check changes the common failure
from "the call ran for 30 s and was killed" to "the call was refused in 200 ms with advice".

## 7. The event watcher

The event watcher is one Go process in the `agent-api-auth` container. Layer 1 does not apply,
because killing the watcher is the OOMKill. Its memory must be bounded inside the process:

- **Bound.** Peak memory ≤ base + Σ over the lists in flight of (page size × bytes per Event),
  with the number of lists in flight limited by a weighted semaphore whose weights are each list's
  estimated size (`remainingItemCount` × bytes per Event), not a fixed count of 4.
- **How.** The watcher handles each page of the initial list and then drops it, instead of
  collecting the whole list first. The reproduction killed the watcher at 2 GiB with four clusters
  of 400,000 Events each; with pages of 500, the same lists need under 10 MiB in flight.
- **Signal.** The watcher logs and counts a relist after its initial sync.

## 8. Dynamic slots and rates

The slot count N and the rate ρ follow the containers' resources. They are recomputed once a
minute from live values, with no restart.

**Slots from memory.**

    N = clamp(1, N_max, ⌊(H − K_measured) / c_p95⌋)

H comes from the live `memory.max`. K_measured is the measured memory of the processes the guard
never kills. c_p95 is the 95th-percentile memory of completed tool processes over the last hour. A
container whose limit is raised to the L size (8 GiB for the credential proxy) gets more slots
without a code change.

**Back-off from pressure.** Linux reports pressure stall information (PSI) in each cgroup's
`memory.pressure` and `cpu.pressure`: the share of time tasks waited for memory or CPU. When
`some avg10` passes a threshold (default 20 % for memory, 50 % for CPU), N falls to ⌈0.7 · N⌉. When
pressure stays under half the threshold for a minute, N rises by one, up to the value from memory.
This is additive increase, multiplicative decrease, the rule TCP uses for its congestion window.

**Rates from throughput.** By Little's law, throughput X = N / S, where S is the mean time a call
holds a slot. The rate per capability is ρ = X / k_active, with k_active the number of capabilities
active in the last minute and a floor of 6 calls per minute. A larger N, or faster calls, raise ρ
for everyone.

## 9. Alternatives and trade-offs

| Option                               | For                                                               | Against                                                                                                              |
| ------------------------------------ | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| **Userspace memory guard (chosen)**  | No privileges; small; the container stays up; proven bound (§4.2) | Needs a growth-rate assumption; one sampling thread per container                                                    |
| Per-child cgroup with `memory.max`   | Kernel-enforced per call; no growth assumption                    | Needs a writable cgroup filesystem in the container (privileged or a custom runtime); not available in a default Pod |
| Raise the limits                     | One-line change                                                   | Moves the threshold, not the failure: the L size still fails at a larger fleet (the harness's L run measures where)  |
| Rate limit only (calls per minute)   | Simple                                                            | Does not bound memory: two calls killed the container                                                                |
| Count-based slots only (today)       | Simple                                                            | A slot's cost varies by a factor of 100                                                                              |
| Cost check only                      | Refuses most large reads early                                    | Estimates can be wrong; no guarantee                                                                                 |
| Kill the container (today's outcome) | None                                                              | Kills every call in flight; work comes back                                                                          |

**Pros of the design as a whole.**

- The guarantee comes from a small, testable mechanism (layer 1), not from estimates.
- A flood is contained to the capability that sends it.
- Refusals are fast and tell the agent what to do.
- Slots and rates follow the containers' resources.

**Cons.**

- A large read that the agent truly needs is refused or killed. The agent must narrow it, or the
  skill must page it. Skills that list fleet-wide (`collect.py`, `fleet_upgrade_report.py`) need
  paged reads to keep working at large fleets.
- The capability key depends on new plumbing (`KUBEAGENTS_CAPABILITY`) until the turn token exists.
- A refusal is text the model reads, so each one needs an evaluation case
  ([`.agents/rules/eval_driven_development.md`](../../.agents/rules/eval_driven_development.md)).
- Three components to change: the credential proxy, the Kubernetes API proxy and the sandbox shell.

## 10. Rollout

0. **Measure.** Run the OOM harness's `proxy` scenario against a build with the Kubernetes API
   proxy, watching the sandbox shell, to confirm where the `kubectl` memory now lands.
1. **Memory guard** in the sandbox shell and the credential proxy. This alone meets G1 for those
   containers. Acceptance: the harness's `proxy` scenario at the default limits ends with zero
   OOMKills and one guard kill per oversized read.
2. **Event watcher** paged initial lists and the size-weighted gate (§7). Acceptance: the
   harness's `watcher` scenario at the default limits ends with zero OOMKills.
3. **Capability key** from role and profile, then `KUBEAGENTS_CAPABILITY`.
4. **Fair share and rate** in Envoy, the exec queue and the Kubernetes API proxy. Acceptance: a
   flood from one capability in the harness leaves another capability's p95 wait under W.
5. **Cost check** in the Kubernetes API proxy.
6. **Dynamic N and ρ.**

Each step is a pull request with its own evaluation case where the model reads new text.

## 11. Prior art

| System                                                         | What it does                                                                                   | What this design takes                                           |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Linux cgroup v2 `memory.high` / `memory.max`                   | Throttles a group above `high`, kills inside it at `max`                                       | The watermark-then-kill shape of layer 1                         |
| Linux PSI (`memory.pressure`, `cpu.pressure`)                  | Time tasks stalled on a resource                                                               | The back-off signal for N (§8)                                   |
| `systemd-oomd`, `earlyoom`, Android `lmkd`                     | Userspace killers that act on PSI or free memory before the kernel's OOM killer                | Layer 1 itself: kill the largest worker, keep the system up      |
| Linux CFS / EEVDF and `cpu.weight`                             | Weighted fair CPU time between groups                                                          | Fair turns per capability                                        |
| Kubernetes API Priority and Fairness                           | Priority levels, fair queuing by flow, shuffle sharding, list requests charged by object count | Layers 2 and 3: fair queues per capability, cost by object count |
| Envoy local rate limit, overload manager, adaptive concurrency | Token buckets per descriptor; shedding on memory; concurrency from latency                     | The rate per capability (§5.4); pressure back-off                |
| TCP congestion control (AIMD)                                  | Additive increase, multiplicative decrease of a window                                         | The rule for N (§8)                                              |
| Netflix `concurrency-limits`                                   | Concurrency limits that adapt to latency (Vegas, Gradient)                                     | An option for N if latency proves a better signal than PSI       |

## 12. Open questions

1. Is r = 100 MiB/s a safe bound for every tool the shell runs (Python scripts that load large
   JSON, `jq`)? The harness can measure each skill script's growth rate.
2. Should the guard kill the most recent large process instead of the largest, to favour long
   calls that are nearly done?
3. Where does the capability key come from for the platform MCP server's tools?
4. Does the Kubernetes API proxy's cost check need the caller's output format, or is the object
   count enough?
