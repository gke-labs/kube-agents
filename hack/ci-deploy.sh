#!/usr/bin/env bash
# ==============================================================================
# Prow CI Deployment Pipeline Script
# ==============================================================================
# The evaluation cluster and its IAM are pre-configured; this script builds
# the PR's images and deploys the kube-agents chart onto that cluster.
#
# Setting RC_COMMIT_SHA switches it to the release-candidate path: no build at
# all, and the chart's published GHCR images at that commit instead. Section 2a
# is the whole of the difference, and with the variable unset nothing below
# behaves differently from the day this line was added.
#
# Setting EVAL_MODE_NEXT=1 flips the installed agent to `spec.mode: next` after
# the today-mode install has proven itself (sections 2a and 2b refuse the flag
# where it cannot work, step 4 builds the A2A images and the bridge sidecar,
# step 5 hands the operator their references, the bridge's settings and the
# inject door, step 6b flips and gates; the constants block below says what
# each does). Unset, nothing behaves differently either.
# ==============================================================================

set -euo pipefail

# The session daemon caps Warning alerts at 5 per UTC day, fleet-wide per
# install (ALERT_DAILY_LIMIT_WARNING, #641). That cap is alert-storm
# protection for a human-watched channel; an eval install's whole job is
# generating alerts. Every smoke build that leases a pool project that day
# spends the same shared budget on its own crash-loop scenarios, and once it
# is gone the daemon quota-suppresses the very alert
# autoops-warning-event-triage waits 300s for, timing out the plant (#1101).
# 0 is the documented off-switch — `_alert_daily_limit` in
# agents/platform/scripts/session_kv_server.py parses it as "cap off" and
# `_claim_alert_quota` lets `limit <= 0` through uncapped — and setting it
# here, on the deploy, leaves the production
# default untouched. tests/test_ci_deploy_alert_quota.py pins the whole
# chain: this flag, the chart rendering it onto the CR, and the operator's
# env allowlist letting it through to the container.
readonly EVAL_ALERT_DAILY_LIMIT_WARNING="0"

# The drift bucket's own ceiling, off for the same reason and by the same
# mechanism. A drift case spends one inject per audit record that survives the
# classifier, and the regression it is there to catch — a classifier that stops
# filtering — spends one per record that should have been dropped, so the
# failing run is the one that needs the most headroom. A finite ceiling is worse
# than no ceiling here rather than merely tighter: a repetition that begins with
# one slot left files a card for the first record and is refused the rest, which
# is what a working filter looks like from the outside. Uncapped, an unfiltered
# pipeline files every card it should not have and the case reds.
#
# What that costs, stated rather than discovered: one lease deploys one install
# and runs the whole matrix against it, so this ceiling is off for every case
# rather than for drift ones, and the board it fills is shared
# (kanban.max_in_progress is 2). A single human-tier record therefore files
# unbounded cards and can starve unrelated cases in the same lease. On a leased
# pool project almost every principal is a service account and the classifier
# drops it, so the steady state is quiet; the triggers are a maintainer running
# kubectl against a leased cluster mid-run, a `user:` principal in the project,
# the classifier regression this setting exists to expose, and the backlog
# below. Accepted because a finite cap makes that regression green, which is
# the failure that matters.
#
# The backlog is the widest of those and is not bounded by the lease. The sink
# exports every cluster in the project and the subscription keeps what nothing
# has acked for terraform/modules/drift-pubsub's default 31 days, never
# expiring; teardown uninstalls the chart, so no detector pulls between leases.
# Each lease therefore opens on everything human-tier logged since the last one
# drained — on a freshly provisioned project, including the provisioning. This
# script cannot drain it: seeking the subscription needs
# pubsub.subscriptions.seek, and provision_ci_pool_project.sh gives the runner
# roles/viewer, which carries get and list and not that. Shortening retention
# for pool projects is the fix and is a tfvars change rather than one made
# here; #2491 tracks it.
readonly EVAL_ALERT_DAILY_LIMIT_DRIFT="0"

# The subscription the drift detector pulls from. The pool project's own
# provisioning owns the resource: scripts/provision_ci_pool_project.sh sets
# enable_drift_pubsub in the full-install tfvars, and terraform/modules/
# drift-pubsub creates the sink, the topic and this subscription and grants
# kubeagents-platform-gsa subscriber and viewer on it. Nothing is created here
# — the install has one engine, and a `gcloud pubsub create` beside the module
# would be a second expression of the same step.
#
# The name is restated rather than read back because this helm upgrade replaces
# the composition's whole value set, the subscription included, so an install
# that had it loses it unless the deploy puts it back. It has to stay equal to
# full-install's `drift_pubsub_subscription` default;
# tests/test_ci_deploy_drift_detector.py pins the two.
readonly EVAL_DRIFT_SUBSCRIPTION="platform-agent-drift-audit-sub"

# What step 6 waits for to call the detector started, and where. The line is
# k8s-operator/cmd/drift-detector/main.go's last before the pull loop, so it
# clears flag parsing, the cluster-name check against the pod's own
# credentials, and the ack-deadline read -- each of which otherwise exits the
# process on every start with the pod still Ready. The detector runs in the
# credential-proxy sidecar, not the agent container
# (deploy/docker/Dockerfile), so the logs call has to name it.
readonly EVAL_DRIFT_READY_MARKER="drift-detector: pulling"
readonly EVAL_DRIFT_READY_CONTAINER="agent-api-auth"
# 5 minutes. start-services.sh holds the first launch until the Session KV
# daemon is listening, and backs off between restarts, so this is well clear of
# a cold start rather than tight against it.
readonly EVAL_DRIFT_READY_ATTEMPTS=60
readonly EVAL_DRIFT_READY_INTERVAL_SECONDS=5

# Deliberately not --log-dropped here, though the detector takes it and
# deploy/shared/start-services.sh will pass it on any install that sets
# DRIFT_DETECTOR_LOG_DROPPED through spec.deployment.env. One deploy serves the
# whole eval matrix, so the cost is paid by every lease rather than by drift
# cases: the post-sink stream runs 1 to 10 records a second and is about 98%
# system tier (terraform/modules/drift-pubsub/main.tf measures ~60k/day on a
# two-cluster project, and a pool project carries the host cluster plus the
# seeded fleet), so a line per drop is most of the audit stream copied into the
# sidecar's stderr and shipped on to Cloud Logging.
#
# A fixture does not need it to tell an empty ingress from an over-eager
# filter. The detector already prints `idle, no messages delivered in %s
# (parsed=%d skipped=%d failed=%d)` on a timer and `pull failed, retrying` when
# the subscription cannot be read (k8s-operator/cmd/drift-detector/
# subscriber.go), which separates the three states; `skipped` is the count a
# noise-filter case asserts on. What --log-dropped adds over that is which
# record and why, and that is worth a targeted rerun rather than every lease.

# The kanban board's worker cap on the eval install. The image ships
# kanban.max_in_progress: 2 (agents/chat/config.yaml), a floor for an install
# that has not measured its own worker footprint, and the operator renders a
# different cap only when the CR carries spec.harness.tuning.maxInProgress.
# The eval fans its units out at EVAL_TASK_PARALLELISM (4 on a pull request,
# 8 on the nightly since oss-test-infra#2707), and nearly every unit's
# opening turn delegates one platform card, so on the image default most
# lanes queue behind two slots: a queued card waits out the cards ahead of
# it and then runs its own 10-45 minutes, past the 2700-3000s delegation
# ceiling with no worker at fault, while the dispatcher logs the same "ready
# queue non-empty ... 0 workers spawned" warning a wedged worker produces
# (#1879, #1880). This bounds the queueing share of what remained after
# their fixes (#2032); the same nightly also had workers wedged for a whole
# delegation by the v2026.9.14 base's approval-regex hang on large terminal
# commands, a separate holder of the same slots that the Hermes bump removes.
#
# Five, not the lane count. The cap bounds ACTIVE workers, and a coordinator
# waiting on the children it fanned out gives its slot back but stays
# resident (deploy/docker/patches/kanban_scheduling.py, Part 4), so the
# process count is the cap plus the waiting coordinators. The gateway
# container's 8Gi memory limit (resolveResources in
# k8s-operator/internal/controller/manifest_helpers.go) was sized for five
# concurrent workers over a 1.8GiB idle set, and a worker the cgroup OOM
# killer takes strands its card with no restart and no event: the same shape
# as the queue this removes, indistinguishable from it in the run record. So
# the cap stops where the sizing stops: five covers the pull request's four
# lanes with one slot for a fan-out child, and the nightly's eight lanes
# still queue three deep until the working set at five is measured and the
# eval install's memory limit is raised together with the cap (the CR patch
# hack/kind-up.sh makes after helm is the shape; #2032 carries the
# measurement). Set on this install only, so the production default stays
# where the CRD reference argues it should. tests/test_ci_deploy_kanban_cap.py
# pins the flag, the floor under the pull request's lanes, the ceiling the
# memory limit was sized for, and the chart rendering the value onto the CR.
readonly EVAL_KANBAN_MAX_IN_PROGRESS="5"

# The release step 5 installs, and — for the poisoned-record guard (#1172) —
# the label pair Helm stamps on every release-record Secret it writes
# (`owner=helm` plus `name=<release>`), selecting every revision's record of
# this release and nothing else in the namespace.
readonly HELM_RELEASE_NAME="kube-agents"
readonly HELM_RELEASE_SECRET_SELECTOR="owner=helm,name=${HELM_RELEASE_NAME}"
# What a healthy revision looks like in `helm history -o json` output. The
# encoder emits compact `"status":"deployed"`; the pattern tolerates spacing
# so a Helm formatting change cannot silently blind the guard.
readonly HELM_DEPLOYED_STATUS_RE='"status"[[:space:]]*:[[:space:]]*"deployed"'
# What an absent release looks like in `helm history` stderr output ("Error: release: not found").
readonly HELM_RELEASE_NOT_FOUND_RE="release: not found"

# Bounded retry attempts for the Helm chart install. Transient API-server 5xx
# errors (500, 502, 503, 504) during Autopilot control-plane scaling or cluster
# startup can fail the first attempt; retrying proceeds to evaluation (#2382).
readonly HELM_DEPLOY_ATTEMPTS=3
readonly HELM_DEPLOY_RETRY_DELAY_SECONDS=5
readonly APISERVER_READYZ_TIMEOUT_SECONDS=30
readonly APISERVER_READYZ_POLL_INTERVAL_SECONDS=2
readonly HELM_HISTORY_PROBE_ATTEMPTS=3
readonly HELM_HISTORY_PROBE_RETRY_DELAY_SECONDS=2
readonly HELM_API_SERVER_5XX_RE="an error on the server|the server is currently unable to handle the request|the server was unable to return a response in the time allotted|the server responded with the status code 50[0234]|Internal error occurred:|etcdserver:|request did not complete within|(HTTP( response status)?|status:)[: ]+50[0234]([^0-9]|$)"

# The keypair the agent uses to reach its shell sandbox over SSH. Generated per
# run and thrown away with the lease: nothing outside this cluster ever sees it,
# and the next run's install gets a pair of its own.
readonly SANDBOX_SSH_KEY_TYPE="ed25519"
readonly SANDBOX_SSH_KEY_COMMENT="kube-agents-ci-eval"

# EVAL_MODE_NEXT=1 flips the eval install to `spec.mode: next` once the
# today-mode install has passed step 6, so the matrix can be run against the
# next stack: the presubmit's on demand, or the next lane's periodic and
# nightly on main (#1686, measuring #1661). Unset, or set to
# anything but "1", is today: every line the flag guards is skipped and the
# script behaves exactly as it did before the flag existed.
#
# What the flag has to do, and where:
#   - section 2a refuses it on the release-candidate path (that path hands
#     the operator none of the settings step 4 does) and section 2b refuses it on a Prow
#     run that is neither a pull request's nor one of the next-lane jobs
#     named below (a mis-set variable on the nightly or a postsubmit would
#     otherwise run that job in next mode, recording and publishing nothing,
#     so main's window and dashboard would silently miss it), and refuses a
#     fan-out the bridge cannot be given as its concurrency, before anything
#     is built;
#   - step 4 also builds the A2A gateway, auth callout, worker and console
#     images from a2a/Dockerfile.* (the pull request's own builds, the same
#     way the four images above are; the operator would derive these same
#     references from its own image, and step 5 names them anyway so the
#     deploy's inputs are explicit) and the Hermes bridge sidecar image, FROM
#     the platform-agent image of the same build;
#   - step 5 passes those references to the operator through the chart's
#     operator.extraEnv, which the operator reads as its image overrides,
#     with the bridge's concurrency beside them, and arms
#     the gateway's inject door the same way (A2A_INJECT_BACKEND=true);
#   - step 6b patches the CR (the mode, and the maxSessions section 2b sized
#     for the bridge's workers), waits for the agent Deployment to roll, gates
#     on the NATS StatefulSet, the callout Deployment, the provisioning Job
#     and the agent Deployment, in that order, waits
#     for the inject door's Service and token Secret, and waits for the bridge
#     sidecar the operator rendered into the agent pod to log that it is
#     consuming `platform` tasks.
# hack/ci-eval-pr.sh then runs the matrix through the door (AGENT_TRANSPORT=
# inject) under the same flag. The chart deliberately renders no spec.mode
# (docs/designs/spec-mode-switch.md), so the flip is a merge patch on the CR
# the chart created. The names below are what the operator renders for a CR
# of this name (a2aNATSName, a2aCalloutName, a2aGatewayName, a2aInjectName and
# the provision Job's component label in
# k8s-operator/internal/controller/platformagent_a2a_manifests.go; the managed
# .env rides the <cr>-config ConfigMap; the creds Secret's name in the API
# package, the bridge's user in platformagent_a2a_identities.go and its
# password key in platformagent_a2a_manifests.go). The CR name is the chart's
# platformAgent.name default, which this deploy does not override.
readonly PLATFORM_AGENT_CR_NAME="platform-agent"
# Timeout for deleting the PlatformAgent CR during retry, allowing the live
# operator to clear its finalizer before uninstallation. Matches
# charts/kube-agents/values.yaml cleanupHook.timeout (120s).
readonly PLATFORM_AGENT_CR_DELETE_TIMEOUT="120s"
# The next lane's Prow jobs: its on-demand presubmit, its six-hourly periodic
# on main, and its daily full-catalog nightly on main (the agent on Claude,
# compared with a today-mode nightly on the same model). Section 2b admits
# the flag on a run whose JOB_NAME is one of these (space-separated, matched
# whole) or that carries a PULL_NUMBER -- the presubmit is admitted by the
# second on a pull request and by the first on a Tide batch, the periodic and
# the nightly only by the first -- and refuses it on any other Prow run, so
# the flag leaking into the today nightly's or a postsubmit's environment
# still stops the deploy at second zero. The names are the jobs' own in
# oss-test-infra (prow/prowjobs/gke-labs/kube-agents/); a rename there is a
# one-line edit here. hack/ci-eval-pr.sh keeps such a run out of the baseline
# recorder on the flag alone, so admitting a job here never lets it write
# main's window.
readonly EVAL_MODE_NEXT_JOB_NAMES="pull-kube-agents-smoke-test-next ci-kube-agents-eval-next ci-kube-agents-eval-nightly-next-claude"
readonly AGENT_DEPLOYMENT_NAME="${PLATFORM_AGENT_CR_NAME}-gateway"
readonly OPERATOR_DEPLOYMENT_NAME="${HELM_RELEASE_NAME}-controller-manager"
# The first patch: the mode, and the maxSessions section 2b sizes for the
# rendered bridge's workers, in one merge so the first render -- and so the
# first provision Job -- sees both. A printf format; %d is MODE_NEXT_MAX_SESSIONS.
# The field path is the CRD's (HarnessSpec.Tuning.MaxSessions in
# k8s-operator/api/v1alpha1), which TestCiDeploySizesMaxSessionsToTheTasksFloor
# holds by decoding this patch into the type.
readonly MODE_NEXT_PATCH_FORMAT='{"spec":{"mode":"next","harness":{"tuning":{"maxSessions":%d}}}}'
readonly MODE_NEXT_GENERATION_ATTEMPTS=60
readonly MODE_NEXT_POLL_SECONDS=5
readonly MODE_NEXT_ROLLOUT_TIMEOUT="600s"
# The inject door: one name for its Service, token Secret, principal map and
# NetworkPolicy, and the key the token sits under (a2aInjectName,
# a2aInjectTokenKey). hack/ci-eval-pr.sh reads the same Secret for the
# harness's AGENT_INJECT_TOKEN.
readonly A2A_INJECT_NAME="${PLATFORM_AGENT_CR_NAME}-a2a-inject"
readonly A2A_INJECT_TOKEN_KEY="token"
readonly A2A_INJECT_BACKEND_ENV_VAR="A2A_INJECT_BACKEND"
readonly A2A_INJECT_BACKEND_ON="true"
# The bridge sidecar. The operator renders it into the agent pod under next
# (platformagent_a2a_bridge.go): its bus identity, its env and the bus token
# it must not mount are the operator's, not this script's. The script names
# the container only to read its log (a2aBridgeContainerName).
readonly BRIDGE_SIDECAR_NAME="hermes-bridge"
# The lane runs the bridge's shipped default executor, api, the one a customer
# install runs. Nothing here sets it: with the operator's A2A_BRIDGE_EXECUTOR
# unset the rendered bridge carries no BRIDGE_EXECUTOR, and the bridge picks
# api when BRIDGE_EXECUTOR is unset and the API_SERVER_KEY the operator sets on
# it is present (bridgeExecutor in a2a/cmd/hermes-bridge/main.go).
# A bridge without the key falls back to cli with a warning, so the start-line
# wait below requires this executor rather than trusting the default. Under api
# a task is a turn in the pod's Hermes API server, whose profile is the chat
# path's own (docs/designs/eval-next-transport.md, "What the lane grades").
readonly BRIDGE_EXECUTOR_EXPECTED="api"
# BRIDGE_CONCURRENCY is sized against the matrix's fan-out: hack/ci-eval-pr.sh
# runs EVAL_TASK_PARALLELISM units at once from the same job environment,
# defaulting to 4 (the nightly sets 8), and every unit past the bridge's
# concurrency waits in its queue for the whole budget and is classified as
# infrastructure (docs/designs/eval-next-transport.md, the executor
# paragraph). The default here is pinned equal to the eval script's by
# tests/test_ci_deploy_mode_next.py. The queue behind the workers holds 1024
# (taskQueueCapacity in a2a/hermes-bridge/bridge.go) before the bridge
# finalizes an accepted task as `bridge-queue-overflow`; a fan-out of 4 or 8
# never approaches it, so the bound below catches a typo, not a sizing. Under
# the api executor each worker's turn also counts against the pod's Hermes API
# server cap, gateway.api_server.max_concurrent_runs (10 by default at the
# pinned hermes-agent tag), which kanban card-completion wakes share; a turn
# refused there ends `hermes-rate-limited`, infrastructure, not graded.
readonly EVAL_TASK_PARALLELISM_DEFAULT=4
readonly BRIDGE_QUEUE_CAPACITY=1024
# The TASKS consumer budget's terms, as the operator sizes it
# (k8s-operator/internal/controller/platformagent_a2a_manifests.go): a fresh
# stream is created at max(budget, A2A_TASKS_FLOOR), where the budget is
# maxSessions * A2A_SESSION_CONSUMERS + A2A_RESERVE_FIXED +
# A2A_RESERVE_PER_WORKER * (the bridge workers the pod runs, rendered or
# declared); provisioning never edits a stream that exists, and a later
# render whose budget exceeds the live stream is refused. The operator
# budgets the bridge from the first next render, with the BRIDGE_CONCURRENCY
# step 4 hands it, so the first provision already counts the lane's workers
# and there is no second render to fit. Section 2b still sizes
# spec.harness.tuning.maxSessions from these four so the lane's budget stays
# within the floor and TASKS is created at it, the width
# TestCiDeploySizesMaxSessionsToTheTasksFloor holds. Copied, not derived, because the
# operator's are Go constants (a2aTasksMaxConsumersFloor,
# a2aSessionConsumersPerSession, and the reserve table
# a2aTasksReservedConsumersFor evaluates: 20 fixed plus 6 per worker with
# #2010's look-ahead row); TestCiDeploySizesMaxSessionsToTheTasksFloor there
# and tests/test_ci_deploy_mode_next.py here fail when either side moves.
readonly A2A_TASKS_FLOOR=64
readonly A2A_SESSION_CONSUMERS=3
readonly A2A_RESERVE_FIXED=20
readonly A2A_RESERVE_PER_WORKER=6
# The line the bridge logs once its durable consumer is bound
# (a2a/hermes-bridge/bridge.go, Run): a JSON record with these three fields.
# Until it appears the bus has an executor for nobody, and every case on the
# inject transport ends as infrastructure.
readonly BRIDGE_CONSUMING_LOG_MSG='"msg":"hermes bridge consuming"'
readonly BRIDGE_CONSUMING_LOG_PROFILE='"profile":"platform"'
# The executor field of that line, for BRIDGE_EXECUTOR_EXPECTED; a bridge that
# fell back to cli logs the same line with "executor":"cli" and fails the wait.
readonly BRIDGE_CONSUMING_LOG_EXECUTOR='"executor":"api"'
readonly MODE_NEXT_BRIDGE_LOG_ATTEMPTS=60
# The provisioning Job depends on NATS and on the callout. The operator now
# creates it only once a callout replica serves (#1702); before that its
# retries backed off exponentially and 19.5 minutes to complete was measured
# under adverse conditions (#1661). A few minutes on a healthy cluster, and the
# budget covers the wait for the callout too.
readonly MODE_NEXT_PROVISION_JOB_TIMEOUT_SECONDS=1500
# What the Job's status.conditions say when it is over, either way. A Job
# that exhausts its backoff carries Failed and never Complete, so the gate
# reads both rather than waiting the whole budget for a Complete that cannot
# come.
readonly JOB_CONDITION_COMPLETE="Complete"
readonly JOB_CONDITION_FAILED="Failed"
# What the CR's status says when the operator refused a provision render
# (updateStatusDegraded in platformagent_controller.go: the phase, and the
# Ready condition's reason a Failed provision Job is given). The provision
# wait prints the reason beside a Failed Job, and gate_cr_not_degraded reads
# both, so a refusal reds the lane rather than parking the CR Degraded over a
# working bus.
readonly CR_PHASE_DEGRADED="Degraded"
readonly CR_READY_REASON_PROVISION_FAILED="A2AProvisionFailed"
# How long a Failed Job is given to reach the CR's status before the failure
# is reported without it: the operator does not watch Jobs, it reads them on
# its requeue (30s while a provision Job runs), so the status lags the Job by
# up to one requeue. Polls of MODE_NEXT_POLL_SECONDS.
readonly MODE_NEXT_STATUS_ATTEMPTS=12
# The one Degraded gate_cr_not_degraded waits out rather than
# failing on (#2414): the operator gives the Ready condition this reason for
# any pod the scheduler marked Unschedulable, and that includes a pod waiting
# for the node an Autopilot scale-up is adding. The gate tells that case from
# the rest by the scheduler's own words in the message, which the operator
# copies in after "cannot be scheduled onto any available node:" -- a count
# of nodes short of CPU or memory, as in "1 node(s) didn't match
# PersistentVolume's node affinity, 2 Insufficient cpu, 2 Insufficient
# memory". One such count is enough, whatever else the message names: a node
# counted there is one the pod fits but for capacity, so another node like it
# places the pod. A message with no such count fails on the first read as
# before: node affinity or selector only, an untolerated taint only, or the
# RuntimeClass sentence the operator writes instead of the scheduler's when
# the CR requests one, none of which a scale-up fixes. So do the rarer
# shortfalls a new node would also fix (Too many pods, Insufficient
# ephemeral-storage): none of #2414's runs showed one, and widening the match
# is for when one turns up in a log.
readonly CR_READY_REASON_POD_UNSCHEDULABLE="PodUnschedulable"
readonly SCHEDULER_CAPACITY_SHORTFALL_RE='[0-9]+ Insufficient (cpu|memory)'
# How many more reads, MODE_NEXT_POLL_SECONDS apart, that Degraded gets before
# the gate fails on it: five minutes, for the scheduler to place the pod. The
# CR's status does not say when it has: the operator watches no Pods, so an
# assignment wakes nothing, and the status keeps the scheduler's message
# until the operator's next pass, which no Pod event starts: in practice the
# Deployment's status moving once the pod is Ready, or the steady-state
# requeue (fifteen minutes) once the provision Job is done. So while it
# forgives that Degraded about an agent pod the gate
# also reads the agent's pods, and the first one bound to a node ends the
# wait: the image pulls and the agent's start on the new node are the next
# gate's to time, on its rollout budget for the same Deployment, not this
# window's. The three runs in #2414 had the pod assigned within seconds; five
# minutes leaves room for a scale-up that takes a few minutes rather than one,
# and is half that rollout budget. Reads that return nothing count against
# the same window, the first read included, so it also bounds how long the
# gate tolerates a CR it cannot read.
readonly MODE_NEXT_UNSCHEDULABLE_ATTEMPTS=60
readonly A2A_PART_OF_SELECTOR="app.kubernetes.io/part-of=a2a-next"
readonly A2A_PROVISION_JOB_SELECTOR="kubeagents.x-k8s.io/a2a-component=provision"
readonly A2A_NATS_POD_SELECTOR="app=${PLATFORM_AGENT_CR_NAME}-a2a-nats"
# How much of each log the step keeps for the artifact: the tail of a pod or
# operator log on failure, the recent events, the gateway's last lines in the
# report, and how far back the agent's entrypoint log is scanned for its
# account of the mode and the skill overlay.
readonly MODE_NEXT_DIAG_LOG_LINES=100
readonly MODE_NEXT_DIAG_EVENT_LINES=40
readonly MODE_NEXT_REPORT_LOG_LINES=30
readonly MODE_NEXT_ENTRYPOINT_SCAN_LINES=400
readonly MODE_NEXT_ENTRYPOINT_MATCH_LINES=40
# The operator's override variables (a2aGatewayImage and a2aWorkerImage in
# platformagent_a2a_manifests.go, a2aCalloutImage in platformagent_a2a_callout.go,
# a2aVerifierImage in platformagent_a2a_verifier.go, a2aConsoleImage in
# platformagent_a2a_console.go) and the repository names
# step 4 pushes the builds under.
readonly A2A_GATEWAY_IMAGE_ENV_VAR="A2A_GATEWAY_IMAGE"
readonly A2A_CALLOUT_IMAGE_ENV_VAR="A2A_CALLOUT_IMAGE"
readonly A2A_WORKER_IMAGE_ENV_VAR="A2A_WORKER_IMAGE"
readonly A2A_VERIFIER_IMAGE_ENV_VAR="A2A_VERIFIER_IMAGE"
readonly A2A_CONSOLE_IMAGE_ENV_VAR="A2A_CONSOLE_IMAGE"
# Two of the rendered bridge's four operator settings (a2aBridgeImageEnvVar
# and a2aBridgeConcurrencyOperatorEnvVar in platformagent_a2a_bridge.go): its
# image and its BRIDGE_CONCURRENCY. The operator reads them from its own
# environment, as it does the overrides above; no CR field carries them. The
# third, A2A_BRIDGE_EXECUTOR, is left unset (BRIDGE_EXECUTOR_EXPECTED says why),
# and so is the fourth, A2A_BRIDGE_RESOURCES, so the lane runs the default sizing.
readonly A2A_BRIDGE_IMAGE_ENV_VAR="A2A_BRIDGE_IMAGE"
readonly A2A_BRIDGE_CONCURRENCY_ENV_VAR="A2A_BRIDGE_CONCURRENCY"
# Named only for the diagnosis when the bridge logs another executor.
readonly A2A_BRIDGE_EXECUTOR_OPERATOR_ENV_VAR="A2A_BRIDGE_EXECUTOR"
readonly A2A_GATEWAY_IMAGE_NAME="a2a-gateway"
readonly A2A_CALLOUT_IMAGE_NAME="a2a-authcallout"
readonly A2A_WORKER_IMAGE_NAME="a2a-worker"
readonly A2A_VERIFIER_IMAGE_NAME="a2a-verifier"
readonly A2A_CONSOLE_IMAGE_NAME="a2a-console"
# The bridge image goes to the operator too (A2A_BRIDGE_IMAGE_ENV_VAR), which
# renders the bridge sidecar from it.
readonly A2A_BRIDGE_IMAGE_NAME="hermes-bridge"

# ─── 1. Validation & Pre-checks ───────────────────────────────────────────────
# Still required with the agent path on vertex_ai below: the judge reads it
# (JUDGE_API_KEY in ci-eval-pr.sh) and the chart's credentials secret carries it.
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY environment variable is required"
  exit 1
fi

# Checked here rather than where the key is generated, because the failure it
# prevents is invisible for fifteen minutes: with no public half in
# platform-agent-secrets the chart renders no <name>-shell-authorized-keys, and
# the sandbox pod then sits in ContainerCreating on a `secret not found` mount
# error until step 6's rollout gate times out. Fail at second zero instead.
if ! command -v ssh-keygen >/dev/null 2>&1; then
  echo "ERROR: ssh-keygen is required to generate the shell sandbox keypair"
  exit 1
fi

# ─── 2. Configuration Environment Variables ───────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
source "${SCRIPT_DIR}/../tags.env"
trap dump_prow_artifacts_on_failure EXIT
ensure_helm

RAW_PULL_SHA="${PULL_PULL_SHA:-latest}"
PULL_SHA_SHORT="${RAW_PULL_SHA:0:7}"
export TAG="pr-${PULL_NUMBER:-local}-${PULL_SHA_SHORT:-latest}"
export AR_REPO="${AR_REPO:-us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents}"

export IMG="${AR_REPO}/kube-agents-operator:${TAG}"
export AGENT_IMAGE="${AR_REPO}/platform-agent"
export AGENT_TAG="${TAG}"
export IMAGE_TAG="${TAG}"

# The operator's A2A image overrides, as chart values. Empty unless
# EVAL_MODE_NEXT=1, in which case step 4 fills it once the image references
# exist; step 5 expands it into the Helm install, where an empty array
# contributes nothing and the release is byte-for-byte the today one.
A2A_OPERATOR_ENV_ARGS=()

# ─── 2a. Image Source: Pull Request Build, or Published Release Candidate ─────
# RC_COMMIT_SHA unset is the presubmit and everything this script did before the
# variable existed: build the pull request's images into the leased project's
# Artifact Registry and install those. Set, it is the release-candidate eval —
# the candidate's images are already published, so there is nothing to build and
# rebuilding would measure a different artefact than the one being released.
#
# hack/resolve-rc-target.sh produces the value and documents why the caller, not
# this script, checks the tree out at that commit.
#
# The two paths differ in registry AND in image name: Artifact Registry carries
# `kube-agents-operator`, the published one is `k8s-operator`. That is why the
# RC path drops the repository overrides rather than rewriting them — the chart
# already defaults every image to the published GHCR path, so only the tag has
# to be said. The same drop is what carries the credential-proxy sidecar across:
# it is not a chart value at all, the operator derives it from the agent image by
# rewriting the trailing path element and keeping the tag
# (resolveCredentialProxyImage in k8s-operator/internal/controller/
# platformagent_manifests.go), so it follows whichever repository the agent uses
# without being named here. Both plugin images default to enabled=false and are
# not rendered on either path.
if [ -n "${RC_COMMIT_SHA:-}" ]; then
  # The release pipeline publishes the A2A images and the bridge beside the
  # others, and the operator derives the references it renders, but this
  # path hands the operator none of the settings step 4 puts
  # in A2A_OPERATOR_ENV_ARGS: no inject door, so the eval's transport has no
  # Service to reach, and no bridge concurrency, so the bridge would not be
  # sized to the matrix's fan-out. Refuse the pair here rather than forty
  # minutes in.
  if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then
    echo "ERROR: EVAL_MODE_NEXT=1 is set together with RC_COMMIT_SHA. The mode-next flip needs" >&2
    echo "       the pull-request build path, which hands the operator the inject door and the" >&2
    echo "       Hermes bridge's settings; this path does not yet set them for the release." >&2
    exit 1
  fi

  # Sourced inside the branch, deliberately. The presubmit path must not acquire
  # a second file's exports and functions just because this one exists.
  # shellcheck source=scripts/release/common.sh
  source "${SCRIPT_DIR}/../scripts/release/common.sh"
  RC_REGISTRY_PREFIX="$(get_registry_prefix)"

  # The checkout contract, enforced rather than only documented. Only the images
  # come from RC_COMMIT_SHA; the chart, the CRDs, bench/tasks and bench/tf/fleet
  # all come from the tree this runs in, so a caller that sets the variable
  # without checking the tree out first gets a run that installs the candidate's
  # images against another revision's everything-else and reports an ordinary
  # green verdict for a combination that will never ship. Nothing else in the
  # run can notice that, which is why it fails here instead of warning.
  RC_TREE_SHA="$(git -C "${SCRIPT_DIR}/.." rev-parse HEAD 2>/dev/null || echo "")"
  if [ "${RC_TREE_SHA}" != "${RC_COMMIT_SHA}" ]; then
    echo "ERROR: RC_COMMIT_SHA is ${RC_COMMIT_SHA} but this tree is at ${RC_TREE_SHA:-an unknown commit}."
    echo "       Check the tree out at the candidate first, then re-run:"
    echo "         git checkout --detach ${RC_COMMIT_SHA}"
    echo "       hack/resolve-rc-target.sh's header explains why the checkout is"
    echo "       the caller's job and cannot be done from inside this script."
    exit 1
  fi

  # Cheap, and 15 minutes earlier than the alternative. Without it a missing
  # image surfaces as `helm --wait` timing out on ImagePullBackOff, which reads
  # as a broken chart rather than an unpublished commit. resolve-rc-target.sh
  # checks the same thing; this repeats it because RC_COMMIT_SHA can be set by
  # hand, and because that script is not what a Prow job is obliged to call.
  if ! check_commit_images_exist "${RC_COMMIT_SHA}"; then
    echo "ERROR: ${RC_REGISTRY_PREFIX} has no complete image set at ${RC_COMMIT_SHA}."
    echo "       Required: ${REQUIRED_RELEASE_IMAGES[*]}"
    echo "       docker-publish-ghcr.yml runs on every push to main; a queued or"
    echo "       failed run leaves the commit with no images to install."
    exit 1
  fi

  export TAG="${RC_COMMIT_SHA}"
  export IMG="${RC_REGISTRY_PREFIX}/k8s-operator:${TAG}"
  export AGENT_IMAGE="${RC_REGISTRY_PREFIX}/platform-agent"
  export AGENT_TAG="${TAG}"
  export IMAGE_TAG="${TAG}"

  IMAGE_ARGS=(
    --set-string "operator.image.tag=${TAG}"
    --set-string "platformAgent.deployment.image.tag=${TAG}"
    --set-string "agentSandbox.image.tag=${TAG}"
  )
  DEPLOY_SOURCE="release candidate ${RC_COMMIT_SHA:0:7} from ${RC_REGISTRY_PREFIX}"
else
  IMAGE_ARGS=(
    --set-string "operator.image.repository=${AR_REPO}/kube-agents-operator"
    --set-string "operator.image.tag=${TAG}"
    --set-string "platformAgent.deployment.image.repository=${AR_REPO}/platform-agent"
    --set-string "platformAgent.deployment.image.tag=${TAG}"
    --set-string "agentSandbox.image.repository=${AR_REPO}/agent-sandbox"
    --set-string "agentSandbox.image.tag=${TAG}"
  )
  DEPLOY_SOURCE="PR #${PULL_NUMBER:-local} build (${TAG})"
fi

# vertex_ai, not gemini. On 2026-09-02 every smoke run redded on 429s from the
# Gemini Developer API key's fixed paid-tier-3 quota -- 8,000,000 input
# tokens/minute for gemini-3.1-pro, named in the error body -- with five
# concurrent builds drawing ~94k-token turns from one shared minute window
# (#1097; the full diagnosis with build artifacts is on #1184). Vertex AI
# serves the same Gemini models under dynamic shared quota, with no fixed
# per-minute wall, and authenticates the LiteLLM pod through Workload Identity
# (the kubeagents-litellm KSA annotation below) instead of the API key.
# Overridable so a rollout problem is a job-env flip rather than a revert.
# Anyone flipping MODEL_DEFAULT_NAME: ci-eval-pr.sh stamps records with its
# own AGENT_MODEL_OVERRIDE, so flip both or the eval version key names a
# model the install is not serving.
export MODEL_PROVIDER="${MODEL_PROVIDER:-vertex_ai}"
export MODEL_DEFAULT_NAME="${MODEL_DEFAULT_NAME:-gemini-3.1-pro-preview}"
# Default to enforcing CMEK database encryption on CI evaluation clusters.
# Set ALLOW_UNENCRYPTED_SECRETS=true to bypass CMEK checks on unencrypted test clusters.
export ALLOW_UNENCRYPTED_SECRETS="${ALLOW_UNENCRYPTED_SECRETS:-false}"

export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
# The gateway's own identity, deliberately not GSA_NAME: LiteLLM is a
# network-exposed proxy forwarding attacker-influenceable prompt content, so
# it holds roles/aiplatform.user and nothing else (the site's
# security-and-iam.md, "The Vertex AI gateway is a separate identity"). The
# pair must exist in the leased PROJECT_ID before a vertex_ai deploy;
# provision_ci_pool_project.sh creates it, verify_ci_pool_project.py checks
# it, and docs/ci-pool-projects.md carries the hand repair.
export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
export MEMORY_ENABLED="false"
export USER_PROFILE_ENABLED="false"
export GOOGLE_CHAT_ENABLED="false"
export SLACK_ENABLED="false"

# ─── 2b. GitOps Repository for This Run ───────────────────────────────────────
# Every GitHub-writing eval scenario begins by reading the registered repositories
# out of the gitops-state ConfigMap — the fleet-audit streams do it in `audit_report.py
# start`, before anything else happens. The operator seeds that entry from
# spec.integration.github.gitRepo on the PlatformAgent CR; with the field unset
# no repository is registered and those scenarios stop at step 0 with nothing to clone.
#
# CI supplies the value and deliberately does NOT lean on the chart default.
# On the presubmit path everything this job deploys — chart, operator, agent —
# is built from the pull request, so a PR that blanks
# `platformAgent.integration.github.gitRepo` in values.yaml, or breaks the
# CR-to-ConfigMap seeding, is precisely the regression the eval should catch
# as a failed scenario. It can only catch it if the value the run is supposed to
# use arrives from outside the artefacts under test. The release-candidate path
# installs published images instead of built ones, which narrows what is under
# test without changing this argument: the chart still comes from the checkout,
# and supplying the value from outside is what keeps either artefact from
# choosing it. Note this is a *correctness* argument, not the containment
# boundary: what a run can actually write to is fixed by which repositories the
# GitHub App is installed on, which no PR can change. See
# docs/ci-pool-projects.md.
#
# One GitOps repo per leasable project, so two concurrent leases can never
# share a ledger issue or race on a remediation branch. Onboarding a further
# project (issue #637, Boskos leasing) is one line here, its row in
# gitlab_project_for_project() below, and the same pair in _EXPECTED_MAPPING in
# tests/test_ci_gitops_repo.py — no other edit in this file.
#
# A mapping here is a claim that the repo exists and that App 4675512 is
# installed on it. It is not self-verifying: with the line present and either
# of those missing, the deploy succeeds and every GitHub-writing scenario
# fails at `audit_report.py start` with a clone or token error instead of the
# named, actionable refusal below. Add the row when the repo and the
# installation are real, not when the project joins the Boskos pool — the two
# are separate events, and kube-agents-evals-3 is what happens when they are
# assumed to be one.
gitops_repo_for_project() {
  case "$1" in
    kube-agents-evals) echo "gke-agentic/kube-agents-evals-infra" ;;
    kube-agents-evals-2) echo "gke-agentic/kube-agents-evals-2-infra" ;;
    kube-agents-evals-3) echo "gke-agentic/kube-agents-evals-3-infra" ;;
    kube-agents-evals-4) echo "gke-agentic/kube-agents-evals-4-infra" ;;
    kube-agents-evals-5) echo "gke-agentic/kube-agents-evals-5-infra" ;;
    kube-agents-evals-6) echo "gke-agentic/kube-agents-evals-6-infra" ;;
    kube-agents-evals-7) echo "gke-agentic/kube-agents-evals-7-infra" ;;
    kube-agents-evals-8) echo "gke-agentic/kube-agents-evals-8-infra" ;;
    kube-agents-evals-9) echo "gke-agentic/kube-agents-evals-9-infra" ;;
    kube-agents-evals-10) echo "gke-agentic/kube-agents-evals-10-infra" ;;
    kube-agents-evals-11) echo "gke-agentic/kube-agents-evals-11-infra" ;;
    kube-agents-evals-12) echo "gke-agentic/kube-agents-evals-12-infra" ;;
    kube-agents-evals-13) echo "gke-agentic/kube-agents-evals-13-infra" ;;
    kube-agents-evals-14) echo "gke-agentic/kube-agents-evals-14-infra" ;;
    kube-agents-evals-15) echo "gke-agentic/kube-agents-evals-15-infra" ;;
    kube-agents-evals-16) echo "gke-agentic/kube-agents-evals-16-infra" ;;
    kube-agents-evals-17) echo "gke-agentic/kube-agents-evals-17-infra" ;;
    kube-agents-evals-18) echo "gke-agentic/kube-agents-evals-18-infra" ;;
    kube-agents-evals-19) echo "gke-agentic/kube-agents-evals-19-infra" ;;
    kube-agents-evals-20) echo "gke-agentic/kube-agents-evals-20-infra" ;;
    kube-agents-evals-21) echo "gke-agentic/kube-agents-evals-21-infra" ;;
    kube-agents-evals-22) echo "gke-agentic/kube-agents-evals-22-infra" ;;
    kube-agents-evals-23) echo "gke-agentic/kube-agents-evals-23-infra" ;;
    kube-agents-evals-24) echo "gke-agentic/kube-agents-evals-24-infra" ;;
    kube-agents-evals-25) echo "gke-agentic/kube-agents-evals-25-infra" ;;
    kube-agents-evals-26) echo "gke-agentic/kube-agents-evals-26-infra" ;;
    kube-agents-evals-27) echo "gke-agentic/kube-agents-evals-27-infra" ;;
    kube-agents-evals-28) echo "gke-agentic/kube-agents-evals-28-infra" ;;
    kube-agents-evals-29) echo "gke-agentic/kube-agents-evals-29-infra" ;;
    kube-agents-evals-30) echo "gke-agentic/kube-agents-evals-30-infra" ;;
    kube-agents-evals-31) echo "gke-agentic/kube-agents-evals-31-infra" ;;
    kube-agents-evals-32) echo "gke-agentic/kube-agents-evals-32-infra" ;;
    kube-agents-evals-33) echo "gke-agentic/kube-agents-evals-33-infra" ;;
    kube-agents-evals-34) echo "gke-agentic/kube-agents-evals-34-infra" ;;
    kube-agents-evals-35) echo "gke-agentic/kube-agents-evals-35-infra" ;;
    *) return 1 ;;
  esac
}

# The same table for the GitLab forge (EVAL_FORGE=gitlab, issue #2394): one
# private gitlab.com project per pool project, same name under the group
# gke-agentic. A row here claims the project exists and the bot account
# kube-agents-eval-bot is a Developer on it (docs/ci-pool-projects.md 5.6),
# and _EXPECTED_GITLAB_MAPPING in tests/test_ci_gitops_repo.py pins the pair.
gitlab_project_for_project() {
  case "$1" in
    kube-agents-evals) echo "gke-agentic/kube-agents-evals-infra" ;;
    kube-agents-evals-2) echo "gke-agentic/kube-agents-evals-2-infra" ;;
    kube-agents-evals-3) echo "gke-agentic/kube-agents-evals-3-infra" ;;
    kube-agents-evals-4) echo "gke-agentic/kube-agents-evals-4-infra" ;;
    kube-agents-evals-5) echo "gke-agentic/kube-agents-evals-5-infra" ;;
    kube-agents-evals-6) echo "gke-agentic/kube-agents-evals-6-infra" ;;
    kube-agents-evals-7) echo "gke-agentic/kube-agents-evals-7-infra" ;;
    kube-agents-evals-8) echo "gke-agentic/kube-agents-evals-8-infra" ;;
    kube-agents-evals-9) echo "gke-agentic/kube-agents-evals-9-infra" ;;
    kube-agents-evals-10) echo "gke-agentic/kube-agents-evals-10-infra" ;;
    kube-agents-evals-11) echo "gke-agentic/kube-agents-evals-11-infra" ;;
    kube-agents-evals-12) echo "gke-agentic/kube-agents-evals-12-infra" ;;
    kube-agents-evals-13) echo "gke-agentic/kube-agents-evals-13-infra" ;;
    kube-agents-evals-14) echo "gke-agentic/kube-agents-evals-14-infra" ;;
    kube-agents-evals-15) echo "gke-agentic/kube-agents-evals-15-infra" ;;
    kube-agents-evals-16) echo "gke-agentic/kube-agents-evals-16-infra" ;;
    kube-agents-evals-17) echo "gke-agentic/kube-agents-evals-17-infra" ;;
    kube-agents-evals-18) echo "gke-agentic/kube-agents-evals-18-infra" ;;
    kube-agents-evals-19) echo "gke-agentic/kube-agents-evals-19-infra" ;;
    kube-agents-evals-20) echo "gke-agentic/kube-agents-evals-20-infra" ;;
    kube-agents-evals-21) echo "gke-agentic/kube-agents-evals-21-infra" ;;
    kube-agents-evals-22) echo "gke-agentic/kube-agents-evals-22-infra" ;;
    kube-agents-evals-23) echo "gke-agentic/kube-agents-evals-23-infra" ;;
    kube-agents-evals-24) echo "gke-agentic/kube-agents-evals-24-infra" ;;
    kube-agents-evals-25) echo "gke-agentic/kube-agents-evals-25-infra" ;;
    kube-agents-evals-26) echo "gke-agentic/kube-agents-evals-26-infra" ;;
    kube-agents-evals-27) echo "gke-agentic/kube-agents-evals-27-infra" ;;
    kube-agents-evals-28) echo "gke-agentic/kube-agents-evals-28-infra" ;;
    kube-agents-evals-29) echo "gke-agentic/kube-agents-evals-29-infra" ;;
    kube-agents-evals-30) echo "gke-agentic/kube-agents-evals-30-infra" ;;
    kube-agents-evals-31) echo "gke-agentic/kube-agents-evals-31-infra" ;;
    kube-agents-evals-32) echo "gke-agentic/kube-agents-evals-32-infra" ;;
    kube-agents-evals-33) echo "gke-agentic/kube-agents-evals-33-infra" ;;
    kube-agents-evals-34) echo "gke-agentic/kube-agents-evals-34-infra" ;;
    kube-agents-evals-35) echo "gke-agentic/kube-agents-evals-35-infra" ;;
    *) return 1 ;;
  esac
}

# PULL_NUMBER and JOB_NAME are set by Prow and by nothing else, which is what
# separates a leased CI run from a laptop. The two get different treatment
# below, but neither gets a silent default: an unmapped project stops the
# deploy rather than installing an agent that writes somewhere unintended or
# nowhere at all.
if [ -n "${PULL_NUMBER:-}" ] || [ -n "${JOB_NAME:-}" ]; then
  IS_PROW_RUN="true"
else
  IS_PROW_RUN="false"
fi

# The mode flip exists for the next lane's runs: a pull request's, or one of
# the jobs EVAL_MODE_NEXT_JOB_NAMES lists (its periodic and nightly on main).
# A flagged run appends nothing to main's baseline and publishes no dashboard
# (hack/ci-eval-pr.sh keeps it out of both on the flag alone; bench-gate
# separately refuses a pull request's sample, bench/baselines/README.md), so
# what the flag mis-set on a job that is not the lane's -- the today nightly,
# a postsubmit -- would do is run that job in next mode and leave main's
# window and dashboard silently missing it, its verdict measuring the wrong
# stack. Keyed on the job's name rather than on PULL_NUMBER, so the lane's
# scheduled jobs are admitted by being named and every other Prow run without
# a pull request is still refused.
if [ "${EVAL_MODE_NEXT:-}" = "1" ] && [ "${IS_PROW_RUN}" = "true" ] && [ -z "${PULL_NUMBER:-}" ]; then
  # One whole-string comparison per listed name, not a pattern over the
  # joined list: a substring match on the space-padded list would also admit
  # a JOB_NAME that spells two adjacent entries with a space between them.
  MODE_NEXT_JOB_ADMITTED="false"
  for mode_next_job in ${EVAL_MODE_NEXT_JOB_NAMES}; do
    if [ "${mode_next_job}" = "${JOB_NAME:-}" ]; then
      MODE_NEXT_JOB_ADMITTED="true"
    fi
  done
  if [ "${MODE_NEXT_JOB_ADMITTED}" = "true" ]; then
    echo "EVAL_MODE_NEXT=1: accepted on ${JOB_NAME} (a next-lane job with no PULL_NUMBER; the baseline store is read, never written)"
  else
    echo "ERROR: EVAL_MODE_NEXT=1 is set on a Prow run with no PULL_NUMBER (JOB_NAME=${JOB_NAME:-})." >&2
    echo "       The flag is for a pull request's presubmit or a next-lane job named in" >&2
    echo "       EVAL_MODE_NEXT_JOB_NAMES (${EVAL_MODE_NEXT_JOB_NAMES}); any other periodic or" >&2
    echo "       postsubmit under it would run in next mode and record nothing to main's" >&2
    echo "       baseline or dashboard, leaving that run silently missing from both." >&2
    exit 1
  fi
fi

# The bridge sidecar's concurrency is the matrix's fan-out, read from the same
# job environment hack/ci-eval-pr.sh reads it from (the constants block says
# how it is sized). Checked here, at second zero, for the same reason the two
# refusals above are: every input is known now, and step 6b, where the
# operator renders it into the bridge, is forty minutes and a leased project
# later (step 4 hands it to the operator as A2A_BRIDGE_CONCURRENCY).
# Digits only, and at most four of them, before the numeric compare: bash's
# `test` skips surrounding whitespace and the bridge's strconv.Atoi does not,
# so " 4" would pass here and start the bridge at its default of 2 with a
# warning nobody reads; and `test` cannot parse a digit string past int64 at
# all, which would let it through the same way. Five digits or more can never
# be within the queue's capacity, whatever they are.
if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then
  MODE_NEXT_BRIDGE_CONCURRENCY="${EVAL_TASK_PARALLELISM:-${EVAL_TASK_PARALLELISM_DEFAULT}}"
  case "${MODE_NEXT_BRIDGE_CONCURRENCY}" in
  '' | *[!0-9]* | ?????*) MODE_NEXT_BRIDGE_CONCURRENCY_OK="false" ;;
  *) MODE_NEXT_BRIDGE_CONCURRENCY_OK="true" ;;
  esac
  if [ "${MODE_NEXT_BRIDGE_CONCURRENCY_OK}" != "true" ] || [ "${MODE_NEXT_BRIDGE_CONCURRENCY}" -lt 1 ] || [ "${MODE_NEXT_BRIDGE_CONCURRENCY}" -gt "${BRIDGE_QUEUE_CAPACITY}" ]; then
    echo "ERROR: EVAL_TASK_PARALLELISM='${MODE_NEXT_BRIDGE_CONCURRENCY}' is not a concurrency the bridge can be given (an integer 1..${BRIDGE_QUEUE_CAPACITY})." >&2
    exit 1
  fi
  # The maxSessions the provision is given, so that its budget, which counts
  # the rendered bridge's workers from the first render, fits the floor: the
  # largest value with maxSessions * A2A_SESSION_CONSUMERS + A2A_RESERVE_FIXED
  # + A2A_RESERVE_PER_WORKER * workers <= A2A_TASKS_FLOOR, and at least 1
  # (the API's minimum; the eval spawns no session pods, so the number is
  # capacity nobody draws on). At the presubmit's 4 workers that is 6, at 6
  # it is 2. At 8 or more the floor cannot hold even the reserve: the clamp
  # gives 1 and the one provision creates TASKS at the budget, wider than the
  # floor. Nothing re-renders the budget afterwards, so nothing is refused.
  MODE_NEXT_MAX_SESSIONS=$(((A2A_TASKS_FLOOR - A2A_RESERVE_FIXED - A2A_RESERVE_PER_WORKER * MODE_NEXT_BRIDGE_CONCURRENCY) / A2A_SESSION_CONSUMERS))
  if [ "${MODE_NEXT_MAX_SESSIONS}" -lt 1 ]; then
    MODE_NEXT_MAX_SESSIONS=1
  fi
fi

# --- Which forge this run deploys against (EVAL_FORGE) ----------------------
# EVAL_FORGE picks the forge the eval run drives: github (default; the
# resolution below) or gitlab (issue #2394). Under gitlab the GitHub
# integration and its minter stay off, the PlatformAgent declares one GitLab
# forge whose credential is a Kubernetes Secret (step 5 fills it from the
# pool's Secret Manager secret gitlab-agent-token in GITLAB_SECRETS_PROJECT),
# and the pool project's GitLab project is its gitops repository. Mapped and gated here, ahead of the
# GitHub resolution, so an unmapped project is refused naming this table.
EVAL_FORGE="${EVAL_FORGE:-github}"
# The Secret the forge's credentialsRef names, and the key the token sits
# under. The key is this deploy's choice: the operator reads only the Secret's
# name today, and the GitLab provider, when it lands, fixes the key it reads.
# This is the one place to change it.
GITLAB_FORGE_SECRET_NAME="gitlab-forge-token"
GITLAB_FORGE_SECRET_KEY="token"
GITLAB_AGENT_SM_SECRET="gitlab-agent-token"
# One token pair serves the whole pool, kept where the runner identities
# live rather than copied into every leased project: GitLab has no minting,
# so the pair is rotated by a human with overlap, and one home keeps that
# the same size however many projects the pool has (docs/ci-pool-projects.md 5.6).
GITLAB_SECRETS_PROJECT="kube-agents-prow"
GITLAB_FORGE_HOST="gitlab.com"
case "${EVAL_FORGE}" in
  github) ;;
  gitlab)
    if ! GITLAB_PROJECT="$(gitlab_project_for_project "${PROJECT_ID}")"; then
      echo "ERROR: EVAL_FORGE=gitlab but no GitLab project is mapped for PROJECT_ID=${PROJECT_ID}." >&2
      echo "       Add it to gitlab_project_for_project() in hack/ci-deploy.sh once the project" >&2
      echo "       exists and the bot is a Developer on it (docs/ci-pool-projects.md 5.6)." >&2
      exit 1
    fi
    # The gate: the chart refuses a provider it does not register, but only at
    # helm time, after the image build. Two hand-mirrored lists say which
    # providers it registers, the CRD's enum and $registered in _helpers.tpl;
    # read both now and fail in seconds unless both name gitlab.
    PLATFORM_AGENT_CRD="${SCRIPT_DIR}/../charts/kube-agents/crds/kubeagents.x-k8s.io_platformagents.yaml"
    CHART_HELPERS="${SCRIPT_DIR}/../charts/kube-agents/templates/_helpers.tpl"
    if ! grep -Eq '^[[:space:]]+- gitlab$' "${PLATFORM_AGENT_CRD}" \
      || ! grep -Eq 'registered := list .*"gitlab"' "${CHART_HELPERS}"; then
      echo "ERROR: EVAL_FORGE=gitlab, but the chart in this checkout does not register provider" >&2
      echo "       gitlab (${PLATFORM_AGENT_CRD#"${SCRIPT_DIR}/../"} and ${CHART_HELPERS#"${SCRIPT_DIR}/../"}" >&2
      echo "       both have to list it). The GitLab provider is the operator half of" >&2
      echo "       gke-labs/kube-agents#1154; this deploy waits for it (#2394)." >&2
      exit 1
    fi
    ;;
  *)
    echo "ERROR: EVAL_FORGE='${EVAL_FORGE}' is not a forge this deploy knows; use github (default) or gitlab." >&2
    exit 1
    ;;
esac

# The override exists for developers, and only for them. Under Boskos the
# project is leased per run, so a value pinned in the job environment would
# eventually point one project's run at another project's GitOps repo — the
# one failure mode worth refusing outright.
if [ "${IS_PROW_RUN}" = "true" ] && [ -n "${EVAL_GITOPS_REPO:-}" ]; then
  echo "ERROR: EVAL_GITOPS_REPO is set in a Prow run (PROJECT_ID=${PROJECT_ID})." >&2
  echo "       The GitOps repo must follow the leased project, so CI resolves it from" >&2
  echo "       gitops_repo_for_project() in hack/ci-deploy.sh. Unset EVAL_GITOPS_REPO," >&2
  echo "       and map the project there if it is missing." >&2
  exit 1
fi

if [ -n "${EVAL_GITOPS_REPO:-}" ]; then
  GITOPS_REPO="${EVAL_GITOPS_REPO}"
  echo "GitOps repo: ${GITOPS_REPO} (from EVAL_GITOPS_REPO)"
elif GITOPS_REPO="$(gitops_repo_for_project "${PROJECT_ID}")"; then
  echo "GitOps repo: ${GITOPS_REPO} (mapped from PROJECT_ID=${PROJECT_ID})"
elif [ "${IS_PROW_RUN}" = "true" ]; then
  echo "ERROR: no GitOps repo is mapped for PROJECT_ID=${PROJECT_ID}." >&2
  echo "       Every project in the kube-agents-evals-project Boskos pool needs its own" >&2
  echo "       private GitOps repo; deploying without one would leave the fleet-audit and" >&2
  echo "       rca-remediation-pr scenarios failing at step 0 for a reason no log explains." >&2
  echo "       Add the project to gitops_repo_for_project() in hack/ci-deploy.sh and follow" >&2
  echo "       docs/ci-pool-projects.md before registering it" >&2
  echo "       in the pool." >&2
  exit 1
else
  echo "ERROR: no GitOps repo is mapped for PROJECT_ID=${PROJECT_ID}, and this is not a" >&2
  echo "       Prow run. A local deploy has no lease, so it has to say where it writes:" >&2
  echo "         EVAL_GITOPS_REPO=owner/repo  — your own throwaway GitOps repo" >&2
  echo "         EVAL_GITOPS_REPO=none        — deploy with the GitHub integration off" >&2
  echo "                                        (managed_repos stays empty, and" >&2
  echo "                                        every GitHub-writing scenario will fail)" >&2
  exit 1
fi

# "none" is the explicit opt-out, and the only route to an empty gitRepo. An
# empty string here makes the chart omit spec.integration.github entirely.
if [ "${GITOPS_REPO}" = "none" ]; then
  echo "GitHub integration: disabled for this deploy (EVAL_GITOPS_REPO=none)"
  GITOPS_REPO=""
elif ! printf '%s' "${GITOPS_REPO}" | grep -Eq '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'; then
  echo "ERROR: GitOps repo '${GITOPS_REPO}' is not in owner/repo form." >&2
  echo "       The minty rule ConfigMap is keyed on the org and repo separately, so the" >&2
  echo "       shorthand is what CI passes — not a URL." >&2
  exit 1
fi

# The in-cluster half of the token path. gitRepo alone only tells the agent
# where to clone; github-token-minter is what turns the platform GSA's OIDC
# identity into a repo-scoped GitHub App token (agents/platform/scripts/
# github_token_refresh.py has no other source, and strips any inherited
# GITHUB_TOKEN).
#
# Off unless EVAL_GITHUB_APP_ID is set, because the minter cannot come up until
# a human has done the two things terraform cannot: install the GitHub App on
# this project's GitOps repo, and import the App's private key into the
# project's KMS signing key. Until then the pod fails its readiness probe, and
# since the minter Deployment is part of this release, `helm --wait` below
# would fail every PR. Setting EVAL_GITHUB_APP_ID is therefore the switch that
# says "the manual half is done for this project" — and if it is not, the
# deploy failing loudly is the right outcome.
#
# The pool's App is kube-agents-evals-token-minter, id 4675512, installed on
# the three *-infra repos above and nothing else. One App for the whole pool, so
# the value is the same in every project's job environment; what is per-project
# is the KMS key its PEM was imported into. That installation list -- not this
# script, and not the minty rule the chart renders -- is what bounds where a
# run can write, because a presubmit deploys the pull request's own chart and
# could otherwise rewrite either of them.
#
# githubMinter.allowedServiceAccount is left at its default, which derives
# kubeagents-platform-gsa@<harness.projectId> — exactly the GSA_NAME/PROJECT_ID
# pair this deploy annotates the agent KSA with, so the rule is keyed on this
# project's platform GSA and no other's.
if [ "${EVAL_FORGE}" = "gitlab" ]; then
  GITHUB_MINTER_ARGS=(--set "githubMinter.enabled=false")
  echo "GitHub token minter: off (EVAL_FORGE=gitlab)"
elif [ -n "${GITOPS_REPO}" ] && [ -n "${EVAL_GITHUB_APP_ID:-}" ]; then
  GITHUB_MINTER_ARGS=(
    --set "githubMinter.enabled=true"
    --set-string "githubMinter.org=${GITOPS_REPO%%/*}"
    --set-string "githubMinter.repo=${GITOPS_REPO##*/}"
    --set-string "githubMinter.appId=${EVAL_GITHUB_APP_ID}"
  )
  echo "GitHub token minter: enabled for ${GITOPS_REPO} (app ${EVAL_GITHUB_APP_ID})"
else
  GITHUB_MINTER_ARGS=(--set "githubMinter.enabled=false")
  echo "GitHub token minter: disabled (EVAL_GITHUB_APP_ID unset) — the agent can read" \
    "managed_repos but cannot mint a token, so GitHub-writing scenarios will fail."
fi

# The values the chart receives for the forge: forges[] + repositories[] for
# gitlab, the deprecated github.gitRepo alias otherwise (the chart refuses both
# at once, so the alias is set empty beside the lists).
case "${EVAL_FORGE}" in
  gitlab)
    GITOPS_REPO=""
    GITHUB_MINTER_ARGS=(--set "githubMinter.enabled=false")
    FORGE_ARGS=(
      --set-string "platformAgent.integration.github.gitRepo="
      --set-string "platformAgent.integration.forges[0].name=gitlab"
      --set-string "platformAgent.integration.forges[0].provider=gitlab"
      --set-string "platformAgent.integration.forges[0].host=${GITLAB_FORGE_HOST}"
      --set-string "platformAgent.integration.forges[0].namespace=${GITLAB_PROJECT%%/*}"
      --set-string "platformAgent.integration.forges[0].credentialsRef.name=${GITLAB_FORGE_SECRET_NAME}"
      --set-string "platformAgent.integration.repositories[0].forge=gitlab"
      --set-string "platformAgent.integration.repositories[0].repository=https://${GITLAB_FORGE_HOST}/${GITLAB_PROJECT}"
      --set-string "platformAgent.integration.repositories[0].role=gitops"
    )
    echo "Forge: gitlab — ${GITLAB_FORGE_HOST}/${GITLAB_PROJECT} (mapped from PROJECT_ID=${PROJECT_ID}); GitHub integration and minter off"
    ;;
  *)
    FORGE_ARGS=(--set-string "platformAgent.integration.github.gitRepo=${GITOPS_REPO}")
    ;;
esac

# ─── 2d. The seeded fleet's read-only credential ──────────────────────────────
# The gate hack/ci-eval-pr.sh applies before it writes the fleet kubeconfigs,
# run here first: everything from here on is the 20-30 minutes of build and
# deploy ahead of it, and a project whose reader cannot be impersonated should
# fail in seconds instead. Failing here usually also keeps the run inside the
# dashboard's setup-death bound -- a zero-task FAILURE under five minutes of
# whole job, scripts/eval_dashboard/classify.py -- so the health bot counts it
# as infrastructure and points at the leased project; a run that waited longer
# than that for its Boskos lease reads as a deploy break instead. A laptop
# fails the same gate for a different reason -- roles/owner cannot impersonate
# the reader -- and the gate's own message tells the two apart, naming the
# pool repair to a job and FLEET_ALLOW_RUNNER_CREDENTIAL=1 (which leaves the
# reader unset and passes on the developer's own credential) to a developer;
# this caller adds no repair of its own. Like EVAL_GITOPS_REPO above, that
# opt-in is for developers only: set in a Prow job's environment it would
# quietly restore the write-credential fallback this gate replaces, so a
# leased run refuses it.
# shellcheck source=hack/fleet-kubeconfigs.sh
source "${SCRIPT_DIR}/fleet-kubeconfigs.sh"
preflight_fleet_reader() {
  _fleet_refuse_opt_in_under_prow || exit 1
  FLEET_READONLY_SA="$(_fleet_reader_for_run "${PROJECT_ID}")"
  _fleet_require_readonly_credential "${FLEET_READONLY_SA}" "${PROJECT_ID}" || {
    echo "FATAL: stopping before the build: the seeded fleet cannot be read as its reader, and it is not graded with the runner's write credential." >&2
    exit 1
  }
}
preflight_fleet_reader

# The GitLab forge's credential, read once now for the same reason: the
# token is hand-provisioned (docs/ci-pool-projects.md 5.6), and a runner
# without the accessor grant, or a secret that is gone, should fail here,
# not after the image build. The value is discarded; step 5 reads it again
# into the Kubernetes Secret.
preflight_gitlab_forge_secret() {
  if ! gcloud secrets versions access latest --secret="${GITLAB_AGENT_SM_SECRET}" --project="${GITLAB_SECRETS_PROJECT}" >/dev/null; then
    echo "FATAL: stopping before the build: Secret Manager ${GITLAB_SECRETS_PROJECT}/${GITLAB_AGENT_SM_SECRET} cannot be read as this runner (docs/ci-pool-projects.md 5.6: the secret and the runner's secretAccessor grant are hand steps)." >&2
    exit 1
  fi
  echo "GitLab forge credential: ${GITLAB_SECRETS_PROJECT}/${GITLAB_AGENT_SM_SECRET} readable"
}
if [ "${EVAL_FORGE}" = "gitlab" ]; then
  preflight_gitlab_forge_secret
fi

# ─── 2c. Image Build Worker ───────────────────────────────────────────────────
# Where the image builds run. Either a private worker pool or a sized machine
# on the default pool -- never both, because a pool declares its own machine
# and rejects being told a different one.
#
# Opt into a pool by exporting CLOUD_BUILD_WORKER_POOL as a full resource name:
# projects/PROJECT/locations/REGION/workerPools/POOL. Unset by default, which
# is the CI path. The region is read back out of that name because
# `gcloud builds submit` otherwise falls back to the `global` region, which
# cannot reach a regional pool.
if [ -n "${CLOUD_BUILD_WORKER_POOL:-}" ]; then
  case "$CLOUD_BUILD_WORKER_POOL" in
    projects/*/locations/*/workerPools/*) ;;
    *)
      echo "ERROR: CLOUD_BUILD_WORKER_POOL must be a full resource name: projects/PROJECT/locations/REGION/workerPools/POOL"
      exit 1
      ;;
  esac
  BUILD_WORKER_ARGS=(
    --worker-pool="$CLOUD_BUILD_WORKER_POOL"
    --region="$(echo "$CLOUD_BUILD_WORKER_POOL" | cut -d'/' -f4)"
  )
else
  # The default pool's unspecified machine is two vCPUs, which is most of why
  # the image builds are the single largest phase of this job. The build also
  # runs the operator step alongside the agent build (see
  # deploy/docker/cloudbuild-ci.yaml), and that is only real overlap on a
  # worker with cores to spare rather than two contending for the same pair.
  BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)
fi

# Heal a poisoned release record with no deployed revision (#1172, #2382).
# Called before initial deployment (step 5a) and before retrying on transient
# 5xx errors (step 5c).
heal_poisoned_release_record() {
  local reason="${1:-a previous run left this pool project poisoned (#1172)}"
  local action="${2:-installing}"
  local history_json="" history_err="" history_rc=0

  for ((probe_try=1; probe_try<=HELM_HISTORY_PROBE_ATTEMPTS; probe_try++)); do
    local probe_tmp
    probe_tmp="$(mktemp)"
    if history_json="$(helm history "${HELM_RELEASE_NAME}" -n "${NAMESPACE}" -o json 2>"${probe_tmp}")"; then
      history_rc=0
      rm -f "${probe_tmp}"
      break
    else
      history_rc=$?
      history_err="$(cat "${probe_tmp}")"
      rm -f "${probe_tmp}"
      # If the release simply does not exist ("release: not found"),
      # there is no release to heal; break immediately without retrying.
      if grep -Eq "${HELM_RELEASE_NOT_FOUND_RE}" <<<"${history_err}"; then
        break
      fi
      # If the probe failed with any other error (API-server 5xx, cluster unreachable,
      # connection reset, TLS timeout, auth failure), wait and re-probe
      # rather than prematurely treating it as "absent release" (#2382).
      if [ "${probe_try}" -lt "${HELM_HISTORY_PROBE_ATTEMPTS}" ]; then
        echo "WARNING: helm history probe attempt ${probe_try} failed (${history_err}), re-probing in ${HELM_HISTORY_PROBE_RETRY_DELAY_SECONDS}s..."
        sleep "${HELM_HISTORY_PROBE_RETRY_DELAY_SECONDS}"
      fi
    fi
  done

  if [ "${history_rc}" -ne 0 ]; then
    # If the release does not exist ("release: not found"),
    # there is no release record to heal.
    if grep -Eq "${HELM_RELEASE_NOT_FOUND_RE}" <<<"${history_err}"; then
      return 0
    fi
    # If probe failed with an error other than "release: not found" and re-probes were exhausted:
    # On in-loop retry (§5c), fail loudly under set -e so the run does not
    # silently skip healing and fail attempt 2 on "has no deployed releases" (#2382).
    # At lease time (§5a), degrade to the pre-fix behaviour: warn and skip
    # the heal so a transient control-plane blip seconds after creation does not
    # abort the run before chart deployment and its retry loop can run.
    if [ "${action}" = "retrying" ]; then
      echo "ERROR: helm history probe failed: ${history_err}" >&2
      return "${history_rc}"
    fi
    echo "WARNING: helm history probe failed after ${HELM_HISTORY_PROBE_ATTEMPTS} attempts (${history_err}); skipping lease-time release record heal and proceeding to deploy."
    return 0
  fi

  if ! grep -Eq "${HELM_DEPLOYED_STATUS_RE}" <<<"${history_json}"; then
    echo "WARNING: the ${HELM_RELEASE_NAME} release record exists with no deployed revision —"
    echo "         ${reason}. Clearing the"
    echo "         record before ${action}."
    # When retrying (§5c), the failed attempt may have already started the
    # operator and added the PlatformAgent finalizer. Delete the CR first and
    # wait for the operator to clear its finalizer before uninstalling (#2382).
    # Without this, Helm deletes the operator and RBAC first, leaving the CR
    # stranded on its finalizer and breaking subsequent install attempts.
    # Timeout matches charts/kube-agents/values.yaml cleanupHook.timeout (120s).
    # If the deletion times out or fails, stop under set -e so we do not issue
    # a --no-hooks uninstall that strands the CR in Terminating.
    if [ "${action}" = "retrying" ]; then
      kubectl delete platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" \
        --ignore-not-found --wait --timeout="${PLATFORM_AGENT_CR_DELETE_TIMEOUT}"
    fi

    # --no-hooks: at lease time (§5a), a leftover release from a failed prior
    # run never started the operator, so running pre-delete hooks would hang.
    # On retry (§5c), the CR was verified deleted above, so the hook is redundant.
    # If even the uninstall cannot clear it, drop the release-record Secrets
    # directly — with no deployed revision the record is all that blocks the
    # install. Both failing leaves the record in place, so let set -e stop
    # the run here, before the upgrade fails less legibly. No --wait and no
    # hooks means Helm's uninstall timeout would bound nothing, so none is
    # passed.
    helm uninstall "${HELM_RELEASE_NAME}" -n "${NAMESPACE}" --no-hooks \
      || kubectl delete secret -n "${NAMESPACE}" -l "${HELM_RELEASE_SECRET_SELECTOR}" --ignore-not-found
    echo "✓ Cleared the poisoned ${HELM_RELEASE_NAME} release record"
  fi
}

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying ${DEPLOY_SOURCE} to Namespace: ${NAMESPACE} ==="

# ─── 3. Cluster Auth ──────────────────────────────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gke_dns_endpoint_flag "$CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# ─── 4. Build Container Images ────────────────────────────────────────────────
# Skipped whole on the release-candidate path: the candidate's images are what
# is being evaluated, and a rebuild from the same source is a different artefact
# — different base-image digests, different build timestamps, and a different
# _KUBE_AGENTS_VERSION baked in as the remote-MCP User-Agent. An eval that graded
# a rebuild would not be grading the thing the release ships.
if [ -n "${RC_COMMIT_SHA:-}" ]; then
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Skipping image builds: installing published images at ${RC_COMMIT_SHA:0:7} ==="
else
  STEP_START=$SECONDS
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Building Container Images (platform, credential-proxy, sandbox, operator) ==="
  # One submit, not four. The two agent images share the agent-base chain, so
  # building them as consecutive steps on one worker lets the second reuse the
  # first's layers instead of rebuilding that chain on a cold daemon; the sandbox
  # and operator builds run alongside them. See the header of cloudbuild-ci.yaml,
  # and #635.
  # Set REQUIRE_CACHE=true in the job environment to fail the build on a cache
  # miss instead of cold-building. Default false so a broken cache source cannot
  # block the PR that fixes it.
  export CACHE_IMAGE="${CACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:latest}"
  # The postsubmit's mode=max cache manifests; CACHE_IMAGE stays the fallback.
  export BUILDCACHE_IMAGE="${BUILDCACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:buildcache}"
  export PROXY_BUILDCACHE_IMAGE="${PROXY_BUILDCACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/credential-proxy:buildcache}"
  # Under EVAL_MODE_NEXT=1 the same build also produces the four first-party
  # A2A images and the Hermes bridge sidecar, in its `a2a` and `a2a-bridge`
  # steps; with the
  # substitutions absent that step is a no-op and the build is the four-image
  # one above. Empty otherwise, so the command below is byte-for-byte what it
  # was. The references go to the operator through operator.extraEnv
  # in step 5: the operator reads its A2A image overrides from its own
  # environment; without them it would derive the same five A2A references
  # from its own image and the bridge's from the agent's (both are this
  # build's, under the same repository and tag), so the overrides are belt and braces that keep the
  # deploy's inputs explicit and byte-pinned by the tests. The same
  # value list arms the gateway's inject door, which the operator likewise
  # reads from its own environment and never from the CR (a2aInjectBackendEnvVar
  # says why): without it there is no Service for the eval's transport to
  # reach. The bridge's reference goes to the operator the same way, with its
  # concurrency: the operator renders the bridge sidecar into the agent pod
  # under next and reads both from its environment. Its executor is left to
  # the bridge's default (BRIDGE_EXECUTOR_EXPECTED).
  A2A_BUILD_SUBSTITUTIONS=""
  if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then
    A2A_GATEWAY_URI="${AR_REPO}/${A2A_GATEWAY_IMAGE_NAME}:${TAG}"
    A2A_CALLOUT_URI="${AR_REPO}/${A2A_CALLOUT_IMAGE_NAME}:${TAG}"
    A2A_WORKER_URI="${AR_REPO}/${A2A_WORKER_IMAGE_NAME}:${TAG}"
    A2A_VERIFIER_URI="${AR_REPO}/${A2A_VERIFIER_IMAGE_NAME}:${TAG}"
    A2A_CONSOLE_URI="${AR_REPO}/${A2A_CONSOLE_IMAGE_NAME}:${TAG}"
    A2A_BRIDGE_URI="${AR_REPO}/${A2A_BRIDGE_IMAGE_NAME}:${TAG}"
    A2A_BUILD_SUBSTITUTIONS=",_A2A_GATEWAY_URI=${A2A_GATEWAY_URI},_A2A_CALLOUT_URI=${A2A_CALLOUT_URI},_A2A_WORKER_URI=${A2A_WORKER_URI},_A2A_VERIFIER_URI=${A2A_VERIFIER_URI},_A2A_CONSOLE_URI=${A2A_CONSOLE_URI},_A2A_BRIDGE_URI=${A2A_BRIDGE_URI}"
    A2A_OPERATOR_ENV_ARGS=(
      --set-string "operator.extraEnv[0].name=${A2A_GATEWAY_IMAGE_ENV_VAR}"
      --set-string "operator.extraEnv[0].value=${A2A_GATEWAY_URI}"
      --set-string "operator.extraEnv[1].name=${A2A_CALLOUT_IMAGE_ENV_VAR}"
      --set-string "operator.extraEnv[1].value=${A2A_CALLOUT_URI}"
      --set-string "operator.extraEnv[2].name=${A2A_WORKER_IMAGE_ENV_VAR}"
      --set-string "operator.extraEnv[2].value=${A2A_WORKER_URI}"
      # The verifier is on the request path: unoverridden it stays on a
      # private dev registry a leased eval project cannot pull, the Deployment
      # never comes up, and every executor refuses every task -- an eval that
      # reads as a broken product rather than a missing override.
      --set-string "operator.extraEnv[3].name=${A2A_VERIFIER_IMAGE_ENV_VAR}"
      --set-string "operator.extraEnv[3].value=${A2A_VERIFIER_URI}"
      --set-string "operator.extraEnv[4].name=${A2A_CONSOLE_IMAGE_ENV_VAR}"
      --set-string "operator.extraEnv[4].value=${A2A_CONSOLE_URI}"
      --set-string "operator.extraEnv[5].name=${A2A_INJECT_BACKEND_ENV_VAR}"
      --set-string "operator.extraEnv[5].value=${A2A_INJECT_BACKEND_ON}"
      # The rendered bridge: its image and the concurrency section 2b admitted
      # (the TASKS budget reads the same value). No executor: the lane runs
      # the bridge's default, api.
      --set-string "operator.extraEnv[6].name=${A2A_BRIDGE_IMAGE_ENV_VAR}"
      --set-string "operator.extraEnv[6].value=${A2A_BRIDGE_URI}"
      --set-string "operator.extraEnv[7].name=${A2A_BRIDGE_CONCURRENCY_ENV_VAR}"
      --set-string "operator.extraEnv[7].value=${MODE_NEXT_BRIDGE_CONCURRENCY}"
    )
    echo "EVAL_MODE_NEXT=1: also building the A2A gateway, auth callout, worker, verifier and console images and the Hermes bridge sidecar"
  fi
  gcloud builds submit --config="deploy/docker/cloudbuild-ci.yaml" \
    --substitutions="_PLATFORM_URI=${AR_REPO}/platform-agent:${TAG},_PROXY_URI=${AR_REPO}/credential-proxy:${TAG},_SANDBOX_URI=${AR_REPO}/agent-sandbox:${TAG},_OPERATOR_URI=${AR_REPO}/kube-agents-operator:${TAG},_CACHE_IMAGE=${CACHE_IMAGE},_BUILDCACHE_IMAGE=${BUILDCACHE_IMAGE},_PROXY_BUILDCACHE_IMAGE=${PROXY_BUILDCACHE_IMAGE},_HERMES_AGENT_TAG=${HERMES_AGENT_TAG},_KUBE_AGENTS_VERSION=${TAG},_REQUIRE_CACHE=${REQUIRE_CACHE:-false}${A2A_BUILD_SUBSTITUTIONS}" \
    --project="${PROJECT_ID}" "${BUILD_WORKER_ARGS[@]}" --quiet .
  echo "✓ Container image builds finished in $((SECONDS - STEP_START))s"
fi

# ─── 5. Chart Deployment ──────────────────────────────────────────────────────
# One helm release carries the whole install — operator, credentials Secret,
# agent CR, and LiteLLM — so there is nothing to apply piecemeal or keep in order.
#
# The chart is `./charts/kube-agents`, out of the checkout, on both paths. On the
# release-candidate path that makes the checkout load-bearing: the images come
# from RC_COMMIT_SHA and the chart, CRDs and eval tasks come from whatever tree
# this runs in, so the caller has to have checked the tree out at that commit
# first or the run grades the candidate's images against another revision's
# everything-else. hack/resolve-rc-target.sh is where that contract is written
# down, and .github/workflows/deploy-environment.yml is the existing precedent
# for honouring it with a checkout step.
# Webhooks stay at the chart's default (off): a PR evaluation cluster carries
# no cert-manager, and admission-webhook coverage belongs to the operator's
# own test suite rather than this smoke pipeline.
#
# runtimeClassName is pinned empty rather than left at the chart's default,
# which is `gvisor`. Step 7 reaches the agent over `kubectl port-forward`, and
# that does not work against a sandboxed pod -- the forward is set up in the
# host-side CNI netns while the listener lives in the sandbox's own network
# stack, so the connection is refused
# (docs/site/src/content/docs/operator/platformagent-crd.md is canonical on
# this; scripts/exec_tunnel.py is the relay that reaches one instead, as
# tests/e2e does). On a pool cluster with no `gvisor` RuntimeClass the pod
# would not schedule at all. Either way this job wants the standard runtime;
# what the sandbox does to the agent is the release pipeline's to exercise,
# not a smoke test's.
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying the kube-agents chart ==="

# ─── 5a. Heal a poisoned release record (#1172, #2382) ─────────────────────────
# A failed or killed prior run can leave the release record behind with no
# deployed revision: its teardown's `helm uninstall` failed, or the teardown
# was killed mid-uninstall — the cause no teardown-side fallback can cover.
# `helm upgrade --install` below then takes the upgrade path and dies with
# `UPGRADE FAILED: "kube-agents" has no deployed releases`, instantly
# failing whichever PR drew this pool project. Heal it here, at lease time
# or in the 5c retry loop, where causes of the no-deployed-revision state converge.
# (A release stuck `pending-upgrade` *above* a deployed revision is a different state —
# upgrade then fails on Helm's in-progress lock, but that run's own teardown
# uninstall clears it, so it burns one run rather than poisoning the pool.)
#
# The probe is `helm history -o json` because it reads the same store the
# failing code path reads: Helm's upgrade errors in Releases.Deployed()
# (pkg/action/upgrade.go) when no release-record Secret carries status
# "deployed", and `helm history` lists exactly those record Secrets with
# their statuses. "History succeeds but no revision is deployed" is
# therefore precisely the state upgrade rejects — including a latest-failed
# release with an older deployed revision, which upgrades fine and is left
# alone. One call (or bounded re-probes if the probe hits a transient control-plane error);
# a healthy or absent release costs the probe and nothing more.
heal_poisoned_release_record

API_SERVER_KEY="${API_SERVER_KEY:-$(openssl rand -hex 16)}"

# ─── 5b. The shell sandbox keypair ────────────────────────────────────────────
# The chart cannot generate this one — sprig emits PEM and has no encoder for
# authorized_keys form — so every install surface supplies it: `install.sh`
# through the Terraform composition's tls_private_key, `upgrade.sh` through
# backfill_sandbox_ssh_key, and this job here. Without it the chart renders no
# authorized-keys Secret and the sandbox never starts.
#
# --set-file rather than --set-string: the private half is a PEM, and Helm's
# --set parser reads its newlines and commas as syntax. Both halves go into
# credentials.data, which is where the chart's authorized-keys template reads
# the public one from and where the gateway's init container finds the private
# one.
SANDBOX_KEY_DIR="$(umask 077 && mktemp -d)"
ssh-keygen -q -t "${SANDBOX_SSH_KEY_TYPE}" -N '' -C "${SANDBOX_SSH_KEY_COMMENT}" \
  -f "${SANDBOX_KEY_DIR}/id_sandbox"

# ─── 5b-ii. The GitLab forge credential ──────────────────────────────────────
# EVAL_FORGE=gitlab only. The agent token is a personal access token of the
# bot account, one for the pool, kept in GITLAB_SECRETS_PROJECT's Secret
# Manager (docs/ci-pool-projects.md 5.6). It goes Secret Manager -> kubectl over a
# pipe: never a file and never an argument, so it is in no artifact and no
# `ps`. The Secret is applied, not created, so a re-deploy on the same
# cluster picks up a rotated token, and it carries the label hack/ci-teardown.sh
# sweeps by (its SWEEP_SELECTOR; the pair is pinned equal by the tests), so the
# token leaves the host cluster with the lease instead of outliving it.
GITLAB_FORGE_SECRET_LABEL="app.kubernetes.io/part-of=kube-agents"
materialize_gitlab_forge_secret() {
  local manifest
  # Rendered first, applied second: in one pipe the apply would run on the
  # empty stream a failed read leaves, and only then would pipefail report it.
  # tr: a value stored with a trailing newline (echo into --data-file=-) would
  # otherwise reach GitLab as part of the token.
  manifest="$(gcloud secrets versions access latest --secret="${GITLAB_AGENT_SM_SECRET}" --project="${GITLAB_SECRETS_PROJECT}" \
    | tr -d '\r\n' \
    | kubectl create secret generic "${GITLAB_FORGE_SECRET_NAME}" -n "${NAMESPACE}" \
        --from-file="${GITLAB_FORGE_SECRET_KEY}=/dev/stdin" --dry-run=client -o yaml \
    | kubectl label --local -f - "${GITLAB_FORGE_SECRET_LABEL}" -o yaml)" || {
    # The read itself passed the preflight in 2d, so the stage that failed is
    # as likely a kubectl one; each stage's own stderr is just above this line.
    echo "ERROR: could not render the GitLab forge Secret from Secret Manager ${GITLAB_SECRETS_PROJECT}/${GITLAB_AGENT_SM_SECRET}; the failing stage (gcloud, tr, kubectl create, kubectl label) reported just above (docs/ci-pool-projects.md 5.6)." >&2
    return 1
  }
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  printf '%s\n' "${manifest}" | kubectl apply -f - >/dev/null
  echo "GitLab forge credential: Secret ${NAMESPACE}/${GITLAB_FORGE_SECRET_NAME} (key ${GITLAB_FORGE_SECRET_KEY}) from Secret Manager ${GITLAB_SECRETS_PROJECT}/${GITLAB_AGENT_SM_SECRET}"
}
if [ "${EVAL_FORGE}" = "gitlab" ]; then
  materialize_gitlab_forge_secret
fi

# ─── 5c. Deploy the chart ─────────────────────────────────────────────────────
# Named in the build log so a run's dispatcher behaviour can be read against
# the cap it was given without opening the rendered CR.
echo "Kanban board cap for this install: max_in_progress=${EVAL_KANBAN_MAX_IN_PROGRESS} (spec.harness.tuning.maxInProgress)"

HELM_INSTALL_OUT="$(mktemp)"
HELM_EXIT=0
for ((attempt=1; attempt<=HELM_DEPLOY_ATTEMPTS; attempt++)); do
  set +e
  helm upgrade --install "${HELM_RELEASE_NAME}" ./charts/kube-agents \
    --namespace "${NAMESPACE}" --create-namespace \
    "${IMAGE_ARGS[@]}" \
    --set-string "platformAgent.harness.clusterName=${CLUSTER_NAME}" \
    --set-string "platformAgent.harness.location=${REGION}" \
    --set-string "platformAgent.harness.projectId=${PROJECT_ID}" \
    --set-string "platformAgent.security.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    "${FORGE_ARGS[@]}" \
    "${GITHUB_MINTER_ARGS[@]}" \
    --set "platformAgent.credentials.create=true" \
    --set-string "platformAgent.credentials.data.API_SERVER_KEY=${API_SERVER_KEY}" \
    --set-string "platformAgent.credentials.data.GEMINI_API_KEY=${GEMINI_API_KEY}" \
    --set-file "platformAgent.credentials.data.SANDBOX_SSH_PRIVATE_KEY=${SANDBOX_KEY_DIR}/id_sandbox" \
    --set-file "platformAgent.credentials.data.SANDBOX_SSH_PUBLIC_KEY=${SANDBOX_KEY_DIR}/id_sandbox.pub" \
    --set-string "litellm.modelProvider=${MODEL_PROVIDER}" \
    --set-string "litellm.modelDefaultName=${MODEL_DEFAULT_NAME}" \
    --set-string "litellm.vertex.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=${LITELLM_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --set "platformAgent.deployment.availability.runtimeClassName=" \
    --set "platformAgent.harness.tuning.maxInProgress=${EVAL_KANBAN_MAX_IN_PROGRESS}" \
    --set-string "platformAgent.deployment.env[0].name=ALERT_DAILY_LIMIT_WARNING" \
    --set-string "platformAgent.deployment.env[0].value=${EVAL_ALERT_DAILY_LIMIT_WARNING}" \
    --set-string "platformAgent.deployment.env[1].name=ALERT_DAILY_LIMIT_DRIFT" \
    --set-string "platformAgent.deployment.env[1].value=${EVAL_ALERT_DAILY_LIMIT_DRIFT}" \
    --set "platformAgent.harness.driftDetector.enabled=true" \
    --set-string "platformAgent.harness.driftDetector.subscription=${EVAL_DRIFT_SUBSCRIPTION}" \
    ${A2A_OPERATOR_ENV_ARGS[@]+"${A2A_OPERATOR_ENV_ARGS[@]}"} \
    --wait --timeout 15m 2>&1 | tee "${HELM_INSTALL_OUT}"
  HELM_EXIT="${PIPESTATUS[0]}"
  set -e

  if [ "${HELM_EXIT}" -eq 0 ]; then
    break
  fi

  if grep -Eq "${HELM_API_SERVER_5XX_RE}" "${HELM_INSTALL_OUT}" && [ "${attempt}" -lt "${HELM_DEPLOY_ATTEMPTS}" ]; then
    retry_delay=$((HELM_DEPLOY_RETRY_DELAY_SECONDS * attempt))
    echo "WARNING: Helm chart deployment attempt ${attempt} of ${HELM_DEPLOY_ATTEMPTS} hit a transient API-server 5xx, retrying in ${retry_delay}s..."
    sleep "${retry_delay}"
    # Wait for the control plane to recover before running recovery calls
    # (CR delete, helm history probe/uninstall) (#2382).
    readyz_start=$SECONDS
    readyz_ok=0
    while (( SECONDS - readyz_start < APISERVER_READYZ_TIMEOUT_SECONDS )); do
      if kubectl get --raw /readyz >/dev/null 2>&1; then
        readyz_ok=1
        break
      fi
      sleep "${APISERVER_READYZ_POLL_INTERVAL_SECONDS}"
    done
    if [ "${readyz_ok}" -eq 0 ]; then
      echo "WARNING: API server /readyz did not become ready within ${APISERVER_READYZ_TIMEOUT_SECONDS}s; proceeding with recovery calls." >&2
    fi
    # If the failed attempt left behind a release record with no deployed revision,
    # clear it so the next attempt can install cleanly (#1172, #2382).
    heal_poisoned_release_record "attempt ${attempt} failed before reaching a deployed revision (#1172, #2382)" "retrying"
  else
    if [ "${attempt}" -lt "${HELM_DEPLOY_ATTEMPTS}" ]; then
      echo "ERROR: Helm chart deployment attempt ${attempt} failed with an error that is not a transient API-server 5xx; it is not retried (exit ${HELM_EXIT})." >&2
    else
      echo "ERROR: Helm chart deployment failed on all ${HELM_DEPLOY_ATTEMPTS} attempts; giving up (exit ${HELM_EXIT})." >&2
    fi
    rm -f "${HELM_INSTALL_OUT}"
    exit "${HELM_EXIT}"
  fi
done
rm -f "${HELM_INSTALL_OUT}"
# Deleted here rather than from the EXIT trap, which two later steps replace.
# A failed install leaves the directory behind in a pod prow destroys with the
# lease, and nothing uploads it — /logs/artifacts is the only path off this box.
rm -rf "${SANDBOX_KEY_DIR}"
echo "✓ Chart deployment finished in $((SECONDS - STEP_START))s"

# ─── 6. Readiness Verification ────────────────────────────────────────────────
# helm --wait covers the chart-created Deployments (operator, LiteLLM); the
# agent Deployment is created by the operator reconciling the CR, so it gets
# its own gate with diagnostics.
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying platform-agent rollout ==="
for _ in {1..60}; do
  kubectl get deployment platform-agent-gateway -n "${NAMESPACE}" >/dev/null 2>&1 && break
  sleep 5
done
if ! kubectl rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=600s; then
  echo "ERROR: platform-agent-gateway rollout failed"
  kubectl describe deployment/platform-agent-gateway -n "${NAMESPACE}" || true
  kubectl get pods -n "${NAMESPACE}" || true
  kubectl logs -n "${NAMESPACE}" -l app=platform-agent-gateway --all-containers --tail=50 || true
  exit 1
fi

# The shell sandbox is the other half of the agent: everything the model runs
# executes there over ssh, so a gateway that is Ready against a StatefulSet
# stuck on ImagePullBackOff is an install this job must fail rather than pass.
# Gated separately for the same reason the Deployment is -- the operator
# creates it from the CR, so `helm --wait` never saw it.
for _ in {1..60}; do
  kubectl get statefulset platform-agent-shell -n "${NAMESPACE}" >/dev/null 2>&1 && break
  sleep 5
done
if ! kubectl rollout status statefulset/platform-agent-shell -n "${NAMESPACE}" --timeout=600s; then
  echo "ERROR: platform-agent-shell rollout failed"
  kubectl describe statefulset/platform-agent-shell -n "${NAMESPACE}" || true
  kubectl get pods -n "${NAMESPACE}" || true
  kubectl logs -n "${NAMESPACE}" statefulset/platform-agent-shell --all-containers --tail=50 || true
  exit 1
fi

# The detector is the third thing the operator builds from the CR that
# `helm --wait` cannot see, and the only one whose failure leaves the pod
# Ready. driftDetectorEnabled returns false without erroring on a numeric
# projectId or an empty location, and an enabled detector that exits on every
# start is retried forever by start-services.sh behind a Ready gateway
# (charts/kube-agents/README.md). Either way every drift case in the lease
# reds as an agent triage failure with nothing in this log saying the install
# was wrong -- which is what this gate is for.
#
# Deliberately not a check that records are arriving. A project onboarded
# before the ingress existed reaches the marker and then fails its pulls, and
# failing the deploy for that would red the whole matrix over a gap only a
# drift case cares about (docs/ci-pool-projects.md).
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying drift-detector startup ==="
drift_detector_started=false
# `grep -F ... >/dev/null` rather than `grep -qF`, and the difference is the
# whole gate. `-q` exits on the first match, which closes the pipe under a
# kubectl still writing; kubectl takes SIGPIPE, and the `set -o pipefail` at
# the top of this script turns that into a failed pipeline. The marker prints
# once when the detector starts and it keeps logging after that, so the log is
# past the 64 KiB pipe buffer by the time this runs and the match is an early
# line -- the shape that fails. Found reads as not found, the loop exhausts,
# and a healthy install reds every case in the lease, with the message below
# saying the detector never started. Draining a bounded log costs one read.
for _ in $(seq "${EVAL_DRIFT_READY_ATTEMPTS}"); do
  if kubectl logs -n "${NAMESPACE}" deployment/platform-agent-gateway \
    -c "${EVAL_DRIFT_READY_CONTAINER}" 2>/dev/null |
    grep -F "${EVAL_DRIFT_READY_MARKER}" >/dev/null; then
    drift_detector_started=true
    break
  fi
  sleep "${EVAL_DRIFT_READY_INTERVAL_SECONDS}"
done
if [[ "${drift_detector_started}" != "true" ]]; then
  echo "ERROR: drift-detector never reached its pull loop on this install"
  echo "       (no '${EVAL_DRIFT_READY_MARKER}' in the ${EVAL_DRIFT_READY_CONTAINER} container)"
  kubectl get platformagent -n "${NAMESPACE}" -o yaml || true
  kubectl logs -n "${NAMESPACE}" deployment/platform-agent-gateway \
    -c "${EVAL_DRIFT_READY_CONTAINER}" --tail=100 || true
  exit 1
fi
echo "✓ Rollout verification finished in $((SECONDS - STEP_START))s"

# ─── 6b. EVAL_MODE_NEXT: switch to spec.mode: next and gate the bus ──────────
# Everything above proved the today-mode install. From here the CR is patched
# and the gates run over what `mode: next` adds, in dependency order: the NATS
# StatefulSet (the bus), the callout Deployment (the Job below cannot
# authenticate to NATS without it), the provisioning Job (the streams; until
# it completes there is nothing on the bus), and then the agent Deployment,
# which the operator rolls when it pins the mode into the managed .env and
# the config hash moves. The Deployment's generation is recorded before the
# patch: `rollout status` right after it would answer for the today
# ReplicaSet, before the operator has reconciled anything.
#
# Then the door and the executor, which the eval's transport needs and the
# mode alone does not give it. The inject door's Service and token Secret are
# waited for (the operator renders them only with the flag step 5 set on it;
# hack/ci-eval-pr.sh reads that Secret). The bridge sidecar is the
# operator's: it budgets it from the first next render and adds it to the
# agent pod once the bus is provisioned, with the image and BRIDGE_CONCURRENCY
# step 5 set on it and the bridge's default executor
# (a2a/docs/hermes-bridge.md, "Where it runs"), so the mode patch is the only
# patch and the first provisioning Job already budgets the bridge's workers:
# no second render re-measures that budget against the stream the Job
# created, so the refusal #2077 guarded against cannot arise. The bridge
# enters the agent pod only once the bus is provisioned (BusProvisioned), so
# the agent Deployment rolls twice: once for the mode patch and once, after
# the Job, for the bridge. The step gates both rolls. Then the step ends on the bridge's own word that it is
# consuming `platform` tasks; until then the bus has an executor for nobody
# and every case on the inject transport ends as infrastructure. A rendered
# bridge leaves with the mode, so a flip back to today needs no CR edit for
# it; the one flip back the lane makes is hack/rollback-roundtrip.sh, after
# the matrix.
#
# The verifier is gated too, and gated LAST of everything here, which is not
# where its dependency would put it. Its precondition is the provisioning Job
# -- it binds the capability bucket at boot and exits when it cannot, so until
# the Job has created the bucket it crash-loops -- but its deadline is the
# first submission, which is hack/ci-eval-pr.sh, after this step. Waiting on
# it right after the Job would put a kubelet restart backoff of up to five
# minutes AHEAD of the agent rollout and the bridge's start, and so add that
# backoff to the deploy; waiting on it at the end spends the same backoff
# alongside the agent rollout and the bridge coming up, and still answers
# the only question that matters, which is whether the verifier is answering
# before anything asks it. Gated rather than reported because every executor
# turns an unanswered Check into a terminal rejection: a verifier still in
# backoff when the eval starts does not slow a case down, it refuses it, and
# the whole eval reads as a broken product (the same reason the slice pins
# A2A_VERIFIER_IMAGE through operator.extraEnv at all).
#
# Reported, not gated: the A2A gateway Deployment. It used to exit on start
# without a chat backend (#1660); the inject door is one, so it now starts,
# but no pool-project run has shown it coming up yet and a gateway that is
# down is what the harness classifies as infrastructure on every case rather
# than a deploy failure. Gating it is the change that follows the first
# measured run through the door. Not gated either: the shell StatefulSet,
# which does not change under next.
#
# Every A2A pod the operator renders carries CPU and memory requests and
# limits (#1700), so a namespace ResourceQuota on limits.cpu admits them;
# nothing this deploy or the chart renders carries such a quota either way.
dump_mode_next_state() {
  kubectl get platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" -o yaml | sed -n '/^status:/,$p' || true
  kubectl get pods,jobs,networkpolicies,pvc -n "${NAMESPACE}" -l "${A2A_PART_OF_SELECTOR}" || true
  kubectl get events -n "${NAMESPACE}" --sort-by=.lastTimestamp | tail -"${MODE_NEXT_DIAG_EVENT_LINES}" || true
  kubectl logs -n "${NAMESPACE}" "deployment/${OPERATOR_DEPLOYMENT_NAME}" --tail="${MODE_NEXT_DIAG_LOG_LINES}" || true
  # The verifier's own log, on every failure path and not only its gate's.
  # Its one durable failure -- it could not bind the capability bucket, so it
  # exited -- is a line in this log and nowhere else: `describe` shows a
  # CrashLoopBackOff without the reason, and the CR's A2AVerifier condition
  # says zero replicas are ready without saying why. Previous as well as
  # current, because by the time anything reads this the container that
  # printed it has usually already been restarted.
  kubectl logs -n "${NAMESPACE}" "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-verifier" --tail="${MODE_NEXT_DIAG_LOG_LINES}" 2>/dev/null || true
  kubectl logs -n "${NAMESPACE}" "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-verifier" --previous --tail="${MODE_NEXT_DIAG_LOG_LINES}" 2>/dev/null || true
}

# Waits for the operator to create the workload, then for its rollout; on
# failure describes it, dumps the stack and stops the deploy.
gate_mode_next_rollout() {
  local workload="$1"
  local gate_start=$SECONDS
  for _ in $(seq 1 "${MODE_NEXT_GENERATION_ATTEMPTS}"); do
    kubectl get "${workload}" -n "${NAMESPACE}" >/dev/null 2>&1 && break
    sleep "${MODE_NEXT_POLL_SECONDS}"
  done
  if ! kubectl rollout status "${workload}" -n "${NAMESPACE}" --timeout="${MODE_NEXT_ROLLOUT_TIMEOUT}"; then
    echo "ERROR: ${workload} rollout failed under mode: next"
    kubectl describe "${workload}" -n "${NAMESPACE}" || true
    dump_mode_next_state
    exit 1
  fi
  echo "✓ ${workload} rolled out $((gate_start - MODE_NEXT_START))s..$((SECONDS - MODE_NEXT_START))s after the patch"
}

# Waits for the agent Deployment's generation to move past the one given,
# which is the operator having reconciled the CR patch named; stops the deploy
# if it never does. Prints the new generation.
wait_agent_generation_past() {
  local before="$1" what="$2"
  local after="${before}"
  for _ in $(seq 1 "${MODE_NEXT_GENERATION_ATTEMPTS}"); do
    # A read the API drops is one more poll, not the end of the deploy.
    after="$(kubectl get "deployment/${AGENT_DEPLOYMENT_NAME}" -n "${NAMESPACE}" -o jsonpath='{.metadata.generation}' 2>/dev/null)" || after="${before}"
    [ "${after}" != "${before}" ] && break
    sleep "${MODE_NEXT_POLL_SECONDS}"
  done
  if [ "${after}" = "${before}" ]; then
    echo "ERROR: the agent Deployment never rolled after ${what} (generation ${before} throughout)"
    dump_mode_next_state
    exit 1
  fi
  echo "Agent Deployment generation ${before} -> ${after} at $((SECONDS - MODE_NEXT_START))s after ${what}"
}

# The CR's phase, a tab, then its Ready condition as "<reason>: <message>"
# (nothing for either the CR does not carry), in one read. The operator writes
# the phase and the condition in one status update, so a reader that needs
# both takes them from this one read, never from two that can straddle that
# update and pair a stale phase with a fresh condition.
cr_phase_and_ready_condition() {
  kubectl get platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.phase}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.reason}{": "}{.message}{end}' 2>/dev/null || true
}

# The CR's Ready condition alone, for the reader below that needs only that.
cr_ready_condition() {
  local pair
  pair="$(cr_phase_and_ready_condition)"
  printf '%s' "${pair#*$'\t'}"
}

# Waits for the A2A provisioning Job to reach a terminal condition and stops
# the deploy unless it is Complete. The Job's name carries a digest of its
# rendered spec, so it is found by its component label. Polled for either
# terminal condition rather than `kubectl wait --for=condition=complete`,
# which would sit out the whole budget on a Job that has already failed and
# errors on a selector that matches nothing; the read prints nothing for a
# Job not yet created and the loop simply comes back, so the budget covers
# the Job's creation too. The read lists every Job the label matches with
# its True conditions; a Complete on any counted Job is the gate passing, a
# Failed on one with no Complete elsewhere is the gate failing.
#
# Arguments: what the Job follows, for the log; then, for a wait after a
# patch that re-renders the Job (step 6b has none since the operator renders
# the bridge, #2592; the form is kept, with its tests, for one that does), the name of the Job the patch supersedes and
# the CR generation the patch produced. The superseded Job is not counted:
# it is Complete, and it stays listed until the operator's next pass sweeps
# it, which is after that pass has rolled the agent Deployment (reconcileA2A
# runs after reconcileWorkload in the operator's Reconcile), so a read right
# after the generation moves can still show only the old run. A render the
# patch did not change keeps the old Job -- same digest, same name -- and
# there is nothing to wait for; that is told from "not created yet" by the
# CR's status.observedGeneration, which the pass that would have created it
# writes at its end. Sets PROVISION_JOB_NAME to the Job that passed and
# PROVISION_JOB_RERENDERED to false when the patch kept the old one.
#
# A Failed re-run is a refusal: the operator writes it to the CR's Ready
# condition on the requeue that reads the Job (it does not watch Jobs), so
# the failure waits up to MODE_NEXT_STATUS_ATTEMPTS polls for that condition
# and prints it beside the Job's own log, then stops the deploy either way.
wait_provision_job() {
  local what="$1" superseded="${2:-}" cr_generation="${3:-}"
  local gate_start=$SECONDS deadline=$((SECONDS + MODE_NEXT_PROVISION_JOB_TIMEOUT_SECONDS))
  local listing entry name conditions complete failed observed condition
  local -a entries
  PROVISION_JOB_NAME=""
  PROVISION_JOB_CONDITIONS=""
  PROVISION_JOB_RERENDERED="true"
  while :; do
    # One entry per Job: its name, a colon, its True condition types each
    # followed by a comma, then a space.
    listing="$(kubectl get jobs -n "${NAMESPACE}" -l "${A2A_PROVISION_JOB_SELECTOR}" -o jsonpath='{range .items[*]}{.metadata.name}{":"}{range .status.conditions[?(@.status=="True")]}{.type}{","}{end}{" "}{end}' 2>/dev/null || true)"
    read -r -a entries <<<"${listing}"
    complete=""
    failed=""
    PROVISION_JOB_NAME=""
    PROVISION_JOB_CONDITIONS=""
    for entry in ${entries[@]+"${entries[@]}"}; do
      name="${entry%%:*}"
      conditions="${entry#*:}"
      [ -n "${superseded}" ] && [ "${name}" = "${superseded}" ] && continue
      case ",${conditions}" in
      *",${JOB_CONDITION_COMPLETE},"*) complete="${name}" ;;
      *",${JOB_CONDITION_FAILED},"*) failed="${name}" ;;
      esac
      PROVISION_JOB_NAME="${name}"
      PROVISION_JOB_CONDITIONS="${conditions//,/ }"
    done
    if [ -n "${complete}" ]; then
      PROVISION_JOB_NAME="${complete}"
      PROVISION_JOB_CONDITIONS="${JOB_CONDITION_COMPLETE}"
      break
    elif [ -n "${failed}" ]; then
      PROVISION_JOB_NAME="${failed}"
      PROVISION_JOB_CONDITIONS="${JOB_CONDITION_FAILED}"
      break
    elif [ -n "${superseded}" ] && [ -z "${PROVISION_JOB_NAME}" ] && [[ " ${listing}" == *" ${superseded}:"* ]]; then
      # Only the superseded Job is listed. Kept by a pass that has observed
      # the patched generation, it is the current render.
      observed="$(kubectl get platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.observedGeneration}' 2>/dev/null || true)"
      if [ -n "${observed}" ] && [ "${observed}" -ge "${cr_generation}" ]; then
        PROVISION_JOB_NAME="${superseded}"
        PROVISION_JOB_CONDITIONS="${JOB_CONDITION_COMPLETE}"
        PROVISION_JOB_RERENDERED="false"
        break
      fi
    fi
    [ "${SECONDS}" -ge "${deadline}" ] && break
    sleep "${MODE_NEXT_POLL_SECONDS}"
  done
  if [ "${PROVISION_JOB_CONDITIONS}" != "${JOB_CONDITION_COMPLETE}" ]; then
    echo "ERROR: the A2A provisioning Job did not complete within ${MODE_NEXT_PROVISION_JOB_TIMEOUT_SECONDS}s after ${what} (Job: ${PROVISION_JOB_NAME:-none}; conditions: ${PROVISION_JOB_CONDITIONS:-none})"
    if [ "${PROVISION_JOB_CONDITIONS}" = "${JOB_CONDITION_FAILED}" ]; then
      condition=""
      for _ in $(seq 1 "${MODE_NEXT_STATUS_ATTEMPTS}"); do
        condition="$(cr_ready_condition)"
        [[ "${condition}" == "${CR_READY_REASON_PROVISION_FAILED}: "* ]] && break
        sleep "${MODE_NEXT_POLL_SECONDS}"
      done
      echo "${PLATFORM_AGENT_CR_NAME} Ready condition: ${condition:-none}"
    fi
    kubectl describe jobs -n "${NAMESPACE}" -l "${A2A_PROVISION_JOB_SELECTOR}" || true
    echo "--- provisioning Job pod logs ---"
    kubectl logs -n "${NAMESPACE}" -l "${A2A_PROVISION_JOB_SELECTOR}" --tail="${MODE_NEXT_DIAG_LOG_LINES}" || true
    echo "--- NATS pod log ---"
    kubectl logs -n "${NAMESPACE}" -l "${A2A_NATS_POD_SELECTOR}" --tail="${MODE_NEXT_DIAG_LOG_LINES}" || true
    dump_mode_next_state
    exit 1
  fi
  if [ "${PROVISION_JOB_RERENDERED}" = "false" ]; then
    echo "✓ ${what} left the A2A provisioning Job's render unchanged (${PROVISION_JOB_NAME} is still the current run) $((gate_start - MODE_NEXT_START))s..$((SECONDS - MODE_NEXT_START))s after the patch"
  else
    echo "✓ A2A provisioning Job ${PROVISION_JOB_NAME} complete $((gate_start - MODE_NEXT_START))s..$((SECONDS - MODE_NEXT_START))s after the patch"
  fi
}

# Reads the CR's phase and Ready condition, in one read, after a provisioning
# Job completed and stops the deploy on a refusal the Job's own conditions did
# not show: phase Degraded, or Ready carrying the reason a refused provision is
# given. Step 6b does not call it since the operator renders the bridge
# (#2592): the one provision Job already counts the bridge's workers, so no
# later render can be refused. It is kept, with its tests, for a step that
# re-renders the Job again.
# A refusal is already written when the Job is done, not a lag, so it fails
# on the first read that answers, and so does every other Degraded but one: a
# pod waiting for CPU or memory (CR_READY_REASON_POD_UNSCHEDULABLE with a
# count matching SCHEDULER_CAPACITY_SHORTFALL_RE), which an Autopilot scale-up
# clears on its own (#2414). That one is re-read up to
# MODE_NEXT_UNSCHEDULABLE_ATTEMPTS times, with a line per read, and fails as
# any other Degraded does if it is still there at the end or turns into
# something else. When the condition is about an agent pod, every read that
# returns it, the first and the last included, also reads the agent's pods,
# in one read, and a live pod bound to a node (the one the condition names,
# while it is listed) ends the gate as a hand-off to the
# agent Deployment's rollout gate that follows it, because the condition can
# outlive the wait it describes (the window's constant says why). A read
# that returns nothing, the first read included, is one more re-read against
# that window, never a pass: the read swallows a failed GET, and the CR
# carries a status once its provisioning Job has run, so nothing read is no
# answer. A window that ends with no read answering
# fails as a CR that could not be read. Prints the condition either way, so
# the artifact says what the CR said.
gate_cr_not_degraded() {
  local what="$1" pair phase="" condition="" rereads=0 answered="" capacity="" unanswered="" gate_start=$SECONDS
  local agent_pods pod_line pod_node="" pod_name="" named_pod line_name line_node line_phase line_deleted rest fallback_name fallback_node named_listed
  while :; do
    # One read, so the phase and the condition are one object version's.
    pair="$(cr_phase_and_ready_condition)"
    if [ -z "${pair//$'\t'/}" ]; then
      # Nothing read: a GET the API dropped (the read swallows the failure)
      # or a status read back empty. One more poll against the window, never
      # a pass. phase and condition keep the last answering read's, so a
      # window that ends here fails on it, or, with none, as unread.
      if [ "${rereads}" -lt "${MODE_NEXT_UNSCHEDULABLE_ATTEMPTS}" ]; then
        rereads=$((rereads + 1))
        if [ -n "${answered}" ]; then
          echo "the read of ${PLATFORM_AGENT_CR_NAME} after ${what} returned nothing, $((SECONDS - gate_start))s in; re-read ${rereads}/${MODE_NEXT_UNSCHEDULABLE_ATTEMPTS} in ${MODE_NEXT_POLL_SECONDS}s (last Ready condition: ${condition})"
        elif [ "${rereads}" -eq 1 ]; then
          echo "the first read of ${PLATFORM_AGENT_CR_NAME} after ${what} returned nothing; re-read ${rereads}/${MODE_NEXT_UNSCHEDULABLE_ATTEMPTS} in ${MODE_NEXT_POLL_SECONDS}s (no read has answered yet)"
        else
          echo "the read of ${PLATFORM_AGENT_CR_NAME} after ${what} returned nothing again, $((SECONDS - gate_start))s in; re-read ${rereads}/${MODE_NEXT_UNSCHEDULABLE_ATTEMPTS} in ${MODE_NEXT_POLL_SECONDS}s (no read has answered yet)"
        fi
        sleep "${MODE_NEXT_POLL_SECONDS}"
        continue
      fi
      if [ -z "${answered}" ]; then
        echo "ERROR: could not read ${PLATFORM_AGENT_CR_NAME} after ${what}: all $((rereads + 1)) reads in $((SECONDS - gate_start))s returned nothing, so there is no phase or Ready condition to judge"
        echo "--- provisioning Job pod logs ---"
        kubectl logs -n "${NAMESPACE}" -l "${A2A_PROVISION_JOB_SELECTOR}" --tail="${MODE_NEXT_DIAG_LOG_LINES}" || true
        dump_mode_next_state
        exit 1
      fi
      unanswered="; the last read returned nothing, so this is the last one that answered"
    else
      answered="true"
      phase="${pair%%$'\t'*}"
      condition="${pair#*$'\t'}"
      if [ "${phase}" != "${CR_PHASE_DEGRADED}" ] && [[ "${condition}" != "${CR_READY_REASON_PROVISION_FAILED}: "* ]]; then
        break
      fi
      if [ "${phase}" = "${CR_PHASE_DEGRADED}" ] && [[ "${condition}" == "${CR_READY_REASON_POD_UNSCHEDULABLE}: Pod ${AGENT_DEPLOYMENT_NAME}-"* ]] &&
        [[ "${condition}" =~ ${SCHEDULER_CAPACITY_SHORTFALL_RE} ]]; then
        # The condition may be stale: the operator watches no Pods, so the
        # scheduler binding the agent pod wakes nothing, and the status keeps
        # the old message until the operator's next pass. So the pods
        # themselves, by the label the operator lists the agent's pods by, in
        # one read a line per pod: name, node, phase, deletion timestamp.
        # Like the operator's scan, a pod being deleted is skipped, and so is
        # one that is Failed or Succeeded (an evicted or admission-rejected
        # pod keeps its node until pod GC, and Recreate does not wait for
        # it). The pod the condition names ("Pod <name> cannot be
        # scheduled..."), while it is listed and live, decides alone; once it
        # is not, the first live pod bound to a node does. Bound is the end of
        # what this gate forgives; the agent Deployment's rollout gate, which
        # runs next, decides the rest. Nothing listed, no live pod bound, or a
        # dropped read (the read swallows the failure) is one more poll like
        # any other here.
        named_pod="${condition#"${CR_READY_REASON_POD_UNSCHEDULABLE}: Pod "}"
        named_pod="${named_pod%% *}"
        pod_name="" pod_node="" fallback_name="" fallback_node="" named_listed=""
        agent_pods="$(kubectl get pods -n "${NAMESPACE}" -l "app=${AGENT_DEPLOYMENT_NAME}" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.nodeName}{"\t"}{.status.phase}{"\t"}{.metadata.deletionTimestamp}{"\n"}{end}' 2>/dev/null || true)"
        while IFS= read -r pod_line; do
          [[ "${pod_line}" == *$'\t'*$'\t'*$'\t'* ]] || continue
          line_name="${pod_line%%$'\t'*}"
          rest="${pod_line#*$'\t'}"
          line_node="${rest%%$'\t'*}"
          rest="${rest#*$'\t'}"
          line_phase="${rest%%$'\t'*}"
          line_deleted="${rest#*$'\t'}"
          [ -n "${line_deleted}" ] && continue
          case "${line_phase}" in Failed | Succeeded) continue ;; esac
          if [ "${line_name}" = "${named_pod}" ]; then
            named_listed="true" pod_name="${line_name}" pod_node="${line_node}"
            break
          fi
          if [ -n "${line_node}" ] && [ -z "${fallback_node}" ]; then
            fallback_name="${line_name}" fallback_node="${line_node}"
          fi
        done <<<"${agent_pods}"
        if [ -z "${named_listed}" ]; then
          pod_name="${fallback_name}" pod_node="${fallback_node}"
        fi
        [ -n "${pod_node}" ] && break
      fi
      if [ "${phase}" = "${CR_PHASE_DEGRADED}" ] && [ "${rereads}" -lt "${MODE_NEXT_UNSCHEDULABLE_ATTEMPTS}" ] &&
        [[ "${condition}" == "${CR_READY_REASON_POD_UNSCHEDULABLE}: "* ]] && [[ "${condition}" =~ ${SCHEDULER_CAPACITY_SHORTFALL_RE} ]]; then
        rereads=$((rereads + 1))
        capacity="true"
        echo "${PLATFORM_AGENT_CR_NAME} is ${phase} after ${what} on a pod waiting for CPU or memory, $((SECONDS - gate_start))s in; re-read ${rereads}/${MODE_NEXT_UNSCHEDULABLE_ATTEMPTS} in ${MODE_NEXT_POLL_SECONDS}s (Ready condition: ${condition})"
        sleep "${MODE_NEXT_POLL_SECONDS}"
        continue
      fi
    fi
    if [ -n "${capacity}" ]; then
      echo "the wait for capacity ended after ${rereads} re-reads, $((SECONDS - gate_start))s${unanswered}"
    elif [ "${rereads}" -gt 0 ]; then
      echo "${PLATFORM_AGENT_CR_NAME} answered after ${rereads} reads that returned nothing, $((SECONDS - gate_start))s"
    fi
    echo "ERROR: ${PLATFORM_AGENT_CR_NAME} is ${phase:-unphased} after ${what}; Ready condition: ${condition:-none}"
    echo "--- provisioning Job pod logs ---"
    kubectl logs -n "${NAMESPACE}" -l "${A2A_PROVISION_JOB_SELECTOR}" --tail="${MODE_NEXT_DIAG_LOG_LINES}" || true
    dump_mode_next_state
    exit 1
  done
  if [ -n "${pod_node}" ]; then
    echo "✓ the wait for capacity handed off after ${rereads} re-reads, $((SECONDS - gate_start))s: the agent pod was scheduled on ${pod_node} (${pod_name}); the rollout gate decides from here (${PLATFORM_AGENT_CR_NAME} still reads ${phase}; Ready condition: ${condition})"
    return 0
  fi
  if [ -n "${capacity}" ]; then
    echo "✓ the wait for capacity cleared after ${rereads} re-reads, $((SECONDS - gate_start))s"
  elif [ "${rereads}" -gt 0 ]; then
    echo "✓ ${PLATFORM_AGENT_CR_NAME} answered after ${rereads} reads that returned nothing, $((SECONDS - gate_start))s"
  fi
  echo "✓ ${PLATFORM_AGENT_CR_NAME} is ${phase:-unphased} after ${what} (Ready condition: ${condition:-none})"
}

if [ "${EVAL_MODE_NEXT:-}" = "1" ]; then
  STEP_START=$SECONDS
  MODE_NEXT_START=$SECONDS
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Switching ${PLATFORM_AGENT_CR_NAME} to mode: next (EVAL_MODE_NEXT=1) ==="
  # The mode and the session cap in one patch, so the first render -- and the
  # first provisioning Job -- sees both. Section 2b sized the cap so the
  # budget that Job measures, which counts the rendered bridge's workers, fits
  # the floor; the arithmetic is in the log for a reader of the artifact who
  # finds a non-default maxSessions on the eval CR.
  echo "setting spec.harness.tuning.maxSessions=${MODE_NEXT_MAX_SESSIONS} so the ${A2A_TASKS_FLOOR}-wide TASKS holds the ${MODE_NEXT_BRIDGE_CONCURRENCY}-worker bridge's budget (${MODE_NEXT_MAX_SESSIONS}*${A2A_SESSION_CONSUMERS} + ${A2A_RESERVE_FIXED} + ${A2A_RESERVE_PER_WORKER}*${MODE_NEXT_BRIDGE_CONCURRENCY} <= ${A2A_TASKS_FLOOR})"
  # The format is a named constant, which is the point of it (SC2059 wants a literal).
  # shellcheck disable=SC2059
  printf -v MODE_NEXT_PATCH "${MODE_NEXT_PATCH_FORMAT}" "${MODE_NEXT_MAX_SESSIONS}"
  GEN_BEFORE="$(kubectl get "deployment/${AGENT_DEPLOYMENT_NAME}" -n "${NAMESPACE}" -o jsonpath='{.metadata.generation}')"
  kubectl patch platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" --type merge -p "${MODE_NEXT_PATCH}"
  wait_agent_generation_past "${GEN_BEFORE}" "the mode patch"
  echo "managed .env now reads:"
  # Whole, not grepped for the mode key: the key is named in exactly two
  # places by design (tests/test_mode_grep.py), and this script is not one.
  kubectl get configmap "${PLATFORM_AGENT_CR_NAME}-config" -n "${NAMESPACE}" -o jsonpath='{.data.managed\.env}' || true
  echo

  gate_mode_next_rollout "statefulset/${PLATFORM_AGENT_CR_NAME}-a2a-nats"
  gate_mode_next_rollout "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-callout"

  # The one run, against a bus with no streams. Its budget already counts
  # the bridge's workers, since the operator renders the bridge from this
  # render on, so no later render re-measures it against the stream it
  # creates and there is no refusal for gate_cr_not_degraded to catch
  # (#2077).
  wait_provision_job "the mode patch"

  gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"

  # The second roll: the operator adds the bridge to the agent pod once it has
  # recorded the bus provisioned, a reconcile or two after the Job completes.
  # Wait for the template to carry it, then gate that rollout like the first,
  # so the bridge log loop below starts against a pod that has the container
  # rather than spending its budget on scheduling (#2414) and image pulls.
  BRIDGE_TEMPLATE_START=$SECONDS
  for _ in $(seq 1 "${MODE_NEXT_GENERATION_ATTEMPTS}"); do
    case " $(kubectl get "deployment/${AGENT_DEPLOYMENT_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.template.spec.containers[*].name}') " in
      *" ${BRIDGE_SIDECAR_NAME} "*) break ;;
    esac
    sleep "${MODE_NEXT_POLL_SECONDS}"
  done
  case " $(kubectl get "deployment/${AGENT_DEPLOYMENT_NAME}" -n "${NAMESPACE}" -o jsonpath='{.spec.template.spec.containers[*].name}') " in
    *" ${BRIDGE_SIDECAR_NAME} "*) ;;
    *)
      echo "ERROR: the operator never added the ${BRIDGE_SIDECAR_NAME} container to deployment/${AGENT_DEPLOYMENT_NAME} after the bus was provisioned" >&2
      dump_mode_next_state
      exit 1
      ;;
  esac
  echo "✓ ${BRIDGE_SIDECAR_NAME} in the agent pod template $((BRIDGE_TEMPLATE_START - MODE_NEXT_START))s..$((SECONDS - MODE_NEXT_START))s after the patch"
  gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"

  # The inject door. The operator renders its Service and token Secret on the
  # same reconcile as the gateway, so this is normally immediate; when it is
  # not, the operator was deployed without the flag, and the eval would find
  # no Service to port-forward to.
  INJECT_GATE_START=$SECONDS
  for _ in $(seq 1 "${MODE_NEXT_GENERATION_ATTEMPTS}"); do
    kubectl get "service/${A2A_INJECT_NAME}" "secret/${A2A_INJECT_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1 && break
    sleep "${MODE_NEXT_POLL_SECONDS}"
  done
  if ! kubectl get "service/${A2A_INJECT_NAME}" "secret/${A2A_INJECT_NAME}" -n "${NAMESPACE}"; then
    echo "ERROR: the inject door's Service and token Secret (${A2A_INJECT_NAME}) never appeared. The operator renders them only with" >&2
    echo "       ${A2A_INJECT_BACKEND_ENV_VAR}=${A2A_INJECT_BACKEND_ON} in its own environment, which step 5 sets through operator.extraEnv." >&2
    echo "--- operator env ---"
    kubectl get "deployment/${OPERATOR_DEPLOYMENT_NAME}" -n "${NAMESPACE}" -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' || true
    dump_mode_next_state
    exit 1
  fi
  echo "✓ inject door rendered (${A2A_INJECT_NAME} Service, and token Secret with key ${A2A_INJECT_TOKEN_KEY} for the eval) $((INJECT_GATE_START - MODE_NEXT_START))s..$((SECONDS - MODE_NEXT_START))s after the patch"

  # Ready is not consuming: the bridge sweeps its registry and binds its
  # durable consumer after the container starts, and only its own log line
  # says the bus has an executor.
  BRIDGE_LOG_START=$SECONDS
  BRIDGE_CONSUMING=""
  for _ in $(seq 1 "${MODE_NEXT_BRIDGE_LOG_ATTEMPTS}"); do
    BRIDGE_CONSUMING="$(kubectl logs -n "${NAMESPACE}" "deployment/${AGENT_DEPLOYMENT_NAME}" -c "${BRIDGE_SIDECAR_NAME}" --tail="${MODE_NEXT_DIAG_LOG_LINES}" 2>/dev/null | grep -F "${BRIDGE_CONSUMING_LOG_MSG}" | grep -F "${BRIDGE_CONSUMING_LOG_PROFILE}" | grep -F "${BRIDGE_CONSUMING_LOG_EXECUTOR}" | tail -1 || true)"
    [ -n "${BRIDGE_CONSUMING}" ] && break
    sleep "${MODE_NEXT_POLL_SECONDS}"
  done
  if [ -z "${BRIDGE_CONSUMING}" ]; then
    echo "ERROR: the ${BRIDGE_SIDECAR_NAME} sidecar never logged ${BRIDGE_CONSUMING_LOG_MSG} with ${BRIDGE_CONSUMING_LOG_PROFILE} and ${BRIDGE_CONSUMING_LOG_EXECUTOR}"
    echo "       The lane runs the bridge's default executor, ${BRIDGE_EXECUTOR_EXPECTED}. A consuming line with another executor means the"
    echo "       bridge started without the agent container's API_SERVER_KEY, or the operator sets ${A2A_BRIDGE_EXECUTOR_OPERATOR_ENV_VAR}."
    echo "--- ${BRIDGE_SIDECAR_NAME} log ---"
    kubectl logs -n "${NAMESPACE}" "deployment/${AGENT_DEPLOYMENT_NAME}" -c "${BRIDGE_SIDECAR_NAME}" --tail="${MODE_NEXT_DIAG_LOG_LINES}" || true
    kubectl logs -n "${NAMESPACE}" "deployment/${AGENT_DEPLOYMENT_NAME}" -c "${BRIDGE_SIDECAR_NAME}" --previous --tail="${MODE_NEXT_DIAG_LOG_LINES}" 2>/dev/null || true
    kubectl describe "deployment/${AGENT_DEPLOYMENT_NAME}" -n "${NAMESPACE}" || true
    dump_mode_next_state
    exit 1
  fi
  echo "✓ bridge consuming $((BRIDGE_LOG_START - MODE_NEXT_START))s..$((SECONDS - MODE_NEXT_START))s after the patch: ${BRIDGE_CONSUMING}"

  # Last, for the reason in this step's header: the bucket it needs exists by
  # now, and the backoff it may still be in has been running against the two
  # rollouts above rather than in front of them.
  gate_mode_next_rollout "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-verifier"

  # What the run has to show for itself, for the artifact log: the CR status,
  # the stack the mode rendered, the ungated gateway, and the entrypoint's
  # account of the A2A skill overlay landing (or not) in the agent pod.
  kubectl get platformagent "${PLATFORM_AGENT_CR_NAME}" -n "${NAMESPACE}" -o yaml | sed -n '/^status:/,$p' || true
  kubectl get statefulsets,deployments,jobs,pods,networkpolicies,pvc -n "${NAMESPACE}" -l "${A2A_PART_OF_SELECTOR}" || true
  echo "--- A2A gateway (reported, not gated; see the comment above this step) ---"
  kubectl get "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-gateway" -n "${NAMESPACE}" || true
  kubectl logs -n "${NAMESPACE}" "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-gateway" --all-containers --tail="${MODE_NEXT_REPORT_LOG_LINES}" 2>/dev/null || true
  kubectl logs -n "${NAMESPACE}" "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-gateway" --all-containers --previous --tail="${MODE_NEXT_REPORT_LOG_LINES}" 2>/dev/null || true
  # kubectl's "unable to retrieve container logs" for a crashed container
  # arrives on stdout without a newline; keep the next header on its own line.
  echo
  echo "--- agent entrypoint lines about the mode and the A2A overlay ---"
  kubectl logs -n "${NAMESPACE}" "deployment/${AGENT_DEPLOYMENT_NAME}" --all-containers --tail="${MODE_NEXT_ENTRYPOINT_SCAN_LINES}" 2>/dev/null | grep -i "a2a\|overlay\|mode" | head -"${MODE_NEXT_ENTRYPOINT_MATCH_LINES}" || true
  echo "✓ mode: next rollout finished in $((SECONDS - STEP_START))s"
fi

# ─── 7. Agent API Connectivity Verification ──────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying Platform Agent API Connectivity ==="
API_KEY="$(kubectl get secret platform-agent-secrets -n "${NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"

# On cold autoscaling pools the API-server tunnel behind `kubectl port-forward`
# drops mid-request ("error: lost connection to pod" with the gateway pod
# healthy throughout), and a dead port-forward never comes back on its own —
# so every attempt gets a fresh tunnel, and only the response decides health.
PF_PID=""
cleanup_pf_and_dump() {
  kill "${PF_PID:-}" 2>/dev/null || true
  dump_prow_artifacts_on_failure
}
trap cleanup_pf_and_dump EXIT

CONNECTIVITY_ATTEMPTS=5
CONNECTIVITY_OK="false"
: >/tmp/pf-8642.log
for ((attempt = 1; attempt <= CONNECTIVITY_ATTEMPTS; attempt++)); do
  # Kill any previous tunnel and start a fresh one; the log is appended so a
  # failure dump shows every attempt, not just the last.
  if [ -n "${PF_PID}" ]; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
  fi
  echo "--- port-forward attempt ${attempt}/${CONNECTIVITY_ATTEMPTS} ---" >>/tmp/pf-8642.log
  kubectl port-forward svc/platform-agent -n "${NAMESPACE}" 8642:8642 >>/tmp/pf-8642.log 2>&1 &
  PF_PID=$!

  echo "Waiting for platform-agent port-forward on port 8642 (attempt ${attempt}/${CONNECTIVITY_ATTEMPTS})..."
  for _ in {1..30}; do
    if nc -z localhost 8642 2>/dev/null; then
      break
    fi
    sleep 1
  done

  HEALTH_RESP="$(curl -s --max-time 120 -X POST http://localhost:8642/v1/responses \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"model": "model-default", "input": "ping"}' || true)"

  if [[ "$HEALTH_RESP" == *"output"* || "$HEALTH_RESP" == *"assistant"* || "$HEALTH_RESP" == *"pong"* ]]; then
    CONNECTIVITY_OK="true"
    break
  fi
  if [ -z "${HEALTH_RESP}" ]; then
    FAIL_REASON="empty response after port-forward drop"
  else
    FAIL_REASON="unexpected response: ${HEALTH_RESP}"
  fi
  if [ "${attempt}" -lt "${CONNECTIVITY_ATTEMPTS}" ]; then
    echo "connectivity attempt ${attempt}/${CONNECTIVITY_ATTEMPTS} failed: ${FAIL_REASON}; respawning tunnel"
  else
    echo "connectivity attempt ${attempt}/${CONNECTIVITY_ATTEMPTS} failed: ${FAIL_REASON}"
  fi
done

kill "${PF_PID:-}" 2>/dev/null || true
trap dump_prow_artifacts_on_failure EXIT

if [ "${CONNECTIVITY_OK}" = "true" ]; then
  echo "✓ Agent API Server responded successfully in $((SECONDS - STEP_START))s!"
else
  echo "ERROR: Platform Agent API server connectivity check failed after ${CONNECTIVITY_ATTEMPTS} attempts!"
  echo "Response received: ${HEALTH_RESP}"
  echo "=== Debug: Port Forward Log (tail) ==="
  tail -n 40 /tmp/pf-8642.log 2>/dev/null || true
  echo "=== Debug: Kubernetes Workloads in Namespace ${NAMESPACE} ==="
  kubectl get pods,svc -n "${NAMESPACE}" || true
  exit 1
fi

TOTAL_DURATION=$((SECONDS - START_TIME))
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deployment Ready in Namespace: ${NAMESPACE} (Total Duration: ${TOTAL_DURATION}s) ==="
