#!/usr/bin/env bash
# Entrypoint for the credential-proxy container.
#
# Four peer services live here, not one service with helpers. They share a
# container because they all need credentials, and credentials are deliberately
# kept out of the agent sandbox — not because any of them belongs to another:
#
#   credential_proxy.py   executes credentialed CLIs on behalf of the sandbox
#   envoy                 fronts the credential proxy on loopback
#   k8s-event-watcher     watches cluster API servers and reports events
#   drift-detector        reads GKE admin-activity audit records from Pub/Sub
#                         and reports the ones a person made outside git
#
# CREDENTIAL_PROXY_ROLE selects which of them start, because they no longer all
# run in the same pod. It is the same variable and the same three values
# credential_proxy.py's resolve_role() reads; this script decides which
# processes to launch and that function decides which halves of the runtime to
# serve. `broker` is the credential pod: Envoy and the credential runtime, which
# the sandbox reaches over a Service. `api-proxy` is what is left in the gateway
# pod — the watcher and the drift detector, which both post to the Session KV
# server on that pod's loopback, and the API authenticator, which forwards to
# the Hermes gateway on the same loopback. None of those is a credential path
# the agent container can drive, which is the point of the split. `combined` is
# the sidecar arrangement and stays the default so an image paired with an older
# operator behaves as it did.
#
# They differ in how their failure is treated. Envoy and the credential runtime
# are the container's reason to exist: if either dies the agent loses every
# credentialed command, so their exit ends the container and Kubernetes
# restarts it. The watcher and the drift detector are best-effort observability
# — losing either must not take the credential path down with it, so each is
# supervised and restarted in place instead. They are also the two that can be
# switched off deliberately, in opposite directions: EVENT_WATCHER_ENABLED=false
# skips the watcher, which is the emergency stop for an event storm, while the
# detector starts only when DRIFT_DETECTOR_ENABLED says so, because the Pub/Sub
# subscription it reads exists only where an install asked for it. See
# event_watcher_disabled and drift_detector_enabled below.
set -euo pipefail

# The sandbox runs as a different user (see the UID constants in the operator's
# platformagent_manifests.go) and shares only the agent PVC with this container.
# Proxied commands run here but write there — a clone, a commit, a kubeconfig pin
# in a profile home — and the sandbox has to be able to change what they leave
# behind. The shared fsGroup gives it the group; this gives the group write.
# Credential state lives on this container's own emptyDir volumes, which nothing
# else mounts, so the wider mode does not widen who can read a credential.
umask 0002

# Below the umask, and nothing goes above it: `tests/test_startup_umask.py`
# asserts the umask is the first line here that could create a file, because one
# sitting under a `mkdir` reads as the control while doing none of its job.
#
# Exported, not just assigned: the runtime reads the same variable, and an
# unset one would otherwise reach it as its own default rather than as the one
# defaulted here. Two defaults that have to agree is a way for them not to.
export CREDENTIAL_PROXY_ROLE="${CREDENTIAL_PROXY_ROLE:-combined}"
case "${CREDENTIAL_PROXY_ROLE}" in
  combined | broker | api-proxy) ;;
  *)
    echo "start-services: unknown CREDENTIAL_PROXY_ROLE=${CREDENTIAL_PROXY_ROLE}" >&2
    exit 1
    ;;
esac

# Watcher restart policy. The watcher is retried in place rather than being
# allowed to end the container, so these bound how hard a permanently broken
# one is retried and how long it must survive to count as recovered.
WATCHER_RETRY_MIN_SECONDS="${WATCHER_RETRY_MIN_SECONDS:-10}"
WATCHER_RETRY_MAX_SECONDS="${WATCHER_RETRY_MAX_SECONDS:-120}"
WATCHER_HEALTHY_RUN_SECONDS="${WATCHER_HEALTHY_RUN_SECONDS:-120}"

# Drift detector restart policy, the same shape and the same reasoning as the
# watcher's above: retried in place, best-effort, never allowed to end the
# container. The values match deliberately — both processes fail for the same
# kinds of reason (a credential that is not there yet, an API that is briefly
# unreachable), and two different backoff curves in one container would be a
# thing to explain rather than a thing to tune.
DRIFT_RETRY_MIN_SECONDS="${DRIFT_RETRY_MIN_SECONDS:-10}"
DRIFT_RETRY_MAX_SECONDS="${DRIFT_RETRY_MAX_SECONDS:-120}"
DRIFT_HEALTHY_RUN_SECONDS="${DRIFT_HEALTHY_RUN_SECONDS:-120}"

# Consecutive short exits before the detector's failure is stated as a
# consequence rather than logged as an exit. Three, as for the watcher: one is
# ordinary during startup, and by the third the cause is not transient.
DRIFT_SHORT_EXIT_ALERT_COUNT="${DRIFT_SHORT_EXIT_ALERT_COUNT:-3}"

# Where the watcher and the detector post. Not overridable, unlike the values
# above: it is the Session KV server on this pod's loopback, fixed by where those
# two processes run rather than by anything an operator chooses. Named rather
# than written into the two flag lists below because a literal there would be the
# one piece of this container's internal wiring that is only discoverable by
# reading a command line — and because the same address written twice is the
# second copy nobody updates.
readonly KV_DAEMON_HOST=127.0.0.1
readonly KV_DAEMON_PORT=8699
readonly KV_DAEMON_URL="http://${KV_DAEMON_HOST}:${KV_DAEMON_PORT}"

# How long to wait for that daemon to start listening before launching the
# detector anyway. This container is a native sidecar, so it starts before the
# platform-agent container the daemon runs in, and the detector's startup check
# against the daemon is fatal by design — it refuses to run against a daemon
# that does not advertise the drift kind, because such a daemon answers 200 to
# every record while misfiling it. Without a wait the detector therefore exits
# on connection-refused two or three times on every cold start, and the third
# short exit prints the ALERT below, which says out-of-band changes are not
# being detected when in fact nothing is wrong yet.
#
# Five minutes because the thing being waited for is the whole agent image
# coming up, not a socket bind. Overshooting costs a silent detector for as long
# as the daemon is genuinely down; undershooting costs the false ALERT this
# exists to remove, on an install where everything is working.
DRIFT_DAEMON_WAIT_SECONDS="${DRIFT_DAEMON_WAIT_SECONDS:-300}"
DRIFT_DAEMON_POLL_SECONDS="${DRIFT_DAEMON_POLL_SECONDS:-2}"

# The floor under every tunable above. All of them arrive from the environment,
# and `spec.deployment.env` reaches this container unfiltered, so a hand-written
# value is the realistic source of a bad one. Every way of getting one wrong ends
# with the same symptom -- a container that stays Ready with nothing in the log --
# which is why they are corrected here rather than acted on where they are read.
#
# Zero is the dangerous value in a backoff, because it survives every check the
# retry loops already make: `sleep 0` returns at once, `0 * 2` stays 0, and the
# cap comparison never lifts it, so a detector or watcher that exits quickly is
# re-exec'd as fast as the kernel allows, for the life of the pod, in the same
# container as the API authenticator. A zero daemon poll never advances the wait
# loop's counter, so the detector is never launched at all — silent in the worst
# way, because a process that never started has no short exits and the
# supervisor's ALERT below cannot fire either.
#
# A non-numeric value fails harder and just as quietly. `sleep abc` returns
# non-zero and errexit ends the supervisor subshell; worse, the bare-word
# comparisons these feed (`[[ "${ran}" -ge "${WATCHER_HEALTHY_RUN_SECONDS}" ]]`)
# treat the value as a variable name, so under `set -u` it is a fatal
# "abc: unbound variable" on the loop's first pass.
#
# A leading zero is the quiet one. It satisfies `^[0-9]+$`, so a check that only
# tested the shape would pass it through, and then every later `$(( ))` that
# touches it fails as a bad octal literal — `08: value too great for base` — which
# under errexit is the same dead supervisor by a third route. Normalising here is
# the fix; testing for it at each use site is the bug waiting to be reintroduced.
#
# Correct rather than reject. These are an escape hatch for a slow cluster, not a
# configuration surface anyone is expected to get right, and a container that
# refuses to start over a mistyped backoff is a worse outcome than one that backs
# off differently than intended. A substitution says so on stderr so the
# difference is discoverable; a normalisation does not, because `08` and `8` are
# the same request.
# shellcheck disable=SC2034  # read indirectly, as clamp_at_least's default floor name
readonly MIN_SETTING_VALUE=1

# Rewrite the *named* variable, in place, to a decimal integer of at least the
# value held by $2 (default MIN_SETTING_VALUE). By name on both sides because the
# point is to correct what the rest of the script reads, and because the message
# has to name the setting that was wrong and the setting that set the floor —
# which are not the same one when a ceiling is raised to meet its minimum.
clamp_at_least() {
  local name="$1"
  local floor_name="${2:-MIN_SETTING_VALUE}"
  local floor="${!floor_name}"
  local value="${!name}"

  # Short-circuit order matters: the arithmetic on the right is only reached for
  # a value the pattern already proved is all digits.
  if [[ "${value}" =~ ^[0-9]+$ ]] && [[ "$((10#${value}))" -ge "${floor}" ]]; then
    printf -v "${name}" '%s' "$((10#${value}))"
    return 0
  fi

  printf -v "${name}" '%s' "${floor}"
  echo "start-services: ${name}=${value} is not usable (minimum ${floor_name}=${floor}); using ${floor}" >&2
}

clamp_at_least WATCHER_RETRY_MIN_SECONDS
clamp_at_least DRIFT_RETRY_MIN_SECONDS
# The ceilings are floored at their own minimum, not at MIN_SETTING_VALUE: a
# maximum below the minimum makes the cap line drag every backoff back down to it
# on the second failure, which is the same hot loop by a longer route.
clamp_at_least WATCHER_RETRY_MAX_SECONDS WATCHER_RETRY_MIN_SECONDS
clamp_at_least DRIFT_RETRY_MAX_SECONDS DRIFT_RETRY_MIN_SECONDS
clamp_at_least DRIFT_DAEMON_POLL_SECONDS
clamp_at_least DRIFT_DAEMON_WAIT_SECONDS
# Not intervals — a run length and a count — but read by the same bare-word
# comparisons from the same unfiltered environment, and they fail worse than the
# ones above: the supervisor dies on its first pass rather than backing off wrong,
# before the process has run once.
clamp_at_least WATCHER_HEALTHY_RUN_SECONDS
clamp_at_least DRIFT_HEALTHY_RUN_SECONDS
clamp_at_least DRIFT_SHORT_EXIT_ALERT_COUNT

# How long terminate() waits for a signalled process to exit before killing the
# subshell supervising it and returning.
#
# It has to wait at all because this script is the container's PID 1: the
# `wait -n` at the bottom of the file is its last command, so terminate()
# returning is the script exiting, and the kernel SIGKILLs everything left in the
# PID namespace the moment it does. Delivering SIGTERM without waiting for it
# delivers the signal and not the shutdown.
#
# Fifteen seconds because the detector budgets ten to settle its in-flight
# records (settleGracePeriod, k8s-operator/cmd/drift-detector/subscriber.go) and
# the pod takes Kubernetes' default thirty-second grace period, so this leaves
# half of it unspent for the credential runtime and Envoy. The poll is whole
# seconds: the drain is bounded by the settle, not by how often it is checked.
readonly SHUTDOWN_DRAIN_SECONDS=15
readonly SHUTDOWN_DRAIN_POLL_SECONDS=1

# Where the watcher keeps its dedup snapshots. Without them the cache starts
# empty on every restart, and an empty cache is not a neutral state: the
# informer's initial LIST replays every event still inside the API server's TTL
# (an hour on GKE by default), so a restart re-reports incidents that were
# already triaged. The supervisor below restarts the watcher in place, which
# makes that a routine occurrence rather than a rare one.
#
# The data volume, not the container's own state directory: that one is a 16Mi
# in-memory emptyDir, so it would lose the cache on exactly the pod restarts
# that matter most. The watcher appends the profile name per cluster, since
# each cluster keeps its own cache and they cannot share a file.
WATCHER_DEDUP_DIR="${WATCHER_DEDUP_DIR:-${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/event-watcher}"

# How long a failure stays suppressed after its last sighting. The window
# SLIDES: every fresh observation pushes the deadline out again, so a workload
# that keeps failing is reported once and then stays quiet. The window only
# expires after a genuine gap, and when it does the incident is rebuilt from
# scratch — new session, new chat thread, count back to 1.
#
# That is why the binary's own 5m default is the wrong value here rather than
# merely a conservative one. The kubelet's image-pull and crash-loop backoffs
# both cap at 300s, so a steadily-failing pod re-reports at almost exactly the
# threshold and clears it or misses it on delivery jitter alone. The customer-
# visible result is the same broken image arriving as an unrelated-looking new
# alert every few minutes, with nothing tying the copies together.
#
# 24h is chosen over anything shorter because a broken deploy is not a
# minutes-scale event. An unresolvable image reference, a missing Secret or a
# node that will not come back stays broken until a human acts, and the useful
# alert cadence for "still broken, nobody has fixed it" is daily, not hourly.
# The cost is the other side of the same coin, and it is real: a failure that
# genuinely clears and returns later the same day is folded into the original
# incident instead of opening a new one, and the agent is not woken for it.
# A fleet whose failures resolve and recur within a shift wants a smaller value.
#
# Overridable because the right value depends on the fleet's failure mix, and
# an operator should not have to rebuild the image to find out.
WATCHER_DEDUP_WINDOW="${WATCHER_DEDUP_WINDOW:-24h}"

# Leading-edge debounce for the crash-loop family: how many times kubelet must
# report the same BackOff before it is treated as an incident rather than a
# startup race that will clear on its own. Passed explicitly even though it
# matches the binary's own default, because the value is the kind of thing an
# operator tunes per install — a cluster with slow-starting workloads wants it
# higher — and threading it through an env var means doing so does not require
# rebuilding the image. Set to 1 to restore firing on the first event.
WATCHER_BACKOFF_MIN_COUNT="${WATCHER_BACKOFF_MIN_COUNT:-3}"

# The same debounce for the half of the image-pull family that self-clears —
# registry rate limits, 5xx, connection timeouts. Only failures the watcher
# positively recognises as transient are held; a bad tag, and any wording the
# classifier does not recognise, still fire on the first event. Worth tuning
# separately from the crash-loop value: an install pulling from a rate-limited
# public registry wants it higher, and one where every pull is from a private
# mirror will rarely see it apply at all. Set to 1 to disable.
WATCHER_IMAGEPULL_TRANSIENT_MIN_COUNT="${WATCHER_IMAGEPULL_TRANSIENT_MIN_COUNT:-3}"

# The backstop for FailedScheduling when cluster-autoscaler has recorded no
# verdict on the pod: five failed scheduling attempts, a count rather than a
# time, since the scheduler retries on every cluster change and at least
# every five minutes. The autoscaler's own events take precedence over it: a
# NotTriggerScaleUp on the pod fires at any count, a TriggeredScaleUp holds at
# any count for WATCHER_SCALEUP_HOLD, and on a cluster with an autoscaler one
# or the other arrives seconds after the pod's first attempt. Both are in the
# --reason list below so the watcher reads them; neither is forwarded. Set to
# 1 to fire on the first event when no verdict is on record.
WATCHER_FAILEDSCHEDULING_MIN_COUNT="${WATCHER_FAILEDSCHEDULING_MIN_COUNT:-5}"

# How long a TriggeredScaleUp on a pod holds its FailedScheduling events,
# measured from the autoscaler's event to the FailedScheduling's own last
# sighting. A ceiling on the hold, not a delay on
# the alert: 15m is cluster-autoscaler's default node-provision timeout, and
# a pod still pending past it is reported on the count whatever the autoscaler
# last said. Raise it on a cluster whose node pools take longer to provision.
WATCHER_SCALEUP_HOLD="${WATCHER_SCALEUP_HOLD:-15m}"

# The agent image's interpreter. The credential-proxy image is built on
# agent-base by way of proxy-tools, so this is the same venv the agent runs
# from; the two proxy scripts import nothing outside the standard library, so
# what is in it does not matter, only that it exists. Named once because a wrong
# path here is a container that exits 127 with one line of output and no
# indication of which of the two callers below asked for it.
PROXY_PYTHON="${PROXY_PYTHON:-/opt/hermes/.venv/bin/python3}"

runtime_pid=""
envoy_pid=""
watcher_pid=""
drift_pid=""

# Shut down every supervised process and the subshell supervising it, given the
# supervisors' pids. Three things here are load-bearing and none is obvious: what
# gets signalled, that the wait exists at all, and what the wait watches.
#
# **Signal both, per supervisor.** The supervised process needs SIGTERM because it
# is the one with shutdown work to do. The supervisor needs it because otherwise
# its `while true` loop simply goes round again: the process exits, the loop
# computes `ran`, logs, and reaches its backoff `sleep`, and once that sleep
# elapses it launches a *replacement* underneath this very function. Its
# `trap 'exit 0' TERM` is deferred by bash until the foreground process returns,
# so signalling the two together costs the supervised process nothing -- it still
# gets its full shutdown, and the supervisor then leaves the loop instead of
# restarting it.
#
# Signalling the supervisor is not the same as ending it, which is why the kill
# is at the bottom rather than here. Ending the subshell reparents the process it
# launched to PID 1, after which `pkill -P` on the dead subshell's pid matches
# nothing; the process never sees SIGTERM and is SIGKILLed when this script exits.
#
# **The wait**, because a SIGTERM this script does not outlive is not a shutdown.
# The drift-detector's handler NACKs the records it has not finished with so that
# they redeliver rather than each costing a duplicate inject, and the watcher's
# writes its dedup snapshot so that a restart does not replay every event still
# inside the API server's TTL. Both take seconds; this script exits milliseconds
# after terminate() returns, and takes the PID namespace with it.
#
# **The wait watches the supervisors, not their children.** With the trap above, a
# supervisor exits only once its foreground process has returned, so its own exit
# is the completion signal and it arrives no earlier -- whereas its child set goes
# empty in the instant between the process returning and the trap running, which
# is a break before the loop has actually been left. Bash reaps its own background
# jobs, so `kill -0` on an exited supervisor fails rather than finding a zombie.
#
# **The bottom kill is SIGKILL**, and only reached when the budget ran out. A
# second SIGTERM would be deferred by the trap exactly as the first one was, so it
# could not end a supervisor whose process is ignoring the signal -- which is the
# only state a supervisor can still be in by the time the loop above gives up.
drain_supervised() {
  local pid
  local waited=0
  local running

  for pid in "$@"; do
    pkill -TERM -P "${pid}" 2>/dev/null || true
    kill -TERM "${pid}" 2>/dev/null || true
  done

  while [[ "${waited}" -lt "${SHUTDOWN_DRAIN_SECONDS}" ]]; do
    running=""
    for pid in "$@"; do
      if kill -0 "${pid}" 2>/dev/null; then
        running=yes
        break
      fi
    done
    [[ -n "${running}" ]] || break
    sleep "${SHUTDOWN_DRAIN_POLL_SECONDS}"
    waited=$((waited + SHUTDOWN_DRAIN_POLL_SECONDS))
  done

  for pid in "$@"; do
    kill -KILL "${pid}" 2>/dev/null || true
  done
}

terminate() {
  trap - EXIT INT TERM

  local supervisors=()
  [[ -z "${watcher_pid}" ]] || supervisors+=("${watcher_pid}")
  [[ -z "${drift_pid}" ]] || supervisors+=("${drift_pid}")
  # One shared drain budget, not one each: the two run concurrently and the pod's
  # grace period does not grow with the number of processes in the container.
  [[ "${#supervisors[@]}" -eq 0 ]] || drain_supervised "${supervisors[@]}"

  [[ -z "${runtime_pid}" ]] || kill "${runtime_pid}" 2>/dev/null || true
  [[ -z "${envoy_pid}" ]] || kill "${envoy_pid}" 2>/dev/null || true
}
trap terminate EXIT INT TERM

# Workload Identity Federation, when the proxy shares a pod with the shell
# sandbox. It has to run before anything that authenticates to GCP: the
# credential runtime's bootstrap command is `gcloud container clusters
# get-credentials`, and gcloud reads CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE at
# invocation. A no-op in the standalone placement, where the metadata server
# still answers. Not backgrounded and not tolerant of failure — a container that
# comes up without an identity serves nothing but errors.
write_wif_credentials() {
  "${PROXY_PYTHON}" /opt/defaults/scripts/wif_credentials.py
}

start_credential_runtime() {
  # The role reaches the runtime as the environment variable it already reads
  # (resolve_role), not as a flag: one spelling, and a container that sets the
  # variable without going through this script still gets the role it asked for.
  "${PROXY_PYTHON}" /opt/defaults/scripts/credential_proxy.py &
  runtime_pid=$!
}

start_envoy() {
  # The baked config binds 127.0.0.1, which is the access control for as long as
  # the broker is a sidecar in the agent's Pod. When the operator puts the broker
  # in a Pod of its own the listener has to accept the Pod IP, and the config is
  # baked into the image rather than rendered by the operator -- so the one line
  # that has to differ is substituted here rather than by shipping two configs
  # that would drift. The value is checked against a character class first: it
  # reaches sed, and sed would happily accept a replacement carrying its own
  # delimiter or newline.
  local envoy_config=/etc/envoy/envoy-credential-proxy.yaml
  if [[ -n "${CREDENTIAL_PROXY_ENVOY_ADDRESS:-}" ]]; then
    if [[ ! "${CREDENTIAL_PROXY_ENVOY_ADDRESS}" =~ ^[0-9a-fA-F.:]+$ ]]; then
      echo "CREDENTIAL_PROXY_ENVOY_ADDRESS is not an IP address" >&2
      exit 1
    fi
    local rendered=/tmp/envoy-credential-proxy.yaml
    sed "s|address: 127\.0\.0\.1|address: ${CREDENTIAL_PROXY_ENVOY_ADDRESS}|" \
      "${envoy_config}" >"${rendered}"
    # A substitution that silently matched nothing would leave the broker bound
    # to loopback in a Pod nothing else can reach, which reads as a hang.
    grep -q "address: ${CREDENTIAL_PROXY_ENVOY_ADDRESS}" "${rendered}"
    envoy_config="${rendered}"
  fi
  /usr/local/bin/envoy --config-path "${envoy_config}" --log-level info &
  envoy_pid=$!
}

# The emergency stop, written by the operator from the PlatformAgent's
# spec.harness.eventWatcher.enabled. Unset means enabled, so that an install
# whose operator predates the field keeps watching rather than going quiet on
# upgrade.
#
# Only a recognised falsey value disables the watcher; anything else unrecognised
# leaves it running and says so. Not for the CR path — `enabled` is a strict
# boolean there and admission rejects anything else before it reaches this
# script — but for the ways a value gets here without passing through the CRD: a
# hand-edited Deployment during an incident, and a container image paired with
# an operator that spells the value differently than this release expects.
#
# It fails towards watching because the two mistakes do not cost the same. A
# value that stops event ingestion is invisible — the container stays Ready, the
# log says nothing more, and the fleet simply never reports another incident —
# while one that leaves the watcher running is obvious the moment the next event
# arrives.
event_watcher_disabled() {
  case "${EVENT_WATCHER_ENABLED:-true}" in
    [Ff][Aa][Ll][Ss][Ee] | 0 | [Nn][Oo] | [Oo][Ff][Ff]) return 0 ;;
    [Tt][Rr][Uu][Ee] | 1 | [Yy][Ee][Ss] | [Oo][Nn]) return 1 ;;
    *)
      echo "start-services: EVENT_WATCHER_ENABLED=${EVENT_WATCHER_ENABLED:-} is not a recognised boolean; starting the k8s-event-watcher anyway. Use 'false' to disable it." >&2
      return 1
      ;;
  esac
}

start_event_watcher() {
  if event_watcher_disabled; then
    # Loud, and worded so it cannot be mistaken for the ALERT lines below: those
    # mean the watcher tried and failed, this one means somebody turned it off.
    # The pod log is where a reader of the container finds that out — the
    # readiness probe covers only the credential proxy, so a container with no
    # watcher in it looks exactly like a healthy one from outside.
    echo "start-services: k8s-event-watcher is DISABLED by configuration (EVENT_WATCHER_ENABLED=${EVENT_WATCHER_ENABLED:-}) — NO cluster events are being watched and no autonomous triage sessions will start. Set spec.harness.eventWatcher.enabled=true on the PlatformAgent to start watching again." >&2
    return 0
  fi

  # Flags are set here rather than passed as container arguments: they describe
  # how processes inside this container reach each other over loopback, which is
  # implementation detail rather than deployment configuration. The one value
  # that varies per install — the cluster's name — comes from the operator via
  # EVENT_WATCHER_CLUSTER_NAME, which it always sets. No default is applied
  # here on purpose: guessing a name would mislabel every payload and metric,
  # so an unset value should fail loudly in the watcher's own validation.
  #
  # --token-env names SESSION_KV_API_KEY rather than API_SERVER_KEY: the latter
  # is the loopback sentinel `cluster-internal-trusted`, which authenticates
  # nothing. The watcher refuses to start when the named variable is empty,
  # which is the behaviour we want — the Session KV server fails closed too.

  # Said once, up front, and in terms of the consequence: the watcher's own
  # error ("bearer token env var ... is empty") names a variable, not what
  # stops working, and it only reaches the ALERT below after three short exits.
  # An install upgraded from before this key existed is exactly the case that
  # lands here — see the backfill in upgrade.sh.
  if [ -z "${SESSION_KV_API_KEY:-}" ]; then
    echo "start-services: ALERT SESSION_KV_API_KEY is empty, so k8s-event-watcher cannot authenticate to the Session KV server and will exit on every start — NO cluster events are being watched. Add the key to the agent Secret (upgrade.sh backfills it; the chart and the Terraform composition generate it on a fresh install) and restart the pod." >&2
  fi

  # An empty value disables persistence, which is what should happen if the
  # directory cannot be created: the watcher still dedups in memory, and losing
  # snapshots must not cost us the watcher itself. Creating it here rather than
  # in the watcher keeps the failure at startup, where it is logged once,
  # instead of on every snapshot tick.
  dedup_persist=""
  if mkdir -p "${WATCHER_DEDUP_DIR}" 2>/dev/null; then
    dedup_persist="${WATCHER_DEDUP_DIR}/dedup.json"
  else
    echo "start-services: cannot create ${WATCHER_DEDUP_DIR}; the dedup cache will not survive a watcher restart, so recent incidents may be reported twice" >&2
  fi

  (
    # Leave the loop on SIGTERM instead of going round it again. bash runs a trap
    # between commands, so this one is deferred until the foreground watcher below
    # returns -- which is what makes it safe to signal the watcher and its
    # supervisor at the same time: the watcher gets its full shutdown, and the
    # supervisor then exits rather than reaching the backoff `sleep` and starting
    # a replacement underneath terminate()'s drain. See drain_supervised.
    trap 'exit 0' TERM
    delay="${WATCHER_RETRY_MIN_SECONDS}"
    consecutive=0
    while true; do
      started=$SECONDS
      /usr/local/bin/k8s-event-watcher \
        --cluster-name="${EVENT_WATCHER_CLUSTER_NAME:-}" \
        --profiles-dir="${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/profiles" \
        --dedup-persist="${dedup_persist}" \
        --dedup-window="${WATCHER_DEDUP_WINDOW}" \
        --in-cluster \
        --daemon-url="${KV_DAEMON_URL}" \
        --token-env=SESSION_KV_API_KEY \
        --owner=platform \
        --reason=Failed,FailedToDrainNode,CrashLoopBackOff,BackOff,ImagePullBackOff,ErrImagePull,OOMKilled,FailedScheduling,TriggeredScaleUp,NotTriggerScaleUp \
        --backoff-min-count="${WATCHER_BACKOFF_MIN_COUNT}" \
        --imagepull-transient-min-count="${WATCHER_IMAGEPULL_TRANSIENT_MIN_COUNT}" \
        --failedscheduling-min-count="${WATCHER_FAILEDSCHEDULING_MIN_COUNT}" \
        --scaleup-hold="${WATCHER_SCALEUP_HOLD}" || true
      ran=$(( SECONDS - started ))

      # A run long enough to have synced and served is treated as a fresh
      # start, so an occasional crash after hours of work does not inherit a
      # backoff earned days earlier. Anything shorter is a failure to start.
      if [[ "${ran}" -ge "${WATCHER_HEALTHY_RUN_SECONDS}" ]]; then
        delay="${WATCHER_RETRY_MIN_SECONDS}"
        consecutive=0
      else
        consecutive=$(( consecutive + 1 ))
      fi

      echo "start-services: k8s-event-watcher exited after ${ran}s (consecutive short exits: ${consecutive}); retrying in ${delay}s" >&2
      if [[ "${consecutive}" -ge 3 ]]; then
        # Loud, greppable, and states the consequence rather than the symptom.
        # Nothing else reports this: the container stays Ready by design, so a
        # watcher that can never start is otherwise indistinguishable from a
        # fleet with no incidents.
        echo "start-services: ALERT k8s-event-watcher has failed to start ${consecutive} times in a row — NO cluster events are being watched" >&2
      fi

      sleep "${delay}"
      # Exponential, capped: a permanent failure (bad RBAC, missing profiles
      # directory) should not hammer the API server every 10s forever.
      delay=$(( delay * 2 ))
      [[ "${delay}" -le "${WATCHER_RETRY_MAX_SECONDS}" ]] || delay="${WATCHER_RETRY_MAX_SECONDS}"
    done
  ) &
  watcher_pid=$!
}

# Whether the drift detector starts, written by the operator from the
# PlatformAgent's spec.harness.driftDetector.enabled.
#
# Unset means NOT started, which is the opposite of event_watcher_disabled
# above, and the asymmetry is the point rather than an oversight. The watcher
# needs nothing an install does not already have, so an install that says
# nothing should keep watching. The detector reads a Pub/Sub subscription that
# exists only where the drift-pubsub Terraform module was applied, so an
# install that says nothing has no subscription to read: starting it there
# gives a process that stays up and retries a failing pull for the life of the
# pod, filling the pod log without ever being able to work. It does not exit, so
# the supervisor below never sees it fail and the pod stays Ready throughout.
#
# It therefore fails towards not starting, including on a value it does not
# recognise, where the watcher's equivalent fails towards running. The two
# mistakes are not symmetric here either: a detector that stays off costs an
# install what it had before this feature existed, and one that starts without
# a subscription costs it a permanently failing process.
drift_detector_enabled() {
  case "${DRIFT_DETECTOR_ENABLED:-false}" in
    [Tt][Rr][Uu][Ee] | 1 | [Yy][Ee][Ss] | [Oo][Nn]) return 0 ;;
    [Ff][Aa][Ll][Ss][Ee] | 0 | [Nn][Oo] | [Oo][Ff][Ff]) return 1 ;;
    *)
      echo "start-services: DRIFT_DETECTOR_ENABLED=${DRIFT_DETECTOR_ENABLED:-} is not a recognised boolean; the drift-detector will NOT start and out-of-band changes will not be reported. Use 'true' to enable it." >&2
      return 1
      ;;
  esac
}

# Block until the Session KV server accepts a connection, or the deadline
# passes. A plain TCP connect rather than a GET /healthz: the detector makes
# that request itself and acts on the answer, and what is being waited for here
# is only the thing it cannot act on usefully — a daemon that is not listening
# yet because the container it runs in has not got there. A daemon that is
# listening and answers wrongly is a real incompatibility, and the detector's
# refusal to start, and eventually the ALERT, are the right report of it.
#
# Bash's /dev/tcp rather than curl, so that the wait does not depend on a
# package this image installs for other reasons.
#
# Returning after the deadline rather than giving up on the detector: a wait
# that outlived its usefulness should not be the reason drift goes undetected,
# and every failure path after this point is already reported.
wait_for_drift_daemon() {
  local waited=0
  while [[ "${waited}" -lt "${DRIFT_DAEMON_WAIT_SECONDS}" ]]; do
    if (exec 3<>"/dev/tcp/${KV_DAEMON_HOST}/${KV_DAEMON_PORT}") 2>/dev/null; then
      return 0
    fi
    sleep "${DRIFT_DAEMON_POLL_SECONDS}"
    waited=$((waited + DRIFT_DAEMON_POLL_SECONDS))
  done

  # Said once, and not as an ALERT: the supervisor's own ALERT is the report of
  # a detector that cannot start, and this line is the context for it — whether
  # the daemon was ever there matters when reading the exits that follow.
  echo "start-services: the Session KV server at ${KV_DAEMON_URL} was not listening after ${DRIFT_DAEMON_WAIT_SECONDS}s; starting drift-detector anyway" >&2
  return 0
}

start_drift_detector() {
  # Silent when off, unlike the watcher's disabled branch, which is loud. Off is
  # this one's ordinary state — every install that has not applied the
  # drift-pubsub module lands here — so a line saying so on every pod start
  # would be noise in the log of an install that is behaving exactly as
  # intended.
  drift_detector_enabled || return 0

  # Said once, up front, in terms of what stops working. The detector's own
  # error for an empty --project names the flag, and the flag's name is not
  # something an operator of this container set: the operator sets
  # spec.harness.projectId, and driftDetectorEnabled() in
  # platformagent_manifests.go only writes DRIFT_DETECTOR_ENABLED=true when that
  # and the other two harness fields are all present. Reaching here with one
  # empty therefore means the enable arrived by some route other than the CR —
  # a hand-edited Deployment during an incident is the realistic one.
  if [ -z "${DRIFT_DETECTOR_PROJECT_ID:-}" ] ||
    [ -z "${DRIFT_DETECTOR_CLUSTER_NAME:-}" ] ||
    [ -z "${DRIFT_DETECTOR_CLUSTER_LOCATION:-}" ]; then
    echo "start-services: ALERT the drift-detector is enabled but its project, cluster name or cluster location is empty, so it will exit on every start — NO out-of-band changes are being detected. Set spec.harness.projectId, .location and .clusterName on the PlatformAgent." >&2
  fi

  # The same token the watcher posts with, and the same failure: the detector
  # refuses to start when the named variable is empty. Warned about separately
  # from the watcher's copy of this check because the two processes are enabled
  # independently — an install running the detector with the watcher switched
  # off would otherwise get no warning at all.
  if [ -z "${SESSION_KV_API_KEY:-}" ]; then
    echo "start-services: ALERT SESSION_KV_API_KEY is empty, so drift-detector cannot authenticate to the Session KV server and will exit on every start — NO out-of-band changes are being detected. Add the key to the agent Secret (upgrade.sh backfills it; the chart and the Terraform composition generate it on a fresh install) and restart the pod." >&2
  fi

  # Built as an array because two of the flags have to be omitted rather than
  # passed empty, which a single backslash-continued command line cannot do.
  local detector_args=(
    --project="${DRIFT_DETECTOR_PROJECT_ID:-}"
    --in-cluster
    --cluster-name="${DRIFT_DETECTOR_CLUSTER_NAME:-}"
    --cluster-location="${DRIFT_DETECTOR_CLUSTER_LOCATION:-}"
    --profiles-dir="${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/profiles"
    # --daemon-url is what turns the inject on: empty means the detector
    # classifies, joins and logs, and escalates nothing. The three below travel
    # together for that reason, and are set here rather than left to the
    # operator because they describe how processes inside this container reach
    # each other, exactly as the watcher's equivalents do.
    --daemon-url="${KV_DAEMON_URL}"
    --token-env=SESSION_KV_API_KEY
    --owner=platform
  )

  # Omitted rather than passed empty. --subscription's own default is the
  # subscription name the drift-pubsub module creates, so passing "" would not
  # fall back to it — it would point the detector at a subscription called the
  # empty string, and the startup check would fail on a name nobody chose.
  if [[ -n "${DRIFT_DETECTOR_SUBSCRIPTION:-}" ]]; then
    detector_args+=(--subscription="${DRIFT_DETECTOR_SUBSCRIPTION}")
  fi

  # --gitops-managers has no default to lose, so this is cosmetic rather than
  # load-bearing: it keeps "not configured" spelled the same way as it is for
  # the subscription, and keeps an empty value out of the process table where
  # it reads like a manager named "".
  if [[ -n "${DRIFT_DETECTOR_GITOPS_MANAGERS:-}" ]]; then
    detector_args+=(--gitops-managers="${DRIFT_DETECTOR_GITOPS_MANAGERS}")
  fi

  (
    # The same trap as the watcher's supervisor, for the same reason: it is what
    # lets terminate() signal the detector and this subshell together without the
    # loop launching a replacement while the drain is still waiting.
    trap 'exit 0' TERM

    delay="${DRIFT_RETRY_MIN_SECONDS}"
    consecutive=0
    while true; do
      # Inside the subshell, not before it. This is the last launcher, so nothing
      # is waiting behind it to be started — what a foreground wait would delay is
      # the script's arrival at the `wait -n` on the credential path at the bottom
      # of this file. For as long as the deadline ran, a dead credential runtime
      # or Envoy would not end the container, and the restart that is their whole
      # failure contract would not happen.
      #
      # Inside the loop, and not only ahead of it, because the cold start is not
      # the only time the daemon is missing. It runs in the platform-agent
      # container and this one is a native sidecar that outlives it, so an agent
      # restart at any point in the pod's life takes the daemon away while this
      # supervisor keeps going. A wait that ran once would leave every relaunch
      # after that going straight to a connection-refused the detector treats as
      # fatal — the same three short exits and the same "NO out-of-band changes
      # are being detected" as a cold start without the wait, which is the thing
      # the wait exists to stop. Costs nothing when the daemon is up: the first
      # /dev/tcp probe connects and the function returns without sleeping.
      wait_for_drift_daemon

      # After the wait, not before it, so that time spent waiting is not counted
      # as run time. Counting it would let a long wait followed by an immediate
      # failure clear `consecutive` and suppress the ALERT.
      started=$SECONDS
      /usr/local/bin/drift-detector "${detector_args[@]}" || true
      ran=$(( SECONDS - started ))

      if [[ "${ran}" -ge "${DRIFT_HEALTHY_RUN_SECONDS}" ]]; then
        delay="${DRIFT_RETRY_MIN_SECONDS}"
        consecutive=0
      else
        consecutive=$(( consecutive + 1 ))
      fi

      echo "start-services: drift-detector exited after ${ran}s (consecutive short exits: ${consecutive}); retrying in ${delay}s" >&2
      if [[ "${consecutive}" -ge "${DRIFT_SHORT_EXIT_ALERT_COUNT}" ]]; then
        # The same reasoning as the watcher's ALERT above: the container stays
        # Ready either way, so a detector that can never start is otherwise
        # indistinguishable from a fleet where nobody has touched a cluster by
        # hand.
        echo "start-services: ALERT drift-detector has failed to start ${consecutive} times in a row — NO out-of-band changes are being detected" >&2
      fi

      sleep "${delay}"
      delay=$(( delay * 2 ))
      [[ "${delay}" -le "${DRIFT_RETRY_MAX_SECONDS}" ]] || delay="${DRIFT_RETRY_MAX_SECONDS}"
    done
  ) &
  drift_pid=$!
}

write_wif_credentials
start_credential_runtime
if [[ "${CREDENTIAL_PROXY_ROLE}" != "api-proxy" ]]; then
  start_envoy
fi
if [[ "${CREDENTIAL_PROXY_ROLE}" != "broker" ]]; then
  # The same guard as the watcher, and for the same reason: both post to the
  # Session KV server on this pod's loopback, which the broker pod does not run.
  start_event_watcher
  start_drift_detector
fi

# Only the credential-path services are waited on. The watcher and the drift
# detector are absent from this list deliberately — see the header. envoy_pid is
# empty in the api-proxy role, and `wait -n` rejects an empty argument, so it is
# expanded unquoted.
# shellcheck disable=SC2086
wait -n "${runtime_pid}" ${envoy_pid}
