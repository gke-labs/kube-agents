# A Child Memory Budget for the Credential Proxy

> **STATUS — implemented.** The credential proxy caps how many
> requests run commands at once, and its container's memory limit was sized against that cap
> and the output each request may hold. The child processes those requests spawn were outside
> that arithmetic, and on a wide-scope install they were most of what the container held.
> This document adds the missing term: a budget for child memory, derived from the limit the
> container actually has, that admission honours alongside the slot cap.

**Scope:** How the broker (`agents/platform/scripts/credential_proxy.py`) decides whether it
may spawn one more `gcloud`, `kubectl` or `git`, where it learns its own memory limit, and what
the operator's sizing test asserts about the two.
**Owns:** the child memory budget, its derivation, its admission rule, and the Downward API
variable that carries the limit.
**Does not own:** the per-request slot cap and output cap, which
[`docs/credential-isolation-design.md`](../credential-isolation-design.md) describes and which
this budget sits beside; the size of a single `kubectl` child (#2045); the Controller Stall
Watch's own schedule and sweep budget (#2325).

## 1. What was true on `main` before this change

The broker admits at most `CREDENTIAL_PROXY_MAX_CONCURRENT_COMMANDS` requests that run commands
(8 as the operator deploys it), in arrival order, each holding its slot from admission until its
response is written. The operator's sizing test
(`TestCredentialProxyOutputCapClearsTheLargestFleetDump`) asserts that the output the slots can
hold, six copies of the 8 MiB output cap per slot, fits under the container's 1Gi limit next to
its 512Mi request. The test says in a comment that the children are outside the arithmetic and
"the rest of the limit is what holds it".

Measured on an install whose `spec.scope` resolves to 36 projects and 143 clusters, during one
Controller Stall Watch tick (100 ms samples inside the proxy container, 2026-10-05):

| Child                                                                | Peak resident   |
| -------------------------------------------------------------------- | --------------- |
| `gcloud container clusters list` / `describe` / `get-credentials`    | 101 to 102 MiB  |
| `gcloud config config-helper` (the GKE auth plugin, under `kubectl`) | 71 MiB          |
| `kubectl`, any read                                                  | 45 to 48 MiB    |
| every child together, at peak                                        | 817 MiB         |
| the container's cgroup, at peak                                      | 948 MiB of 1024 |

The slot cap held throughout: the broker's own log, which stamps every request's admission and
completion, shows at most eight requests in flight at the listing burst, all of them `gcloud`,
and never a ninth. The process sampler nonetheless counted up to twelve `gcloud` processes at
once, because a request's child is a small process tree rather than one process: `gcloud` spawns
short-lived `gcloud` children of its own, and the GKE auth plugin spawns `config config-helper`
under a `kubectl`. What the budget has to charge is the tree, per request. Across the eight
listings the children summed to 817 MiB, about 102 MiB per request. The broker itself held
104 MiB and Envoy 64 MiB. Earlier the same day the container was OOM-killed (exit 137) seconds
after eight `gcloud` requests were admitted within 400 ms (#2324).

Two things follow. A `kubectl` request can cost about as much as a `gcloud` request, roughly
120 MiB, once the helper under it is counted, so a `gcloud`-only rule would leave the same hole
open from the other side. And the slot cap, which counts requests, has no relation to the limit,
which holds processes: eight heavy requests at the 128 MiB this design charges each, plus the
broker's and Envoy's resident 168 MiB, plus the output the eight slots may hold, is more than
1Gi whichever executable fills the slots.

The broker did not read its own memory limit or usage anywhere. The operator already handed the
event watcher its container memory limit through the Downward API
(`eventWatcherMemoryLimitEnv`, a `resourceFieldRef` on `limits.memory`); the proxy container got
nothing of the kind.

## 2. The change

### 2.1 A reservation per request

Every request that runs commands reserves `REQUEST_CHILD_MEMORY_RESERVE_BYTES`, 128 MiB, for its
children when it is admitted, beside the slot it takes, and releases it with the slot. One size,
not a size per executable, because every route can end up running the heavy case:

| What a request can spawn                                                | Measured                                                                                                                                                                                |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gcloud`, directly or on a `kubectl`'s cold path                        | 101 to 102 MiB per process; 102 MiB per request across eight listings                                                                                                                   |
| `kubectl` with the auth plugin's `gcloud config config-helper` under it | 48 plus 71 MiB                                                                                                                                                                          |
| the forge credential helper, then `git`                                 | the helper is Python; the full refresh runs `gh` always and `gcloud auth print-identity-token` when federation is absent; the read-only mint runs at most that one `gcloud` and no `gh` |

The last row is why there is no cheaper class for `git`. The vcs verbs and the forge refresh
route reach `refresh_forge_credential`, whose helper (`github_token_refresh.py`) always runs
`gh auth login --with-token` and `gh auth setup-git` after its own Python, and runs
`gcloud auth print-identity-token` only when it cannot get an identity token without one:
federation is tried first and spawns nothing, so on a federated broker, where the operator sets
the credential-file variables to the federation file it lays down, no `gcloud` runs; on an
unfederated one it runs when a credential file is set or the metadata server yields no token.
The content-workspace `open` and `commit` reach the same helper through `mint_read_credential`
with its read-only flag, which prints a minted token and returns before any `gh`: at most that
one `gcloud`, under the same conditions, and nothing else. Either way the vcs route can run a
Python helper, a `gcloud` and a `gh` behind one `git`, which is what the single reserve has to
hold; the mint path is lighter, and §2.1 below takes it out of the per-request budget on other
grounds. The reserve is sized for the
tree a request spawns, not for one process: `gcloud` starts short-lived `gcloud` children of its
own, which is why the sampler saw twelve processes for eight requests, and the figure that
matters is the 102 MiB per request the eight listings summed to. The margin to 128 MiB covers
that; the live check in §4 (cgroup `memory.peak` under the limit across three ticks) is what
confirms it against the trees as they actually run.

One reservation per request, not per child, because a request's children run one after another:
the exec route runs the command it was asked for, and on a `kubectl`'s cold path the
`get-credentials` into the managed kubeconfig and the `describe` behind the DNS-endpoint decision
before it; a vcs verb runs several `git`s back to back. `_execute` spawns under the reservation its thread
holds, recorded the way the shared request deadline already is. A spawn on a thread holding none
takes a transient reservation for the child's lifetime, with the same wait and the same refusal;
no shipped route reaches that branch once every route reserves at its start, and it exists so a
new route that forgets to is throttled rather than uncounted.

Where each route reserves, and why there:

- The exec and vcs routes reserve where they take their slot. On the vcs route that is before
  the body is read, so the route's existing handling of a refusal, including its body drain,
  applies unchanged.
- The forge refresh route takes no slot, and most of its calls spawn nothing:
  `refresh_forge_credential` returns from its cache when the repository is in the last refresh's
  scoped set and that refresh is inside `FORGE_REFRESH_COALESCE_SECONDS`, which is the common
  case for the sandbox `gh` wrapper and the fleet-audit skill, both of which call it before every
  credentialed step. So the route reads the coalesce cache first, without the refresh lock, and
  re-raises a failure recorded since it arrived. Otherwise it takes `_refresh_lock` first, under
  the admission wait counted from arrival: refused busy past it, whether or not the budget is on.
  Under the lock it repeats both checks, and only then, with the budget on, reserves for the
  helper it is about to run, on the reservation's own admission wait, and releases the
  reservation with the helper. So only the caller that runs the helper ever holds a reservation:
  every other refresher waits on the lock holding none, and a cache hit never waits for either.
  A route holder waiting for the budget yields the lock to a vcs verb that needs a refresh: the
  verb runs the helper under its own reservation while the route caller queues behind it and
  coalesces on its result, since otherwise the verb would wait on the lock while the holder
  waits for budget the verb holds. The yielded caller's wait for the verb has a bound of its own,
  `COMMAND_SLOT_WAIT_SECONDS` from the yield, because the budget wait ran on its own clock and may
  have spent the arrival bound; refused past it, the caller is told it stepped aside, with the
  time it spent since arrival. It yields once: a caller whose re-check is still stale after the
  yield (the verb refreshed another org, or its helper timed out) reserves on what remains of the
  yield bound and does not yield again: a vcs verb that counts itself during that second wait holds
  the budget the wait needs, so the caller steps aside for good, refused busy, and the verb runs the
  helper under its own reservation; the lock is never held across a budget wait against a verb
  that holds the budget. Another route refresher waits on the lock as before. A
  refresher behind a helper that runs past the bound is told busy although that helper lands the
  token seconds later; the client
  reports a failed refresh, and its next call coalesces. The route hands its connection to both
  waits, as the exec and vcs routes hand theirs to the slot wait, so a caller that hangs up while
  queued is dropped before the helper runs, and the route has a handler for that drop (a log line
  and no response, as the exec route has) beside the busy handler. Called from inside a vcs
  request, the function runs under that request's reservation and takes none; it reads the
  coalesce cache without the lock, and otherwise waits for the lock as long as it takes; from the
  content-workspace `open` and `commit`, through `mint_read_credential`, it runs under the store's fixed term.
- The content-workspace verbs take no reservation at all. The store serves one verb at a time
  under its single lock, across every open workspace, so at most one of its process trees exists
  at any moment, whatever the load; that is a fixed quantity, and the budget carries it as a
  fixed term, `CONTENT_WORKSPACE_RESERVE_BYTES` (128 MiB, one request's worth), subtracted
  alongside the resident reserve in §2.2. Nothing on the store's path waits for admission, so the
  store's recorded reason for taking no slot (a wait while holding its lock would stall every
  verb behind it, reads included) is left standing rather than reversed, and the two states in
  which the budget is held for long, a listing burst and four long-running commands, cannot
  stall a `read` or refuse a `publish`. The store's spawns stay uncounted individually but not
  unbounded: the lock is the bound, and the reserve is its size. A slot-less request that does
  reserve, which is the forge refresh route alone, joins the same arrival-order queue
  as a slot taker, may pass slot takers that only the slot cap holds (§2.3), and leaves it when
  it holds its reservation.

- The cold path reserves nothing of its own: it runs under its `kubectl` request's reservation,
  so nothing waits for admission under `_kubeconfig_lock`.

Besides the store's (§2.1), two spawns stay outside the budget on purpose: the one-off bootstrap command at startup, which
runs before the broker serves, and the `git config` read of a repository alias, which is a few
hundred kilobytes and over in milliseconds.

### 2.2 The budget

At startup the broker computes what the children may hold together:

```
children_budget(now) = memory_limit
                     - BROKER_RESIDENT_RESERVE_BYTES           (192 MiB: broker + Envoy, measured 168)
                     - CONTENT_WORKSPACE_RESERVE_BYTES         (128 MiB: the store's one tree at a time, §2.1)
                     - OUTPUT_COPIES_PER_COMMAND * max_output_bytes * slots_in_use
```

The last term is the output each admitted request may hold, at the same six copies the sizing
test models, counted for the slots in use now rather than for the cap, so a broker with two
requests in flight is not charged for eight. A request is admitted when a slot is free and the
sum of live reservations plus its own fits `children_budget` with its slot counted.

(The default limit is now 2Gi, raised when the kanban slot default rose to six: 1728 MiB after the reserves admits nine,
and the slot cap of eight binds. The figures below are the 1Gi limit this design was written
against.)

At the operator's defaults (1Gi limit, 8 MiB output cap) a request costs 128 MiB plus 48 MiB of
output allowance, 176 MiB, against 704 MiB after the two fixed reserves: four requests that run
commands are in flight at once (4 × 176 = 704), whatever order they arrive in, where the slot cap
alone admitted eight. The reconciler's and the stall watch's listing pools are four wide (§2.6),
the admitted count; at eight wide each listing budget, sized for eight at a time, would have run
out with about forty of a hundred projects unlisted at a 10-second listing. When the hourly
reconcile and a stall-watch tick coincide the two pools together submit eight against four
admitted, and the second wave waits one listing: over two hours on the install above, 414
`gcloud` requests took 2 s at the median, 18 s at the 95th percentile and 25 s at most, within the
60-second bound with room to spare.

Four requests at once is the cost of the design at the default limit, and it binds long-running
commands too: four `kubectl logs --follow` or `kubectl wait` hold the budget for as
long as they run, where the slot cap alone let eight. An install that needs more raises the limit and the
budget follows: `spec.deployment.credentialProxy.resources` moves the proxy container's limit,
the one knob, and the budget is what makes it safe to set in either direction, since raising it
lets the budget admit more, up to the slot cap, and lowering it cannot OOM while it stays at or above the floor
(§2.3); below the floor the budget is off and the slot cap alone holds, so the operator refuses
such a limit at reconcile, and the webhook refuses it at apply where it is enabled. The output term is
the worst case, a request holding its full capped output, which a listing never does; charging
captured bytes instead of the cap would roughly double the concurrency and is the refinement to
measure first if four proves tight.

### 2.3 Waiting, refusing, and the degenerate case

A request that fits a slot but not the budget waits where it already waits for a slot: the same
arrival-order queue under the same condition variable, woken when a reservation is released or a
slot freed, polled every `COMMAND_SLOT_POLL_SECONDS` with the same check that a caller which has
hung up while queued is dropped before anything starts, and bounded by the same
`COMMAND_SLOT_WAIT_SECONDS` (60). A request still queued at the bound raises
`CommandSlotUnavailable` with a message that names the memory budget rather than the slot count.
On the exec and vcs routes the exception is raised where a slot refusal is raised, before any
command has run and, on the vcs route, before the body is read, so each route's existing handler
and the vcs route's body drain apply unchanged and answer `503 CREDENTIAL_PROXY_BUSY`. The
forge refresh route takes no slot, and has its own busy handler answering the same 503, so a
refusal does not reach its generic branch and read as a `FORGE_TOKEN_REFRESH_FAILED` 502; it
also has the hang-up handler §2.1 names. The content-workspace route reserves nothing (§2.1) and
needs neither. The sandbox shim, the `busy`
metric status, the audit record and the site's troubleshooting entry see the same signal as for a
slot refusal, with a different sentence in it. A wait of a second or more logs, as a slot wait does:
`request waited %dms for memory budget (… MiB in use of … MiB: … MiB reserved for children, … MiB of output allowance for … requests)`, the figure the admission check compares.

The queue keeps arrival order with one exception. A slot-less reserver that fits the budget is
admitted past the tickets ahead of it when every one of them takes a slot and the slots are full:
those wait on the slot cap, not the budget, and the reserver competes with them for nothing, so
eight `kubectl logs -f` holding every slot and a ninth exec queued behind them do not keep a forge
refresh waiting out the bound. Behind a ticket the budget holds, order is kept, because a reserver
admitted ahead of it would take the budget that ticket is waiting for and could starve it. A
request that fits but is still queued at the bound is refused naming the queue rather than the
budget: `the credential proxy's admission queue is held by requests waiting for its child memory budget (…) and this request waited 60s behind them`.

A request whose own cost exceeds the budget while no other request is admitted is admitted anyway,
with a warning logged once per process. Otherwise a limit small enough to make the budget
negative would refuse every command forever, which is worse than the OOM it was meant to
prevent. The floor below pre-empts this case: a budget too small for one request is under it and
turned off at startup, so the branch is defensive, kept for a floor lowered later. The operator's
sizing test (§2.5) makes sure the operator's own numbers never reach it.

A budget that would admit fewer than `BUDGET_MINIMUM_ADMITTED_REQUESTS` (two) requests at once is
treated as absent: the broker logs one WARNING at startup naming the limit it read and the floor,
`child_memory_budget_floor_bytes` (672 MiB at the 8 MiB output cap: the two fixed reserves plus two
requests' cost), and admits by slot alone. The case is real. GKE Autopilot without bursting sets a
container's limits equal to its requests, so the proxy's limit there is its 512Mi request, under
which the budget would admit one request and serialise every brokered command; a listing phase
that cannot run two at once costs more than the out-of-memory exposure the budget prevents. The
operator's sizing test holds its own limit to the same floor
(`credentialProxyMinimumAdmittedRequests`).

### 2.4 Where the limit comes from

In order:

1. `CREDENTIAL_PROXY_MEMORY_LIMIT_BYTES`, which the operator sets through the Downward API as a
   `resourceFieldRef` on the proxy container's `limits.memory` with divisor 1, the pattern the
   event watcher already uses. Read from the Deployment rather than copied from the Resources
   block so the two cannot drift: a limit changed by `spec.deployment.credentialProxy.resources`, or by a Vertical Pod
   Autoscaler recreating the pod, is the limit the broker budgets against.
2. `/sys/fs/cgroup/memory.max`, for a broker whose Deployment carries no such variable: an
   image paired with an operator older than this change, or a run outside the operator. A
   variable set to anything but a positive integer is logged at WARNING and read as unset. A
   value of `max` in the file means no limit.
3. Neither readable, or unparsable, or `max`, or under the floor (§2.3): the budget is disabled,
   logged once at startup, and admission is by slot alone.

The derivation runs in `serve`, which builds the executor from the parsed arguments, and hands
`CommandExecutor` an explicit optional limit that defaults to none. A test that constructs the executor directly therefore gets
no budget unless it passes one, whatever cgroup the test runner is in; a CI container's own
memory limit, which `/sys/fs/cgroup/memory.max` reports there, cannot make a slot-only assertion
flake. The startup tests that call `serve` directly set the variable or assert the disabled
branch explicitly.

The resident reserve assumes the layout this operator deploys, the `broker` role, where the
container holds the broker and Envoy alone. The image's default role is `combined`, which also
runs the event watcher and drift detector, and it is the compatibility arrangement for an image
paired with an older operator, the pairing the cgroup fallback serves. There the reserve omits
the watcher's informer caches, so the budget is generous by that amount: a budget that is too
large by a known term for one transitional pairing, where before this change there was none.

The operator reserves the variable's name in `mergeCredentialProxyEnv` by setting it in the base
env list, as it does the two caps: a CR that could set it would detach the budget from the limit.

### 2.5 The sizing test's child term

`TestCredentialProxyOutputCapClearsTheLargestFleetDump` asserts
`request + output_burst(all slots) <= limit`, with the container's 512Mi memory request as the
resting term, upstream's statement of what the pod holds with nothing in flight. It also makes a second assertion in the broker's own terms, with the broker's fixed reserves as the
resting term, because those are the numbers the broker subtracts and so the budget the children
see. A slot in use is a request holding a reservation, so the slot count and the reservation
count are one variable, not two, and the assertion is the admission rule itself at a floor:
the budget admits at least two requests at once, each with its output allowance, so a listing
phase still parallelises at all:

```
resident_reserve + workspace_reserve + 2 * (copies * output_cap + request_reserve) <= limit
```

At the defaults that is 192 + 128 + 2 × 176 = 672 MiB against 2048 (1024 when this design was
written). The test derives the count the rule admits from the rendered values,
`floor((limit − reserves) / (copies × cap + reserve))`, nine at the 2Gi default (192 + 128 + 9 × 176
= 1904 MiB against 2048; four at the original 1Gi), held to eight by the slot cap, names it in its
failure message, and fails below two, so a future reduction of the
limit or a raise of either cap has to argue with the number it prints. The slot cap does not
enter the rule except as an upper bound, and the test says so where a reader would otherwise
expect it to. The broker holds the six output copies as `OUTPUT_COPIES_PER_COMMAND` beside
its other budget constants, and the two fixed reserves, the request reserve and the copies are
constants in `credential_proxy_manifests.go` that name their Python counterparts, so a change to
one side has a name to search for on the other.

The test also asserts the memory-limit variable is rendered as a `resourceFieldRef` on the proxy
container's `limits.memory`, not as a literal, so a change to the Resources block cannot leave
the broker budgeting against a stale number.

### 2.6 What does not change, and what the implementing change also touches

The slot cap and output cap, their values and their reservation. The sandbox shim
(`credential_proxy_client.py`): a 503 is still printed and exit 1 returned; with four heavy
requests at once the eight-wide burst of both listing pools coinciding waits one listing, so no
retry is added here. (Those are the 1Gi figures; at the 2Gi default the budget admits nine and
the slot cap of eight binds.) The proxy container's requests and limits, and so the
chart's generated footprint and quota preflight. No agent-visible behaviour: no eval case.

The reconciler's and the stall watch's listing pools are the caller settings the change moved.
The reconciler's had been eight at the default `maxProjects` cap and up to sixteen when the cap
was raised; it is now four at every cap, the count §2.2 admitted at the original 1Gi limit (the 2Gi
default admits nine, held to eight by the slot cap, so the four-wide pools now leave slots for
other callers), and its listing budget doubles to
300 seconds at the default cap to keep the 12 seconds per listing the 150-second budget gave at
eight wide, growing by that per default cap's worth of projects. The bootstrap gate's floor
follows the budget, from 240 to 390 seconds. The stall watch's pool goes from eight to four and
its listing budget from 150 to 300 seconds, for the same 12 seconds per listing; the listing runs
inside the tick's 1500-second budget, which does not change, so a tick whose listings use the
whole budget leaves its scans 150 seconds less.

Prose the change updated because it had become false: in
[`docs/credential-isolation-design.md`](../credential-isolation-design.md), the CLI-commands
bullet that explains the busy 503 by the concurrency cap alone, and the agent-supplied-kubeconfigs
consequence that says the two caps together are what the memory limit is sized against and quotes
the slot message; the site's troubleshooting entry for the busy 503, which quotes the slot
message and said eight long-running commands may hold slots, where four hold the budget;
the operator's three sizing comments, beside the Resources constants, beside the env block
(which named a 256Mi request) and in the sizing test, all of which said the children were
outside the arithmetic; and the three locking statements §2.1 extends without reversing:
`_execute`'s docstring (no lock is held while waiting for a slot, which now covers the budget
too), `request_slot`'s docstring (the content workspace's git takes no slot because the store's
lock serialises it, and no reservation for the same reason), and the store's own rationale,
which gained the sentence that its lock is what sizes the budget's fixed term. The docs map's
identifier-sources table gained a row for the constants that have to agree between the broker and
the operator (the two fixed reserves, the per-request reserve, the output copies and the floor).

## 3. Alternatives considered

**A Vertical Pod Autoscaler on the proxy.** It learns from minute-granularity usage samples and
from OOMKill events; the burst here lasts a few seconds every thirty minutes, so it would learn
by letting the container die, then evict the pod to apply each new size. It is also a cluster
addon the install does not control on a bring-your-own cluster, and it rewrites the requests the
chart's footprint and quota preflight sum. With the budget in place a VPA becomes a throughput
knob with no correctness risk, which is the only form it is suitable in.

**Sizing the proxy from the resolved fleet.** The operator does not know the fleet size; the
agent pod resolves `spec.scope` and writes `fleet_scope.json` to the data PVC, which the operator
never reads. Feeding it back is a new status field and a reconcile that restarts the broker, to
size on a variable that predicts the burst worse than the broker's own concurrency does: the
burst is four wide per listing pool on any install with four projects, however many
clusters they hold.

**A `gcloud`-only sub-cap.** The smallest diff. It leaves the `kubectl`-plus-helper term, which
the measurement shows to be the same size, unmodelled, and a static sub-cap set by the operator
does not move with the limit.

**Reading live cgroup usage at each spawn.** Adaptive and needs no reserve constant, but `memory.current`
counts reclaimable page cache, eight threads can pass the check before any child has grown, and
the behaviour cannot be asserted in a unit test. The reservation model is deterministic and
testable; a live reading could be layered on later as a second check if the constants prove
wrong.

## 4. Testing

Unit, in `agents/platform/scripts/test_credential_proxy.py`: the budget is derived from the
variable, from the cgroup file, and disabled when neither is readable or the file says `max`; a
request waits when the sum would exceed the budget and is admitted when a reservation is
released; the wait is in arrival order, except that a slot-less reservation that fits passes slot takers
the full slot cap holds and keeps its place behind one the budget holds, and drops a caller that
hangs up while queued; a request
still waiting at the bound raises `CommandSlotUnavailable` naming the budget, and the exec, vcs
and forge refresh routes each answer it with the existing 503 body, the vcs route before reading
its body; the output term is charged for slots in use, not the cap; a
`kubectl` request's cold-path `gcloud`s spawn under the request's reservation and take no second
one; no content-workspace verb reserves and the store's fixed term is subtracted whether or not a
workspace is open; a forge refresh that coalesces reserves nothing, one that runs the helper
reserves under the refresh lock, a refresher waiting on the lock holds no reservation and is
refused at the bound with the budget on or off, and the route drops a caller that hangs up while
queued and answers a refusal with the 503; a spawn on a thread with no reservation takes a transient one; the
degenerate case admits and logs once; a reservation is released with its slot, including after a
timed-out command and a caller hang-up; an executor constructed without a limit has no budget
whatever the process's cgroup says.

Operator, in `platformagent_manifests_test.go`: the extended sizing assertion in §2.5, the
`resourceFieldRef` assertion, and the variable's reservation against a CR override, alongside the
two caps' existing checks. Goldens under `k8s-operator/internal/testing/testdata/` re-blessed for
the new env entry; `make chart-check` confirms the footprint is unchanged.

Live, on the 143-cluster install (#2324's): build the agent and operator images from the branch,
deploy, and observe three Controller Stall Watch ticks. Expected: at four wide a listing phase alone
produces no second wave, so the broker log shows budget waits only when the hourly reconcile and a
stall-watch tick coincide or long execs hold the budget; every listing completes (no `CREDENTIAL_PROXY_BUSY` in the
stall-watch output), the container's cgroup `memory.peak` stays under the limit, the container
does not restart, and the tick's wall time is within a minute of what it was before the change.

## 5. Sequencing

One pull request: broker, operator, test, docs. There is no intermediate state worth shipping:
the broker change without the variable budgets from the cgroup file, which works but leaves the
operator's test blind, and the variable without the broker change is an unused env entry.
